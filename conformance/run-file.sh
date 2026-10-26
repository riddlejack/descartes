#!/bin/sh
# The same unmodified runner, with every memory world turned into a file log
# (STEMMA_WM_MEMORY_AS_FILE), so the file store the register uses is what passes.
# Fails if the runner fails or if no log file was written.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
dir=$(mktemp -d "${TMPDIR:-/tmp}/wm-conformance-file.XXXXXX")
STEMMA_WM_MEMORY_AS_FILE="$dir" sh "$here/run.sh"
n=$(ls "$dir"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
echo "file logs written: $n (in $dir)"
[ "$n" -gt 0 ]
