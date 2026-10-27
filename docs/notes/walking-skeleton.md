# Walking-skeleton run: pre-registration

Written and committed before the first launch (branch `claude/walking-skeleton`, off
main 0f65706). Results are added below the line, after the run, without editing what is
above it.

## Hypothesis

The real lead, roles, evidence service, register and guards complete a run end to end on
a small public question:
1. every session launches with the model and effort in `run.json`;
2. the host starts every checking session (no checking role is ever started as a
   subagent);
3. `finish_run` releases a deliverable in which every figure carries a `holds`,
   `holds_with_correction` or `could_not_check` verdict;
4. no guard blocks a legitimate step.

## The run

- Question: "Is the United States on track to reach 500,000 public EV charging ports by
  2030?"
- No `as_of`. Default switches except s3: s1 `decomposition`, s2 `team`, s3 `true` (as the
  orchestrator asked), s4 `frame_first`, s6 `off`; s5 the defaults (Opus 5.5; `max` effort
  for the lead and the three final reviewers, `high` for the rest).
- Intake answers: purpose "a briefing for a policy analyst"; who "a policy analyst"; use
  "decide whether the target needs new measures"; detail "a short memo"; data held "none".
- Resource limits (recorded in `run.json` as resource limits, never a quality bar): at
  most 3 checking sessions at once; a checking session stopped after 40 minutes; the
  lead stopped after 4 hours; at most 3 launches per check request on a model mismatch.
- Harness: Claude Code 2.1.308, the clean config, the real evidence service (part A),
  the merged plugin (part C's texts), part B's register, guards and driver.

## What would refute it

1. A `resolved_model_effort` event with `match: false` for any session, or a check
   revoked for a model or effort mismatch.
2. A checking role seen as a subagent (`subagent_seen` with a checking `agent_type`), or
   a checking session with no host check request behind it.
3. No accepted `finish_run` within the limits; or, in the released deliverable, a `c` tag
   whose claim's latest verdict is not one of the three (checked again after the run,
   independently of the gate), or an untagged figure.
4. A refusal (`denied` event or refused tool call) of a step the role legitimately needed
   for its job. Each refusal is classified by reading the call: legitimate step blocked,
   or a step the rules exist to stop.

A crash, a hang or a loop that the run cannot leave counts as a failure of the
hypothesis, recorded with where it happened.

## What will be recorded

- Wall-clock time of the run and of each session; turns per session.
- Cost (`total_cost_usd` from each session's result, which for the lead includes its
  workers) and tokens where the stream reports them, per role.
- Every session launched: role, prompt, model and effort resolved, outcome; subagents
  seen with type and depth.
- Every refusal: tool, refusal text, who, and the classification above.
- Checks requested, launched, ended, verified, revoked; claims filed and their verdicts.
- The deliverable's path and the gate's view of it.
- Every fix made on this branch to make the run work, with its test; and every role-text
  failure sent to the orchestrator, with the line.

---

## Results

(added after the run, 26 October 2026)

**The hypothesis is refuted.** No `finish_run` was ever called, so nothing was released.
The lead reached its 4-hour limit at 15:02 UTC, and the run was then stopped on the
orchestrator's word. Points 1 and 2 held. Point 3 failed. Point 4 failed early: three faults in part B's code blocked legitimate
steps (29 refusals), all fixed during the run (listed below).
The run also straddled the account's five-hour usage stop and was resumed twice.

The run folder is kept as a recording of a real run that did not release, for the
sample pack: `<runs>/stemma2-walking-skeleton/20261026-070412-2b3c98`, outside the
repository (a
byte-identical copy of the scratch run folder; 11,624 files). Its last draft is
`register/drafts/answer.v11.md`; there is no `deliverable/answer.md`.

### Why it did not release

1. **The lead never asked the gate.** `plugin/lead/base.md` line 71 says to call
   `finish_run` "when a round changes nothing material". Every round changed something
   (each review raised points), so the lead wrote 11 drafts, from 1,865 to 4,765 words,
   and never called `finish_run`. It never saw the gate's list of what was missing.
2. **Seven nodes could never close.** The gate needs every tree node `answered`,
   `gap_accepted` or `stopped_by_limit`, and only a branch review closes a node. The five
   options (O1 to O5), the open lane (OL) and one condition (T3) were never given a
   branch, and nothing in the role text says they must be.

