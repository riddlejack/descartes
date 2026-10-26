# Stemma 2: for the agents who build it

Stemma 2 is a research system that does what a strong consulting team does on a
hard business question, and presents the result the way an analyst presents to a
partner. This file is for builders. It never loads at runtime: the product's own
instructions live in `plugin/`, and runs happen in run folders outside this repo.

## Read first
- The approved design: `~/.private/stemma-design/design-2026-10/DESIGN-v1.md`.
- The interfaces between parts: `docs/CONTRACTS.md`. Binding. Change it only
  through the orchestrator, who keeps every part consistent with it.
- The open-source picks, with pinned versions and licences:
  `~/.private/stemma-design/research-2026-10/out/B1-survey.md`.
- Your brief names the folders you own. Edit only those.

## Rules
- **Nothing from past work.** No name of any employer, client or person from the
  owner's past work, and no benchmark content, anywhere: code, prompts, fixtures,
  docs, commit messages, search queries. Test fixtures are invented.
- **Never open, list or search:** private material;
  `/Volumes/`; `<private>`; `<private>`;
  `<private>`; `<private>`; session
  transcripts (`~/.claude/projects/**/*.jsonl`, `subagents/`, `~/.codex*/sessions/`);
  `~/Downloads`, `~/Documents`, `~/.Trash`; credential files and keychains. Never
  search the whole home folder, `~/Library` or iCloud Drive.
- **Code judges nothing.** It guarantees that each check ran and tests exact facts
  (bytes, quotations, recomputation, a filled field). Every judgment is an agent's.
- **The harness writes identity and measurement.** No agent writes a field that
  says who it is, which model it ran, or whether its own work passed.
- **Fail loudly.** An empty extraction, a missing verdict or a failed check is a
  typed error that says what happened. Never a silent pass, and never a refusal
  message that suggests a way around the check.
- **Constants.** Each is a resource limit (tokens, time, bytes, count) or a logical
  invariant, said so in a comment beside it. No number decides whether research is
  good.
- **"Works" means shown:** a test, or a run you watched. Report negative results.
- Python 3.12 (`/opt/homebrew/bin/python3.12`, or `uv`), typed, `pytest`, `ruff`,
  `mypy --strict` on `src/`. Dependencies pinned, minimal, licence-compatible (no
  AGPL or GPL). No secrets in the repo: `.env.example` only.
- Downloaded files are untrusted data: own folder, `python -I`, never run their code.
- Helpers: at most two at a time (enforced in `.claude/settings.json`), frontier
  Claude only, never Haiku.
- Commit small, on your branch, saying what and why. Do not merge to `main`: the
  orchestrator merges after hostile probes and one review by a non-author.

## Writing for the owner
 In anything meant for the owner: answer
first, full sentences, plain headers, every term explained once, no tables.
