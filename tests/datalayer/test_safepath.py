"""Hostile tests: a planted symlink never redirects a host write in datalayer/."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from stemma.datalayer import Settings, query_log, sync
from stemma.datalayer.safepath import UnsafePath, append_line, check_sqlite_file

from .conftest import FakeRegister


def test_linked_folder_is_refused(run_dir: Path, tmp_path: Path, register: FakeRegister) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, run_dir / "datalayer")
    with pytest.raises(UnsafePath):
        sync(run_dir, Settings.from_env({}), register)
    assert list(outside.iterdir()) == []


def test_linked_database_file_is_refused(run_dir: Path, tmp_path: Path) -> None:
    (run_dir / "datalayer").mkdir()
    target = tmp_path / "CLAUDE.md"
    target.write_text("rules")
    os.symlink(target, run_dir / "datalayer" / "evidence.sqlite")
    with pytest.raises(UnsafePath):
        check_sqlite_file(run_dir, "datalayer/evidence.sqlite")
    os.symlink(target, run_dir / "datalayer" / "evidence.sqlite-journal")
    with pytest.raises(UnsafePath):
        check_sqlite_file(run_dir, "datalayer/evidence.sqlite")
    assert target.read_text() == "rules"


def test_linked_log_file_is_refused(run_dir: Path, tmp_path: Path) -> None:
    (run_dir / "datalayer").mkdir()
    target = tmp_path / "events.jsonl"
    target.write_text("")
    os.symlink(target, run_dir / "datalayer" / "queries.jsonl")
    with pytest.raises(UnsafePath):
        query_log(run_dir).write(engine="x", purpose="probe")
    assert target.read_text() == ""


@pytest.mark.parametrize("rel", ["../escape.jsonl", "/abs.jsonl", "datalayer/../../x", ""])
def test_paths_outside_the_run_are_refused(run_dir: Path, rel: str) -> None:
    with pytest.raises(UnsafePath):
        append_line(run_dir, rel, "{}")
