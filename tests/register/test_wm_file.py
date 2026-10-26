"""File storage: persistence, several processes on one log, torn tails, corruption."""

from __future__ import annotations

import itertools
import json
import multiprocessing as mp
from pathlib import Path
from typing import Any

import pytest
import wm_race_worker

from stemma.register.wm import LogCorruptError, StorageError, WorldModel

SRC = {"kind": "manual", "at": "2026-01-01T00:00:00.000Z"}


def open_world(path: Path) -> WorldModel:
    return WorldModel({"storage": "file", "path": str(path)})


def test_file_world_persists_and_replays(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    w = open_world(log)
    w.upsertEntity({"type": "x", "name": "Persisted", "attrs": {"a": 1}, "source": SRC})
    w.upsertEntity({"type": "x", "name": "Persisted Too", "source": SRC})
    lines = log.read_text().splitlines()
    assert [json.loads(line)["tx"] for line in lines] == [1, 2]
    again = open_world(log)
    assert again.stateShapeCanonical() == w.stateShapeCanonical()
    assert again.audit() == w.audit()
    assert (tmp_path / "log.jsonl.lock").exists()


def test_reader_sees_another_writers_appends(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    reader, writer = open_world(log), open_world(log)
    writer.upsertEntity({"type": "x", "name": "From Writer", "source": SRC})
    assert len(reader.view("graph").query({"type": "x"})) == 1  # reads refresh themselves


def test_writer_validates_against_other_processes_writes(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    a, b = open_world(log), open_world(log)
    for w, key in ((a, "1"), (b, "2")):
        w.upsertEntity({"type": "t", "externalKey": {"system": "s", "id": key}, "source": SRC})
    a.upsertEntity({"type": "hub", "externalKey": {"system": "s", "id": "h"}, "source": SRC})
    a.relate({"from": "hub:s:h", "type": "holds", "to": "t:s:1", "source": SRC})
    # b has not read a's relate yet; its relate must still close it
    b.relate({"from": "hub:s:h", "type": "holds", "to": "t:s:2", "source": SRC})
    open_edges = [
        r for r in open_world(log).stateShape()["relationships"] if "validTo" not in r
    ]
    assert [r["to"] for r in open_edges] == ["t:s:2"]


def test_missing_folder_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="does not exist"):
        open_world(tmp_path / "nope" / "log.jsonl")


def test_torn_tail_is_ignored_by_readers_and_discarded_by_the_next_writer(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    w = open_world(log)
    w.upsertEntity({"type": "x", "name": "Before Crash", "source": SRC})
    with open(log, "ab") as f:
        f.write(b'{"tx": 2, "at": "2026')  # a crashed writer's partial line
    reader = open_world(log)
    assert len(reader.audit()) == 1
    reader.upsertEntity({"type": "x", "name": "After Crash", "source": SRC})
    assert [a["seq"] for a in open_world(log).audit()] == [1, 2]
    assert "torn append" in open_world(log).audit()[-1]["summary"]
    assert list(tmp_path.glob("log.jsonl.torn-*"))


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b'{"tx": 5, "at": "x", "op": "o", "actor": "a", "summary": "s", "events": []}\n',
        b"[1, 2]\n",
    ],
)
def test_corrupt_committed_line_fails_loudly(tmp_path: Path, line: bytes) -> None:
    log = tmp_path / "log.jsonl"
    open_world(log).upsertEntity({"type": "x", "name": "Ok", "source": SRC})
    with open(log, "ab") as f:
        f.write(line)
    with pytest.raises(LogCorruptError):
        open_world(log)


def test_duplicated_line_is_detected(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    open_world(log).upsertEntity({"type": "x", "name": "Ok", "source": SRC})
    log.write_bytes(log.read_bytes() * 2)
    with pytest.raises(LogCorruptError, match="duplicate"):
        open_world(log)


# ------------------------------------------------------------------ the race
WORKERS = 4  # resource limit for the test: processes
WRITES = 40  # resource limit for the test: write pairs per process


def test_barrier_synchronised_writers_lose_and_duplicate_nothing(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    setup = open_world(log)
    setup.upsertEntity({"type": "hub", "externalKey": {"system": "race", "id": "0"}, "source": SRC})

    ctx = mp.get_context("spawn")
    start, done = ctx.Barrier(WORKERS), ctx.Barrier(WORKERS)
    results: Any = ctx.Queue()
    procs = [
        ctx.Process(target=wm_race_worker.run, args=(str(log), n, WRITES, start, done, results))
        for n in range(WORKERS)
    ]
    for p in procs:
        p.start()
    outcomes = [results.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    # Raw log: every transaction once, numbered without gaps; events likewise.
    txs = [json.loads(line) for line in log.read_text().splitlines()]
    expected_tx = 1 + WORKERS * WRITES * 2
    assert [t["tx"] for t in txs] == list(range(1, expected_tx + 1))
    seqs = [e["seq"] for t in txs for e in t["events"]]
    assert seqs == list(range(1, len(seqs) + 1))
    assert len({e["id"] for t in txs for e in t["events"]}) == len(seqs)

    # The writes really interleaved (otherwise this would not be a race).
    actors = [t["actor"] for t in txs[1:]]
    switches = sum(1 for x, y in itertools.pairwise(actors) if x != y)
    assert switches > WORKERS

    # Nothing lost: every process's every entity is there.
    final = open_world(log)
    items = {e["id"] for e in final.view("graph").query({"type": "item"})}
    assert items == {f"item:race:w{n}-{i}" for n in range(WORKERS) for i in range(WRITES)}

    # Each relate saw every earlier append: one open edge, and the closed ones
    # form a single chain through all of them.
    rels = final.stateShape()["relationships"]
    assert len(rels) == WORKERS * WRITES
    open_edges = [r for r in rels if "validTo" not in r]
    assert len(open_edges) == 1
    successors = [r["supersededBy"] for r in rels if "supersededBy" in r]
    assert len(successors) == len(set(successors)) == WORKERS * WRITES - 1

    # Every process folds the same state, audit seq strictly increasing.
    shapes = {shape for _, shape, _ in outcomes}
    assert shapes == {final.stateShapeCanonical()}
    for _, _, audit_seqs in outcomes:
        assert audit_seqs == list(range(1, expected_tx + 1))
