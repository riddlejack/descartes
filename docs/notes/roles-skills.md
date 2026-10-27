# Part C notes: the role texts and skills

Builder C, against `docs/CONTRACTS.md` v0.2.7. Detail behind the report; the owner's
page is `<a private page>`.

## What is in the branch

- `plugin/STANDING-RULES.md`: the four standing rules of design step 0, which the
  driver writes as the run folder's `CLAUDE.md`.
- `plugin/lead/base.md` and one fragment per switch value (`s1_method`, `s2_shape`,
  `s3_hypothesis_in_briefs`, `s4_orientation`, `s6_cross_family`); `s5_models` has no
  fragment and the three `s6` files are empty (contract 7). The decomposition and
  placebo S1 fragments are byte-identical.
- `plugin/agents/`: worker, blind-framer, claim-checker, branch-reviewer,
  deciders-eye-reviewer, evidence-auditor, mechanical-reader, retrospective-writer.
- `plugin/skills/`: `decomposition-method/` and `placebo-method/` (both `name: method`),
  four cards (`card-company-evaluation`, `card-market-sizing`, `card-decision`,
  `card-mechanism`), each with a `placebo-` twin carrying identical frontmatter.
- `tools/roles/rolemap.py`: the lint, the lead assembly, the tool-prefix rewrite
  (`retool`), the shared-block copy (`sync`), the switch for deferred tools
  (`undefer`, already run), and the generator of `docs/ROLE-MAP.md`.
- `tools/roles/shared/branch-work.md`: the worker's branch method, copied by `sync`
  into the worker and the single-loop lead fragment, so both arms of S2 carry the same
  method. `tools/roles/skill-evidence.md`: the failure and evidence behind every
  sentence of every real skill. `tests/roles/`: 44 tests.

## How it was shown to work, and what is not shown

- Shown: `python tools/roles/rolemap.py check` passes; `pytest tests/roles` passes
  (37), with a hostile case for each guard (a line with no comment, a comment with no
  evidence, two sentences behind one comment, an unknown tool, a wrong prefix,
  WebFetch, Grep, a small model, low effort, an extra role file, a text over a page,
  an assembled lead over a page, a missing fragment, frontmatter in a fragment, S1
  fragments that differ, a fifth standing rule, a skill sentence with no evidence, a
  comment inside a skill, a placebo with different frontmatter, a placebo that uses a
  method word, a placebo of the wrong length, a missing placebo, a missing ToolSearch
  line, a needless ToolSearch line, a shared block edited in one role, an unknown
  switch value, S3 paired wrongly with the single loop, a malformed prefix); `ruff` and `mypy --strict` are clean on the tool;
  `claude plugin validate` (2.1.296, the only CLI on this machine) passes on the agent
  files and on an assembled lead.
- Not shown: no role has run. Whether each text produces the behaviour it names is the
  test loop's question. Unverified on the pinned 2.1.308: `Agent(stemma:worker)` as the
  lead's spawn allowlist under `--agent` (the documented syntax for main-thread agents,
  with the plugin-scoped name as my reading); that two skills sharing `name: method` never both load
  (the driver copies one).

## Decisions and interpretations (each open to correction)

1. **Comments reach the model.** A subagent file's body is its system prompt, so the
   per-sentence comments are part of what each role reads (about 13,000 of 33,000
   characters across the lead base and the agents). I kept them: a stated reason tends
   to help a model apply an instruction sensibly, and the failure names are plain
   English. If a test shows they cost quality, the driver can strip `<!-- -->` at
   assembly and record both digests.
2. **Skills carry no comments.** A placebo twin must match its real skill's length and
   frontmatter (contract 7), which comments would break; the evidence lives in
   `tools/roles/skill-evidence.md`, and the lint fails when a skill sentence has no entry.
3. **Page length.** The lint takes a page as 850 words (800 until E6a; briefly 900 after the autopsy; see the consolidation below) of instruction, excluding
   frontmatter and comments. Every agent fits a strict page (the worker is longest at
   about 550 words); the lead's base is about 600 and the longest team lead is 798. The
   single-loop lead may add the shared branch method (1,171 words at most), because it
   does the worker's job too. The owner may want the lead shorter.
