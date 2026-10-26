# Contracts between the parts (v0.1)

These are the interfaces the parts agree on, so they can be built in parallel. The
design (`DESIGN-v1.md`, cited as "step n") says what the system does; this file says
where each thing lives and what crosses each boundary. Internals are the builder's.
A builder who finds a contract wrong, or impossible on the pinned harness, stops and
tells the orchestrator with evidence; nobody works around a contract silently.

## 1. Parts and owners
- **A. Evidence service:** `src/stemma/evidence/`, `tests/evidence/`.
- **B. Register, guards and driver:** `src/stemma/register/`, `src/stemma/hooks/`,
  `src/stemma/driver/`, `src/stemma/analysis/`, `src/stemma/mcp_server.py`,
  `plugin/.mcp.json`, `conformance/`, `plugin/hooks/`,
  their tests.
- **C. Roles and skills:** `plugin/agents/`, `plugin/skills/`,
  `plugin/STANDING-RULES.md`, `docs/ROLE-MAP.md`.
- **Orchestrator:** this file, `AGENTS.md`, `pyproject.toml` (each part adds its
  own pinned dependencies to its own extras group only),
  `plugin/.claude-plugin/plugin.json`.
- Later: **D. Hackathon adapters** (`hackathon/`, `run.py`, `Dockerfile`).

## 2. How a run executes
1. The driver (B) creates a run folder, writes `run.json` from a `task.json`, and
   starts the **lead** as a headless Claude Code session with the run folder as its
   working directory, the plugin loaded, the pinned binary
   (`STEMMA_CLAUDE_BIN`) and the clean config (`STEMMA_CLAUDE_CONFIG`). It writes
   the run's standing rules (`plugin/STANDING-RULES.md`) as the run folder's
   `CLAUDE.md`. Nothing else loads: no user memory, skills or global instructions,
   checked by a canary at launch. The driver sets `STEMMA_RUN_DIR` for the session;
   the MCP server and the hooks find the run folder through it.
2. The lead's role text is its system prompt. Every other role is a **plugin agent
   definition** (`plugin/agents/<role>.md`) that the lead (or a branch lead) starts
   through the Agent tool. Checkers, reviewers, the blind framer and workers are
   always started **fresh, never forked** (step 0); B finds and sets whatever the
   pinned harness needs for that, and proves it.
3. Agents talk to the evidence service and the register through one MCP server,
   `stemma` (`src/stemma/mcp_server.py`), which loads A's tools and B's tools.
4. Agents exchange work through files in the run folder (section 3), never through
   retold figures: a figure crosses a hand-off as a claim id.
5. The run ends when the lead calls `finish_run` (section 5); the driver records the
   end, the time and tokens, and keeps everything.

The product's own concurrency and spending limits are recorded per run in
`run.json` (resource limits). They are unrelated to the two-helper cap on builders.

## 3. The run folder (`$STEMMA_RUNS/<run_id>/`)
Markdown is for files only agents read; JSON is for anything code reads. Files
marked *tool-only* are written by an MCP tool, never by an agent's Write or Edit
(a hook denies direct writes there).
- `run.json` (driver): run id, task, `as_of` date or null, switches (section 7),
  model and effort per role, harness version, resource limits.
- `task/` (driver): the question, the intake answers if supplied, attachments.
- `intake.md` (lead): the five intake answers and the restated job (step 1).
- `frame.md` (lead, living): the frame of step 2, including the working answer, the
  rival and the cards loaded with why. Earlier versions kept under `history/`.
- `tree.json` (*tool-only*): the issue tree (section 5). `tree.md` is a rendering.
- `framer/` (blind framer): `own-frame.md`, then `critique.md`; the lead's answers
  in `framer/lead-response.md`.
- `branches/<branch_id>/`:
  - `order.md` (lead or branch lead): the work order, with exactly these headings:
    Intent; Place in the tree; What would change the decision; End product; Out of
    scope; Known facts (each a claim id, or marked unverified); Where to look; What
    to return.
  - `restatement.md` (worker): the intent and plan in its own words, written first.
  - `record.md` (worker): the branch record, with exactly these headings: Conclusion
    and confidence; Claims (ids); Model; Rival tested; Methods tried and what each
    returned; Gaps (typed); Implications for other branches; What was not checked.
  - `verdicts/<n>.json` (*tool-only*): branch reviewer verdicts.
  - `sub/` for a branch lead's own branches, same shape.
- `store/` (A, *tool-only*): the evidence store (section 4).
- `register/` (B, *tool-only*): the claim register (section 5).
- `analysis/` (workers, lead): recorded calculation scripts and their outputs.
- `draft/` (lead): `answer.md` and earlier versions.
- `reviews/`: `deciders-eye.md`, `evidence-audit.md`, `mechanical.md`, per version.
- `deliverable/`: `answer.md` (the final slide-by-slide text, tagged as section 5
  says), `answer.reader.md` (tags rendered as references); a deck later.
