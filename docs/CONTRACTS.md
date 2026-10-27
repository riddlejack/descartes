# Contracts between the parts (v0.4)

These are the interfaces the parts agree on, so they can be built in parallel. The
design (`DESIGN-v1.md`, cited as "step n") says what the system does; this file says
where each thing lives and what crosses each boundary. Internals are the builder's.
A builder who finds a contract wrong, or impossible on the pinned harness, stops and
tells the orchestrator with evidence; nobody works around a contract silently.

v0.2 applies a blind pressure test of v0.1 (26 Oct). Items marked *(B verifies)* are
facts about the pinned harness that a reviewer checked in the docs but not by a run;
part B proves each with a tiny run before anything depends on it.

v0.4 applies the owner's ten decisions after run 1 (`V2-CHANGES-2026-10-27.md`, cited
D1 to D10); where an older line below disagrees, section 0 wins.

## 0. What v0.4 changed, and why (each with its decision)
- **task.json** (D1, D4, D2, D10; section 9) gains `scope` (`"question_only"`, the
  default, or `"broader_decision"`), `criteria` (a list of strings, at most 12), `options`
  (a list of at least two, only when the user named them), `roles` (`{<role>: {model,
  effort}}`, the same thing as `switches.s5_models`, which a task may give instead but
  not as well) and `stop_after` (`"full"`, the default, or `"frame"`). Every role, the
  lead included, defaults to Opus 5.5 at **high** effort (D10). `run.json` records
  `scope`, `options`, `stop_after`, the models, and `criteria: {source: "task" | "lead",
  file, count}`.
