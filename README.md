<!--
DRAFT for the pre-public pass. Every [[...]] is a placeholder for a result from the demo
run or a decision still open; search for "[[" before publishing and remove this comment.
Claim nothing here that the repository does not show.
-->

# Stemma 2

Stemma answers one hard business question the way a strong consulting team would. It
splits the question into an issue tree, researches each branch on the open web, has a
second agent check every figure against the exact passage it came from, and puts the
answer first. Every figure in the answer opens the stored source behind it, and you can
re-check the whole run on your own machine, offline, with no keys.

[[DEMO RESULT: one sentence with the demo question and Stemma's answer, and a link to
the hosted report on GitHub Pages.]]

## Who it is for

[[NAMED USER: one short paragraph. Suggested default: Dana, a strategy analyst who has
until tomorrow morning to tell her CFO whether a competitor's announced plan is
credible. She does not need a reading list; she needs an answer she can defend line by
line when someone asks "where does that number come from?"]]

## One question, start to finish

1. **You ask one question.** `python run.py new "QUESTION"` drafts what the question
   could mean before anything runs: its type, each ambiguous term with its readings,
   and three to five plain criteria the answer must meet. You accept or edit each.
   By default Stemma researches only the question you asked, not a broader decision
   it might inform.
2. **It frames the question as an issue tree.** A lead agent splits the question into
   sub-questions that together cover it. A blind framer, with none of the lead's
   instructions, frames the same question on its own first, then critiques the lead's
   frame and tree.
3. **Workers research each branch on the open web.** Every page or filing a worker
   relies on is captured and fingerprinted (SHA-256) before it can be cited. Figures
   are filed as claims in a claim register, each tied to an exact quotation, and every
   calculation runs in a sandbox from registered inputs.
4. **Every cited figure is checked by an agent that did not write it.** Claim checkers
   read the stored source and record a verdict. Reviewers check each branch and the
   final answer against your criteria. The release gate refuses an answer with an
   untagged figure or a figure whose claim the checker found not supported; a figure the
   checker could not check appears only marked "not checked". It also refuses while a
   final review holds a material point, until the review-round limit is reached; points
   still open then are printed with the answer.
5. **You get the answer first.** The report opens with the answer; every figure has a
   chip that opens the claim, the checker's verdict and the highlighted passage. What
   was not researched, and why, is stated at the end.

[[DEMO RESULT: the demo run's tree (top two levels), its time, its cost, and how many
figures were cited and checked.]]

## Why the issue tree matters

Most research agents search first and organise later. Stemma decides what has to be
true for the answer before it searches: each node of the tree is one short
sub-question, and research, checking and review are all organised by node. That is
what lets a reviewer say exactly which part of the answer a weak figure affects, and
what lets the report say what was set aside and why.

[[TREE EVIDENCE: the tree-check results the owner decides to publish, stated with their
limits. Keep any result where a plain model did better.]]

## Verify it yourself

You do not have to trust the report. Three commands work on any recorded run, offline:

- `python run.py verify RUN_DIR` re-hashes every captured source, finds every quotation
  again in the stored text, re-runs every calculation in the sandbox, re-derives every
  claim's id from its content, and re-hashes every file the register fingerprinted. It
  names each failure; exit code 0 means everything passed.
- `python run.py tamper RUN_DIR` copies the run, changes one byte of the stored source
  behind a cited figure, verifies again and shows the release refused. The original is
  never touched.
- `python run.py report RUN_DIR OUT_DIR` writes a single static page (`index.html`) that
  GitHub Pages can serve as it is.

Verify is not a formality. On the first live run it found that 18 of 181 calculations
no longer reproduce, because their scripts were edited after their claims were filed
(`docs/notes/product-v2.md`).

[[DEMO RESULT: verify's counts on the demo run, and the tamper demo's output.]]

## Head to head with a plain model

The same question goes to Claude Opus 5.5 on its own, with web search and a sandbox
for calculations, and no Stemma. Both answers are scored by the same rules
(`hackathon/headtohead/`): code finds every figure and decides whether the cited page
resolves and contains it; a judge from another model family decides which figures a
source contradicts, and its answer counts only when code finds the quotation it gives.
The plain model is asked to cite a source for every figure, which a plain user might
not ask; without that the comparison would measure nothing.

