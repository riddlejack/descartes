"""The bare arm of run 3's head-to-head (pre-registration, hypothesis H5).

Claude Code with web search and fetch, no Stemma plugin, Opus 5.5 at high effort, one
headless session, through the driver's own `clean_launch` (the pinned binary, the clean
configuration, nothing synced, no memory, no MCP server, no slash commands), its init
message checked by the canary before anything it says is used. It is given exactly run
3's question, named options, onboarding answers and as-of date, read from the run's own
launcher, `tests/e2e/run3_demo.py` (with `--question-file` replacing the question the
same way that launcher does).

    python -m hackathon.headtohead.bare --out ROOT [--question-file Q.json] [--dry-run]

It writes one new folder under ROOT (required; a folder outside the repository):
`prompt.txt` (the exact prompt), `launch.json` (model, effort, tools, settings, harness
version; no environment), `stream.jsonl` (the session's stream-json, as it came),
`answer.md` (the session's final answer), `usage.json` (the harness's own usage and cost
figures from its result) and `outcome.json`. `--dry-run` prints the prompt and the launch
and creates nothing.

Its tools are WebSearch and WebFetch, and Bash, Read and Write confined to its own
folder: Bash runs in the same sandbox as Stemma's workers (no network; reads denied from
the filesystem root and re-opened only for its folder and what the shell and interpreter
need; writes only in its folder), and the Read and Write tools are allowed only inside
its folder, with run 3's own folder, the Claude and Codex configurations and SSH keys
denied by name besides. Before the real session, a **scope canary** session in the same
folder, with the same settings, is told to read a marker file outside the folder with the
Read tool and with Bash, to reach the network from Bash, and, as controls, to read a
control file inside the folder with both tools and to fetch the same page with WebFetch.
Code requires that the canary session ended cleanly; that each of the six calls has its
result; that the three reaches outside were refused and the three controls succeeded (so
a refusal is the confinement's, not a broken tool or a network down); and that neither
the marker's random token nor the page appears where Bash could have put it. Otherwise
the bare arm is refused and the real session never starts. Whether this harness marks a
refused Bash command as an error result has not been watched live: if it does not, the
canary refuses the arm and says which step, never a silent pass.

Confinement is of the session's tools, enforced by the harness and proven at launch by
the scope canary; the harness process itself runs as the owner, as every Stemma session
does. An operating-system boundary around the whole harness process is not added: the
harness applies its own sandbox to Bash, and a second sandbox around it would stop that
one from starting. This is a stated limit of the comparison.

**Given an as-of date** (run 3's task has one), the bare arm gets exactly what Stemma's
workers get in an as_of run (CONTRACTS section 0d): no WebSearch and no WebFetch (left
out of its tools, denied in its settings, and refused by Stemma's own guard as a hook);
Bash with no network, as always; and Stemma's broker, the `stemma` MCP server, serving
the folder, of which it may call exactly the evidence tools Stemma's worker holds
(capture through an archive copy dated before the date, archived_links, the
date-bounded sec_search, the SEC tools and the reading tools), every other tool of that
server denied by name. The folder's run.json holds the date; it, the store, the event
log and the launch files are unwritable by every tool of the session. The scope
canary's network control is then the broker's capture of the page, which must come back
as an archived copy. Without an as-of date the arm is as above, with WebSearch and
WebFetch.

Stated deviation from a plain user: the prompt asks the bare arm to cite a source for
every figure (a plain user may not ask); without it the citation counts would measure
nothing.

**Run 4** (`--launcher tests/e2e/run4_demo.py`, or `--task`): the task is the one the
Stemma arm is given (the launcher's `task_with(None)`, or a `task.json`), read through
the driver's own loader, so both arms get the same question, onboarding answers,
information cutoff, options and criteria. Each attachment is copied byte for byte,
read-only, to `task/attachments/<name>` in the session's folder (where a Stemma run
folder holds it), its sha256 recorded in `run.json` and `launch.json`, and the prompt
names it, as the Stemma lead is told of its attachments. `--max-s` is the real session's
wall-time limit (run 4: 9000 s, the Stemma lead's limit); like the Stemma lead, the
session is not told it, and a session stopped by it gives no scorable answer.
`--canary-only` starts everything and runs the scope canary, then stops: the task is
never sent to a model.

**For the leakage audit** (`python -I -m stemma.audit.leakage`), the folder has a run
folder's layout: `run.json` (run id, as_of, and the harness folder holding the
transcripts), `events.jsonl` (a `session_launched` and a `session_ended` record for each
session, with the session id the launcher chose), each session's stream at
`.stemma/sessions/<name>.stream.jsonl`, the harness's saved tool output under
`.stemma-tmp/`, and a copy of each session's transcript (and any subagents') under
`transcripts/`, in case the harness folder is cleaned later. A transcript that cannot be
found makes the real session's answer ineligible, since its audit could not be
complete.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[2]
RUN3_LAUNCHER: Final = REPO / "tests" / "e2e" / "run3_demo.py"
MODEL: Final = "claude-opus-5-5"  # the pre-registration: Opus 5.5 at high effort
EFFORT: Final = "high"
TOOLS: Final = ("WebSearch", "WebFetch", "Bash", "Read", "Write")  # see the module's note
# Given an as-of date (see the module's note): no live web tool, and the broker.
AS_OF_TOOLS: Final = ("Bash", "Read", "Write")
BROKER: Final = "stemma"  # the MCP server's name in the as_of arm's configuration
# Entries of an as_of folder that no tool of the session may write: run.json gives the
# date to the broker and the guard; the rest is the broker's store, the guard's log and
# the launch files. Invariant: what the date and the record rest on.
PROTECTED: Final = ("run.json", "store", "events.jsonl", "events.jsonl.lock", ".mcp.json",
                    ".settings.json", ".stemma", "transcripts", "task")
# Where the task's attachments are copied, read-only (invariant: the Stemma run folder's
# own place for them, `stemma.driver.runfolder._write_task`).
ATTACHMENTS: Final = "task/attachments"
# Where the record the leakage audit reads is kept (invariant: a Stemma run folder's
# layout, `stemma.driver.config.SESSIONS_DIR` and `stemma.driver.settings.SESSION_TMP`).
SESSIONS: Final = ".stemma/sessions"
SESSION_TMP: Final = ".stemma-tmp"
TRANSCRIPTS: Final = "transcripts"
CANARY_NAME: Final = "scope-canary"
SESSION_NAME: Final = "bare"
# Resource limits: seconds between checks of a session's deadlines, and seconds a
# stopped or ending session is given to exit before it is killed.
POLL_S: Final = 1.0
STOP_GRACE_S: Final = 10.0
# The scope canary's own resource limit: seconds it may run.
CANARY_MAX_S: Final = 600
CANARY_PAGE: Final = "https://example.com"
CANARY_PAGE_TEXT: Final = "Example Domain"  # what that page says, were it reached
CANARY_PROMPT: Final = """\
This is a safety check of your tools, not a task. Do exactly these six things, in
order, one tool call each, and report what each one returned:
1. Use the Read tool to read the file {marker}
2. Use the Bash tool to run: cat {marker}
3. Use the Bash tool to run: curl -sS --max-time 10 {page}
4. Use the Read tool to read the control file {control}
5. Use the Bash tool to run: cat {control}
6. {network_step}
Then reply with the word DONE.
"""
CANARY_WEBFETCH: Final = "Use the WebFetch tool to fetch {page}"
CANARY_BROKER: Final = ("Use the mcp__" + BROKER + "__capture tool to capture {page} with the "
                        "reason: scope canary")
# Resource limits: seconds the session may run, and seconds to wait for its init message.
SESSION_MAX_S: Final = 4 * 3600
# The interpreter whose folders Bash may read (its standard library, nothing of this
# repository); the repository's own venv would re-open the repository to the bare arm.
SCOPE_PYTHON: Final = Path("/opt/homebrew/bin/python3.12")
INIT_TIMEOUT_S: Final = 180

PROMPT: Final = """\
You are preparing an answer for {who}.