- **criteria.md** (D4) at the run folder's top: one line per criterion, `- C<n>: <text>`,
  ids C1, C2, ... in order. When the task gives criteria the driver writes it and no
  agent may change it; when it does not, only the lead's own thread writes it, during
  intake (no shell ever writes it; hostile tests). The criteria freeze when the first
  review is asked for (frame critique, branch proposal or draft submission, each refused
  without a criterion): a copy goes to `register/criteria/criteria.r<n>.md` and its
  digest is recorded; after that only the lead's `revise_criteria(text, reason)` changes
  them (never the user's). Each final review is bound to the criteria digest at the
  moment its draft was submitted (the request), not when it records; the gate refuses
  when criteria.md differs from the frozen copy or a review was asked to judge an
  earlier version.
  Whether a criterion is met is a reviewer's judgment, never code's.
- **Tree authority**: an author below the lead may add a node, never move one, change
  its branch, add an open lane, or place work under a set-aside node; only the lead
  does. A node with points sent back on it closes only through its own closing verdict's
  resolutions, never through its children.
- **Tree** (D2, D5; section 5): node kinds are `question` and `open_lane` only; options,
  rivals, gates and checks are never nodes (`tree_upsert` refuses any other kind). A node
  gains `set_aside`: the lead (only) gives the reason a node is set aside, or null to
  bring it back. A set-aside node, and every node under it, counts as closed (`closes_by`
  `set_aside` or `ancestor_set_aside`, status `set_aside`); its reason shows in
  `tree.md`, `gate_status` lists every set-aside node, and a branch under one needs no
  review. Brought back, a node starts again (`open`, or `in_progress` with a branch).
- **Review points** (D4; section 6): every point, final review or branch verdict, is
  `{point, material, node, criterion}`: `node` a tree node id or null, `criterion` a
  criterion id or null, at least one set, each checked to exist. The register gives each
  an id: `v<version>.<de|ea|mr>.<n>` for final reviews, `<branch>.r<round>.<n>` for branch
  verdicts. `gate_status(version)` returns, beside `unmet`, `open_material_points`
  (`{count, by_criterion, by_node}`) and `set_aside`.
- **A branch is sent back once** (D9): `record_branch_verdict` takes `points` and
  `resolutions`. `find_more`, `analyse` and `bound` send a branch back and need at least
  one material point; a branch already sent back (by a session not revoked) cannot be
  sent back again. Obligations follow the node as well as the branch id: a node moved to
  another branch carries its unresolved points. A closing verdict (`answered`, `accept_gap`) then gives a resolution
  `{point_id, status, note}` for every point sent back: `resolved`; `bounded` or
  `conditional` (the record snapshot must name the point id); or `disputed` (a revision
  order of the branch must name it, the lead having decided). `ask_user` is not a
  send-back. The gate re-checks that every sent-back point of a closed branch has a
  resolution. A closing verdict may carry new material points: they cannot go back,
  so they stay as the branch's open limits, listed by `gate_status` (`branch_limits`)
  and in `deliverable/record.json`.
- **Revision orders** (D6): `branches/<branch_id>/revision-<n>.md`, headings Finding;
  Raised by; Is it right?; Fix. The lead writes the finding, the worker says whether it
  is right (with evidence) and then fixes it, the lead decides disputes. After a
  send-back, `propose_branch_done` refuses, naming the ids, until each point sent back
  is named in `record.md` (resolved, bounded or conditional) or in a revision order with
  all four headings (disputed, for the lead's ruling).
- **Order heading** (D1): an order's third heading is "What would change the answer"
  (was "What would change the decision").
- **Parked switch values** (section 7): `s2_shape: single_loop` (the single author would
  need the workers' source tools, which the lead no longer has, D6) and
  `s3_hypothesis_in_briefs: true` (its hypothesis-blind control was a tree node, D2)
  are refused with `SwitchParked`; `s3_hypothesis_in_briefs` is `false` for v0.4 runs.
- **The lead reads no sources** (D6; section 6): its own thread may not call capture,
  capture_file, sec_filings, sec_capture, sec_facts, outline, read, read_section,
  search_store, tables, check_quote, query_data, WebSearch, WebFetch or Bash, and its
  Read is an allowlist of the run's approved artifacts (`stemma.hooks.guards.LEAD_READS`:
  intake, frame, criteria, tree, framer files, orders, restatements, branch records,
  revision orders, verdicts, drafts, reviews, the deliverable, the task's question,
  intake, options and task.json, register drafts, snapshots, frames and criteria,
  `analysis/**.py`, the run plugin), checked on the path with every link resolved, so a
  source copied into any other file stays out of its reach (Codex review #7). The
  PreToolUse guard enforces this for the lead's main thread (agent type `stemma:lead`,
  no agent id); its workers keep their tools and reads. Bash is refused outright because a guard cannot parse every way a shell command
  reads a file. Arrival notices tell the lead to have a worker capture the file.
- **Claim checks** (D7; sections 2 and 6): filing a claim requests no check. After
  `submit_draft(v)` the register puts every claim the frozen draft cites (by `[[c:…]]`
  tag), and every input of a cited estimate (through estimates of estimates, cited or
  not), into exactly one checker list, skipping claims with a verdict and claims an
  unfinished check already lists:
  - a claim with a home document (its first support item's receipt; for an estimate,
    the first document its derivation names) goes to that document's checker, prompt
    `doc:<receipt_id>`, list `checks/doc-<receipt_id>.r<round>.json` (tool-only);
  - a claim resting on no document (an estimate whose derivation names none, an
    assumption) goes to the draft's one derivations checker, prompt
    `derivations:v<v>`, list `checks/derivations-v<v>.r<round>.json` (tool-only), each entry
    with its tier, statement, value, derivation and the sentences of draft v that cite
    it (the checker reads nothing else of the draft). It judges whether each script
    computes what the claim states from the right inputs, and whether each assumption
    is stated as one; the exact re-run is code's.
  `claim:<claim_id>` is no longer a route. Each list is never changed once its check is
  requested (a list a crash left before its request is an orphan and is replaced); `check_assignment()` gives a checker its own list. A checker
  records verdicts only on the claims its own round's list names. Observed figures are still checked; the host
  still starts every checker. (Replayed on run 1's eleven drafts: 81 checker sessions
  where run 1 started 407.)
- **Reviewer memory** (D8): the branch reviewer of a branch is resumed (`--resume` of
  its previous session) for each re-review of that branch; a resumed session's earlier
  verification no longer counts until the driver verifies it again. The final reviewers
  are fresh each round; `submit_draft(v)` for v > 1 writes
  `register/drafts/previous.v<v>.md` (the previous round's points with their ids, and the
  lead's `draft/response.v<v>.md`); it refuses until each of the previous round's three
  reviews is recorded or its check ended for good, and until `draft/response.v<v>.md`
  names every point id of that round. On round v > 1 every final
  reviewer, the decider's-eye reviewer included, may read `criteria.md`,
  `reviews/v<v-1>/`, `draft/response.v<v>.md` and `register/drafts/previous.v<v>.md`
  (on round 1, `criteria.md` only, beside its usual reads); the Read guard and the
  settings allow exactly these, and nothing of the current round's other reviews.
- **Stopping** (D9): the answer is releasable when the latest round's final reviews
  raise no material point (and the rest of the gate holds); at `max_review_rounds` (a
  resource limit) the open points are listed in `deliverable/completion.md`, as before.
  `finish_run` also writes `deliverable/record.json`: criteria, set-aside nodes,
  assumptions taken, and the points left open, for the deliverable and the viewer.
- **Derivation bundles** (section 5): an estimate's re-run copies only its bundle: the
  script and the modules under its folder it imports statically, found by `ast`,
  transitively, at most 64 files and 4 MiB (resource limits); a link or an import that
  climbs out of `analysis/` is refused. At filing the bundle is stored at
  `store/derivations/<sha256>/` (`files/<path>`, `bundle.json`); its sha256 is the
  SHA-256 of canonical JSON `{format: "stemma-bundle/1", script, files: {path:
  sha256}}`, recorded as `derivation.bundle_sha256` and on the `derivation_reproduces`
  check (with `bundle_files`). A claim's id covers `derivation.bundle_sha256`, so a
  re-filing after a script edit is a new claim with its own checks. A filed claim
  re-runs only its stored bundle (each stored file read with its cap applied before
  reading), so a script edited after filing changes nothing filed. The
  sandbox puts the bundle's folder on `sys.path` under `python -I`.
  `stemma.register.derivation.build_bundle(run_dir, script_rel)` is the one builder;
  `stemma.register.filing.rerun_filed(...)` re-runs a filed claim.
- **`run_analysis(script, inputs, documents)`** (lead and workers): runs a script under
  `analysis/` exactly as a re-run does (its bundle, no network, no writes outside a
  temporary folder, a time limit) and returns the exact `result(...)` value, stdout
  (capped), the exit code and the bundle's hash; anything outside `analysis/` is
  refused. It writes nothing to the run. For the lead's own thread it takes claim inputs
  only: a `documents` argument is refused and no document reaches the sandbox, so
  `document()` fails (D6); workers keep `document()`. A live test on the pinned machine
  shows the sandbox refusing a network connection and a write outside its folder.
- **Sealed release**: after the `finished` fact, `finish_run` writes
  `release/manifest.json` (canonical JSON, format `stemma-release-manifest/2`: run id,
  version, release time; every regular file's path, size and SHA-256 except the exact
  operational files `stemma.register.manifest.EXCLUDED` names, by full path, never by
  folder or suffix, plus the driver-owned folders `.stemma/` and `.stemma-tmp/`, which
  no agent writes; `"log"` and `"checks"`: `{path, bytes, sha256, records or
  transactions}` for `register/log.jsonl` and `register/checks.jsonl`, each sealed as an
  append-only prefix taken in the same step; `"growing": ["register/calls/"]`, where new
  identity stamps may appear but sealed ones never change). Its own SHA-256 goes to
  `events.jsonl` (`release_sealed`) and the result, never the sealed log. A link or
  special file in the run refuses the release and blocks it. After the seal, only the
  excluded files may change, new lines may follow the sealed prefixes (reported by
  `manifest.after_seal`, never counted), and nothing else may exist under `release/`;
  `manifest.check(run_dir, manifest_sha256)` names every other difference.
  `manifest.read_log` and `manifest.parse_log` read the log with no writer or lock.
- **Frame completion** (Codex review #9): a frame-only run is complete only if, after
  every session has ended and been verified, the critique is from the latest round's
  verified session; otherwise it ends `ended_without_release` with what is missing.
  `criteria.md` is among the frame files whose settling it waits for.
- **A read-only register**: `Register.read_only(run_dir)` opens a run's register for a
  reader (a viewer, `stemma verify`): it creates, locks and writes nothing, and every
  write method is refused; `recorded_reviews`, `recorded_frames`, `run_facts` and
  `frozen_records` are its public reads. World Model's file log takes `readOnly`.
  `Register.from_sealed(run_dir, log=bytes, checks=bytes, refused=bytes | None)` reads a
  release as sealed: it folds exactly the given log prefix (World Model storage
  `bytes`) and takes requests, verifications, revocations and release blocks from
  exactly the given checks prefix (and refused corrections from `refused`), so nothing
  appended after the seal counts; a prefix ending inside a line is refused.
- **CLI**: `stemma report|verify|new` call builder W3's entry points
  (`stemma.report.site`, `stemma.report.verify`, `stemma.onboard.new`), or refuse with
  exit code 2 when they are not installed.
- **Host settings and secrets**: the driver passes the named non-secret host settings
  (`stemma.driver.config.HOST_SETTINGS`: STEMMA_SEC_IDENTITY, the data layer's mode and
  fallback, ZETARIS_REST_URL, ZETARIS_ORG_ID, STEMMA_ZETARIS_NAMESPACE, STEMMA_PG_HOST,
  _PORT, _DB, _USER, _JDBC_HOST) into every session's environment, and so to the MCP
  server; nothing else from the host. Secrets (ZETARIS_API_KEY, STEMMA_PG_PASSWORD)
  never enter any agent's environment: the driver writes them to
  `$STEMMA_RUNS/.host-secrets/<run_id>.json` (mode 0600, folder 0700, outside every run
  folder, removed when the driver finishes, rewritten on resume) and passes only its
  path, `STEMMA_HOST_SECRETS`; only the MCP server process loads it, refusing a file
  that is not a private regular file of this user. Sessions cannot read it (the Read
  guard and the Bash sandbox deny everything outside the run). The server also serves
  part D's `query_data`.
- **Task files notice**: every run's lead receives, with its first tool call, a notice
  naming the task files this run holds (`task/question.md`, then `task/intake.md` and
  `task/options.md` when present, `task/task.json`, the driver's `criteria.md` when the
  task gave criteria, and each attachment path for a worker to capture), since the lead
  has no folder listing.
- **Frame-only runs** (section 9): `stop_after: "frame"` in task.json, or `stemma start
  --frame-only`. The lead's first tool call carries a notice saying so, and the lead may
  start no worker (Agent is denied). The driver ends the run once intake.md, criteria.md
  (at least one criterion), frame.md, a tree, the blind framer's critique and a
  `framer/lead-response.md` newer than it all exist and have not changed for
  `frame_settle_s` (a resource limit, default 60 s), or the lead has stopped: it stops
  the lead and any check and ends with outcome `frame_complete`, exit code 8. A lead
  that stops before the frame is done ends as before (`ended_without_release`), with
  what was missing in the driver state's `frame_missing`.
- **Automatic resume after a usage stop** (section 9): when the account's limit stops a
  run (`usage_limited`), the driver waits until the limit resets (the reset time the
  harness reported, plus a margin) and resumes, at most `max_usage_resumes` times (a
  resource limit in `run.json`, default 6), logging each wait as a `usage_wait` event
  and each resume as `run_resumed`. The automatic resume holds the run lock throughout,
  waits included (another start or resume gets `DriverBusy`). Before each wait and
  resume it rereads the recorded outcome, the release state and the lead's remaining
  time (`lead_max_s` less the time run, from events.jsonl); it resumes only a
  usage-stopped run with time left, capped at that time, and otherwise records
  `usage_resume_refused`. A lead stopped by its time limit is terminal and never
  resumed automatically.
- **Verdict writes** (Codex review #6): a branch verdict's authoritative record is
  written first; a crash between its writes is repaired, idempotently, by the next
  writer under the lock, so no send-back is ever lost.

## 0b. The lead stays until the release (run 3, 27 Oct)
- **Stop hook.** The plugin's `Stop` hook (`stemma.hooks stop`, timeout 300 s) answers
  for the lead's own session only (the session id in the driver's state). Until
  `finish_run` is accepted, the run is frame-only, or the lead's time limit has passed,
  it refuses the stop (exit 2) with a reason naming what is in flight: branches ordered
  with no record, branches in progress, branches awaiting their verdict, checks the host
  is running or will start; it says to wait with `await_checks` and that background
  workers report back. Before refusing it waits inside the hook, up to 240 s and never
  past the lead's time limit (resource limits), for any of that to move, so a waiting
  lead costs a turn per change. After 30 refusals in a row with nothing changing (a
  resource limit) it lets the lead stop. Every other session stops; a failure in the
  hook lets the session stop (and is logged), since the driver resumes the lead.
  Events: `lead_stop_refused`, `stop_allowed`, `stop_hook_failed`.
- **Driver resume.** A lead whose session ends (completed or failed) without an accepted
  `finish_run`, outside a frame-only run, a usage stop or a blocked release, with lead
  time left, is resumed with `--resume`, at most `max_lead_resumes` times (a resource
  limit, default 3), with a notice on its first call naming the work in flight and the
  background workers the harness killed when it ended (from its stream's
  `task_updated` killed records), so it re-dispatches them. Events:
  `lead_resumed_after_end`, `lead_not_resumed`.
- **Lead time, one count** (`stemma.driver.leadtime`): the lead's execution time is the
  sum of its sessions, never the gaps between them (usage waits, a driver that was
  down); every launch gets only what is left of `lead_max_s`, and the Stop hook, the
  resume of an ended lead and the automatic resume all read the same count.
- **Running-time notices** (27 Oct): at each fraction in `limits.lead_notice_fractions`
  (run.json, a resource limit, default 0.6 and 0.8) of `lead_max_s`, the driver queues a
  notice for a running lead of an unreleased run, delivered like an arrival on its next
  tool call: how many minutes it has run and how many remain; the last says to submit
  its draft and call `finish_run` before the limit, since a lead stopped at the limit
  ends the run with no deliverable. Each fraction is told once, across resumes; a lead
  that has ended, or a run already released, is told nothing. Event: `lead_time_notice`.
- **A usage stop is shared at once:** any session's usage rejection sets one stop flag
  and persists `usage_limited` under the lock every lead launch decision takes (a
  session already marked rate-limited counts, even while its reader waits to persist
  it, and the flag is read again right before the launch); the Stop hook then lets the
  lead stop, so the supervisor recovers the run after the reset. A resume clears the old
  stop, in memory and on disk, under the same lock, before the lead launches.
- **A crashed driver's lead session** (no `session_ended`) is closed at the last activity
  the log holds before a resume logs anything, so the downtime is never lead time.
- **Deliberate stops:** a lead stopped on purpose (SIGTERM or SIGINT sent to its
  process, not by the driver) ends `lead_cancelled` (exit code 9) and is never resumed.
- **Notices name every unfinished worker:** confirmed kills, and workers started with no
  end recorded (possibly interrupted by a crash). The Stop hook's idle-refusal count
  starts afresh for each driver-authorised launch.
- **Live** (`tests/e2e/stop_hook_probe.py`, pinned 2.1.308, headless): the hook refused
  the lead's stops, the background worker ran to completion (none killed) and its
  completion reached the lead.
## 0a. The second model family (N1; built against fakes, pending the owner's choice)
- **What it does.** After a draft is submitted and a cited claim has a Claude verdict
  from a verified session, the host (the driver, never an agent) asks a Nemotron model
  through Meterless Nightshift whether the claim's exact stored passage (each quotation
  with up to 600 characters of stored context on each side, a resource limit) supports
  the claim as stated (entity, period, unit, scope, direction of change): `supports`,
  `does_not_support` or `cannot_tell`, with a one-line reason, as JSON. Models:
  `nvidia/nemotron-3-ultra-550b-a55b`, then `nvidia/nemotron-3.5-lightning-30b-a3b` when
  the first cannot be asked or answers unreadably (recorded in run.json). Claims resting
  on no passage (estimates, assumptions) are not asked.
- **Recorded** as a `second_family` fact on the claim: the answer, the model asked, the
  model the answer names, the Nightshift receipt (no token, no message text), and the
  Claude verdict and session it was compared with. Each claim is asked once.
- **It only adds a check.** A disagreement (Claude approves and the answer is
  `does_not_support`, or Claude rejects and the answer is `supports`; `cannot_tell` and
  `could_not_check` disagree with nothing) makes the host start one fresh Claude claim
  checker, prompt `recheck:<claim_id>`, whose assignment
  (`checks/recheck-<claim_id>.r<n>.json`, via `check_assignment`) holds the claim, its
  support, both opinions and their reasons. That checker's verdict, the newest, decides.
  The second family's answer never changes a verdict itself: it never approves and
  never lowers. While a disagreement's third look is pending or running, the gate
  refuses the release; once it has ended, the usual verdict rules apply. Every answer
  and disagreement, with what the third look decided, is in `deliverable/record.json`
  (`second_family`) for the viewer.
- **Never blocks.** When Nightshift is not configured, unreachable, refuses the key
  (`provider_auth_failed`) or cannot route the model, the run records
  `second_family_unavailable` (an event and a register fact, once) and carries on;
  nothing about it can refuse a release. Questions still in flight at release are
  dropped, never recorded into the sealed run. At most `max_second_family` questions
  are in flight at once (a resource limit, default 2).
- **Setting.** task.json `second_family`: `true`, `false`, or absent (on exactly when the
  host has Nightshift configured: `METERLESS_RUNTIME_TOKEN` or `METERLESS_STATE_DIR`);
  run.json records `second_family: {enabled, source, nightshift_configured, models}`.
  The runtime token stays in the driver process and never enters a session.
- **Pinned model.** The Nightshift client sends a provider model id (for example
  `nvidia/nemotron-3-ultra-550b-a55b`) by name only when `GET /v1/models` lists it, and
  refuses one it does not list; `meterless/*` routes work as before. The receipt says
  `pinned` and records the model the answer names; an answer named by any other model
  than the one pinned is a failed second-family verdict for that claim (recorded on the
  claim, loudly, never a silent substitute); the claim's Claude verdict stands and it is
  not asked again. An unreadable answer from both models is the same.
- **Reading the answer** (Codex review of N1): only a final answer counts: every complete
  think block is removed, an unfinished one is refused, and the reply must end with one
  JSON object, `{verdict, reason}` and nothing else, nothing after it; an answer cut
  short (`finish_reason` other than `stop`) is unreadable. A passage that cannot be
  placed in the stored text ends the claim's look before anything is asked; an answer
  named by another model ends it at once (no fallback rescues it); a gateway error or a
  malformed response is this claim's failure, while a refused key, an offline gateway
  or an unlisted model is the run's. A failure is recorded on the claim (never asked
  again); a claim is put to the second family at most `max_second_family_attempts`
  times (a resource limit, default 2).
- **What is kept:** the receipt is cut to an allowlist (route, pinned, sizes and
  digests, timing, HTTP status, served model, finish reason, whole-number usage), every
  string scrubbed of registered secrets; headers and response ids are dropped. Every
  attempt's cleaned receipt is kept, for the spend.
- **Sealed runs:** every second-family write checks, inside the register's lock, that
  the run is not released, and refuses after the seal (a late answer is discarded,
  logged only to events.jsonl).
- **The third look is recoverable:** a disagreement whose request was cut off by a crash
  gets it on the driver's next step (`repair_rechecks`), and the gate counts a
  disagreement with no request as an unfinished third look.
- **Role text:** the claim checker's text must handle `recheck:<claim_id>`.
- **Replay:** `tests/e2e/replay_n1.py <copy of run 1>` builds every question from run 1's
  stored passages (248 claims with a Claude verdict and a passage; run 1 started 407
  checks) and asks only with `--live`.

## 0c. Removing a tree node (tree-skill development runs, 27 Oct)
- **Why.** `tree_upsert` could not remove a node, so a lead retired the nodes it had
  merged by setting them aside. That left duplicates in `tree.json` and would list them
  as set aside in the deliverable. Set-aside keeps its one meaning: the node cannot
  change the headline.
- **The call.** `tree_upsert({"id": <node>, "remove": <reason>})`, the lead only (not a
  subagent of the lead), with nothing else in the object; the reason is text, at most
  `SET_ASIDE_MAX_CHARS`.
- **What holds a node** (exact facts; any one refuses the removal, and the refusal
  names each): its child nodes; a branch assigned to it now or at any time before; a
  status other than `open` or `set_aside`; any fact in the register's whole log about
  it or whose value holds its id, revoked sessions' facts included (among them a branch
  verdict on it, a review point naming it, an assumption); a branch order (`order.md`)
  or revision order (`revision-<n>.md`) anywhere under `branches/` naming its id as a
  whole id (each run of id characters is one id, less trailing full stops, so
  `q9._child` is not `q9`, and no node id ends in a full stop). Its own set-aside mark
  does not hold it, and neither does a child moved away earlier (moving the children
  is how a duplicate merges). A set-aside node nothing holds can be removed.
- **What is not read.** No other file is scanned. A narrative that names the node
  does not hold it: the framer's critique (which may say "q9 duplicates q3", the case
  removal is for), a branch record, a restatement. The lead removes on its own reading
  of those.
