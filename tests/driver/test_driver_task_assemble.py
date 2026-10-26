"""task.json validation, switch defaults, run folder and plugin assembly per switch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from driverkit import write_plugin, write_task

from stemma.driver import assemble, frontmatter
from stemma.driver.config import PLUGIN_DIR, DriverConfig
from stemma.driver.errors import (
    AgentFileInvalid,
    FragmentMissing,
    PluginInvalid,
    SwitchInvalid,
    TaskInvalid,
)
from stemma.driver.runfolder import create_run
from stemma.driver.task import fill_switches, load_task


def test_defaults_filled() -> None:
    sw = fill_switches(None)
    assert sw["s1_method"] == "decomposition" and sw["s2_shape"] == "team"
    assert sw["s3_hypothesis_in_briefs"] is False and sw["s6_cross_family"] == "off"
    m = sw["s5_models"]
    assert {r: v["effort"] for r, v in m.items()} == {
        "lead": "max", "deciders-eye-reviewer": "max", "evidence-auditor": "max",
        "mechanical-reader": "max", "worker": "high", "blind-framer": "high",
        "claim-checker": "high", "branch-reviewer": "high", "retrospective-writer": "high"}
    assert {v["model"] for v in m.values()} == {"claude-opus-5-5"}


@pytest.mark.parametrize("given", [
    {"s1_method": "lens"}, {"s9": "x"}, {"s3_hypothesis_in_briefs": "yes"},
    {"s5_models": {"lead": {"model": "claude-haiku-4-5"}}},
    {"s5_models": {"lead": {"effort": "ultra"}}}, {"s5_models": {"intern": {"effort": "low"}}},
])
def test_bad_switches_refused(given: dict[str, object]) -> None:
    with pytest.raises(SwitchInvalid):
        fill_switches(given)


def test_single_loop_makes_s3_not_applicable() -> None:
    assert fill_switches({"s2_shape": "single_loop"})["s3_hypothesis_in_briefs"] == \
        "not_applicable"
    assert fill_switches({"s2_shape": "team"})["s3_hypothesis_in_briefs"] is False


def test_s5_override() -> None:
    m = fill_switches({"s5_models": {"stemma:worker": {"model": "claude-fable-5-1"}}})["s5_models"]
    assert m["worker"] == {"model": "claude-fable-5-1", "effort": "high"}


def test_task_validation(tmp: Path) -> None:
    for bad in ({"question": ""}, {"question": "q", "as_of": "2026-13-01"},
                {"question": "q", "attachments": ["missing.pdf"]}, {"question": "q", "x": 1},
                {"question": "q", "intake": {"a": 3}}):
        p = tmp / "t.json"
        p.write_text(json.dumps(bad))
        with pytest.raises(TaskInvalid):
            load_task(p)


def test_create_run(cfg: DriverConfig, tmp: Path) -> None:
    (tmp / "memo.txt").write_text("invented attachment")
    t = write_task(tmp, intake={"Who decides?": "The board."}, attachments=["memo.txt"],
                   as_of="2026-01-31", switches={"s1_method": "placebo"})
    run = create_run(cfg, t)
    rj = json.loads((run / "run.json").read_text())
    assert rj["as_of"] == "2026-01-31" and rj["switches"]["s1_method"] == "placebo"
    assert rj["harness"]["version"] == "2.1.308 (Claude Code)"
    assert rj["models"]["lead"] == {"model": "claude-opus-5-5", "effort": "max"}
    assert rj["limits"]["max_concurrent_checks"] == 4
    assert rj["evidence_stub"] is False
    assert (run / "task" / "attachments" / "memo.txt").read_text() == "invented attachment"
    assert "The board." in (run / "task" / "intake.md").read_text()
    assert (run / "CLAUDE.md").read_text() == (cfg.plugin_src / "STANDING-RULES.md").read_text()
    digest, _ = assemble.folder_digest(run / PLUGIN_DIR)
    assert rj["plugin"]["digest"] == digest
    assert rj["plugin"]["skills"] == ["card-decision", "deck", "method"]
    assert rj["plugin"]["skill_sources"]["method"] == "placebo-method"


def _asm(src: Path, tmp: Path, **sw: object) -> assemble.Assembly:
    return assemble.assemble(src, tmp / f"out{len(list(tmp.glob('out*')))}", fill_switches(sw))


def test_lead_text_is_base_plus_fragments_in_order(plugin_src: Path, tmp: Path) -> None:
    a = _asm(plugin_src, tmp, s1_method="none", s4_orientation="basic_facts",
             s3_hypothesis_in_briefs=True, s6_cross_family="final")
    text = (a.plugin_dir / "agents" / "lead.md").read_text()
    order = ["LEAD-FRAGMENT s1_method=none", "LEAD-FRAGMENT s2_shape=team",
             "LEAD-FRAGMENT s3_hypothesis_in_briefs=true",
             "LEAD-FRAGMENT s4_orientation=basic_facts", "LEAD-FRAGMENT s6_cross_family=final"]
    pos = [text.index(o) for o in order]
    assert pos == sorted(pos)
    assert text.index("You are an invented test role") < pos[0]
    assert a.assembled["agents/lead.md"]["from"][0] == "lead/base.md"
    assert len(a.assembled["agents/lead.md"]["from"]) == 6
    assert a.assembled["agents/lead.md"]["sha256"] == assemble.sha256_bytes(text.encode())
    worker = (a.plugin_dir / "agents" / "worker.md").read_text()
    assert "WORKER-FRAGMENT s4_orientation=basic_facts" in worker
    assert not list((a.plugin_dir / "agents").glob("*.*.md")), "fragments are never agents"


def test_model_and_effort_written_from_s5(plugin_src: Path, tmp: Path) -> None:
    a = _asm(plugin_src, tmp)
    for name, effort in (("lead", "max"), ("worker", "high"), ("claim-checker", "high"),
                         ("deciders-eye-reviewer", "max")):
        f = frontmatter.parse((a.plugin_dir / "agents" / f"{name}.md").read_text(), name)
        assert f.scalar("model") == "claude-opus-5-5" and f.scalar("effort") == effort


@pytest.mark.parametrize("method,names,bodies", [
    ("decomposition", ["card-decision", "deck", "method"], ["REAL CARD", "OTHER", "REAL METHOD"]),
    ("placebo", ["card-decision", "deck", "method"], ["PLACEBO CARD", "OTHER", "PLACEBO METHOD"]),
    ("none", ["deck"], ["OTHER"]),
])
def test_s1_selects_method_and_cards(plugin_src: Path, tmp: Path, method: str,
                                     names: list[str], bodies: list[str]) -> None:
    a = _asm(plugin_src, tmp, s1_method=method)
    skills = sorted(p.name for p in (a.plugin_dir / "skills").iterdir())
    assert skills == names == a.skills
    for n, b in zip(names, bodies, strict=True):
        assert b in (a.plugin_dir / "skills" / n / "SKILL.md").read_text()
    # Both arms present identical names; nothing of the other arm is in the run plugin.
    other = "PLACEBO" if method == "decomposition" else "REAL"
    for f in (a.plugin_dir / "skills").rglob("*"):
        assert f.is_dir() or (other not in f.read_text() or method == "none")


def test_s1_arms_must_share_names(plugin_src: Path, tmp: Path) -> None:
    (plugin_src / "skills" / "placebo-card-decision" / "SKILL.md").write_text(
        "---\nname: card-other\ndescription: x\n---\nx\n")
    with pytest.raises(PluginInvalid, match="differ in names"):
        _asm(plugin_src, tmp, s1_method="placebo")
    _asm(plugin_src, tmp, s1_method="none")


def test_s1_method_skill_required(plugin_src: Path, tmp: Path) -> None:
    import shutil
    shutil.rmtree(plugin_src / "skills" / "placebo-method")
    with pytest.raises(PluginInvalid, match="needs skills/placebo-method"):
        _asm(plugin_src, tmp, s1_method="placebo")


def test_missing_fragment_is_typed(plugin_src: Path, tmp: Path) -> None:
    (plugin_src / "lead" / "s4_orientation.primary_record.md").unlink()
    with pytest.raises(FragmentMissing, match="s4_orientation.primary_record"):
        _asm(plugin_src, tmp, s4_orientation="primary_record")
    _asm(plugin_src, tmp, s4_orientation="frame_first")  # other values still assemble
    (plugin_src / "agents" / "worker.s2_shape.team.md").unlink()
    with pytest.raises(FragmentMissing, match="worker.s2_shape.team"):
        _asm(plugin_src, tmp)


def test_single_loop_has_no_worker(plugin_src: Path, tmp: Path) -> None:
    a = _asm(plugin_src, tmp, s2_shape="single_loop")
    assert "worker" not in a.agents and not (a.plugin_dir / "agents" / "worker.md").exists()


def test_without_worker_base_copies_worker_as_is(tmp: Path) -> None:
    src = write_plugin(tmp / "src2", worker_base=False)
    (src / "agents" / "worker.md").write_text(
        "---\nname: worker\ntools:\n  - Read\n  - Bash\n---\nPlain worker.\n")
    a = _asm(src, tmp)
    assert a.tools["worker"] == ["Read", "Bash"]
    assert "agents/worker.md" not in a.assembled


def test_digest_changes_with_content(plugin_src: Path, tmp: Path) -> None:
    a1, a2 = _asm(plugin_src, tmp), _asm(plugin_src, tmp)
    assert a1.digest == a2.digest
    (a2.plugin_dir / "hooks" / "extra.py").write_text("x")
    assert assemble.folder_digest(a2.plugin_dir)[0] != a1.digest


def test_bad_sources_refused(plugin_src: Path, tmp: Path) -> None:
    (plugin_src / "agents" / "claim-checker.md").write_text("no frontmatter")
    with pytest.raises(AgentFileInvalid):
        _asm(plugin_src, tmp)
    (plugin_src / "agents" / "claim-checker.md").write_text("---\nname: other\ntools: Read\n---\n")
    with pytest.raises(AgentFileInvalid, match="name must be 'claim-checker'"):
        _asm(plugin_src, tmp)
    (plugin_src / "agents" / "claim-checker.md").unlink()
    (plugin_src / "agents" / "lead.md").write_text("---\nname: lead\ntools: Read\n---\n")
    with pytest.raises(PluginInvalid, match="assembled from lead"):
        _asm(plugin_src, tmp)


def test_manifest_name_checked(plugin_src: Path, tmp: Path) -> None:
    (plugin_src / ".claude-plugin" / "plugin.json").write_text('{"name": "other"}')
    with pytest.raises(PluginInvalid, match="manifest name"):
        _asm(plugin_src, tmp)


def test_frontmatter_set_fields_round_trip() -> None:
    text = "---\nname: x\ndescription: >\n  long\n  text\neffort: low\ntools: Read\n---\nBody\n"
    out = frontmatter.set_fields(text, "x", {"effort": "max", "model": "m"})
    f = frontmatter.parse(out, "x")
    assert f.scalar("effort") == "max" and f.scalar("model") == "m"
    assert f.scalar("description") == "long text" and f.body == "Body\n"
    with pytest.raises(AgentFileInvalid):
        frontmatter.parse("---\ntools: [Read]\n---\n", "x")
