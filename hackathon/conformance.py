"""Run World Model's conformance suite live: the upstream runner, unmodified, against the
register's engine (`conformance/run.sh`, part B's). Its output is returned line by line,
labelled live, with the exit code; nothing is summarised or re-judged here."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Resource limit: seconds for the suite, including a first `npm ci` of its pinned runner.
TIMEOUT_S = 600


RECORD_NAME = "conformance.json"


@dataclass(frozen=True)
class Conformance:
    exit_code: int
    lines: list[str]
    at: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def labelled(self, label: str = "live") -> list[tuple[str, str]]:
        """Lines under `label`: `live` only for a run this process made just now."""
        shown = [line for line in self.lines if line.strip()]
        verdict = "PASSED" if self.passed else f"FAILED (exit {self.exit_code})"
        return [(label, line.strip()) for line in shown] + [(label, f"suite {verdict}")]


    def save(self, path: Path) -> None:
        """Keep the result, so a log written later can show it (as recorded, not live)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"suite": "World Model conformance", "at": self.at,
                                    "exit_code": self.exit_code, "lines": self.lines},
                                   indent=1), encoding="utf-8")


def load(path: Path) -> Conformance | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Conformance(int(data["exit_code"]), [str(x) for x in data["lines"]],
                           str(data["at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def latest(runs_dir: Path) -> tuple[Conformance, Path] | None:
    """The newest saved result under `runs_dir/conformance/`."""
    found = sorted((runs_dir / "conformance").glob("*.json"))
    for path in reversed(found):
        result = load(path)
        if result is not None:
            return result, path
    return None


def run_conformance() -> Conformance:
    env = dict(os.environ)
    env.setdefault("STEMMA_PYTHON", sys.executable)
    try:
        proc = subprocess.run(["sh", str(REPO / "conformance" / "run.sh")], env=env,
                              capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except FileNotFoundError as exc:
        return Conformance(127, [f"could not start the suite: {exc}"], _now())
    except subprocess.TimeoutExpired:
        return Conformance(124, [f"the suite ran past its {TIMEOUT_S} s limit"], _now())
    lines = proc.stdout.splitlines()
    if proc.returncode != 0:
        lines += [f"stderr: {line}" for line in proc.stderr.splitlines()[-20:]]
    return Conformance(proc.returncode, lines, _now())


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def save_run(result: Conformance, runs_dir: Path) -> Path:
    stamp = result.at.replace(":", "").replace("-", "")
    path = runs_dir / "conformance" / f"{stamp}.json"
    result.save(path)
    return path