At the stop the gate listed 87 unmet conditions: those 7 open nodes, the three reviews of
v11 (cut off by the stop), and 77 segments of v11 with an untagged number. Many of those
numbers are ones the contract's splitter counts but a reader would not call figures:
"Level 2", "section 1", a heading "## 1.", the date in the byline, and label cells in a
table whose figure cell is tagged. Whether that is right is a contract question.

### Points 1 and 2: held

- 667 sessions launched. 594 passed the start-up canary. 661 were verified at their end
  against `run.json`. There were 480 resolved model and effort pairs, none mismatched;
  none were revoked; none went unverified.
- Subagents seen: 21, all `stemma:worker`. No checking role ran as a subagent; every
  checking session had a host check request behind it.

### Claims and checks

- 455 claims: 407 filed by authors and 48 corrections filed by checkers. 407 were live at
  the stop.
- By tier, all 455: 181 estimated, 177 observed, 40 secondary, 32 assumed, 25 interested
  party.
- Checker verdicts on the 407 filed claims: 321 holds, 48 holds with correction, 17 not
  supported, 21 could not check. The 48 corrected claims carry the `holds` recorded when
  they were filed. Could-not-check falls mostly on assumed claims (15 of 21).
- One verdict on record is wrong. At 12:40 to 12:41 a checker found claim 71728d0fc75a
  wrong in a detail (low end about 87,900, not 88,800). The register refused its
  correction twice: it had no derivation, then a derivation that did not reproduce the
  number. The checker then recorded `holds` on the claim. The orchestrator decided on a
  role-text fix (part C) and a register guard (c01c27c, below).
- Claim-checker sessions: 610 launched for 407 check requests. 139 failed at the usage
  stop, and 73 were stopped by the canary on the first resume.
- On the face of the latest draft (v11): 116 distinct claims are tagged, all `holds` and
  none superseded. There are 2 `nc` tags.

### Tree, branches and reviews

- 23 nodes; 16 `answered`, 7 `open` (above).
- 9 branch proposals across 5 branches; 7 branch verdicts: 6 `answered`, 1 `find_more`
  (b2, round 1). One branch review was refused because a later round had replaced its
  round. It was relaunched by the first resume's bug, now fixed.
- 11 drafts submitted (08:01, then from 10:45 to 14:52 about every 20 to 40 minutes).
  There were 10 complete rounds of the three final reviews (30 reviews recorded); the
  reviews of v11 were cut off by the stop.

### Time and cost

- Wall time 07:04 to 15:04 UTC, 8 hours, in these phases:
  - intake, frame and critique: 07:04 to 07:15;
  - branch work by 5 workers, and draft v1: 07:15 to 08:21;
  - the usage stop: 08:25 to 10:40, 2 hours 15 minutes, nothing running;
  - the first resume: 10:41 to 11:02;
  - the second resume, the draft and review loop from v3 to v11: 11:02 to 15:02;
  - the stop: 15:04.
- Cost, from `total_cost_usd` in each session's result. This is the harness's own price
  estimate. It is a lower bound: the lead session killed at 11:02 reported none.
  - Lead, including its workers: $240.74 (47%).
  - Claim checkers: $141.39.
  - Evidence auditors: $62.91.
  - Mechanical readers: $34.47.
  - Decider's-eye reviewers: $27.20.
  - Branch reviewers: $8.81.
  - Blind framer: $0.65.
  - Total: $516.16.
- Session hours by role: claim checkers 7.2, lead 5.8, mechanical readers 3.2,
  decider's-eye 2.4, evidence auditors 2.4, branch reviewers 0.3, framer 0.05.
- Tokens were not recorded: the driver keeps only the result's cost and turns. That is a
  gap against what was pre-registered.

### Refusals: 59, each classified

