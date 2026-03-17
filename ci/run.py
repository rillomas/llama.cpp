#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
A sub-set port of ci/run.sh
This is currently targeting Vulkan backend for Windows only.
We created this script since Windows cannot execute bash scripts natively
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
from typing import Optional

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

def run_streaming(cmd, log_file: Path, cwd: Optional[Path] = None, env: Optional[dict] = None) -> int:
    """Run a command, stream stdout/stderr to both console and log file, return exit code."""
    ensure_dir(log_file.parent)
    with log_file.open("ab") as lf:
        lf.write(f"\n$ {' '.join(map(str, cmd))}\n".encode())
        lf.flush()
        start = time.time()
        proc = subprocess.Popen(
            list(map(str, cmd)),
            cwd=str(cwd) if cwd else None,
            env=env if env else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True
        )
        if proc.stdout is not None:
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

def check_build_requirements() -> list[str]:
    missing = []
    if not which("cmake"):
        missing.append("cmake")
    if not which("ctest"):
        missing.append("ctest")
    return missing

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

    # Vulkan
    if getenv_bool("GG_BUILD_VULKAN"):
        cmake_opts += ["-DGGML_VULKAN=1"]
        if platform.system().lower().startswith("darwin"):
            cmake_opts += ["-DGGML_METAL=OFF", "-DGGML_BLAS=OFF"]

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

    # We do not execute python tests at the moment since we cannot apply venv from python script.
    # Will need to modify test-jinja-py so we can give the absolute path of the python executable in venv.
    label_expr = ["-L", "main"] if not getenv_bool("GG_BUILD_LOW_PERF") else ["-L", "main", "-E", "test-opt"]
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

    # Initialize CI context
    ctx = CIContext(OUT, MNT, SRC, cmake_extra, env)

    # Sequence
    steps = [
        ("ctest_debug", gg_run_ctest_debug, gg_sum_ctest_debug),
        ("ctest_release", gg_run_ctest_release, gg_sum_ctest_release),
    ]

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