"""The bare arm of run 3's head-to-head (pre-registration, hypothesis H5).

Claude Code with web search and fetch, no Stemma plugin, Opus 5.5 at high effort, one
headless session, through the driver's own `clean_launch` (the pinned binary, the clean
configuration, nothing synced, no memory, no MCP server, no slash commands), its init
message checked by the canary before anything it says is used. It is given exactly run
3's question, named options, onboarding answers and as-of date, read from the run's own
launcher, `tests/e2e/run3_demo.py` (with `--question-file` replacing the question the
same way that launcher does).

    python -m hackathon.headtohead.bare [--question-file Q.json] [--dry-run] [--out ROOT]

It writes one new folder under ROOT (default `~/.private/runs/stemma2-run3/bare/`):
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
Read tool and with Bash, and to reach the network from Bash; code requires that it tried
all three and that neither the marker's random token nor the page reached appears
anywhere in its stream. Otherwise the bare arm is refused and the real session never
starts.

Stated deviation from a plain user: the prompt asks the bare arm to cite a source for
every figure (a plain user may not ask); without it the citation counts would measure
nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[2]
RUN3_LAUNCHER: Final = REPO / "tests" / "e2e" / "run3_demo.py"
DEFAULT_ROOT: Final = Path.home() / ".private" / "runs" / "stemma2-run3" / "bare"
MODEL: Final = "claude-opus-5-5"  # the pre-registration: Opus 5.5 at high effort
EFFORT: Final = "high"
TOOLS: Final = ("WebSearch", "WebFetch", "Bash", "Read", "Write")  # see the module's note
# The scope canary's own resource limit: seconds it may run.
CANARY_MAX_S: Final = 600
CANARY_PAGE: Final = "https://example.com"
CANARY_PAGE_TEXT: Final = "Example Domain"  # what that page says, were it reached
CANARY_PROMPT: Final = """\
This is a safety check of your tools, not a task. Do exactly these three things, in
order, and report what each one returned:
1. Use the Read tool to read the file {marker}
2. Use the Bash tool to run: cat {marker}
3. Use the Bash tool to run: curl -sS --max-time 10 {page}
Then reply with the word DONE.
"""
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

Scope: answer only this question. Research nothing outside it; say briefly, at the end,
what you did not research that a reader might want.

Use only information published on or before {as_of}; treat {as_of} as today.

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


def prompt_for(t: dict[str, Any]) -> str:
    intake = t.get("intake") or {}
    who = str(intake.get("Who asked for it, and who will use it?") or "the person who asked")
    options = t.get("options") or []
    opts = ("The options they named, to compare:\n"
            + "".join(f"- {o}\n" for o in options) + "\n") if options else ""
    lines = "".join(f"- {k} {v}\n" for k, v in intake.items())
    return PROMPT.format(who=who, question=t["question"], options=opts,
                         intake=lines.rstrip("\n") or "- nothing else",
                         as_of=t.get("as_of") or datetime.now(UTC).date().isoformat())


def denied_reads(folder: Path, config: Path) -> list[str]:
    """Read-tool denials besides the allow-only-inside rule (a backstop): run 3's own
    folder, the Claude and Codex configurations and SSH keys."""
    home = Path.home()
    paths = [folder.parent.parent / "20*", config, home / ".claude", home / ".codex",
             home / ".ssh"]
    return [f"Read(/{p}/**)" for p in paths]


def settings_extra(folder: Path, config: Path, python: Path) -> dict[str, Any]:
    """The bare arm's permissions and sandbox (see the module's note)."""
    from stemma.driver.settings import _links_on, read_scope

    scope = read_scope(python)
    allow_read = [str(folder), *scope.allow] + _links_on([str(folder)])
    return {
        "permissions": {
            "allow": ["WebSearch", "WebFetch", "Bash", f"Read(/{folder}/**)",
                      f"Write(/{folder}/**)"],
            "deny": denied_reads(folder, config),
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "network": {"allowedDomains": []},
            "filesystem": {"denyRead": list(scope.deny),
                           "allowRead": list(dict.fromkeys(allow_read))},
        },
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


def _session(folder: Path, prompt: str, stream_name: str, binary: Path, config: Path,
             version: str, settings: dict[str, Any], popen: Any,
             max_s: float) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """One headless session in `folder`: (outcome, result message, the whole stream).
    The init message's canary is checked first; a failed canary stops the session."""
    from stemma.driver.clean import bare_expectation, check_canary, clean_launch

    cl = clean_launch(binary, config, prompt, cwd=folder, model=MODEL, effort=EFFORT,
                      tools=list(TOOLS), settings_path=folder / ".settings.json",
                      settings_extra=settings,
                      env_extra={"CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
                                 "CLAUDE_CODE_TMPDIR": str(folder / ".tmp")},
                      strict_mcp_config=True, disable_slash_commands=True)
    (folder / ".tmp").mkdir(exist_ok=True)
    started = time.monotonic()
    outcome: dict[str, Any] = {"canary": None, "exit_code": None}
    result: dict[str, Any] | None = None
    seen: list[str] = []
    with open(folder / stream_name, "w", encoding="utf-8") as stream:
        proc = popen(cl.argv, env=cl.env, cwd=folder, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            for line in proc.stdout:
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
                    problems = check_canary(msg, bare_expectation(
                        cwd=folder, tools=list(TOOLS), harness_version=version,
                        session_id=msg.get("session_id") if isinstance(
                            msg.get("session_id"), str) else None))
                    outcome["canary"] = problems or "passed"
                    if problems:
                        proc.kill()
                        break
                elif msg.get("type") == "result":
                    result = msg
                if time.monotonic() - started > max_s:
                    outcome["stopped"] = f"ran past {max_s:.0f} s (a resource limit)"
                    proc.kill()
                    break
        finally:
            outcome["exit_code"] = proc.wait()
    outcome["wall_s"] = round(time.monotonic() - started, 1)
    return outcome, result, "".join(seen)


def _tool_uses(stream: str) -> list[dict[str, Any]]:
    out = []
    for line in stream.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = ((msg.get("message") or {}).get("content") if isinstance(msg, dict)
                   else None)
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out.append(block)
    return out


def scope_problems(stream: str, marker: Path, token: str) -> list[str]:
    """What the scope canary's stream shows went wrong (empty: the scope held). Exact
    facts only: the attempts were made, and nothing outside the folder came back."""
    uses = _tool_uses(stream)
    inputs = [json.dumps(u.get("input"), sort_keys=True) for u in uses]
    problems = []
    if not any(u.get("name") == "Read" and str(marker) in i for u, i in zip(uses, inputs,
                                                                           strict=True)):
        problems.append("the canary did not try the Read tool on the marker")
    if not any(u.get("name") == "Bash" and "cat" in i and str(marker) in i
               for u, i in zip(uses, inputs, strict=True)):
        problems.append("the canary did not try cat on the marker in Bash")
    if not any(u.get("name") == "Bash" and "curl" in i for u, i in zip(uses, inputs,
                                                                      strict=True)):
        problems.append("the canary did not try the network from Bash")
    if token in stream:
        problems.append("the marker's token appears in the stream: a read outside the "
                        "folder succeeded")
    if CANARY_PAGE_TEXT in stream:
        problems.append("the page reached from Bash appears in the stream: Bash has network")
    return problems


def launch(root: Path, t: dict[str, Any], popen: Any = subprocess.Popen,
           version: str | None = None) -> Path:
    """The scope canary, then one bare session, in a new folder under `root`; returns
    the folder. Either canary failing refuses the arm, and nothing it wrote is used."""
    import secrets

    from stemma.driver.config import DriverConfig, default_plugin_src
    from stemma.driver.launch import harness_version

    binary, config = _pinned()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    folder = Path(root).expanduser() / stamp
    folder.mkdir(parents=True, exist_ok=False)
    folder = folder.resolve()
    prompt = prompt_for(t)
    _write(folder / "prompt.txt", prompt)
    python = SCOPE_PYTHON if SCOPE_PYTHON.exists() else Path(sys.executable)
    if version is None:
        version = harness_version(DriverConfig(folder, binary, config, python,
                                               default_plugin_src()))
    settings = settings_extra(folder, config, python)
    _write(folder / "launch.json", json.dumps({
        "model": MODEL, "effort": EFFORT, "tools": list(TOOLS), "harness": version,
        "settings_extra": settings, "task": t, "started_at": stamp}, indent=1))

    token = secrets.token_hex(16)
    marker = folder.parent / f".scope-canary-{stamp}-{secrets.token_hex(4)}.txt"
    marker.write_text(f"{token}\n", encoding="utf-8")
    try:
        c_out, _c_res, c_stream = _session(
            folder, CANARY_PROMPT.format(marker=marker, page=CANARY_PAGE),
            "scope-canary.jsonl", binary, config, version, settings, popen, CANARY_MAX_S)
    finally:
        marker.unlink(missing_ok=True)
    problems = ([f"init canary: {c_out['canary']}"] if c_out["canary"] != "passed"
                else scope_problems(c_stream, marker, token))
    _write(folder / "scope-canary.json", json.dumps({**c_out, "problems": problems},
                                                    indent=1))
    if problems:
        raise BareError("the scope canary failed, so the bare session was not started: "
                        + "; ".join(problems))

    outcome, result, _stream = _session(folder, prompt, "stream.jsonl", binary, config,
                                        version, settings, popen, SESSION_MAX_S)
    if outcome["canary"] != "passed":
        _write(folder / "outcome.json", json.dumps(outcome, indent=1))
        raise BareError(f"the session did not start clean, so nothing it wrote is used: "
                        f"{outcome['canary']}")
    if result is None or not isinstance(result.get("result"), str):
        _write(folder / "outcome.json", json.dumps(outcome, indent=1))
        raise BareError("the session ended without an answer; see stream.jsonl")
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
    ap.add_argument("--out", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt and the launch; create nothing")
    args = ap.parse_args(argv)
    try:
        t = task(args.question_file)
        if args.dry_run:
            print(prompt_for(t))
            folder = Path(args.out).expanduser() / "<new run folder>"
            config = Path(os.environ.get("STEMMA_CLAUDE_CONFIG") or "<STEMMA_CLAUDE_CONFIG>")
            print(json.dumps({"model": MODEL, "effort": EFFORT, "tools": list(TOOLS),
                              "settings_extra": settings_extra(
                                  folder, config,
                                  SCOPE_PYTHON if SCOPE_PYTHON.exists() else
                                  Path(sys.executable)),
                              "out": str(args.out)}, indent=1))
            return 0
        folder = launch(args.out, t)
    except BareError as exc:
        print(f"bare arm: {exc}", file=sys.stderr)
        return 2
    print(f"bare arm: answer and usage in {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
