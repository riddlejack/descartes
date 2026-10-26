"""The role-file lint passes on the real files and fails, naming the fault, on each way
a hostile edit could slip a line past it."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rolemap", REPO / "tools/roles/rolemap.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["rolemap"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rm(tmp_path: Path) -> Iterator[ModuleType]:
    """The module pointed at a private copy of the plugin, the evidence file and the map."""
    module = load_module()
    shutil.copytree(REPO / "plugin", tmp_path / "plugin")
    (tmp_path / "tools/roles").mkdir(parents=True)
    shutil.copy(REPO / "tools/roles/skill-evidence.md", tmp_path / "tools/roles")
    shutil.copy(REPO / "tools/roles/rolemap.py", tmp_path / "tools/roles")
    shutil.copytree(REPO / "tools/roles/shared", tmp_path / "tools/roles/shared")
    (tmp_path / "docs").mkdir()
    shutil.copy(REPO / "docs/ROLE-MAP.md", tmp_path / "docs")
    shutil.copy(REPO / "docs/CONTRACTS.md", tmp_path / "docs")
    module.set_root(tmp_path)
    yield module


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"fixture text not found in {path}: {old!r}"
    path.write_text(text.replace(old, new, 1))


def errors_of(rm: ModuleType) -> str:
    return "\n".join(rm.check_all())


def test_real_files_pass(rm: ModuleType) -> None:
    assert rm.check_all() == []


def test_role_map_is_current() -> None:
    module = load_module()
    assert (REPO / "docs/ROLE-MAP.md").read_text() == module.build_map()


def test_line_without_comment_fails(rm: ModuleType) -> None:
    worker = rm.AGENTS_DIR / "worker.md"
    worker.write_text(worker.read_text() + "Always be thorough.\n")
    assert "no comment naming the failure" in errors_of(rm)


def test_comment_without_evidence_fails(rm: ModuleType) -> None:
    worker = rm.AGENTS_DIR / "worker.md"
    worker.write_text(worker.read_text() + "Be thorough. <!-- prevents: sloppiness -->\n")
    assert "no comment naming the failure" in errors_of(rm)


def test_two_sentences_behind_one_comment_fail(rm: ModuleType) -> None:
    worker = rm.AGENTS_DIR / "worker.md"
    worker.write_text(
        worker.read_text()
        + "Read everything. Then read it again. <!-- prevents: x; evidence: y -->\n"
    )
    assert "not exactly one sentence" in errors_of(rm)


def test_unknown_tool_fails(rm: ModuleType) -> None:
    edit(
        rm.AGENTS_DIR / "claim-checker.md",
        "  - Read\n",
        "  - Read\n  - mcp__plugin_stemma_stemma__set_verdict_passed\n",
    )
    assert "not a tool the contract defines" in errors_of(rm)


def test_wrong_prefix_fails(rm: ModuleType) -> None:
    edit(rm.AGENTS_DIR / "claim-checker.md", "  - Read\n", "  - Read\n  - mcp__other__read\n")
    assert "does not use the prefix" in errors_of(rm)


def test_webfetch_fails(rm: ModuleType) -> None:
    edit(rm.AGENTS_DIR / "worker.md", "  - Read\n", "  - Read\n  - WebFetch\n")
    assert "not an allowed harness tool" in errors_of(rm)


def test_small_model_fails(rm: ModuleType) -> None:
    edit(rm.AGENTS_DIR / "mechanical-reader.md", "model: claude-opus-5-5", "model: haiku")
    assert "not an approved model" in errors_of(rm)


def test_low_effort_fails(rm: ModuleType) -> None:
    edit(rm.AGENTS_DIR / "mechanical-reader.md", "effort: high", "effort: medium")
    assert "below high" in errors_of(rm)


def test_extra_agent_file_fails(rm: ModuleType) -> None:
    shutil.copy(rm.AGENTS_DIR / "worker.md", rm.AGENTS_DIR / "helper.md")
    assert "not a role in contract section 8" in errors_of(rm)


def test_over_a_page_fails(rm: ModuleType) -> None:
    worker = rm.AGENTS_DIR / "worker.md"
    filler = " ".join(["word"] * (rm.PAGE_WORDS + 1))
    worker.write_text(worker.read_text() + f"{filler}. <!-- prevents: x; evidence: y -->\n")
    assert "over a page" in errors_of(rm)


def test_assembled_lead_over_a_page_fails(rm: ModuleType) -> None:
    fragment = rm.LEAD_DIR / "s6_cross_family.final.md"
    filler = " ".join(["word"] * rm.PAGE_WORDS)
    fragment.write_text(f"{filler}. <!-- prevents: x; evidence: y -->\n")
    assert "assembled lead" in errors_of(rm)


def test_missing_fragment_fails(rm: ModuleType) -> None:
    (rm.LEAD_DIR / "s4_orientation.basic_facts.md").unlink()
    assert "missing fragment" in errors_of(rm)


def test_fragment_that_branches_by_frontmatter_fails(rm: ModuleType) -> None:
    fragment = rm.LEAD_DIR / "s6_cross_family.off.md"
    fragment.write_text("---\nmodel: claude-fable-5-1\n---\n")
    assert "may not carry frontmatter" in errors_of(rm)


def test_s1_fragments_that_differ_fail(rm: ModuleType) -> None:
    fragment = rm.LEAD_DIR / "s1_method.placebo.md"
    fragment.write_text(fragment.read_text().replace("in full", "quickly"))
    assert "S1 fragments differ" in errors_of(rm)


def test_standing_rules_must_be_four(rm: ModuleType) -> None:
    rules = rm.STANDING_RULES
    rules.write_text(rules.read_text() + "Be careful. <!-- prevents: x; evidence: y -->\n")
    assert "design step 0 has 4" in errors_of(rm)


def test_skill_sentence_without_evidence_fails(rm: ModuleType) -> None:
    skill = rm.SKILLS_DIR / "card-mechanism/SKILL.md"
    skill.write_text(skill.read_text() + "\nAlways assume demand is elastic.\n")
    assert "without evidence" in errors_of(rm)


def test_comment_inside_skill_fails(rm: ModuleType) -> None:
    skill = rm.SKILLS_DIR / "card-decision/SKILL.md"
    skill.write_text(skill.read_text() + "\n<!-- note -->\n")
    assert "comments belong in" in errors_of(rm)


def test_placebo_with_different_frontmatter_fails(rm: ModuleType) -> None:
    edit(rm.SKILLS_DIR / "placebo-method/SKILL.md", "name: method", "name: placebo")
    assert "frontmatter differs" in errors_of(rm)


def test_placebo_that_teaches_method_fails(rm: ModuleType) -> None:
    edit(
        rm.SKILLS_DIR / "placebo-method/SKILL.md",
        "Before the violin.",
        "Before the violin, weigh the evidence.",
    )
    assert "method word 'evidence'" in errors_of(rm)


def test_placebo_of_wrong_length_fails(rm: ModuleType) -> None:
    placebo = rm.SKILLS_DIR / "placebo-card-mechanism/SKILL.md"
    text = placebo.read_text()
    placebo.write_text(text[: len(text) // 2])
    assert "characters against" in errors_of(rm)


def test_missing_placebo_twin_fails(rm: ModuleType) -> None:
    shutil.rmtree(rm.SKILLS_DIR / "placebo-card-decision")
    assert "missing placebo twin" in errors_of(rm)


def test_assembly_is_base_then_fragments_in_contract_order(rm: ModuleType) -> None:
    choice = {
        "s1_method": "none",
        "s2_shape": "team",
        "s3_hypothesis_in_briefs": "false",
        "s4_orientation": "frame_first",
        "s6_cross_family": "off",
    }
    text = rm.assemble_lead(choice)
    assert text.startswith((rm.LEAD_DIR / "base.md").read_text())
    assert text.index("## Dispatch") < text.index("## What orders carry")
    assert text.index("## What orders carry") < text.index("## Before the first frame")
    assert "stemma:method" not in text


def test_assembly_refuses_an_unknown_value(rm: ModuleType) -> None:
    with pytest.raises(rm.RoleFileError, match="has no value"):
        rm.assemble_lead({"s1_method": "both"})


def test_retool_rewrites_every_name_and_the_constant(rm: ModuleType) -> None:
    changed = rm.retool("mcp__stemma__")
    assert rm.ROOT / "tools/roles/rolemap.py" in changed
    for path in [*rm.AGENTS_DIR.glob("*.md"), *rm.LEAD_DIR.glob("*.md")]:
        assert "mcp__plugin_stemma_stemma__" not in path.read_text()
    assert 'TOOL_PREFIX = "mcp__stemma__"' in (rm.ROOT / "tools/roles/rolemap.py").read_text()


def test_retool_refuses_a_malformed_prefix(rm: ModuleType) -> None:
    with pytest.raises(rm.RoleFileError, match="not an MCP tool prefix"):
        rm.retool("plugin_stemma")


def test_grep_and_glob_are_not_tools(rm: ModuleType) -> None:
    edit(rm.AGENTS_DIR / "branch-reviewer.md", "  - Read\n", "  - Read\n  - Grep\n")
    assert "Grep is not an allowed harness tool" in errors_of(rm)


def test_toolsearch_line_when_tools_load_at_start_fails(rm: ModuleType) -> None:
    worker = rm.AGENTS_DIR / "worker.md"
    worker.write_text(worker.read_text() + rm.DEFERRED_TOOLS_LINE + "\n")
    assert "does not need" in errors_of(rm)


def test_toolsearch_tool_when_tools_load_at_start_fails(rm: ModuleType) -> None:
    edit(rm.AGENTS_DIR / "worker.md", "  - Read\n", "  - Read\n  - ToolSearch\n")
    assert "lists ToolSearch" in errors_of(rm)


def test_shared_block_edited_in_one_role_fails(rm: ModuleType) -> None:
    edit(rm.AGENTS_DIR / "worker.md", "primary first:", "primary or secondary first:")
    assert "differs from its source" in errors_of(rm)


def test_sync_restores_shared_blocks(rm: ModuleType) -> None:
    edit(rm.AGENTS_DIR / "worker.md", "primary first:", "primary or secondary first:")
    assert rm.AGENTS_DIR / "worker.md" in rm.sync()
    assert rm.check_all() == []


def test_single_loop_lead_carries_the_worker_method(rm: ModuleType) -> None:
    choice = {
        "s1_method": "none",
        "s2_shape": "single_loop",
        "s3_hypothesis_in_briefs": "not_applicable",
        "s4_orientation": "frame_first",
        "s6_cross_family": "off",
    }
    shared = (rm.SHARED_DIR / "branch-work.md").read_text()
    assert shared in rm.assemble_lead(choice)


def test_undefer_removes_every_toolsearch_line(rm: ModuleType) -> None:
    worker = rm.AGENTS_DIR / "worker.md"
    edit(worker, "  - Read\n", "  - Read\n  - ToolSearch\n")
    worker.write_text(worker.read_text() + rm.DEFERRED_TOOLS_LINE + "\n")
    rm.undefer()
    assert rm.DEFERRED_TOOLS_LINE not in worker.read_text()
    assert "ToolSearch" not in worker.read_text()
    assert "MCP_TOOLS_DEFERRED = False" in (rm.ROOT / "tools/roles/rolemap.py").read_text()


def test_single_loop_loads_no_s3_fragment(rm: ModuleType) -> None:
    choice = {
        "s1_method": "none",
        "s2_shape": "single_loop",
        "s3_hypothesis_in_briefs": "not_applicable",
        "s4_orientation": "frame_first",
        "s6_cross_family": "off",
    }
    assert "## What orders carry" not in rm.assemble_lead(choice)


def test_s3_with_single_loop_is_refused(rm: ModuleType) -> None:
    choice = {
        "s1_method": "none",
        "s2_shape": "single_loop",
        "s3_hypothesis_in_briefs": "true",
        "s4_orientation": "frame_first",
        "s6_cross_family": "off",
    }
    with pytest.raises(rm.RoleFileError, match="not_applicable"):
        rm.assemble_lead(choice)


def test_not_applicable_s3_with_workers_is_refused(rm: ModuleType) -> None:
    choice = {
        "s1_method": "none",
        "s2_shape": "team",
        "s3_hypothesis_in_briefs": "not_applicable",
        "s4_orientation": "frame_first",
        "s6_cross_family": "off",
    }
    with pytest.raises(rm.RoleFileError, match="not_applicable"):
        rm.assemble_lead(choice)


def append(rm: ModuleType, role: str, line: str) -> None:
    path = rm.AGENTS_DIR / f"{role}.md"
    path.write_text(path.read_text() + line + "\n")


def test_prose_name_unknown_to_the_contract_fails(rm: ModuleType) -> None:
    append(rm, "worker", "Call mark_done when you finish. <!-- prevents: x; evidence: y -->")
    assert "'mark_done', which the contract does not" in errors_of(rm)


def test_prose_tool_the_role_does_not_hold_fails(rm: ModuleType) -> None:
    append(
        rm,
        "mechanical-reader",
        "File each figure with file_claim. <!-- prevents: x; evidence: y -->",
    )
    assert "use file_claim, which it does not hold" in errors_of(rm)


def test_prose_path_outside_the_run_fails(rm: ModuleType) -> None:
    append(rm, "worker", "Keep notes in /tmp/notes.md for later. <!-- prevents: x; evidence: y -->")
    assert "outside the run folder" in errors_of(rm)


def test_prose_parent_path_fails(rm: ModuleType) -> None:
    append(rm, "worker", "Read ../secrets.md first. <!-- prevents: x; evidence: y -->")
    assert "outside the run folder" in errors_of(rm)


def test_prose_path_unknown_to_the_contract_fails(rm: ModuleType) -> None:
    append(rm, "worker", "Keep notes in scratch/notes.md always. <!-- prevents: x; evidence: y -->")
    assert "the path 'scratch/notes.md', which the contract does not" in errors_of(rm)


def test_two_sentences_sharing_one_comment_fail(rm: ModuleType) -> None:
    append(rm, "worker", "Read the order first. <!-- prevents: same; evidence: same -->")
    append(rm, "worker", "Read the record next. <!-- prevents: same; evidence: same -->")
    assert "shares its comment with another sentence" in errors_of(rm)


def test_sentence_break_before_a_number_fails(rm: ModuleType) -> None:
    append(rm, "worker", "Read it whole. 2 passes follow. <!-- prevents: x; evidence: y -->")
    assert "not exactly one sentence" in errors_of(rm)
