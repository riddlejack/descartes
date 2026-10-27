# Descartes

> Accept nothing as true that you cannot verify, and divide every difficulty into as many parts as it takes to solve it. (Descartes, *Discourse on the Method*, 1637, Part II, paraphrased)

Descartes answers one hard business question the way a strong consulting team does: it frames the question, breaks it into an issue tree, staffs each branch with its own researcher and reviewer, and answers the question asked first. Every figure in the answer quotes its passage from a stored, fingerprinted copy of the source, is checked by an agent that did not write it, and opens that passage in the report. Code, not a model, decides whether the answer may be released, and you can re-check a finished run on your own machine, offline, with no keys.

Entry for the Open Agent Hackathon 2026, Track 02, "The Agent That Can Explain Why", by Jack Riddle. The demo video and slide deck are linked from the submission. Descartes was called Stemma 2 while it was built; the Python package, its paths and its environment variables keep the name `stemma`.

```bash
python run.py conformance
```

That runs Meterless World Model's own conformance suite, unmodified, against the claim register's engine, with no keys. Setup, sample mode and the checks you can run yourself are under [Try it](#try-it).

## The problem

Research with real breadth and depth either takes a team of people or gets done inadequately. As a consulting analyst, I broke hard problems down and researched them; for proposals that meant hours of public research, either reading articles by hand or, like me, stitching deep-research reports together. Both ways make mistakes, and neither is as structured as the work deserves.

The mistakes get expensive. Deloitte refunded A$97,587 on an A$440,000 report for the Australian government that cited papers that do not exist and a quote from a Federal Court judgment that the judgment does not contain ([The Register](https://www.theregister.com/2025/10/06/deloitte_ai_report_australia/); the refund figure was disclosed at Senate estimates, [Region Canberra](https://region.com.au/government-urged-to-get-deloittes-total-fee-returned-for-delivering-a-botched-ai-report/911846/)). The AI Hallucination Cases database lists 2,225 court cases in which AI-invented material reached a filing, 879 of them filed by lawyers ([Charlotin](https://www.damiencharlotin.com/hallucinations/), updated 27 Oct 2026). A deep-research tool gives a confident report in minutes, and nothing tells you what it skipped or whether its sources say what it claims.

## Who it is for

Anyone who has to stand behind an answer: consultants, investment bankers, equity and credit analysts, corporate strategy teams, independent businesses researching their own questions, and scientists. Should we enter this market? How big is it? Can this company pay its debts? Each becomes an issue tree you can inspect. The reader who decides (a partner, an investment committee, a board) can open any figure and see the sentence it rests on.

It is not for quick look-ups, or for questions where a five-minute report that is mostly right is good enough.

## One question, start to finish

1. **You ask one question.** `python run.py new "QUESTION"` drafts what the question could mean before anything runs: its type, each ambiguous term with its readings, and three to five plain criteria the answer must meet. You accept or edit each. By default Descartes researches only the question you asked, not a broader decision it might inform.
2. **It frames the question as an issue tree.** An idea generator lists what could matter. A lead agent builds an issue tree of short sub-questions that together decide the answer. A blind framer, with none of the lead's instructions, frames the same question on its own first, then critiques the lead's frame and tree, and the lead answers every point in writing.
3. **Workers research each branch on the open web.** Every page or filing a worker relies on is captured and fingerprinted (SHA-256) before it can be cited. Figures are filed as claims in a claim register, each tied to an exact quotation, and every calculation runs in a sandbox from registered inputs. A branch counts as done only when a separate branch reviewer says it answered its sub-question.
4. **Every cited figure is checked by an agent that did not write it.** Claim checkers read the stored source and record a verdict. NVIDIA Nemotron, through Meterless Nightshift, gives a second opinion from another model family on each quoted claim. Three final reviewers read the draft: one as the person who asked, one working back from statements to sources, one for mechanical faults. The release gate refuses an answer with an untagged figure or a figure whose claim the checker found not supported; a figure the checker could not check appears only marked "not checked". It also refuses while a final review holds a material point; a run that reaches its review-round limit without clearing it ends unreleased and says so.
5. **You get the answer first.** The report opens with the answer; every figure has a chip that opens the claim, the checker's verdict and the highlighted passage. What was not researched, and why, is stated at the end.

Ten roles in all. Each instruction a role reads names the failure it prevents and the evidence for it, generated into [`docs/ROLE-MAP.md`](docs/ROLE-MAP.md).

An example: a framing run on "Is the U.S. on track to reach 500,000 public EV charging ports by 2030?" (27 October 2026; framing only, so no branch was researched). Its tree, top two levels:

- Is the U.S. public port count on course to reach 500,000 by end-2030?
  - How many public charging ports are in service now?
  - Have yearly net additions of public ports been rising, and how fast? (a crux: openings, removals, reclassifications)
  - Will net additions from now to 2030 run above or below the recent pace? (ports already funded or committed; builds not yet committed; time from award to opening; removals)
  - What else in primary sources bears on the 2030 count? (an open lane)

The blind framer's critique changed it: the lead restated the root question, cut the 2030 subtree from 27 live questions to 17, and made two cruxes: whether yearly additions are steady or accelerating, and "did openings fall after the 30C tax credit stopped applying?" The questions it merged stay in the tree, marked set aside with the reason.

## Where the method comes from

The framing and the issue tree are the method I used as a consultant, and they follow the problem-solving literature consultants train on: Barbara Minto's *The Pyramid Principle*, Conn and McLean's *Bulletproof Problem Solving*, Rasiel and Friga's *The McKinsey Mind*, Friga's *The McKinsey Engagement* and Howson's *Commercial Due Diligence*. Each level of the tree splits its parent by one analytical act (parts of a whole, causes of one effect, or kinds of one thing), with no gaps or overlaps. Holding a working answer against its strongest rival comes from decision research (Nutt's field studies of management decisions, Platt's "Strong Inference", Chamberlin's multiple working hypotheses); the blind framer and the reviewers from red-teaming and premortem practice (Klein) and the research on dissent and accountability (Nemeth; Lerner and Tetlock). How the agents are run follows Anthropic's engineering guidance, starting with ["How we built our multi-agent research system"](https://www.anthropic.com/engineering/multi-agent-research-system). No author or publisher has reviewed or endorsed Descartes.

## Verify it yourself

You do not have to trust the report. Three commands work on any recorded run, offline:

- `python run.py verify RUN_DIR` re-hashes every captured source, finds every quotation again in the stored text, re-runs every calculation in the sandbox, re-derives every claim's id from its content, and re-hashes every file the register fingerprinted. It names each failure; exit code 0 means everything passed.
- `python run.py tamper RUN_DIR` copies the run, changes one byte of the stored source behind a cited figure, verifies again and shows the release refused. The original is never touched.
- `python run.py report RUN_DIR OUT_DIR` writes a single static page (`index.html`) that GitHub Pages can serve as it is.

Verify is not a formality. On the first live run it found that 18 of 181 calculations no longer reproduce, because their scripts were edited after their claims were filed (`docs/notes/product-v2.md`).


## Sponsor technology

| | What Descartes uses it for | What breaks without it | How to check it |
|---|---|---|---|
| **Zetaris** | The data layer. Each run's evidence tables (captures, claims, support, calculation inputs, entities, verdicts, SEC facts, table cells) are registered in a local Zetaris install as one Unified Semantic Layer per run. Every calculation's inputs are read back through it in SQL, and a one-bit difference from the registered value is refused. A read-only `query_data` tool answers questions over the evidence. | The same evidence and read-back run through SQLite, but with no governed SQL access point where the evidence sits beside other data, and no data-quality rules on it. | `python run.py logs RUN_DIR` shows each query's SQL and row count (from the run's `datalayer/queries.jsonl`). `tests/datalayer/test_live.py` syncs an invented run into Zetaris and reads every value back equal to the local SQLite copy, bit for bit. Install: [`hackathon/zetaris/README.md`](hackathon/zetaris/README.md). |
| **Meterless World Model** | The claim register's engine. A clean-room Python implementation of World Model's data model holds every claim, verdict, supersession and the issue tree. | The register's format would be Descartes's own, with no outside specification or test suite to check a claim's history against. | `python run.py conformance` runs World Model's own conformance runner, unmodified, against the engine: 8 of 8 checks pass, and three deliberate breakages each fail it (`conformance/RESULT.md`). |
| **Meterless Nightshift + NVIDIA Nemotron** | A second model family. Each quoted claim's exact passage is put to Nemotron 3 Ultra (fallback Nemotron 3.5 Lightning) through Nightshift, beside Claude's verdict; a disagreement starts a fresh Claude checker for a third look. It never approves a claim and never lowers a check. | Every check would come from one model family. | The judge's log lists each Nightshift receipt naming the model that answered. On the first live run, Nemotron agreed on 40 of 49 cited claims; the third look on the other 9 found no Claude error, and 7 of them were citations whose quoted span alone did not show the claim, which is exactly what a reader sees on opening one (`docs/notes/register-driver-v2.md`). |

### How Descartes connects to Nightshift

`src/stemma/providers/nightshift.py` is a client for the Nightshift Runtime's OpenAI-compatible contract (0.90.0). It reads `METERLESS_BASE_URL` (default `http://127.0.0.1:8088/v1`) and `METERLESS_RUNTIME_TOKEN`, sends `meterless-app: stemma` on every request and `meterless-scope: user` on chat, checks status with `GET /v1/models`, and calls `POST /v1/chat/completions` with the Nemotron model named explicitly. Every call writes a receipt: the route asked for, the model the answer names, the `meterless-*` response headers, the request's digest and sizes, timing and usage. The token and the message text are never logged, and the client never falls back to another provider. The NVIDIA key lives only in Nightshift.

**What does not run through Nightshift.** Descartes's research agents (the lead, workers, framer, checkers and reviewers) run on Claude Opus 5.5 in Claude Code, signed in on a Claude subscription. Inside a run, every model call the host makes itself goes through Nightshift; the agents' own calls do not. Running them through Nightshift would need a bridge from Claude Code's Anthropic Messages format to Nightshift's OpenAI-compatible one, and would be billed at Anthropic API rates through a key configured in Nightshift. That bridge is not built. Nightshift is therefore a working provider for one real feature, not yet the primary provider of the research calls.

## Where it can go (designed or planned, not built)

Descartes is wired for Claude Code today, because that is the subscription I pay for, rather than thousands of dollars in API tokens. What exists and what does not:

| Extension | Today | Not built yet |
|---|---|---|
| Other harnesses (Codex, Cursor) | The evidence service and the register run as their own MCP server (`plugin/.mcp.json`), which both speak | Role identity rests on a Claude Code hook; a design for other harnesses is written, not built |
| API models, or Nightshift end to end | Nightshift already carries the second-family check | Research roles on non-Claude models; the format bridge for Claude Code through Nightshift |
| Fully local (for example a DGX Spark) | `verify` needs no model and no network; frozen-web research reads archived sources | Research on local models; untested, since I have no machine to test it on |
| A company's own files | Attached files are captured, hashed, quoted and checked like web pages (`capture_file`) | Connectors to Drive, SharePoint and similar |
| A company's databases through Zetaris | Each run's evidence already lives in a Zetaris semantic layer | Capturing a company's own Zetaris query results as receipted evidence |
| Data sources such as PitchBook | Two worked examples of a source route that issues receipts: SEC filings and market series | Any paid-database connector (and its licence terms) |
| Your own skills and playbooks | Four question-type cards ship as skills | Loading practice cards pinned by SHA-256 is built on a branch, not merged |

## Try it

Python 3.12 and Node are needed (Node runs the conformance suite).

```bash
pip install -e '.[evidence,server,register,driver,datalayer,providers,dev]'
```

```bash
SAMPLE_MODE=true python run.py
```

Sample mode replays a recorded pack from `hackathon/sample/pack/`. No pack is committed yet, so for now this command reports that there is no recorded run; `python run.py replay PACK` replays any pack you have.

Sample mode replays a recorded run and checks every file against the pack's manifest. The judge's log marks every replayed line with its hash; only what this process runs itself (the conformance suite and a data-layer query) is marked live. No keys are needed. Docker runs the same sample:

```bash
docker build -t stemma2 .
```

```bash
docker run --rm stemma2
```

A live run (`python run.py ask "QUESTION" [--as-of YYYY-MM-DD]`) needs the pinned Claude Code CLI signed in on the host and the variables in `.env.example`, exported in your shell. Live runs happen on the host, not in the image. Zetaris and Nightshift are optional for a live run and stay on the host. [`hackathon/README.md`](hackathon/README.md) lists every command, including `status`, `logs`, `replay` and `record`.

## Limits

- **A live run is long and costs real money.** The first full run took about 8 hours, cost $516.16 and never released an answer; the cause and the fixes are recorded in `docs/notes/walking-skeleton.md`. A later run stopped after about 30 minutes and $33, also without a release.
- **Checking is only as good as the stored source.** A checker confirms that a passage says what the claim says; it cannot tell whether the source itself is right.
- **The second model family has not yet caught a Claude error.** On the first run its value was showing citations whose quoted span is too narrow.
- **Zetaris adds access, not accuracy.** The numbers are the same through SQLite; what Zetaris adds is one SQL access point where the evidence sits beside other data, and data-quality rules on it.
- **Live runs depend on one pinned Claude Code version (2.1.308)**, launched with a scrubbed environment; another version is untested.
- **Tree quality rests on my judgment as a former consultant;** it has not been measured on a broad test.
- **Research runs on Claude only.** Nightshift serves the second-family check (above); the extensions in the table above are designed or planned, not built.
- **No run has yet released an answer end to end, and no accuracy comparison is claimed.** The gate refusing an answer whose final review is still open is the design working; it also means there is no released answer to show yet.
- **Zetaris can be unreachable mid-run.** The run then falls back to the local copy and logs each fallback loudly.

## How it was built

Built by Jack Riddle in Claude Code, with Claude Opus 5.5 writing the code and Codex as the reviewer at merge gates. Cursor was never used. At run time: Claude Opus 5.5 for every research role, NVIDIA Nemotron 3 Ultra and 3.5 Lightning through Meterless Nightshift for the second-family check, Zetaris as the data layer, and Meterless World Model's data model for the register.

Where to look:
- `src/stemma/register/`: the claim register on World Model's data model (`store.py`, `wm/`), and the second-family check (`second_family.py`).
- `src/stemma/providers/nightshift.py`: the Nightshift client and its receipts.
- `src/stemma/datalayer/`: the sync into Zetaris (or SQLite), the read-back of calculation inputs, and the query log.
- `src/stemma/report/`: `verify`, `tamper` and the static report.
- `plugin/`: the role texts, generated into `docs/ROLE-MAP.md`.
- `docs/CONTRACTS.md`: the interfaces between parts. `docs/notes/`: dated build logs, negative results included.

`AGENTS.md` describes the layout and the rules every change follows: code checks exact facts and never judges research quality, the harness (not an agent) records who ran and whether checks passed, and every failure is a typed error.

## Pre-existing work

Before the build window, I explored the idea in a private prototype (September to October 2026). No code, data or prompts from it are in this repository, which was started on 26 October 2026 (first commit 03:35 UTC). The problem-solving books the method draws on are my own copies and are not in the repository. The same statement is in the submission form.

## Licence

Apache-2.0 (`LICENSE`). World Model's conformance runner in `conformance/vendor/` is Apache-2.0, with its own notice.
