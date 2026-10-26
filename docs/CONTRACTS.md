# Contracts between the parts (v0.2.8)

These are the interfaces the parts agree on, so they can be built in parallel. The
design (`DESIGN-v1.md`, cited as "step n") says what the system does; this file says
where each thing lives and what crosses each boundary. Internals are the builder's.
A builder who finds a contract wrong, or impossible on the pinned harness, stops and
tells the orchestrator with evidence; nobody works around a contract silently.

v0.2 applies a blind pressure test of v0.1 (26 Oct). Items marked *(B verifies)* are
facts about the pinned harness that a reviewer checked in the docs but not by a run;
part B proves each with a tiny run before anything depends on it.

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
   - `file_claim` → a claim checker, prompt `claim:<claim_id>`;
   - `propose_branch_done` → a branch reviewer, prompt `branch:<branch_id>`;
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
- `frame.md` (lead, living): the frame of step 2, including the working answer, the
  rival, and the cards loaded with why. Earlier versions under `history/`.
- `tree.json` (*tool-only*): the issue tree (section 5). `tree.md` is a rendering.
- `framer/own-frame.md`, `framer/critique.md` (blind framer);
  `framer/lead-response.md` (lead).
- `branches/<branch_id>/`:
  - `order.md` (lead or branch lead), with exactly these headings: Intent; Place in
    the tree; What would change the decision; End product; Out of scope; Known facts
    (each a claim id, or marked unverified); Where to look; What to return.
  - `restatement.md` (worker): intent and plan in its own words, written first.
  - `record.md` (worker), with exactly these headings: Conclusion and confidence;
    Claims (ids); Model; Rival tested; Methods tried and what each returned; Gaps
    (typed); Implications for other branches; What was not checked.
  - `verdicts/<n>.json` (*tool-only*): branch reviewer verdicts.
  - `sub/<branch_id>/` for a branch lead's own branches, same shape.
- `store/` (*tool-only*, A): the evidence store (section 4).
- `register/` (*tool-only*, B): the claim register (section 5).
- `analysis/` (workers, lead): recorded calculation scripts (section 5) and outputs.
- `draft/answer.v<n>.md` (lead); `submit_draft` freezes a version.
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
  their values) and reports with `stemma.analysis.result(number)`. `file_claim`
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
- **Tree** (`tree.json`): nodes with `id`, `parent`, `kind` (`option`, `condition`,
  `rival`, `open_lane`), `statement`, `doubt`, `settles_by`, `flip_point`, `crux`
  (true for the one to three the decision turns on), `branch_id`, `status`. Every
  change goes through a tool and leaves a history entry.
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
  `proceed_on_assumption`, `get_tree`, `claim_dependents`, `finish_run(version)`
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
- **`finish_run` refuses** unless:
  - every tree node is `answered`, `gap_accepted` or `stopped_by_limit`;
  - `framer/critique.md` and `framer/lead-response.md` exist;
  - the three reviews exist for the draft version being released;
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
  `single_loop` the run settings deny `Agent` to the lead.
- `s3_hypothesis_in_briefs`: true | false; it needs workers, so with `single_loop`
  the driver records it `not_applicable` and loads no S3 fragment.
- `s4_orientation`: `frame_first` | `basic_facts` | `primary_record`.
- `s5_models`: model and effort per role (defaults: Opus 5.5; highest effort for the
  lead and the final reviewers, high for the rest; never Haiku).
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
  with `check_canary`, used by the driver and by every other runner (baselines,
  sealed runs). `--strict-mcp-config` drops the plugin's own server and
  `--disable-slash-commands` drops the method skills, so Stemma runs use neither.
- Input: `task.json` with `question`, optional `intake` answers, `attachments`,
  optional `as_of`, optional `switches`. The sealed side adapts its task package to
  this; the driver never reads benchmark material itself.
- It launches the lead and every checking session, verifies the resolved model and
  effort of each (for the lead's subagents, from their transcripts, which land in
  the clean config's `projects/<cwd-slug>/`; a copied config folder is not signed
  in), waits, records artifact-landed events, keeps `status.json` and
  `status.html`, and resumes a stopped run.
- It runs the system under test only through the isolated runner configuration, and
  reads only transcripts of sessions it launched.

## 10. Open questions each builder answers in their report
- B: answered by run on 26 Oct (probe notes and kit in B's branch). Nesting: the
  lead is depth 0 and a depth-3 agent has no Agent tool, so a worker may have two
  levels below it. `alwaysLoad` loads MCP tools at start. Sandbox denials held against case variants, `..`, symlinks, renames, not-yet-existing paths and `**` globs.
- A: which archive service gives a copy before a date, and how reliably.
- C: which lines depend on the pending E6a reviewer study (reviewers stay goal-only
  until it reports).
