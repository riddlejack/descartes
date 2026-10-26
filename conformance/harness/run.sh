#!/bin/zsh
# Harness probe runner: one headless session of the pinned Claude Code with a scrubbed
# environment and the probe plugin beside this script.
#   run.sh <name> '<prompt>' [claude flags...]
# Env: STEMMA_CLAUDE_BIN (pinned binary), STEMMA_CLAUDE_CONFIG (clean config),
#      STEMMA_PYTHON (interpreter with mcp installed), PROBE_RUNS (output folder,
#      default ./runs-local), PROBE_PREP (shell run in the session's cwd first),
#      PROBE_ENV (extra VAR=value words), PROBE_PLUGIN (plugin folder, default ./plugin).
# Settings files here embed the absolute run folder of the original probe (W=...);
# rewrite that path before reusing a settings file elsewhere.
S=${0:A:h}
name=$1; prompt=$2; shift 2
d=${PROBE_RUNS:-$S/runs-local}/$name; rm -rf $d; mkdir -p $d/cwd
cd $d/cwd; eval "${PROBE_PREP:-:}"
env -i HOME=$HOME PATH=/usr/bin:/bin:/usr/sbin:/sbin USER=$USER LANG=en_US.UTF-8 \
  CLAUDE_CONFIG_DIR=${STEMMA_CLAUDE_CONFIG:?} \
  PROBE_PY=${STEMMA_PYTHON:?} \
  PROBE_LOG=$d/probe.log STEMMA_RUN_DIR=$d/cwd ${=PROBE_ENV} \
  ${STEMMA_CLAUDE_BIN:?} -p "$prompt" \
  --output-format stream-json --verbose --plugin-dir ${PROBE_PLUGIN:-$S/plugin} "$@" < /dev/null > $d/out.jsonl 2> $d/err.txt
echo "exit $?" > $d/exit.txt
