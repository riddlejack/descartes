"""Launch one headless session of the pinned harness, and follow its stream.

The Claude Agent SDK (0.2.174) is not used to launch: its transport starts the child with
`{**os.environ, ...}` (inherited environment, which from the desktop app makes the pinned
binary "Not logged in"), and always adds `--input-format stream-json` and other flags, so
it cannot give `-p <prompt>` with exactly the probe-proven flags. The driver starts the
CLI itself with an environment built from nothing (`tests/driver/test_sdk_finding.py`
shows the SDK's leak). Every launch goes through `stemma.driver.clean.clean_launch`.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from stemma.driver import canary, clean, events, hostio
from stemma.driver.config import SESSIONS_DIR, SETTINGS_DIR, DriverConfig
from stemma.driver.errors import HarnessError
from stemma.driver.settings import (
    SESSION_TMP,
    ReadScope,
    ToolSets,
    read_scope,
    stemma_settings_extra,
)


def driver_env(cfg: DriverConfig, run_dir: Path) -> dict[str, str]:
    """What Stemma adds to the clean environment of each of its sessions."""
    extra = {
        "STEMMA_RUN_DIR": str(run_dir),
        "STEMMA_PYTHON": str(cfg.python),
        "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
    }
    if cfg.cache_dir is not None:
        extra["STEMMA_CACHE"] = str(cfg.cache_dir)  # the evidence service's fetch cache
    return extra


def scrubbed_env(cfg: DriverConfig, run_dir: Path) -> dict[str, str]:
    """The whole environment of a Stemma session, built from nothing."""
    return clean.scrubbed_env(cfg.claude_config, driver_env(cfg, run_dir))


def harness_version(cfg: DriverConfig) -> str:
    """`claude --version` of the pinned binary, e.g. "2.1.308 (Claude Code)"."""
    try:
        out = subprocess.run(
            [str(cfg.claude_bin), "--version"], env=scrubbed_env(cfg, Path("/nonexistent")),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=60,  # resource limit: seconds for the binary to print its version
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"{cfg.claude_bin} --version failed: {exc}") from exc
    text = out.stdout.strip()
    if out.returncode != 0 or not text:
        raise HarnessError(f"{cfg.claude_bin} --version exited {out.returncode}: "
                           f"{out.stderr.strip()[:500]}")
    return text


def bare_version(version: str) -> str:
    return version.split()[0]


@dataclass(frozen=True)
class LaunchSpec:
    name: str  # unique label within the run: "lead", "lead-r1", "check-<request_id>-1"
    role: str  # short role name
    prompt: str  # id-only
    model: str
    effort: str
    session_id: str
    resume: bool
    tools: ToolSets
    request_id: str | None = None  # set for checking sessions
    deny_agent: bool = False  # the lead under s2 single_loop may start no subagent


@dataclass
class Session:
    spec: LaunchSpec
    argv: list[str]
    proc: subprocess.Popen[bytes]
    started: float
    stream_path: Path
    stderr_path: Path
    init: dict[str, Any] | None = None
    canary_problems: list[str] | None = None
    result: dict[str, Any] | None = None
    killed_reason: str | None = None
    revoke_reason: str | None = None  # a check that ran on the wrong model or effort
    rate_limited: dict[str, Any] | None = None  # the account's usage limit refused it
    reader: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def running(self) -> bool:
        return self.proc.poll() is None

    def reap(self, grace_s: float) -> None:
        """After exit: kill what is left of the process group, then let the reader finish."""
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if self.reader is not None:
            self.reader.join(timeout=grace_s)
            if self.reader.is_alive():
                raise HarnessError(f"stream of session {self.spec.name} did not close")

    def stop(self, reason: str, grace_s: float) -> None:
        with self.lock:
            if self.killed_reason is None:
                self.killed_reason = reason
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self.proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()

    def stderr_tail(self, n: int) -> str:
        try:
            data = self.stderr_path.read_bytes()
        except FileNotFoundError:
            return ""
        return data[-n:].decode("utf-8", "replace")


OnInit = Callable[[Session, dict[str, Any]], list[str]]


def _follow(run_dir: Path, s: Session, on_init: OnInit, grace_s: float,
            out: IO[bytes], sink: IO[bytes]) -> None:
    """Reader thread: copy the stream to disk, run the canary on init, note compactions."""
    unparsed_seen = False
    with sink:
        for raw in out:
            sink.write(raw)
            sink.flush()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                if not unparsed_seen:
                    unparsed_seen = True
                    events.emit(run_dir, "stream_unparsed", session=s.spec.name,
                                line=raw[:200].decode("utf-8", "replace"))
                continue
            if not isinstance(msg, dict):
                continue
            mtype, sub = msg.get("type"), msg.get("subtype")
            if s.init is None and s.canary_problems is None:
                if mtype == "system" and sub == "init":
                    s.init = msg
                    s.canary_problems = on_init(s, msg)
                    if s.canary_problems:
                        s.stop("canary_failed", grace_s)
                elif mtype in ("assistant", "user", "result"):
                    s.canary_problems = ["a model turn began before the init message"]
                    s.stop("canary_failed", grace_s)
                continue
            if mtype == "system" and isinstance(sub, str) and "compact" in sub:
                events.emit(run_dir, "compaction", session=s.spec.name,
                            session_id=s.spec.session_id, subtype=sub,
                            detail={k: v for k, v in msg.items()
                                    if k in ("compact_metadata", "trigger", "pre_tokens")})
            elif mtype == "rate_limit_event":
                limit = msg.get("rate_limit_info")
                if isinstance(limit, dict) and limit.get("status") == "rejected":
                    s.rate_limited = limit
                    events.emit(run_dir, "usage_limit_hit", session=s.spec.name,
                                session_id=s.spec.session_id,
                                limit_type=limit.get("rateLimitType"),
                                resets_at=limit.get("resetsAt"))
            elif mtype == "result":
                s.result = msg


_SCOPES: dict[str, ReadScope] = {}


def session_read_scope(cfg: DriverConfig) -> ReadScope:
    """Every session's Bash reads only its own run plus what the shell and interpreter
    need (computed once per interpreter)."""
    key = str(cfg.python)
    if key not in _SCOPES:
        _SCOPES[key] = read_scope(cfg.python)
    return _SCOPES[key]


def session_tmp(run_dir: Path, session_id: str) -> str:
    """The session's own harness temp folder inside the run (made here, no symlink)."""
    hostio.write_text(run_dir, f"{SESSION_TMP}/{session_id}/.keep", "")
    return str(run_dir / SESSION_TMP / session_id)


