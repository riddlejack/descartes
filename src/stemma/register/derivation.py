"""Re-running a recorded calculation script for `file_claim` (CONTRACTS section 5).

The script runs in a subprocess, never in the MCP server's own process, under macOS
`sandbox-exec` with a profile that denies all network access and every file write
outside one temporary folder, with a scrubbed environment and a recorded time limit.
It reads its inputs through `stemma.analysis.inputs()` (a file the register writes)
and reports through `stemma.analysis.result()` (a file it writes back).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Resource limit: seconds a re-run may take before it is stopped (recorded on each claim).
DEFAULT_TIME_LIMIT_S = 120


class DerivationError(Exception):
    """The script could not be re-run, or reported no result."""


@dataclass(frozen=True)
class RerunResult:
    number: float | None
    exit_code: int
    stderr_tail: str
    time_limit_s: int


def _profile(tmp: Path) -> str:
    # Seatbelt profile: default allow, then deny network and writes outside `tmp`.
    t = str(tmp.resolve()).replace('"', "")
    return (
        "(version 1)\n(allow default)\n(deny network*)\n"
        f'(deny file-write* (require-not (require-any (subpath "{t}") '
        '(literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr") '
        '(literal "/dev/dtracehelper"))))\n'
    )


def rerun(script: Path, values: dict[str, float], python: str | None = None,
          time_limit_s: int = DEFAULT_TIME_LIMIT_S) -> RerunResult:
    """Run `script` with `values` as its inputs and return the number it reported."""
    if not script.is_file():
        raise DerivationError(f"script {script.name} does not exist")
    if not Path(SANDBOX_EXEC).exists():
        raise DerivationError("sandbox-exec is not available on this machine")
    py = python or os.environ.get("STEMMA_PYTHON") or sys.executable
    with tempfile.TemporaryDirectory(prefix="stemma-rerun-") as td:
        tmp = Path(td).resolve()
        inputs_file = tmp / "inputs.json"
        result_file = tmp / "result.json"
        inputs_file.write_text(json.dumps(values), encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp),
            "TMPDIR": str(tmp),
            "LANG": "en_US.UTF-8",
            "STEMMA_ANALYSIS_INPUTS": str(inputs_file),
            "STEMMA_ANALYSIS_RESULT": str(result_file),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        argv = [SANDBOX_EXEC, "-p", _profile(tmp), py, "-I", str(script.resolve())]
        try:
            proc = subprocess.run(argv, cwd=tmp, env=env, capture_output=True, text=True,
                                  timeout=time_limit_s, check=False)
        except subprocess.TimeoutExpired as exc:
            raise DerivationError(f"the script ran past its {time_limit_s} s limit") from exc
        number: float | None = None
        if proc.returncode == 0 and result_file.exists():
            reported = json.loads(result_file.read_text(encoding="utf-8")).get("number")
            if isinstance(reported, int | float) and not isinstance(reported, bool):
                number = float(reported)
        return RerunResult(number, proc.returncode, proc.stderr[-2000:], time_limit_s)
