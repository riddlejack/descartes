"""Assemble the per-run plugin folder from the repo `plugin/` (CONTRACTS section 7).

The run plugin holds only what the switches select. The lead's text is `lead/base.md`
followed by one fragment per switch; the worker's likewise when `agents/worker.base.md`
exists. s1 selects the real method and cards, the placebo method and cards, or neither;
a skill not copied cannot load. Every agent file's `model` and `effort` are set from s5_models, because subagents
run on their frontmatter values. Digests of the folder and of each assembled text are
returned for `run.json`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from stemma.driver import frontmatter
from stemma.driver.errors import AgentFileInvalid, FragmentMissing, PluginInvalid
from stemma.driver.task import FRAGMENT_SWITCHES, NOT_APPLICABLE, fragment_value
from stemma.register import roles

PLUGIN_NAME: Final = roles.PLUGIN
# s1 (invariant: section 7, v0.2.2): `decomposition` copies the method and the real
# question-type cards, `placebo` the placebo method and placebo cards, `none` neither.
# Real and placebo versions share frontmatter names, so each is copied into a folder named
# by its frontmatter `name` and both arms present the same skill names. Every other
# skill is copied as it is.
METHOD_SKILLS: Final = {"decomposition": "decomposition-method", "placebo": "placebo-method"}
REAL_CARD_PREFIX: Final = "card-"
PLACEBO_CARD_PREFIX: Final = "placebo-card-"
_SKILL_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9-]*$")
STANDING_RULES: Final = "STANDING-RULES.md"
IGNORED_NAMES: Final = frozenset({".gitkeep", ".DS_Store", "__pycache__"})


@dataclass
class Assembly:
    plugin_dir: Path
    digest: str
    files: dict[str, str]  # relative path -> sha256
    assembled: dict[str, dict[str, Any]] = field(default_factory=dict)
    agents: list[str] = field(default_factory=list)  # short role names
    skills: list[str] = field(default_factory=list)
    tools: dict[str, list[str]] = field(default_factory=dict)  # role -> frontmatter tools
    skill_sources: dict[str, str] = field(default_factory=dict)  # skill name -> source folder
    standing_rules_sha256: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.plugin_dir),
            "digest": self.digest,
            "files": self.files,
            "assembled": self.assembled,
            "agents": self.agents,
            "skills": self.skills,
            "skill_sources": self.skill_sources,
            "standing_rules_sha256": self.standing_rules_sha256,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def folder_digest(root: Path) -> tuple[str, dict[str, str]]:
    """sha256 over the sorted (relative path, file sha256) list of every file under root."""
    files: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise PluginInvalid(f"run plugin may not hold symlinks: {p}")
        if p.is_file():
            files[p.relative_to(root).as_posix()] = sha256_bytes(p.read_bytes())
    manifest = "".join(f"{k}\t{v}\n" for k, v in sorted(files.items()))
    return sha256_bytes(manifest.encode()), files


def _copy_tree(src: Path, dst: Path) -> None:
    def ignore(_d: str, names: list[str]) -> set[str]:
        return {n for n in names if n in IGNORED_NAMES}

    for p in src.rglob("*"):
        if p.is_symlink():
            raise PluginInvalid(f"plugin source may not hold symlinks: {p}")
    shutil.copytree(src, dst, ignore=ignore)


def _fragments(src_dir: Path, prefix: str, switches: dict[str, Any]) -> list[Path]:
    out = []
    for name in FRAGMENT_SWITCHES:
        if switches[name] == NOT_APPLICABLE:
            continue  # no fragment for a switch the run's shape makes moot
        frag = src_dir / f"{prefix}{name}.{fragment_value(switches, name)}.md"
        if not frag.is_file():
            raise FragmentMissing(f"missing fragment {frag.relative_to(src_dir.parent)} "
                                  f"for {name}={fragment_value(switches, name)}")
        out.append(frag)
    return out


def _join(base: str, fragments: list[str]) -> str:
    parts = [base.rstrip("\n")] + [f.strip("\n") for f in fragments if f.strip()]
    return "\n\n".join(parts) + "\n"


def _set_model(text: str, where: str, role: str, models: dict[str, dict[str, str]]) -> str:
    spec = models.get(role)
    if spec is None:
        raise AgentFileInvalid(f"{where}: no model and effort in s5_models for role {role!r}")
    return frontmatter.set_fields(text, where, {"model": spec["model"], "effort": spec["effort"]})


def _skill_kind(folder: str) -> str:
    if folder in METHOD_SKILLS.values():
        return folder
    if folder.startswith(PLACEBO_CARD_PREFIX):
        return "placebo-card"
    if folder.startswith(REAL_CARD_PREFIX):
        return "card"
    return "other"


def _skill_name(folder: Path) -> str:
    where = f"skills/{folder.name}/SKILL.md"
    if not (folder / "SKILL.md").is_file():
        raise PluginInvalid(f"skill folder without SKILL.md: skills/{folder.name}")
    name = frontmatter.parse((folder / "SKILL.md").read_text(encoding="utf-8"), where) \
        .scalar("name")
    if not name or not _SKILL_NAME.match(name):
        raise AgentFileInvalid(f"{where}: frontmatter name missing or not a plain name")
    return name


def _copy_skills(src: Path, dst: Path, method: str) -> tuple[list[str], dict[str, str]]:
    by_kind: dict[str, dict[str, str]] = {}  # kind -> frontmatter name -> source folder
    if src.is_dir():
        for d in sorted(p for p in src.iterdir() if p.is_dir() and p.name not in IGNORED_NAMES):
            kind = _skill_kind(d.name)
            name = _skill_name(d)
            group = by_kind.setdefault(kind, {})
            if name in group:
                raise PluginInvalid(f"skills/{group[name]} and skills/{d.name} share the "
                                    f"name {name!r}")
            group[name] = d.name
    real = {**by_kind.get("decomposition-method", {}), **by_kind.get("card", {})}
    placebo = {**by_kind.get("placebo-method", {}), **by_kind.get("placebo-card", {})}
    if method != "none":
        if not by_kind.get(METHOD_SKILLS[method]):
            raise PluginInvalid(f"s1_method={method} needs skills/{METHOD_SKILLS[method]}")
        # The arms must offer the same skill names, or the lead's words would differ.
        if real and placebo and sorted(real) != sorted(placebo):
            raise PluginInvalid(f"real and placebo skills differ in names: real {sorted(real)}, "
                                f"placebo {sorted(placebo)}")
    chosen = {"decomposition": real, "placebo": placebo, "none": {}}[method]
    selected = {**by_kind.get("other", {})}
    for name, folder in chosen.items():
        if name in selected:
            raise PluginInvalid(f"skills/{folder} and skills/{selected[name]} share the "
                                f"name {name!r}")
        selected[name] = folder
    if selected:
        dst.mkdir()
        for name, folder in sorted(selected.items()):
            _copy_tree(src / folder, dst / name)
    return sorted(selected), dict(sorted(selected.items()))


def assemble(src: Path, dst: Path, switches: dict[str, Any]) -> Assembly:
    """Build the run plugin at `dst` (which must not exist) from the source `src`."""
    if dst.exists():
        raise PluginInvalid(f"run plugin folder already exists: {dst}")
    manifest = src / ".claude-plugin" / "plugin.json"
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise PluginInvalid(f"plugin manifest unreadable: {manifest}: {exc}") from exc
    if not isinstance(meta, dict) or meta.get("name") != PLUGIN_NAME:
        raise PluginInvalid(f"plugin manifest name must be {PLUGIN_NAME!r}: {manifest}")
    for need in (".mcp.json", "hooks/hooks.json", "lead/base.md", STANDING_RULES):
        if not (src / need).is_file():
            raise PluginInvalid(f"plugin source lacks {need}")
    models: dict[str, dict[str, str]] = switches["s5_models"]

    dst.mkdir(parents=True)
    (dst / ".claude-plugin").mkdir()
    shutil.copy2(manifest, dst / ".claude-plugin" / "plugin.json")
    shutil.copy2(src / ".mcp.json", dst / ".mcp.json")
    _copy_tree(src / "hooks", dst / "hooks")
    agents_dst = dst / "agents"
    agents_dst.mkdir()

    asm = Assembly(dst, "", {})

    # The lead: base + one fragment per switch, in section 7's order.
    lead_frags = _fragments(src / "lead", "", switches)
    lead_text = _join((src / "lead" / "base.md").read_text(encoding="utf-8"),
                      [f.read_text(encoding="utf-8") for f in lead_frags])
    lead_text = _set_model(lead_text, "lead/base.md", "lead", models)
    (agents_dst / "lead.md").write_text(lead_text, encoding="utf-8")
    asm.assembled["agents/lead.md"] = {
        "sha256": sha256_bytes(lead_text.encode()),
        "from": ["lead/base.md"] + [f"lead/{f.name}" for f in lead_frags],
    }

    # Other agents. `<name>.md` is copied; `worker.base.md` is assembled with fragments
    # `worker.<switch>.<value>.md`. Any other dotted name is a fragment, never an agent.
    agents_src = src / "agents"
    single_loop = switches["s2_shape"] == "single_loop"
    if agents_src.is_dir():
        plain = {p.stem for p in agents_src.glob("*.md") if "." not in p.stem}
        if "lead" in plain:
            raise PluginInvalid("agents/lead.md must not exist: the lead is assembled from lead/")
        if (agents_src / "worker.base.md").is_file():
            if "worker" in plain:
                raise PluginInvalid("both agents/worker.md and agents/worker.base.md exist")
            if not single_loop:
                frags = _fragments(agents_src, "worker.", switches)
                text = _join((agents_src / "worker.base.md").read_text(encoding="utf-8"),
                             [f.read_text(encoding="utf-8") for f in frags])
                text = _set_model(text, "agents/worker.base.md", "worker", models)
                (agents_dst / "worker.md").write_text(text, encoding="utf-8")
                asm.assembled["agents/worker.md"] = {
                    "sha256": sha256_bytes(text.encode()),
                    "from": ["agents/worker.base.md"] + [f"agents/{f.name}" for f in frags],
                }
        for stem in sorted(plain):
            if stem == "worker" and single_loop:
                continue  # s2 single_loop: one author, no workers; the type cannot start
            where = f"agents/{stem}.md"
            text = _set_model((agents_src / f"{stem}.md").read_text(encoding="utf-8"),
                              where, stem, models)
            (agents_dst / f"{stem}.md").write_text(text, encoding="utf-8")

    # Every agent file names itself and states its tools (section 8).
    for p in sorted(agents_dst.glob("*.md")):
        where = f"run plugin agents/{p.name}"
        agent = frontmatter.parse(p.read_text(encoding="utf-8"), where)
        if agent.scalar("name") != p.stem:
            raise AgentFileInvalid(f"{where}: frontmatter name must be {p.stem!r}")
        asm.tools[p.stem] = agent.tools()
        asm.agents.append(p.stem)

    asm.skills, asm.skill_sources = _copy_skills(src / "skills", dst / "skills",
                                                 switches["s1_method"])
    asm.standing_rules_sha256 = sha256_bytes((src / STANDING_RULES).read_bytes())
    asm.digest, asm.files = folder_digest(dst)
    return asm
