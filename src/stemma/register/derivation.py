"""Re-running a recorded calculation script for `file_claim` (CONTRACTS section 5).

The script runs in a subprocess, never in the MCP server's own process, under macOS
`sandbox-exec` with a profile that denies all network access and every file write
outside one temporary folder, with a scrubbed environment and a recorded time limit.
It reads its inputs through `stemma.analysis.inputs()` (a file the register writes)
and reports through `stemma.analysis.result()` (a file it writes back).

What a re-run copies is the script's *bundle* (`build_bundle`): the script and the
modules under its own folder in `analysis/` that it imports, found by parsing the
code with `ast` (never by running it), transitively. Nothing else in `analysis/` is
read. At filing the bundle is stored in the evidence store under its SHA-256
(`store_bundle`), and a filed claim is re-run from that stored copy (`load_bundle`),
so editing `analysis/` later changes nothing already filed.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Resource limit: seconds a re-run may take before it is stopped (recorded on each claim).
DEFAULT_TIME_LIMIT_S = 120
# Resource limit: bytes of the result file read (one number in JSON needs far fewer).
RESULT_MAX_BYTES = 4096
# Resource limit: bytes of the script's stderr kept for the refusal record.
STDERR_TAIL_BYTES = 2000
# Resource limit: bytes of the script's standard output kept (the first ones).
STDOUT_MAX_BYTES = 64 * 1024
# Resource limit: Python files in one bundle (the script and the modules it imports).
CODE_MAX_FILES = 64
# Resource limit: bytes of those files together.
CODE_MAX_BYTES = 4 * 1024 * 1024
# Resource limit: captured documents one derivation may name.
DOCUMENTS_MAX = 32
# Resource limit: characters of those documents together.
DOCUMENTS_MAX_CHARS = 64 * 1024 * 1024

# Logical invariant: names the canonical serialisation a bundle's SHA-256 is taken over.
BUNDLE_FORMAT = "stemma-bundle/1"
# Logical invariant: where stored bundles live inside the run folder.
BUNDLES_DIR = "store/derivations"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_SHA = re.compile(r"[0-9a-f]{64}")


class DerivationError(Exception):
    """The script could not be bundled or re-run, or reported no result."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Bundle:
    """A script and the modules under its folder it imports, read without following links.

    `files` is keyed by POSIX path relative to the script's folder (the bundle's root);
    `script` is the script's key. Its SHA-256 is taken over `manifest()` serialised as
    canonical JSON (keys sorted, no spaces, UTF-8): the format name, the script's key
    and each file's own SHA-256. `source` says where it was read from; it is not hashed."""

    script: str
    files: dict[str, bytes]
    source: str = field(default="", compare=False)

    def manifest(self) -> dict[str, Any]:
        return {"format": BUNDLE_FORMAT, "script": self.script,
                "files": {p: hashlib.sha256(d).hexdigest()
                          for p, d in sorted(self.files.items())}}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical(self.manifest())).hexdigest()


@dataclass(frozen=True)
class RerunResult:
    number: float | None
    exit_code: int
    stderr_tail: str
    time_limit_s: int
    stdout: str = ""
    stdout_truncated: bool = False
    reported: int | float | None = None  # the result() value exactly as the script wrote it


# ------------------------------------------------------------------ the bundle

