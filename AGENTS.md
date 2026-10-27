# Contributing to Stemma 2

Stemma 2 is a research system that answers a hard business question the way a strong
consulting team would, and shows every figure's evidence. This file is for people and
coding agents who change the code. It never loads at runtime: the product's own
instructions live in `plugin/`, and runs happen in run folders outside this repository.

## Where things are
- `docs/CONTRACTS.md`: the interfaces between the parts. It is binding; a change to an
  interface changes this file first, in the same commit.
- `src/stemma/`: the evidence service (`evidence/`), the claim register and its engine
  (`register/`), the driver that launches every session (`driver/`), the hooks that
  guard every tool call (`hooks/`), the analysis sandbox (`analysis/`), the data layer
  (`datalayer/`), the provider client (`providers/`), onboarding (`onboard/`) and the
  report viewer, verify and tamper commands (`report/`).
- `plugin/`: the role texts and skills each session is given. `docs/ROLE-MAP.md` is
  generated from them by `python -m tools.roles.rolemap`.
- `conformance/`: World Model's own conformance runner, vendored unmodified, and the
  bridge to the Python engine.
- `hackathon/`: `run.py`'s sample mode, replay, judge's log and the head-to-head.
- `docs/notes/`: dated build logs. The code, the tests and `docs/CONTRACTS.md` are the
  reference; references in the notes such as "lesson 2.4", "P1" or "design step 3"
  name private design records not included in this repository.

## Rules
- **Code judges nothing.** Code guarantees that each check ran and tests exact facts:
  bytes, quotations, recomputation, a filled field. Every judgment of research quality
  is an agent's.
- **The harness writes identity and measurement.** No agent writes a field that says
  who it is, which model it ran, or whether its own work passed.
- **Fail loudly.** An empty extraction, a missing verdict or a failed check is a typed
  error that says what happened. Never a silent pass, and never a refusal message that
  suggests a way around the check.
- **Constants.** Each one is a resource limit (tokens, time, bytes, count) or a logical
  invariant, and says so in a comment beside it. No number decides whether research is
  good.
- **"Works" means shown:** a test, or a run you watched. Report negative results; keep
  them in the notes.
- **Tests.** Unit tests never touch the network (use fake transports). A test that must
  reach the live web or a live service is marked and skipped unless its variables are
  set (`STEMMA_LIVE=1`, or the Postgres and Zetaris settings in `.env.example`). Every
  guard gets a hostile test that tries to get round it; concurrent writers get a
  barrier-synchronised race test.
- **Test fixtures are invented.** No real person's or client's material, and no
  benchmark content, in code, prompts, fixtures, docs, commit messages or search
  queries.
- **Downloaded files are untrusted data:** their own folder, `python -I`, and never run
  their code.
- **Secrets.** None in the repository: `.env.example` holds names only, and the values
  are exported at run time.

## Tooling
- Python 3.12, typed. Install with
  `pip install -e '.[evidence,server,register,driver,datalayer,providers,dev]'`.
- Before a commit, all of these pass: `pytest tests hackathon/tests`,
  `ruff check src tests hackathon run.py`, `mypy --strict src`, and
  `python -m tools.roles.rolemap check`.
- Dependencies are pinned, minimal and licence-compatible with Apache-2.0 (no AGPL or
  GPL).
- Commit small, saying what changed and why.

## Writing for readers
Answer first, in full sentences, with plain headers and every term explained once.
