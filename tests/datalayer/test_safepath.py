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


# Resource limits of the race test below: processes released together by the barrier,
# and fresh run folders, one per round (the race needs a log that does not exist yet).
_RACERS = 8
_ROUNDS = 40


def _append_in_lockstep(roots: list[str], barrier: object) -> None:
    try:
        for root in roots:
            barrier.wait()  # type: ignore[attr-defined]
            append_line(Path(root), "datalayer/queries.jsonl", '{"probe": 1}')
    except BaseException:
        barrier.abort()  # type: ignore[attr-defined]  # release the others: no hang
        raise


def test_concurrent_first_appends_all_land(tmp_path: Path) -> None:
    # W3's flaky sync (27 Oct): on macOS, openat(dir_fd, name, O_CREAT) answers ENOENT
    # when several processes create the same new file at once. Every racer must land
    # its line in every round, with no error.
    import multiprocessing as mp

    roots = []
    for i in range(_ROUNDS):
        (tmp_path / f"run{i}").mkdir()
        roots.append(str(tmp_path / f"run{i}"))
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(_RACERS)
    procs = [ctx.Process(target=_append_in_lockstep, args=(roots, barrier))
             for _ in range(_RACERS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert [p.exitcode for p in procs] == [0] * _RACERS
    assert [len((Path(r) / "datalayer/queries.jsonl").read_text().splitlines())
            for r in roots] == [_RACERS] * _ROUNDS


def test_a_removed_folder_fails_loudly_after_the_retries(tmp_path: Path) -> None:
    # The retry above must not hide a real loss: a folder that is gone stays an error.
    from stemma.datalayer.safepath import AppendFailed, _open_append

    (tmp_path / "datalayer").mkdir()
    fd = os.open(tmp_path / "datalayer", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.rmdir(tmp_path / "datalayer")
        with pytest.raises(AppendFailed, match="'no such file'"):
            _open_append(fd, "queries.jsonl", "datalayer/queries.jsonl")
    finally:
        os.close(fd)
