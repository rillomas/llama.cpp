#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cross-platform Python port of ci/run.sh
This is currently targeting Vulkan backend for Windows only.
We create this scripst since Windows cannot exeucte bash script natively
and trying to run on Git Bash/MSYS2 has other issues.
We assume this script to be executed from a normal cmd.exe or PowerShell on Windows paired with Visual Studio build tools.
"""

import os
import sys
import platform
import shutil
import subprocess
import time
import re
from pathlib import Path
from urllib.request import urlretrieve
from urllib.error import URLError, HTTPError
from zipfile import ZipFile

# ----------------------------
# Helpers
# ----------------------------

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def which(cmd: str):
    return shutil.which(cmd)

def cpu_count():
    return os.cpu_count() or 1

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def abs_path(p: Path):
    return p.resolve()

def remove_glob(dir_path: Path, pattern: str):
    for f in dir_path.glob(pattern):
        try:
            f.unlink()
        except Exception:
            pass

def run_streaming(cmd, log_file: Path, cwd: Path = None, env: dict = None) -> int:
    """Run a command, stream stdout/stderr to both console and log file, return exit code."""
    ensure_dir(log_file.parent)
    with log_file.open("ab") as lf:
        lf.write(f"\n$ {' '.join(map(str, cmd))}\n".encode())
        lf.flush()
        start = time.time()
        proc = subprocess.Popen(
            list(map(str, cmd)),
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True
        )
        for line in proc.stdout:
            sys.stdout.write(line)
            lf.write(line.encode(errors="ignore"))
        proc.wait()
        elapsed = time.time() - start
        lf.write(f"\n[time] elapsed: {elapsed:.2f}s\n".encode())
        lf.flush()
        return proc.returncode

def write_exit(out_dir: Path, name: str, code: int):
    (out_dir / f"{name}.exit").write_text(str(code), encoding="utf-8")

def download_file_if_needed(out_dir: Path, url: str):
    """Simple replacement for `wget -c -N`: if file exists and non-empty, skip; else download."""
    ensure_dir(out_dir)
    filename = url.split("/")[-1]
    dest = out_dir / filename

    if dest.exists() and dest.stat().st_size > 0:
        return dest

    try:
        eprint(f"Downloading: {url} -> {dest}")
        urlretrieve(url, dest)
    except (URLError, HTTPError) as ex:
        eprint(f"ERROR: download failed: {url} ({ex})")
        raise
    return dest

def unzip_overwrite(zip_path: Path, dest_dir: Path):
    ensure_dir(dest_dir)
    with ZipFile(zip_path, 'r') as zf:
        zf.extractall(dest_dir)

def detect_ostype():
    sysname = platform.system().lower()
    if sysname.startswith("darwin"):
        return "darwin"
    elif sysname.startswith("windows"):
        return "msys"  # rough alignment to original logic
    elif sysname.startswith("linux"):
        return "linux"
    else:
        return sysname

def check_build_requirements() -> list[str]:
    missing = []
    if not which("cmake"):
        missing.append("cmake")
    if not which("ctest"):
        missing.append("ctest")
    return missing

def run_or_skip_bash(cmd_list, log_file: Path, cwd: Path = None, env: dict = None) -> int:
    """If bash exists, run; else skip gracefully with message."""
    if which("bash"):
        return run_streaming(cmd_list, log_file, cwd=cwd, env=env)
    else:
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write("bash not found; skipping this step (likely Windows without Git Bash).\n")
        print("bash not found; skipping this step (likely Windows without Git Bash).")
        return 0

def create_venv(venv_dir: Path, python_exe: str = sys.executable) -> Path:
    """Create venv and return path to venv python executable."""
    import venv
    builder = venv.EnvBuilder(with_pip=True, clear=False)
    builder.create(venv_dir)

    if platform.system().lower().startswith("win"):
        vpy = venv_dir / "Scripts" / "python.exe"
    else:
        vpy = venv_dir / "bin" / "python"
    if not vpy.exists():
        raise RuntimeError("Failed to create Python virtual environment")

    # Upgrade pip minimally to support editable installs cleanly (optional)
    subprocess.run([str(vpy), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel", "--disable-pip-version-check"], check=True)
    return vpy

def install_requirements(vpy: Path, src_dir: Path, out_dir: Path):
    req = src_dir / "requirements.txt"
    if req.exists():
        run_streaming([str(vpy), "-m", "pip", "install", "-r", str(req), "--disable-pip-version-check"],
                      out_dir / "pip-install.log")
    else:
        with (out_dir / "pip-install.log").open("a", encoding="utf-8") as lf:
            lf.write("requirements.txt not found; skipping pip install for requirements.\n")

    gguf_py = src_dir / "gguf-py"
    if gguf_py.exists():
        run_streaming([str(vpy), "-m", "pip", "install", "--editable", str(gguf_py), "--disable-pip-version-check"],
                      out_dir / "pip-install.log")

def parse_float_from_text(text: str) -> float | None:
    m = re.findall(r"[0-9]+\.[0-9]+", text)
    if not m:
        return None
    try:
        return float(m[-1])
    except ValueError:
        return None

def getenv_bool(name: str) -> bool:
    val = os.environ.get(name)
    if val is None:
        return False
    return str(val).strip() == "1"

# ----------------------------
# README writer (append-only)
# ----------------------------

class ReadmeWriter:
    """Keep README.md open in append mode for the whole CI run."""
    def __init__(self, readme_path: Path):
        self.path = readme_path
        ensure_dir(self.path.parent)
        # newline='' to avoid newline conversion surprises on Windows
        self._f = self.path.open("a", encoding="utf-8", newline="")

    def write(self, s: str):
        self._f.write(s)

    def printf(self, fmt: str, *args):
        self._f.write(fmt % args)

    def flush(self):
        try:
            self._f.flush()
        except Exception:
            pass

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass

# ----------------------------
# Compute Settings (CMAKE_EXTRA)
# ----------------------------

def build_cmake_extra(src_dir: Path, out_dir: Path) -> tuple[list[str], dict]:
    """Return (cmake_options_list, updated_env)."""
    env = os.environ.copy()

    # We turn off fatal warnings by default on Windows
    # since there is too much difference in warnings between MSVC and GCC/Clang
    default_fatal_warnings = "OFF" if platform.system().lower().startswith("win") else "ON"
    cmake_opts = [
        f"-DLLAMA_FATAL_WARNINGS={env.get('LLAMA_FATAL_WARNINGS', default_fatal_warnings)}",
        "-DLLAMA_OPENSSL=OFF",
        "-DGGML_SCHED_NO_REALLOC=ON",
    ]

    # # METAL
    # if getenv_bool("GG_BUILD_METAL"):
    #     cmake_opts += ["-DGGML_METAL=ON"]

    # # CUDA
    # if getenv_bool("GG_BUILD_CUDA"):
    #     if not which("nvidia-smi"):
    #         print("Error: nvidia-smi not found, cannot build with CUDA")
    #         sys.exit(1)

    #     # TODO: remove GGML_CUDA_CUB_3DOT2 when CTK bundles CCCL 3.2
    #     cmake_opts += ["-DGGML_CUDA=ON", "-DGGML_CUDA_CUB_3DOT2=ON"]
    #     # detect compute capability via nvidia-smi
    #     try:
    #         proc = subprocess.run(
    #             ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader,nounits"],
    #             capture_output=True, text=True, check=False
    #         )
    #         cap = proc.stdout.strip().splitlines()[0].replace(".", "").strip()
    #         if cap.isdigit():
    #             cmake_opts.append(f"-DCMAKE_CUDA_ARCHITECTURES={cap}")
    #         else:
    #             print("Warning: Using fallback CUDA architectures")
    #             cmake_opts.append("-DCMAKE_CUDA_ARCHITECTURES=61;70;75;80;86;89")
    #     except Exception:
    #         print("Warning: Using fallback CUDA architectures")
    #         cmake_opts.append("-DCMAKE_CUDA_ARCHITECTURES=61;70;75;80;86;89")

    # # ROCm/HIP
    # if getenv_bool("GG_BUILD_ROCM"):
    #     cmake_opts += ["-DGGML_HIP=ON"]
    #     targets = os.environ.get("GG_BUILD_AMDGPU_TARGETS")
    #     if not targets:
    #         print("Missing GG_BUILD_AMDGPU_TARGETS (e.g. gfx90a, gfx1100, etc.)")
    #         sys.exit(1)
    #     cmake_opts += [f"-DGPU_TARGETS={targets}"]

    # # SYCL (oneAPI)
    # if getenv_bool("GG_BUILD_SYCL"):
    #     if not os.environ.get("ONEAPI_ROOT"):
    #         print("Not detected ONEAPI_ROOT. Install oneAPI base toolkit and enable it:")
    #         if platform.system().lower().startswith("win"):
    #             print(r'Run "C:\Program Files (x86)\Intel\oneAPI\setvars.bat" first.')
    #         else:
    #             print("source /opt/intel/oneapi/setvars.sh")
    #         sys.exit(1)
    #     env["ONEAPI_DEVICE_SELECTOR"] = "level_zero:0"
    #     env["ZES_ENABLE_SYSMAN"] = "1"
    #     env["SYCL_PROGRAM_COMPILE_OPTIONS"] = "-cl-fp32-correctly-rounded-divide-sqrt"
    #     cmake_opts += ["-DGGML_SYCL=1", "-DCMAKE_C_COMPILER=icx", "-DCMAKE_CXX_COMPILER=icpx", "-DGGML_SYCL_F16=ON"]

    # Vulkan
    if getenv_bool("GG_BUILD_VULKAN"):
        cmake_opts += ["-DGGML_VULKAN=1"]
        if platform.system().lower().startswith("darwin"):
            cmake_opts += ["-DGGML_METAL=OFF", "-DGGML_BLAS=OFF"]

    # WebGPU (Dawn)
    # if getenv_bool("GG_BUILD_WEBGPU"):
    #     cmake_opts += ["-DGGML_WEBGPU=1", "-DGGML_METAL=OFF", "-DGGML_BLAS=OFF"]
    #     dawn_prefix = os.environ.get("GG_BUILD_WEBGPU_DAWN_PREFIX")
    #     if dawn_prefix:
    #         if env.get("CMAKE_PREFIX_PATH"):
    #             env["CMAKE_PREFIX_PATH"] = f"{dawn_prefix}:{env['CMAKE_PREFIX_PATH']}"
    #         else:
    #             env["CMAKE_PREFIX_PATH"] = dawn_prefix
    #     dawn_dir = os.environ.get("GG_BUILD_WEBGPU_DAWN_DIR")
    #     if dawn_dir:
    #         cmake_opts += [f"-DDawn_DIR={dawn_dir}"]

    # # MUSA
    # if getenv_bool("GG_BUILD_MUSA"):
    #     musa_arch = os.environ.get("MUSA_ARCH", "21")
    #     cmake_opts += ["-DGGML_MUSA=ON", f"-DMUSA_ARCHITECTURES={musa_arch}"]

    # # NO SVE baseline
    # if getenv_bool("GG_BUILD_NO_SVE"):
    #     cmake_opts += ["-DGGML_NATIVE=OFF", "-DGGML_CPU_ARM_ARCH=armv8.5-a+fp16+i8mm"]

    # # KleidiAI
    # if getenv_bool("GG_BUILD_KLEIDIAI"):
    #     print(">>===== Enabling KleidiAI support")
    #     candidates = [
    #         "armv9-a+dotprod+i8mm+sve2",
    #         "armv9-a+dotprod+i8mm",
    #         "armv8.6-a+dotprod+i8mm",
    #         "armv8.2-a+dotprod",
    #     ]
    #     cxx = os.environ.get("CXX") or which("c++") or which("g++") or which("clang++")
    #     baseline = None
    #     if not cxx:
    #         print("ERROR: No C++ compiler found for KleidiAI probing.")
    #         sys.exit(1)
    #     for cpu in candidates:
    #         try:
    #             # Try to compile a dummy program with -march baseline
    #             proc = subprocess.run(
    #                 [cxx, "-march=" + cpu, "-x", "c++", "-c", "-", "-o", os.devnull],
    #                 input="int main(){}",
    #                 text=True, capture_output=True
    #             )
    #             if proc.returncode == 0:
    #                 baseline = cpu
    #                 break
    #         except Exception:
    #             pass
    #     if not baseline:
    #         print("ERROR: None of the required ARM baselines (armv9/armv8.6/armv8.2 + dotprod) are supported by this compiler.")
    #         sys.exit(1)
    #     print(f">>===== Using ARM baseline: {baseline}")
    #     cmake_opts += [
    #         "-DGGML_NATIVE=OFF",
    #         "-DGGML_CPU_KLEIDIAI=ON",
    #         "-DGGML_CPU_AARCH64=ON",
    #         f"-DGGML_CPU_ARM_ARCH={baseline}",
    #         "-DBUILD_SHARED_LIBS=OFF"
    #     ]

    return cmake_opts, env

# ----------------------------
# CI Context
# ----------------------------

class CIContext:
    def __init__(self, out_dir: Path, mnt_dir: Path, src_dir: Path, cmake_extra: list[str], env: dict):
        self.OUT = out_dir
        self.MNT = mnt_dir
        self.SRC = src_dir
        self.env = env.copy()
        self.cmake_extra = cmake_extra
        self.ret = 0

        # README writer
        self.readme = ReadmeWriter(self.OUT / "README.md")

        # Setup defaults
        self.env["LLAMA_LOG_PREFIX"] = "1"
        self.env["LLAMA_LOG_TIMESTAMPS"] = "1"

        # Determine venv python if created later
        self.venv_python: Path | None = None

        # paths for models (avoid symlink; use directly to be Windows-friendly)
        self.models_root = self.MNT / "models"
        ensure_dir(self.models_root)

    # Append raw string to README
    def gg_printf(self, s: str):
        self.readme.write(s)

    # printf-style append to README
    def gg_printf_fmt(self, fmt: str, *args):
        self.readme.printf(fmt, *args)

    def write_sum_section(self, title: str, lines: list[str]):
        self.gg_printf_fmt("### %s\n\n", title)
        for ln in lines:
            self.gg_printf(ln)
        self.gg_printf("\n")

    def run_ci(self, name: str, fn_run, fn_sum):
        print(f"\n==== Running {name} ====\n")
        code = fn_run(self, name)
        write_exit(self.OUT, name, code)
        # Update overall return (bitwise OR)
        self.ret = self.ret | code
        fn_sum(self, name)

    def cmake_configure_build(self, build_dir: Path, build_type: str, log_prefix: str) -> int:
        ensure_dir(build_dir)
        # configure
        rc1 = run_streaming(
            ["cmake", f"-DCMAKE_BUILD_TYPE={build_type}", *self.cmake_extra, ".."],
            self.OUT / f"{log_prefix}-cmake.log",
            cwd=build_dir,
            env=self.env
        )
        if rc1 != 0:
            return rc1
        # build
        rc2 = run_streaming(
            ["cmake", "--build", ".", "--config", build_type, f"-j{cpu_count()}"],
            self.OUT / f"{log_prefix}-make.log",
            cwd=build_dir,
            env=self.env
        )
        return rc2

    def finalize(self):
        self.readme.flush()
        self.readme.close()

# ----------------------------
# CI Steps
# ----------------------------

# ---- ctest_debug

def gg_run_ctest_debug(ctx: CIContext, ci: str) -> int:
    os.chdir(ctx.SRC)
    build_dir = ctx.SRC / "build-ci-debug"
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    ensure_dir(build_dir)

    missing = check_build_requirements()
    if missing:
        ctx.gg_printf(f"Missing build tools: {', '.join(missing)}\n")

    rc = ctx.cmake_configure_build(build_dir, "Debug", ci)
    if rc != 0:
        return rc

    rc3 = run_streaming(
        ["ctest", "-C", "Debug", "--output-on-failure", "-L", "main", "-E", "test-opt|test-backend-ops"],
        ctx.OUT / f"{ci}-ctest.log",
        cwd=build_dir,
        env=ctx.env
    )
    return rc3

def gg_sum_ctest_debug(ctx: CIContext, ci: str):
    ctx.gg_printf_fmt("### %s\n\n", ci)
    ctx.gg_printf("Runs ctest in debug mode\n")
    ctx.gg_printf_fmt("- status: %s\n", (ctx.OUT / f"{ci}.exit").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n")
    if (ctx.OUT / f"{ci}-ctest.log").exists():
        ctx.gg_printf((ctx.OUT / f"{ci}-ctest.log").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n\n")

# ---- ctest_release

def gg_run_ctest_release(ctx: CIContext, ci: str) -> int:
    os.chdir(ctx.SRC)
    build_dir = ctx.SRC / "build-ci-release"
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    ensure_dir(build_dir)

    missing = check_build_requirements()
    if missing:
        ctx.gg_printf(f"Missing build tools: {', '.join(missing)}\n")

    rc = ctx.cmake_configure_build(build_dir, "Release", ci)
    if rc != 0:
        return rc

    label_expr = ["-L", "main|python"] if not getenv_bool("GG_BUILD_LOW_PERF") else ["-L", "main", "-E", "test-opt"]
    rc3 = run_streaming(
        ["ctest", "-C", "Release", "--output-on-failure", *label_expr],
        ctx.OUT / f"{ci}-ctest.log",
        cwd=build_dir,
        env=ctx.env
    )
    return rc3

def gg_sum_ctest_release(ctx: CIContext, ci: str):
    ctx.gg_printf_fmt("### %s\n\n", ci)
    ctx.gg_printf("Runs ctest in release mode\n")
    ctx.gg_printf_fmt("- status: %s\n", (ctx.OUT / f"{ci}.exit").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n")
    if (ctx.OUT / f"{ci}-ctest.log").exists():
        ctx.gg_printf((ctx.OUT / f"{ci}-ctest.log").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n")

# ---- test_scripts

def gg_run_test_scripts(ctx: CIContext, ci: str) -> int:
    os.chdir(ctx.SRC)
    # Run tests.sh for gguf-split and quantize
    log = ctx.OUT / f"{ci}-scripts.log"
    rc1 = run_or_skip_bash(["bash", "tests.sh", str(ctx.SRC / "build-ci-release" / "bin"), str(ctx.MNT / "models")],
                           log, cwd=ctx.SRC / "tools" / "gguf-split", env=ctx.env)
    if rc1 != 0:
        return rc1
    rc2 = run_or_skip_bash(["bash", "tests.sh", str(ctx.SRC / "build-ci-release" / "bin"), str(ctx.MNT / "models")],
                           log, cwd=ctx.SRC / "tools" / "quantize", env=ctx.env)
    return rc2

def gg_sum_test_scripts(ctx: CIContext, ci: str):
    ctx.gg_printf_fmt("### %s\n\n", ci)
    ctx.gg_printf("Runs test scripts\n")
    ctx.gg_printf_fmt("- status: %s\n", (ctx.OUT / f"{ci}.exit").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n")
    if (ctx.OUT / f"{ci}-scripts.log").exists():
        ctx.gg_printf((ctx.OUT / f"{ci}-scripts.log").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n\n")

# ---- gg_get_model (for ctest_with_model_*)

def gg_get_model(ctx: CIContext) -> Path:
    # Default: $MNT/models/qwen3/0.6B/ggml-model-q4_0.gguf
    gguf_0 = ctx.MNT / "models" / "qwen3" / "0.6B" / "ggml-model-q4_0.gguf"
    if gguf_0.exists() and gguf_0.stat().st_size > 0:
        return gguf_0
    eprint("No model found. Can't run ctest_with_model.")
    sys.exit(1)

# ---- ctest_with_model_debug/release

def gg_run_ctest_with_model_debug(ctx: CIContext, ci: str) -> int:
    os.chdir(ctx.SRC)
    model = gg_get_model(ctx)
    build_dir = ctx.SRC / "build-ci-debug"
    return run_streaming(
        ["ctest", "-C", "Debug", "--output-on-failure", "-L", "model"],
        ctx.OUT / f"{ci}-ctest.log",
        cwd=build_dir,
        env={**ctx.env, "LLAMACPP_TEST_MODELFILE": str(model)}
    )

def gg_sum_ctest_with_model_debug(ctx: CIContext, ci: str):
    ctx.gg_printf_fmt("### %s\n\n", ci)
    ctx.gg_printf("Runs ctest with model files in debug mode\n")
    ctx.gg_printf_fmt("- status: %s\n", (ctx.OUT / f"{ci}.exit").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n")
    if (ctx.OUT / f"{ci}-ctest.log").exists():
        ctx.gg_printf((ctx.OUT / f"{ci}-ctest.log").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n")

def gg_run_ctest_with_model_release(ctx: CIContext, ci: str) -> int:
    os.chdir(ctx.SRC)
    model = gg_get_model(ctx)
    build_dir = ctx.SRC / "build-ci-release"
    return run_streaming(
        ["ctest", "-C", "Release", "--output-on-failure", "-L", "model"],
        ctx.OUT / f"{ci}-ctest.log",
        cwd=build_dir,
        env={**ctx.env, "LLAMACPP_TEST_MODELFILE": str(model)}
    )

def gg_sum_ctest_with_model_release(ctx: CIContext, ci: str):
    ctx.gg_printf_fmt("### %s\n\n", ci)
    ctx.gg_printf("Runs ctest with model files in release mode\n")
    ctx.gg_printf_fmt("- status: %s\n", (ctx.OUT / f"{ci}.exit").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n")
    if (ctx.OUT / f"{ci}-ctest.log").exists():
        ctx.gg_printf((ctx.OUT / f"{ci}-ctest.log").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n")

# ---- qwen3_0_6b

def gg_run_qwen3_0_6b(ctx: CIContext, ci: str) -> int:
    os.chdir(ctx.SRC)

    # Download Qwen3-0.6B Base HF files
    qwen_dir = ctx.MNT / "models" / "qwen3" / "0.6B"
    ensure_dir(qwen_dir)
    urls = [
        "https://huggingface.co/Qwen/Qwen3-0.6B-Base/raw/main/config.json",
        "https://huggingface.co/Qwen/Qwen3-0.6B-Base/raw/main/tokenizer.json",
        "https://huggingface.co/Qwen/Qwen3-0.6B-Base/raw/main/tokenizer_config.json",
        "https://huggingface.co/Qwen/Qwen3-0.6B-Base/resolve/main/model.safetensors",
    ]
    for u in urls:
        download_file_if_needed(qwen_dir, u)

    # Wikitext eval set
    wiki_dir = ctx.MNT / "models" / "wikitext"
    ensure_dir(wiki_dir)
    zipf = download_file_if_needed(wiki_dir, "https://huggingface.co/datasets/ggml-org/ci/resolve/main/wikitext-2-raw-v1.zip")
    unzip_overwrite(zipf, wiki_dir)

    path_models = qwen_dir
    path_wiki = wiki_dir / "wikitext-2-raw"

    # build release
    build_dir = ctx.SRC / "build-ci-release"
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    ensure_dir(build_dir)

    rc1 = ctx.cmake_configure_build(build_dir, "Release", ci)
    if rc1 != 0:
        return rc1

    # Python conversions
    vpy = ctx.venv_python or Path(sys.executable)
    conv_script = ctx.SRC / "convert_hf_to_gguf.py"

    rc2 = run_streaming([str(vpy), str(conv_script), str(path_models), "--outfile", str(path_models / "ggml-model-f16.gguf"), "--outtype", "f16"],
                        ctx.OUT / f"{ci}-convert.log", cwd=ctx.SRC, env=ctx.env)
    if rc2 != 0:
        return rc2
    rc3 = run_streaming([str(vpy), str(conv_script), str(path_models), "--outfile", str(path_models / "ggml-model-bf16.gguf"), "--outtype", "bf16"],
                        ctx.OUT / f"{ci}-convert.log", cwd=ctx.SRC, env=ctx.env)
    if rc3 != 0:
        return rc3

    # Model variant paths
    model_f16 = path_models / "ggml-model-f16.gguf"
    model_bf16 = path_models / "ggml-model-bf16.gguf"
    variants = {
        "q8_0": path_models / "ggml-model-q8_0.gguf",
        "q4_0": path_models / "ggml-model-q4_0.gguf",
        "q4_1": path_models / "ggml-model-q4_1.gguf",
        "q5_0": path_models / "ggml-model-q5_0.gguf",
        "q5_1": path_models / "ggml-model-q5_1.gguf",
        "q2_k": path_models / "ggml-model-q2_k.gguf",
        "q3_k": path_models / "ggml-model-q3_k.gguf",
        "q4_k": path_models / "ggml-model-q4_k.gguf",
        "q5_k": path_models / "ggml-model-q5_k.gguf",
        "q6_k": path_models / "ggml-model-q6_k.gguf",
    }
    wiki_test = path_wiki / "wiki.test.raw"

    # Quantize from bf16
    log_quant = ctx.OUT / f"{ci}-make.log"  # append to build log
    for qname, qpath in variants.items():
        rcq = run_streaming([str(build_dir / "bin" / "llama-quantize"), str(model_bf16), str(qpath), qname, str(cpu_count())],
                            log_quant, cwd=build_dir, env=ctx.env)
        if rcq != 0:
            return rcq

    # Fit params
    run_streaming([str(build_dir / "bin" / "llama-fit-params"), "--model", str(model_f16)],
                  ctx.OUT / f"{ci}-fp-f16.log", cwd=build_dir, env=ctx.env)

    # Text generation across variants
    prompt = "I believe the meaning of life is"
    def tg(model_path: Path, tag: str):
        return run_streaming(
            [str(build_dir / "bin" / "llama-completion"), "-no-cnv", "--model", str(model_path),
             "-ngl", "99", "-c", "1024", "-s", "1234", "-n", "64", "--ignore-eos", "-p", prompt],
            ctx.OUT / f"{ci}-tg-{tag}.log",
            cwd=build_dir, env=ctx.env
        )

    rc = tg(model_f16, "f16")
    if rc != 0:
        return rc
    rc = tg(model_bf16, "bf16") if not getenv_bool("GG_BUILD_NO_BF16") else 0
    if rc != 0:
        return rc
    for qname, qpath in variants.items():
        rcq = tg(qpath, qname)
        if rcq != 0:
            return rcq

    # Perplexity
    def ppl(model_path: Path, tag: str):
        return run_streaming(
            [str(build_dir / "bin" / "llama-perplexity"), "--model", str(model_path),
             "-f", str(wiki_test), "-ngl", "99", "-c", "1024", "-b", "512", "--chunks", "2"],
            ctx.OUT / f"{ci}-tg-{tag}.log",
            cwd=build_dir, env=ctx.env
        )

    rc = ppl(model_f16, "f16")
    if rc != 0:
        return rc
    if not getenv_bool("GG_BUILD_NO_BF16"):
        rc = ppl(model_bf16, "bf16")
        if rc != 0:
            return rc
    for qname, qpath in variants.items():
        rc = ppl(qpath, qname)
        if rc != 0:
            return rc

    # imatrix
    run_streaming([str(build_dir / "bin" / "llama-imatrix"), "--model", str(model_f16),
                   "-f", str(wiki_test), "-ngl", "99", "-c", "1024", "-b", "512", "--chunks", "2"],
                  ctx.OUT / f"{ci}-imatrix.log", cwd=build_dir, env=ctx.env)

    # save-load-state checks
    for args in [
        ["--model", str(variants["q4_0"]), "-ngl", "10", "-c", "1024", "-fa", "off", "--no-op-offload"],
        ["--model", str(variants["q4_0"]), "-ngl", "10", "-c", "1024", "-fa", "on",  "--no-op-offload"],
        ["--model", str(variants["q4_0"]), "-ngl", "99", "-c", "1024", "-fa", "off"],
        ["--model", str(variants["q4_0"]), "-ngl", "99", "-c", "1024", "-fa", "on"],
    ]:
        run_streaming([str(build_dir / "bin" / "llama-save-load-state"), *args],
                      ctx.OUT / f"{ci}-save-load-state.log", cwd=build_dir, env=ctx.env)

    # PPL checks (thresholds)
    ppl_log = ctx.OUT / f"{ci}-ppl.log"
    with ppl_log.open("a", encoding="utf-8") as lf:
        def check_ppl(tag: str, text: str):
            val = parse_float_from_text(text)
            if val is None:
                lf.write(f"  - {tag} @ N/A (FAIL: no number)\n")
                return 20
            if val > 20.0:
                lf.write(f"  - {tag} @ {val} (FAIL: ppl > 20.0)\n")
                return 20
            lf.write(f"  - {tag} @ {val} OK\n")
            return 0

        # read last lines with [1] in each tg log
        def grep_last_1(path: Path) -> str:
            if not path.exists():
                return ""
            txt = path.read_text(encoding="utf-8", errors="ignore")
            lines = [ln for ln in txt.splitlines() if ln.startswith("[1]")]
            return lines[-1] if lines else txt

        _ = check_ppl("f16",  grep_last_1(ctx.OUT / f"{ci}-tg-f16.log"))
        if not getenv_bool("GG_BUILD_NO_BF16"):
            _ = check_ppl("bf16", grep_last_1(ctx.OUT / f"{ci}-tg-bf16.log"))
        _ = check_ppl("q8_0", grep_last_1(ctx.OUT / f"{ci}-tg-q8_0.log"))
        _ = check_ppl("q4_0", grep_last_1(ctx.OUT / f"{ci}-tg-q4_0.log"))
        _ = check_ppl("q4_1", grep_last_1(ctx.OUT / f"{ci}-tg-q4_1.log"))
        _ = check_ppl("q5_0", grep_last_1(ctx.OUT / f"{ci}-tg-q5_0.log"))
        _ = check_ppl("q5_1", grep_last_1(ctx.OUT / f"{ci}-tg-q5_1.log"))
        # q2_k often > 20; skip like original
        _ = check_ppl("q3_k", grep_last_1(ctx.OUT / f"{ci}-tg-q3_k.log"))
        _ = check_ppl("q4_k", grep_last_1(ctx.OUT / f"{ci}-tg-q4_k.log"))
        _ = check_ppl("q5_k", grep_last_1(ctx.OUT / f"{ci}-tg-q5_k.log"))
        _ = check_ppl("q6_k", grep_last_1(ctx.OUT / f"{ci}-tg-q6_k.log"))

    # summarize imatrix
    imatrix_txt = (ctx.OUT / f"{ci}-imatrix.log").read_text(encoding="utf-8", errors="ignore") if (ctx.OUT / f"{ci}-imatrix.log").exists() else ""
    final_lines = "\n".join([ln for ln in imatrix_txt.splitlines() if "Final" in ln])
    (ctx.OUT / f"{ci}-imatrix-sum.log").write_text(final_lines, encoding="utf-8")

    return 0

def gg_sum_qwen3_0_6b(ctx: CIContext, ci: str):
    ctx.gg_printf_fmt("### %s\n\n", ci)
    ctx.gg_printf("Qwen3 0.6B:\n")
    ctx.gg_printf_fmt("- status: %s\n", (ctx.OUT / f"{ci}.exit").read_text(encoding="utf-8"))
    if (ctx.OUT / f"{ci}-ppl.log").exists():
        ctx.gg_printf(f"- perplexity:\n{(ctx.OUT / f'{ci}-ppl.log').read_text(encoding='utf-8')}\n")
    if (ctx.OUT / f"{ci}-imatrix-sum.log").exists():
        ctx.gg_printf(f"- imatrix:\n```\n{(ctx.OUT / f'{ci}-imatrix-sum.log').read_text(encoding='utf-8')}\n```\n")
    for tag in ["f16", "bf16", "q8_0", "q4_0", "q4_1", "q5_0", "q5_1", "q2_k", "q3_k", "q4_k", "q5_k", "q6_k"]:
        logf = ctx.OUT / f"{ci}-tg-{tag}.log"
        if logf.exists():
            ctx.gg_printf(f"- {tag}:\n```\n{logf.read_text(encoding='utf-8')}\n```\n")
    sll = ctx.OUT / f"{ci}-save-load-state.log"
    if sll.exists():
        ctx.gg_printf(f"- save-load-state: \n```\n{sll.read_text(encoding='utf-8')}\n```\n")

# ---- embd_bge_small

def gg_run_embd_bge_small(ctx: CIContext, ci: str) -> int:
    os.chdir(ctx.SRC)
    bge_dir = ctx.MNT / "models" / "bge-small"
    ensure_dir(bge_dir)
    urls = [
        "https://huggingface.co/BAAI/bge-small-en-v1.5/raw/main/config.json",
        "https://huggingface.co/BAAI/bge-small-en-v1.5/raw/main/tokenizer.json",
        "https://huggingface.co/BAAI/bge-small-en-v1.5/raw/main/tokenizer_config.json",
        "https://huggingface.co/BAAI/bge-small-en-v1.5/raw/main/special_tokens_map.json",
        "https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/main/pytorch_model.bin",
        "https://huggingface.co/BAAI/bge-small-en-v1.5/raw/main/sentence_bert_config.json",
        "https://huggingface.co/BAAI/bge-small-en-v1.5/raw/main/vocab.txt",
        "https://huggingface.co/BAAI/bge-small-en-v1.5/raw/main/modules.json",
        "https://huggingface.co/BAAI/bge-small-en-v1.5/raw/main/config.json",
    ]
    for u in urls:
        download_file_if_needed(bge_dir, u)
    pooling_dir = bge_dir / "1_Pooling"
    ensure_dir(pooling_dir)
    download_file_if_needed(pooling_dir, "https://huggingface.co/BAAI/bge-small-en-v1.5/raw/main/1_Pooling/config.json")

    build_dir = ctx.SRC / "build-ci-release"
    if not build_dir.exists():
        rc = ctx.cmake_configure_build(build_dir, "Release", ci)
        if rc != 0:
            return rc

    vpy = ctx.venv_python or Path(sys.executable)
    conv_script = ctx.SRC / "convert_hf_to_gguf.py"

    rc1 = run_streaming([str(vpy), str(conv_script), str(bge_dir), "--outfile", str(bge_dir / "ggml-model-f16.gguf")],
                        ctx.OUT / f"{ci}-cmake.log", cwd=ctx.SRC, env=ctx.env)
    if rc1 != 0:
        return rc1

    model_f16 = bge_dir / "ggml-model-f16.gguf"
    model_q8_0 = bge_dir / "ggml-model-q8_0.gguf"

    run_streaming([str(build_dir / "bin" / "llama-quantize"), str(model_f16), str(model_q8_0), "q8_0"],
                  ctx.OUT / f"{ci}-make.log", cwd=build_dir, env=ctx.env)

    run_streaming([str(build_dir / "bin" / "llama-fit-params"), "--model", str(model_f16)],
                  ctx.OUT / f"{ci}-fp-f16.log", cwd=build_dir, env=ctx.env)

    # Embeddings
    prompt = "I believe the meaning of life is"
    run_streaming([str(build_dir / "bin" / "llama-embedding"), "--model", str(model_f16),
                   "-p", prompt, "-ngl", "99", "-c", "0", "--no-op-offload"],
                  ctx.OUT / f"{ci}-tg-f16.log", cwd=build_dir, env=ctx.env)

    run_streaming([str(build_dir / "bin" / "llama-embedding"), "--model", str(model_q8_0),
                   "-p", prompt, "-ngl", "99", "-c", "0", "--no-op-offload"],
                  ctx.OUT / f"{ci}-tg-q8_0.log", cwd=build_dir, env=ctx.env)

    return 0

def gg_sum_embd_bge_small(ctx: CIContext, ci: str):
    ctx.gg_printf_fmt("### %s\n\n", ci)
    ctx.gg_printf("BGE Small (BERT):\n")
    ctx.gg_printf_fmt("- status: %s\n", (ctx.OUT / f"{ci}.exit").read_text(encoding="utf-8"))
    for tag in ["f16", "q8_0"]:
        logf = ctx.OUT / f"{ci}-tg-{tag}.log"
        if logf.exists():
            ctx.gg_printf(f"- {tag}: \n```\n{logf.read_text(encoding='utf-8')}\n```\n")

# ---- rerank_tiny

def gg_run_rerank_tiny(ctx: CIContext, ci: str) -> int:
    os.chdir(ctx.SRC)
    rr_dir = ctx.MNT / "models" / "rerank-tiny"
    ensure_dir(rr_dir)
    urls = [
        "https://huggingface.co/jinaai/jina-reranker-v1-tiny-en/raw/main/config.json",
        "https://huggingface.co/jinaai/jina-reranker-v1-tiny-en/raw/main/tokenizer.json",
        "https://huggingface.co/jinaai/jina-reranker-v1-tiny-en/raw/main/tokenizer_config.json",
        "https://huggingface.co/jinaai/jina-reranker-v1-tiny-en/raw/main/special_tokens_map.json",
        "https://huggingface.co/jinaai/jina-reranker-v1-tiny-en/resolve/main/pytorch_model.bin",
        "https://huggingface.co/jinaai/jina-reranker-v1-tiny-en/raw/main/vocab.json",
    ]
    for u in urls:
        download_file_if_needed(rr_dir, u)

    build_dir = ctx.SRC / "build-ci-release"
    if not build_dir.exists():
        rc = ctx.cmake_configure_build(build_dir, "Release", ci)
        if rc != 0:
            return rc

    vpy = ctx.venv_python or Path(sys.executable)
    conv_script = ctx.SRC / "convert_hf_to_gguf.py"

    rc1 = run_streaming([str(vpy), str(conv_script), str(rr_dir), "--outfile", str(rr_dir / "ggml-model-f16.gguf")],
                        ctx.OUT / f"{ci}-cmake.log", cwd=ctx.SRC, env=ctx.env)
    if rc1 != 0:
        return rc1

    model_f16 = rr_dir / "ggml-model-f16.gguf"

    run_streaming([str(build_dir / "bin" / "llama-fit-params"), "--model", str(model_f16)],
                  ctx.OUT / f"{ci}-fp-f16.log", cwd=build_dir, env=ctx.env)

    # SEP token is "</s>"
    text = (
        "what is panda?\thi\n"
        "what is panda?\tit's a bear\n"
        "what is panda?\tThe giant panda (Ailuropoda melanoleuca), sometimes called a panda bear or simply panda, is a bear species endemic to China."
    )
    rc2 = run_streaming(
        [str(build_dir / "bin" / "llama-embedding"), "--model", str(model_f16),
         "-p", text, "-ngl", "99", "-c", "0", "--pooling", "rank", "--embd-normalize", "-1", "--no-op-offload", "--verbose-prompt"],
        ctx.OUT / f"{ci}-rk-f16.log", cwd=build_dir, env=ctx.env
    )
    if rc2 != 0:
        return rc2

    # Check scores in ranges
    rk_text = (ctx.OUT / f"{ci}-rk-f16.log").read_text(encoding="utf-8", errors="ignore") if (ctx.OUT / f"{ci}-rk-f16.log").exists() else ""
    def find_score(idx: int) -> float | None:
        m = re.findall(rf"rerank score {idx}:\s*([0-9]+\.[0-9]+)", rk_text)
        return float(m[-1]) if m else None

    ranges = {
        0: (0.00, 0.05),
        1: (0.00, 0.05),
        2: (0.10, 0.30),
    }
    ok = True
    with (ctx.OUT / f"{ci}-rk-f16.log").open("a", encoding="utf-8") as lf:
        for idx, (lo, hi) in ranges.items():
            val = find_score(idx)
            if val is None or not (lo <= val <= hi):
                lf.write(f"  - rerank score {idx} @ {val} (FAIL: score not in range [{lo}, {hi}])\n")
                ok = False
            else:
                lf.write(f"  - rerank score {idx} @ {val} OK\n")

    return 0 if ok else 20

def gg_sum_rerank_tiny(ctx: CIContext, ci: str):
    ctx.gg_printf_fmt("### %s\n\n", ci)
    ctx.gg_printf("Rerank Tiny (Jina):\n")
    ctx.gg_printf_fmt("- status: %s\n", (ctx.OUT / f"{ci}.exit").read_text(encoding="utf-8"))
    logf = ctx.OUT / f"{ci}-rk-f16.log"
    if logf.exists():
        ctx.gg_printf(f"- f16: \n```\n{logf.read_text(encoding='utf-8')}\n```\n")

# ---- test_backend_ops_cpu

def gg_run_test_backend_ops_cpu(ctx: CIContext, ci: str) -> int:
    os.chdir(ctx.SRC)
    build_dir = ctx.SRC / "build-ci-release"
    return run_streaming(
        [str(build_dir / "bin" / "test-backend-ops"), "-b", "CPU"],
        ctx.OUT / f"{ci}-test-backend-ops-cpu.log",
        cwd=build_dir, env=ctx.env
    )

def gg_sum_test_backend_ops_cpu(ctx: CIContext, ci: str):
    ctx.gg_printf_fmt("### %s\n\n", ci)
    ctx.gg_printf("Runs test-backend-ops for CPU backend\n")
    ctx.gg_printf_fmt("- status: %s\n", (ctx.OUT / f"{ci}.exit").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n")
    if (ctx.OUT / f"{ci}-test-backend-ops-cpu.log").exists():
        ctx.gg_printf((ctx.OUT / f"{ci}-test-backend-ops-cpu.log").read_text(encoding="utf-8"))
    ctx.gg_printf("```\n\n")

# ----------------------------
# Main
# ----------------------------

def main():
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} <output-dir> <mnt-dir>")
        sys.exit(1)

    OUT = abs_path(Path(sys.argv[1]))
    MNT = abs_path(Path(sys.argv[2]))

    ensure_dir(OUT)
    ensure_dir(MNT)

    # Clean output logs
    remove_glob(OUT, "*.log")
    remove_glob(OUT, "*.exit")
    remove_glob(OUT, "*.md")

    script_dir = Path(__file__).resolve().parent
    SRC = (script_dir / "..").resolve()

    # Build CMAKE_EXTRA and env
    cmake_extra, env = build_cmake_extra(SRC, OUT)

    # Prepare venv (unless LOW_PERF)
    vpy = None
    if not getenv_bool("GG_BUILD_LOW_PERF"):
        venv_dir = MNT / "venv"
        try:
            vpy = create_venv(venv_dir)
        except Exception as ex:
            print(f"Error: Failed to create Python virtual environment at {venv_dir}: {ex}")
            sys.exit(1)
        # Install requirements and gguf-py if present
        install_requirements(vpy, SRC, OUT)

    # Initialize CI context
    ctx = CIContext(OUT, MNT, SRC, cmake_extra, env)
    if not getenv_bool("GG_BUILD_LOW_PERF"):
        ctx.venv_python = (MNT / "venv" / ("Scripts" if platform.system().lower().startswith("win") else "bin") / ("python.exe" if platform.system().lower().startswith("win") else "python"))

    # Sequence
    steps = [
        ("ctest_debug", gg_run_ctest_debug, gg_sum_ctest_debug),
        ("ctest_release", gg_run_ctest_release, gg_sum_ctest_release),
    ]

    if getenv_bool("GG_BUILD_HIGH_PERF"):
        steps.append(("test_backend_ops_cpu", gg_run_test_backend_ops_cpu, gg_sum_test_backend_ops_cpu))

    if not getenv_bool("GG_BUILD_LOW_PERF"):
        steps.extend([
            ("embd_bge_small", gg_run_embd_bge_small, gg_sum_embd_bge_small),
            ("rerank_tiny", gg_run_rerank_tiny, gg_sum_rerank_tiny),
        ])

        if (not getenv_bool("GG_BUILD_CLOUD")) or getenv_bool("GG_BUILD_EXTRA_TESTS_0"):
            steps.append(("test_scripts", gg_run_test_scripts, gg_sum_test_scripts))

        steps.append(("qwen3_0_6b", gg_run_qwen3_0_6b, gg_sum_qwen3_0_6b))
        steps.append(("ctest_with_model_debug", gg_run_ctest_with_model_debug, gg_sum_ctest_with_model_debug))
        steps.append(("ctest_with_model_release", gg_run_ctest_with_model_release, gg_sum_ctest_with_model_release))

    try:
        # Run steps
        for name, fn_run, fn_sum in steps:
            if ctx.ret == 0:
                ctx.run_ci(name, fn_run, fn_sum)
            else:
                print(f"Skipping {name} because previous step failed (ret={ctx.ret})")
                write_exit(OUT, name, 1)
                fn_sum(ctx, name)
    finally:
        # Be sure to close README before reading/printing
        ctx.finalize()

    # Print README summary to console
    readme = OUT / "README.md"
    if readme.exists():
        print(readme.read_text(encoding="utf-8"))

    sys.exit(ctx.ret)

if __name__ == "__main__":
    main()