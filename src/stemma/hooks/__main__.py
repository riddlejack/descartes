"""Hook entry point: `$STEMMA_PYTHON -I -m stemma.hooks <event>`.

Reads the harness's hook input from stdin, applies the guards for that event, records
their events in `events.jsonl`, and answers in the harness protocol:
- allow: exit 0, nothing on stdout;
- rewrite: exit 0, `updatedInput` JSON on stdout;
- an arrival notice for the lead (PreToolUse, allowed calls only): exit 0,
  `additionalContext` JSON on stdout, beside any `updatedInput`;
- refuse: exit 2, the refusal on stderr. Exit 2 blocks the tool whatever stdout holds.
Any failure inside a guard is a refusal, so a broken guard never lets a tool through
(the hook entry's `"onFailure": "block"` covers a crash before this code runs).
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from stemma.hooks import arrivals, guards
from stemma.register import checks_api, jsonl, roles

EXIT_ALLOW = 0
EXIT_BLOCK = 2  # harness protocol: exit code 2 blocks the tool call


def _decide_pre(hook: dict[str, Any], run_dir: Path) -> guards.Decision:
    tool = guards._require(hook, "tool_name")
    web = guards.guard_live_web(hook, run_dir)
    if web is not None:
        return web
    lead = guards.guard_lead_tool(hook)
    if lead is not None:
        return lead
    if tool in guards.AGENT_TOOLS:
        return guards.guard_agent(hook)
    if tool in guards.WRITE_TOOLS:
        return guards.guard_write(hook, run_dir)
    if tool in guards.READ_TOOLS:
        draft = None
        if hook.get("agent_type") == roles.DECIDERS_EYE:
            draft = checks_api.draft_version_for_session(run_dir, str(hook.get("session_id")))
        return guards.guard_read(hook, run_dir, draft)
    if isinstance(tool, str) and tool.startswith(roles.MCP_PREFIX):
        return guards.guard_mcp(hook, run_dir)
    return guards.ALLOW


def _decide_post(hook: dict[str, Any], run_dir: Path) -> guards.Decision:
    if guards._require(hook, "tool_name") in guards.WRITE_TOOLS:
        return guards.Decision(allow=True, events=[guards.write_event(hook, run_dir)])
    return guards.ALLOW


def _decide_subagent_start(hook: dict[str, Any], run_dir: Path) -> guards.Decision:
    event = {"event": "subagent_start", "at": guards.now(), "agent_type": hook.get("agent_type"),
             "agent_id": hook.get("agent_id"), "session_id": hook.get("session_id")}
    return guards.Decision(allow=True, events=[event])


HANDLERS = {"pre": _decide_pre, "post": _decide_post, "subagent-start": _decide_subagent_start}


def _answer(decision: guards.Decision, notice: str | None) -> str:
    specific: dict[str, Any] = {}
    if decision.updated_input is not None:
        specific["updatedInput"] = decision.updated_input
    if notice is not None:
        specific["additionalContext"] = notice
    if not specific:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **specific}})


def run_stop(stdin: str) -> tuple[int, str, str]:
    """The Stop event: the lead's own session may not stop before its run is released
    (stemma.hooks.stop). Every other session stops; so does any session when this
    fails, since the driver resumes a lead that ended (failing open, unlike the
    guards, never traps a session)."""
    from stemma.hooks import stop

    run_dir: Path | None = None
    try:
        hook = json.loads(stdin)
        if not isinstance(hook, dict):
            raise guards.GuardInputError("hook input is not a JSON object")
        run_dir = guards.run_dir_from_env()
        decision = stop.decide(hook, run_dir)
    except Exception as exc:  # noqa: BLE001 - fail open, loudly
        if run_dir is not None:
            with contextlib.suppress(Exception):  # the stderr line below still says it
                jsonl.append(run_dir / "events.jsonl", {
                    "event": "stop_hook_failed", "at": guards.now(),
                    "error": f"{type(exc).__name__}: {exc}"[:500]})
        return EXIT_ALLOW, "", f"Stemma's stop hook failed ({type(exc).__name__})."
    jsonl.append(run_dir / "events.jsonl", {
        "event": "lead_stop_refused" if decision.block else "stop_allowed",
        "at": guards.now(), "session_id": hook.get("session_id"), **decision.detail})
    if decision.block:
        return EXIT_BLOCK, "", decision.reason
    return EXIT_ALLOW, "", ""


def run(event: str, stdin: str) -> tuple[int, str, str]:
    """Return (exit code, stdout, stderr) for one hook call."""
    if event == "stop":
        return run_stop(stdin)
    try:
        handler = HANDLERS[event]
        hook = json.loads(stdin)
        if not isinstance(hook, dict):
            raise guards.GuardInputError("hook input is not a JSON object")
        run_dir = guards.run_dir_from_env()
        decision = handler(hook, run_dir)
        if not decision.allow or event != "pre":
            if decision.events:
                jsonl.append(run_dir / "events.jsonl", *decision.events)
            out = _answer(decision, None) if decision.allow else ""
        else:
            # An allowed call of the lead's own thread carries every queued arrival
            # notice; it is marked delivered only once the events are recorded and the
            # answer is built, so any failure before leaves it queued.
            with arrivals.held(run_dir, hook) as due:
                events = list(decision.events) + arrivals.delivery_events(due, hook)
                if events:
                    jsonl.append(run_dir / "events.jsonl", *events)
                out = _answer(decision, arrivals.notice_text(due) if due else None)
                if due:
                    arrivals.mark_delivered(run_dir, due, hook)
    except Exception as exc:  # noqa: BLE001 - any guard failure blocks: fail closed
        return EXIT_BLOCK, "", f"Refused: a Stemma guard failed ({type(exc).__name__})."
    if not decision.allow:
        return EXIT_BLOCK, "", decision.refusal
    return EXIT_ALLOW, out, ""


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    code, out, err = run(event, sys.stdin.read())
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    sys.exit(code)


if __name__ == "__main__":
    main()