def launch(cfg: DriverConfig, run_dir: Path, plugin_dir: Path, spec: LaunchSpec,
           on_init: OnInit, grace_s: float) -> Session:
    """Write the session's settings file and start the process. Never raises after start."""
    settings_rel = f"{SETTINGS_DIR}/{spec.name}.json"
    cl = clean.clean_launch(
        cfg.claude_bin, cfg.claude_config, spec.prompt, cwd=run_dir, model=spec.model,
        effort=spec.effort, tools=spec.tools.builtin,
        settings_path=run_dir / settings_rel, plugin_dir=plugin_dir,
        settings_writer=lambda text: hostio.write_text(run_dir, settings_rel, text),
        agent=f"stemma:{spec.role}",
        settings_extra=stemma_settings_extra(run_dir, spec.role, spec.prompt, spec.tools,
                                             cfg.claude_config, cache_dir=cfg.cache_dir,
                                             deny_agent=spec.deny_agent,
                                             scope=session_read_scope(cfg),
                                             session_id=spec.session_id),
        env_extra={**driver_env(cfg, run_dir),
                   "CLAUDE_CODE_TMPDIR": session_tmp(run_dir, spec.session_id)},
        session_id=spec.session_id, resume=spec.resume)
    argv = cl.argv
    stream_rel = f"{SESSIONS_DIR}/{spec.name}.stream.jsonl"
    stderr_rel = f"{SESSIONS_DIR}/{spec.name}.stderr"
    stream_path, stderr_path = run_dir / stream_rel, run_dir / stderr_rel
    sink = hostio.open_append(run_dir, stream_rel)
    with hostio.open_append(run_dir, stderr_rel) as err:
        try:
            proc = subprocess.Popen(
                argv, cwd=run_dir, env=cl.env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=err, start_new_session=True,
            )
        except OSError as exc:
            sink.close()
            raise HarnessError(f"could not start {cfg.claude_bin}: {exc}") from exc
    s = Session(spec, argv, proc, time.monotonic(), stream_path, stderr_path)
    assert proc.stdout is not None  # invariant: stdout=PIPE
    s.reader = threading.Thread(target=_follow, name=f"follow-{spec.name}", daemon=True,
                                args=(run_dir, s, on_init, grace_s, proc.stdout, sink))
    s.reader.start()
    return s


def expectation(run_dir: Path, plugin_dir: Path, agents: list[str], skills: list[str],
                spec: LaunchSpec, version: str) -> canary.Expectation:
    return clean.stemma_expectation(
        cwd=run_dir, plugin_dir=plugin_dir, agents=agents, skills=skills,
        main_tools=spec.tools.main, harness_version=version,
        session_id=None if spec.resume else spec.session_id)
