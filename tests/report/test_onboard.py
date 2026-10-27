"""Onboarding (`stemma new`): one drafted model reply, the user's edits, and the
task.json the driver reads. The model is faked here except in the live test."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from stemma.onboard import draft as draft_mod
from stemma.onboard import new
from stemma.onboard.draft import OnboardError, parse_draft

QUESTION = "Will the invented town of Lowmarsh have 400 public benches by 2030?"
REPLY: dict[str, Any] = {
    "question_type": "fact",
    "question_type_reason": "It asks whether a count reaches a level by a date.",
    "broader_decision": "Whether the council should fund more benches.",
    "ambiguous_terms": [
        {"term": "public benches", "readings": ["benches in parks only",
                                                "benches anywhere the public may sit"],
         "both_cheap": True, "changes_whole_research": False,
         "why_it_matters": "the count differs by about a third", "pick": None},
        {"term": "by 2030", "readings": ["by 1 January 2030", "by 31 December 2030"],
         "both_cheap": False, "changes_whole_research": True,
         "why_it_matters": "a year of building", "pick": "by 31 December 2030"}],
    "criteria": ["Says whether Lowmarsh reaches 400 public benches by 2030",
                 "States today's count and where it comes from",
                 "Says how fast benches are being added"],
    "options_named": []}


def fake_call(reply: Any = REPLY) -> Any:
    seen: list[str] = []

    def call(prompt: str) -> str:
        seen.append(prompt)
        return "```json\n" + json.dumps(reply) + "\n```"

    call.seen = seen  # type: ignore[attr-defined]
    return call


def scripted(*answers: str) -> tuple[Any, list[str]]:
    it: Iterator[str] = iter(answers)
    shown: list[str] = []

    def ask(prompt: str) -> str:
        shown.append(prompt)
        return next(it)

    return ask, shown


def run_new(tmp_path: Path, argv: list[str], ask: Any = None,
            call: Any = None) -> tuple[int, dict[str, Any] | None, list[str]]:
    out: list[str] = []
    if isinstance(ask, tuple):
        ask = ask[0]
    code = new.main([QUESTION, "--out", str(tmp_path / "task.json"), *argv],
                    call=call or fake_call(), ask=ask or scripted()[0], say=out.append)
    path = tmp_path / "task.json"
    return code, (json.loads(path.read_text()) if path.exists() else None), out


def test_pressing_enter_everywhere_takes_the_narrow_scope_and_the_draft(tmp_path: Path) -> None:
    ask, _shown = scripted("", "", "", "", "", "", "", "", "")
    code, task, out = run_new(tmp_path, [], ask)
    assert code == 0 and task is not None
    assert task["question"] == QUESTION and task["scope"] == "question_only"
    assert task["criteria"] == REPLY["criteria"]
    assert "options" not in task
    assert set(task["roles"]) == set(new.role_names())
    assert all(v == {"model": "claude-opus-5-5", "effort": "high"} for v in task["roles"].values())
    terms = task["intake"]["Ambiguous terms"]
    assert '"public benches": both readings are researched' in terms
    assert '"by 2030" is read as: by 31 December 2030' in terms
    assert task["intake"]["Broader decision it may inform"].startswith("Whether the council")
    assert "never more research" in task["intake"]["Scope"]
    text = "\n".join(out)
    assert "Research only this question, or also a broader decision it informs?" in text
    assert "These readings would change the whole research, so you choose." in text


def test_the_user_edits_each_piece(tmp_path: Path) -> None:
    ask, _ = scripted("2", "Whether to move benches from parks to streets",  # scope
                      "a", "b",                                              # readings
                      "", "Gives today's count with its date", "-",           # criteria
                      "Names what would change the answer", "",
                      "build more; move existing ones",                       # options
                      "lead=max worker=claude-fable-5-1:xhigh")               # roles
    code, task, _ = run_new(tmp_path, [], ask)
    assert code == 0 and task is not None
    assert task["scope"] == "broader_decision"
    assert task["intake"]["Broader decision it may inform"] == \
        "Whether to move benches from parks to streets"
    assert task["criteria"] == [REPLY["criteria"][0], "Gives today's count with its date",
                                "Names what would change the answer"]
    assert task["options"] == ["build more", "move existing ones"]
    assert task["roles"]["lead"] == {"model": "claude-opus-5-5", "effort": "max"}
    assert task["roles"]["worker"] == {"model": "claude-fable-5-1", "effort": "xhigh"}
    assert '"public benches" is read as: benches in parks only' in task["intake"]["Ambiguous terms"]


def test_a_bad_answer_is_asked_again_not_guessed(tmp_path: Path) -> None:
    ask, _shown = scripted("3", "", "z", "", "", "", "", "", "", "", "lead=huge", "")
    code, task, out = run_new(tmp_path, [], ask)
    assert code == 0 and task is not None
    assert any("Type 1 or 2, or press Enter." in line for line in out)
    assert "Type one of the letters, or both." in "\n".join(out)
    assert any("effort must be one of" in line for line in out)


def test_one_option_is_not_a_choice(tmp_path: Path) -> None:
    ask, _ = scripted("", "", "", "", "", "", "", "only one", "")
    code, task, out = run_new(tmp_path, [], ask)
    assert code == 0 and task is not None and "options" not in task
    assert any("One option is not a choice" in line for line in out)


def test_an_answers_file_asks_nothing_and_a_saved_draft_makes_no_call(tmp_path: Path) -> None:
    saved = tmp_path / "draft.json"
    run_new(tmp_path / "a", ["--save-draft", str(saved)], scripted(*[""] * 9))
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({
        "scope": "question_only", "readings": {"by 2030": "by 1 January 2030"},
        "criteria": ["Says yes or no", "Gives the count's source"],
        "options": ["build", "wait"], "roles": {"lead": {"effort": "max"}},
        "as_of": "2030-01-01"}))

    def no_call(prompt: str) -> str:
        raise AssertionError("no model call with a saved draft")

    def no_ask(prompt: str) -> str:
        raise AssertionError("nothing is asked with an answers file")

    code, task, _ = run_new(tmp_path, ["--draft", str(saved), "--answers", str(answers)],
                            no_ask, no_call)
    assert code == 0 and task is not None
    assert task["criteria"] == ["Says yes or no", "Gives the count's source"]
    assert task["options"] == ["build", "wait"] and task["as_of"] == "2030-01-01"
    assert task["roles"]["lead"]["effort"] == "max"
    assert '"by 2030" is read as: by 1 January 2030' in task["intake"]["Ambiguous terms"]
    again = tmp_path / "again"
    again.mkdir()
    run_new(again, ["--draft", str(saved), "--answers", str(answers)], no_ask, no_call)
    assert (again / "task.json").read_bytes() == (tmp_path / "task.json").read_bytes()


@pytest.mark.parametrize("bad", [
    {"scope": "everything"}, {"readings": {"not a term": "both"}}, {},
    {"readings": {"by 2030": "by 2031"}}, {"criteria": []}, {"options": ["one"]},
    {"roles": {"intern": {"effort": "max"}}}, {"roles": {"lead": {"model": "gpt"}}},
    {"surprise": 1}, {"scope": "broader_decision", "broader_decision": ""}])
def test_a_bad_answers_file_is_refused_by_name(tmp_path: Path, bad: dict[str, Any]) -> None:
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(bad))
    code, task, out = run_new(tmp_path, ["--answers", str(answers)])
    assert code == 2 and task is None and out[-1].startswith("stemma new: ")


@pytest.mark.parametrize("change", [
    {"question_type": "opinion"}, {"criteria": ["only one"]},
    {"criteria": ["1", "2", "3", "4", "5", "6"]},
    {"ambiguous_terms": [{"term": "x", "readings": ["one"], "both_cheap": True,
                          "changes_whole_research": False, "why_it_matters": "y"}]},
    {"ambiguous_terms": [{"term": "x", "readings": ["a", "b"], "both_cheap": "yes",
                          "changes_whole_research": False, "why_it_matters": "y"}]},
    {"ambiguous_terms": [{"term": "x", "readings": ["a", "b"], "both_cheap": True,
                          "changes_whole_research": False, "why_it_matters": "y",
                          "pick": "c"}]}])
def test_a_draft_out_of_shape_fails_loudly(change: dict[str, Any]) -> None:
    with pytest.raises(OnboardError):
        parse_draft({**REPLY, **change})


def test_a_reply_that_is_not_json_fails_loudly(tmp_path: Path) -> None:
    code, task, out = run_new(tmp_path, [], call=lambda p: "I think the answer is yes.")
    assert code == 2 and task is None and "holds no JSON object" in out[-1]


def test_the_prompt_carries_the_question_and_files_and_no_threshold_licence(
        tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("Parks office memo: 212 benches counted in March.")
    call = fake_call()
    run_new(tmp_path, ["--file", str(note)], scripted(*[""] * 9), call)
    prompt = call.seen[0]
    assert QUESTION in prompt and "212 benches counted in March" in prompt
    assert "Add no threshold" in prompt and "Do not research or answer" in prompt
    task = json.loads((tmp_path / "task.json").read_text())
    assert task["attachments"] == [str(note.resolve())]


def test_an_existing_task_is_never_overwritten_silently(tmp_path: Path) -> None:
    (tmp_path / "task.json").write_text("{}")
    code, _task, out = run_new(tmp_path, [])
    assert code == 2 and "exists; pass --force" in out[-1]
    assert (tmp_path / "task.json").read_text() == "{}"


def test_the_model_call_needs_the_pinned_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEMMA_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("STEMMA_CLAUDE_CONFIG", raising=False)
    with pytest.raises(OnboardError, match="STEMMA_CLAUDE_BIN and STEMMA_CLAUDE_CONFIG"):
        draft_mod.clean_model_call("hello")


@pytest.mark.skipif(os.environ.get("STEMMA_LIVE") != "1", reason="live: set STEMMA_LIVE=1")
def test_live_one_clean_call_drafts_a_valid_onboarding() -> None:
    """Live: one real model call through the clean launch, its canary checked."""
    d = draft_mod.draft(QUESTION, [])
    assert d.question_type in draft_mod.QUESTION_TYPES
    assert 3 <= len(d.criteria) <= 5
