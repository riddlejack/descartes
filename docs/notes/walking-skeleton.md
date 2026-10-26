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

(added after the run)