class _Reader:
    """Collects a bundle from a folder open as `root_fd`: exact-case names only, no link
    followed, every limit checked as each file is read."""

    def __init__(self, root_fd: int, where: str) -> None:
        self.where = where
        self.dirs: dict[tuple[str, ...], int] = {(): root_fd}  # root_fd: the caller's
        self.names: dict[tuple[str, ...], frozenset[str]] = {}
        self.files: dict[str, bytes] = {}
        self.todo: list[tuple[tuple[str, ...], str]] = []
        self.total = 0

    def close(self) -> None:
        for key, fd in self.dirs.items():
            if key:
                os.close(fd)

    def show(self, d: tuple[str, ...], name: str = "") -> str:
        return "/".join((self.where, *d, name) if name else (self.where, *d))

    def _names(self, d: tuple[str, ...]) -> frozenset[str]:
        if d not in self.names:
            self.names[d] = frozenset(os.listdir(self.dirs[d]))
        return self.names[d]

    def kind(self, d: tuple[str, ...], name: str) -> str | None:
        """"dir", "file", or None when no entry has exactly this name. A link is refused:
        it could reach outside analysis/."""
        if name not in self._names(d):
            return None
        try:
            st = os.stat(name, dir_fd=self.dirs[d], follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode):
            raise DerivationError(f"{self.show(d, name)} is a link; a re-run copies nothing "
                                  f"that could reach outside analysis/")
        if stat.S_ISDIR(st.st_mode):
            return "dir"
        if stat.S_ISREG(st.st_mode):
            return "file"
        return None

    def open_dir(self, d: tuple[str, ...], name: str) -> tuple[str, ...]:
        sub = (*d, name)
        if sub not in self.dirs:
            try:
                self.dirs[sub] = os.open(name, _DIR_FLAGS, dir_fd=self.dirs[d])
            except OSError as exc:
                raise DerivationError(f"{self.show(sub)} is not a plain folder") from exc
        return sub

    def add(self, d: tuple[str, ...], name: str) -> None:
        key = "/".join((*d, name))
        if key in self.files:
            return
        if len(self.files) >= CODE_MAX_FILES:
            raise DerivationError(f"the bundle of {self.where} would hold more than the "
                                  f"{CODE_MAX_FILES} Python files a re-run copies (a "
                                  f"resource limit)")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=self.dirs[d])
        except OSError as exc:
            raise DerivationError(f"{self.show(d, name)} is not a plain file") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise DerivationError(f"{self.show(d, name)} is not a plain file (a link, or "
                                      f"more than one name for one file)")
            if self.total + st.st_size > CODE_MAX_BYTES:
                raise DerivationError(f"the Python files of {self.where}'s bundle are larger "
                                      f"than {CODE_MAX_BYTES} bytes together")
            data = os.read(fd, CODE_MAX_BYTES - self.total + 1)
        finally:
            os.close(fd)
        self.total += len(data)
        if self.total > CODE_MAX_BYTES:
            raise DerivationError(f"the Python files of {self.where}'s bundle are larger "
                                  f"than {CODE_MAX_BYTES} bytes together")
        self.files[key] = data
        self.todo.append((d, name))

    def module(self, names: list[str]) -> str | None:
        """Add the files importing the dotted `names` from the root loads, as Python's
        path finder would find them there. Returns "package", "module", or None when the
        first name is not under the root (the standard library, stemma, site-packages)."""
        d: tuple[str, ...] = ()
        for i, n in enumerate(names):
            if not n.isidentifier():
                return None
            is_dir = self.kind(d, n) == "dir"
            if is_dir:
                sub = self.open_dir(d, n)
                if self.kind(sub, "__init__.py") == "file":
                    self.add(sub, "__init__.py")
                    d = sub
                    continue
            if self.kind(d, n + ".py") == "file":
                self.add(d, n + ".py")
                return "module"  # any further names are attributes of the module
            if is_dir:
                d = (*d, n)  # a namespace package
                continue
            return None if i == 0 else "module"
        return "package"

    def imports(self, d: tuple[str, ...], name: str) -> None:
        key = "/".join((*d, name))
        try:
            tree = ast.parse(self.files[key], filename=key)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            raise DerivationError(f"{self.show(d, name)} is not Python that parses "
                                  f"({type(exc).__name__}: {exc})") from exc
        package = d  # the dotted package of a module in folder d (the root's is empty)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.module(alias.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(package):
                        dots = "." * node.level
                        raise DerivationError(
                            f"{self.show(d, name)}: 'from {dots}{node.module or ''} import' "
                            f"climbs above the script's folder in analysis/")
                    base = list(package[:len(package) - node.level + 1])
                else:
                    base = []
                target = base + (node.module.split(".") if node.module else [])
                found = self.module(target) if target else "package"
                if found == "package":
                    for alias in node.names:
                        if alias.name != "*":
                            self.module([*target, alias.name])

    def collect(self, script: str) -> Bundle:
        if self.kind((), script) != "file":
            raise DerivationError(f"script {self.show((), script)} does not exist or is "
                                  f"not a plain file")
        self.add((), script)
        while self.todo:
            self.imports(*self.todo.pop())
        return Bundle(script, dict(sorted(self.files.items())), self.where)


