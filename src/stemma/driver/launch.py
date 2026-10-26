"""Launch one headless session of the pinned harness, and follow its stream.

The Claude Agent SDK (0.2.174) is not used to launch: its transport starts the child with
`{**os.environ, ...}` (inherited environment, which from the desktop app makes the pinned
binary "Not logged in"), and always adds `--input-format stream-json` and other flags, so
it cannot give `-p <prompt>` with exactly the probe-proven flags. The driver starts the
CLI itself with an environment built from nothing (`tests/driver/test_sdk_finding.py`
shows the SDK's leak).
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

from stemma.driver import canary, events
from stemma.driver.config import (
    SCRUBBED_LANG,
    SCRUBBED_PATH,
    SESSIONS_DIR,
    SETTINGS_DIR,
    DriverConfig,
)
from stemma.driver.errors import HarnessError
from stemma.driver.settings import ToolSets, build_settings

# Never set in a run's environment (invariant: section 6; it overrides --effort).
FORBIDDEN_ENV: Final = frozenset({"CLAUDE_CODE_EFFORT_LEVEL"})


def scrubbed_env(cfg: DriverConfig, run_dir: Path) -> dict[str, str]:
    """The whole environment of a session, built from nothing (no inherited variable)."""
    pw = pwd.getpwuid(os.getuid())
    env = {
        "HOME": pw.pw_dir,
        "USER": pw.pw_name,
        "PATH": SCRUBBED_PATH,
        "LANG": SCRUBBED_LANG,
        "CLAUDE_CONFIG_DIR": str(cfg.claude_config),
        "STEMMA_RUN_DIR": str(run_dir),
        "STEMMA_PYTHON": str(cfg.python),
        "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
    }
    if cfg.cache_dir is not None:
        env["STEMMA_CACHE"] = str(cfg.cache_dir)  # the evidence service's fetch cache
    assert not FORBIDDEN_ENV & env.keys()  # invariant
    return env


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


def build_argv(cfg: DriverConfig, plugin_dir: Path, settings_path: Path,
               spec: LaunchSpec) -> list[str]:
    if spec.prompt.startswith("-") or not spec.prompt:
        raise HarnessError(f"prompt must not be empty or start with '-': {spec.prompt!r}")
    argv = [
        str(cfg.claude_bin), "-p", spec.prompt,
        "--output-format", "stream-json", "--verbose",
        "--agent", f"stemma:{spec.role}",
        "--model", spec.model,
        "--effort", spec.effort,
        "--plugin-dir", str(plugin_dir),
        "--settings", str(settings_path),
        "--tools", ",".join(spec.tools.builtin),
    ]
    # Equals form: the value can never be read as a separate flag.
    argv.append(f"--resume={spec.session_id}" if spec.resume else f"--session-id={spec.session_id}")
    return argv


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
            out: IO[bytes]) -> None:
    """Reader thread: copy the stream to disk, run the canary on init, note compactions."""
    unparsed_seen = False
    with open(s.stream_path, "ab") as sink:
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
            elif mtype == "result":
                s.result = msg


def launch(cfg: DriverConfig, run_dir: Path, plugin_dir: Path, spec: LaunchSpec,
           on_init: OnInit, grace_s: float) -> Session:
    """Write the session's settings file and start the process. Never raises after start."""
    settings_dir = run_dir / SETTINGS_DIR
    sessions_dir = run_dir / SESSIONS_DIR
    settings_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    settings = build_settings(run_dir, spec.role, spec.prompt, spec.tools, cfg.claude_config)
    settings_path = settings_dir / f"{spec.name}.json"
    settings_path.write_text(json.dumps(settings, indent=1, sort_keys=True), encoding="utf-8")
    argv = build_argv(cfg, plugin_dir, settings_path, spec)
    stream_path = sessions_dir / f"{spec.name}.stream.jsonl"
    stderr_path = sessions_dir / f"{spec.name}.stderr"
    with open(stderr_path, "ab") as err:
        try:
            proc = subprocess.Popen(
                argv, cwd=run_dir, env=scrubbed_env(cfg, run_dir), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=err, start_new_session=True,
            )
        except OSError as exc:
            raise HarnessError(f"could not start {cfg.claude_bin}: {exc}") from exc
    s = Session(spec, argv, proc, time.monotonic(), stream_path, stderr_path)
    assert proc.stdout is not None  # invariant: stdout=PIPE
    s.reader = threading.Thread(target=_follow, name=f"follow-{spec.name}", daemon=True,
                                args=(run_dir, s, on_init, grace_s, proc.stdout))
    s.reader.start()
    return s


def expectation(run_dir: Path, plugin_dir: Path, agents: list[str], skills: list[str],
                spec: LaunchSpec, version: str) -> canary.Expectation:
    return canary.Expectation(
        cwd=run_dir, plugin_dir=plugin_dir, agents=frozenset(agents), skills=frozenset(skills),
        builtin_tools=frozenset(spec.tools.main), harness_version=bare_version(version),
        session_id=None if spec.resume else spec.session_id,
    )
