#!/bin/bash

run() {
  fails=0
  for i in $(seq 100); do
    env "$@" GGML_SCHED_DEBUG_REALLOC=1 "$TEST" \
      -m build-release/tests/test-models/lfm2moe-moe.gguf \
      >/dev/null 2>&1 || fails=$((fails+1))
  done
  echo "$(basename "$TEST") $* -> $fails/100"
}

for TEST in \
  build-release/bin/test-llama-context
do
  # run X=baseline
  run GGML_VK_DISABLE_HOST_BARRIER=1
done