def _bundle_from(root_fd: int, script: str, where: str) -> Bundle:
    reader = _Reader(root_fd, where)
    try:
        return reader.collect(script)
    finally:
        reader.close()


def _script_parts(script_rel: str) -> list[str]:
    parts = script_rel.split("/") if isinstance(script_rel, str) else []
    if (len(parts) < 2 or parts[0] != "analysis" or "\x00" in script_rel
            or any(p in ("", ".", "..") for p in parts)):
        raise DerivationError(f"script {script_rel!r} is not a plain path in analysis/")
    if not parts[-1].endswith(".py") or parts[-1] == ".py":
        raise DerivationError(f"script {script_rel} is not a .py file")
    return parts


@contextlib.contextmanager
def _exact_folder(run_dir: Path, parts: list[str], rel: str) -> Iterator[int]:
    """The folder run_dir/parts..., each component an entry of exactly that name (no
    case or Unicode-form alias on a case-insensitive disk), opened without following a
    link."""
    fd = os.open(os.path.realpath(run_dir), _DIR_FLAGS)
    try:
        for name in parts:
            if name not in os.listdir(fd):
                raise DerivationError(f"script {rel} does not exist")
            try:
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise DerivationError(f"the folder of script {rel} is not a plain "
                                      f"folder") from exc
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def build_bundle(run_dir: Path, script_rel: str) -> Bundle:
    """The bundle of run_dir/script_rel, a script under `analysis/`: the script plus every
    module under the script's own folder that it imports statically (`import a.b`,
    `from a import b`, relative imports inside a package), transitively, found by
    parsing, never by running. A name not found under that folder (the standard
    library, `stemma.analysis`, site-packages) is not a member. A link anywhere, a
    relative import climbing above the folder, or a bundle over CODE_MAX_FILES files or
    CODE_MAX_BYTES bytes is a DerivationError naming it."""
    parts = _script_parts(script_rel)
    with _exact_folder(run_dir, parts[:-1], script_rel) as fd:
        return _bundle_from(fd, parts[-1], "/".join(parts[:-1]))


def _bundle_of(script: Path | Bundle) -> Bundle:
    """A Bundle as given, or, for a bare path, the bundle of that file in its folder."""
    if isinstance(script, Bundle):
        return script
    try:
        dfd = os.open(script.parent, _DIR_FLAGS)
    except OSError as exc:
        raise DerivationError(f"script {script.name} does not exist") from exc
    try:
        return _bundle_from(dfd, script.name, script.parent.name)
    finally:
        os.close(dfd)


# ------------------------------------------------------------ the stored copy

def _plain_rel(path: str) -> list[str]:
    parts = path.split("/")
    if not parts or any(p in ("", ".", "..") or "\x00" in p for p in parts):
        raise DerivationError(f"bundle path {path!r} is not a plain relative path")
    return parts


def bundle_dir(sha256: str) -> str:
    """The run-relative folder of a stored bundle."""
    if not _SHA.fullmatch(sha256):
        raise DerivationError(f"{sha256!r} is not a bundle's SHA-256")
    return f"{BUNDLES_DIR}/{sha256}"


def _ensure_folders(run_dir: Path, rels: list[list[str]]) -> None:
    """Make each run_dir/rel folder, following no link, where another writer making the
    same folder at the same moment is not an error (`safeio`'s writes then find them)."""
    for parts in rels:
        fd = os.open(os.path.realpath(run_dir), _DIR_FLAGS)
        try:
            for name in parts:
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)  # a link here: ELOOP, refused
                os.close(fd)
                fd = nxt
        finally:
            os.close(fd)


