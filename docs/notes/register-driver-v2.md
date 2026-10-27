# Register, gate, driver and contract, version 2 (builder W2)

This round implements the owner's decisions after run 1 (D1 to D10) in the register,
the release gate, the driver, the hooks and the contract, which is now v0.4. Section 0
of `docs/CONTRACTS.md` lists every change with its decision; this note says how each
was shown to work, what was not shown, and what the other builders must change.

## What works, and how it was shown

Every item below has unit tests, and each guard has a hostile test that tries to get
round it. The full suite (`pytest tests hackathon/tests`), ruff and `mypy --strict` on
`src/` pass. No live run was made in this round.

- **Frame-only stop.** `stop_after: "frame"` in task.json, or `stemma start
  --frame-only`. The driver denies the lead the Agent tool, queues a notice for its
  first tool call, and ends the run with outcome `frame_complete` (exit code 8) once the
  frame is done and has settled. Shown with the fake harness: a lead that would have
  slept 30 seconds is stopped within the settle time, and a lead that stops before
  answering the critique ends `ended_without_release` with what was missing recorded
  (`tests/driver/test_driver_v04.py`).
- **task.json fields** (scope, criteria, options, roles, stop_after), every role at Opus
  5.5 high by default, and `criteria.md` written by the driver and protected from every
  agent when the task gives criteria (Write and Edit guards, permission rules and the
  Bash sandbox's write denials).
- **Tree** (D2, D5): only `question` and `open_lane` nodes; set-aside nodes, lead only,
  with a reason, closing their subtree, listed by `gate_status`, shown in `tree.md`, and
  written to `deliverable/record.json` at release.
- **Review points** (D4): every point names an existing node or criterion; ids;
  `gate_status` groups open material points by criterion and by node.
- **A branch is sent back once** (D9), with resolutions on the closing verdict, record or
  revision-order checks for bounded, conditional and disputed points, and revision
  orders required before re-proposal. Hostile tests send a branch back a second time
  with each send-back verdict, invent point ids, and close without resolutions.
- **The lead reads no sources** (D6): the hook refuses the lead's own thread the twelve
  source tools, WebSearch, WebFetch and Bash, and Read of `store/`, `task/attachments/`
  and `task/arrivals/`, including case variants, absolute paths and symlinks; a worker
  under the lead keeps them all.
- **Claim checks per document** (D7): filing starts nothing; `submit_draft` lists cited
  claims by document, or in one derivations list per draft (see the second round
  below); a checker judges only its own round's list. A barrier-synchronised race test runs a submission against
  two document checkers recording verdicts at the same moment.
- **Reviewer memory** (D8): a branch's re-review resumes its previous reviewer session
  (shown in the argv: `--resume=<id>`); a resumed session counts as unverified until the
  driver verifies the new launch. Final reviewers of a later version read
  `register/drafts/previous.v<n>.md`: the last round's points and the lead's response,
  which must name every material point.
- **Automatic resume after a usage stop**: `stemma start` and `stemma resume` wait until
  the reset time the harness reported (epoch seconds in `rate_limit_info.resetsAt`, seen
  142 times in run 1's streams) plus a two-minute margin, then resume, at most six times;
  a reset more than six hours off (a weekly limit) or Ctrl-C ends the run as before. All
  four numbers are resource limits recorded in run.json.

## Run 1 replayed under v0.4

`tests/e2e/replay_run1_v04.py` reads a copy of run 1's folder; nothing reruns.

- **Tree.** v0.4's `tree_upsert` refuses 22 of the 23 nodes by kind: five options, one
  rival and sixteen conditions (the open lane fits). Rewritten as sub-questions with the
  options dropped, every node closes, but six roots remain: the question asked (T)
  beside the option subtrees (K1, K2, L), the gate (T0) and the open lane, and the blind
  projection (TB) still sits under T. This is a negative result for code: it cannot tell
  a gate, a projection or a branch about an unasked option from a sub-question. Only the
  lead's text and the blind framer can, which is why the frame-only stop exists.
- **Claim checks.** Run 1 filed 455 claims and started 407 checks, 278 of them on claims
  no draft ever cited (the diagnosis counted 260 against the final drafts; this count is
  against every submitted draft). Under v0.4 the eleven drafts would have needed 81
  checker sessions, assuming each round's verdicts landed before the next draft. The
  first draft alone cited 42 estimates with no document of their own; grouping each by
  the document its inputs rest on is what brings version 1 from 57 sessions to 25.
- **Send-backs.** Run 1's branch reviewers sent back once in all (b2, find_more), so the
  send-back rule would have changed nothing there.
- **Final reviews.** Run 1's reviews carried no structured points (recorded before
  v0.3.1), so the stopping rule cannot be replayed on them.

## Second round: the orchestrator's checklist and Codex review 1

After the first report the orchestrator sent a checklist, and Codex reviewed
84d68f7..5992bf8 (ten defects, eight serious). All are fixed on this branch, each with
tests, most with a hostile or crash test:

- **Derivation integrity.** A re-run copies only the script and the modules it imports
  (found by parsing), so run 1's 223 scripts no longer refuse every estimate; the bundle
  is stored under its hash at filing, and every later re-run uses that copy, so a
  script edited after filing changes nothing filed.
- **run_analysis**, for the lead's synthesis arithmetic now that it has no shell.
- **Claim-check routes.** A claim goes to its document's checker, or (an estimate on no
  document, an assumption) to one derivations checker per draft, which is given the
  sentences that cite it. Each assignment is written once per round, and a checker
  asks for its own with `check_assignment`.
