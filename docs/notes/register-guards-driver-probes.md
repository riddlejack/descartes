# Harness probes (builder B), Claude Code 2.1.308, clean config

Every answer below was shown by a tiny headless run of the pinned binary
(`<TOOLS>/claude-code-2.1.308/node_modules/.bin/claude`, the `STEMMA_CLAUDE_BIN`) with
`CLAUDE_CONFIG_DIR=<TOOLS>/claude-clean-config` (the `STEMMA_CLAUDE_CONFIG`; how to
make one is in `.env.example`), launched with
`--plugin-dir <probe plugin named "stemma">`, `--agent stemma:lead`, `-p`,
`--output-format stream-json --verbose`. The probe kit (plugin, hooks, MCP server,
settings files, runner) and each run's `out.jsonl` and `probe.log` are in
`conformance/harness/` (runs under `conformance/harness/runs/`). Run names are in brackets.

## Launch environment
- **Scrub the environment.** Launched with the desktop app's inherited environment
  (CLAUDE_CODE_ENTRYPOINT, CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH, ...), the pinned
  binary answered "Not logged in". With `env -i HOME PATH USER LANG CLAUDE_CONFIG_DIR`
  it ran. [p0]
- **One config folder for all runs.** A copy of the clean config (its `.claude.json`
  plus settings) is "Not logged in": the login lives in the keychain, keyed to the
  config folder. So every run uses the one clean config folder; transcripts land in
  `<config>/projects/<cwd with / replaced by ->/<session>.jsonl` and
  `.../<session>/subagents/agent-<id>.jsonl` + `agent-<id>.meta.json`. [p4cfg]
- **The clean config is not clean.** Without extra settings a run loads claude.ai-
  synced org plugins (engineering, data, design, productivity, cowork-plugin-
  management) with about 25 MCP servers, the anthropic-skills set, the claude.ai Docs
  connector, three built-in plugins (cc-plugin-agents-md, -telemetry,
  -plugin-authoring), 19 bundled skills and six built-in agent types. [p1]
  Per-launch fix, all via `--settings` (flag settings) and env, nothing written to the
  shared config: `syncClaudeAiPlugins:false`, `syncClaudeAiSkills:false`,
  `disableClaudeAiConnectors:true`, `disableBundledSkills:true`,
  `skillOverrides:{design:off, doctor:off}` (two bundled skills survive
  disableBundledSkills), `enabledPlugins:{"cc-plugin-agents-md@builtin":false,
  "cc-plugin-telemetry@builtin":false, "cc-plugin-plugin-authoring@builtin":false}`,
  env `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1`, and `--tools <allowlist>` for the
  built-in tools. Then init shows plugins `[stemma]`, agents `stemma:*` only, skills
  `[]`, MCP `[plugin:stemma:stemma]`, tools = the allowlist + the plugin's MCP tools.
  [p5, p6] The init message is the canary's input.

## The *B verifies* items of CONTRACTS v0.2
1. **MCP tool names:** a server `stemma` in plugin `stemma` gives
   `mcp__plugin_stemma_stemma__<tool>`. Confirmed. MCP tools load **deferred**: the
   agent must call `ToolSearch` with `select:<name>` before the first call (every
   probe agent did). Role texts may want to say so. [p1]
2. **Hooks fail closed with `"onFailure": "block"`:** confirmed for a crashing hook
   (exception), a hook that exceeds its `timeout`, and a missing interpreter
   (`/bin/sh: ... No such file`): each blocks the tool with "blocking because
   onFailure is \"block\"". **Hole:** a hook that exits 0 with garbage stdout is read
   as allow. Guards must deny only via exit 2 or deny JSON, and never print anything
   else. [p3a, p6]
