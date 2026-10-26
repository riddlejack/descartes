"""A fake of the pinned harness for driver tests. No network, no model.

Installed as an executable script (see conftest). It records its argv, environment and
working folder to `<its folder>/calls/<n>.json`, prints a stream-json init built the way
the harness builds it (the agent's own tools within --tools, agents and skills from the plugin folder),
writes a transcript under CLAUDE_CONFIG_DIR, and prints a result. Behaviour per role
comes from `<its folder>/fake-config.json`: {"<role>" or "<role>@resume": {...}} with
keys init_overrides, sleep_s, exit_code, transcript_model, transcript_effort, subagents,
no_init, compact, write_files.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def opt(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("2.1.308 (Claude Code)")
        return 0
    start = time.time()
    agent = opt(argv, "--agent") or ""
    role = agent.split(":", 1)[-1]
    resume = opt(argv, "--resume")
    sid = resume or opt(argv, "--session-id") or str(uuid.uuid4())
    cfg_all = {}
    if (HERE / "fake-config.json").exists():
        cfg_all = json.loads((HERE / "fake-config.json").read_text())
    cfg = dict(cfg_all.get(role, {}))
    if resume and f"{role}@resume" in cfg_all:
        cfg = dict(cfg_all[f"{role}@resume"])
    calls = HERE / "calls"
    calls.mkdir(exist_ok=True)
    rec_path = calls / f"{time.time_ns()}-{os.getpid()}.json"
    record = {"argv": argv, "env": dict(os.environ), "cwd": os.getcwd(), "start": start,
              "role": role, "session_id": sid}
    rec_path.write_text(json.dumps(record))

    def out(msg: dict[str, object]) -> None:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    if cfg.get("no_init"):
        sys.stderr.write("Not logged in\n")
        sys.stdout.write("this is not json\n")
        return 1

    plugin_arg = opt(argv, "--plugin-dir")
    plugin = Path(plugin_arg or "/nonexistent")
    tools = [t for t in (opt(argv, "--tools") or "").split(",") if t]
    # Like the harness under --agent: the agent's own frontmatter tools within --tools.
    own: list[str] = []
    agent_file = plugin / "agents" / f"{role}.md"
    if agent_file.is_file():
        for line in agent_file.read_text().splitlines():
            if line.startswith("tools:"):
                own = [t.strip() for t in line[len("tools:"):].split(",") if t.strip()]
    own_names = {t.split("(", 1)[0] for t in own}
    if not agent:
        own_names = set(tools)  # no --agent: the whole --tools list
    shown = ["Task" if t == "Agent" else t for t in tools if t in own_names]
    shown += [t for t in own if t.startswith("mcp__")]
    agents = sorted(f"stemma:{p.stem}" for p in (plugin / "agents").glob("*.md"))
    skills = sorted(f"stemma:{p.name}" for p in (plugin / "skills").glob("*")
                    if (p / "SKILL.md").is_file())
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    cwd = os.getcwd()
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    proj = config / "projects" / slug
    init: dict[str, object] = {
        "type": "system", "subtype": "init", "cwd": cwd, "session_id": sid,
        "tools": shown, "mcp_servers": [{"name": "plugin:stemma:stemma", "status": "connected",
                                         "source": "plugin"}],
        "model": opt(argv, "--model"), "claude_code_version": "2.1.308",
        "agents": agents, "skills": skills,
        "plugins": [{"name": "stemma", "path": str(plugin), "source": "stemma@inline"}],
        "memory_paths": {"auto": str(proj / "memory") + "/"},
    }
    if plugin_arg is None:  # a bare session: nothing from a plugin
        init.update({"plugins": [], "mcp_servers": [], "agents": [], "skills": []})
    init.update(cfg.get("init_overrides", {}))
    out(init)

    model = cfg.get("transcript_model", opt(argv, "--model"))
    effort = cfg.get("transcript_effort", opt(argv, "--effort"))
    proj.mkdir(parents=True, exist_ok=True)
    with open(proj / f"{sid}.jsonl", "a") as t:
        t.write(json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}) + "\n")
        t.write(json.dumps({"type": "assistant", "effort": effort,
                            "message": {"model": model, "content": []}}) + "\n")
        t.write(json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}) + "\n")
    for n, sub in enumerate(cfg.get("subagents", [])):
        d = proj / sid / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        aid = f"a{n}{os.getpid()}"
        (d / f"agent-{aid}.jsonl").write_text(json.dumps(
            {"type": "assistant", "effort": sub["effort"],
             "message": {"model": sub["model"], "content": []}}) + "\n")
        (d / f"agent-{aid}.meta.json").write_text(json.dumps(
            {"agentType": sub["agentType"], "spawnDepth": sub.get("depth", 1)}))
    for rel, text in cfg.get("write_files", {}).items():
        p = Path(cwd) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    if cfg.get("compact"):
        out({"type": "system", "subtype": "compact_boundary",
             "compact_metadata": {"trigger": "auto", "pre_tokens": 1}})
    time.sleep(float(cfg.get("sleep_s", 0)))
    out({"type": "assistant", "message": {"model": model, "content": [
        {"type": "text", "text": "ok"}]}, "session_id": sid})
    code = int(cfg.get("exit_code", 0))
    out({"type": "result", "subtype": "success" if code == 0 else "error_during_execution",
         "is_error": code != 0, "result": cfg.get("result", "done"), "session_id": sid,
         "num_turns": 1, "permission_denials": []})
    record["end"] = time.time()
    rec_path.write_text(json.dumps(record))
    return code


if __name__ == "__main__":
    sys.exit(main())
