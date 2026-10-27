"""The driver's no-follow opens under concurrent first use (27 Oct).

Two defects in `hostio.open_nofollow`, each with its own race below: two processes that
both find a folder missing both make it, and the slower one's mkdir raised
FileExistsError; and on macOS, openat(dir_fd, name, O_CREAT) answers ENOENT while other
processes create the same new file (the race W3 found in datalayer/, fixed in 13e192f).
Sessions launched together create their streams in a shared new `sessions/` folder, and
drivers started together create the same lock file.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import pytest

from stemma.driver import hostio

# Resource limits of the races below: processes released together by the barrier, and
# fresh run folders, one per round (each race needs folders or a file not yet made).
_RACERS = 8
_ROUNDS = 40


def _append_in_lockstep(roots: list[str], barrier: object, rel: str) -> None:
    try:
        for root in roots:
            barrier.wait()  # type: ignore[attr-defined]
            with hostio.open_append(Path(root), rel.format(pid=os.getpid())) as f:
                f.write(b'{"probe": 1}\n')
    except BaseException:
        barrier.abort()  # type: ignore[attr-defined]  # release the others: no hang
        raise


def _race(tmp_path: Path, rel: str) -> list[str]:
    roots = []
    for i in range(_ROUNDS):
        (tmp_path / f"run{i}").mkdir()
        roots.append(str(tmp_path / f"run{i}"))
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(_RACERS)
    procs = [ctx.Process(target=_append_in_lockstep, args=(roots, barrier, rel))
             for _ in range(_RACERS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert [p.exitcode for p in procs] == [0] * _RACERS
    return roots


def test_racers_making_the_same_new_folders_all_get_their_files(tmp_path: Path) -> None:
    # Each racer its own file, all in folders that do not exist yet.
    roots = _race(tmp_path, "sessions/w1/{pid}.stream.jsonl")
    assert [len(list((Path(r) / "sessions/w1").iterdir())) for r in roots] == (
        [_RACERS] * _ROUNDS)


def test_racers_creating_the_same_new_file_all_land(tmp_path: Path) -> None:
    roots = _race(tmp_path, "sessions/lead.stderr")
    assert [len((Path(r) / "sessions/lead.stderr").read_bytes().splitlines())
            for r in roots] == [_RACERS] * _ROUNDS


def test_a_removed_folder_fails_loudly_after_the_retries(tmp_path: Path) -> None:
    # The create retry must not hide a real loss: a folder that is gone stays an error.
    (tmp_path / "sessions").mkdir()
    fd = os.open(tmp_path / "sessions", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.rmdir(tmp_path / "sessions")
        with pytest.raises(hostio.CreateFailed, match="'no such file'"):
            hostio._create(fd, "lead.stderr", os.O_WRONLY | os.O_APPEND, "sessions/lead.stderr")
    finally:
        os.close(fd)


def test_links_are_still_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    os.symlink(outside, run / "sessions")
    with pytest.raises(hostio.UnsafePath):
        hostio.open_append(run, "sessions/lead.stderr")
    (run / "logs").mkdir()
    os.symlink(outside / "x", run / "logs" / "lead.stderr")
    with pytest.raises(hostio.UnsafePath):
        hostio.open_append(run, "logs/lead.stderr")
    assert list(outside.iterdir()) == []
