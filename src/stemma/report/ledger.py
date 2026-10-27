"""The claim register, read without writing: from the log's bytes, in memory.

`read_log` reads `register/log.jsonl` through `safefs` (confined, bounded, read-only)
and refuses an unterminated tail: a log must end with a newline, because the register's
own reader silently leaves trailing bytes unread. `snapshot` folds the transactions with
World Model's own fold, in memory, and returns a `Register` over that world. The
register's read methods then give the same claim records, verdicts and gate the run saw,
and nothing opens the run's log for writing or takes its lock.

STOP-GAP: `_memory_world` and `_read_only_register` reach two private attributes of the
register's classes (`WorldModel._log`, `Register.__init__` bypassed). Replace both with
W2's public read-only constructor once it exists, so that the viewer and verify read
the register through the register's own code.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from stemma.report import safefs
from stemma.report.errors import RunReadError

LOG: Final = "register/log.jsonl"
# Resource limit: bytes of the register's log read.
LOG_MAX_BYTES: Final = 512 * 1024 * 1024


@dataclass(frozen=True)
class Log:
    data: bytes
    lines: list[bytes]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def read_log(run_dir: Path) -> Log:
    data = safefs.read_bytes(run_dir, LOG, LOG_MAX_BYTES)
    if data and not data.endswith(b"\n"):
        tail = len(data) - (data.rfind(b"\n") + 1)
        raise RunReadError(f"{LOG} ends with {tail:,} bytes after its last newline (an "
                           f"unterminated tail)")
    return Log(data, data.splitlines(keepends=True))


def _memory_world(lines: list[bytes]) -> Any:
    """STOP-GAP (see the module's note): World Model's fold over these lines, in memory."""
    from stemma.register.wm import WorldModel, WorldModelError
    from stemma.register.wm.model import MEMORY_AS_FILE_ENV

    if os.environ.get(MEMORY_AS_FILE_ENV):
        raise RunReadError(f"{MEMORY_AS_FILE_ENV} is set; it would turn the in-memory "
                           f"reading into a file")
    wm = WorldModel({"storage": "memory", "namespace": "register"})
    wm._log._lines = list(lines)  # type: ignore[attr-defined]
    try:
        wm.refresh()
    except (WorldModelError, RecursionError) as exc:
        raise RunReadError(f"{LOG} does not fold: {exc}") from exc
    return wm


def _read_only_register(run_dir: Path, wm: Any) -> Any:
    """STOP-GAP (see the module's note): a Register over an in-memory world; only its
    read methods are called."""
    from stemma.register.store import Register

    reg = Register.__new__(Register)
    reg.run_dir = run_dir
    reg.dir = run_dir / "register"
    reg.wm = wm
    reg._evidence = None
    reg._rerun = None  # type: ignore[assignment]
    reg._relied = []
    reg._lock = reg.dir / "register"
    return reg


@dataclass
class Snapshot:
    log: Log
    world: Any
    register: Any

    @property
    def transactions(self) -> int:
        return len(self.log.lines)


def snapshot(run_dir: Path) -> Snapshot:
    log = read_log(run_dir)
    wm = _memory_world(log.lines)
    return Snapshot(log, wm, _read_only_register(run_dir, wm))
