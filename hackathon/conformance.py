"""Run World Model's conformance suite live: the upstream runner, unmodified, against the
register's engine (`conformance/run.sh`, part B's). Its output is returned line by line,
labelled live, with the exit code; nothing is summarised or re-judged here."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Resource limit: seconds for the suite, including a first `npm ci` of its pinned runner.
TIMEOUT_S = 600


@dataclass(frozen=True)
class Conformance:
    exit_code: int
    lines: list[str]

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def labelled(self) -> list[tuple[str, str]]:
        shown = [line for line in self.lines if line.strip()]
        verdict = "PASSED" if self.passed else f"FAILED (exit {self.exit_code})"
        return [("live", line.strip()) for line in shown] + [("live", f"suite {verdict}")]


def run_conformance() -> Conformance:
    env = dict(os.environ)
    env.setdefault("STEMMA_PYTHON", sys.executable)
    try:
        proc = subprocess.run(["sh", str(REPO / "conformance" / "run.sh")], env=env,
                              capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except FileNotFoundError as exc:
        return Conformance(127, [f"could not start the suite: {exc}"])
    except subprocess.TimeoutExpired:
        return Conformance(124, [f"the suite ran past its {TIMEOUT_S} s limit"])
    lines = proc.stdout.splitlines()
    if proc.returncode != 0:
        lines += [f"stderr: {line}" for line in proc.stderr.splitlines()[-20:]]
    return Conformance(proc.returncode, lines)
