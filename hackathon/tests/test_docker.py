"""The image takes only named paths, and no env file can enter its build context."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from .conftest import REPO

ALLOWED_SOURCES = {"pyproject.toml", "README.md", "src", "conformance/package.json",
                   "conformance/package-lock.json", "conformance/bridge.ts",
                   "conformance/run.sh", "conformance/vendor", "run.py",
                   "hackathon/__init__.py", "hackathon/replay.py", "hackathon/judgelog.py",
                   "hackathon/conformance.py", "hackathon/sample.py", "hackathon/README.md",
                   "hackathon/sample"}


def _copies() -> list[list[str]]:
    text = (REPO / "Dockerfile").read_text().replace("\\\n", " ")
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*COPY\s+(?!--from)(.+)", line)
        if m:
            out.append(m.group(1).split()[:-1])
    return out


def test_only_named_paths_are_copied() -> None:
    sources = [s for copy in _copies() for s in copy]
    assert sources and set(sources) <= ALLOWED_SOURCES
    assert all(s not in (".", "./", "*") for s in sources)
    assert all((REPO / s).exists() for s in sources)


def _ignored(rel: str, patterns: list[str]) -> bool:
    ignored = False
    for pat in patterns:  # Docker's rule: the last matching pattern wins
        negate = pat.startswith("!")
        core = pat[1:] if negate else pat
        candidates = {core, core.removeprefix("**/")}
        if any(fnmatch.fnmatchcase(rel, c) or fnmatch.fnmatchcase(Path(rel).name, c)
               for c in candidates if "/" not in c or fnmatch.fnmatchcase(rel, c)):
            ignored = not negate
    return ignored


def test_env_files_at_any_depth_are_ignored() -> None:
    patterns = [ln.strip() for ln in (REPO / ".dockerignore").read_text().splitlines()
                if ln.strip() and not ln.startswith("#")]
    for rel in (".env", "hackathon/zetaris/.env", "src/stemma/.env.local",
                "hackathon/zetaris/stemma.env", ".env.production"):
        assert _ignored(rel, patterns), rel
    assert not _ignored(".env.example", patterns)
