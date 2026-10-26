"""Every session reads only its own run (CONTRACTS v0.2.8): the Read guard and Bash's
sandbox settings. The live proof is tests/e2e/run_read_scope.py (RESULT.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stemma.driver.settings import ToolSets, build_settings, read_scope
from stemma.hooks import guards

REPO = Path(__file__).resolve().parents[2]
HOME = Path.home()
TARGETS = {
    "checker answer keys": REPO / "tests/checkers/answer_keys.json",
    "design record": HOME / ".private/stemma-design/design-2026-10/DESIGN-v1.md",
    "home file": HOME / "notes.txt",
}


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    r = tmp_path / "runs"
    (r / "this/store").mkdir(parents=True)
    (r / "other/store").mkdir(parents=True)
    return r


def read_hook(path: str, run_dir: Path, role: str = "stemma:claim-checker") -> dict[str, object]:
    return {"tool_name": "Read", "tool_input": {"file_path": path}, "agent_type": role,
            "session_id": "s", "tool_use_id": "t", "cwd": str(run_dir)}


@pytest.mark.parametrize("role", ["stemma:claim-checker", "stemma:lead", "stemma:worker",
                                  "stemma:branch-reviewer", "stemma:evidence-auditor"])
@pytest.mark.parametrize("what", [*TARGETS, "another run's store", "relative escape", "tilde"])
def test_read_outside_the_run_is_refused(runs: Path, role: str, what: str) -> None:
    run = runs / "this"
    path = {"another run's store": str(runs / "other/store/s.txt"),
            "relative escape": "../other/store/s.txt",
            "tilde": "~/.private/stemma-design/design-2026-10/DESIGN-v1.md"}.get(what) \
        or str(TARGETS[what])
    d = guards.guard_read(read_hook(path, run, role), run, None)
    assert not d.allow and "outside the run folder" in d.refusal


def test_read_inside_the_run_and_its_plugin_allowed(runs: Path) -> None:
    run = runs / "this"
    for rel in ("store/text/a.txt", ".stemma/plugin/skills/method/SKILL.md", "analysis/x.py"):
        assert guards.guard_read(read_hook(str(run / rel), run), run, None).allow, rel


def test_bash_sandbox_denies_by_default_from_root(runs: Path, tmp_path: Path) -> None:
    run = runs / "this"
    py = REPO / ".venv/bin/python"
    s = build_settings(run, "claim-checker", "claim:abc", ToolSets(("Bash",), ("Bash",)),
                       tmp_path / "cfg", scope=read_scope(py), session_id="s-1")
    fs = s["sandbox"]["filesystem"]
    assert "/" in fs["denyRead"]
    allow = fs["allowRead"]
    # Re-opened: this run, this session's temp folder, the shell's system folders, and the
    # interpreter's own folders; never the home folder, the repository root or its tests,
    # /private/tmp, /Volumes or /Users as such.
    assert str(run) in allow and str(run / ".stemma-tmp/s-1") in allow
    assert str(run / ".stemma-tmp") in fs["denyRead"]
    assert str((REPO / ".venv").resolve()) in allow or str(REPO / ".venv") in allow
    assert str((REPO / "src").resolve()) in allow
    for never in (str(HOME), str(REPO), str(REPO / "tests"), "/private/tmp", "/Volumes",
                  "/Users", str(runs)):
        assert never not in allow, never
    assert not any(a.startswith(str(REPO / "tests")) for a in allow)
    # The blind role's own, more specific denials stay (probe p16: specific deny wins).
    assert any(d.endswith("/this/frame.md") for d in fs["denyRead"])


def test_deciders_eye_keeps_its_narrow_reads_under_the_scope(runs: Path, tmp_path: Path) -> None:
    run = runs / "this"
    s = build_settings(run, "deciders-eye-reviewer", "draft:1", ToolSets((), ()),
                       tmp_path / "cfg", scope=read_scope(REPO / ".venv/bin/python"),
                       session_id="s-2")
    fs = s["sandbox"]["filesystem"]
    assert str(run) in fs["denyRead"] and str(run) not in fs["allowRead"]
    assert f"{run}/register/drafts/answer.v1.md" in fs["allowRead"]


def test_another_sessions_temp_output_is_unreadable(runs: Path) -> None:
    run = runs / "this"
    for path, ok in ((run / ".stemma-tmp/s-1/claude-501/out.txt", True),
                     (run / ".stemma-tmp/s-other/claude-501/out.txt", False),
                     (run / ".stemma-tmp", False)):
        hook = read_hook(str(path), run, "stemma:claim-checker")
        hook["session_id"] = "s-1"
        assert guards.guard_read(hook, run, None).allow is ok, path


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit"])
@pytest.mark.parametrize("path", ["jump/../secret.txt", "analysis/../frame.md",
                                  "./jump/../../outside.txt"])
def test_dotdot_paths_refused_even_inside(tmp_path: Path, tool: str, path: str) -> None:
    run = tmp_path / "run"
    (run / "analysis").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (run / "jump").symlink_to(outside)
    hook = {"tool_name": tool, "tool_input": {"file_path": str(run / path)},
            "agent_type": "stemma:worker", "session_id": "s", "tool_use_id": "t",
            "cwd": str(run)}
    d = guards.guard_read(hook, run, None) if tool == "Read" else guards.guard_write(hook, run)
    assert not d.allow and "outside the run folder" in d.refusal


def test_symlink_out_is_outside(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (tmp_path / "outside").mkdir()
    (run / "jump").symlink_to(tmp_path / "outside")
    hook = {"tool_name": "Read", "tool_input": {"file_path": str(run / "jump/x.txt")},
            "agent_type": "stemma:worker", "session_id": "s", "tool_use_id": "t",
            "cwd": str(run)}
    assert not guards.guard_read(hook, run, None).allow
