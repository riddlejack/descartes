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
  sentence of every real skill. `tests/roles/`: 37 tests.

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
3. **Page length.** The lint takes a page as 800 words of instruction, excluding
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

## Lines that depend on the pending E6a reviewer study (contract 10)

Marked `E6a-provisional` in their comments and listed in `docs/ROLE-MAP.md`:
- blind-framer: the four named points of its goal;
- branch-reviewer: reading frame.md and tree.json (an input beyond the goal that also
  shows it the working answer), and its four questions;
- claim-checker: the five questions;
- deciders-eye-reviewer: the named failures in its goal, and the field expert's two
  questions;
- evidence-auditor: the focus on figures made during synthesis;
- mechanical-reader: its list of mechanical faults.
Each reviewer's goal sentence, its own inputs and its line against needless hedging
are not provisional.

## For part B

- The driver's lead assembly is `rolemap.assemble_lead(choice)`; B may import it or
  reimplement it, in the contract's order (s1, s2, s3, s4, s6).
