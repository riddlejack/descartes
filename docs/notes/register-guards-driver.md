# Part B notes: the claim register, the guards and the driver

Working notes for builders. The owner-facing summary is the report; this file holds
the detail. Harness facts below were each shown by a run (probe kit and outputs in
the shared notes folder, `B-harness-probes.md`).

## Harness facts the build relies on (Claude Code 2.1.308)
- Launch with a scrubbed environment (`env -i` plus an allowlist); the desktop app's
  inherited variables make the pinned binary report "Not logged in".
- All runs share the one clean config folder (the login is in the keychain, keyed to
  the folder). Transcripts: `<config>/projects/<cwd slug>/<session>.jsonl`, subagents
  under `<session>/subagents/agent-<id>.jsonl` and `.meta.json`.
- The clean config loads claude.ai-synced plugins, connectors and bundled skills unless
  each launch passes the canary settings (`driver/settings.py`).
- MCP tools are `mcp__plugin_stemma_stemma__<tool>` and load deferred (ToolSearch).
- PreToolUse input carries `agent_type`, `agent_id` (subagents), `session_id`,
  `tool_use_id`, `effort.level`; `updatedInput` can rewrite and add arguments; the MCP
  request `_meta["claudecode/toolUseId"]` equals the hook's `tool_use_id`.
- `"onFailure": "block"` blocks on crash, timeout and missing interpreter; exit 0 with
  junk stdout is read as allow.
- Depth: lead 0 → 1 → 2 → 3; depth 3 has no Agent tool.
- Lead effort comes only from `--effort` (frontmatter effort is ignored on the main
  thread); subagents follow frontmatter `model` and `effort`.
- Grep and Glob are not tools in 2.1.308.

## Layout
- `src/stemma/register/wm/`: the Meterless World Model engine in Python, clean-room
  from its specification. `WorldModel`, `entityIdFromName`, `normalizeName`, `decide`
  are the conformance surface (camelCase on purpose: the unmodified runner calls those
  names through a bridge that holds no logic). File storage is an append-only JSONL
  event log under an exclusive `fcntl.flock`; each mutation locks, folds events other
  processes appended, validates, appends, unlocks.
- `conformance/`: the bridge (`bridge.ts`), a pinned `package.json` for `tsx`, the
  runner fetched unmodified from Meterless at a recorded commit, the command and the
  recorded passing output.
- `src/stemma/register/`: the claim register on the World Model (claims, code checks,
  verdicts, corrections, dependency index, echo candidates, entities, tree, check
  requests, `finish_run`).
- `src/stemma/hooks/`: the guards (plugin hooks), one entry point per event.
- `src/stemma/analysis/`: `inputs()` and `result()` for recorded calculation scripts.
- `src/stemma/mcp_server.py`: the one `stemma` MCP server.
- `src/stemma/driver/`: run folder, plugin assembly, launches, watch loop, status.

## Interfaces between B's modules
- **Identity.** The PreToolUse guard writes `register/calls/<tool_use_id>.json`
  (`agent_type`, `agent_id`, `session_id`) for every `mcp__plugin_stemma_stemma__*`
  call. The MCP server reads it by `_meta["claudecode/toolUseId"]` and refuses a
  register write without it. No identity passes through a tool argument.
- **Check requests.** The register records a check request when an event calls for
  one (`file_claim`, `propose_branch_done`, run start, `request_frame_critique`,
  `submit_draft`, accepted `finish_run`). The driver reads pending requests through
  `stemma.register.checks_api` (`pending_checks`, `mark_launched`, `mark_ended`) and
  launches `--agent stemma:<role>` with the request's id-only prompt.