4. **The order headings live in the lead's base** (both S2 arms write orders); the
   record headings live in the shared branch method.
5. **The ghost storyline sits in the S3 hypothesis fragment** (design step 2: "it
   anchors, so it sits inside switch S3"). In the blind arm the lead writes none.
6. **No worker fragments.** Every `.md` in a plugin's `agents/` loads as an agent, so
   there is no `worker.base.md`; the single text the worker and the single-loop lead
   share is copied in by `sync` and checked by the lint instead.
7. **The decision log is a section of frame.md**: the design names a decision log
   (step 11) and the contract's run folder has no file for it.
8. **Intake never blocks.** No contract tool asks the user, so the lead writes a marked
   assumption; the status page could surface those lines.
9. **Authors wait for their own branch verdict** with `await_checks` and act on
   find_more, analyse or bound themselves; when a worker has ended, the team lead starts
   a new one with the order, record and verdict. `await_checks` accepts branch ids
   and draft versions (contract v0.2.4).
10. **The blind framer's first pass reads task/ only**, since the driver starts it at
    run start, before intake.md exists. It has Skill so that it sees the card list.
11. **P3's rubric is in no role** (orchestrator, 26 October). The decider's-eye reviewer
    carries the design's own words, marked E6a-provisional.
12. **Effort**: max for the lead and the three final reviewers, high for the rest. The
    lead's frontmatter model and effort are informational: the driver sets them on the
    command line (contract 2).
13. **Timidity.** Each judging role carries one line aimed at overshoot, the failure the
    design ranks most likely, and the blind review removed four more (below).
14. **Tools.** Grep and Glob are gone (not tools in 2.1.308). Read-only checkers have
    no Bash: they know their paths from their id and the contract. The retrospective
    writer has Bash to list the run folder. Stemma tools load at start
    (`"alwaysLoad": true` in B's `plugin/.mcp.json`, contract v0.2.3), so roles call them
    directly and list no ToolSearch; the lint rejects both. If a later harness defers
    them again, set `MCP_TOOLS_DEFERRED = True` in the lint, which then requires one
    identical ToolSearch line (`DEFERRED_TOOLS_LINE`) in every role with stemma tools.

## The placebo, and how it was made neutral

- **Same invocation.** Real and placebo skills share names and descriptions, the card
  descriptions name only the question type (orchestrator, 26 October), and the two S1
  lead fragments are identical, so the lead selects and loads them in the same words.
- **Same length and shape.** Each placebo body is within 5% of its twin's characters and
  keeps its layout (title, the same number of sections, numbered paragraphs for the
  method).
- **Inert content.** The placebos describe the history and making of the violin family
  and rosin: descriptive prose, no imperatives, no vocabulary of reasoning, research,
  business or checking, and facts that are general and widely documented. The lint
  rejects twenty method words; the reviewer judged the bodies inert. The topic was
  chosen because nothing in it maps onto framing, evidence or argument: a chronological
  history, not a breakdown of parts, so it does not model decomposition by example.
- **Known limits.** (a) A model told to "use what it teaches" may notice the placebo is
  off topic; that is the control's point, but the arm is not blind to the model.
  (b) Decomposition against placebo is clean, but placebo against none compares more
  than "reading a document first": the shared S1 fragment tells the lead to load
  question-type cards and record which, and the card descriptions name question types.
  (c) The lead's base already carries much of the method by contract (options including
  the status quo, a falsifiable working answer, a rival, doubt and flip points, the
  crux, the open lane, no dropping), and the worker always carries the mechanism steps;
  so S1 tests the method's remainder: choosing the top cut, division principles,
  ranking, grouping leaves, the self-check.

## The blind review (one fresh helper, given the design and these files only)

Applied:
- the claim checker no longer kills sound sample statistics: a share holds when it
  names the universe measured or sampled, and is corrected when stated more widely;
- the evidence auditor's goal covers statements of fact and figures, not the draft's
  own inferences shown with their basis;
- the worker carries a source's caveat into the record and goes beyond it only with
  independent evidence, instead of never concluding more strongly than the caveat;
- a could_not_check claim may stay, listed as not checked (contract 6), instead of
  being settled away;
- the single-loop lead now carries the worker's whole branch method (the shared block),
  so S2 compares shapes rather than texts;
- re-dispatch after a send-back when the worker has ended; re-deriving uses of a
  superseded claim (lead and branch method); new attachments and client question lists
  mapped at once; questions only the user can answer logged with the assumption used;
- the branch reviewer's gap rule has the cost clause its own stopping test has;
- the company card states inferences as findings with basis and confidence;
- domain knowledge cut from the cards (the utility example, "diagnosis", the pricing
  formula; the mechanism card keeps one price example, the owner's named case);
- the team fragment's needless "never start a checking role" clause is gone;
- the framer's card point applies only if cards are installed; frame-first no longer
  conflicts with capturing attachments; the blind order on the crux gets its own node
  and carries nothing from the ghost storyline.

Recorded at first, then decided by the orchestrator in contract v0.2.4 and applied:
- **A branch waiting on the user.** The lead calls `proceed_on_assumption` when no
  answer comes (always so headless) and lists each question with its assumption in the
  deliverable; the branch reviewer may then close the branch as accept_gap with gap
  type user_held; the team lead restarts a worker on it with the assumption. A risk
  the user must accept is named in the lead's section on how the work was checked.
- **Checking roles started by an author.** Run settings deny `Agent(stemma:<each
  checking role>)` and `Agent(stemma:lead)`, and the register accepts verdicts only
  from sessions the driver launched for that role and id.
- **S3 needs workers.** With `single_loop` the driver records S3 `not_applicable` and
  loads no S3 fragment; `assemble_lead` refuses any other pairing.
- **Single-loop runs deny `Agent`** to the lead in settings.
- **`await_checks`** accepts claim ids, branch ids and draft versions.
- **Late documents and documents behind a re-cut** are accepted as covered by the
  per-claim checks for now.

## Contract v0.2.7 (part B's merge-gate fixes)

- The blind framer records its frame and critique with `record_frame` (kind own or
  critique); `framer/` files are tool-only, so it has no Write.
- The three final reviewers read the frozen draft, `register/drafts/answer.v<n>.md`,
  and record with `record_review`; `reviews/` is tool-only, so they have no Write.
- The lead calls `finish_run(version)`, which copies the reviewed draft into
  `deliverable/`; no role writes there.
- The claim checker passes a correction inside `record_claim_verdict`; it must not
  already be in the register (the register refuses it, and no verdict would land), so
  the checker records not_supported and names the existing claim instead. The
  checker no longer lists `file_claim`.
- The lead and the shared branch method find a superseded claim's uses with
  `claim_dependents`.
- `query_data` is in the lead's and worker's tools with no line of its own: a figure
  from a row still has to be filed with a quotation found in its receipt, and
  derivations read only claims, so code already stops an unfiled row figure.

## Contract v0.2.9 (attachments mid-run)

- One lead line, replacing the earlier "capture each new file in task/": when the hook
  announces an arrival, the lead captures it, maps it at once against the frame and
  tree, records in intake.md what it changed, maps any client question list to
  answered, out of scope or unaddressed, and re-cuts or adds orders where the
  decision's needs change (design step 1, lessons 2.0a and 2.8). No worker or checker
  line: arrivals reach workers only through the orders the lead amends. A few words
  elsewhere in the lead were trimmed to keep the longest team lead within its page.

## From the walking-skeleton run (26 October)

- A worker tried four times to read `src/stemma/analysis/__init__.py` because its text
  said "compute through stemma.analysis" without the call. The shared branch method
  and the lead now give the call shape: `from stemma.analysis import inputs, result`,
  `v = inputs("<12-hex claim id>", ...)`, `result(number)`, run with
  `"$STEMMA_PYTHON"` (python3 on PATH is unreadable under the run's read sandbox), and
  derivation `{"script": "analysis/<file>.py", "inputs": [ids]}` on an estimated claim.
  The longest team lead is 799 words after small trims.
- A worker rendered PDF pages to PNG in /tmp and the read guard refused them. The
  shared branch method now says to Read the stored PDF (`store/bytes/<sha256>`) with
  the pages parameter, and to put any rendered image in analysis/. No lead line sends
  the lead to page images, so the lead's own text is unchanged.
- Scripts that opened `store/text/<sha>.txt` directly had about a dozen estimated
  claims refused on the confined re-run. The branch method and the lead now read a
  captured dataset with `document(receipt_id)`, name it in the derivation's
  "documents" list, may keep helper `.py` files beside the script, and never open
  `store/` paths (contract v0.2.10). Scratch scripts and files go in analysis/, never
  /tmp, after a worker's write there was refused.
- A claim checker found an estimate's low end wrong, could not file a corrected
  estimate (a correction to an estimated claim needs a derivation that reproduces it,
  and the checker has no Write or Bash), and recorded holds while naming the error
  (claim 71728d0fc75a). The checker now records not_supported for an estimate it finds
  wrong, naming the error and the value it found, and never holds after finding an
  error; part B adds a register guard to the same effect.
- **The run never released** (`docs/notes/walking-skeleton.md` on branch
  claude/walking-skeleton; contract v0.3). The lead was told to call finish_run only
  when a round changed nothing material; every round changed something, so it wrote 11
  drafts in 10 rounds (1,865 to 4,765 words) and never read the gate. Now it calls
  finish_run after every review round, fixes exactly what the refusal lists, revises
  only what a reviewer marked material (the three final reviewers now mark each point
  material or minor), grows the text only with a stated reason, and at the run's
  max_review_rounds lists unresolved criticisms in the completion statement. Seven
  nodes (five options, the open lane, one condition) had no branch and could never
  close: every condition and rival now gets a branch, or one whose record says what was
  not done and why; options close through their conditions; an empty open lane needs
  nothing. Draft v11 had 77 untagged number segments, many of them labels and dates:
  every number on the face now carries a c tag or an nc tag with its reason.
- **Codex review and contract v0.3.1.** Calling finish_run to read the gate would have
  released a draft whose reviews raised material points, so the lead now reads
  `gate_status(version)` after every round, revises for every point marked material
  and resubmits, and calls finish_run only when nothing is unmet. The final reviewers
  record `record_review(text, points)` with each point's materiality, material only
  if it would change the answer, a figure or the decider's action. The worker's gap
  line now carries the reviewer's cost clause (it invited endless search), and the
  lead computes only figures no source states (it invited sourced facts turned into
  estimates). The lint now also checks every snake_case name and every path in the
  prose against the contract, refuses a tool the role does not hold, a path outside
  the run folder or unknown to the contract, and two sentences sharing one comment;
  `/tmp` no longer appears in any text. 44 tests.
- **Autopsy of the first live run** (`build-2026-10/autopsy/walking-skeleton.md`). The
  lead's context grew to 400,000 to 980,000 tokens and re-reading it cost about $109:
  the lead now works from its files and the register and does not re-open what a claim
  or branch record holds, and (with workers) sends new research and long reading,
  including what a review asks for, to workers by work order. All 11 "re-run does not
  reproduce" refusals were rounding: authors file the exact number the script reports
  and round inside it with `result(round(x, n))`. The mechanical reader, 95% thinking
  at max effort and never changing substance, runs at high; the judgment reviewers
  stay at max (E6a). The lead's own effort is left to switch S5. To make room, the
  frame line and its reasons line were merged and the arrival line shortened; the
  longest team lead is still about 900 words, and the lint's page rose from 850 to
  900. The lead is now well over a strict page: a decision for the owner.
- **Consolidation (27 October): the lead back under a page.** Each fix had added words
  (the longest team lead reached 897). Lines that prevent overlapping failures were
  merged; each merged line's comment carries every failure and every piece of
  evidence of the lines it absorbed, so no failure tag was lost. The longest team lead
  is now 842 words and the lint's page is back to 850. No other role changed length.
  What moved, and where each failure now lives (all in `plugin/lead/`):
  - intake: "write intake.md" and "fill any gap with a marked assumption" are one line
    in base.md (failures: wrong question, wrong size, a blocked run, intake capping
    research);
  - the tree: "build the tree" and "give every condition and rival a branch" are one
    line in base.md (topic trees, no home for unplanned reading, dropped knock-out
    branches, nodes nothing can close). "An empty open lane needs nothing" was cut:
    the gate no longer lists an empty lane (contract v0.3), so code prevents that
    failure;
  - reading: "read each branch record in full" and "work from your files and the
    register" are one line in base.md (connections never made, a frame never revised,
    a lead context of 400,000 to 980,000 tokens);
  - checks: "a failed check is a reason to find a better figure" and "when a claim you
    used is superseded" are one line in base.md (checks shrinking the answer, a
    corrected figure left standing elsewhere);
  - arrivals, waiting on the user, the derivation call and the release gate are each
    one line as before, shortened without dropping a failure;
  - team: "send new research to workers" and "keep working while workers run" are one
    line in `s2_shape.team.md` (a lead researching inside its swollen context, an idle
    lead);
  - headings shortened: Framing, Branches, Drafting, Orders, Orientation.

## The E6a reviewer study (26 October), and what it settles here

E6a gave 36 fresh reviewers only the question and one of two answers. They reliably
caught flaws inside an answer's frame (a weak figure, a missing size, a claim beyond
its evidence) and almost never frame-level flaws (why this metric, why this
structure, invented specifics, no analogue or bounding), whatever they were given.
Generic standards (ICD 203) did no better than no criteria; medium effort did worst;
Fable missed flaws Opus caught. Its limits: two answers to one question, reviewers
could not open sources, no author history, model-judged matching.