- **What is recorded.** The node's record gains `removed: {reason, at}` and leaves the
  tree: `tree()`, `get_tree`, `tree.md`, the closure, the gate and `set_aside` no longer
  see it. `tree.json` lists it under `removed` (`id`, `statement`, `reason`), and the
  run's log holds a `node_removed` fact (`node`, `statement`, `reason`, `at`). A removed
  id is never used again: `tree_upsert` refuses it, as a node, a parent or a removal.
- **Races.** The check and the write happen under the register's lock, so a branch
  assignment or a new child racing a removal either lands first (and the removal is
  refused, naming it) or finds the node removed (and is refused). A review or branch
  verdict checks its points' nodes again under the same lock before it writes, so a
  point never lands on a node removed after the first check.

## 0d. An as_of run sees no live web (reality-graded runs, 27 Oct)
- **Why.** A run with `as_of` is graded against what happened after that date, so
  nothing it reads or searches may show later information. Date-filtered web search
  leaks (updated pages, ranking, related-content modules), so it is not used.
- **Built-in web tools.** When `run.json` has `as_of`, the driver leaves `WebSearch`
  and `WebFetch` out of every session's `--tools` (by name or as a rule) and denies
  both in its settings; the guard refuses both to every caller, subagents included,
  and refuses them too when `run.json` is missing, unreadable, or its `as_of` is not a
  `YYYY-MM-DD` date. Tool arguments never change this; only `run.json` (driver-written,
  tool-only) says what the run is. A run without `as_of` is unchanged (`WebFetch` stays
  denied in every run).
