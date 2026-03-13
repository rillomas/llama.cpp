#pragma once

#include <cpp-httplib/httplib.h>

#include <cstdlib>

struct common_http_url {
    std::string scheme;
    std::string user;
    std::string password;
    std::string host;
    int port;
    std::string path;
};

static common_http_url common_http_parse_url(const std::string & url);

static std::string common_http_get_env(const char * key_upper, const char * key_lower) {
    const char * val = std::getenv(key_upper);
    if (val && val[0] != '\0') {
        return val;
    }

    val = std::getenv(key_lower);
    if (val && val[0] != '\0') {
        return val;
    }

    return {};
}

static std::string common_http_parse_scheme(const std::string& url, size_t & scheme_end) {
    scheme_end = url.find("://");

    if (scheme_end == std::string::npos) {
        return {};
    }
    return url.substr(0, scheme_end);
}

static common_http_url common_http_parse_url(const std::string & url) {
    common_http_url parts;
    size_t scheme_end = 0;
    auto scheme = common_http_parse_scheme(url, scheme_end);
    if (scheme.empty()) {
        throw std::runtime_error("invalid URL: no scheme");
    }
    parts.scheme = scheme;

    if (parts.scheme != "http" && parts.scheme != "https") {
        throw std::runtime_error("unsupported URL scheme: " + parts.scheme);
    }

    auto rest = url.substr(scheme_end + 3);
    auto at_pos = rest.find('@');

    if (at_pos != std::string::npos) {
        auto auth = rest.substr(0, at_pos);
        auto colon_pos = auth.find(':');
        if (colon_pos != std::string::npos) {
            parts.user = auth.substr(0, colon_pos);
            parts.password = auth.substr(colon_pos + 1);
        } else {
            parts.user = auth;
        }
        rest = rest.substr(at_pos + 1);
    }

    auto slash_pos = rest.find('/');

    if (slash_pos != std::string::npos) {
        parts.host = rest.substr(0, slash_pos);
        parts.path = rest.substr(slash_pos);
    } else {
        parts.host = rest;
        parts.path = "/";
    }

    auto colon_pos = parts.host.find(':');

    if (colon_pos != std::string::npos) {
        parts.port = std::stoi(parts.host.substr(colon_pos + 1));
        parts.host = parts.host.substr(0, colon_pos);
    } else if (parts.scheme == "http") {
        parts.port = 80;
    } else if (parts.scheme == "https") {
        parts.port = 443;
    } else {
        throw std::runtime_error("unsupported URL scheme: " + parts.scheme);
    }

    return parts;
}

static std::pair<httplib::Client, common_http_url> common_http_client(const std::string & url) {
    common_http_url parts = common_http_parse_url(url);

    if (parts.host.empty()) {
        throw std::runtime_error("error: invalid URL format");
    }

#ifndef CPPHTTPLIB_OPENSSL_SUPPORT
    if (parts.scheme == "https") {
        throw std::runtime_error(
            "HTTPS is not supported. Please rebuild with one of:\n"
            "  -DLLAMA_BUILD_BORINGSSL=ON\n"
            "  -DLLAMA_BUILD_LIBRESSL=ON\n"
            "  -DLLAMA_OPENSSL=ON (default, requires OpenSSL dev files installed)"
        );
    }
#endif

    httplib::Client cli(parts.scheme + "://" + parts.host + ":" + std::to_string(parts.port));

    std::string proxy_env;
    if (parts.scheme == "https") {
        proxy_env = common_http_get_env("HTTPS_PROXY", "https_proxy");
        if (proxy_env.empty()) {
            proxy_env = common_http_get_env("HTTP_PROXY", "http_proxy");
        }
    } else {
        proxy_env = common_http_get_env("HTTP_PROXY", "http_proxy");
    }

    if (!proxy_env.empty()) {
        size_t proxy_scheme_end = 0;
        auto scheme = common_http_parse_scheme(proxy_env, proxy_scheme_end);
        auto proxy_env_revised = proxy_env;
        if (scheme.empty()) {
            // No scheme was provided so we assume http://
            proxy_env_revised = "http://" + proxy_env;
        }
        common_http_url proxy_parts = common_http_parse_url(proxy_env_revised);
        cli.set_proxy(proxy_parts.host, proxy_parts.port);
        if (!proxy_parts.user.empty()) {
            cli.set_proxy_basic_auth(proxy_parts.user, proxy_parts.password);
        }
    }

    if (!parts.user.empty()) {
        cli.set_basic_auth(parts.user, parts.password);
    }

    cli.set_follow_location(true);

    return { std::move(cli), std::move(parts) };
}

static std::string common_http_show_masked_url(const common_http_url & parts) {
    return parts.scheme + "://" + (parts.user.empty() ? "" : "****:****@") + parts.host + parts.path;
}