- `events.jsonl` (driver and hooks, *tool-only*): every launch and the model and
  effort it resolved to, tool denials, artifact-landed events, compactions.
- `status.json`, `status.html` (driver): what is running, what landed, what waits
  on the user.
- `retro.md` (retrospective writer).

## 4. The evidence service (A)
Discovery uses Claude Code's own WebSearch. **WebFetch is denied** (step 6). Every
source an agent reasons about comes through these tools. Names are the MCP tool
names; inputs and outputs are JSON.
- `capture(url, reason)` → a receipt: `receipt_id`, `sha256` of the exact bytes,
  `status`, HTTP facts, `content_type`, `text_path` (relative to the run folder),
  `text_chars`, `outline` (headings or pages with character offsets), `warnings`,
  `served_from_cache`. Every attempt writes a receipt, failures included. Stored
  text is the whole page or document converted by B1's picks (markdownify over the
  whole HTML minus scripts and styles; pypdfium2 for PDFs); nothing in between
  decides what counts as content. Empty or implausibly short text is a loud
  `status`, never a silent success; Playwright is tried only then. When `run.json`
  has an `as_of` date, every capture comes from an archived copy dated before it,
  or fails with `no_snapshot_before_as_of`.
- `capture_file(path, reason)` → a receipt, for the user's attachments.
- `sec_filings(company, form=None, filed_after=None, filed_before=None)` → a list
  of filings with accession numbers and filing dates; `filed_before` defaults to
  `as_of`. `sec_capture(accession, document=None)` → a receipt.
  `sec_facts(company, concept, filed_before=None)` → as-filed XBRL facts, each with
  its filing date and the receipt of the filing it came from (B1: the default views
  of every tool collapse to the latest figure; as-of work must not).
- `outline(sha256)`, `read(sha256, start, length)`, `read_section(sha256, anchor)`:
  reading in parts; no tool returns a whole long document at once.
- `search_store(query, mode="words"|"exact", sha256=None, limit)` → hits with
  `sha256`, offsets and a short snippet (SQLite FTS5: porter for words, trigram for
  exact strings such as figures).
- `check_quote(sha256, quote)` → found or not, with offsets. Exact match after a
  normalisation of whitespace and quotation marks that the tool documents.
- `tables(sha256)` → Docling's table view, a derived view never used for quoting
  (optional extra; returns `not_installed` when absent).
- Store layout: `store/bytes/<sha256>`, `store/text/<sha256>.txt`,
  `store/receipts/<receipt_id>.json`, `store/index.sqlite`. Bytes are written once
  and never changed. Each receipt records the extractor and its version.
- **One store per run; no store shared across runs.** An expensive fetch may be
  cached in `$STEMMA_CACHE`, keyed by the exact request (URL, `as_of`, extractor
  versions), and a run gets cached bytes only when it makes that same request
  itself; its receipt says `served_from_cache` and the original fetch time.