- **The shell.** Every session's sandbox has `network.allowedDomains: []`,
  `allowUnsandboxedCommands: false` and `failIfUnavailable: true`, as in every run.
- **The broker.** Every other path to the network runs in the MCP server: `capture`
  takes an archived copy dated before `as_of` (section 4), and the `sec_*` tools keep
  only filings dated before it. A refusal says nothing dated on or after `as_of`: a
  `sec_capture` of a later filing and one of an accession that does not exist get the
  same refusal (`no_filing_before_as_of`, the accession given and nothing else), and a
  later filing is never remembered in the store. Every SEC redirect hop stays in the
  approved filing's own folder on `www.sec.gov` (or the requested path on
  `data.sec.gov`), or the fetch is refused. No error page's body is stored in an
  `as_of` run, and no response holding later content is stored where a session can
  read it. `archived_links` reads only a successful capture shown to predate `as_of`
  (an archive copy with a full capture timestamp before it, or a filing dated before
  it). The broker reads `as_of` from `run.json` on every call; a `run.json` that is
  missing, unreadable or not an object, or whose `as_of` is set but not a real date
  (an empty one included), refuses the call (`bad_run_json`), never falls back to live.
- **Discovery.** No keyword web search: a worker browses archived pages (`capture` a
  front, press or investor page, then `archived_links`, each link captured in turn) and
  searches filings with `sec_search`, whose end date the host sets (section 4); rates
  and prices come from `market_series`, its end set by the host too. The worker and the
  claim checker hold these tools and are told so in one line each.
