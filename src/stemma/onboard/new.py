"""`stemma new "QUESTION"`: the onboarding step, ending in the run's `task.json`.

Interactive: the draft is shown piece by piece, and the user accepts (Enter) or edits
each: the scope (D1, narrow by default), the reading of each ambiguous term (D3), the
criteria (D4), the options if they name any, and each role's model and effort (D10).

Non-interactive, for pre-registered runs: `--answers answers.json` supplies every answer
and nothing is asked. `--draft draft.json` uses a saved draft instead of the model call,
so a pre-registered task is reproducible byte for byte; `--save-draft` writes one.

`task.json` is written in W2's v0.4 format: `question`, `scope`, `criteria`, `options`
(only when the user named them), `roles`, `attachments`, and `intake`, which carries the
question type, the broader decision named as context, and the readings taken.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from stemma.onboard.draft import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    Draft,
    ModelCall,
    OnboardError,
    Term,
    draft,
    parse_draft,
)

SCOPES: Final = ("question_only", "broader_decision")  # invariant: D1's two readings
# Resource limit: criteria and options a task may carry (the driver's own limit).
MAX_ITEMS: Final = 12
ANSWER_KEYS: Final = frozenset({"scope", "broader_decision", "readings", "criteria",
                                "options", "roles", "as_of"})
TYPE_WORDS: Final = {"fact": "a fact question: the answer is a finding, with its confidence",
                     "cause": "a why question: the answer is a cause",
                     "decision": "a decision: options compared on shared criteria",
                     "other": "another kind of question"}

Ask = Callable[[str], str]
Say = Callable[[str], None]


@dataclass
class Answers:
    scope: str
    broader_decision: str | None
    readings: dict[str, str]  # term -> the reading taken, or "both"
    criteria: list[str]
    options: list[str] | None
    roles: dict[str, dict[str, str]]
    as_of: str | None = None


def role_names() -> tuple[str, ...]:
    from stemma.driver.task import ALL_ROLE_NAMES

    return tuple(ALL_ROLE_NAMES)


def default_roles(model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT
                  ) -> dict[str, dict[str, str]]:
    """Every role on one model and effort (D10: Opus 5.5 at high, the lead included)."""
    return {r: {"model": model, "effort": effort} for r in role_names()}


def apply_role_changes(roles: dict[str, dict[str, str]], spec: str) -> None:
    """`lead=max worker=claude-fable-5-1:high` changes those roles in place."""
    from stemma.driver.config import AVAILABLE_MODELS, EFFORTS

    for token in spec.split():
        role, sep, value = token.partition("=")
        if not sep or role not in roles:
            raise OnboardError(f"{token!r}: name a role ({', '.join(roles)}) and =")
        model, _, effort = value.rpartition(":") if ":" in value else ("", "", value)
        if value in AVAILABLE_MODELS:
            model, effort = value, ""
        if model and model not in AVAILABLE_MODELS:
            raise OnboardError(f"{token!r}: the model must be one of {list(AVAILABLE_MODELS)}")
        if effort and effort not in EFFORTS:
            raise OnboardError(f"{token!r}: the effort must be one of {list(EFFORTS)}")
        if model:
            roles[role]["model"] = model
        if effort:
            roles[role]["effort"] = effort


def _reading_note(t: Term, taken: str) -> str:
    if taken == "both":
        return (f'"{t.term}": both readings are researched ({"; ".join(t.readings)}), and '
                f"the answer says which one the stated target or source uses. Why it "
                f"matters: {t.why_it_matters}")
    return f'"{t.term}" is read as: {taken}. Why it matters: {t.why_it_matters}'


def default_reading(t: Term) -> str:
    """D3 without asking: both when both are cheap, else the reading the draft picked."""
    return "both" if t.both_cheap or not t.pick else t.pick


# ---------------------------------------------------------------- interactive
def interview(question: str, d: Draft, ask: Ask, say: Say) -> Answers:
    say(f"\nQuestion: {question}")
    say(f"Type: {TYPE_WORDS[d.question_type]}. ({d.question_type_reason})")

    say("\n1. Scope")
    say("   Research only this question, or also a broader decision it informs?")
    if d.broader_decision:
        say(f"   (A broader decision it may inform: {d.broader_decision})")
    say("   [1] only this question (the default)   [2] also the broader decision")
    scope, broader = "question_only", d.broader_decision
    while True:
        got = ask("   > ").strip()
        if got in ("", "1"):
            break
        if got == "2":
            scope = "broader_decision"
            other = ask("   Name the broader decision, or press Enter to keep the one "
                        "above: ").strip()
            broader = other or broader
            if not broader:
                say("   A broader decision needs a name.")
                scope = "question_only"
                continue
            break
        say("   Type 1 or 2, or press Enter.")

    readings: dict[str, str] = {}
    if d.terms:
        say("\n2. Terms with two readings")
    for t in d.terms:
        letters = "abcdefgh"[:len(t.readings)]
        say(f'   "{t.term}"')
        for letter, r in zip(letters, t.readings, strict=False):
            say(f"     ({letter}) {r}")
        say(f"     Why it matters: {t.why_it_matters}")
        default = default_reading(t)
        if t.changes_whole_research:
            say("     These readings would change the whole research, so you choose.")
        shown = "both" if default == "both" else f"({letters[t.readings.index(default)]})"
        while True:
            got = ask(f"     Reading: {'/'.join(letters)}/both, Enter for {shown} > ").strip()
            if not got:
                readings[t.term] = default
                break
            if got == "both":
                readings[t.term] = "both"
                break
            if len(got) == 1 and got in letters:
                readings[t.term] = t.readings[letters.index(got)]
                break
            say("     Type one of the letters, or both.")

    say('\n3. What "done" means: the criteria the answer must meet')
    say("   Enter keeps a criterion, new text replaces it, a single - drops it.")
    criteria: list[str] = []
    for i, c in enumerate(d.criteria, start=1):
        say(f"   C{i}. {c}")
        got = ask("     > ").strip()
        if got == "-":
            continue
        criteria.append(got or c)
    while len(criteria) < MAX_ITEMS:
        got = ask("   Add a criterion, or press Enter to finish: ").strip()
        if not got:
            if criteria:
                break
            say("   The answer needs at least one criterion.")
            continue
        criteria.append(got)

    say("\n4. Options to compare (only if you name them)")
    options: list[str] | None = None
    if d.options_named:
        say(f"   The question names: {'; '.join(d.options_named)}")
        got = ask("   Enter keeps them, - for none, or list your own separated by ; > ").strip()
        options = (None if got == "-" else list(d.options_named) if not got
                   else [o.strip() for o in got.split(";") if o.strip()])
    else:
        got = ask("   Options, separated by ; (press Enter for none) > ").strip()
        options = [o.strip() for o in got.split(";") if o.strip()] or None
    if options is not None and len(options) < 2:
        say("   One option is not a choice; no options are recorded.")
        options = None

    roles = default_roles()
    say(f"\n5. Models: every role runs on {DEFAULT_MODEL} at {DEFAULT_EFFORT} effort.")
    say(f"   Roles: {', '.join(roles)}")
    while True:
        got = ask("   Enter keeps this, or change roles like lead=max "
                  "worker=claude-fable-5-1:high > ").strip()
        if not got:
            break
        try:
            apply_role_changes(roles, got)
            break
        except OnboardError as exc:
            say(f"   {exc}")
    return Answers(scope, broader, readings, criteria, options, roles)


# -------------------------------------------------------------- answers file
def answers_from_file(path: Path, d: Draft) -> Answers:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OnboardError(f"the answers file cannot be read: {exc}") from exc
    if not isinstance(data, dict):
        raise OnboardError("the answers file is not a JSON object")
    unknown = sorted(set(data) - ANSWER_KEYS)
    if unknown:
        raise OnboardError(f"the answers file has unknown keys: {unknown}")
    scope = data.get("scope", "question_only")
    if scope not in SCOPES:
        raise OnboardError(f"scope must be one of {list(SCOPES)}")
    broader = data.get("broader_decision", d.broader_decision)
    if scope == "broader_decision" and not (isinstance(broader, str) and broader.strip()):
        raise OnboardError("scope broader_decision needs the broader decision named")
    readings = {t.term: default_reading(t) for t in d.terms}
    given = data.get("readings") or {}
    if not isinstance(given, dict):
        raise OnboardError("readings must be an object of term -> reading or both")
    terms = {t.term: t for t in d.terms}
    for term, taken in given.items():
        if term not in terms:
            raise OnboardError(f"readings names {term!r}, which the draft does not list")
        if taken != "both" and taken not in terms[term].readings:
            raise OnboardError(f"readings[{term!r}] must be one of its readings, or both")
        readings[term] = taken
    unchosen = [t.term for t in d.terms if t.changes_whole_research and t.term not in given]
    if unchosen:
        raise OnboardError(f"readings must choose for {', '.join(map(repr, unchosen))}: "
                           f"their readings would change the whole research (D3)")
    criteria = data.get("criteria", list(d.criteria))
    if (not isinstance(criteria, list) or not criteria
            or not all(isinstance(c, str) and c.strip() for c in criteria)):
        raise OnboardError("criteria must be a list of at least one text")
    options = data.get("options")
    if options is not None and (not isinstance(options, list) or len(options) < 2
                                or not all(isinstance(o, str) and o.strip() for o in options)):
        raise OnboardError("options must list at least two texts, or be left out")
    roles = default_roles()
    for role, spec in (data.get("roles") or {}).items():
        if role not in roles or not isinstance(spec, dict) or not set(spec) <= {"model",
                                                                                 "effort"}:
            raise OnboardError(f"roles[{role!r}] must be a known role with model and/or effort")
        if spec.get("model"):
            apply_role_changes(roles, f"{role}={spec['model']}")
        if spec.get("effort"):
            apply_role_changes(roles, f"{role}={spec['effort']}")
    as_of = data.get("as_of")
    if as_of is not None and not isinstance(as_of, str):
        raise OnboardError("as_of must be a YYYY-MM-DD date")
    return Answers(scope, broader.strip() if isinstance(broader, str) else None, readings,
                   [c.strip() for c in criteria], options, roles, as_of)


# ------------------------------------------------------------------- task.json
def build_task(question: str, d: Draft, a: Answers, files: list[Path]) -> dict[str, Any]:
    if len(a.criteria) > MAX_ITEMS or (a.options and len(a.options) > MAX_ITEMS):
        raise OnboardError(f"at most {MAX_ITEMS} criteria and {MAX_ITEMS} options "
                           f"(a resource limit)")
    intake = {"Question type": f"{d.question_type}: {d.question_type_reason}",
              "Scope": ("Research only this question; the broader decision is context for "
                        "the deliverable's form, never more research."
                        if a.scope == "question_only"
                        else "Research this question and the broader decision it informs.")}
    if a.broader_decision:
        intake["Broader decision it may inform"] = a.broader_decision
    terms = {t.term: t for t in d.terms}
    if a.readings:
        intake["Ambiguous terms"] = " ".join(_reading_note(terms[k], v)
                                             for k, v in a.readings.items())
    task: dict[str, Any] = {"question": question.strip(), "scope": a.scope,
                            "criteria": a.criteria, "roles": a.roles,
                            "attachments": [str(f.resolve()) for f in files],
                            "intake": intake}
    if a.options:
        task["options"] = a.options
    if a.as_of:
        task["as_of"] = a.as_of
    return task


def check_with_driver(path: Path) -> None:
    """The driver's own reading of the task, when it is the v0.4 driver; a refusal
    there is a refusal here."""
    from stemma.driver import task as driver_task
    from stemma.driver.errors import DriverError

    if "scope" not in getattr(driver_task, "_TASK_KEYS", frozenset()):
        return  # an older driver; it would refuse the v0.4 keys this writes
    try:
        driver_task.load_task(path)
    except DriverError as exc:
        raise OnboardError(f"the driver refuses this task.json: {exc}") from exc


def summary(task: dict[str, Any]) -> str:
    lines = [f"Question: {task['question']}",
             f"Scope: {'only this question' if task['scope'] == 'question_only' else 'also the broader decision'}"]
    lines += [f"  C{i}. {c}" for i, c in enumerate(task["criteria"], start=1)]
    if task.get("options"):
        lines.append(f"Options: {'; '.join(task['options'])}")
    efforts = sorted({f"{v['model']} at {v['effort']}" for v in task["roles"].values()})
    lines.append(f"Models: {', '.join(efforts)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, call: ModelCall | None = None,
         ask: Ask = input, say: Say = print) -> int:
    ap = argparse.ArgumentParser(prog="stemma new", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question")
    ap.add_argument("--file", type=Path, action="append", default=[],
                    help="a file for the research team (repeat for several)")
    ap.add_argument("--answers", type=Path, help="answer every question from this file")
    ap.add_argument("--draft", type=Path, help="use this saved draft, not a model call")
    ap.add_argument("--save-draft", type=Path, help="also write the draft here")
    ap.add_argument("--out", type=Path, default=Path("task.json"))
    ap.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    args = ap.parse_args(argv)
    try:
        for f in args.file:
            if not f.is_file():
                raise OnboardError(f"{f} is not a file")
        if args.out.exists() and not args.force:
            raise OnboardError(f"{args.out} exists; pass --force to replace it")
        if args.draft:
            try:
                d = parse_draft(json.loads(args.draft.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:
                raise OnboardError(f"the draft file cannot be read: {exc}") from exc
        else:
            say("Drafting the onboarding questions (one model call)…")
            d = draft(args.question, list(args.file), call)
        if args.save_draft:
            args.save_draft.write_text(json.dumps(d.to_json(), indent=1, ensure_ascii=False)
                                       + "\n", encoding="utf-8")
        answers = (answers_from_file(args.answers, d) if args.answers
                   else interview(args.question, d, ask, say))
        task = build_task(args.question, d, answers, list(args.file))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(task, indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        check_with_driver(args.out)
    except OnboardError as exc:
        say(f"stemma new: {exc}")
        return 2
    except (EOFError, KeyboardInterrupt):
        say("\nstemma new: stopped; nothing was written.")
        return 130
    say("\n" + summary(task))
    say(f"\nWrote {args.out}. Start the run with: python -m stemma.driver start {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