def store_bundle(run_dir: Path, bundle: Bundle) -> str:
    """Write the bundle once to store/derivations/<sha256>/ (files/<path> for each file,
    then bundle.json, its manifest), following no link (`safeio`). Returns the hash. A
    bundle already stored is read back and checked, never rewritten."""
    from stemma.register import safeio  # the register's safe writes

    sha = bundle.sha256
    folder = bundle_dir(sha)
    try:
        if safeio.is_file(run_dir, f"{folder}/bundle.json"):
            load_bundle(run_dir, sha)
            return sha
        paths = {path: _plain_rel(path) for path in bundle.files}
        _ensure_folders(run_dir, [["register", "tmp"]] + [
            [*folder.split("/"), "files", *parts[:-1]] for parts in paths.values()])
        for path, data in bundle.files.items():
            safeio.write_bytes(run_dir, f"{folder}/files/{path}", data)
        safeio.write_bytes(run_dir, f"{folder}/bundle.json", _canonical(bundle.manifest()))
    except (safeio.UnsafePath, NotADirectoryError, OSError) as exc:
        raise DerivationError(f"bundle {sha} could not be stored: {exc}") from exc
    return sha


def load_bundle(run_dir: Path, sha256: str) -> Bundle:
    """The bundle stored under `sha256`, read without following a link; every file's
    hash and the bundle's own hash checked against what was stored."""
    from stemma.register import safeio

    folder = bundle_dir(sha256)

    def read(rel: str, cap: int) -> bytes:
        try:
            data = safeio.read_bytes(run_dir, rel)
        except FileNotFoundError as exc:
            raise DerivationError(f"stored bundle {sha256} lacks {rel}") from exc
        except (safeio.UnsafePath, NotADirectoryError, OSError) as exc:
            raise DerivationError(f"stored bundle {sha256}: {rel} is not a plain "
                                  f"file") from exc
        if len(data) > cap:
            raise DerivationError(f"stored bundle {sha256}: {rel} is larger than {cap} bytes")
        return data

    try:
        manifest = json.loads(read(f"{folder}/bundle.json", CODE_MAX_BYTES))
    except ValueError as exc:
        raise DerivationError(f"stored bundle {sha256}: bundle.json is not JSON") from exc
    listed = manifest.get("files") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT
            or not isinstance(listed, dict) or not isinstance(manifest.get("script"), str)
            or manifest["script"] not in listed or len(listed) > CODE_MAX_FILES
            or not all(isinstance(v, str) for v in listed.values())):
        raise DerivationError(f"stored bundle {sha256}: bundle.json is not a bundle manifest")
    files: dict[str, bytes] = {}
    total = 0
    for path, want in sorted(listed.items()):
        _plain_rel(path)
        data = read(f"{folder}/files/{path}", CODE_MAX_BYTES - total)
        total += len(data)
        if hashlib.sha256(data).hexdigest() != want:
            raise DerivationError(f"stored bundle {sha256}: {path} does not match its hash")
        files[path] = data
    bundle = Bundle(manifest["script"], files, folder)
    if bundle.sha256 != sha256:
        raise DerivationError(f"stored bundle {sha256} does not match its hash")
    return bundle


# ------------------------------------------------------------------ the re-run

# What any process needs to start on macOS (dyld, system libraries, devices). Logical
# invariant of the platform, not tuning.
_SYSTEM_READS = ("/usr/lib", "/usr/share", "/System", "/Library/Apple", "/dev",
                 "/private/var/db/timezone", "/private/var/db/dyld", "/private/etc/localtime")

# `python -I` puts no script folder on sys.path, so the launcher puts the bundle's root
# there explicitly (first, as running the script by its path would), then runs the
# script as __main__ with its own path as __file__ and argv[0].
_LAUNCHER = ("import runpy, sys\n"
             "script, root = sys.argv[1], sys.argv[2]\n"
             "sys.argv = [script]\n"
             "sys.path.insert(0, root)\n"
             "runpy.run_path(script, run_name='__main__')\n")


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