Legitimate steps blocked (three faults, every one part B's, fixed):
- 4 Reads of the `stemma.analysis` source by a worker (07:22). Fixed in 536dcd2.
- 1 `as_written` not found because the quotation crossed a PDF line break (07:38). Fixed
  in 5b5a564.
- 25 "the derivation's re-run reported no result", 24 of them before 07:50: re-runs did
  not copy a script's helper module or give it the documents it read. Fixed in c8c8988.
  These count as one blocked step repeated. The one later instance, at 10:43, was right:
  the checker passed the wrong input list, then retried and recorded.

Steps the rules exist to stop (right):
- 11 "re-run does not reproduce the claim's number". Two of these were the 12:40 to 12:41
  correction attempts described above, right by the rule but leading to a wrong verdict.
  The others were authors whose stated number differed from their own script's output.
  Judged from the call; not every one re-derived by hand.
- 5 `as_written` empty; 2 derivations without script or inputs; 1 "an estimated claim
  needs a derivation" (12:40).
- 3 quotations not in the receipt (paraphrases); 1 citation of a receipt with no text;
  1 mistyped receipt id.
- 1 Read of a page image and 1 Write in /tmp, both outside the run folder. Right by the
  rule; the role texts should say where scratch files and page images go (reported).
- 1 branch verdict from a session started for an earlier round.

### The resume after the usage stop

It worked after one fix.
- At 08:25 the account's five-hour limit refused every session. About 90 checks failed
  fast and the lead failed, so the driver ended `lead_failed`. Fix 2999c28: stop launching
  at the limit, end `usage_limited`, and on resume relaunch checks that ended without
  doing their job.
- First resume at 10:41: the lead resumed in its own conversation with `--resume`.
  But the driver relaunched all 138 retryable checks at once. While so many sessions
  were starting, the Stemma tool server was still starting when each session's start-up
  check ran, so 73 sessions were stopped. A stopped check was not relaunched. Fix
  7ba2715: relaunches wait their turn within the 3-session limit and in launch order; a
  check whose round was replaced is not relaunched; a check stopped by the canary
  launches again, up to 3 attempts.
- Second resume at 11:02, after a clean stop (exit 130): the lead resumed again. Checks
  went 3 at a time; every relaunched session passed the canary. The replaced round-2
  branch review was skipped. The other 72 stopped checks all completed.

### Fixes on this branch during the run, each with a test

- 536dcd2: the Read guard allows the `stemma.analysis` source and nothing else outside the
  run.
- 5b5a564: the `as_written` test uses the quotation check's own normalisation.
- c8c8988: derivations may read named documents and import helper modules beside them.
- 2920874: reviews start before a backlog of claim checks (192 queued checks had blocked
  the draft reviews).
- 2999c28: the driver stops at the account's usage limit, and resume reruns checks that
  never ran.
- 7ba2715: resume relaunches within the concurrency limit, skips replaced rounds, and
  retries canary stops.
- c01c27c: a session whose correction of a claim was refused may not then record `holds`
  on it. Effective from the next run.

### Role-text and other parts' failures sent to the orchestrator

- `plugin/agents/worker.md` line 56 sends workers to read the analysis API, which was
  outside the read scope (now allowed).
- Workers rendered page images and wrote scratch files to /tmp; the role texts should
  name `analysis/` or reading the stored PDF by pages.
- Part A's tokeniser reads "2025.18" (a footnote marker after a year) as one number, so
  the end of that sentence cannot be quoted.
- `plugin/agents/claim-checker.md` line 34 gives no route for correcting an estimated
  claim; the checker cannot write scripts.
- `plugin/lead/base.md` line 71 and the closing of option and open-lane nodes: the two
  causes above.

### Replay of the recorded tree under contracts v0.3 (added after the run)

The run's final `tree.json` was read by the v0.3 closure rule (`stemma.register.store
.closure`); nothing was rerun. 20 of the 23 nodes would now close:
- the 16 leaves a branch answered stay closed by their branch verdicts;
- T3, a condition stored `open` with no branch, has three children (T3a, T3b, T3c), all
  answered, so it closes by derivation;
- T, L (conditions with children), O1 and O2 (options with children) close by
  derivation;
- the open lane OL holds no work (no branch, no children), so it needs nothing.

O3, O4 and O5 are options with no children, and would still read open. The
orchestrator ruled that a parent closes only with at least one child, all closed. A
childless option is an unfinished frame, and the gate says so: "node O3 is an option
with no conditions under it; add what would have to be true, or file it as a branch".
Beyond those three, the gate would have refused only on the untagged-number segments of
v11 and on its reviews, which the stop cut off.
