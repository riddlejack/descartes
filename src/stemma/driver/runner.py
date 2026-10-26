"""The watch loop: the lead, every checking session, verification, events and status.

One driver process per run folder (an exclusive lock). The register says which checks are
due (`stemma.register.checks_api`); the driver launches each, at most the recorded number
at once, alongside the lead. Every launch is canaried, its resolved model and effort read
from its transcript, and its end recorded. A stopped run resumes: the lead with
`--resume <its session id>`, checks that were launched but never ended relaunched fresh.
"""

from __future__ import annotations

import fcntl
import importlib
import json
import os
import re
import signal
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, Final, Protocol, cast

from stemma.driver import canary, events, frontmatter, status, verify
from stemma.driver.assemble import folder_digest, sha256_bytes
from stemma.driver.config import LOCK_FILE, PLUGIN_DIR, STATE_FILE, DriverConfig, Limits
from stemma.driver.errors import DriverBusy, DriverError, HarnessError, RunStateError
from stemma.driver.launch import LaunchSpec, Session, expectation, launch
from stemma.driver.runfolder import load_run, write_json_atomic
from stemma.driver.settings import ToolSets, tools_for_role
from stemma.register import roles

# Checking roles the driver may start (invariant: section 2), short names.
CHECK_ROLES: Final = frozenset(roles.short(r) for r in roles.CHECKING_ROLES)
# An id-only prompt: a kind and an id, nothing an author could use to steer the check.
ID_PROMPT: Final = re.compile(r"^[a-z][a-z_-]*:[A-Za-z0-9_.\-]+$")
# Run-folder files whose arrival is an artifact-landed event (section 3).
_ARTIFACT_RES: Final = tuple(re.compile(p) for p in (
    r"^intake\.md$", r"^frame\.md$", r"^framer/(own-frame|critique|lead-response)\.md$",
    r"^branches/(.+/)?(order|restatement|record)\.md$", r"^branches/(.+/)?verdicts/[^/]+\.json$",
    r"^draft/answer\.v\d+\.md$", r"^reviews/v\d+/[^/]+\.md$",
    r"^deliverable/answer(\.reader)?\.md$", r"^retro\.md$", r"^tree\.json$",
))
_SKIP_TOP: Final = frozenset({".stemma", ".claude", "store", "register", "task"})


class CheckRequestLike(Protocol):
    @property
    def request_id(self) -> str: ...
    @property
    def role(self) -> str: ...
    @property
    def prompt(self) -> str: ...
    @property
    def event(self) -> str: ...
    @property
    def created_at(self) -> str: ...


class CheckStateLike(Protocol):
    @property
    def request(self) -> CheckRequestLike: ...
    @property
    def status(self) -> str: ...
    @property
    def session_id(self) -> str | None: ...
    @property
    def outcome(self) -> str | None: ...


class ChecksApi(Protocol):
    """What the driver needs from `stemma.register.checks_api`."""

    def request_check(self, run_dir: Path, role: str, prompt: str,
                      event: str) -> CheckRequestLike: ...
    def pending_checks(self, run_dir: Path) -> Sequence[CheckRequestLike]: ...
    def mark_launched(self, run_dir: Path, request_id: str, session_id: str, model: str,
                      effort: str, relaunch: bool = False) -> None: ...
    def mark_ended(self, run_dir: Path, request_id: str, exit_code: int,
                   outcome: str) -> None: ...
    def states(self, run_dir: Path) -> dict[str, Any]: ...


def real_checks_api() -> ChecksApi:
    return cast(ChecksApi, importlib.import_module("stemma.register.checks_api"))


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    path = run_dir / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DriverBusy(f"another driver is running this run: {run_dir}") from None
        yield
    finally:
        os.close(fd)


class _Stop(Exception):
    """SIGTERM or SIGINT reached the driver."""


@dataclass
class Live:
    """A running session and what the driver has verified of it so far."""

    session: Session
    tail: verify.Tail | None = None
    subagents: dict[str, verify.Subagent] = field(default_factory=dict)


def lead_prompt(run_id: str) -> str:
    return f"run:{run_id}"


