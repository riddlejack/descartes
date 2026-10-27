"""run.py routes the product commands (verify, report, tamper, new) and fails loudly on a
folder that is not a run."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def run_py() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stemma_run_py", REPO / "run.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stemma_run_py"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("argv", [["verify", "{d}"], ["report", "{d}", "{d}/out"],
                                  ["tamper", "{d}"]])
def test_a_folder_that_is_not_a_run_is_refused(tmp_path: Path, argv: list[str],
                                                capsys: pytest.CaptureFixture[str]) -> None:
    code = run_py().main([a.format(d=tmp_path) for a in argv])
    assert code == 2
    assert "has no run.json" in capsys.readouterr().err


def test_new_refuses_to_overwrite_a_task(tmp_path: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "task.json").write_text("{}")
    code = run_py().main(["new", "Any question?", "--out", str(tmp_path / "task.json")])
    assert code == 2 and "exists; pass --force" in capsys.readouterr().out
