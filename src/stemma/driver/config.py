"""Where the driver finds things, and the resource limits it records in `run.json`.

Every number here is a resource limit (time, count, bytes) or a logical invariant, and
says which. None decides whether research is good.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from stemma.driver.errors import ConfigError

# Layout of the run folder that belongs to the driver (invariant: agreed with the guards,
# which treat everything under `.stemma/` as tool-only).
STEMMA_DIR: Final = ".stemma"
PLUGIN_DIR: Final = ".stemma/plugin"
SETTINGS_DIR: Final = ".stemma/settings"
SESSIONS_DIR: Final = ".stemma/sessions"
STATE_FILE: Final = ".stemma/driver-state.json"
LOCK_FILE: Final = ".stemma/driver.lock"

# Paths no agent may write (CONTRACTS section 3, plus the files that configure later
# sessions). Invariant: the same list the guards enforce in `stemma.hooks.paths`.
# A trailing "/" marks a folder; "**" is any depth.
TOOL_ONLY: Final = (
    "store/",
    "register/",
    "tree.json",
    "tree.json.tmp",
    "tree.md",
    "events.jsonl",
    "events.jsonl.lock",
    "run.json",
    "status.json",
    "status.html",
    "task/",
    "CLAUDE.md",
    ".claude/",
    ".stemma/",
    "branches/**/verdicts/",
    "deliverable/",
    "reviews/",
    "framer/own-frame.md",
    "framer/critique.md",
)

# Models a run may use (invariant: CONTRACTS section 6, `availableModels`; never Haiku).
AVAILABLE_MODELS: Final = ("claude-opus-5-5", "claude-fable-5-1")
EFFORTS: Final = ("low", "medium", "high", "xhigh", "max")  # invariant: the harness's levels

# The scrubbed environment's fixed values (invariant: the probe-proven allowlist).
SCRUBBED_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin"
SCRUBBED_LANG: Final = "en_US.UTF-8"


@dataclass(frozen=True)
class Limits:
    """Resource limits of one run, recorded in `run.json`."""

    # Resource limit: checking sessions running at the same time (the lead runs beside them).
    max_concurrent_checks: int = 4
    # Resource limit: seconds between passes of the watch loop.
    poll_interval_s: float = 2.0
    # Resource limit: seconds a launch may take to print its stream-json init message.
    init_timeout_s: float = 180.0
    # Resource limit: wall-clock seconds one checking session may run before it is stopped.
    check_max_s: float = 3 * 3600.0
    # Resource limit: wall-clock seconds the lead session may run before it is stopped.
    lead_max_s: float = 24 * 3600.0
    # Resource limit: seconds between SIGTERM and SIGKILL when a session is stopped.
    stop_grace_s: float = 10.0
    # Resource limit: launches of one check request when each resolves the wrong model or
    # effort (each is revoked and relaunched until this many have run).
    max_check_attempts: int = 3
    # Resource limit: bytes of a session's stderr kept in an event when it fails.
    stderr_tail_bytes: int = 2000
    # Resource limit: rounds of the three final reviews (one per submitted draft); the
    # register refuses another round after this many and releases the last one with its
    # criticisms listed (CONTRACTS section 6, v0.3).
    max_review_rounds: int = 3
    # Recorded beside the numbers in run.json, so no reader takes them for a quality bar.
    purpose: str = "resource limits only (time, sessions, bytes); never a quality bar"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> Limits:
        try:
            return Limits(**data)
        except TypeError as exc:
            raise ConfigError(f"run.json limits do not match this driver: {exc}") from exc


def runs_dir_from_env() -> Path:
    """$STEMMA_RUNS, the one folder runs live in (an absolute path)."""
    value = os.environ.get("STEMMA_RUNS", "")
    if not value:
        raise ConfigError("STEMMA_RUNS is not set")
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise ConfigError(f"STEMMA_RUNS must be an absolute path, got {value!r}")
    return p


@dataclass(frozen=True)
class DriverConfig:
    runs_dir: Path
    claude_bin: Path
    claude_config: Path
    python: Path
    plugin_src: Path
    cache_dir: Path | None = None

    @staticmethod
    def from_env(plugin_src: Path | None = None) -> DriverConfig:
        def need(name: str) -> Path:
            value = os.environ.get(name, "")
            if not value:
                raise ConfigError(f"{name} is not set")
            p = Path(value).expanduser()
            if not p.is_absolute():
                raise ConfigError(f"{name} must be an absolute path, got {value!r}")
            return p

        runs = need("STEMMA_RUNS")
        binary = need("STEMMA_CLAUDE_BIN")
        config = need("STEMMA_CLAUDE_CONFIG")
        py = Path(os.environ.get("STEMMA_PYTHON") or sys.executable)
        cache = os.environ.get("STEMMA_CACHE") or None
        src = plugin_src or default_plugin_src()
        cfg = DriverConfig(runs, binary, config, py, src, Path(cache) if cache else None)
        cfg.check()
        return cfg

    def check(self) -> None:
        if not os.access(self.claude_bin, os.X_OK):
            raise ConfigError(f"STEMMA_CLAUDE_BIN is not an executable file: {self.claude_bin}")
        if not self.claude_config.is_dir():
            raise ConfigError(f"STEMMA_CLAUDE_CONFIG is not a folder: {self.claude_config}")
        if not os.access(self.python, os.X_OK):
            raise ConfigError(f"STEMMA_PYTHON is not an executable file: {self.python}")
        if not self.plugin_src.is_dir():
            raise ConfigError(f"plugin source is not a folder: {self.plugin_src}")


def default_plugin_src() -> Path:
    """The repo's `plugin/` folder (src/stemma/driver/config.py -> repo root)."""
    return Path(__file__).resolve().parents[3] / "plugin"
