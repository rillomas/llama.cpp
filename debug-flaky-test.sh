#!/bin/bash

RUN_ITERATIONS=50

run() {
  fails=0
  for i in $(seq "$RUN_ITERATIONS"); do
    env "$@" GGML_SCHED_DEBUG_REALLOC=1 "$TEST" \
      -m build-release/tests/test-models/lfm2moe-moe.gguf \
      --test '^shared$' \
      >/dev/null 2>&1 || fails=$((fails+1))
  done
  echo "$(basename "$TEST") $* -> $fails/$RUN_ITERATIONS"
}

for TEST in \
  build-release/bin/test-llama-context
do
  run X=baseline
  run GGML_VK_DISABLE_HOST_BARRIER=1
done
