# Walking-skeleton run 2: pre-registration

Written and committed before launch (branch `claude/token-accounting`, off main b5ab12c,
with token accounting d0b65ec and 1784cc7). Results are added below the line, after the
run, without editing what is above it.

## Hypothesis

With the v0.3 fixes, the run releases a deliverable through `finish_run`, with every
figure on its face carrying an allowed verdict, within the lead's time limit. Spelled
out:
1. `finish_run` is accepted within 4 hours of lead running time, counted across any
   resumes.
2. In the released `deliverable/answer.md`, every `c` tag names a claim that is not
   superseded and whose latest verdict is `holds`, `holds_with_correction` or
   `could_not_check`. Every segment holding a number carries a tag. Both are checked
   again after the run, independently of the gate.
3. As in run 1: every session runs on the model and effort in `run.json`; no checking
   role runs as a subagent; no guard blocks a legitimate step.

A release after the third review round, with the last round's points listed in
`deliverable/completion.md` (contract v0.3), counts as a release. It is recorded as
`stopped_by_limit` for review rounds.

## The run

- Question: "Is the United States on track to reach 500,000 public EV charging ports by
  2030?" No `as_of`.
- Intake, as in run 1: purpose "a briefing for a policy analyst"; who "a policy
  analyst"; use "decide whether the target needs new measures"; detail "a short memo";
  data held "none".
- Default switches: s1 `decomposition`, s2 `team`, s3 `false` (run 1 had `true`), s4
  `frame_first`, s6 `off`, s5 the defaults (Opus 5.5; `max` effort for the lead and the
  three final reviewers, `high` for the rest).
- Resource limits, recorded in `run.json` as resource limits, never a quality bar:
  - at most 3 checking sessions at once;
  - a checking session stopped after 40 minutes;
  - a lead launch stopped after 4 hours;
  - at most 3 launches per check request;
  - `max_review_rounds` 3.
- Harness: Claude Code 2.1.308, the clean config (a signed-in Claude Code configuration), the real
  evidence service, the merged plugin with part C's v0.3.1 texts, and part B's register,
  guards and driver.

## What would refute it

1. No accepted `finish_run` within 4 hours of lead time; or the run ends any other way
   (`stopped_by_limit` on the lead, `ended_without_release`, a crash, a hang).
2. In the released deliverable, checked independently: a `c` tag whose claim is
   superseded or whose latest verdict is not one of the three; or an untagged number
   segment.
3. A `resolved_model_effort` event with `match: false`, or a check revoked; a checking
   role seen as a subagent.
4. A refusal of a step the role legitimately needed. Each refusal is classified by
   reading the call.

## What will be recorded

The same as run 1, plus the token accounting:
- wall time by phase;
- sessions launched by role;
- claims by tier, and verdicts;
- claims on the deliverable's face;
- branch verdicts;
- drafts and review rounds, and whether the round limit was reached;
- the material points of each round;
- refusals by class, each classified;
- fixes made on this branch, and role-text failures sent to the orchestrator;
- from `usage.jsonl` and `usage.md`: input, output, cache-write and cache-read tokens by
  role, by phase and for the whole run; the lead's own share and its subagents' share;
  the harness's cost estimate (an estimate at list prices, not a bill).

---

## Results

(added after the run)
