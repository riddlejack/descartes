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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any, Final, Protocol, cast

from stemma.driver import canary, events, frontmatter, hostio, status, usage, verify
from stemma.driver.assemble import folder_digest, sha256_bytes
from stemma.driver.config import (
    LOCK_FILE,
    PLUGIN_DIR,
    SESSIONS_DIR,
    STATE_FILE,
    DriverConfig,
    Limits,
)
from stemma.driver.errors import DriverBusy, DriverError, HarnessError, RunStateError
from stemma.driver.launch import LaunchSpec, Session, expectation, launch
from stemma.driver.runfolder import load_run
from stemma.driver.settings import ToolSets, tools_for_role
from stemma.register import checks_api, roles, safeio

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
    r"^criteria\.md$", r"^branches/(.+/)?revision-\d+\.md$", r"^checks/doc-[^/]+\.json$",
))
_SKIP_TOP: Final = frozenset({".stemma", ".claude", "store", "register", "task"})
# The files a frame-only run ends on (CONTRACTS v0.4 section 9); the tree is tree.json.
FRAME_FILES: Final = ("intake.md", "frame.md", "tree.json", "framer/critique.md",
                      "framer/lead-response.md")


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
    @property
    def round(self) -> int: ...


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


# Which pending checks start first when sessions are scarce (invariant: the flow of a run,
# not a quality bar). Frame, branch and draft reviews gate the lead's next step; claim
# checks are many and each gates only its own figure. Within a rank, oldest first.
LAUNCH_RANK: Final = {"blind-framer": 0, "branch-reviewer": 1, "deciders-eye-reviewer": 2,
                      "evidence-auditor": 2, "mechanical-reader": 2, "claim-checker": 3,
                      "retrospective-writer": 4}


def launch_order(pending: Sequence[CheckRequestLike]) -> list[CheckRequestLike]:
    """Pending checks in launch order: by LAUNCH_RANK, then by request time."""
    return sorted(pending, key=lambda r: (LAUNCH_RANK.get(r.role, 3), r.created_at))


def real_checks_api() -> ChecksApi:
    return cast(ChecksApi, importlib.import_module("stemma.register.checks_api"))


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    fd = hostio.open_nofollow(Path(os.path.realpath(run_dir)), LOCK_FILE, os.O_RDWR)
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


# How a driver run ended, and its exit code (invariant: one code per kind of end).
OUTCOME_CODES: Final = {
    "released": 0,  # the register recorded finish_run accepted
    "ended_without_release": 1,  # the lead exited and nothing released the run
    "canary_failed": 2,  # the lead did not start on the run plugin alone
    "stopped_by_limit": 3,  # the lead reached a recorded resource limit
    "lead_failed": 4,  # the lead could not start or crashed
    "release_withdrawn": 5,  # released, then a session it rested on was revoked
    "revocation_failed": 6,  # a wrong-model check could not be revoked: release blocked
    "usage_limited": 7,  # the account's usage limit stopped sessions; resume continues
    "frame_complete": 8,  # a frame-only run: the frame, tree and critique response exist
    "driver_stopped": 130,  # SIGTERM or SIGINT reached the driver
}

# Ends of a checking session that say nothing about the check itself, so resume runs it
# again (invariant shared with the register: these outcomes mean it never did its job).
RETRYABLE_OUTCOMES: Final = checks_api.RETRYABLE_OUTCOMES

Revoke = Callable[[Path, str, str], None]
Released = Callable[[Path], bool]


def register_released(run_dir: Path) -> bool:
    """True when the register recorded `finish_run` accepted and it still stands."""
    return register_release_state(run_dir) == "released"


def register_release_state(run_dir: Path) -> str | None:
    """`released`, `withdrawn` or None, as the register records it."""
    from stemma.register.store import Register

    return Register(run_dir).release_state()


def register_revoke(run_dir: Path, session_id: str, reason: str) -> None:
    """Take verdict authority from a session (`Register.revoke_session`, part B)."""
    from stemma.register.store import Register

    fn = getattr(Register(run_dir), "revoke_session", None)
    if fn is None:
        raise RunStateError("Register.revoke_session is not available")
    fn(session_id, reason)