## 5. The claim register and the tree (B)
**Claim.** Fields the agent supplies: `statement` (the sentence as it will be used),
`value` (optional: number, unit, period, and `denominator` for any ratio or share),
`tier`, `support` (a list of `{sha256, quote}`), `derivation` (optional:
`{script, inputs: [claim ids]}`), `branch_id`, `entities` (names). Fields the code
writes: `claim_id` (content-derived), `author` (the role, from the harness),
`created_at`, the result of each code check, `supersedes`.
- **Tiers** (one vocabulary for all roles): `observed` (in a captured source, by its
  author's own measurement); `interested_party` (stated by a party with a stake,
  tiered as theirs); `secondary` (a source reporting someone else's figure);
  `estimated` (computed by us from claims, with a derivation); `assumed` (an input we
  chose, with the reason in the statement). The model's own knowledge is never a
  claim.
- **Code checks at filing** (exact facts only, step 8): each quotation exists in the
  stored text; the figure appears in its quotation; a ratio has a denominator; a
  derivation recomputes to the stated value. A failure refuses the claim and says
  which check failed.
- **Verdicts** (claim checker only): `holds`, `holds_with_correction` (with the
  corrected claim filed as a new claim that supersedes the old), `not_supported`,
  `could_not_check`, each with its evidence.
- **Corrections** never delete. A superseded claim is closed and points to its
  successor; the dependency index lists every claim, analysis script, branch record
  and draft section that used it, so the owner of each can re-derive it.
- **Entities** are resolved with World Model's near-duplicate review queue; mapping a
  name to a parent needs two sources.
- **Echo candidates:** claims citing the same document are listed for the lead to
  judge; code never decides independence.
- **Meterless World Model.** The register is World Model's data model: content-derived
  ids, the append-only log, superseded links closed and never deleted, idempotent
  re-ingest, the review queue. It must pass World Model's own conformance runner,
  unmodified, against our implementation; any bridge between the runner and our
  code holds no logic. That passing run is the hackathon's Engine test.
- **Storage:** `register/log.jsonl` (append-only, under an exclusive file lock: more
  than one process may write), and a derived SQLite index rebuilt from the log.
- **Tree** (`tree.json`): nodes with `id`, `parent`, `kind` (option, condition,
  rival, open-lane), `statement`, `doubt`, `settles_by`, `flip_point`, `crux`
  (true for the one to three the decision turns on), `branch_id`, `status` (`open`,
  `in_progress`, `proposed_done`, `answered`, `gap_accepted`, `stopped_by_limit`).
  Every change goes through a tool and leaves a history entry.
- **Tools** (names may be refined by B, then recorded here): `file_claim`,
  `record_claim_verdict`, `supersede_claim`, `cite` (records a use: claim id → file
  and section), `get_claim`, `find_claims`, `echo_candidates`, `entity_review`,
  `tree_upsert`, `propose_branch_done`, `record_branch_verdict`, `finish_run`.
- **Tags in drafts and the deliverable.** Each figure is followed by `[[c:<first 12
  hex of claim_id>]]`. A number that is not a figure (a date used as a date, a label)
  is followed by `[[nc:<why>]]`. Code requires every number to carry one or the
  other; the mechanical reader and the evidence auditor judge the `nc` tags.

## 6. What code guarantees, and who produces what it reads
Each guard below must have a producer in the live path, or it blocks every run.
- WebFetch is denied. (No producer needed.)
- A tree node becomes `answered` or `gap_accepted` only with a branch reviewer
  verdict saying so. Producer: the branch reviewer calls `record_branch_verdict`.
- `finish_run` refuses unless every number in `deliverable/answer.md` is tagged and
  every `c` tag names a claim whose latest verdict is `holds` or
  `holds_with_correction` (the release gate, step 12). Producers: the claim checker's
  verdicts; the lead's tags.
- Tool-only files cannot be written directly.
- Only the claim checker records claim verdicts; only the branch reviewer records
  branch verdicts. **The role comes from the harness, never from a tool argument.**
  Restrict each role's tools in its agent definition, and stamp the true role on
  every register write from what the harness reports (B verifies what the pinned
  version's hook input carries, for example the subagent's type, and how a hook can
  overwrite a tool argument with it).
- Refusals name what failed and nothing else: no hint of a way around.

## 7. Switches (in `run.json`, read by the driver; step and section 5 of the design)
- `s1_method`: `decomposition` | `placebo` | `none`. C writes the short decomposition
  method and a neutral text of the same length as two skills; the driver loads one
  or neither.
- `s2_shape`: `team` | `single_loop` (one agent, checkers still separate).
- `s3_hypothesis_in_briefs`: true | false (when false, work orders name the condition
  and what the evidence must show, with no expected finding).
- `s4_orientation`: `frame_first` | `basic_facts` | `primary_record`.
- `s5_models`: model and effort per role (defaults: Opus 5.5; highest effort for the
  lead and final reviewers, high for the rest; never Haiku).
- `s6_cross_family`: `off` | `during` | `final` (a frontier OpenAI model as a
  flag-only evidence auditor; wired later).
Every switch value is recorded on the run. Role texts are written so each switch is
one file chosen or not, never an instruction that branches inside a text.

## 8. Roles and skills (C)
- `plugin/agents/`: `blind-framer.md`, `worker.md` (also a branch lead),
  `claim-checker.md`, `branch-reviewer.md`, `deciders-eye-reviewer.md`,
  `evidence-auditor.md`, `mechanical-reader.md`, `retrospective-writer.md`; the lead
  is `plugin/agents/lead.md`, loaded by the driver as the main session's prompt.
  Each states its model, its tools (least needed) and its text: a page or less, every
  sentence tagged in a comment with the failure it prevents and its evidence.
- `plugin/skills/`: `decomposition-method/`, `placebo-method/`, question-type cards
  (method only, never a lens applied to every question), and later the deck skill.
- `plugin/STANDING-RULES.md`: the four standing rules of step 0.
- `docs/ROLE-MAP.md`, generated from the files: one page saying what each role is
  told. The owner reads it before the first real run.

## 9. The driver (B)
- Input: `task.json` with `question`, optional `intake` answers, `attachments`,
  optional `as_of`, optional `switches`. The sealed side adapts its task package to
  this; the driver never reads benchmark material itself.
- It launches, verifies the resolved model and effort of every launch it makes (and
  records what hooks report for subagents), waits, records artifact-landed events,
  keeps `status.json` and `status.html`, and resumes a stopped run.
- It runs the system under test only through the isolated runner configuration.

## 10. Open questions each builder answers in their report
- B: how the pinned harness starts subagents fresh rather than forked; whether a
  subagent can start its own (nesting depth); how effort is set per subagent; what
  hook input carries about the calling subagent; the plugin's MCP tool name prefix.
- A: which archive service gives a copy before a date, and how reliably.
- C: what in the role texts depends on an answer still pending from the E6a reviewer
  study (reviewers stay goal-only until it reports).
