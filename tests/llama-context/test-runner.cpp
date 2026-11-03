#include "test-runner.h"

#include "test-log.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <regex>
#include <utility>

#if defined(LLAMA_TEST_RENDERDOC)
#include <renderdoc_app.h>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <dlfcn.h>
#endif
#endif

renderdoc_capture::~renderdoc_capture() {
#if defined(LLAMA_TEST_RENDERDOC)
    if (owns_library_ && library_ != nullptr) {
#ifdef _WIN32
        FreeLibrary(static_cast<HMODULE>(library_));
#else
        dlclose(library_);
#endif
    }
#endif
}

bool renderdoc_capture::initialize(const std::string & capture_dir, std::string & error) {
#if !defined(LLAMA_TEST_RENDERDOC)
    (void) capture_dir;
    error = "test-llama-context was built without the RenderDoc SDK submodule";
    return false;
#else
    pRENDERDOC_GetAPI get_api = nullptr;

#ifdef _WIN32
    HMODULE module = GetModuleHandleA("renderdoc.dll");
    if (module != nullptr) {
        library_ = module;
        get_api = reinterpret_cast<pRENDERDOC_GetAPI>(GetProcAddress(module, "RENDERDOC_GetAPI"));
    }
#else
#ifdef __APPLE__
    const char * library_name = "librenderdoc.dylib";
#else
    const char * library_name = "librenderdoc.so";
#endif
    library_ = dlopen(library_name, RTLD_NOW | RTLD_NOLOAD);
    owns_library_ = library_ != nullptr;
    if (library_ != nullptr) {
        get_api = reinterpret_cast<pRENDERDOC_GetAPI>(dlsym(library_, "RENDERDOC_GetAPI"));
    }
#endif

    if (get_api == nullptr) {
        error = "RenderDoc is not loaded; launch the test from RenderDoc";
        return false;
    }

    void * api = nullptr;
    if (!get_api(eRENDERDOC_API_Version_1_1_2, &api) || api == nullptr) {
        error = "RenderDoc does not support API version 1.1.2";
        return false;
    }

    api_ = api;
    capture_dir_ = capture_dir;
    return true;
#endif
}

bool renderdoc_capture::start(const std::string & case_name, std::string & error) {
#if !defined(LLAMA_TEST_RENDERDOC)
    (void) case_name;
    error = "RenderDoc capture support is not available";
    return false;
#else
    auto * api = static_cast<RENDERDOC_API_1_1_2 *>(api_);
    if (api == nullptr) {
        error = "RenderDoc API is not initialized";
        return false;
    }

    std::string safe_case_name = case_name;
    for (char & c : safe_case_name) {
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_')) {
            c = '_';
        }
    }

    const std::filesystem::path path = std::filesystem::path(capture_dir_) /
            ("test-llama-context-" + safe_case_name);
    const std::string path_template = path.u8string();
    api->SetCaptureFilePathTemplate(path_template.c_str());
    api->StartFrameCapture(nullptr, nullptr);

    if (!api->IsFrameCapturing()) {
        error = "RenderDoc did not start a capture";
        return false;
    }

    return true;
#endif
}

bool renderdoc_capture::finish(std::string & error) {
#if !defined(LLAMA_TEST_RENDERDOC)
    error = "RenderDoc capture support is not available";
    return false;
#else
    auto * api = static_cast<RENDERDOC_API_1_1_2 *>(api_);
    if (api == nullptr || !api->IsFrameCapturing()) {
        error = "RenderDoc capture is not active";
        return false;
    }

    if (!api->EndFrameCapture(nullptr, nullptr)) {
        error = "RenderDoc failed to save the capture";
        return false;
    }

    return true;
#endif
}

std::vector<const ctx_test *> select_cases(const std::string & filter) {
    std::vector<const ctx_test *> selected;

    if (filter.empty()) {
        selected.reserve(test_cases().size());
        for (const auto & tc : test_cases()) {
            selected.push_back(tc.get());
        }

        return selected;
    }

    const std::regex re(filter);

    for (const auto & tc : test_cases()) {
        if (std::regex_match(tc->name(), re)) {
            selected.push_back(tc.get());
        }
    }

    return selected;
}

std::vector<case_run> run_cases(model_run & mr, const std::vector<const ctx_test *> & cases, bool report, renderdoc_capture * renderdoc) {
    // the state cases keep the historical default of a unified KV cache when running a single
    // sequence, the recurrent cases use whatever was passed on the command line
    const bool unified_base  = mr.params.kv_unified;
    const bool unified_state = mr.params.n_parallel == 1 ? true : unified_base;

    size_t name_width = 4; // "case"
    for (const ctx_test * tc : cases) {
        name_width = std::max(name_width, strlen(tc->name()));
    }

    std::vector<case_run> runs;
    runs.reserve(cases.size());

    for (const ctx_test * tc : cases) {
        mr.params.kv_unified = tc->group() == TEST_GROUP_STATE ? unified_state : unified_base;

        case_run run;
        run.name = tc->name();
        bool capture_started = false;

        // the log of a model is read on its own, mark where each case begins
        TLOG_INF("%s\n", run.name.c_str());

        // a case that throws is a case that failed, and the run goes on with the next one
        try {
            if (const std::string why = tc->skip_reason(mr); !why.empty()) {
                run.status = test_status::SKIP;
                run.reason = why;
            } else if (tc->needs_baseline() && !mr.have_baseline) {
                run.status = test_status::SKIP;
                run.reason = "no baseline";
            } else {
                std::string capture_error;
                if (renderdoc != nullptr) {
                    if (!renderdoc->start(run.name, capture_error)) {
                        run.status = test_status::FAIL;
                        run.reason = capture_error;
                    } else {
                        capture_started = true;
                    }
                }

                if (renderdoc == nullptr || capture_started) {
                    if (!tc->uses_fill()) {
                        mr.fill = 0;

                        const case_result res = tc->run(mr);

                        run.status = res.status;
                        run.reason = res.reason;
                    } else {
                        // the recurrent cases are also run against a cache pre-filled with a known
                        // pattern, so a restore reading cells it never wrote cannot pass

                        for (const uint8_t fill : { 0, 0x3e }) {
                            TLOG_INF("(fill 0x%02x)\n", fill);

                            mr.fill = fill;

                            const case_result res = tc->run(mr);

                            run.status = merge_status(run.status, res.status);
                            if (run.reason.empty()) {
                                run.reason = res.reason;
                            }
                            if (res.status == test_status::FAIL) {
                                break;
                            }
                        }
                    }
                }
            }
        } catch (...) {
            run.status = test_status::FAIL;
            run.reason = "unhandled exception";
        }

        if (capture_started) {
            std::string capture_error;
            if (!renderdoc->finish(capture_error)) {
                run.status = test_status::FAIL;
                if (run.reason.empty()) {
                    run.reason = capture_error;
                } else {
                    run.reason += "; " + capture_error;
                }
            }
        }

        if (unified_state) {
            mr.params.kv_unified = unified_base;
        }

        // the verdict is one word, what it means goes to the log of the model
        if (!run.reason.empty()) {
            TLOG_INF("%s %s: %s\n", run.name.c_str(),
                    run.status == test_status::FAIL ? "failed" : "skipped", run.reason.c_str());
        }

        if (report) {
            fprintf(stderr, "%-*s  %s%s%s\n", (int) name_width, run.name.c_str(),
                    test_status_str(run.status),
                    run.reason.empty() ? "" : "  ", run.reason.c_str());
        }

        runs.push_back(std::move(run));
    }

    return runs;
}