class Driver:
    def __init__(self, cfg: DriverConfig, run_dir: Path, api: ChecksApi | None = None,
                 limits: Limits | None = None) -> None:
        self.cfg = cfg
        self.run_dir = Path(os.path.realpath(run_dir))
        self.api = api if api is not None else real_checks_api()
        self.run = load_run(self.run_dir)
        self.run_id: str = self.run["run_id"]
        self.limits = limits or Limits.from_json(self.run["limits"])
        self.plugin_dir = self.run_dir / PLUGIN_DIR
        self.models: dict[str, dict[str, str]] = self.run["models"]
        self.version: str = self.run["harness"]["version"]
        self.agents: list[str] = list(self.run["plugin"]["agents"])
        self.skills: list[str] = list(self.run["plugin"]["skills"])
        self.tools: dict[str, list[str]] = {}
        for a in self.agents:
            p = self.plugin_dir / "agents" / f"{a}.md"
            self.tools[a] = frontmatter.parse(p.read_text(encoding="utf-8"), str(p)).tools()
        self.state = self._load_state()
        self.live: dict[str, Live] = {}  # by session name
        self.skipped: set[str] = set()  # request ids that could not be launched
        self.exit_code = 0

    # ----- state -------------------------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        p = self.run_dir / STATE_FILE
        if not p.exists():
            return {"run_id": self.run_id, "lead": {"session_id": None, "launches": 0,
                                                    "running": False},
                    "artifacts": {}, "check_attempts": {}}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RunStateError(f"driver state is not JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("run_id") != self.run_id:
            raise RunStateError("driver state does not belong to this run")
        return data

    def _save_state(self) -> None:
        write_json_atomic(self.run_dir / STATE_FILE, self.state)

    # ----- launching ---------------------------------------------------------------
    def _check_plugin(self) -> None:
        digest, _ = folder_digest(self.plugin_dir)
        if digest != self.run["plugin"]["digest"]:
            raise HarnessError(f"run plugin changed since assembly: digest {digest}, "
                               f"run.json {self.run['plugin']['digest']}")

    def _on_init(self, s: Session, init: dict[str, Any]) -> list[str]:
        exp = expectation(self.run_dir, self.plugin_dir, self.agents, self.skills, s.spec,
                          self.version)
        problems = canary.check(init, exp)
        common = {"session": s.spec.name, "role": s.spec.role,
                  "session_id": init.get("session_id"), "request_id": s.spec.request_id}
        if problems:
            events.emit(self.run_dir, "canary_failed", problems=problems, **common)
        else:
            events.emit(self.run_dir, "canary_passed", **common)
        if init.get("model") != s.spec.model:
            events.emit(self.run_dir, "model_effort_mismatch", scope="init",
                        model=init.get("model"), expected_model=s.spec.model, **common)
        return problems

    def _start(self, spec: LaunchSpec) -> Live:
        self._check_plugin()
        s = launch(self.cfg, self.run_dir, self.plugin_dir, spec, self._on_init,
                   self.limits.stop_grace_s)
        events.emit(self.run_dir, "session_launched", session=spec.name, role=spec.role,
                    prompt=spec.prompt, session_id=spec.session_id, resume=spec.resume,
                    model=spec.model, effort=spec.effort, request_id=spec.request_id,
                    argv_sha256=sha256_bytes("\0".join(s.argv).encode()))
        live = Live(s)
        self.live[spec.name] = live
        return live

    def _spec(self, role: str, prompt: str, session_id: str, resume: bool, name: str,
              request_id: str | None) -> LaunchSpec:
        m = self.models[role]
        tools: ToolSets = tools_for_role(role, self.tools)
        return LaunchSpec(name, role, prompt, m["model"], m["effort"], session_id, resume,
                          tools, request_id)

    def start_lead(self, resume: bool) -> None:
        lead = self.state["lead"]
        if resume:
            sid = lead.get("session_id")
            if not sid:
                raise RunStateError("the lead was never launched; nothing to resume")
        else:
            if lead.get("session_id"):
                raise RunStateError("the lead was already launched; use resume")
            sid = str(uuid.uuid4())
        n = int(lead.get("launches", 0))
        name = "lead" if n == 0 else f"lead-r{n}"
        spec = self._spec("lead", lead_prompt(self.run_id), sid, resume, name, None)
        lead.update({"session_id": sid, "launches": n + 1, "running": True, "name": name,
                     "outcome": None, "exit_code": None})
        self._save_state()
        self._start(spec)

    def _launch_check(self, req: CheckRequestLike, relaunch: bool) -> None:
        rid = req.request_id
        role = roles.short(req.role)
        if role not in CHECK_ROLES or not ID_PROMPT.match(req.prompt):
            events.emit(self.run_dir, "invalid_check_request", request_id=rid, role=req.role,
                        prompt=req.prompt[:200])
            self.skipped.add(rid)
            return
        attempts = self.state["check_attempts"]
        n = int(attempts.get(rid, 0)) + 1
        try:
            spec = self._spec(role, req.prompt, str(uuid.uuid4()), False,
                              f"check-{rid}-{n}", rid)
        except DriverError as exc:
            events.emit(self.run_dir, "launch_failed", request_id=rid, role=role,
                        prompt=req.prompt, reason=str(exc))
            self.skipped.add(rid)
            return
        attempts[rid] = n
        self._save_state()
        try:
            self._start(spec)
        except DriverError as exc:
            events.emit(self.run_dir, "launch_failed", request_id=rid, role=role,
                        prompt=req.prompt, reason=str(exc))
            self.skipped.add(rid)
            return
        self.api.mark_launched(self.run_dir, rid, spec.session_id, spec.model, spec.effort,
                               relaunch=relaunch)

    def relaunch_unended_checks(self) -> None:
        for rid, st in sorted(self.api.states(self.run_dir).items()):
            if st.status == "running":
                events.emit(self.run_dir, "check_relaunch", request_id=rid,
                            previous_session_id=st.session_id)
                self._launch_check(st.request, relaunch=True)

    # ----- watching ----------------------------------------------------------------
    def _expected(self, agent_type: str | None) -> dict[str, str] | None:
        if not agent_type or not agent_type.startswith(roles.PLUGIN + ":"):
            return None
        return self.models.get(roles.short(agent_type))

    def _record_pairs(self, live: Live, pairs: set[tuple[str, str]], scope: str,
                      expected: dict[str, str], extra: dict[str, Any]) -> None:
        s = live.session
        for model, effort in sorted(pairs):
            match = model == expected["model"] and effort == expected["effort"]
            rec = {"session": s.spec.name, "session_id": s.spec.session_id, "scope": scope,
                   "model": model, "effort": effort, "expected_model": expected["model"],
                   "expected_effort": expected["effort"], "match": match, **extra}
            events.emit(self.run_dir, "resolved_model_effort", **rec)
            if not match:
                events.emit(self.run_dir, "model_effort_mismatch", **rec)

    def _scan_transcripts(self, live: Live, final: bool) -> None:
        s = live.session
        if s.init is None:
            return
        sid = s.init.get("session_id") or s.spec.session_id
        if live.tail is None:
            path = verify.find_transcript(self.cfg.claude_config, self.run_dir, s.init, sid)
            if path is None:
                if final:
                    events.emit(self.run_dir, "transcript_missing", session=s.spec.name,
                                session_id=sid)
                return
            live.tail = verify.Tail(path)
        self._record_pairs(live, live.tail.read(), "main", self.models[s.spec.role], {})
        for sub in verify.subagents(live.tail.path, live.subagents):
            exp = self._expected(sub.agent_type)
            events.emit(self.run_dir, "subagent_seen", session=s.spec.name, session_id=sid,
                        agent_id=sub.agent_id, agent_type=sub.agent_type,
                        spawn_depth=sub.spawn_depth)
            if exp is None:
                events.emit(self.run_dir, "unexpected_subagent", session=s.spec.name,
                            session_id=sid, agent_id=sub.agent_id, agent_type=sub.agent_type)
        for sub in live.subagents.values():
            exp = self._expected(sub.agent_type)
            if exp is not None:
                self._record_pairs(live, sub.tail.read(), "subagent", exp,
                                   {"agent_id": sub.agent_id, "agent_type": sub.agent_type,
                                    "spawn_depth": sub.spawn_depth})

    def _outcome(self, s: Session) -> str:
        if s.killed_reason in ("canary_failed", "stopped_by_limit", "driver_stopped"):
            return s.killed_reason
        if s.init is None or s.killed_reason == "init_timeout":
            return "launch_failed"
        r = s.result or {}
        if s.proc.returncode == 0 and r.get("subtype") == "success" and not r.get("is_error"):
            return "completed"
        return "failed"

    def _end(self, name: str, live: Live) -> None:
        s = live.session
        s.proc.wait()
        s.reap(self.limits.stop_grace_s)
        self._scan_transcripts(live, final=True)
        outcome = self._outcome(s)
        code = s.proc.returncode if s.proc.returncode is not None else -1
        r = s.result or {}
        summary: dict[str, Any] = {k: r.get(k) for k in ("subtype", "is_error", "terminal_reason", "num_turns",
                                          "total_cost_usd", "subagent_stats") if k in r}
        if outcome == "launch_failed":
            events.emit(self.run_dir, "launch_failed", session=name, role=s.spec.role,
                        request_id=s.spec.request_id, exit_code=code,
                        stderr_tail=s.stderr_tail(self.limits.stderr_tail_bytes))
        events.emit(self.run_dir, "session_ended", session=name, role=s.spec.role,
                    session_id=s.spec.session_id, request_id=s.spec.request_id,
                    exit_code=code, outcome=outcome, result=summary,
                    permission_denials=len(r.get("permission_denials") or []))
        del self.live[name]
        if s.spec.request_id is not None:
            if outcome != "driver_stopped":
                self.api.mark_ended(self.run_dir, s.spec.request_id, code, outcome)
            return
        lead = self.state["lead"]
        if s.init is not None and isinstance(s.init.get("session_id"), str):
            lead["session_id"] = s.init["session_id"]
        last: dict[str, Any] = {"result": str(r.get("result") or "")[:4000], **summary}
        lead.update({"running": False, "exit_code": code, "outcome": outcome, "result": last})
        self._save_state()

    def _police(self, now: float) -> None:
        for live in list(self.live.values()):
            s = live.session
            age = now - s.started
            if s.init is None and s.canary_problems is None and age > self.limits.init_timeout_s:
                s.stop("init_timeout", self.limits.stop_grace_s)
            limit = self.limits.check_max_s if s.spec.request_id else self.limits.lead_max_s
            if age > limit and s.killed_reason is None:
                events.emit(self.run_dir, "session_stopped_by_limit", session=s.spec.name,
                            limit_s=limit)
                s.stop("stopped_by_limit", self.limits.stop_grace_s)

    def _scan_artifacts(self) -> None:
        seen: dict[str, list[Any]] = self.state["artifacts"]
        changed = False
        for root, dirs, files in os.walk(self.run_dir):
            rel_root = Path(root).relative_to(self.run_dir).as_posix()
            if rel_root == ".":
                dirs[:] = [d for d in dirs if d not in _SKIP_TOP]
            for f in files:
                rel = f if rel_root == "." else f"{rel_root}/{f}"
                if not any(r.match(rel) for r in _ARTIFACT_RES):
                    continue
                p = Path(root) / f
                try:
                    stt = p.stat()
                except FileNotFoundError:
                    continue
                key = [stt.st_mtime_ns, stt.st_size]
                old = seen.get(rel)
                if old is not None and old[:2] == key:
                    continue
                digest = sha256_bytes(p.read_bytes())
                if old is not None and old[2] == digest:
                    seen[rel] = key + [digest, old[3]]
                    changed = True
                    continue
                at = events.now()
                events.emit(self.run_dir, "artifact_landed" if old is None else "artifact_changed",
                            path=rel, sha256=digest, bytes=stt.st_size)
                seen[rel] = key + [digest, old[3] if old else at]
                changed = True
        if changed:
            self._save_state()

    def write_status(self, state: str) -> None:
        checks = []
        for rid, st in sorted(self.api.states(self.run_dir).items()):
            checks.append({"request_id": rid, "role": st.request.role,
                           "prompt": st.request.prompt, "status": st.status,
                           "session_id": st.session_id, "outcome": st.outcome})
        lead = dict(self.state["lead"])
        landed = [{"path": k, "at": v[3]} for k, v in sorted(self.state["artifacts"].items(),
                                                             key=lambda kv: kv[1][3])]
        status.write(self.run_dir, status.build(self.run_dir, self.run_id, state, lead, checks,
                                                landed))

    def _live_checks(self) -> int:
        return sum(1 for lv in self.live.values() if lv.session.spec.request_id is not None)

    def step(self) -> bool:
        """One pass. Returns False when nothing is running and nothing is due."""
        now = time.monotonic()
        self._police(now)
        for name, live in list(self.live.items()):
            if not live.session.running():
                self._end(name, live)
            else:
                self._scan_transcripts(live, final=False)
        lead = self.state["lead"]
        if lead.get("outcome") == "canary_failed":
            # The lead did not start on the run plugin alone: nothing else may run.
            for live in list(self.live.values()):
                live.session.stop("driver_stopped", self.limits.stop_grace_s)
            for name, live in list(self.live.items()):
                self._end(name, live)
            self.exit_code = 2
            return False
        launching = {lv.session.spec.request_id for lv in self.live.values()}
        for req in self.api.pending_checks(self.run_dir):
            if self._live_checks() >= self.limits.max_concurrent_checks:
                break
            if req.request_id in launching or req.request_id in self.skipped:
                continue
            self._launch_check(req, relaunch=False)
        self._scan_artifacts()
        pending = [r for r in self.api.pending_checks(self.run_dir)
                   if r.request_id not in self.skipped]
        busy = bool(self.live) or bool(pending)
        self.write_status("running" if busy else "stopped")
        return busy

    def watch(self) -> int:
        def handler(signum: int, _frame: FrameType | None) -> None:
            raise _Stop(signum)

        old_term = signal.signal(signal.SIGTERM, handler)
        old_int = signal.signal(signal.SIGINT, handler)
        try:
            while self.step():
                time.sleep(self.limits.poll_interval_s)
        except _Stop:
            events.emit(self.run_dir, "driver_stopped")
            for live in list(self.live.values()):
                live.session.stop("driver_stopped", self.limits.stop_grace_s)
            for name, live in list(self.live.items()):
                self._end(name, live)
            self.write_status("stopped")
            return 130
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
        lead = self.state["lead"]
        if self.exit_code == 0 and lead.get("outcome") != "completed":
            self.exit_code = 1
        events.emit(self.run_dir, "driver_finished", exit_code=self.exit_code,
                    lead_outcome=lead.get("outcome"))
        self.write_status("finished" if self.exit_code == 0 else "stopped")
        return self.exit_code


def start(cfg: DriverConfig, run_dir: Path, api: ChecksApi | None = None,
          limits: Limits | None = None, watch: bool = True) -> int:
    with run_lock(run_dir):
        d = Driver(cfg, run_dir, api, limits)
        events.emit(d.run_dir, "run_started", run_id=d.run_id,
                    plugin_digest=d.run["plugin"]["digest"], harness=d.version)
        d.api.request_check(d.run_dir, "blind-framer", "frame:own", "run_start")
        d.start_lead(resume=False)
        return d.watch() if watch else 0


def resume(cfg: DriverConfig, run_dir: Path, api: ChecksApi | None = None,
           limits: Limits | None = None) -> int:
    with run_lock(run_dir):
        d = Driver(cfg, run_dir, api, limits)
        events.emit(d.run_dir, "run_resumed", run_id=d.run_id,
                    lead_session_id=d.state["lead"].get("session_id"))
        d.start_lead(resume=True)
        d.relaunch_unended_checks()
        return d.watch()


def show_status(cfg: DriverConfig, run_dir: Path, api: ChecksApi | None = None) -> dict[str, Any]:
    """Rebuild the status files from what is on disk, without launching anything."""
    d = Driver(cfg, run_dir, api)
    try:
        with run_lock(run_dir):
            d.state["lead"]["running"] = False
            d.write_status("stopped")
    except DriverBusy:
        pass  # a live driver keeps the files current itself
    try:
        return cast(dict[str, Any], json.loads((d.run_dir / "status.json").read_text()))
    except FileNotFoundError:
        raise RunStateError(f"no status.json yet in {d.run_dir}") from None
