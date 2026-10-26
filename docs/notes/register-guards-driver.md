# Part B notes: the claim register, the guards and the driver

Working notes for builders. The owner-facing summary is the report; this file holds
the detail. Harness facts below were each shown by a run (report and kit in
`docs/notes/register-guards-driver-probes/`).

## Harness facts the build relies on (Claude Code 2.1.308)
- Launch with a scrubbed environment (`env -i` plus an allowlist); the desktop app's
  inherited variables make the pinned binary report "Not logged in".
- All runs share the one clean config folder (the login is in the keychain, keyed to
  the folder). Transcripts: `<config>/projects/<cwd slug>/<session>.jsonl`, subagents
  under `<session>/subagents/agent-<id>.jsonl` and `.meta.json`.
- The clean config loads claude.ai-synced plugins, connectors and bundled skills unless
  each launch passes the canary settings (`driver/settings.py`).
- MCP tools are `mcp__plugin_stemma_stemma__<tool>`. They load deferred unless the
  server entry sets `"alwaysLoad": true`, which `plugin/.mcp.json` does (probe p11).
- PreToolUse input carries `agent_type`, `agent_id` (subagents), `session_id`,
  `tool_use_id`, `effort.level`; `updatedInput` can rewrite and add arguments; the MCP
  request `_meta["claudecode/toolUseId"]` equals the hook's `tool_use_id`.
- `"onFailure": "block"` blocks on crash, timeout and missing interpreter; exit 0 with
  junk stdout is read as allow.
- Depth: lead 0 → 1 → 2 → 3; depth 3 has no Agent tool.
- Lead effort comes only from `--effort` (frontmatter effort is ignored on the main
  thread); subagents follow frontmatter `model` and `effort`.
- Grep and Glob are not tools in 2.1.308.
- Under `--agent`, the init tool list is the agent's own frontmatter `tools` within
  `--tools`; the canary expects exactly that.
- Sandbox `denyWrite`/`denyRead` hold against case variants, `..`, symlinks, renames,
  paths not yet created and `**` globs (probes p9, p10).

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

## Decisions and deviations (each needs the orchestrator's eye)
- **The driver runs the CLI, not the Agent SDK.** claude-agent-sdk 0.2.174 builds the
  child's environment as `{**os.environ, **options.env}` (it can add keys, never remove
  one), and always adds `--input-format stream-json --system-prompt ""`. Inherited
  desktop variables break sign-in (probe p0) and `CLAUDE_CODE_EFFORT_LEVEL` must never
  reach a run, so the driver launches the pinned binary itself with `env -i` plus an
  allowlist. `tests/driver/test_driver_sdk_finding.py` shows the SDK leak. The pin stays
  in the `driver` extras for that test.
- **Identity travels as a stamp keyed by tool use id**, not as an injected argument,
  although injection works (p1, p6): nothing the model writes reaches the register as
  identity, and an argument named like an identity is ignored (test_mcp_server).
- **Verdicts are bound to the session started for them.** `record_claim_verdict` needs
  a `stemma:claim-checker` caller whose session the driver launched for
  `claim:<that id>`; `record_branch_verdict` likewise for `branch:<id>`, latest round.
- **Only `stemma:worker` may start as a subagent.** Checking roles started by an author
  would be contaminated checks; the hook refuses them, forks and built-in types, and
  strips `model`/`effort` from Agent calls.
- **Tool-only paths are wider than section 3 lists:** also `CLAUDE.md`, `.claude/`,
  `.stemma/` (the run plugin, session settings, driver state), `task/`, `status.*`,
  `deliverable/answer.reader.md`. An agent that could edit `CLAUDE.md` or
  `.claude/settings*.json` would change later sessions' rules.
- **Blind-role read denials are slightly wider than section 6:** the claim checker also
  cannot read `history/` (old frames), `restatement.md`, `deliverable/`, `reviews/`.
- **`finish_run` renders `deliverable/answer.reader.md`** (section 3 names the driver
  as its writer); it is written inside the gate's lock from the same register state
  the gate checked.
- **Re-asked checks are rounds.** A branch re-proposed, or a critique asked again, is a
  new check request (same id-only prompt, round n+1).
- **Tool names** (section 5 allows refinement): the thirteen listed, plus `get_tree`
  and `claim_dependents`.
- **Evidence stub.** Until part A lands here, the register reaches A's
  `stemma.evidence.api` if importable, else a stub only when `STEMMA_EVIDENCE_STUB=1`
  (recorded in `run.json`); never silently.

