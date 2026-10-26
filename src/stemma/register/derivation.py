"""Re-running a recorded calculation script for `file_claim` (CONTRACTS section 5).

The script runs in a subprocess, never in the MCP server's own process, under macOS
`sandbox-exec` with a profile that denies all network access and every file write
outside one temporary folder, with a scrubbed environment and a recorded time limit.
It reads its inputs through `stemma.analysis.inputs()` (a file the register writes)
and reports through `stemma.analysis.result()` (a file it writes back).
"""

from __future__ import annotations

import functools
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Resource limit: seconds a re-run may take before it is stopped (recorded on each claim).
DEFAULT_TIME_LIMIT_S = 120
# Resource limit: bytes of the result file read (one number in JSON needs far fewer).
RESULT_MAX_BYTES = 4096
# Resource limit: bytes of the script's stderr kept for the refusal record.
STDERR_TAIL_BYTES = 2000


class DerivationError(Exception):
    """The script could not be re-run, or reported no result."""


@dataclass(frozen=True)
class RerunResult:
    number: float | None
    exit_code: int
    stderr_tail: str
    time_limit_s: int


# What any process needs to start on macOS (dyld, system libraries, devices). Logical
# invariant of the platform, not tuning.
_SYSTEM_READS = ("/usr/lib", "/usr/share", "/System", "/Library/Apple", "/dev",
                 "/private/var/db/timezone", "/private/var/db/dyld", "/private/etc/localtime")


@functools.lru_cache(maxsize=8)
def runtime_reads(python: str) -> tuple[str, ...]:
    """The folders the interpreter itself reads: its prefix and base install, the real
    executable's tree, and every sys.path entry (stdlib, site-packages, stemma's source).
    Asked of the interpreter itself, once."""
    code = ("import os, sys, json; print(json.dumps([sys.prefix, sys.base_prefix, "
            "os.path.dirname(os.path.dirname(os.path.realpath(sys.executable)))] "
            "+ [p for p in sys.path if p]))")
    out = subprocess.run([python, "-I", "-c", code], capture_output=True, text=True,
                         timeout=60, check=True,  # resource limit: seconds to ask it
                         env={"PATH": "/usr/bin:/bin"}).stdout
    paths: list[str] = []
    for p in json.loads(out):
        for q in (p, os.path.realpath(p)):
            if q not in paths and os.path.exists(q):
                paths.append(q)
    return tuple(paths)


def _q(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _profile(tmp: Path, reads: tuple[str, ...], python: str) -> str:
    """Seatbelt: no network; writes only in `tmp`; file contents readable only in `tmp`,
    the interpreter's own folders and the system's start-up libraries. Each ancestor
    folder of those is readable as a single entry (path resolution lists it), never its
    subtree. Rules are deny-all then allow, because later rules win. So a script reads its
    copy, its inputs file and the Python runtime, nothing else."""
    t = str(tmp.resolve())
    roots = (t, *reads, *_SYSTEM_READS)
    ancestors: set[str] = set()
    for r in (*roots, os.path.abspath(python)):
        ancestors.update(str(a) for a in Path(r).parents)
        ancestors.add(r)
    allow = " ".join(f"(subpath {_q(r)})" for r in roots)
    literal = " ".join(f"(literal {_q(a)})" for a in sorted(ancestors))
    return (
        "(version 1)\n(allow default)\n(deny network*)\n"
        f"(deny file-write* (require-not (require-any (subpath {_q(t)}) "
        '(literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr") '
        '(literal "/dev/dtracehelper"))))\n'
        "(deny file-read-data)\n"
        f"(allow file-read-data {allow} {literal})\n"
    )


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _read_capped(path: Path, cap: int, what: str, tail: bool = False) -> bytes:
    """A regular file's bytes, never following a link, never blocking, at most `cap`."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise DerivationError(f"the {what} is not a plain file") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise DerivationError(f"the {what} is not a plain file")
        if st.st_size > cap and not tail:
            raise DerivationError(f"the {what} is larger than {cap} bytes")
        if tail and st.st_size > cap:
            os.lseek(fd, st.st_size - cap, os.SEEK_SET)
        return os.read(fd, cap)
    finally:
        os.close(fd)


def _copy_code(script: Path, tmp: Path) -> Path:
    """Copy the script and the other Python files beside it (its helper modules), never
    following a link, into `tmp`; return the script's copy. Data files are not copied:
    a script reads figures through inputs() and documents through document()."""
    folder = script.parent
    for entry in os.scandir(folder):
        if not entry.name.endswith(".py") or entry.is_symlink() or not entry.is_file():
            continue
        fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                continue
            data = b""
            while chunk := os.read(fd, 1 << 20):
                data += chunk
        finally:
            os.close(fd)
        (tmp / entry.name).write_bytes(data)
    copy = tmp / script.name
    if not copy.is_file():
        raise DerivationError(f"script {script.name} is not a plain file")
    return copy


def rerun(script: Path, values: dict[str, float], documents: dict[str, str] | None = None,
          python: str | None = None,
          time_limit_s: int = DEFAULT_TIME_LIMIT_S) -> RerunResult:
    """Run `script` with `values` as its inputs and return the number it reported."""
    if not script.is_file():
        raise DerivationError(f"script {script.name} does not exist")
    if not Path(SANDBOX_EXEC).exists():
        raise DerivationError("sandbox-exec is not available on this machine")
    py = os.path.abspath(python or os.environ.get("STEMMA_PYTHON") or sys.executable)
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
        # The script and its helper modules run from copies inside the temporary folder,
        # with the documents it named, so nothing else (the run folder included) is
        # readable to it.
        copy = _copy_code(script, tmp)
        docs = tmp / "documents"
        docs.mkdir()
        for rid, text in (documents or {}).items():
            if "/" in rid or rid.startswith("."):
                raise DerivationError(f"receipt id {rid!r} is not a plain name")
            (docs / f"{rid}.txt").write_text(text, encoding="utf-8")
        env["STEMMA_ANALYSIS_DOCUMENTS"] = str(docs)
        argv = [SANDBOX_EXEC, "-p", _profile(tmp, runtime_reads(py), py), py, "-I", str(copy)]
        stderr_path = tmp / ".stderr"
        with open(stderr_path, "wb") as err:
            # Own process group, output to a file, not a pipe: a child the script leaves
            # behind can hold nothing open that the register waits on, and is killed.
            proc = subprocess.Popen(argv, cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=err,
                                    start_new_session=True)
            try:
                code = proc.wait(timeout=time_limit_s)
            except subprocess.TimeoutExpired as exc:
                _kill_group(proc.pid)
                proc.wait()
                raise DerivationError(f"the script ran past its {time_limit_s} s limit") from exc
            finally:
                _kill_group(proc.pid)
        stderr_tail = _read_capped(stderr_path, STDERR_TAIL_BYTES, "script's error output",
                                   tail=True).decode(
            "utf-8", "replace")
        number: float | None = None
        if code == 0 and os.path.lexists(result_file):
            raw = _read_capped(result_file, RESULT_MAX_BYTES, "result file")
            try:
                reported = json.loads(raw).get("number")
            except (ValueError, AttributeError) as exc:
                raise DerivationError("the result file is not a JSON object") from exc
            if isinstance(reported, int | float) and not isinstance(reported, bool):
                number = float(reported)
        return RerunResult(number, code, stderr_tail, time_limit_s)
