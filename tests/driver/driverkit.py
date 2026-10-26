"""Driver test kit: a fake harness binary, an invented plugin source, a driver config."""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from stemma.driver.config import DriverConfig, Limits
from stemma.driver.task import SWITCH_VALUES

HERE = Path(__file__).resolve().parent


@dataclass
class Fake:
    path: Path

    def configure(self, cfg: dict[str, Any]) -> None:
        (self.path.parent / "fake-config.json").write_text(json.dumps(cfg))

    def calls(self) -> list[dict[str, Any]]:
        d = self.path.parent / "calls"
        if not d.is_dir():
            return []
        return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return Path(os.path.realpath(tmp_path))


@pytest.fixture
def fake(tmp: Path) -> Fake:
    d = tmp / "fakebin"
    d.mkdir()
    p = d / "claude"
    p.write_text(f"#!{sys.executable}\n" + (HERE / "fake_claude.py").read_text())
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return Fake(p)


AGENT = """---
name: {name}
description: Invented role for driver tests.
model: claude-fable-5-1
effort: low
tools: {tools}
---
You are an invented test role. Reply ok.
"""


def write_plugin(root: Path, *, worker_base: bool = True) -> Path:
    """An invented plugin source with every fragment, three skills and five roles."""
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "stemma", "version": "0.0.0-test", "description": "test fixture"}))
    (root / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"stemma": {"command": "${STEMMA_PYTHON}", "args": ["-c", "pass"]}}}))
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}))
    (root / "STANDING-RULES.md").write_text("# Standing rules (fixture)\n\nRule one.\n")
    (root / "lead").mkdir()
    (root / "lead" / "base.md").write_text(AGENT.format(
        name="lead", tools="Read, Write, Bash, Agent(stemma:worker), Skill, "
                           "mcp__plugin_stemma_stemma__ping"))
    (root / "agents").mkdir()
    for sw, values in SWITCH_VALUES.items():
        for v in values:
            text = "" if v in ("off", "false") else f"LEAD-FRAGMENT {sw}={v}\n"
            (root / "lead" / f"{sw}.{v}.md").write_text(text)
            if worker_base:
                (root / "agents" / f"worker.{sw}.{v}.md").write_text(f"WORKER-FRAGMENT {sw}={v}\n")
    if worker_base:
        (root / "agents" / "worker.base.md").write_text(AGENT.format(
            name="worker", tools="Read, WebSearch, mcp__plugin_stemma_stemma__file_claim"))
    for role, tools in (("blind-framer", "Read, Bash"),
                        ("claim-checker", "Read, Bash, mcp__plugin_stemma_stemma__get_claim"),
                        ("deciders-eye-reviewer", "Read, Write")):
        (root / "agents" / f"{role}.md").write_text(AGENT.format(name=role, tools=tools))
    # Real and placebo versions share frontmatter names (contract v0.2.2, section 7).
    for folder, name, body in (("decomposition-method", "method", "REAL METHOD"),
                               ("placebo-method", "method", "PLACEBO METHOD"),
                               ("card-decision", "card-decision", "REAL CARD"),
                               ("placebo-card-decision", "card-decision", "PLACEBO CARD"),
                               ("deck", "deck", "OTHER SKILL")):
        (root / "skills" / folder).mkdir(parents=True)
        (root / "skills" / folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture\n---\n{body}\n")
    return root


@pytest.fixture
def plugin_src(tmp: Path) -> Path:
    return write_plugin(tmp / "plugin-src")


@pytest.fixture
def cfg(tmp: Path, fake: Fake, plugin_src: Path) -> DriverConfig:
    (tmp / "config").mkdir()
    return DriverConfig(runs_dir=tmp / "runs", claude_bin=fake.path,
                        claude_config=tmp / "config", python=Path(sys.executable),
                        plugin_src=plugin_src)


# Resource limits shrunk so tests finish quickly.
FAST = Limits(max_concurrent_checks=2, poll_interval_s=0.05, init_timeout_s=5.0,
              check_max_s=60.0, lead_max_s=60.0, stop_grace_s=2.0)


@pytest.fixture
def limits() -> Limits:
    return FAST


def write_task(tmp: Path, **extra: Any) -> Path:
    p = tmp / "task.json"
    p.write_text(json.dumps({"question": "Should the invented firm enter market X?", **extra}))
    return p
