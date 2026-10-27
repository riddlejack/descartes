"""safeio's first-time folder creation under racing writers: a writer whose mkdir finds
the folder already made carries on, and a link swapped in at the name is refused."""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path
from typing import Any

import pytest

from stemma.register import safeio

WRITERS = 6  # processes racing; enough to collide on every new folder
DEEP = "a/b/c/d"


def _write(run: str, barrier: Any, n: int, out: Any) -> None:
    barrier.wait()
    try:
        safeio.write_text(Path(run), f"{DEEP}/w{n}.txt", f"writer {n}\n")
        safeio.write_text(Path(run), f"{DEEP}/shared.txt", "same bytes\n")
        out.put((n, "ok"))
    except BaseException as exc:  # noqa: BLE001 - reported to the test
        out.put((n, f"{type(exc).__name__}: {exc}"))


def _swap_in_link(run: str, barrier: Any, outside: str, out: Any) -> None:
    barrier.wait()
    for _ in range(2000):  # resource limit: attempts to win the race
        try:
            os.symlink(outside, os.path.join(run, "a"))
            out.put((-1, "linked"))
            return
        except FileExistsError:
            out.put((-1, "a writer made it first"))
            return
    out.put((-1, "never linked"))


def _race(run: Path, outside: Path | None) -> dict[int, str]:
    ctx = mp.get_context("spawn")
    parties = WRITERS + (1 if outside else 0)
    barrier = ctx.Barrier(parties)
    out = ctx.Queue()
    procs = [ctx.Process(target=_write, args=(str(run), barrier, n, out))
             for n in range(WRITERS)]
    if outside:
        procs.append(ctx.Process(target=_swap_in_link,
                                 args=(str(run), barrier, str(outside), out)))
    for p in procs:
        p.start()
    results = dict(out.get(timeout=60) for _ in procs)  # resource limit: seconds
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    return results


def test_racing_writers_all_make_the_same_new_nested_folders(tmp_path: Path) -> None:
    for _ in range(3):  # a fresh run each time, so every folder is new to every writer
        run = tmp_path / f"run{_}"
        run.mkdir()
        results = _race(run, None)
        assert results == {n: "ok" for n in range(WRITERS)}
        folder = run / DEEP
        assert sorted(p.name for p in folder.iterdir()) == sorted(
            [f"w{n}.txt" for n in range(WRITERS)] + ["shared.txt"])
        assert all((folder / f"w{n}.txt").read_text() == f"writer {n}\n"
                   for n in range(WRITERS))


def test_a_link_swapped_in_during_the_race_is_refused(tmp_path: Path) -> None:
    for i in range(3):
        run = tmp_path / f"run{i}"
        run.mkdir()
        outside = tmp_path / f"outside{i}"
        outside.mkdir()
        results = _race(run, outside)
        assert list(outside.iterdir()) == []  # nothing ever written through the link
        writers = [results[n] for n in range(WRITERS)]
        assert all(r == "ok" or r.startswith("UnsafePath") for r in writers), writers
        if (run / "a").is_symlink():
            assert all(r.startswith("UnsafePath") for r in writers), writers
        else:
            assert all(r == "ok" for r in writers), writers


def test_mkdir_losing_to_a_real_folder_carries_on(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    real_mkdir = os.mkdir

    def lose(name: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        real_mkdir(name, mode, dir_fd=dir_fd)  # the other writer, a moment earlier
        raise FileExistsError(17, "File exists", name)

    monkeypatch.setattr(os, "mkdir", lose)
    safeio.write_text(tmp_path, "x/y/f.txt", "hi")
    assert (tmp_path / "x" / "y" / "f.txt").read_text() == "hi"


@pytest.mark.parametrize("planted", ["link", "file"])
def test_mkdir_losing_to_a_link_or_a_file_is_refused(tmp_path: Path, planted: str,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    run = tmp_path / "run"
    run.mkdir()

    real_mkdir = os.mkdir

    def lose(name: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if name != "x":
            return real_mkdir(name, mode, dir_fd=dir_fd)
        if planted == "link":
            os.symlink(outside, name, dir_fd=dir_fd)
        else:
            os.close(os.open(name, os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=dir_fd))
        raise FileExistsError(17, "File exists", name)

    monkeypatch.setattr(os, "mkdir", lose)
    with pytest.raises(safeio.UnsafePath, match="x is not a plain folder"):
        safeio.write_text(run, "x/f.txt", "hi")
    assert list(outside.iterdir()) == []


def test_exists_nofollow_is_kept(tmp_path: Path) -> None:
    safeio.write_text(tmp_path, "d/f.txt", "x")
    assert safeio.exists_nofollow(tmp_path, "d/f.txt")
    assert not safeio.exists_nofollow(tmp_path, "d/g.txt")
