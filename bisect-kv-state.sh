#!/usr/bin/env bash
# git bisect run script: detect "incompatible V transposition" in llama-bench -d (state save/restore)
#
# usage:
#   git bisect start <bad> <good>
#   MODEL=/path/to/gemma-4-E4B-it-Q4_K_M.gguf git bisect run ./bisect-kv-state.sh
#
# exit codes: 0 = good, 1 = bad, 125 = skip (build failure / model not supported)
#
# NOTE: keep this script untracked (or copy it outside the repo) so checkouts do not touch it.

set -u

MODEL="${MODEL:-gemma-4-E4B-it-Q4_K_M.gguf}"
BUILD_DIR="${BUILD_DIR:-build-bisect}"
JOBS="${JOBS:-$(nproc)}"
CMAKE_ARGS="${CMAKE_ARGS:--DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release -DLLAMA_OPENSSL=OFF -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF}"
BENCH_ARGS="${BENCH_ARGS:--d 8 -fa 0,1 -p 0 -n 8 -r 1}"

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT" || exit 125

if [ ! -f "$MODEL" ]; then
    echo "model not found: $MODEL" >&2
    exit 128 # abort bisect
fi

COMMIT="$(git rev-parse --short HEAD)"
LOG_DIR="${LOG_DIR:-$ROOT/bisect-logs}"
mkdir -p "$LOG_DIR"
BUILD_LOG="$LOG_DIR/$COMMIT-build.log"
RUN_LOG="$LOG_DIR/$COMMIT-run.log"

echo "=== $COMMIT: building ==="
# shellcheck disable=SC2086
if ! cmake -S . -B "$BUILD_DIR" $CMAKE_ARGS > "$BUILD_LOG" 2>&1; then
    echo "cmake configure failed -> skip (see $BUILD_LOG)"
    exit 125
fi
if ! cmake --build "$BUILD_DIR" --config Release -j "$JOBS" --target llama-bench >> "$BUILD_LOG" 2>&1; then
    echo "build failed -> skip (see $BUILD_LOG)"
    exit 125
fi

BENCH="$BUILD_DIR/bin/llama-bench"
if [ ! -x "$BENCH" ]; then
    echo "llama-bench not found -> skip"
    exit 125
fi

echo "=== $COMMIT: running ==="
# shellcheck disable=SC2086
timeout 600 "$BENCH" -m "$MODEL" $BENCH_ARGS > "$RUN_LOG" 2>&1
RC=$?
cat "$RUN_LOG"

if grep -qE "incompatible V transposition|failed to restore kv cache|error loading state" "$RUN_LOG"; then
    echo "=== $COMMIT: BAD ==="
    exit 1
fi

if grep -qE "unknown model architecture|failed to load model|error: failed to load" "$RUN_LOG"; then
    echo "=== $COMMIT: model not supported -> skip ==="
    exit 125
fi

if [ $RC -ne 0 ]; then
    echo "=== $COMMIT: llama-bench exited with $RC -> skip ==="
    exit 125
fi

echo "=== $COMMIT: GOOD ==="
exit 0