[[DEMO RESULT: figures cited, figures found in their cited source, sources that resolve,
figures contradicted, and limits admitted, for each side, plus time and cost.]]

## Sponsor integrations, and how to check each one

| Sponsor tool | What Stemma uses it for | How to check it |
|---|---|---|
| World Model (Meterless) | The claim register's engine: a clean-room Python implementation of World Model's specification holds every claim, verdict and supersession. | `python run.py conformance` runs World Model's own conformance runner, unmodified, against the engine: 8 of 8 checks pass, and three deliberate breakages each fail it (`conformance/RESULT.md`). |
| Zetaris | The data layer: each run's evidence tables are registered in Zetaris as one Unified Semantic Layer, and the inputs of every calculation are read back through it in SQL. | `python run.py logs RUN_DIR` shows each data-layer query, its SQL and row counts (from the run's `datalayer/queries.jsonl`). A live test (`tests/datalayer/test_live.py`) syncs an invented run into Zetaris and reads every value back equal to the local SQLite copy, bit for bit. Install: `hackathon/zetaris/README.md`. [[DEMO RESULT: the SQL lines from the demo run's log.]] |
| Nightshift (Meterless) | A second model family: each cited claim's exact passage is put to NVIDIA Nemotron through Nightshift, beside Claude's verdict; a disagreement starts a fresh Claude checker for a third look. | The judge's log lists each Nightshift receipt naming the model. On the first live run, Nemotron agreed on 40 of 49 cited claims; the third look on the other 9 found no Claude error, and 7 of them were citations whose quoted span was narrower than the claim (`docs/notes/register-driver-v2.md`). [[DEMO RESULT: receipts on the demo run.]] |

## Try it

Python 3.12 and Node are needed (Node runs the conformance suite).

```bash
pip install -e '.[evidence,server,register,driver,datalayer,providers,dev]'
```

```bash
SAMPLE_MODE=true python run.py
```

[[SAMPLE PACK: sample mode replays `hackathon/sample/pack/`, which is not in the
repository yet. Until it is, this command says there is no recorded run. Record the
demo run with `python run.py record RUN_DIR hackathon/sample/pack` after checking it
for private content.]]

Sample mode replays a recorded run and checks every file against the pack's manifest.
The judge's log marks every replayed line with its hash; only what this process runs
itself (the conformance suite and a data-layer query) is marked live. No keys are
needed. Docker runs the same sample:

```bash
docker build -t stemma2 .
```

```bash
docker run --rm stemma2
```

A live run (`python run.py ask "QUESTION"`) needs the pinned Claude Code CLI signed in on
the host and the variables in `.env.example`, exported in your shell. Live runs happen
on the host, not in the image. `hackathon/README.md` lists every command.

## Limits

- **A live run is long and costs real money.** The first full run took about 8 hours,
  cost $516.16 and never released an answer; the cause and the fixes are recorded in
  `docs/notes/walking-skeleton.md`. [[DEMO RESULT: the demo run's time and cost.]]
- **Checking is only as good as the stored source.** A checker confirms that a passage
  says what the claim says; it cannot tell whether the source itself is right.
- **The second model family has not yet caught a Claude error.** On the first run its
  value was showing citations whose quoted span is too narrow.
- **Zetaris adds access, not accuracy.** The numbers are the same through SQLite; what
  Zetaris adds is one SQL access point where the evidence sits beside other data, and
  data-quality rules on it.
- **Live runs depend on one pinned Claude Code version (2.1.308)**, launched with a
  scrubbed environment; another version is untested.
- [[LIMITS: anything the demo run or the tree check showed that belongs here.]]

## How it is built

`AGENTS.md` describes the layout and the rules every change follows: code checks exact
facts and never judges research quality, the harness (not an agent) records who ran and
whether checks passed, and every failure is a typed error. The interfaces between parts
are in `docs/CONTRACTS.md`. `docs/notes/` holds dated build logs, negative results
included.

## Licence

Apache-2.0 (`LICENSE`). World Model's conformance runner in `conformance/vendor/` is
Apache-2.0, with its own notice.