- **Every path an as_of run has to the network** (the list the leakage audit scans; any
  other is a defect). Agents' own tools reach none: no `WebSearch` or `WebFetch`, and
  Bash, `run_analysis` and the derivation sandbox have no network. The host reaches:
  1. the model provider, by the harness: model calls only, which carry no source;
  2. `web.archive.org`, by the broker's `capture`: the CDX index (`/cdx/search/cdx`,
     `to=` the day before `as_of`) and replays (`/web/<timestamp>id_/<url>`), every hop
     on that host and dated before `as_of`, the replay's `Memento-Datetime` too;
  3. SEC EDGAR, by the broker: `efts.sec.gov` full-text search (`sec_search`, end date
     set by the host, later hits withheld); `www.sec.gov/Archives/` documents
     (`sec_capture`, only filings dated before `as_of`, refused before any fetch
     otherwise); `data.sec.gov` XBRL company concepts (`sec_facts`, facts filed before
     `as_of` kept, the raw answer's text withheld); and the edgartools library's own
     calls for company lookup, filing lists and accession lookup (`sec_filings`,
     `sec_capture`), whose answers are filtered by filing date before anything
     returns. The company name and ticker mapping is today's (a warning says so). The
     raw XBRL answer is not stored in an `as_of` run (it holds later facts), only
     the facts filed before `as_of`;
  3a. `fred.stlouisfed.org` (`fredgraph.csv`) and `stooq.com` (`/q/d/l/`), by the
     broker's `market_series`: the end date set by the host, every row checked before
     anything is kept;
  4. the run's own data-layer store, when configured (Postgres and Zetaris, one
     database per run, holding only this run's evidence);
  5. the second-family provider, by the register: claim text and source passages go
     out, verdicts come back; whether that model's training reaches past `as_of` is a
     model choice for the owner, not a path this contract can close.
  The driver itself makes no network call: it starts sessions and reads their streams
  (usage and limits come from those streams).
- **The bare arm.** A head-to-head bare arm given an as-of date gets the same: no live
  web tool, the guard as a hook, Bash without network, and the broker serving its
  folder with exactly the evidence tools the worker holds (`hackathon/headtohead/bare.py`).

## 1. Parts and owners
- **A. Evidence service:** `src/stemma/evidence/`, `tests/evidence/`.
- **B. Register, guards and driver:** `src/stemma/register/`, `src/stemma/hooks/`,
  `src/stemma/driver/`, `src/stemma/analysis/`, `src/stemma/mcp_server.py`,
  `plugin/.mcp.json`, `plugin/hooks/`, `conformance/`, their tests.
- **C. Roles and skills:** `plugin/agents/`, `plugin/skills/`, `plugin/lead/`,
  `plugin/STANDING-RULES.md`, `docs/ROLE-MAP.md`, `tools/roles/` (the role-file lint
  and the role-map generator), `tests/roles/`.
- **Each part** writes its notes in `docs/notes/<part>.md`, and adds pinned
  dependencies only to its own extras group in `pyproject.toml` (A: `evidence`; B:
  `server`, `register`, `driver`); the orchestrator resolves any merge there.
- **Orchestrator:** this file, `AGENTS.md`, `plugin/.claude-plugin/plugin.json`.
- **D. Hackathon adapters:** `src/stemma/datalayer/`, `src/stemma/providers/`,
  `hackathon/`, `run.py`, `Dockerfile`, their tests; at merge, the six-line hook in
  B's `register/store.py` that routes derivation inputs through the data layer.
- Later: **E. Checker tests** (`tests/checkers/`), and the
  paid search service the design names (one chosen by a small test; needs the
  owner's account). Until then, discovery is Claude Code's WebSearch.

## 2. How a run executes
**Authors start authors; the host starts every check.** Workers are started by the
lead (or a branch lead) as subagents. Every checking role (blind framer, claim
checker, branch reviewer, decider's-eye reviewer, evidence auditor, mechanical
reader) and the retrospective writer is started **by the driver**, as its own
headless session, when the register records the event that calls for it. So each
check is guaranteed to run, is fresh by construction, runs on the model the driver
set and verified, cannot be contaminated by its author's prompt, and does not depend
on how deep subagents may nest. This is the design's "code guarantees each check
ran" (commitment 3) made structural.

1. The driver (B) creates a run folder, writes `run.json` from a `task.json`, and
   assembles a **per-run plugin folder** holding only what the run's switches select
   (section 7), recording its digest. It starts the **lead** headless with
   `--agent stemma:lead`, the run folder as working directory, that plugin, the
   pinned binary (`STEMMA_CLAUDE_BIN`), the clean config (`STEMMA_CLAUDE_CONFIG`),
   per-run settings (section 6) and the standing rules written as the run folder's
   `CLAUDE.md`. The lead gets `--model` and `--effort` on its command line (a
   `--agent` main session ignores frontmatter effort). Nothing else loads, checked
   by a canary at launch: every session starts from `env -i` plus an allowlisted
   environment (the desktop app's inherited environment breaks the sign-in), with
   per-launch settings that switch off the clean config's claude.ai-synced plugins,
   skills and connectors, bundled skills and built-in plugins, and with `--tools`
   naming the built-in tools allowed. Never `--bare` (it skips plugin hooks). The driver sets `STEMMA_RUN_DIR` and `STEMMA_PYTHON`
   (the interpreter with every part's dependencies) for every session; the MCP
   server and hooks find the run folder and the interpreter through them.
2. The driver watches the register and the tree, and launches each checking session
   with `--agent stemma:<role>` and a prompt holding only an id:
   - `submit_draft` → one claim checker per source document the draft's cited claims
     rest on, prompt `doc:<receipt_id>`, and one for the cited claims resting on no
     document, prompt `derivations:v<version>` (v0.4, D7);
   - `propose_branch_done` → a branch reviewer, prompt `branch:<branch_id>`, resumed
     from its previous session on each later round (v0.4, D8);
   - run start → the blind framer, prompt `frame:own`; the lead's
     `request_frame_critique` → the framer again, prompt `frame:critique`;
   - the lead's `submit_draft` → the decider's-eye reviewer, the evidence auditor
     and the mechanical reader, each with prompt `draft:<version>`;
   - `finish_run` accepted → the retrospective writer.
   Concurrency of checking sessions is a recorded resource limit in `run.json`.
3. Authors wait for checks with `await_checks(ids, max_wait_s)`, which returns the
   verdicts that have landed and lists the ones still running. It never invents one.
4. All agents reach the evidence service and the register through one MCP server,
   `stemma` (tools named `mcp__plugin_stemma_stemma__<tool>`, verified). MCP tools
   load at start because `plugin/.mcp.json` sets `"alwaysLoad": true` (verified; without
   it 2.1.308 defers them), so role texts need no `ToolSearch` step.
5. Agents exchange work through files in the run folder (section 3), never through
   retold figures: a figure crosses a hand-off as a claim id.
6. The run ends when `finish_run` is accepted (section 6).

The product's own concurrency and spending limits are resource limits recorded in
`run.json`. They are unrelated to the two-helper cap on builders.

## 3. The run folder (`$STEMMA_RUNS/<run_id>/`)
Markdown is for files only agents read; JSON is for anything code reads. Files
marked *tool-only* are written by an MCP tool or the driver, never by an agent
(section 6 says how that is enforced).
- `run.json` (driver): run id, task, `as_of` date or null, switches, model and effort
  per role, harness version, plugin digest, resource limits.
- `task/` (driver): the question, intake answers if supplied, attachments.
- `intake.md` (lead): the five intake answers and the restated job (step 1).
- `criteria.md` (driver from the task, or the lead at intake): what done means (v0.4).
- `checks/doc-<receipt_id>.json` (*tool-only*): the claims a document's checker checks.
- `frame.md` (lead, living): the frame of step 2, including the working answer, the
  rival, and the cards loaded with why. Earlier versions under `history/`.
- `tree.json` (*tool-only*): the issue tree (section 5). `tree.md` is a rendering.
- `framer/own-frame.md`, `framer/critique.md` (blind framer);
  `framer/lead-response.md` (lead).
- `branches/<branch_id>/`:
  - `order.md` (lead or branch lead), with exactly these headings: Intent; Place in
    the tree; What would change the answer; End product; Out of scope; Known facts
    (each a claim id, or marked unverified); Where to look; What to return.
  - `restatement.md` (worker): intent and plan in its own words, written first.
  - `record.md` (worker), with exactly these headings: Conclusion and confidence;
    Claims (ids); Model; Rival tested; Methods tried and what each returned; Gaps
    (typed); Implications for other branches; What was not checked.
  - `verdicts/<n>.json` (*tool-only*): branch reviewer verdicts.
  - `revision-<n>.md` (lead and worker): revision orders (v0.4, D6).
  - `sub/<branch_id>/` for a branch lead's own branches, same shape.
- `store/` (*tool-only*, A): the evidence store (section 4).
- `register/` (*tool-only*, B): the claim register (section 5).
- `analysis/` (workers, lead): recorded calculation scripts (section 5) and outputs.
- `draft/answer.v<n>.md` (lead); `submit_draft` freezes a version into
  `register/drafts/answer.v<n>.md` (tool-only), which the reviewers read.
- `reviews/v<n>/deciders-eye.md`, `evidence-audit.md`, `mechanical.md`.
- `deliverable/answer.md` (lead: the final slide-by-slide text, tagged as section 5
  says); `deliverable/answer.reader.md` (driver: tags rendered as references); a deck
  later.
- `events.jsonl` (*tool-only*, driver and hooks): every launch with its resolved model
  and effort, every file write with the role that made it, denials, artifact-landed
  events, compactions.
- `status.json`, `status.html` (driver): running, landed, waiting on the user.
- `retro.md` (retrospective writer).

## 4. The evidence service (A)
**WebFetch is denied** (step 6); so is network access from Bash (section 6). Every
source an agent reasons about comes through these tools. Offsets everywhere are
Unicode code points in the stored text.
- `capture(url, reason)` → a receipt: `receipt_id`, `sha256` of the exact bytes,
  `status`, HTTP facts, `content_type`, `text_path` (relative to the run folder),
  `text_chars`, `outline` (headings or pages with offsets), `warnings`,
  `served_from_cache`. Every attempt writes a receipt, failures included. Stored text
  is the whole page or document converted by B1's picks (markdownify over the whole
  HTML minus scripts and styles; pypdfium2 for PDFs); nothing in between decides what
  counts as content. Empty or implausibly short text is a loud `status`, never a
  silent success; Playwright is tried only then. When `run.json` has an `as_of`
  date, every capture comes from an archived copy dated before it, or fails with
  `no_snapshot_before_as_of`.
- `capture_file(path, reason)` → a receipt, for the user's attachments.
- `sec_filings(company, form=None, filed_after=None, filed_before=None)` → filings
  with accession numbers and filing dates (`filed_before` defaults to `as_of`);
  `sec_capture(accession, document=None)` → a receipt;
  `sec_facts(company, concept, filed_before=None)` → as-filed XBRL facts, each with
  its filing date and the receipt of the filing it came from.
- `sec_search(query, forms=None, cik=None, page=1)` → EDGAR full-text search hits
  (`accession`, `form`, `filing_date`, `filers`, `ciks`, `document`), 100 a page, pages
  1 to 20; in an `as_of` run the host sets the end date to the day before `as_of` and
  withholds any hit filed on or after it without a trace (no total, no highlighted
  text) (section 0d).
- `market_series(source, symbol, reason)` → a receipt for a whole dated series as CSV:
  `source` "fred" (a FRED series id, `fredgraph.csv`) or "stooq" (a daily price
  history); the caller names the series, never a date. In an `as_of` run the host sets
  the end to the day before `as_of` (FRED `coed`, stooq `d2`), and checks every row's
  date before anything is cached or stored: a row on or after `as_of` refuses the
  whole series (`series_past_as_of`, the later date never shown), an answer that is not
  a dated series is `series_unreadable`, and a live response's headers other than its
  content type are not kept (section 0d).
- `archived_links(sha256)` → the links of a captured HTML page, absolute against the
  page's own URL, each with its text; a link into the Wayback Machine is given as the
  page it copies; refused for a page that is not HTML, and in an `as_of` run for a page
  captured from the live web (section 0d).
- `outline(sha256)`, `read(sha256, start, length)`, `read_section(sha256, anchor)`:
  reading in parts; no tool returns a whole long document at once.
- `search_store(query, mode="words"|"exact", sha256=None, limit)` → hits with
  `sha256`, `receipt_id`, offsets, a short snippet (FTS5: porter for words, trigram
  for exact strings such as figures).
- `check_quote(receipt_id, quote)` → found or not, with offsets; exact match after a
  documented normalisation of whitespace and quotation marks, never matching from or
  to the middle of a number. It also reports whether the match lies in a region the
  extractor marked as not visible to a reader (hidden HTML, invisible or white PDF
  text) or in a table the converter may have misaligned (merged cells): a warning for
  the checker, never a refusal.
- **The extractor never deletes or joins.** Hidden and invisible text stays in the
  stored text, its ranges recorded on the receipt; elements whose text would run
  together (superscripts, subscripts, adjacent blocks) are separated so no two
  figures fuse into one.
- B's register calls the
  same function through `stemma.evidence.api.check_quote(run_dir, receipt_id, quote)`;
  `stemma.evidence.api.read_text(run_dir, receipt_id)` returns the stored text.
- `tables(sha256)` → Docling's table view, a derived view never used for quoting
  (optional extra; `not_installed` when absent).
- Store layout: `store/bytes/<sha256>`, `store/text/<sha256>.txt`,
  `store/receipts/<receipt_id>.json`, `store/index.sqlite`. Bytes are written once
  and never changed. Each receipt records the extractor and its version.
- **One store per run; none shared across runs.** An expensive fetch may be cached in
  `$STEMMA_CACHE`, keyed by the exact request (URL, `as_of`, extractor versions); a
  run gets cached bytes only when it makes that same request itself, and its receipt
  says `served_from_cache` with the original fetch time.

## 5. The claim register and the tree (B)
**Claim.** Fields the agent supplies: `statement` (the sentence as it will be used);
`value` (optional: `number`, `unit`, `period`, `kind`, `as_written`, and `denominator`
when `kind` is `ratio`, `share` or `rate`); `tier`; `support` (a list of
`{receipt_id, quote}`); `derivation` (optional, below); `branch_id`; `entities`
(names). Fields the code writes: `claim_id` (content-derived), `author` (the role,
from the harness), `created_at`, each code check's result, `supersedes`,
`superseded_by`.
- **`kind`:** `level`, `count`, `ratio`, `share`, `rate`, `change`. Which one is right
  is the checker's judgment; code only requires a denominator for the three that
  need one.
- **Tiers** (one vocabulary for all roles): `observed` (in a captured source, by its
  author's own measurement); `interested_party` (stated by a party with a stake,
  tiered as theirs); `secondary` (a source reporting someone else's figure);
  `estimated` (computed by us, with a derivation); `assumed` (an input we chose, the
  reason in the statement). The model's own knowledge is never a claim.
- **Code checks at filing** (exact facts only, step 8):
  - for `observed`, `interested_party`, `secondary`: at least one support item; each
    quotation is found in its receipt's stored text; `value.as_written` (the figure
    exactly as the source writes it, e.g. `1.2bn`) appears inside a quotation.
    Whether `number` and `unit` correctly read `as_written` is the checker's call;
    code may attach a parse warning, never a refusal;
  - for `estimated`: a derivation whose re-run reproduces `number` exactly;
  - for `assumed`: no support required;
  - for `ratio`, `share`, `rate`: a filled `denominator`.
  A failure refuses the claim and names the failed check, nothing more.
- **Derivations.** A script in `analysis/` reads its inputs only through
  `stemma.analysis.inputs()` (a mapping from the claim ids named in the derivation to
  their values) and `stemma.analysis.document(receipt_id)` (the stored text of a
  capture the derivation names), and reports with `stemma.analysis.result(number)`.
  A derivation is `{script, inputs: [claim ids], documents: [receipt ids]}`; the
  re-run copies the script, its sibling `.py` files and exactly the named documents
  into its sandbox, nothing else, so a figure computed from a captured dataset
  re-runs from the stored bytes. No script opens `store/` paths directly. `file_claim`
  re-runs it in a subprocess with no network, no write access outside a temporary
  folder, and a recorded time limit, and compares the reported number with the
  claim's. Agent-written code never runs inside the MCP server's own process.
- **Verdicts** (only from a claim-checker session): `holds`; `holds_with_correction`
  (the checker files the corrected claim, which supersedes the old one and is
  recorded `holds` at filing by the same checker); `not_supported`;
  `could_not_check`. Each carries its evidence.
- **Corrections** never delete: a superseded claim is closed and points to its
  successor. The dependency index is built by code from the `[[c:…]]` tags in every
  file written in the run, so each owner of a use can be told to re-derive.
- **Entities** go through World Model's near-duplicate review queue; mapping a name
  to a parent needs two sources.
- **Echo candidates:** claims citing the same document are listed for the lead to
  judge; code never decides independence.
- **Meterless World Model.** The register is World Model's data model: content-derived
  ids, the append-only log, superseded links closed and never deleted, idempotent
  re-ingest, the review queue. It must pass World Model's own conformance runner,
  unmodified, against the code that runs in production; any bridge between the runner
  and that code holds no logic. That passing run is the hackathon's Engine test.
- **Storage:** `register/log.jsonl`, append-only under an exclusive file lock (several
  processes write), and a derived SQLite index rebuilt from the log.
- **Tree** (`tree.json`): nodes with `id`, `parent`, `kind` (`question`, `open_lane`;
  v0.4), `statement`, `doubt`, `settles_by`, `flip_point`, `crux` (true for the one to
  three the answer turns on), `branch_id`, `status`, `set_aside` (the reason, or null).
  Every change goes through a tool and leaves a history entry.
  - Status: `open` → `in_progress` (lead dispatches) → `proposed_done` (worker) →
    by the branch reviewer's verdict: `answered`, `gap_accepted`, back to
    `in_progress` (`find_more` with the method named, `analyse`, `bound`), or
    `waiting_user` (`ask_user`). The driver sets `stopped_by_limit` when a recorded
    resource limit is reached.
  - **A branch waiting on the user.** The driver puts the question on the status
    page. If no answer arrives (always so in a headless run, where the task package's
    intake answers are all the user will say), the lead calls
    `proceed_on_assumption(branch_id, assumption)`: the node returns to
    `in_progress` with the assumption recorded, and the branch reviewer may later
    close it `gap_accepted` with gap type `user_held`. The deliverable lists each
    such question with the assumption taken. `finish_run` refuses while a node is
    `waiting_user`.
  - **Branch verdict:** `{branch_id, verdict: answered | find_more | analyse | bound |
    ask_user | accept_gap, reason, method (for find_more), record_digest}`.
- **Tools** (B may refine names, then records them here): `file_claim`,
  `record_claim_verdict`, `get_claim`, `find_claims`, `echo_candidates`,
  `entity_review`, `tree_upsert`, `propose_branch_done`, `record_branch_verdict`,
  `request_frame_critique`, `record_frame` and `record_review` (accepted only from the
  session the driver launched for that role, id, version and round), `submit_draft`,
  `await_checks` (accepts claim ids, branch ids and draft versions),
  `proceed_on_assumption`, `get_tree`, `claim_dependents`, `finish_run(version)`,
  (v0.4) `gate_status`, `run_analysis`, `check_assignment`, `revise_criteria`
  (which copies the frozen, reviewed draft into `deliverable/` and writes
  `answer.reader.md` from the same bytes), and part D's `query_data(sql)`.
- **Tags.** In drafts and the deliverable each figure is followed by `[[c:<first 12
  hex of claim_id>]]`; a number that is not a figure (a date used as a date, a label,
  a slide number) by `[[nc:<why>]]`. A *segment* is a sentence, list item, table cell
  or heading. A number token is a run of digits with optional sign, separators,
  decimal point, percent sign or scale suffix, found in the text with every tag
  removed first. Code requires each segment holding a number token to carry a tag;
  whether each tag covers each number is the evidence auditor's judgment, and the
  `nc` tags are the mechanical reader's.

## 5a. The data layer (D)
- Evidence-derived tables (receipts, claims and verdicts, as-filed SEC facts, tables
  extracted from captures) are synced into Zetaris as one semantic layer, through a
  route Zetaris can reach; `datalayer/` in the run folder is tool-only.
- `query_data(sql)`: read-only SQL over those tables for agents' analysis, through
  Zetaris when configured and SQLite otherwise; every query logged with its text,
  backend and row count. Rows carry the receipt or claim ids they came from; a figure
  taken from a row is still filed as a claim with its own support.
- Derivation inputs on re-run: with `STEMMA_DATALAYER=zetaris`, the register resolves
  each input id itself, then reads the number through Zetaris (logged). A number that
  differs from the register's by a single bit refuses the claim (`input_mismatch`).
  Zetaris unreachable: the register's own number is used and `datalayer_unavailable`
  is logged loudly on the run; the research path never stops or changes for it.
- SAMPLE_MODE needs neither Zetaris nor Nightshift.

## 6. What code guarantees, and who produces what it reads
Each guard has a producer in the live path, or it would block every run.
- **Every guard hook fails closed** (`onFailure: "block"`, verified): a crashed or
  timed-out guard stops the tool. A hook that exits 0 with unreadable output counts
  as allow, so every guard catches its own errors and denies (exit 2 or deny JSON);
  a test feeds each guard malformed input.
- **Bash runs sandboxed** with no network and no write access to tool-only paths
  (`store/`, `register/`, `tree.json`, `verdicts/`, `events.jsonl`, `run.json`, and
  `$STEMMA_CACHE`, whose entries a later run would be served, and the run folder's
  `CLAUDE.md`, `.claude/`, `.stemma/`, `task/`, `deliverable/`, `reviews/`,
  `framer/own-frame.md`, `framer/critique.md`, `tree.md`, `datalayer/` and every
  temporary file), verified, including refusal of a
  request to run unsandboxed. Write and Edit to those paths are denied by permission
  rules. The MCP server runs outside the sandbox, so **every host write is
  path-contained and follows no symlink** (each component opened without following
  links; temporary files inside a tool-only folder; atomic rename). WebFetch is denied.
- **Subagents:** forks denied (`Agent(fork)`, verified); built-in agent types
  (general-purpose, Explore, Plan and any other) denied, so only plugin roles start;
  only approved models allowed (`availableModels`: Opus 5.5, Fable 5.1); model and
  effort arguments on an Agent call stripped by a hook (verified: an effort argument
  is otherwise obeyed);
  `CLAUDE_CODE_EFFORT_LEVEL` never set in a run's environment. MCP tools the role
  needs are allowed explicitly (headless runs deny what is not allowed).
- **Only the host starts checking roles.** Run settings deny
  `Agent(stemma:<each checking role>)` and `Agent(stemma:lead)` to every session, and
  the register accepts a claim or branch verdict only from a session the driver
  itself launched for that role and that id (the driver records each such session
  id). A worker that starts a checker of its own gets no verdict recorded.
- **Identity comes from the harness, never from a tool argument.** A PreToolUse hook
  records the caller's `agent_type` keyed by the tool use id, which the MCP request
  also carries in its `_meta`; the register reads identity from that stamp, never
  from an argument the model wrote (verified feasible). Only a
  `stemma:claim-checker` session records claim verdicts; only a
  `stemma:branch-reviewer` session records branch verdicts.
- **Every session reads only its own run.** Read is allowed only inside the run
  folder and the run's plugin folder (a guard on Read, deny by default); Bash's
  sandbox denies reads of the home folder, the repository and every other run, except
  what the interpreter itself needs. So no role, checker or worker can read this
  repository (test answer keys included), the design record, another run's store, or
  anything else of the owner's. A test proves a checker cannot read each of these.
- **Blind roles stay blind:** the claim checker cannot read `branches/*/record.md`,
  `order.md`, `frame.md` or drafts; the decider's-eye reviewer reads only `task/`,
  `intake.md` and the draft under review; the blind framer cannot read `frame.md`,
  `tree.json` or `branches/` until `framer/own-frame.md` exists. Enforced on Read
  and Bash (Grep and Glob are not tools in 2.1.308) by each session's own settings,
  verified.
- **A branch reaches `answered` or `gap_accepted` only by a branch verdict saying so.**
  Producer: the branch-reviewer session the driver starts on `propose_branch_done`.
- **`gate_status(version)` reads the gate without releasing**: the full list of unmet
  conditions, one per line. The lead calls it after every review round. `finish_run`
  releases when nothing is unmet, so it is never used to probe.
- **Reviews carry structured points.** `record_review` takes the review text and a
  list of points, each `{point, material: true|false, node, criterion}` (v0.4: at least
  one of node and criterion set); the reviewer judges materiality, code only reads it.
- **`finish_run` refuses** unless:
  - `criteria.md` names at least one criterion (v0.4);
  - every leaf node (a `question`, and an `open_lane` that holds work) not set aside is
    `answered`, `gap_accepted` or `stopped_by_limit`; a parent node closes when all its
    children have closed (derived, never set); an empty open lane needs nothing; a
    set-aside node and its subtree count as closed (v0.4); every point sent back on a
    closed branch has a resolution (v0.4). A leaf the lead does not research is still closed only by a branch
    review: the lead files it as a branch whose record says what was not done and why,
    and the reviewer may accept the gap or send it back;
  - `framer/critique.md` and `framer/lead-response.md` exist;
  - the three reviews exist for the draft version being released, and none of them
    holds a material point, unless the review-round limit below has been reached;
  - review rounds: `max_review_rounds` in `run.json` is a recorded resource limit
    (default 3). After the last permitted round `submit_draft` refuses another, and the
    release lists every criticism the last round raised that the draft did not
    resolve, in the completion statement. Reaching the limit is `stopped_by_limit`,
    never a quality verdict;
  - every number segment in `deliverable/answer.md` is tagged, and every `c` tag names
    a claim that is not superseded and whose latest verdict is `holds`,
    `holds_with_correction` or `could_not_check`. A `could_not_check` figure is
    rendered in `answer.reader.md` marked "not checked", following the standing rule
    that a check not run is listed as not checked (step 0). Decided: a figure the
    checker could not check may appear only so marked, and a `not_supported` figure
    never; refusing every unchecked figure would push answers toward timidity, the
    failure the design ranks most likely.
  Producers: the framer, reviewer and checker sessions the driver starts; the lead's
  tags and response.
- **Refusals** name what failed and nothing else: no hint of a way around.

## 7. Switches (in `run.json`; design section 5)
- `s1_method`: `decomposition` | `placebo` | `none`. Design arm 1 is the method for
  the lead and the question-type cards for workers; arm 2 puts neutral text of the
  same length in place of both; arm 3 has neither. So the driver copies into the
  run's plugin folder: for `decomposition`, `decomposition-method/` and the real
  cards; for `placebo`, `placebo-method/` and the placebo cards; for `none`, no
  method and no cards. A skill not copied cannot load. Real and placebo versions
  share file names, frontmatter (`name`, `description`) and length, and differ only
  in body, so the lead selects and loads them in identical words in both arms; the
  `decomposition` and `placebo` lead fragments are identical.
- `s2_shape`: `team` | `single_loop` (one author, no workers; checks unchanged). In
  `single_loop` the run settings deny `Agent` to the lead. *v0.4: `single_loop` is
  parked and refused (section 0).*
- `s3_hypothesis_in_briefs`: true | false (*v0.4: `true` is parked and refused*); it needs workers, so with `single_loop`
  the driver records it `not_applicable` and loads no S3 fragment.
- `s4_orientation`: `frame_first` | `basic_facts` | `primary_record`.
- `s5_models`: model and effort per role (defaults, v0.4 D10: Opus 5.5 at high effort
  for every role, the lead included; never Haiku). task.json's `roles` sets the same.
- `s6_cross_family`: `off` | `during` | `final` (wired later).
- **Assembly:** the lead's text is `plugin/lead/base.md` followed by one fragment per
  switch, `plugin/lead/<switch>.<value>.md` (an empty file where a value adds
  nothing; `s5_models` is configuration and has no fragment), concatenated in the
  order listed above into the run's
  `agents/lead.md`. Worker texts follow the same pattern if a switch touches them
  (`plugin/agents/worker.base.md` plus fragments). The driver records each digest.
  No text branches on a switch inside itself.

## 8. Roles and skills (C)
- `plugin/lead/`: `base.md` and switch fragments (section 7).
- `plugin/agents/`: `worker` (also a branch lead), `blind-framer`, `claim-checker`,
  `branch-reviewer`, `deciders-eye-reviewer`, `evidence-auditor`,
  `mechanical-reader`, `retrospective-writer`. Each states its model and tools (the
  fewest it needs, full tool names) and its text: a page or less, every sentence
  followed by a comment naming the failure it prevents and its evidence. Plugin
  agents ignore `hooks`, `mcpServers` and `permissionMode` fields; do not rely on them.
- Each checking role starts from an id prompt (section 2) and finds everything else
  through tools and files it may read.
- `plugin/skills/`: `decomposition-method/`, `placebo-method/`, question-type cards
  (method only, never a lens applied to every question); later the deck skill.
- `plugin/STANDING-RULES.md`: the four standing rules of step 0.
- `docs/ROLE-MAP.md`, generated from the files: one page saying what each role is
  told. The owner reads it before the first real run.

## 9. The driver (B)
- It drives the pinned Claude Code CLI headless, not the Agent SDK: the SDK passed the
  parent's environment through to the session, which broke the sign-in (B's probe
  runs, 26 Oct). Launch flags live in one function, `stemma.driver.clean.clean_launch`,
  with `check_canary`, used by the driver and by every other runner. `--strict-mcp-config` drops the plugin's own server and
  `--disable-slash-commands` drops the method skills, so Stemma runs use neither.
- Input: `task.json` with `question`, optional `intake` answers, `attachments`,
  optional `as_of`, optional `switches`, and (v0.4) optional `scope`, `criteria`,
  `options`, `roles` and `stop_after` (section 0). Any other runner adapts its tasks
  to this shape.
- **Attachments mid-run** (design step 1: an attachment slot open at every stage; a
  client's question list arriving mid-run is mapped at once). `stemma attach <run_id>
  <file>` copies the file into `task/arrivals/<n>/` (tool-only), records an event, and
  queues a notice; the lead's next tool call receives the notice through a hook
  (`additionalContext`, *B verifies*), never through a file the lead must remember to
  check. The lead maps the arrival against its frame and tree at once and records
  what it changed in `intake.md`. A task may deliver later attachments this way. v0.4:
  `stemma attach` takes no run lock, so it works while the driver runs or waits for a
  usage reset (the notice reaches the resumed lead's next tool call); a worker captures
  the file (the lead reads no sources); a released, sealed run refuses attachments.
- It launches the lead and every checking session, verifies the resolved model and
  effort of each (for the lead's subagents, from their transcripts, which land in
  the clean config's `projects/<cwd-slug>/`; a copied config folder is not signed
  in), waits, records artifact-landed events, keeps `status.json` and
  `status.html`, and resumes a stopped run, by itself after a usage stop (section 0).
- Outcomes and exit codes: `released` 0, `ended_without_release` 1, `canary_failed` 2,
  `stopped_by_limit` 3, `lead_failed` 4, `release_withdrawn` 5, `revocation_failed` 6,
  `usage_limited` 7 (resumes left used up), `frame_complete` 8, `lead_cancelled` 9,
  `driver_stopped` 130.
- It runs the system under test only through the isolated runner configuration, and
  reads only transcripts of sessions it launched.

## 10. Open questions each builder answers in their report
- B: answered by run on 26 Oct (probe notes and kit in B's branch). Nesting: the
  lead is depth 0 and a depth-3 agent has no Agent tool, so a worker may have two
  levels below it. `alwaysLoad` loads MCP tools at start. Sandbox denials held against case variants, `..`, symlinks, renames, not-yet-existing paths and `**` globs.
- A: which archive service gives a copy before a date, and how reliably.
- C: which lines depend on the pending E6a reviewer study (reviewers stay goal-only
  until it reports).
