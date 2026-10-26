#!/bin/zsh
# usage: run.sh <name> <prompt> [extra claude args...]
S=${0:A:h}
name=$1; prompt=$2; shift 2
d=$S/runs/$name; rm -rf $d; mkdir -p $d/cwd
cd $d/cwd; eval "${PROBE_PREP:-:}"
env -i HOME=$HOME PATH=/usr/bin:/bin:/usr/sbin:/sbin USER=$USER LANG=en_US.UTF-8 \
  CLAUDE_CONFIG_DIR=${PROBE_CONFIG:-$HOME/.private/runs/tools/claude-clean-config} \
  PROBE_PY=<HOME>/Desktop/stemma-2/.claude/worktrees/part-b-register-guards-driver-bd46bc/.venv/bin/python \
  PROBE_LOG=$d/probe.log STEMMA_RUN_DIR=$d/cwd ${=PROBE_ENV} \
  $HOME/.private/runs/tools/claude-code-2.1.308/node_modules/.bin/claude -p "$prompt" \
  --output-format stream-json --verbose --plugin-dir ${PROBE_PLUGIN:-$S/plugin} "$@" < /dev/null > $d/out.jsonl 2> $d/err.txt
echo "exit $?" > $d/exit.txt