Applied:
- **The lead's frame states its reasons** for the metric, the options, the conditions,
  how each unknown is bounded, the analogues, and any specific such as a timeline,
  because frame quality has to be built upstream.
- **The blind framer** builds its own frame first (as before), now with its reasons for
  the metric, the cut and the bounding, and compares those reasons with the lead's.
- **Reviewers run on Opus at max effort**: the three final reviewers already did; the
  branch reviewer is raised from high. The claim checker stays at high: E6a tested
  reviewers of finished answers, not claim checking, and the design sets checkers at
  high.
- **No generic standards list** is in any role (none was; confirmed).

Markers, line by line:
- decider's-eye reviewer, its named failures and the field expert's two questions:
  left `E6a-provisional`, as the orchestrator asked, pending one fact about P3's
  wording.
- blind framer's critique points: provisional, not settled; E6a tested no blind-first
  reviewer (the design addendum proposes that component test).
- branch reviewer reading frame.md and tree.json: provisional, not settled; E6a's
  reviewers had no frame or author history.
- branch reviewer's four questions: provisional, not settled; E6a tested reviews of
  finished answers, not of branches.
- claim checker's five questions and the evidence auditor's focus: provisional, not
  settled; E6a's reviewers could not open sources.
- mechanical reader's list: provisional, not settled; E6a found generic lists no
  better than none, and this list is specific to lesson 7's faults, which it did not
  test.