3. **Bash sandbox:** `sandbox:{enabled:true, failIfUnavailable:true,
   allowUnsandboxedCommands:false, network:{allowedDomains:[]},
   filesystem:{denyWrite:[...], denyRead:[...]}}`: curl refused ("deny
   network-outbound"), raw socket `Operation not permitted`, writes into `register/`
   and to `run.json` refused, read of a denied folder refused, and
   `dangerouslyDisableSandbox:true` still refused. Writes elsewhere in the run folder
   work. `Read(...)`/`Write(...)`/`Edit(...)` deny rules refuse the file tools ("File
   is in a directory that is denied by your permission settings"). [p5]
4. **Forks denied:** `Agent(fork)` in permissions.deny gives "Agent type 'fork' has
   been denied by permission rule". [p2b]
5. **Blind roles' read denials:** per session via that session's `--settings`
   (`sandbox.filesystem.denyRead` + `Read(...)` deny). **Grep and Glob are not tools in
   2.1.308** (not in the tool list even unrestricted); searching goes through Bash,
   which the sandbox covers. Contract section 6's "Read, Grep, Glob and Bash" should
   read "Read and Bash". [p1, p5]

## The brief's questions
- **Fresh vs forked subagent:** in 2.1.308 headless, a named type (`stemma:plain`)
  and an omitted `subagent_type` (resolved to `general-purpose`) both start fresh:
  shown with a secret word in the parent's prompt that neither could see; the
  transcript's first message is the bare prompt. Only `subagent_type:"fork"` inherits
  (it quoted the word). The fork default is switchable by a server flag
  (`tengu_fork_subagent_enabled`) and `CLAUDE_CODE_FORK_SUBAGENT`, so it can change
  without a version change: deny `Agent(fork)` regardless. Built-in types are denied
  by `Agent(general-purpose)`, `Agent(Explore)` etc., and removed from the list by
  `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1`. [p2a, p2b, p5]
- **Nesting:** lead (depth 0) → subagent depth 1 → 2 → 3; the depth-3 agent has no
  Agent tool. `subagent_stats.max_depth` = 3 in the result. So a worker started by the
  lead can have two levels below it. Raisable with
  `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`. [p1]
- **Model and effort per plugin agent:** subagents follow frontmatter `model:` and
  `effort:` (fable/medium and opus/low ran as written, from transcript `message.model`
  and per-line `effort`). For the main thread started with `--agent`, frontmatter
  `model:` applies but frontmatter `effort:` is ignored (low written, medium/high ran)
  → the driver passes `--effort` (and `--model`). An Agent call's `effort` argument is
  honoured (max ran at max) and its `model` argument, with
  `availableModels:[claude-opus-5-5, claude-fable-5-1]`, silently fell back to the
  frontmatter model (haiku and sonnet asked, opus ran). A PreToolUse hook that drops
  `model` and `effort` via `updatedInput` works (meta.json lost them, frontmatter
  effort ran). [p1, p2a, p2b, p6, p7]
- **What a hook's input carries:** every PreToolUse input has `agent_type`
  (`stemma:lead` for the main thread under `--agent`, `stemma:<role>` inside a
  subagent), `agent_id` (subagents only), `session_id`, `tool_use_id`,
  `effort:{level}`, `permission_mode`, `cwd`, and for MCP tools
  `mcp_server:{name:"plugin:stemma:stemma", source:"plugin"}`. SubagentStart carries
  `agent_id` and `agent_type`. Env given to the claude process (`STEMMA_RUN_DIR`)
  reaches hooks and the MCP server. [p1]
- **Can PreToolUse overwrite a tool argument:** yes. `updatedInput` (without a
  permissionDecision) rewrote a Bash command (`echo ORIGINAL` printed REWRITTEN) and
  MCP arguments, and **can add a key absent from the MCP tool's schema** (the server
  received it). An agent-supplied value for the same key is overwritten. [p1, p6]
- **A second identity channel:** the MCP request's `_meta` carries
  `claudecode/toolUseId`, equal to the hook's `tool_use_id`. So the hook can write
  `{agent_type, agent_id, session_id}` to a tool-only file keyed by tool_use_id and the
  server read it, with nothing passing through model-controlled arguments. B uses
  this. [p6]
- **Headless denies what is not allowed:** an MCP tool not in `permissions.allow`
  was refused ("you haven't granted it yet"). [p8]

## Later probes
- **p9:** sandbox denials hold against `Register/` (case), `analysis/../register/`,
  a symlink to `register/`, renaming `register/`, `SECRET/`, `./Secret/../secret/`
  and a symlink to the denied folder.
- **p10:** `denyWrite` and `denyRead` hold for paths that do not exist yet and for
  `**` globs (`branches/**/verdicts`, `branches/**/record.md`).
- **p11:** `"alwaysLoad": true` on the plugin's MCP server entry loads its tools at
  start: with `--tools Bash` (no ToolSearch at all) the init list held
  `mcp__plugin_stemma_stemma__echo` and the agent called it directly.
- **p12:** `denyRead: [<run>]` with `allowRead: [<run>/task, <run>/intake.md,
  <run>/draft/answer.v2.md]` lets exactly those be read; `answer.v1.md`, `frame.md`
  and `ls .` are refused. This is how the decider's-eye reviewer reads only its inputs.

- **p13:** `--strict-mcp-config` drops the plugin's own MCP server (init `mcp: []`,
  the tool gone), so Stemma sessions cannot use it; a bare baseline may.
- **p14:** `denyRead: [<home>, <runs>]` with `allowRead: [<run>, <repo>/.venv,
  <repo>/src]`: the run, the interpreter and `import stemma.analysis` work; `ls ~`, the
  design record, the repo's tests and another run's store are refused.
- **p15:** the same without re-opening the config's shell snapshots: Bash still works
  (only `git`, which reads `~/.gitconfig`, fails; agents do not need it).
- **p16:** `allowRead: [<run>]` with `denyRead` of `<run>/frame.md` and
  `<run>/branches/**/record.md`: the specific denials still hold (the most specific rule
  wins either way), so blind roles keep their denials under a re-opened run.
- **p17 to p19:** deny by default from the root (`denyRead: ["/"]`). What a shell needs,
  found by these runs: `/bin`, `/usr`, `/System`, `/dev`, `/private/etc` and the `/etc`
  link, `/Library/Apple`, the `/tmp` link, the harness's own
  `/private/tmp/claude-<uid>/cwd-*` files, every symlink on the interpreter's path
  (`/opt/homebrew/opt/python@3.12`), and the harness temp folder moved into the run
  with `CLAUDE_CODE_TMPDIR` (without it the shell cannot write its stdout file). With
  those re-opened, the run, `/etc/hosts` and `import stemma.analysis` work, and a decoy
  in `/private/tmp`, `ls /tmp`, `ls /Volumes`, `ls /Users` and `ls ~` are refused.

## Reproducing (when the harness version changes)
`conformance/harness/run.sh <name> '<prompt>' [claude flags...]` with
`STEMMA_CLAUDE_BIN`, `STEMMA_CLAUDE_CONFIG` and `STEMMA_PYTHON` set launches the
binary with a scrubbed environment and the probe plugin, writing
`runs-local/<name>/out.jsonl`; `python3 show.py runs-local/<name>` prints init, tool
calls and results. Each probe above names its flags and settings file; the prompts
are in the recorded `out.jsonl` (first assistant turn) and in this file.
