"""The guards, run through their real entry point, each with attempts to get round it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from stemma.register import checks_api, jsonl

PY = sys.executable


def call(run_dir: Path, event: str, hook: dict[str, Any] | str,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    e = {"PATH": os.environ["PATH"], "STEMMA_RUN_DIR": str(run_dir)}
    if env is not None:
        e = env
    stdin = hook if isinstance(hook, str) else json.dumps(hook)
    return subprocess.run([PY, "-I", "-m", "stemma.hooks", event], check=False, input=stdin,
                          capture_output=True, text=True, env=e, timeout=60)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run"
    for sub in ("register", "store", "analysis", "branches/b1/sub/b2", "framer", "draft", "task"):
        (d / sub).mkdir(parents=True)
    (d / "branches/b1/record.md").write_text("record")
    (d / "branches/b1/order.md").write_text("order")
    (d / "frame.md").write_text("frame")
    (d / "intake.md").write_text("intake")
    (d / "draft/answer.v1.md").write_text("v1")
    (d / "draft/answer.v2.md").write_text("v2")
    return d


def pre(run_dir: Path, tool: str, tool_input: dict[str, Any], agent_type: str = "stemma:lead",
        **extra: Any) -> dict[str, Any]:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
            "agent_type": agent_type, "session_id": "s-1", "tool_use_id": "toolu_1",
            "cwd": str(run_dir), **extra}


def events(run_dir: Path) -> list[dict[str, Any]]:
    return jsonl.read_all(run_dir / "events.jsonl")


# --- Agent calls -------------------------------------------------------------------


def test_worker_subagent_allowed(run_dir: Path) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Agent", {"subagent_type": "stemma:worker",
                                                    "prompt": "go"}))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_only_the_idea_generator_writes_the_idea_list_and_only_that(run_dir: Path) -> None:
    def write(rel: str, who: str, **extra: Any) -> subprocess.CompletedProcess[str]:
        return call(run_dir, "pre", pre(run_dir, "Write", {
            "file_path": str(run_dir / rel), "content": "x"}, agent_type=who, **extra))
    assert write("framer/ideas.md", "stemma:idea-generator").returncode == 0
    for other in ("frame.md", "intake.md", "framer/lead-response.md", "branches/b1/x.md",
                  "framer/../frame.md"):
        r = write(other, "stemma:idea-generator")
        assert r.returncode == 2 and "Refused" in r.stderr, other
    for who, extra in (("stemma:lead", {}), ("stemma:worker", {"agent_id": "a-1"}),
                       ("stemma:lead", {"agent_id": "a-2"}), ("stemma:blind-framer", {})):
        r = write("framer/ideas.md", who, **extra)
        assert r.returncode == 2 and "only by the idea generator" in r.stderr, who
    for tool_only in ("framer/critique.md", "framer/own-frame.md"):
        r = write(tool_only, "stemma:idea-generator")
        assert r.returncode == 2 and "written only by Stemma's tools" in r.stderr


@pytest.mark.parametrize("rel", ["task/question.md", "task/intake.md", "task/task.json"])
def test_the_idea_generator_reads_the_task(run_dir: Path, rel: str) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": str(run_dir / rel)},
                                 agent_type="stemma:idea-generator"))
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("rel", ["frame.md", "intake.md", "criteria.md", "framer/own-frame.md",
                                 "task/options.md", "task/arrivals/1/x.md", "branches/b1/record.md",
                                 "task/../frame.md"])
def test_the_idea_generator_reads_nothing_the_run_made(run_dir: Path, rel: str) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": str(run_dir / rel)},
                                 agent_type="stemma:idea-generator"))
    assert r.returncode == 2 and r.stderr.startswith("Refused"), rel


def test_the_blind_frame_cannot_read_the_idea_list(run_dir: Path) -> None:
    from stemma.driver import settings as st
    assert "framer/ideas.md" in st.read_denials(run_dir, "blind-framer", "frame:own")
    assert "framer/ideas.md" not in st.read_denials(run_dir, "blind-framer", "frame:critique")


@pytest.mark.parametrize("sub", [None, "fork", "general-purpose", "Explore", "Plan",
                                 "stemma:claim-checker", "stemma:branch-reviewer",
                                 "stemma:blind-framer", "Stemma:worker", "stemma:worker ",
                                 "stemma:lead", "worker", "idea-generator", "stemma:idea-generator",
                                 "Stemma:idea-generator", "stemma:idea-generator "])
def test_hostile_subagent_types_refused(run_dir: Path, sub: str | None) -> None:
    ti: dict[str, Any] = {"prompt": "go"}
    if sub is not None:
        ti["subagent_type"] = sub
    r = call(run_dir, "pre", pre(run_dir, "Agent", ti))
    assert r.returncode == 2
    assert r.stderr.startswith("Refused: subagent type")
    assert events(run_dir)[-1]["event"] == "denied"


def test_task_alias_guarded_too(run_dir: Path) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Task", {"subagent_type": "fork", "prompt": "x"}))
    assert r.returncode == 2


def test_model_and_effort_arguments_stripped(run_dir: Path) -> None:
    ti = {"subagent_type": "stemma:worker", "prompt": "go", "model": "haiku", "effort": "max"}
    r = call(run_dir, "pre", pre(run_dir, "Agent", ti))
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["updatedInput"] == {"subagent_type": "stemma:worker", "prompt": "go"}
    assert "permissionDecision" not in out  # a rewrite never grants a permission
    assert events(run_dir)[-1]["stripped"] == ["model", "effort"]


# --- Writes to tool-only paths ------------------------------------------------------


@pytest.mark.parametrize("target", [
    "register/log.jsonl", "Register/log.jsonl", "./analysis/../register/x", "store/bytes/abc",
    "tree.json", "TREE.JSON", "events.jsonl", "run.json", "status.json", "status.html",
    "task/question.md", "CLAUDE.md", "claude.md", ".claude/settings.local.json",
    ".stemma/plugin/agents/lead.md", "branches/b1/verdicts/1.json",
    "branches/b1/sub/b2/verdicts/3.json", "deliverable/answer.reader.md",
    "deliverable/answer.md", "tree.md", "tree.json.tmp", "reviews/v1/deciders-eye.md",
    "Reviews/V1/mechanical.md", "framer/own-frame.md", "framer/critique.md",
    "Framer/Critique.md", "events.jsonl.lock",
])
@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
def test_hostile_tool_only_writes_refused(run_dir: Path, tool: str, target: str) -> None:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    r = call(run_dir, "pre", pre(run_dir, tool, {key: target, "content": "x"}))
    assert r.returncode == 2, target
    expect = "outside the run folder" if ".." in target else "written only by Stemma's tools"
    assert expect in r.stderr


def test_write_through_symlink_refused(run_dir: Path) -> None:
    (run_dir / "analysis" / "lnk").symlink_to(run_dir / "register")
    r = call(run_dir, "pre", pre(run_dir, "Write", {"file_path": str(run_dir / "analysis/lnk/x")}))
    assert r.returncode == 2


def test_absolute_path_into_register_refused(run_dir: Path) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Edit", {"file_path": str(run_dir / "register/x")}))
    assert r.returncode == 2


@pytest.mark.parametrize("target", ["analysis/model.py", "branches/b1/record.md", "frame.md",
                                    "draft/answer.v3.md", "framer/lead-response.md",
                                    "branches/b1/verdicts.md", "registers.md"])
def test_ordinary_writes_allowed(run_dir: Path, target: str) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Write", {"file_path": target, "content": "x"}))
    assert (r.returncode, r.stderr) == (0, "")


def test_post_write_records_the_writer(run_dir: Path) -> None:
    hook = pre(run_dir, "Write", {"file_path": "frame.md"}, agent_type="stemma:worker",
               agent_id="a1")
    hook["hook_event_name"] = "PostToolUse"
    r = call(run_dir, "post", hook)
    assert r.returncode == 0
    ev = events(run_dir)[-1]
    assert (ev["event"], ev["path"], ev["agent_type"], ev["agent_id"]) == (
        "file_write", "frame.md", "stemma:worker", "a1")


# --- Blind roles --------------------------------------------------------------------


@pytest.mark.parametrize("target", ["branches/b1/record.md", "branches/b1/sub/b2/record.md",
                                    "branches/b1/order.md", "./branches/b1/../b1/record.md",
                                    "BRANCHES/b1/RECORD.md", "frame.md", "history/frame.v1.md",
                                    "draft/answer.v1.md", "deliverable/answer.md",
                                    "register/drafts/answer.v1.md", "Register/Log.jsonl",
                                    "framer/own-frame.md"])
def test_claim_checker_hostile_reads_refused(run_dir: Path, target: str) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": target},
                                 agent_type="stemma:claim-checker"))
    assert r.returncode == 2, target
    expect = "outside the run folder" if ".." in target else "the claim checker does not read"
    assert expect in r.stderr


def test_claim_checker_symlinked_record_refused(run_dir: Path) -> None:
    (run_dir / "analysis" / "notes.md").symlink_to(run_dir / "branches/b1/record.md")
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": "analysis/notes.md"},
                                 agent_type="stemma:claim-checker"))
    assert r.returncode == 2


@pytest.mark.parametrize("target", ["store/text/abc.txt", "analysis/model.py"])
def test_claim_checker_reads_its_evidence(run_dir: Path, target: str) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": target},
                                 agent_type="stemma:claim-checker"))
    assert r.returncode == 0


def test_same_read_allowed_for_lead(run_dir: Path) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": "branches/b1/record.md"}))
    assert r.returncode == 0


def test_deciders_eye_reads_only_task_intake_and_its_draft(run_dir: Path) -> None:
    req = checks_api.request_check(run_dir, "deciders-eye-reviewer", "draft:2", "submit_draft")
    checks_api.mark_launched(run_dir, req.request_id, "s-1", "claude-opus-5-5", "max")
    who = "stemma:deciders-eye-reviewer"
    for ok in ("task/question.md", "intake.md", "register/drafts/answer.v2.md"):
        assert call(run_dir, "pre", pre(run_dir, "Read", {"file_path": ok},
                                        agent_type=who)).returncode == 0, ok
    for bad in ("draft/answer.v1.md", "draft/answer.v2.md", "frame.md", "branches/b1/record.md",
                "register/x", "register/drafts/answer.v1.md", "store/text/a.txt", "/etc/hosts",
                "DRAFT/answer.v1.md"):
        assert call(run_dir, "pre", pre(run_dir, "Read", {"file_path": bad},
                                        agent_type=who)).returncode == 2, bad


def test_deciders_eye_unknown_session_gets_no_draft(run_dir: Path) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": "register/drafts/answer.v2.md"},
                                 agent_type="stemma:deciders-eye-reviewer"))
    assert r.returncode == 2


def test_blind_framer_blind_until_own_frame_exists(run_dir: Path) -> None:
    who = "stemma:blind-framer"
    for bad in ("frame.md", "tree.json", "branches/b1/order.md", "Frame.md"):
        assert call(run_dir, "pre", pre(run_dir, "Read", {"file_path": bad},
                                        agent_type=who)).returncode == 2, bad
    assert call(run_dir, "pre", pre(run_dir, "Read", {"file_path": "task/q.md"},
                                    agent_type=who)).returncode == 0
    (run_dir / "framer/own-frame.md").write_text("mine")
    assert call(run_dir, "pre", pre(run_dir, "Read", {"file_path": "frame.md"},
                                    agent_type=who)).returncode == 0


# --- Identity stamps ----------------------------------------------------------------


def test_mcp_call_stamped_with_harness_identity(run_dir: Path) -> None:
    hook = pre(run_dir, "mcp__plugin_stemma_stemma__file_claim",
               {"statement": "x", "agent_type": "stemma:claim-checker"},
               agent_type="stemma:worker", agent_id="a7", tool_use_id="toolu_abc")
    r = call(run_dir, "pre", hook)
    assert r.returncode == 0 and r.stdout == ""
    stamp = json.loads((run_dir / "register/calls/toolu_abc.json").read_text())
    assert stamp["agent_type"] == "stemma:worker"  # the harness's, not the argument's
    assert stamp["agent_id"] == "a7"


@pytest.mark.parametrize("tid", ["../escape", "a/b", ".hidden", ""])
def test_hostile_tool_use_ids_refused(run_dir: Path, tid: str) -> None:
    hook = pre(run_dir, "mcp__plugin_stemma_stemma__file_claim", {}, tool_use_id=tid)
    assert call(run_dir, "pre", hook).returncode == 2
    assert not (run_dir / "escape.json").exists()


def test_mcp_call_without_agent_type_refused(run_dir: Path) -> None:
    hook = pre(run_dir, "mcp__plugin_stemma_stemma__file_claim", {})
    del hook["agent_type"]
    assert call(run_dir, "pre", hook).returncode == 2


# --- Failing closed -----------------------------------------------------------------


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", "null"])
def test_garbage_input_blocks(run_dir: Path, stdin: str) -> None:
    r = call(run_dir, "pre", stdin)
    assert r.returncode == 2 and r.stdout == ""
    assert r.stderr == f"Refused: a Stemma guard failed ({'JSONDecodeError' if stdin in ('', 'not json') else 'GuardInputError'})."


def test_missing_run_dir_blocks(run_dir: Path) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Write", {"file_path": "frame.md"}),
             env={"PATH": os.environ["PATH"]})
    assert r.returncode == 2


def test_unknown_event_blocks(run_dir: Path) -> None:
    assert call(run_dir, "nonsense", pre(run_dir, "Read", {"file_path": "x"})).returncode == 2


def test_unwritable_events_log_blocks(run_dir: Path) -> None:
    (run_dir / "events.jsonl").mkdir()  # appending to a directory fails
    r = call(run_dir, "pre", pre(run_dir, "Agent", {"subagent_type": "fork"}))
    assert r.returncode == 2


def test_refusals_name_only_what_failed(run_dir: Path) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Write", {"file_path": "register/x"}))
    assert r.stderr == "Refused: register/x is written only by Stemma's tools."
