"""In an as_of run the guard refuses every live web tool to every caller (CONTRACTS
section 0d), through the real hook entry point; a run.json that cannot say whether the
run is as of a date refuses them too. Hostile cases try to get round it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_guards import call, events, pre

AS_OF = "2026-10-19"
CALLERS: list[dict[str, Any]] = [
    {"agent_type": "stemma:lead"},
    {"agent_type": "stemma:worker", "agent_id": "a1"},  # a worker under the lead
    {"agent_type": "stemma:worker"},
    {"agent_type": "stemma:claim-checker"},
    {"agent_type": "stemma:blind-framer"},
    {"agent_type": "stemma:branch-reviewer"},
]
WEB = {"WebSearch": {"query": "Anthropic S-1 revenue"},
       "WebFetch": {"url": "https://www.sec.gov/", "prompt": "summarise"}}


def _run(tmp_path: Path, run_json: str | None) -> Path:
    d = tmp_path / "run"
    (d / "register").mkdir(parents=True)
    if run_json is not None:
        (d / "run.json").write_text(run_json)
    return d


@pytest.mark.parametrize("tool", sorted(WEB))
@pytest.mark.parametrize("who", CALLERS, ids=lambda w: f"{w['agent_type']}{w.get('agent_id', '')}")
def test_every_caller_is_refused_the_live_web_in_an_as_of_run(
        tmp_path: Path, tool: str, who: dict[str, Any]) -> None:
    run = _run(tmp_path, json.dumps({"run_id": "r", "as_of": AS_OF}))
    who = dict(who)
    r = call(run, "pre", pre(run, tool, WEB[tool], agent_type=who.pop("agent_type"), **who))
    assert r.returncode == 2
    assert f"this run is as of {AS_OF}; {tool} reaches the live web" in r.stderr
    assert events(run)[-1]["event"] == "denied" and events(run)[-1]["tool"] == tool


def test_a_live_run_leaves_websearch_to_the_worker(tmp_path: Path) -> None:
    run = _run(tmp_path, json.dumps({"run_id": "r", "as_of": None}))
    r = call(run, "pre", pre(run, "WebSearch", WEB["WebSearch"], agent_type="stemma:worker",
                             agent_id="a1"))
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("run_json", [
    None,  # missing
    "{not json",
    "[]",
    json.dumps({"as_of": "19 October 2026"}),
    json.dumps({"as_of": 20261019}),
    json.dumps({"as_of": ["2026-10-19"]}),
    json.dumps({"as_of": "2026-10-19T00:00:00"}),
])
def test_a_run_json_that_cannot_say_refuses_the_live_web(tmp_path: Path,
                                                         run_json: str | None) -> None:
    # Fail closed: an unreadable answer to "is this run as of a date?" is not "no".
    run = _run(tmp_path, run_json)
    r = call(run, "pre", pre(run, "WebSearch", WEB["WebSearch"], agent_type="stemma:worker",
                             agent_id="a1"))
    assert r.returncode == 2 and "WebSearch is not available" in r.stderr


def test_arguments_cannot_turn_the_refusal_off(tmp_path: Path) -> None:
    # Hostile: a date filter, an allowed-domains list, or an as_of in the tool input
    # changes nothing; only run.json (driver-written, tool-only) says what the run is.
    run = _run(tmp_path, json.dumps({"run_id": "r", "as_of": AS_OF}))
    for ti in ({"query": "x before:2026-10-19"},
               {"query": "x", "allowed_domains": ["web.archive.org"]},
               {"query": "x", "as_of": None}):
        r = call(run, "pre", pre(run, "WebSearch", ti, agent_type="stemma:worker",
                                 agent_id="a1", as_of=None))
        assert r.returncode == 2, ti


def test_other_tools_are_not_touched_by_the_as_of_guard(tmp_path: Path) -> None:
    run = _run(tmp_path, json.dumps({"run_id": "r", "as_of": AS_OF}))
    r = call(run, "pre", pre(run, "Bash", {"command": "ls"}, agent_type="stemma:worker",
                             agent_id="a1"))
    assert r.returncode == 0, r.stderr