def lead_prompt(run_id: str) -> str:
    return f"run:{run_id}"


class Driver:
    def __init__(self, cfg: DriverConfig, run_dir: Path, api: ChecksApi | None = None,
                 limits: Limits | None = None, *, released: Released | None = None,
                 revoke: Revoke | None = None) -> None:
        self.cfg = cfg
        self.released = released or register_released
        self.revoke = revoke or register_revoke
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
        # Checks to launch again, by request id, with the end of their last session; they
        # wait their turn with the pending checks (see step).
        self.relaunch_due: dict[str, tuple[CheckRequestLike, str | None, str | None]] = {}
        self.usage_pending: list[dict[str, Any]] = []  # records not yet written
        self._usage_deferred = False
        self.usage_summary: dict[str, Any] = {}
        self._refresh_usage_summary(report_skipped=True)
        self.milestones = usage.Milestones(self.run_dir / events.EVENTS)
        self._reg: Any = None  # the register, opened when a claim's branch is first needed
        self.exit_code = 0
        # Frame-only runs (CONTRACTS v0.4 section 9): end once the frame is done.
        self.stop_after: str = str(self.run.get("stop_after") or "full")
        self._frame_sig: tuple[Any, ...] | None = None
        self._frame_sig_at = 0.0

    # ----- state -------------------------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        try:
            text = safeio.read_text(self.run_dir, STATE_FILE)
        except FileNotFoundError:
            return {"run_id": self.run_id, "lead": {"session_id": None, "launches": 0,
                                                    "running": False},
                    "artifacts": {}, "check_attempts": {}}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RunStateError(f"driver state is not JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("run_id") != self.run_id:
            raise RunStateError("driver state does not belong to this run")
        return data

    def _save_state(self) -> None:
        hostio.write_json(self.run_dir, STATE_FILE, self.state)

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
            if s.spec.request_id is not None and not problems:
                self._lose_authority(s, f"init model {init.get('model')!r}, run.json "
                                        f"{s.spec.model!r}")
        return problems

    def _lose_authority(self, s: Session, reason: str) -> None:
        """A check on the wrong model or effort: no verdict of it may stand; stop it now.
        `_end` revokes the session in the register and relaunches the request."""
        with s.lock:
            if s.revoke_reason is not None:
                return
            s.revoke_reason = reason
        s.stop("model_mismatch", self.limits.stop_grace_s)

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
        # The lead starts no worker under s2 single_loop, nor in a frame-only run.
        deny_agent = role == "lead" and (self.run["switches"]["s2_shape"] == "single_loop"
                                         or self.stop_after == "frame")
        return LaunchSpec(name, role, prompt, m["model"], m["effort"], session_id, resume,
                          tools, request_id, deny_agent)

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
        self.state["outcome"] = None
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
        prior = self._resumable_reviewer(req) if n == 1 else None
        try:
            spec = self._spec(role, req.prompt, prior or str(uuid.uuid4()), prior is not None,
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

    def _resumable_reviewer(self, req: CheckRequestLike) -> str | None:
        """Reviewer memory (D8): a branch's re-review resumes the session that reviewed
        its previous round, when that session ended, was verified and not revoked. Any
        other check, and a relaunch after a failed resume, starts fresh."""
        if roles.short(req.role) != "branch-reviewer" or req.round < 2:
            return None
        prev = [st for st in self.api.states(self.run_dir).values()
                if st.request.role == req.role and st.request.prompt == req.prompt
                and st.request.round == req.round - 1]
        if not prev or prev[0].status != "ended" or not prev[0].session_id:
            return None
        sid = str(prev[0].session_id)
        if sid not in checks_api.verified_sessions(self.run_dir):
            return None
        events.emit(self.run_dir, "reviewer_resumed", request_id=req.request_id,
                    prompt=req.prompt, round=req.round, session_id=sid)
        return sid

    def _latest_round(self, req: CheckRequestLike) -> int:
        return max((o.request.round for o in self.api.states(self.run_dir).values()
                    if o.request.role == req.role and o.request.prompt == req.prompt),
                   default=req.round)

    def _due_again(self, st: CheckStateLike) -> None:
        """Queue a check to launch again (same request and round, a new session), unless
        a later round replaced it or it used up its attempts."""
        rid, req = st.request.request_id, st.request
        latest = self._latest_round(req)
        attempts = int(self.state["check_attempts"].get(rid, 0))
        if latest > req.round:
            reason, outcome = f"round {latest} replaced round {req.round}", "superseded"
        elif attempts >= self.limits.max_check_attempts:
            reason, outcome = f"{attempts} attempts", "given_up"
        else:
            self.relaunch_due[rid] = (req, st.session_id, st.outcome)
            return
        events.emit(self.run_dir, "check_not_relaunched", request_id=rid, reason=reason,
                    previous_outcome=st.outcome)
        if st.status == "running":
            self.api.mark_ended(self.run_dir, rid, -1, outcome)

    def relaunch_unended_checks(self) -> None:
        """On resume: queue every check whose session never ended, and every check whose
        session ended without doing its job for a reason outside it (the usage limit, a
        crash, a failed launch, a failed canary). They launch within the usual limits."""
        self.state["usage_limited"] = False
        for _rid, st in sorted(self.api.states(self.run_dir).items()):
            retry = st.status == "ended" and st.outcome in RETRYABLE_OUTCOMES
            if st.status == "running" or retry:
                self._due_again(st)

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
                if scope == "main" and s.spec.request_id is not None:
                    self._lose_authority(s, f"transcript model {model!r} effort {effort!r}, "
                                            f"run.json {expected['model']!r} "
                                            f"{expected['effort']!r}")

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
        if s.revoke_reason is not None:
            return "model_mismatch"
        if s.rate_limited is not None:
            return "rate_limited"
        if s.killed_reason in ("canary_failed", "stopped_by_limit", "driver_stopped",
                               "frame_complete"):
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
        try:
            self._record_usage(name, live, outcome)
        except Exception as exc:  # noqa: BLE001 - accounting never stops a run; it is loud
            events.emit(self.run_dir, "usage_not_recorded", session=name,
                        error=f"{type(exc).__name__}: {exc}")
        if outcome == "rate_limited":
            self.state["usage_limited"] = True
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
            if s.revoke_reason is not None:
                self._revoke_and_relaunch(s, code)
                return
            init_model = (s.init or {}).get("model")
            if live.tail is not None and init_model == s.spec.model:
                # The final transcript scan found no model or effort mismatch (a mismatch
                # would have set revoke_reason): this session's records may now count.
                checks_api.mark_verified(self.run_dir, s.spec.session_id, s.spec.model,
                                         s.spec.effort)
                events.emit(self.run_dir, "session_verified", session=name,
                            session_id=s.spec.session_id, model=s.spec.model,
                            effort=s.spec.effort)
            else:
                events.emit(self.run_dir, "session_unverified", session=name,
                            session_id=s.spec.session_id, init_model=init_model,
                            transcript_found=live.tail is not None)
            if outcome != "driver_stopped":
                self.api.mark_ended(self.run_dir, s.spec.request_id, code, outcome)
            if outcome == "canary_failed":
                # The session was stopped before it began work: nothing was checked.
                st = self.api.states(self.run_dir).get(s.spec.request_id)
                if st is not None:
                    self._due_again(st)
            return
        lead = self.state["lead"]
        if s.init is not None and isinstance(s.init.get("session_id"), str):
            lead["session_id"] = s.init["session_id"]
        last: dict[str, Any] = {"result": str(r.get("result") or "")[:4000], **summary}
        lead.update({"running": False, "exit_code": code, "outcome": outcome, "result": last})
        self._save_state()

    # ----- token accounting -------------------------------------------------------------
    def _branch_of(self, prompt: str) -> tuple[str | None, str | None]:
        if prompt.startswith("branch:"):
            return prompt.split(":", 1)[1], "the check's prompt"
        if prompt.startswith("claim:"):
            try:
                if self._reg is None:
                    from stemma.register.store import open_register  # the register (part B)

                    self._reg = open_register(self.run_dir)
                return self._reg.get_claim(prompt.split(":", 1)[1]).get("branch_id"), \
                    "the claim's branch_id"
            except Exception:  # noqa: BLE001 - an unknown claim leaves the branch unknown
                return None, None
        return None, None

    def _refresh_usage_summary(self, report_skipped: bool = False) -> None:
        """The summary from usage.jsonl plus the records still waiting to be written.
        Never raises: accounting observes the run and never stops it."""
        try:
            loaded = usage.load(self.run_dir)
            self.usage_summary = usage.summary(loaded.records + self.usage_pending)
            self.usage_summary["skipped_lines"] = loaded.skipped
            if report_skipped and loaded.skipped:
                events.emit(self.run_dir, "usage_records_skipped", lines=loaded.skipped)
        except Exception as exc:  # noqa: BLE001 - loud, never fatal
            events.emit(self.run_dir, "usage_not_recorded", what="summary",
                        error=f"{type(exc).__name__}: {exc}")

    def _usage_records(self, name: str, session_id: str, role: str, prompt: str,
                       request_id: str | None, model: str, effort: str, started: float,
                       end: float, outcome: str, main: Path | None,
                       subs: list[verify.Subagent], result: dict[str, Any] | None,
                       recovered: bool = False) -> list[dict[str, Any]]:
        """One record for the session's own thread and one per subagent, counted from
        their transcripts within [started, end]."""
        starts = usage.phase_starts(self.milestones.update())
        branch, branch_source = self._branch_of(prompt)
        base = {"session": name, "session_id": session_id, "request_id": request_id,
                "prompt": prompt, "expected_model": model, "expected_effort": effort,
                "launched_at": usage.iso(started), "ended_at": usage.iso(end),
                "outcome": outcome, "recovered": recovered}
        mine = usage.in_window(usage.read_messages(main), started, end) if main else []
        records = [{"kind": "session", "role": role, **base,
                    "branch": branch, "branch_source": branch_source,
                    "wall_s": round(end - started, 1), "transcript_found": main is not None,
                    "harness": usage.harness_figures(result), "cost_note": usage.COST_NOTE,
                    **usage.tally(mine, starts)}]
        for sub in sorted(subs, key=lambda x: x.agent_id):
            msgs = usage.in_window(usage.read_messages(sub.tail.path), started, end)
            if not msgs:
                continue  # an earlier launch's subagent, already counted then
            records.append({"kind": "subagent", "role": roles.short(sub.agent_type or "unknown"),
                            **base, "agent_id": sub.agent_id, "agent_type": sub.agent_type,
                            "spawn_depth": sub.spawn_depth,
                            "branch": usage.branch_from_brief(sub.tail.path),
                            "branch_source": "the first branches/<id>/ its brief names",
                            "wall_s": round(msgs[-1].at - msgs[0].at, 1), "harness": None,
                            "cost_note": "inside the parent session's cost estimate",
                            **usage.tally(msgs, starts)})
        return records

    def _record_usage(self, name: str, live: Live, outcome: str) -> None:
        s = live.session
        sid = (s.init or {}).get("session_id") or s.spec.session_id
        main = live.tail.path if live.tail is not None else verify.find_transcript(
            self.cfg.claude_config, self.run_dir, s.init, sid)
        self.usage_pending += self._usage_records(
            name, sid, s.spec.role, s.spec.prompt, s.spec.request_id, s.spec.model,
            s.spec.effort, s.started_wall, time.time(), outcome, main,
            list(live.subagents.values()), s.result)
        self._flush_usage()

    def _flush_usage(self) -> None:
        """Write the waiting records if usage.jsonl's lock comes within its bounded wait;
        otherwise keep them for the next pass (recorded once per deferral)."""
        if not self.usage_pending:
            return
        try:
            written = usage.append_bounded(self.run_dir, self.usage_pending)
        except Exception as exc:  # noqa: BLE001 - loud, never fatal; kept for later
            events.emit(self.run_dir, "usage_not_recorded", what="usage.jsonl",
                        records=len(self.usage_pending), error=f"{type(exc).__name__}: {exc}")
            written = False
        if written:
            self.usage_pending = []
            self._usage_deferred = False
        elif not self._usage_deferred:
            self._usage_deferred = True
            events.emit(self.run_dir, "usage_write_deferred", records=len(self.usage_pending),
                        wait_s=usage.LOCK_WAIT_S)
        self._refresh_usage_summary()

    def recover_usage(self) -> None:
        """On resume: count every launch this run recorded whose usage was never written
        (the driver stopped first), from its transcript and saved stream."""
        try:
            have = {(r["session"], r.get("session_id")) for r in usage.read_records(self.run_dir)
                    if r["kind"] == "session"}
            evs = events.read(self.run_dir)
            ended = {(e.get("session"), e.get("session_id")): e for e in evs
                     if e.get("event") == "session_ended"}
            for e in evs:
                if e.get("event") != "session_launched":
                    continue
                name, sid = str(e.get("session")), str(e.get("session_id"))
                if (name, sid) in have:
                    continue
                started = usage._ts(e.get("at"))
                if started is None:
                    continue
                end_ev = ended.get((name, sid))
                end = usage._ts(end_ev.get("at")) if end_ev else None
                main = verify.find_transcript(self.cfg.claude_config, self.run_dir, None, sid)
                subs = verify.subagents(main, {}) if main is not None else []
                self.usage_pending += self._usage_records(
                    name, sid, str(e.get("role")), str(e.get("prompt") or ""),
                    e.get("request_id"), str(e.get("model")), str(e.get("effort")), started,
                    end if end is not None else time.time(),
                    str(end_ev.get("outcome")) if end_ev else "not recorded (driver stopped)",
                    main, subs, self._saved_result(name), recovered=True)
                have.add((name, sid))
            self._flush_usage()
        except Exception as exc:  # noqa: BLE001 - loud, never fatal
            events.emit(self.run_dir, "usage_not_recorded", what="recovery",
                        error=f"{type(exc).__name__}: {exc}")

    def _saved_result(self, name: str) -> dict[str, Any] | None:
        """The last result message of a session's saved stream, if it wrote one."""
        try:
            data = safeio.read_bytes(self.run_dir, f"{SESSIONS_DIR}/{name}.stream.jsonl")
        except (FileNotFoundError, safeio.UnsafePath, NotADirectoryError):
            return None
        result = None
        for raw in data.splitlines():
            if b'"result"' not in raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "result":
                result = msg
        return result

    def _revoke_and_relaunch(self, s: Session, code: int) -> None:
        rid = s.spec.request_id
        assert rid is not None and s.revoke_reason is not None  # invariant: a check
        try:
            self.revoke(self.run_dir, s.spec.session_id, s.revoke_reason)
        except Exception as exc:  # noqa: BLE001 - fail closed: the release is blocked
            reason = (f"revoking session {s.spec.session_id} failed "
                      f"({type(exc).__name__}: {exc})")
            events.emit(self.run_dir, "revoke_failed", request_id=rid,
                        session_id=s.spec.session_id, error=reason)
            self.state["release_blocked"] = reason
            self._save_state()
            try:
                checks_api.block_release(self.run_dir, reason)
            except Exception as exc2:  # noqa: BLE001 - the driver's own state still blocks
                events.emit(self.run_dir, "release_block_unrecorded",
                            error=f"{type(exc2).__name__}: {exc2}")
        events.emit(self.run_dir, "check_revoked", request_id=rid, role=s.spec.role,
                    session_id=s.spec.session_id, reason=s.revoke_reason)
        attempts = int(self.state["check_attempts"].get(rid, 0))
        if attempts >= self.limits.max_check_attempts:
            events.emit(self.run_dir, "check_given_up", request_id=rid, attempts=attempts)
            self.api.mark_ended(self.run_dir, rid, code, "model_mismatch")
            return
        st = self.api.states(self.run_dir).get(rid)
        if st is None:
            raise RunStateError(f"check request {rid} vanished from the register")
        self._launch_check(st.request, relaunch=True)

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

    def write_status(self, state: str, outcome: str | None = None) -> None:
        checks = []
        for rid, st in sorted(self.api.states(self.run_dir).items()):
            checks.append({"request_id": rid, "role": st.request.role,
                           "prompt": st.request.prompt, "status": st.status,
                           "session_id": st.session_id, "outcome": st.outcome})
        lead = dict(self.state["lead"])
        landed = [{"path": k, "at": v[3]} for k, v in sorted(self.state["artifacts"].items(),
                                                             key=lambda kv: kv[1][3])]
        status.write(self.run_dir, status.build(self.run_dir, self.run_id, state, lead, checks,
                                                landed, outcome or self.state.get("outcome"),
                                                usage=self.usage_summary))

    # ----- frame-only runs ---------------------------------------------------------------
    def frame_failures(self) -> list[str]:
        """What a frame-only run still lacks before it may end (empty: the frame is done)."""
        from stemma.register.store import Register

        return Register(self.run_dir).frame_failures()

    def _frame_signature(self) -> tuple[Any, ...]:
        out: list[Any] = []
        for rel in FRAME_FILES:
            try:
                st = os.stat(self.run_dir / rel, follow_symlinks=False)
                out.append((rel, st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                out.append((rel, None, None))
        return tuple(out)

    def _frame_settled(self, now: float) -> bool:
        """The frame is done and has not changed for `frame_settle_s` (or the lead has
        stopped working on it)."""
        try:
            missing = self.frame_failures()
        except Exception as exc:  # noqa: BLE001 - loud: an unreadable register is not done
            missing = [f"the register could not be read ({type(exc).__name__}: {exc})"]
        self.state["frame_missing"] = missing
        if missing:
            self._frame_sig = None
            return False
        sig = self._frame_signature()
        if sig != self._frame_sig:
            self._frame_sig, self._frame_sig_at = sig, now
        lead_running = any(lv.session.spec.role == "lead" for lv in self.live.values())
        return not lead_running or now - self._frame_sig_at >= self.limits.frame_settle_s

    def _end_frame_only(self) -> None:
        events.emit(self.run_dir, "frame_complete", files=list(FRAME_FILES))
        self.state["frame_complete"] = True
        self._save_state()
        for live in list(self.live.values()):
            reason = "frame_complete" if live.session.spec.role == "lead" else "driver_stopped"
            live.session.stop(reason, self.limits.stop_grace_s)
        for name, live in list(self.live.items()):
            self._end(name, live)

    def _live_checks(self) -> int:
        return sum(1 for lv in self.live.values() if lv.session.spec.request_id is not None)

    def step(self) -> bool:
        """One pass. Returns False when nothing is running and nothing is due."""
        now = time.monotonic()
        self._police(now)
        if self.usage_pending:
            self._flush_usage()
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
        if any(lv.session.rate_limited is not None for lv in self.live.values()):
            self.state["usage_limited"] = True
        if self.stop_after == "frame" and not self.state.get("frame_complete") \
                and self._frame_settled(now):
            self._end_frame_only()
            self.write_status("stopped", "frame_complete")
            return False
        if self.state.get("usage_limited"):
            # The account's usage limit refused a session: launching more only burns
            # checks into failures. Let live sessions end; resume continues the run.
            self._scan_artifacts()
            busy = bool(self.live)
            self.write_status("running" if busy else "stopped")
            return busy
        launching = {lv.session.spec.request_id for lv in self.live.values()}
        due = [*self.api.pending_checks(self.run_dir),
               *(req for req, _sid, _out in self.relaunch_due.values())]
        for req in launch_order(due):
            if self._live_checks() >= self.limits.max_concurrent_checks:
                break
            if req.request_id in launching or req.request_id in self.skipped:
                continue
            again = self.relaunch_due.pop(req.request_id, None)
            if again is not None:
                latest = self._latest_round(req)
                if latest > req.round:  # a newer round was asked for while this one waited
                    events.emit(self.run_dir, "check_not_relaunched",
                                request_id=req.request_id, previous_outcome=again[2],
                                reason=f"round {latest} replaced round {req.round}")
                    st = self.api.states(self.run_dir).get(req.request_id)
                    if st is not None and st.status == "running":
                        self.api.mark_ended(self.run_dir, req.request_id, -1, "superseded")
                    continue
                events.emit(self.run_dir, "check_relaunch", request_id=req.request_id,
                            previous_session_id=again[1], previous_outcome=again[2])
            self._launch_check(req, relaunch=again is not None)
        self._scan_artifacts()
        pending = [r for r in self.api.pending_checks(self.run_dir)
                   if r.request_id not in self.skipped]
        busy = bool(self.live) or bool(pending) or bool(self.relaunch_due)
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
            return self._finish("driver_stopped")
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
        return self._finish(self._run_outcome())

    def _run_outcome(self) -> str:
        """Success is the register's record of the release, and nothing else."""
        lead_outcome = self.state["lead"].get("outcome")
        if lead_outcome == "canary_failed":
            return "canary_failed"
        if self.stop_after == "frame" and self.state.get("frame_complete"):
            return "frame_complete"
        if self.state.get("usage_limited") and not self.released(self.run_dir):
            return "usage_limited"
        if self.state.get("release_blocked"):
            return "revocation_failed"  # whatever the register says, nothing stands
        if self.released(self.run_dir):
            return "released"
        try:
            withdrawn = register_release_state(self.run_dir) == "withdrawn"
        except Exception:  # noqa: BLE001 - unreadable register: not released either way
            withdrawn = False
        if withdrawn:
            return "release_withdrawn"
        if lead_outcome == "stopped_by_limit":
            return "stopped_by_limit"
        if lead_outcome in ("launch_failed", "failed"):
            return "lead_failed"
        return "ended_without_release"

    def _finish(self, outcome: str) -> int:
        code = OUTCOME_CODES[outcome]
        self.exit_code = code
        self.state["outcome"] = outcome
        self._save_state()
        events.emit(self.run_dir, "driver_finished", exit_code=code, outcome=outcome,
                    lead_outcome=self.state["lead"].get("outcome"))
        self.write_status("finished" if outcome == "released" else "stopped", outcome)
        try:  # the run's outcome and exit code never depend on its accounting
            self._flush_usage()
            usage.write_md(self.run_dir, self.run_id, self.usage_summary)
        except Exception as exc:  # noqa: BLE001 - loud, never fatal
            events.emit(self.run_dir, "usage_not_recorded", what="usage.md",
                        error=f"{type(exc).__name__}: {exc}")
        return code


def start(cfg: DriverConfig, run_dir: Path, api: ChecksApi | None = None,
          limits: Limits | None = None, watch: bool = True, *,
          released: Released | None = None, revoke: Revoke | None = None) -> int:
    with run_lock(run_dir):
        d = Driver(cfg, run_dir, api, limits, released=released, revoke=revoke)
        events.emit(d.run_dir, "run_started", run_id=d.run_id,
                    plugin_digest=d.run["plugin"]["digest"], harness=d.version)
        d.api.request_check(d.run_dir, "blind-framer", "frame:own", "run_start")
        d.start_lead(resume=False)
        return d.watch() if watch else 0


def resume(cfg: DriverConfig, run_dir: Path, api: ChecksApi | None = None,
           limits: Limits | None = None, *, released: Released | None = None,
           revoke: Revoke | None = None) -> int:
    with run_lock(run_dir):
        d = Driver(cfg, run_dir, api, limits, released=released, revoke=revoke)
        events.emit(d.run_dir, "run_resumed", run_id=d.run_id,
                    lead_session_id=d.state["lead"].get("session_id"))
        d.recover_usage()
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