- **The launch is one function.** `stemma.driver.clean.clean_launch` builds argv, the
  environment (from nothing) and the settings file for any runner; `check_canary` with
  `stemma_expectation` or `bare_expectation` checks what loaded. It refuses
  `CLAUDE_CODE_EFFORT_LEVEL`, unproven `CLAUDE_*` and all `ANTHROPIC_*` variables, and
  refuses `--strict-mcp-config`/`--disable-slash-commands` with a plugin (p13).
- **`await_checks` waits for a check not yet requested**, up to its limit; returning at
  once made a placeholder checker give up (see `tests/e2e/RESULT.md`).
- **Contract v0.2.4:** `proceed_on_assumption`, gap type `user_held` (needs a recorded
  assumption), `finish_run` requires each assumption in the deliverable, verdicts only
  from the main thread of the session launched for that id, `Agent(stemma:<checking
  role>)` and `Agent(stemma:lead)` denied, `$STEMMA_CACHE` unwritable, single_loop
  denies `Agent` to the lead and records s3 `not_applicable`.

- **Merge-gate review fixes (after 09e15da).** Host writes go through
  `register/safeio.py` (no symlink followed, temp files in `register/tmp/`, rename by
  directory fd; FIFOs refused). The framer's frames and the three reviews are written
  by `record_frame`/`record_review`, accepted only from the session the driver launched
  for that role, id, version and latest round. `finish_run(version)` releases the frozen
  reviewed draft itself (`register/drafts/answer.v<n>.md`), one read for gate,
  deliverable and reader; `deliverable/`, `reviews/`, `framer/own-frame.md`,
  `framer/critique.md` and `tree.md` are tool-only. Reviewers read the frozen draft.
  Malformed tags are refused; segmentation fails closed (`tags.py`). Corrections must be
  new claims; changing a reviewed node reopens it. Torn log lines are moved to
  `<log>.torn`. The reviewer's 27 attack probes pass on 09e15da and fail now;
  `tests/register/test_review_attacks.py` keeps each.

- **Cross-family review fixes (after 4eea3ba).** Authorization and write share one
  register lock. `propose_branch_done` freezes `record.md` per round
  (`register/branches/<id>/record.r<n>.md`); the verdict binds to that digest and a later
  change to `record.md` reopens the node. `revoke_session` (called by the driver on a
  model or effort mismatch, which then relaunches the check, at most 3 attempts) discounts
  a session's verdicts, frames and reviews. Derivation re-runs: own process group,
  killed at the end, output to files, result read regular-file-only, no links, 4096-byte
  cap. A damaged frozen draft refuses the release; the driver exits 0 only on a recorded
  release. Agent writes outside the run folder are refused.

- **Every session reads only its own run (v0.2.8).** The Read guard refuses any path
  outside the run folder (the run's plugin is inside it). Bash's sandbox denies reads of
  the home folder, the runs folder, the repository, the plugin source and the fetch
  cache, and re-opens only the run and what the interpreter needs (its venv, its base
  install, the stemma package source); probes p14 to p16, live proof
  `tests/e2e/run_read_scope.py`. No extra-denials field was added.

## How it was shown
- 503 unit and hostile tests (`pytest tests`), no network; `ruff` and `mypy --strict`
  clean on `src/`.
- Race tests: two processes filing claims at once (register), four writing the World
  Model log at once (engine), both barrier-synchronised.
- World Model conformance: `sh conformance/run.sh` (memory store) and
  `sh conformance/run-file.sh` (the file store the register uses), unmodified runner,
  8/8 each (`conformance/RESULT.md`).
- Harness probes p0 to p13 (`docs/notes/register-guards-driver-probes.md`,
  `conformance/harness/`).
- End-to-end launch with placeholder roles (`tests/e2e/RESULT.md`).

## Known gaps
- The decider's-eye reviewer: Bash reads are confined by deny-all plus allowRead (p12);
  the Read tool's deny rules cannot be overridden by an allow, so the Read guard hook
  (`guards.guard_read`, which allows only `task/`, `intake.md` and its own draft) is
  what confines the Read tool.
- Resume after a real stop has been tested with the fake binary only.
- Part A's real evidence service has not been run with the register; the e2e used the
  stub (recorded in `run.json`).
- World Model features not built: clustering and scores, aggregates, snapshot writes,
  tombstones, unmerge, custom views, subscriptions.
