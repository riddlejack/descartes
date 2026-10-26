"""Where a path lands inside a run folder, resolved the way the filesystem will.

An agent path with a `..` component is refused outright (no run-relative form):
collapsing `..` before resolving symlinks would let `run/jump/../secret`, with `jump` a
link out, look like a path inside the run. Every other path is resolved component by
component through every existing symlink (`os.path.realpath`), and compared casefolded,
because the default macOS filesystem is case-insensitive (`Register/log.jsonl` is
`register/log.jsonl`). A path outside the run folder has no run-relative form.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


def has_parent_ref(path: str) -> bool:
    return ".." in Path(path).parts


def resolve(path: str, cwd: str) -> Path:
    """Absolute real path of `path` (symlinks followed in order), even if its last parts
    do not exist yet. Callers refuse `..` first."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(cwd) / p
    return Path(os.path.realpath(p))


def run_relative(path: str, cwd: str, run_dir: Path) -> PurePosixPath | None:
    """The path relative to the run folder, casefolded; None when outside it or when the
    path has a `..` component."""
    if has_parent_ref(path) or has_parent_ref(cwd):
        return None
    target = str(resolve(path, cwd)).casefold()
    root = str(Path(os.path.realpath(run_dir))).casefold()
    if target == root:
        return PurePosixPath(".")
    if not target.startswith(root + os.sep):
        return None
    return PurePosixPath(target[len(root) + 1 :].replace(os.sep, "/"))


def _parts(rel: PurePosixPath) -> tuple[str, ...]:
    return rel.parts


# Written only by Stemma's tools, the driver or the hooks (CONTRACTS section 3), plus the
# files that configure later sessions (`CLAUDE.md`, `.claude/`), which no agent may change.
_TOOL_ONLY_TOP = frozenset(
    {"store", "register", "tree.json", "tree.md", "events.jsonl", "run.json", "status.json",
     "status.html", "usage.jsonl", "usage.md", "task", "claude.md", ".claude", ".stemma",
     "reviews", "deliverable"}
)
# Tool-written files inside an otherwise author-written folder (casefolded).
_TOOL_ONLY_FILES = frozenset({("framer", "own-frame.md"), ("framer", "critique.md")})


def is_tool_only(rel: PurePosixPath) -> bool:
    parts = _parts(rel)
    if not parts or parts == (".",):
        return False
    if parts[0] in _TOOL_ONLY_TOP:
        return True
    if parts[0] == "branches" and "verdicts" in parts[1:-1]:
        return True
    if parts[0] == "branches" and len(parts) >= 2 and parts[-1] == "verdicts":
        return True
    if parts[0].endswith((".tmp", ".lock")) or parts[0].startswith(("tree.json", "tree.md")):
        return True  # host temp and lock names beside tool-only files
    return parts in _TOOL_ONLY_FILES


def is_branch_file(rel: PurePosixPath, names: frozenset[str]) -> bool:
    parts = _parts(rel)
    return len(parts) >= 3 and parts[0] == "branches" and parts[-1] in names


def under(rel: PurePosixPath, top: str) -> bool:
    parts = _parts(rel)
    return bool(parts) and parts[0] == top