The page limit in the lint rose from 800 to 850 words: the analysis call (from the
live run) and the frame's reasons (from E6a) pushed the longest team lead to 807.

## For part B

- The driver's lead assembly is `rolemap.assemble_lead(choice)`; B may import it or
  reimplement it, in the contract's order (s1, s2, s3, s4, s6).

## Version 2: the ten decisions of 27 October (builder W1)

The texts now follow D1 to D10 of `V2-CHANGES-2026-10-27.md`; each changed line's
comment cites the decision and the run 1 diagnosis section behind it. Lines whose
failure no longer exists came out: "make it a choice", options and their conditions
as the tree, the rival node, "options close through their conditions", "never
dropping or thinning", and phrase about a past error (its failure,
a striking claim on a sampled universe, is kept).

### What is shown
- `rolemap.py check` passes; `map --check` passes; `tests/roles` 48 pass; the whole
  suite 1,170 pass, 18 skipped (live tests excluded), after the blind-review fixes.
- The longest team lead is 850 words against the 850-word page (s1 decomposition,
  team, s3 true, s4 basic_facts). Nothing more fits without a cut.
- New hostile tests: the lead listing a source-reading tool (an MCP reader, or
  WebSearch) fails the lint; a pending contract name passes until the contract names
  it, then the lint says to drop it; an unagreed path still fails beside the pending
  ones.