def _read_capped(path: Path, cap: int, what: str, tail: bool = False,
                 head: bool = False) -> bytes:
    """A regular file's bytes, never following a link, never blocking, at most `cap`
    (the last `cap` with `tail`, the first `cap` with `head`, else refused when longer)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise DerivationError(f"the {what} is not a plain file") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise DerivationError(f"the {what} is not a plain file")
        if st.st_size > cap and not (tail or head):
            raise DerivationError(f"the {what} is larger than {cap} bytes")
        if tail and st.st_size > cap:
            os.lseek(fd, st.st_size - cap, os.SEEK_SET)
        return os.read(fd, cap)
    finally:
        os.close(fd)


def rerun(script: Path | Bundle, values: dict[str, float],
          documents: dict[str, str] | None = None, python: str | None = None,
          time_limit_s: int = DEFAULT_TIME_LIMIT_S) -> RerunResult:
    """Run `script` (a Bundle, or a bare path whose bundle is read from its folder) with
    `values` as its inputs and return the number it reported and its output."""
    bundle = _bundle_of(script)
    documents = documents or {}
    if len(documents) > DOCUMENTS_MAX:
        raise DerivationError(f"{len(documents)} documents named, more than {DOCUMENTS_MAX}")
    if sum(len(t) for t in documents.values()) > DOCUMENTS_MAX_CHARS:
        raise DerivationError(f"the named documents are longer than {DOCUMENTS_MAX_CHARS} "
                              f"characters together")
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
        # The bundle runs from copies inside the temporary folder, with the documents it
        # named, so nothing else (the run folder included) is readable to it.
        code_dir = tmp / "code"
        code_dir.mkdir()
        for name, data in bundle.files.items():
            target = code_dir.joinpath(*_plain_rel(name))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        copy = code_dir.joinpath(*_plain_rel(bundle.script))
        docs = tmp / "documents"
        docs.mkdir()
        for rid, text in documents.items():
            if "/" in rid or rid.startswith("."):
                raise DerivationError(f"receipt id {rid!r} is not a plain name")
            (docs / f"{rid}.txt").write_text(text, encoding="utf-8")
        env["STEMMA_ANALYSIS_DOCUMENTS"] = str(docs)
        argv = [SANDBOX_EXEC, "-p", _profile(tmp, runtime_reads(py), py), py, "-I", "-c",
                _LAUNCHER, str(copy), str(copy.parent)]
        stderr_path = tmp / ".stderr"
        stdout_path = tmp / ".stdout"
        with open(stderr_path, "wb") as err, open(stdout_path, "wb") as out:
            # Own process group, output to files, not pipes: a child the script leaves
            # behind can hold nothing open that the register waits on, and is killed.
            proc = subprocess.Popen(argv, cwd=code_dir, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, start_new_session=True)
            try:
                code = proc.wait(timeout=time_limit_s)
            except subprocess.TimeoutExpired as exc:
                _kill_group(proc.pid)
                proc.wait()
                raise DerivationError(f"the script ran past its {time_limit_s} s limit") from exc
            finally:
                _kill_group(proc.pid)
        stderr_tail = _read_capped(stderr_path, STDERR_TAIL_BYTES, "script's error output",
                                   tail=True).decode("utf-8", "replace")
        stdout_size = os.lstat(stdout_path).st_size
        stdout = _read_capped(stdout_path, STDOUT_MAX_BYTES, "script's output",
                              head=True).decode("utf-8", "replace")
        number: float | None = None
        reported: int | float | None = None
        if code == 0 and os.path.lexists(result_file):
            raw = _read_capped(result_file, RESULT_MAX_BYTES, "result file")
            try:
                value = json.loads(raw).get("number")
            except (ValueError, AttributeError) as exc:
                raise DerivationError("the result file is not a JSON object") from exc
            if isinstance(value, int | float) and not isinstance(value, bool):
                reported = value
                number = float(value)
        return RerunResult(number, code, stderr_tail, time_limit_s, stdout,
                           stdout_size > STDOUT_MAX_BYTES, reported)
