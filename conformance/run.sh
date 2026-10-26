#!/bin/sh
# Run World Model's conformance runner, unmodified (vendor/runner.ts), against the
# Python engine through the logic-free bridge. Exit code is the runner's.
#   sh conformance/run.sh            (STEMMA_PYTHON overrides the interpreter)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
cd "$here"
[ -d node_modules/tsx ] || npm ci --no-audit --no-fund >&2
WORLD_MODEL_IMPL="$here/bridge.ts" exec ./node_modules/.bin/tsx vendor/runner.ts