- **Criteria** freeze when the first review is asked for; reviews record the digest
  they judged; only the lead's `revise_criteria` changes them afterwards, and that
  voids the reviews made before.
- **Tree authority.** Only the lead moves nodes or reassigns branches; send-back
  obligations follow the node.
- **Verdict writes** survive a crash between them; the next writer repairs the nodes.
- **Previous-round memory** waits for the previous round to finish and needs a response
  to every point.
- **The lead's reads** are an allowlist of approved artifacts.
- **The automatic resume** keeps the run locked through its waits, rechecks the run
  before relaunching, and never restarts a lead stopped by its time limit.
- **The sealed release**: `release/manifest.json`, checked by `manifest.check`.
- Parked switch values (single_loop, hypothesis in briefs) are refused.

Changed names W1 depends on: per-round assignment files, `check_assignment`,
`revise_criteria`, `run_analysis`, a response to every previous point, and lead-only
structural tree edits.

## What is not shown

- No live run with the real harness: `--resume` of a checking session, the frame-only
  notice reaching the lead through `additionalContext`, and the lead running without
  Bash are shown only against the fake harness.
- The six-hour cap on a usage wait and the two-minute margin are guesses at sensible
  resource limits, not measured.
- "Sent back once" is read per branch, not per point. A per-point reading would let a
  reviewer raise a new point each round, which is how run 1 never finished.

## What the other builders must change

- **W1 (role texts).** Drop from the lead's tools: capture, capture_file, sec_filings,
  sec_capture, sec_facts, outline, read, read_section, search_store, tables,
  check_quote, query_data, WebSearch and Bash (the canary compares the agent's tools,
  and the hook refuses them anyway). The lead writes `criteria.md` as `- C1: ...` lines
  when the task gave none, and `draft/response.v<n>.md` naming each material point id
  before submitting a later version. Revision orders use the four headings and name the
  point ids. Branch reviewers pass `points` and `resolutions`. Final reviewers give
  `node` or `criterion` with every point and read `register/drafts/previous.v<n>.md`. The
  claim checker handles `doc:<receipt_id>` by reading `checks/doc-<receipt_id>.json`.
  The deliverable should list set-aside nodes and open points; `deliverable/record.json`
  holds them.
- **W3 (onboarding, viewer, verify).** task.json takes `scope`, `criteria`, `options`,
  `roles` and `stop_after`. The viewer can read `deliverable/record.json`, the
  `set_aside` field and `closes_by` values `set_aside` and `ancestor_set_aside` in
  `tree.json`, outcome `frame_complete` (exit 8), and the status state
  `waiting_for_usage_reset` with `resume_at`.

## N1, the second model family: what run 1 showed (27 Oct)

Run live on run 1, with the strict parser and Nemotron Ultra pinned by name, N1 asked
the 49 cited claims the driver itself would ask: 40 agreed with Claude and 9 disagreed
(Claude holds, Nemotron does not support). That cost 251,000 Nemotron tokens, about
18 s per answer, and 505 s of wall time at two in flight. The third look then put each
of the 9 to a fresh Claude claim checker through the driver's own launch path
(`tests/e2e/third_look.py`). All 9 recorded holds and none changed a verdict. That cost
9 Opus sessions, 113 s, about $2.72. The two models did not see the same evidence: the
checker reads the whole stored document, while Nemotron sees each quotation with 600
characters of context. Every figure Nemotron called missing lay outside its window and
inside the stored source.
- (a) Claude wrong: none.
- (b) The claim is right but its cited span is too narrow: 1e303d628b04, 36ae5c7f938a,
  5d140c4f1fc5, 85787f92ad95, a9652819d902 and c33701ec451f, each supported elsewhere
  in the documents it cites; and 05375029d056, which leans on two other registered
  claims (a414f6a87516, 22cd53b36d42) that its own support does not cite.
- (c) Nemotron wrong because N1 asked the wrong question: 02dddcd40aa2 and
  19a446a5cf30, both estimates, whose value is their derivation's. Estimated claims
  are therefore no longer N1-eligible; their observed inputs are asked on their own.

The orchestrator's recommendation, recorded here for the owner and the next
orchestrator, who decide: N1 as built does not catch Claude errors (none on run 1), but
it does measure something the reader experiences. That is whether the cited span
alone shows what the claim says, which is exactly what a reader sees when the report
viewer opens a citation. The honest feature is therefore a citation-precision check:
Nemotron sees only what the reader will see, and its finding goes back to the claim's
author as "widen or fix the cited span", not to a full third look. The routing change
is not built.

The final review round (Codex re-check of b62b609, five findings, each now with a test
that failed before its fix). The model's answer is read only in one form: after the
last `</think>`, or the whole reply when there is none, exactly one JSON object, alone
or in one code fence; words before it or a second object are refused, and so is an
answer whose completion status is anything but "stop", including a missing one. The
prompt now says so. One consequence to watch in a live run: a model that writes plain
reasoning before its object without closing it by `</think>` is now refused, falls to
the fallback model, and then is this claim's recorded failure (loud, never a verdict).
The replay's raw answers were not stored, so whether every run-1 answer had this form
is not known; under the replay's parser, any answer that closed its reasoning already
had to be the object alone. A malformed response from the runtime now keeps its receipt, so
its tokens are counted. The replay script cleans the model's reason before writing it,
and the third-look script reports a third look only when the recheck session filed it,
"missing" otherwise.