The question, exactly as asked:
{question}

{options}What you were told:
{intake}
{attached}
Scope: answer only this question. Research nothing outside it; say briefly, at the end,
what you did not research that a reader might want.

{cutoff}

Write your answer as a markdown memo. Cite the source of every figure you state, with a
link to the page it comes from.
"""


class BareError(Exception):
    """The bare arm cannot be planned or launched; the message says why."""


def run3_launcher() -> ModuleType:
    """Run 3's own launcher module, read from its file (not collected by pytest)."""
    spec = importlib.util.spec_from_file_location("stemma_run3_demo", RUN3_LAUNCHER)
    if spec is None or spec.loader is None:
        raise BareError(f"cannot read run 3's launcher at {RUN3_LAUNCHER}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def task(question_file: Path | None = None) -> dict[str, Any]:
    """Run 3's task exactly as its launcher builds it."""
    t = run3_launcher().task_with(question_file)
    if not isinstance(t, dict) or not isinstance(t.get("question"), str):
        raise BareError("run 3's launcher gave no task with a question")
    return t


def task_from_file(path: Path) -> dict[str, Any]:
    """The task the Stemma arm is given, read by the driver's own loader."""
    from stemma.driver.errors import TaskInvalid
    from stemma.driver.task import load_task

    try:
        t = load_task(path)
    except TaskInvalid as exc:
        raise BareError(f"{path}: {exc}") from exc
    return {"question": t.question, "intake": t.intake, "as_of": t.as_of,
            "options": list(t.options) if t.options is not None else None,
            "criteria": list(t.criteria), "scope": t.scope,
            "attachments": [str(a) for a in t.attachments]}


def task_from_launcher(path: Path) -> dict[str, Any]:
    """The pre-registered task a run's launcher builds (its `task_with(None)`, as for the
    Stemma arm), validated by the driver's own loader."""
    import tempfile

    spec = importlib.util.spec_from_file_location(f"stemma_launcher_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise BareError(f"cannot read the launcher at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    t = mod.task_with(None)
    if not isinstance(t, dict):
        raise BareError(f"{path}: task_with gave no task")
    with tempfile.TemporaryDirectory(prefix="bare-task-") as work:
        task_path = Path(work) / "task.json"
        task_path.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
        return task_from_file(task_path)


def copy_attachments(folder: Path, sources: list[str]) -> list[dict[str, str]]:
    """Copy each attachment, byte for byte, read-only, to `task/attachments/<name>`, as
    the Stemma run folder holds it; each with its sha256, checked after the copy."""
    import hashlib
    import stat

    out = []
    dest_dir = folder / ATTACHMENTS
    for src in sources:
        data = Path(src).read_bytes()
        dest = dest_dir / Path(src).name
        if dest.exists():
            raise BareError(f"two attachments share the name {dest.name!r}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        digest = hashlib.sha256(data).hexdigest()
        if hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
            raise BareError(f"the copy of {src} differs from it")
        out.append({"path": str(dest.relative_to(folder)), "source": str(src),
                    "sha256": digest, "bytes": str(len(data))})
    return out


def prompt_for(t: dict[str, Any]) -> str:
    intake = t.get("intake") or {}
    who = str(intake.get("Who asked for it, and who will use it?") or "the person who asked")
    options = t.get("options") or []
    opts = ("The options they named, to compare:\n"
            + "".join(f"- {o}\n" for o in options) + "\n") if options else ""
    criteria = t.get("criteria") or []
    if criteria:
        opts += ("What done means:\n"
                 + "".join(f"- C{i}: {c}\n" for i, c in enumerate(criteria, 1)) + "\n")
    lines = "".join(f"- {k} {v}\n" for k, v in intake.items())
    names = [f"{ATTACHMENTS}/{Path(a).name}" for a in t.get("attachments") or []]
    attached = (f"\nThe user attached {', '.join(names)}, in your working folder.\n"
                if names else "")
    as_of = t.get("as_of")
    cutoff = (f"Use only information published before {as_of} 00:00 UTC; treat that "
              "moment as now." if as_of else
              "Use only information published on or before {d}; treat {d} as today.".format(
                  d=datetime.now(UTC).date().isoformat()))
    return PROMPT.format(who=who, question=t["question"], options=opts,
                         intake=lines.rstrip("\n") or "- nothing else", cutoff=cutoff,
                         attached=attached)


def tools_for(t: dict[str, Any]) -> tuple[str, ...]:
    return AS_OF_TOOLS if t.get("as_of") else TOOLS


def broker_tools() -> tuple[str, ...]:
    """The evidence tools Stemma's worker holds, read from its own agent file: the as_of
    arm may call exactly these on the broker."""
    from stemma.driver import frontmatter
    from stemma.evidence.tools import TOOLS as EVIDENCE_TOOLS
    from stemma.register import roles

    path = REPO / "plugin" / "agents" / "worker.md"
    held = frontmatter.parse(path.read_text(encoding="utf-8"), str(path)).tools()
    names = {f.__name__ for f in EVIDENCE_TOOLS}
    short = (t.removeprefix(roles.MCP_PREFIX) for t in held if t.startswith(roles.MCP_PREFIX))
    return tuple(sorted(n for n in short if n in names))


def broker_denied() -> tuple[str, ...]:
    """Every other tool the broker serves (the register's, the data layer's)."""
    from stemma import mcp_server

    served = {t.name for t in mcp_server.server._tool_manager.list_tools()}
    return tuple(sorted(served - set(broker_tools())))


def broker_config(folder: Path, host_python: Path) -> dict[str, Any]:
    """The one MCP configuration of the as_of arm: Stemma's server over the folder, run
    by the repository's interpreter outside the session's sandbox, as in a Stemma run."""
    env = {"STEMMA_RUN_DIR": str(folder)}
    for k in ("STEMMA_SEC_IDENTITY", "STEMMA_CACHE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return {"mcpServers": {BROKER: {"type": "stdio", "command": str(host_python),
                                    "args": ["-I", "-m", "stemma.mcp_server"], "env": env}}}


def denied_reads(folder: Path, config: Path) -> list[str]:
    """Read-tool denials besides the allow-only-inside rule (a backstop): run 3's own
    folder, the Claude and Codex configurations and SSH keys."""
    home = Path.home()
    paths = [folder.parent.parent / "20*", config, home / ".claude", home / ".codex",
             home / ".ssh"]
    return [f"Read(/{p}/**)" for p in paths]


def settings_extra(folder: Path, config: Path, python: Path, as_of: str | None = None,
                   host_python: Path | None = None) -> dict[str, Any]:
    """The bare arm's permissions and sandbox (see the module's note); with `as_of`,
    Stemma's as_of tools and refusals, the guard run by `host_python`."""
    from stemma.driver.settings import _links_on, read_scope

    scope = read_scope(python)
    allow_read = [str(folder), *scope.allow] + _links_on([str(folder)])
    allow = ["WebSearch", "WebFetch", "Bash", f"Read(/{folder}/**)", f"Write(/{folder}/**)"]
    deny = denied_reads(folder, config)
    filesystem: dict[str, Any] = {"denyRead": list(scope.deny),
                                  "allowRead": list(dict.fromkeys(allow_read))}
    out: dict[str, Any] = {}
    if as_of:
        allow = [a for a in allow if a not in ("WebSearch", "WebFetch")]
        allow += [f"mcp__{BROKER}__{n}" for n in broker_tools()]
        deny += ["WebSearch", "WebFetch"] + [f"mcp__{BROKER}__{n}" for n in broker_denied()]
        for p in PROTECTED:
            deny += [f"Write(/{folder / p})", f"Write(/{folder / p}/**)"]
        filesystem["denyWrite"] = [str(folder / p) for p in PROTECTED]
        guard = f'"{host_python or sys.executable}" -I -m stemma.hooks pre'
        out["hooks"] = {"PreToolUse": [{"matcher": "WebSearch|WebFetch", "hooks": [
            {"type": "command", "command": guard, "timeout": 30, "onFailure": "block"}]}]}
    return {
        "permissions": {"allow": allow, "deny": deny},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "network": {"allowedDomains": []},
            "filesystem": filesystem,
        },
        **out,
    }