### Interpretations (open to correction)
- **Pending contract names.** The prose lint checks names against `CONTRACTS.md`,
  which part B is changing in parallel. `PENDING_CONTRACT_NAMES` (`question_only`,
  `broader_decision`) and `PENDING_CONTRACT_PATHS` (`criteria.md`, `checks/`) let
  the agreed names through; once the contract carries one, `check` reports it so the
  entry is deleted. On merge with B's contract, expect those reports and delete the
  entries.
- **The lead's responses to review points** go in `draft/responses.v<n>.md` (lead
  writes; `reviews/` is tool-only). Final reviewers read `reviews/v<n-1>/` and
  `draft/responses.v<n-1>.md` from the second version on. Asked the orchestrator to
  confirm and to have B open those reads (and `criteria.md`) to all three reviewers,
  the decider's-eye reviewer included.
- **Revision orders.** The lead fills Finding and Raised by; the worker fills Is it
  right? (first, with evidence) and Fix; the lead decides disputes in its responses
  file. In single_loop the lead does both halves.
- **Echo pairs** go to the branch's worker to trace, since the lead no longer reads
  sources.
- **The lead still computes** figures that only combine filed claims (a gap, a sum),
  in a recorded script; modelling goes to a worker. It keeps file_claim, Bash and the
  claim look-ups for that.
