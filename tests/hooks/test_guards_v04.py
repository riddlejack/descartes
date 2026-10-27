"""v0.4 guards: the lead reads no sources (D6); the user's criteria are not rewritten (D4).
Each through the real hook entry point, with attempts to get round it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_guards import call, events, pre

from stemma.register import roles

SOURCE_TOOLS = ["capture", "capture_file", "sec_filings", "sec_capture", "sec_facts", "outline",
                "read", "read_section", "search_store", "tables", "check_quote", "query_data"]


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run"
    for sub in ("register", "store/text", "task/attachments", "task/arrivals/1", "branches/b1",
                "framer"):
        (d / sub).mkdir(parents=True)
    (d / "store/text/abc.txt").write_text("a stored source")
    (d / "task/attachments/brief.pdf").write_text("the user's file")
    (d / "task/arrivals/1/late.csv").write_text("a,b")
    (d / "task/question.md").write_text("q")
    (d / "branches/b1/record.md").write_text("record")
    (d / "run.json").write_text('{"as_of": null}')  # the driver writes it before any session
    return d


@pytest.mark.parametrize("tool", [roles.MCP_PREFIX + t for t in SOURCE_TOOLS]
                         + ["WebSearch", "WebFetch", "Bash"])
def test_the_leads_own_thread_cannot_call_a_source_tool(run_dir: Path, tool: str) -> None:
    ti = {"command": "cat store/text/abc.txt"} if tool == "Bash" else {"sha256": "abc"}
    r = call(run_dir, "pre", pre(run_dir, tool, ti))
    assert r.returncode == 2 and "the lead does not call" in r.stderr
    assert events(run_dir)[-1]["event"] == "denied"
    # No identity stamp was written, so the MCP server would refuse the call as well.
    assert not (run_dir / "register/calls").exists()


@pytest.mark.parametrize("tool", [roles.MCP_PREFIX + "read", "Bash", "WebSearch"])
def test_a_worker_under_the_lead_keeps_its_source_tools(run_dir: Path, tool: str) -> None:
    ti = {"command": "ls"} if tool == "Bash" else {"sha256": "abc"}
    r = call(run_dir, "pre", pre(run_dir, tool, ti, agent_type="stemma:worker",
                                 agent_id="a1"))
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("agent_type", ["stemma:lead", "stemma:worker"])
def test_a_lead_typed_subagent_is_not_the_leads_own_thread(run_dir: Path,
                                                           agent_type: str) -> None:
    # Identity comes from the harness: a subagent carries an agent_id.
    r = call(run_dir, "pre", pre(run_dir, roles.MCP_PREFIX + "read", {"sha256": "x"},
                                 agent_type=agent_type, agent_id="a9"))
    assert r.returncode == 0


@pytest.mark.parametrize("path", [
    "store/text/abc.txt", "./store/text/abc.txt", "STORE/text/abc.txt", "Store/Text/ABC.txt",
    "task/attachments/brief.pdf", "task/arrivals/1/late.csv", "store", "store/",
])
def test_the_lead_cannot_read_stored_sources(run_dir: Path, path: str) -> None:
    for target in (path, str(run_dir / path)):
        r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": target}))
        assert r.returncode == 2 and "the lead does not read" in r.stderr, (target, r.stderr)


def test_the_lead_cannot_read_a_source_through_a_symlink(run_dir: Path) -> None:
    (run_dir / "branches/b1/notes.txt").symlink_to(run_dir / "store/text/abc.txt")
    (run_dir / "jump").symlink_to(run_dir / "store")
    for target in ("branches/b1/notes.txt", "jump/text/abc.txt"):
        r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": target}))
        assert r.returncode == 2, target


@pytest.mark.parametrize("path", ["branches/b1/record.md", "task/question.md", "frame.md"])
def test_the_lead_reads_records_and_its_own_files(run_dir: Path, path: str) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": path}))
    assert r.returncode == 0, r.stderr


def test_a_worker_may_read_the_store(run_dir: Path) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": "store/text/abc.txt"},
                                 agent_type="stemma:worker", agent_id="a1"))
    assert r.returncode == 0


# --- criteria.md --------------------------------------------------------------------


def _task(run_dir: Path, criteria: list[str]) -> None:
    (run_dir / "task/task.json").write_text(json.dumps({"question": "q", "criteria": criteria}))


@pytest.mark.parametrize("target", ["criteria.md", "CRITERIA.md", "./criteria.md"])
def test_the_users_criteria_cannot_be_rewritten(run_dir: Path, target: str) -> None:
    _task(run_dir, ["Says whether the target is met"])
    for tool in ("Write", "Edit"):
        r = call(run_dir, "pre", pre(run_dir, tool, {"file_path": target, "content": "x"}))
        assert r.returncode == 2 and "user's criteria" in r.stderr


def test_without_criteria_in_the_task_the_lead_writes_criteria_md(run_dir: Path) -> None:
    _task(run_dir, [])
    r = call(run_dir, "pre", pre(run_dir, "Write", {"file_path": "criteria.md", "content": "x"}))
    assert r.returncode == 0, r.stderr


def test_checks_folder_is_tool_only(run_dir: Path) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Write", {"file_path": "checks/doc-r1.json",
                                                    "content": "{}"}))
    assert r.returncode == 2 and "written only by Stemma's tools" in r.stderr


# --- D8: what the decider's-eye reviewer reads of the previous round ----------------

@pytest.mark.parametrize(("version", "path", "allowed"), [
    ("2", "criteria.md", True), ("1", "criteria.md", True),
    ("2", "reviews/v1/deciders-eye.md", True), ("2", "reviews/v1/evidence-audit.md", True),
    ("2", "draft/response.v2.md", True), ("2", "register/drafts/previous.v2.md", True),
    ("2", "reviews/v2/mechanical.md", False), ("3", "reviews/v1/deciders-eye.md", False),
    ("2", "draft/response.v3.md", False), ("2", "draft/answer.v2.md", False),
    ("2", "register/drafts/previous.v3.md", False), ("1", "reviews/v0/x.md", False),
    ("1", "draft/response.v1.md", False), ("2", "frame.md", False),
    ("2", "reviews/v1/../v2/mechanical.md", False), ("2", "REVIEWS/V2/mechanical.md", False),
])
def test_deciders_eye_reads_the_previous_round_only(tmp_path: Path, version: str, path: str,
                                                    allowed: bool) -> None:
    from stemma.hooks import guards

    rd = tmp_path / "run"
    rd.mkdir()
    hook = {"tool_name": "Read", "tool_input": {"file_path": str(rd / path)},
            "agent_type": "stemma:deciders-eye-reviewer", "session_id": "s",
            "tool_use_id": "t", "cwd": str(rd)}
    assert guards.guard_read(hook, rd, version).allow is allowed


@pytest.mark.parametrize("role", ["stemma:evidence-auditor", "stemma:mechanical-reader"])
def test_the_other_final_reviewers_read_the_previous_round(tmp_path: Path, role: str) -> None:
    from stemma.hooks import guards

    rd = tmp_path / "run"
    rd.mkdir()
    for rel in ("criteria.md", "reviews/v1/deciders-eye.md", "draft/response.v2.md",
                "register/drafts/previous.v2.md"):
        hook = {"tool_name": "Read", "tool_input": {"file_path": str(rd / rel)},
                "agent_type": role, "session_id": "s", "tool_use_id": "t", "cwd": str(rd)}
        assert guards.guard_read(hook, rd, None).allow, rel


def test_only_the_lead_writes_criteria_and_not_once_frozen(run_dir: Path) -> None:
    _task(run_dir, [])
    r = call(run_dir, "pre", pre(run_dir, "Write", {"file_path": "criteria.md", "content": "x"},
                                 agent_type="stemma:worker", agent_id="a1"))
    assert r.returncode == 2 and "only the lead writes" in r.stderr
    (run_dir / "register/criteria").mkdir(parents=True)
    r = call(run_dir, "pre", pre(run_dir, "Edit", {"file_path": "criteria.md", "old_string": "a",
                                                   "new_string": "b"}))
    assert r.returncode == 2 and "frozen" in r.stderr


# --- Codex review #7: the lead reads approved artifacts only -------------------------

@pytest.mark.parametrize("path", [
    "branches/b1/source-copy.txt", "branches/b1/notes.md", "analysis/copy.txt",
    "checks/doc-r1.r1.json", "events.jsonl", "register/log.jsonl", "usage.md",
    "framer/sub/x.md", "task/attachments/brief.pdf", "odd.md", ".stemma/sessions/x.jsonl",
])
def test_the_lead_reads_nothing_beyond_the_approved_artifacts(run_dir: Path, path: str) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": path}))
    assert r.returncode == 2 and "the lead does not read" in r.stderr, path


@pytest.mark.parametrize("path", [
    "intake.md", "frame.md", "criteria.md", "tree.json", "framer/critique.md",
    "branches/b1/order.md", "branches/b1/sub/b2/record.md", "branches/b1/revision-1.md",
    "branches/b1/verdicts/1.json", "draft/answer.v2.md", "draft/response.v2.md",
    "reviews/v1/deciders-eye.md", "register/drafts/previous.v2.md", "analysis/sum.py",
    "task/question.md", "Frame.MD",
])
def test_the_lead_reads_its_approved_artifacts(run_dir: Path, path: str) -> None:
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": path}))
    assert r.returncode == 0, (path, r.stderr)


def test_a_link_from_an_approved_name_to_a_copy_is_refused(run_dir: Path) -> None:
    (run_dir / "branches/b1/source-copy.txt").write_text("copied source text")
    (run_dir / "branches/b1/order.md").symlink_to(run_dir / "branches/b1/source-copy.txt")
    r = call(run_dir, "pre", pre(run_dir, "Read", {"file_path": "branches/b1/order.md"}))
    assert r.returncode == 2
