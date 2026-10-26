"""Worker for the barrier-synchronised race test (run in spawned processes).

Not a test module: pytest does not collect it; the race test imports it by name.
"""

from __future__ import annotations

from typing import Any

from stemma.register.wm import WorldModel

AT = "2026-03-01T00:00:00.000Z"


def run(path: str, worker: int, writes: int, start: Any, done: Any, results: Any) -> None:
    w = WorldModel({"storage": "file", "path": path})
    start.wait()  # every process is released at once
    for i in range(writes):
        key = f"w{worker}-{i}"
        source = {"kind": "agent", "at": AT, "by": f"worker-{worker}"}
        w.upsertEntity(
            {"type": "item", "externalKey": {"system": "race", "id": key}, "attrs": {"i": i},
             "source": source}
        )
        # Single-valued edge from one shared hub: each write must close the edge
        # that is open at that moment, which it can only see if it folded every
        # other process's appends before validating.
        w.relate({"from": "hub:race:0", "type": "holds", "to": f"item:race:{key}",
                  "source": source})
    done.wait()  # everyone has finished writing
    w.refresh()
    results.put((worker, w.stateShapeCanonical(), [a["seq"] for a in w.audit()]))
