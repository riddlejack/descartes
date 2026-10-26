# End-to-end launch with placeholder roles (26 October 2026)

**Result: passed.** One run of the driver with the pinned Claude Code 2.1.308 and the
clean config, placeholder role texts (`placeholder_plugin/`), the real
`plugin/hooks/` and `plugin/.mcp.json`, and one invented capture
(`r-invented-1`, written by the script into the run's store; the evidence service was
the register's stub, `STEMMA_EVIDENCE_STUB=1`, because part A is not in this branch).
Recorded run: `20261026-044400-e99681` on the final code (commit after 5616b79), about
one minute, driver exit 0; its files are in `recorded/`. An earlier run on older code
(`20261026-043549-60adb0`) also passed.

What it shows, in order (from `recorded/events.jsonl`):
1. The driver launched the lead (`--agent stemma:lead`) and, at run start, the blind
   framer (`frame:own`). Both canaries passed.
2. The lead filed one claim on the invented capture; the register asked for a check and
   the driver launched a claim-checker session with the prompt `claim:<id>`.
3. The lead called `finish_run` and was refused by the release gate, naming every unmet
   condition, including `claim 197b37675a64 has verdict none`.
4. The host then started, each from an id-only prompt: the branch reviewer
   (`branch:b1`), the framer again (`frame:critique`), the decider's-eye reviewer, the
   evidence auditor and the mechanical reader (each `draft:1`, three at once), and,
   after `finish_run` was accepted, the retrospective writer (`retro:run`). Eight
   checking sessions in all, none started by an author.
5. The claim checker recorded `holds`; the second `finish_run` was accepted and
   `deliverable/answer.reader.md` rendered the tag as a numbered reference.
6. Every launch's resolved model and effort matched `run.json`
   (`resolved_model_effort ... match: true`, claude-opus-5-5 at the effort the task set).

**A negative result on the way.** Run `20261026-044202-f9f745` did not release: the
placeholder checker called `await_checks(["frame:critique"])` before the critique was
requested, `await_checks` returned at once with `not_requested`, and after two such
answers the checker stopped without a verdict. The gate correctly refused both
`finish_run` calls. `await_checks` now waits for a check not yet requested, up to its
limit (test `test_await_checks_waits_for_a_check_not_yet_requested`), and the next run
released.

Rerun: see the docstring of `run_e2e.py`. It needs the pinned binary and the signed-in
clean config, so it is not part of the unit tests.

Not shown here: a subagent (no worker was started by the placeholder lead; the driver's
own real launch showed a `stemma:worker` subagent's model and effort verified), resume
after a real stop, and part A's real evidence service.