- **D9 in the branch reviewer:** a material point is sent back once; if still open,
  the reviewer records `bound` naming the bound or condition the record must state,
  then closes the branch once it does.
- **Points need a node or criterion.** The decider's-eye reviewer names the
  criterion (it does not see the tree); the auditor names the node of the claim's
  branch, else a criterion; the mechanical reader names a criterion, else the node of
  the section.
- **Orientation (S4)** arms that read before framing now create the open_lane node
  and have its branch do the reading, since the lead cannot.

### Rulings from the orchestrator (27 October)
- `draft/responses.v<n>.md` is agreed; B opens `criteria.md`, `reviews/v<n-1>/` and
  the responses file to all three final reviewers.
- S2 `single_loop` and S3 `true` are parked: the fragments stay as they are, the map
  marks them, and the driver refuses both values. S3 `false` is the v2 default.
- The order heading is now "What would change the answer" (B renames it in the
  contract).
- The pending vocabulary list stays until B's contract v0.4 carries the names.
- Pre-public audit: the private paths in comments and in the evidence file's header
  are replaced by "a private design record"; the map's "lesson" label is neutral.
- Comments now say "decision D1" for the owner's decisions, so they cannot be read
  as the research file "D1" ("D1 principle 12").

### The blind review of the v2 texts (one fresh helper, given V2-CHANGES and the files)
It returned 23 findings. Fixed:
- Workers were told to wait on claim checks before proposing done, but under
  decision D7 checks run only on claims a submitted draft cites, so no branch could
  ever finish. Workers now propose done without waiting, and a failed check comes
  back as a revision order; the branch reviewer reads any verdicts already recorded.
- The broader_decision scope was overruled by the tree line, the framer and the
  decider's-eye reviewer; each now honours it.
- A worker who shows a finding wrong now writes "none, disputed" under Fix, and the
  lead rules in its responses file.
- The branch reviewer's bound loop now ends: send back once, then bound once, then
  answered or accept_gap with the point named open.
- Workers take what they need from the order, never frame.md, so S3's blind arm
  stays blind.
- Method: "before any remedy" cut; the "proposed change" fit limited to questions
  whether a named change works; the open lane tied to the question.
- Decision card takes the goal from the question and task/; market-sizing card
  starts from the user's own term; the decider's-eye reviewer's expert questions are
  about the question asked; the framer checks lead-written criteria for added
  thresholds.
