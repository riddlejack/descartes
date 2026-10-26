"""Concurrent writers to one store, released together by a barrier.

Several processes capture the same attachment (same bytes) and different ones at
the same instant. Afterwards: one bytes file per distinct content, every receipt
present and readable, each document indexed exactly once, and every quote checks.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from pathlib import Path
from typing import Any

from stemma.evidence import api
from stemma.evidence.service import EvidenceService

WORKERS = 6


def _worker(run_dir: str, name: str, barrier: Any, out: Any) -> None:
    svc = EvidenceService(run_dir, cache_dir=None, throttle=False)
    barrier.wait()
    receipt = svc.capture_file(f"task/attachments/{name}", "race")
    out.put((name, receipt["receipt_id"], receipt["sha256"], receipt["status"]))


def test_barrier_synchronised_writers(run_dir: Path) -> None:
    attachments = run_dir / "task" / "attachments"
    shared = "Shared memo. Margin was 7.25% in the invented year.\n" * 30
    (attachments / "same.txt").write_text(shared)
    for i in range(3):
        (attachments / f"own{i}.txt").write_text(f"Own memo {i}. Figure {i}.75 units.\n" * 30)
    EvidenceService(run_dir, cache_dir=None)  # create the store first
    names = ["same.txt"] * (WORKERS - 3) + [f"own{i}.txt" for i in range(3)]
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(len(names))
    out = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(str(run_dir), n, barrier, out)) for n in names]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    results = [out.get(timeout=5) for _ in names]
    assert {status for *_, status in results} == {"ok"}
    shas = {sha for _, _, sha, _ in results}
    assert len(shas) == 4
    assert sorted(p.name for p in (run_dir / "store" / "bytes").iterdir()) == sorted(shas)
    db = sqlite3.connect(run_dir / "store" / "index.sqlite")
    assert db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 4
    assert db.execute("SELECT COUNT(*) FROM fts_exact").fetchone()[0] == 4
    assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == len(names)
    for name, receipt_id, _sha, _ in results:
        quote = "Margin was 7.25%" if name == "same.txt" else f"Figure {name[3]}.75 units."
        assert api.check_quote(run_dir, receipt_id, quote)["found"]
    svc = EvidenceService(run_dir, cache_dir=None)
    hits = svc.search_store("7.25%", mode="exact", limit=50)["hits"]
    assert len({h["sha256"] for h in hits}) == 1  # indexed once, not once per writer