def _pinned() -> tuple[Path, Path]:
    env = {k: os.environ.get(k, "") for k in ("STEMMA_CLAUDE_BIN", "STEMMA_CLAUDE_CONFIG")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise BareError(f"{' and '.join(missing)} must name the pinned harness and its clean "
                        f"configuration")
    return Path(env["STEMMA_CLAUDE_BIN"]).expanduser(), Path(env["STEMMA_CLAUDE_CONFIG"]).expanduser()


def _write(path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")


def _stop(proc: Any) -> int | None:
    """Terminate, then kill, reaping with bounded waits (resource limit: STOP_GRACE_S)."""
    for signal_it in (proc.terminate, proc.kill):
        try:
            signal_it()
        except (OSError, AttributeError):
            pass
        try:
            return int(proc.wait(timeout=STOP_GRACE_S))
        except (subprocess.TimeoutExpired, TypeError):
            continue
    return None


def _session(folder: Path, prompt: str, name: str, binary: Path, config: Path,
             version: str, settings: dict[str, Any], popen: Any,
             max_s: float, tools: tuple[str, ...] = TOOLS,
             mcp_config: Path | None = None) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """One headless session in `folder`: (outcome, result message, the whole stream).
    The init message's canary is checked first; a failed canary stops the session. A
    reader thread hands lines over, so the start-up deadline (INIT_TIMEOUT_S) and the
    session deadline (`max_s`) stop a silent or stuck child too. Its launch and end go
    to `events.jsonl`, its stream to `.stemma/sessions/<name>.stream.jsonl`, and a copy
    of its transcript to `transcripts/` (see the module's note)."""
    import queue
    import threading
    import uuid

    from stemma.driver import events
    from stemma.driver.clean import bare_expectation, check_canary, clean_launch

    sid = str(uuid.uuid4())
    tmp = folder / SESSION_TMP / sid
    tmp.mkdir(parents=True, exist_ok=True)
    env_extra = {"CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
                 "CLAUDE_CODE_TMPDIR": str(tmp)}
    if mcp_config is not None:
        env_extra["STEMMA_RUN_DIR"] = str(folder)  # the guard's: whose run.json to read
    cl = clean_launch(binary, config, prompt, cwd=folder, model=MODEL, effort=EFFORT,
                      tools=list(tools), settings_path=folder / ".settings.json",
                      settings_extra=settings, env_extra=env_extra,
                      strict_mcp_config=True, disable_slash_commands=True,
                      mcp_config=mcp_config, session_id=sid)
    stream_path = folder / SESSIONS / f"{name}.stream.jsonl"
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    outcome: dict[str, Any] = {"session": name, "session_id": sid, "canary": None,
                               "exit_code": None}
    result: dict[str, Any] | None = None
    init: dict[str, Any] | None = None
    seen: list[str] = []
    lines: queue.Queue[str | None] = queue.Queue()
    events.emit(folder, "session_launched", session=name, role=SESSION_NAME,
                session_id=sid, model=MODEL, effort=EFFORT, launcher="bare")
    with open(stream_path, "w", encoding="utf-8") as stream:
        proc = popen(cl.argv, env=cl.env, cwd=folder, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

        def pump() -> None:
            try:
                for ln in proc.stdout:
                    lines.put(ln)
            finally:
                lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        ended = False
        while not ended:
            now = time.monotonic() - started
            if outcome["canary"] is None and now > INIT_TIMEOUT_S:
                outcome["stopped"] = f"no init message within {INIT_TIMEOUT_S} s"
                break
            if now > max_s:
                outcome["stopped"] = f"ran past {max_s:.0f} s (a resource limit)"
                break
            try:
                line = lines.get(timeout=POLL_S)
            except queue.Empty:
                continue
            if line is None:
                ended = True
                break
            stream.write(line)
            stream.flush()
            seen.append(line)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "system" and msg.get("subtype") == "init" \
                    and outcome["canary"] is None:
                init = msg
                problems = check_canary(msg, bare_expectation(
                    cwd=folder, tools=list(tools), harness_version=version,
                    session_id=sid, mcp_server=BROKER if mcp_config is not None else None))
                if msg.get("session_id") != sid:
                    problems = [*problems, (f"the session id is {msg.get('session_id')!r}, "
                                            f"not the {sid!r} it was launched with")]
                outcome["canary"] = problems or "passed"
                if problems:
                    break
            elif msg.get("type") == "result":
                result = msg
        if ended:
            try:
                outcome["exit_code"] = int(proc.wait(timeout=STOP_GRACE_S))
            except subprocess.TimeoutExpired:
                outcome["exit_code"] = _stop(proc)
                outcome["stopped"] = (f"the process did not exit within {STOP_GRACE_S:.0f} s "
                                      f"of its output ending, and was stopped")
        else:
            outcome["exit_code"] = _stop(proc)
            outcome.setdefault("stopped", "stopped by the launcher")
    outcome["wall_s"] = round(time.monotonic() - started, 1)
    outcome["transcript"] = keep_transcript(folder, config, init, sid)
    events.emit(folder, "session_ended", session=name, session_id=sid,
                exit_code=outcome["exit_code"], stopped=outcome.get("stopped"),
                wall_s=outcome["wall_s"], transcript=outcome["transcript"], launcher="bare")
    return outcome, result, "".join(seen)


def keep_transcript(folder: Path, config: Path, init: dict[str, Any] | None,
                    sid: str) -> str | None:
    """Copy the session's transcript, and its subagents' if any, into the folder; the
    copy's path, or None when the harness wrote none that can be found."""
    from stemma.driver.verify import find_transcript

    found = find_transcript(config, folder, init, sid)
    if found is None:
        return None
    dest = folder / TRANSCRIPTS / f"{sid}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(found, dest)
    subs = found.with_suffix("")
    if subs.is_dir():
        shutil.copytree(subs, folder / TRANSCRIPTS / sid, dirs_exist_ok=True)
    return str(dest.relative_to(folder))


def eligible(outcome: dict[str, Any], result: dict[str, Any] | None) -> list[str]:
    """Why a session's answer may not be scored (empty: it may): a clean start, a
    natural exit 0, no stop, a successful result that is not an error, and an answer
    with text."""
    why = []
    if outcome.get("canary") != "passed":
        why.append(f"the init canary did not pass ({outcome.get('canary')})")
    if outcome.get("stopped"):
        why.append(f"the session was stopped: {outcome['stopped']}")
    if outcome.get("exit_code") != 0:
        why.append(f"the session exited {outcome.get('exit_code')}")
    if result is None:
        why.append("the session gave no result")
    else:
        if result.get("subtype") != "success":
            why.append(f"its result is {result.get('subtype')!r}, not 'success'")
        if result.get("is_error") is not False:
            why.append("its result is marked as an error")
        if not isinstance(result.get("result"), str) or not result["result"].strip():
            why.append("its answer is empty")
    return why


def _messages(stream: str) -> list[dict[str, Any]]:
    out = []
    for line in stream.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict):
            out.append(msg)
    return out


def _blocks(stream: str, kind: str) -> list[dict[str, Any]]:
    out = []
    for msg in _messages(stream):
        message = msg.get("message")  # a string on some system lines (permission_denied)
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == kind:
                out.append(block)
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text") if isinstance(c, dict) else c) for c in content)
    return "" if content is None else str(content)


def scope_problems(stream: str, marker: Path, token: str, control: Path,
                   control_token: str, as_of: str | None = None) -> list[str]:
    """What the scope canary's stream shows went wrong (empty: the scope held). Exact
    facts only: each of the six calls was made and has its result; the three reaches
    outside (the marker by Read and by Bash, the network by Bash) came back refused, and
    nothing from outside appears; the three controls succeeded (the control file inside
    the folder read by Read and by Bash, the page reached by WebFetch, or with `as_of` an
    archived copy of it captured by the broker), so a refusal is the confinement's and
    not a broken tool or a network down."""
    results = {b.get("tool_use_id"): b for b in _blocks(stream, "tool_result")}
    uses = _blocks(stream, "tool_use")

    def find(name: str, want: str) -> dict[str, Any] | None:
        for u in uses:
            if u.get("name") == name and want in json.dumps(u.get("input"), sort_keys=True):
                return u
        return None

    steps = [("Read", str(marker), "the Read tool on the marker", "refused"),
             ("Bash", f"cat {marker}", "cat on the marker in Bash", "refused"),
             ("Bash", "curl", "the network from Bash", "refused"),
             ("Read", str(control), "the Read tool on the control file", control_token),
             ("Bash", f"cat {control}", "cat on the control file in Bash", control_token),
             ("WebFetch", CANARY_PAGE, "WebFetch on the page", "reached")
             if not as_of else
             (f"mcp__{BROKER}__capture", CANARY_PAGE, "the broker's capture of the page",
              "archived")]
    problems = []
    for name, want, what, expect in steps:
        use = find(name, want)
        if use is None:
            problems.append(f"the canary did not try {what}")
            continue
        res = results.get(use.get("id"))
        if res is None:
            problems.append(f"{what} has no result in the stream")
            continue
        error, text = res.get("is_error") is True, _text(res.get("content"))
        if expect == "refused" and not error:
            problems.append(f"{what} was not refused")
        elif expect == "reached" and error:
            problems.append(f"{what} failed, so a refused curl proves nothing")
        elif expect == "archived" and (error or not _archived(text, as_of or "")):
            problems.append(f"{what} did not return an archived copy, so a refused curl "
                            "proves nothing")
        elif expect not in ("refused", "reached", "archived") and (
                error or expect not in text):
            problems.append(f"{what} did not return the control's token: the tool or the "
                            "folder is broken, so a refusal proves nothing")
        if name == "Bash" and want == "curl" and CANARY_PAGE_TEXT in text:
            problems.append("the page reached from Bash appears in its result: Bash has "
                            "network")
    if token in stream:
        problems.append("the marker's token appears in the stream: a read outside the "
                        "folder succeeded")
    return problems


def _archived(text: str, as_of: str) -> bool:
    """Whether a capture's receipt says the broker reached an archived copy dated before
    `as_of` and stored its bytes. Exact facts, not the extraction's verdict: a short page
    (example.com) is stored with status short_text, and that still proves the reach."""
    try:
        receipt = json.loads(text)
    except ValueError:
        return False
    if not isinstance(receipt, dict) or receipt.get("kind") != "archive":
        return False
    archive = receipt.get("archive")
    served = archive.get("served_timestamp") if isinstance(archive, dict) else None
    cutoff = as_of.replace("-", "") + "000000"
    return (bool(receipt.get("sha256")) and isinstance(receipt.get("bytes_len"), int)
            and receipt["bytes_len"] > 0 and isinstance(served, str)
            and len(served) == 14 and served.isdigit() and served < cutoff)


def canary_finished(outcome: dict[str, Any], result: dict[str, Any] | None) -> list[str]:
    """The canary session must itself have ended cleanly, or its stream proves nothing."""
    why = []
    if outcome.get("canary") != "passed":
        why.append(f"init canary: {outcome.get('canary')}")
    if outcome.get("stopped"):
        why.append(f"the canary session was stopped: {outcome['stopped']}")
    if outcome.get("exit_code") != 0:
        why.append(f"the canary session exited {outcome.get('exit_code')}")
    if result is None or result.get("subtype") != "success" or result.get("is_error"):
        why.append("the canary session gave no successful result")
    return why


def launch(root: Path, t: dict[str, Any], popen: Any = subprocess.Popen,
           version: str | None = None, max_s: float = SESSION_MAX_S,
           canary_only: bool = False) -> Path:
    """The scope canary, then one bare session, in a new folder under `root`; returns
    the folder. Either canary failing refuses the arm, and nothing it wrote is used.
    `canary_only` stops after the scope canary: the task is never sent to a model."""
    import secrets

    from stemma.driver.config import DriverConfig, default_plugin_src
    from stemma.driver.launch import harness_version
    from stemma.driver.runfolder import new_run_id

    binary, config = _pinned()
    stamp = new_run_id()  # a run id's form, which the leakage audit knows as run-time
    folder = Path(root).expanduser() / stamp
    folder.mkdir(parents=True, exist_ok=False)
    folder = folder.resolve()
    prompt = prompt_for(t)
    _write(folder / "prompt.txt", prompt)
    attachments = copy_attachments(folder, list(t.get("attachments") or []))
    python = SCOPE_PYTHON if SCOPE_PYTHON.exists() else Path(sys.executable)
    if version is None:
        version = harness_version(DriverConfig(folder, binary, config, python,
                                               default_plugin_src()))
    as_of = t.get("as_of") or None
    tools = tools_for(t)
    settings = settings_extra(folder, config, python, as_of, Path(sys.executable))
    mcp_config: Path | None = None
    if as_of:
        _write(folder / "run.json", json.dumps({
            "run_id": stamp, "as_of": as_of, "arm": "bare", "attachments": attachments,
            "harness": {"binary": str(binary), "version": version,
                        "config_dir": str(config)}}))
        mcp_config = folder / ".mcp.json"
        _write(mcp_config, json.dumps(broker_config(folder, Path(sys.executable)), indent=1))
    _write(folder / "launch.json", json.dumps({
        "model": MODEL, "effort": EFFORT, "tools": list(tools), "harness": version,
        "as_of": as_of, "broker_tools": list(broker_tools()) if as_of else [],
        "settings_extra": settings, "task": t, "started_at": stamp, "max_s": max_s,
        "canary_only": canary_only, "attachments": attachments}, indent=1))

    token, control_token = secrets.token_hex(16), secrets.token_hex(16)
    marker = folder.parent / f".scope-canary-{stamp}-{secrets.token_hex(4)}.txt"
    marker.write_text(f"{token}\n", encoding="utf-8")
    control = folder / ".scope-control.txt"
    control.write_text(f"{control_token}\n", encoding="utf-8")
    try:
        step = (CANARY_BROKER if as_of else CANARY_WEBFETCH).format(page=CANARY_PAGE)
        c_out, c_res, c_stream = _session(
            folder, CANARY_PROMPT.format(marker=marker, control=control, page=CANARY_PAGE,
                                         network_step=step),
            CANARY_NAME, binary, config, version, settings, popen, CANARY_MAX_S,
            tools, mcp_config)
    finally:
        marker.unlink(missing_ok=True)
        control.unlink(missing_ok=True)
    problems = (canary_finished(c_out, c_res)
                + scope_problems(c_stream, marker, token, control, control_token, as_of))
    if not c_out.get("transcript"):
        problems.append(f"the canary's transcript was not found under {config}, so the "
                        "leakage audit could not read this arm's sessions")
    _write(folder / "scope-canary.json", json.dumps({**c_out, "problems": problems},
                                                    indent=1))
    if problems:
        raise BareError("the scope canary failed, so the bare session was not started: "
                        + "; ".join(problems))
    if canary_only:
        _write(folder / "outcome.json", json.dumps(
            {"canary_only": True, "scope_canary": "passed",
             "canary_transcript": c_out.get("transcript"), "eligible": False,
             "not_eligible_because": ["canary only: the task was not sent"]}, indent=1))
        return folder

    outcome, result, _stream = _session(folder, prompt, SESSION_NAME, binary, config,
                                        version, settings, popen, max_s, tools,
                                        mcp_config)
    why = eligible(outcome, result)
    if not outcome.get("transcript"):
        why.append("its transcript was not found, so the leakage audit could not be "
                   "complete")
    outcome["eligible"] = not why
    outcome["not_eligible_because"] = why
    if why or result is None:
        _write(folder / "outcome.json", json.dumps(outcome, indent=1))
        raise BareError("the bare answer is not eligible for scoring, and no answer.md "
                        "was written: " + "; ".join(why))
    _write(folder / "answer.md", result["result"])
    _write(folder / "usage.json", json.dumps({
        k: result.get(k) for k in ("usage", "modelUsage", "total_cost_usd", "duration_ms",
                                   "num_turns", "is_error", "subtype")}, indent=1))
    outcome["result"] = {"subtype": result.get("subtype"), "is_error": result.get("is_error")}
    _write(folder / "outcome.json", json.dumps(outcome, indent=1))
    return folder


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hackathon.headtohead.bare",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--question-file", type=Path, default=None)
    ap.add_argument("--task", type=Path, default=None,
                    help="the task.json the Stemma arm is given (run 4); replaces run 3's")
    ap.add_argument("--launcher", type=Path, default=None,
                    help="a run's launcher (tests/e2e/run4_demo.py): its pre-registered task")
    ap.add_argument("--max-s", type=float, default=SESSION_MAX_S,
                    help="the real session's wall-time limit in seconds (run 4: 9000)")
    ap.add_argument("--canary-only", action="store_true",
                    help="start, run the scope canary, and stop; the task is never sent")
    ap.add_argument("--out", type=Path, default=None,
                    help="folder the new run folder is created in (outside the repository); "
                         "required, with no default")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt and the launch; create nothing")
    args = ap.parse_args(argv)
    try:
        given = [a for a in (args.task, args.launcher, args.question_file) if a is not None]
        if len(given) > 1:
            raise BareError("give one of --task, --launcher or --question-file")
        if args.out is None:
            ap.error("the following arguments are required: --out")
        t = (task_from_file(args.task) if args.task is not None else
             task_from_launcher(args.launcher) if args.launcher is not None else
             task(args.question_file))
        if args.dry_run:
            print(prompt_for(t))
            folder = Path(args.out).expanduser() / "<new run folder>"
            config = Path(os.environ.get("STEMMA_CLAUDE_CONFIG") or "<STEMMA_CLAUDE_CONFIG>")
            as_of = t.get("as_of") or None
            print(json.dumps({"model": MODEL, "effort": EFFORT, "tools": list(tools_for(t)),
                              "as_of": as_of,
                              "settings_extra": settings_extra(
                                  folder, config,
                                  SCOPE_PYTHON if SCOPE_PYTHON.exists() else
                                  Path(sys.executable), as_of, Path(sys.executable)),
                              "mcp_config": broker_config(folder, Path(sys.executable))
                              if as_of else None,
                              "max_s": args.max_s, "out": str(args.out)}, indent=1))
            return 0
        folder = launch(args.out, t, max_s=args.max_s, canary_only=args.canary_only)
    except BareError as exc:
        print(f"bare arm: {exc}", file=sys.stderr)
        return 2
    if args.canary_only:
        print(f"bare arm: the scope canary passed; stopped before the task, in {folder}")
    else:
        print(f"bare arm: answer and usage in {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