Not changed, with reasons:
- single_loop's missing source tools and "order modelling" in that arm: parked.
- Estimated claims with no document (the lead's combined figures) belong to no
  doc:<receipt_id> checker. The host decides grouping; asked B, through the
  orchestrator, to assign each cited estimate to the session of a document its
  inputs rest on, or to state another route.
- D2 and D3 live mainly in the method skill: the base keeps their binding parts
  (question type, options only if named, rival and separating sub-question, terms
  defined, tree of sub-questions only); moving the rest would shrink what switch S1
  tests, and the lead is at its page limit.
- Company card's reading list: the card loads only for a company question and its
  lenses are already "where the question needs it".
- "Client question list" is in contract section 9; the decider's-eye comment's
  E6a caveat is evidence, not a missing failure; the lead keeps Read and Bash (B's
  read hook refuses stored sources) and claim look-ups (needed for tags); the framer
  keeps Skill to read cards.

### Open, needing B
- Estimated claims with no document need a checker route (above).
- D10: frontmatter is Opus 5.5 at high for every role; the driver's defaults
  (`MAX_EFFORT_ROLES` in `src/stemma/driver/task.py`) still set max for the lead and
  final reviewers and are B's to change.

### Aligned to contract v0.4 (part B, commit 188bae3)
- This branch is rebased onto 188bae3 so the lint reads v0.4. The pending list then
  reported all four entries as now in the contract, and they were deleted; only
  `run_analysis` remains pending (part B is adding it), and `check` will report it
  when the contract names it.
- The lead loses Bash as well as the source tools (the lint now refuses either);
  its arithmetic script runs through `run_analysis`, and it files the exact result.
- `criteria.md` lines are `- C1: <text>`. The lead answers each final-review point
  by id in `draft/response.v<v>.md` before submitting version v (this replaces the
  `draft/responses.v<n>.md` name agreed earlier); reviewers read
  `register/drafts/previous.v<version>.md`. Points are `{point, material, node,
  criterion}`.
- Branch reviewer: one send-back per branch; the closing verdict gives every
  sent-back point a resolution (resolved, bounded, conditional, disputed) and
  records a new material point as an open limit. The worker writes the revision
  order after a send-back, naming each point id, and names a bounded or conditional
  point's id in its record.
- Claim checker takes `doc:<receipt_id>` or, for a cited claim with no document,
  `claim:<claim_id>`; the earlier gap on estimates is closed by v0.4.
- The host lists points open at `max_review_rounds` in the completion statement, so
  the lead's text no longer does.
- v0.4 still has the order heading "What would change the decision"; the texts use
  the approved "What would change the answer".
- Shown: lint and map check pass; tests/roles 49 pass; whole suite 1,261 pass, 18
  skipped.

### Codex review of 4d83095 (one fix round, 27 October)
Nine findings; the orchestrator's instructions per item:
1. Integration (host on old interfaces, map wording on effort and parked switches):
   true once part B is merged; nothing changed here.
2. Document-free estimates: the claim checker now takes `derivations:v<n>` (part B
   is adding it; `checks/derivations-v<n>.json`) as well as `doc:` and `claim:`, and
   judges for each estimate whether its script computes what the claim states from
   the right input claims.
3. Lint bypass: `WebSearch(*)` and `WebSearch(domain:...)` passed the lead check.
   Tool specs are now parsed (`TOOL_SPEC`); only `Agent` may carry an argument, any
   other syntax is refused, and the lead check judges the reduced name. Hostile
   tests: five parameterised or malformed forbidden specs on the lead, five
   unsupported specs on a worker.
4. Company card: discovery limited to what the scoped question needs; every
   document relied on is still read whole. Orientation fragments read only what
   bears on the question.
5. Framer: criteria are checked against the question and the user's answers in
   task/ (user thresholds allowed); nodes are checked against the broader decision
   when the scope is broader_decision.
6. Lead: one line for decision D3 (both cheap readings), and options at framing only
   as the user named them, after the research only with broader_decision scope or a
   "what to do" question. To fit, parked arms are exempt from the page check (they
   cannot run; a test shows the check returns once an arm is un-parked) and a few
   lines were trimmed. The longest runnable lead is 850/850.
7. Branch reviewer: before the closing review, the old accept-gap rule; at it, the
   send-back limit decides, a point still open closes bounded or conditional with
   its effect stated, never called evidence exhausted.
8. Standing rule 1: roles that read sources capture before they cite; a role that
   reads none (the lead) cites only filed claims.
9. Mechanical reader: the shortest change that makes a segment correct or clear (a
   unit, a year, a definition), and no growth beyond it.
Also: the worker may call `run_analysis` (orchestrator); its Bash is unchanged.
Shown: lint and map check pass; tests/roles 60 pass; whole suite 1,272 pass, 18
skipped.
