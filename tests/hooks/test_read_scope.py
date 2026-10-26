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


def test_bash_sandbox_denies_home_repo_and_other_runs(runs: Path, tmp_path: Path) -> None:
    run = runs / "this"
    py = REPO / ".venv/bin/python"
    scope = read_scope(py, runs, REPO, REPO / "plugin", tmp_path / "cache")
    s = build_settings(run, "claim-checker", "claim:abc", ToolSets(("Bash",), ("Bash",)),
                       tmp_path / "cfg", scope=scope)
    fs = s["sandbox"]["filesystem"]
    for denied in (HOME, runs, REPO, REPO / "plugin", tmp_path / "cache"):
        assert str(denied.resolve()) in fs["denyRead"], denied
    # Re-opened: this run, and only what the interpreter needs (venv, base install,
    # the stemma package source); never the repository root or its tests.
    assert str(run.resolve()) in fs["allowRead"]
    assert str((REPO / ".venv").resolve()) in fs["allowRead"]
    assert str((REPO / "src").resolve()) in fs["allowRead"]
    assert str(REPO.resolve()) not in fs["allowRead"]
    assert not any(a.startswith(str((REPO / "tests").resolve())) for a in fs["allowRead"])
    # The blind role's own, more specific denials stay (probe p16: specific deny wins).
    assert any(d.endswith("/this/frame.md") for d in fs["denyRead"])


def test_deciders_eye_keeps_its_narrow_reads_under_the_scope(runs: Path, tmp_path: Path) -> None:
    run = runs / "this"
    scope = read_scope(REPO / ".venv/bin/python", runs, REPO, REPO / "plugin", None)
    s = build_settings(run, "deciders-eye-reviewer", "draft:1", ToolSets((), ()),
                       tmp_path / "cfg", scope=scope)
    fs = s["sandbox"]["filesystem"]
    assert str(run) in fs["denyRead"] and str(run.resolve()) not in fs["allowRead"]
    assert f"{run}/register/drafts/answer.v1.md" in fs["allowRead"]
