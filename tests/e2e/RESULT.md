# End-to-end launch with placeholder roles (26 October 2026)

**Result: passed.** One run of the driver with the pinned Claude Code 2.1.308 and the
clean config, placeholder role texts (`placeholder_plugin/`), the real
`plugin/hooks/` and `plugin/.mcp.json`, and part A's real evidence service: the script
puts one invented note under the run's `task/`, and the placeholder lead captures it with
`capture_file` before filing its claim on that receipt (earlier runs, before part A was
merged, used a stub that is now removed).
Recorded run: `20261026-065636-1`… (see `recorded/run_e2e.out`), on the merged code with part A's
evidence service; before it, `20261026-055319-ce9326`, on the code after the cross-family review fixes
(1a2dce1; driver exit 0 now means the register recorded the release), about one minute, driver exit 0; its files are in `recorded/`. Earlier runs
`20261026-050744-7917ca`, `20261026-044400-e99681` and `20261026-043549-60adb0`, on older code, also passed.

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
5. The framer and the three reviewers recorded their work through `record_frame` and
   `record_review` (tool-written files). The claim checker recorded `holds`; the second
   `finish_run(1)` was accepted, copied the frozen reviewed draft to
   `deliverable/answer.md` byte for byte, and rendered the tag as a numbered reference
   in `deliverable/answer.reader.md`.
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

## Every session reads only its own run (CONTRACTS v0.2.8)

`run_read_scope.py` (live, same environment) had the driver launch a hostile placeholder
claim checker, with its real settings and guards, that tried the Read tool and Bash on
four places: a decoy answer key in the repository, the design record in
`~/.private/stemma-design/`, another run's `store/`, and a decoy file in the home folder.
All eight attempts were refused ("outside the run folder" from the Read guard;
"Operation not permitted" from the sandbox), and none of the decoys' unique markers
appeared anywhere in the session's stream (`recorded/run_read_scope.out`). The full
placeholder run (`20261026-060000-fecf06`) still released under the same read scope.
After the last fix round (deny reads from the root, verified sessions only), both were
run again: the hostile checker again failed all eight reads with no marker leaked, and
the placeholder run `20261026-064412-4fb980`, recorded in `recorded/`, released.
