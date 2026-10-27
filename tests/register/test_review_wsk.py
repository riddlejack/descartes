"""The Codex review of claude/walking-skeleton (15437e8), each finding reproduced, now
refused; and contract v0.3.1 (gate_status, review points)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_filing import RATIO, check, estimated, lookup_ab
from test_store import LEAD, WORKER, FakeEvidence, checker_for, claim, launched, ready_run

from stemma.register import derivation
from stemma.register.derivation import DerivationError, build_bundle, rerun
from stemma.register.filing import Refused
from stemma.register.store import Register, closure


@pytest.fixture
def reg(tmp_path: Path) -> Register:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "criteria.md").write_text("- C1: The answer says whether the target is met.\n")
    return Register(tmp_path / "run", evidence=FakeEvidence())


# --- 1. A cycle in the tree never lets the gate skip nodes ------------------------

def test_reparenting_a_node_under_its_own_child_is_refused(reg: Register) -> None:
    reg.tree_upsert(LEAD, {"id": "A", "kind": "question", "statement": "a"})
    reg.tree_upsert(LEAD, {"id": "B", "kind": "question", "statement": "b", "parent": "A"})
    reg.tree_upsert(LEAD, {"id": "C", "kind": "question", "statement": "c", "parent": "B"})
    for parent in ("B", "C"):
        with pytest.raises(Refused, match="cannot be placed under its own descendant"):
            reg.tree_upsert(LEAD, {"id": "A", "parent": parent})


def test_a_planted_cycle_is_unreachable_and_unmet(reg: Register) -> None:
    # Before the fix: A under B under A, both open, both skipped as "derived".
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    reg.tree_upsert(LEAD, {"id": "A", "kind": "question", "statement": "a"})
    reg.tree_upsert(LEAD, {"id": "B", "kind": "question", "statement": "b", "parent": "A"})
    a = next(n for n in reg.tree() if n["id"] == "A")
    reg._put_node({**a, "parent": "B"}, reg._src(LEAD))  # a write that bypassed the check
    failures = [f for f in reg.finish_failures(1) if f.startswith("node ")]
    assert sorted(failures) == [
        f"node {x} is not reachable from a root of the tree (its parents form a cycle or "
        f"lead to no node)" for x in ("A", "B")]
    with pytest.raises(Refused, match="not reachable from a root"):
        reg.finish_run(LEAD, 1)


def test_the_walk_visits_each_node_once_from_the_roots() -> None:
    nodes = [{"id": "R", "kind": "question", "parent": None, "status": "open"},
             {"id": "X", "kind": "question", "parent": "Y", "status": "answered"},
             {"id": "Y", "kind": "question", "parent": "X", "status": "answered"},
             {"id": "Z", "kind": "question", "parent": "gone", "status": "answered"}]
    c = closure(nodes)
    assert {k for k, v in c.items() if v["closes_by"] == "unreachable"} == {"X", "Y", "Z"}
    assert not any(c[k]["closed"] for k in ("X", "Y", "Z"))


# --- 2. A refused correction withdraws the same session's earlier holds -----------

def test_holds_then_a_refused_correction_leaves_the_claim_unverdicted(reg: Register) -> None:
    cid = ready_run(reg)
    chk = checker_for(reg, cid)
    reg.record_claim_verdict(chk, cid, "holds", "ok")
    assert reg.get_claim(cid)["verdict"]["verdict"] == "holds"
    with pytest.raises(Refused, match="needs a derivation"):
        reg.record_claim_verdict(chk, cid, "holds_with_correction", "a detail is wrong",
                                 corrected_claim=claim(tier="estimated", support=[]))
    assert reg.get_claim(cid)["verdict"] is None  # needs a verdict again
    with pytest.raises(Refused, match="has verdict none"):
        reg.finish_run(LEAD, 1)
    reg.record_claim_verdict(chk, cid, "not_supported", "the detail is wrong")
    assert reg.get_claim(cid)["verdict"]["verdict"] == "not_supported"


# --- 3 and 4. The code a re-run copies: no link anywhere, one name, within limits ---

def no_run(*_a: Any) -> Any:
    raise AssertionError("the code must be refused before any re-run")


def test_a_linked_folder_on_the_way_to_the_script_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "calc.py").write_text(RATIO)
    (tmp_path / "analysis").mkdir()
    (tmp_path / "analysis" / "sub").symlink_to(outside)  # only the last part was guarded
    with pytest.raises(Refused, match="not a plain folder"):
        check(estimated("analysis/sub/calc.py", 0.75), tmp_path, rerun_fn=no_run,
              lookup=lookup_ab)


def test_a_hard_linked_helper_is_refused(tmp_path: Path) -> None:
    (tmp_path / "analysis").mkdir()
    (tmp_path / "analysis" / "calc.py").write_text("import helper\n" + RATIO)
    secret = tmp_path / "elsewhere.py"
    secret.write_text("KEY = 'not for the sandbox'\n")
    (tmp_path / "analysis" / "helper.py").hardlink_to(secret)
    with pytest.raises(Refused, match="more than one name for one file"):
        check(estimated("analysis/calc.py", 0.75), tmp_path, rerun_fn=no_run,
              lookup=lookup_ab)


def test_code_limits_are_checked_before_copying(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "analysis").mkdir()
    (tmp_path / "analysis" / "calc.py").write_text("import h0, h1, h2\n" + RATIO)
    for i in range(3):
        (tmp_path / "analysis" / f"h{i}.py").write_text("x = 1\n" * 100)
    monkeypatch.setattr(derivation, "CODE_MAX_FILES", 3)
    with pytest.raises(DerivationError, match="more than the 3 Python files a re-run copies"):
        build_bundle(tmp_path, "analysis/calc.py")
    monkeypatch.setattr(derivation, "CODE_MAX_FILES", 64)
    monkeypatch.setattr(derivation, "CODE_MAX_BYTES", 1000)
    with pytest.raises(DerivationError, match="larger than 1000 bytes together"):
        build_bundle(tmp_path, "analysis/calc.py")


def test_document_limits_are_checked_before_copying(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "analysis").mkdir()
    (tmp_path / "analysis" / "calc.py").write_text(RATIO)
    code = build_bundle(tmp_path, "analysis/calc.py")
    monkeypatch.setattr(derivation, "DOCUMENTS_MAX", 2)
    with pytest.raises(DerivationError, match="3 documents named, more than 2"):
        rerun(code, {}, {"r1": "a", "r2": "b", "r3": "c"})
    monkeypatch.setattr(derivation, "DOCUMENTS_MAX_CHARS", 5)
    with pytest.raises(DerivationError, match="longer than 5 characters"):
        rerun(code, {}, {"r1": "abcdef"})
    raw = estimated("analysis/calc.py", 0.75)
    raw["derivation"]["documents"] = ["r1", "r2", "r3"]
    from stemma.register import filing
    monkeypatch.setattr(filing, "DOCUMENTS_MAX", 2)
    with pytest.raises(Refused, match="names 3 documents, more than 2"):
        check(raw, tmp_path, rerun_fn=no_run, lookup=lookup_ab)


# --- 6. At the review limit only the latest version is released, with its reviews --

def write_limits(reg: Register, n: int) -> None:
    import json

    reg.run_dir.mkdir(parents=True, exist_ok=True)
    (reg.run_dir / "run.json").write_text(json.dumps({"limits": {"max_review_rounds": n}}))


def draft(reg: Register, version: int, cid: str) -> None:
    (reg.run_dir / f"draft/answer.v{version}.md").write_text(
        f"# Answer\n\nRevenue was 1.2bn [[c:{cid[:12]}]] in the year to 2025 [[nc:period]].\n")
    reg.submit_draft(LEAD, version)


def review(reg: Register, version: int, points: list[dict[str, Any]] | None = None) -> None:
    for i, role in enumerate(("deciders-eye-reviewer", "evidence-auditor",
                              "mechanical-reader")):
        who = launched(reg, role, f"draft:{version}", f"s-w{version}-{i}")
        reg.record_review(who, version, f"review of v{version} by {role}",
                          points if i == 0 and points else [])


def test_at_the_limit_an_older_version_or_pending_reviews_never_release(
        reg: Register) -> None:
    # Before the fix: v1 released while v3's reviews were pending, and completion.md
    # was frozen with "(no review recorded)" placeholders.
    write_limits(reg, 2)
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    draft(reg, 2, cid)  # v2's reviews not yet recorded
    with pytest.raises(Refused, match="only the latest submitted version, 2, may be"):
        reg.finish_run(LEAD, 1)
    with pytest.raises(Refused, match="review of version 2 is not recorded"):
        reg.finish_run(LEAD, 2)
    review(reg, 2, [{"point": "the pace figure omits Q4", "material": True, "criterion": "C1"}])
    out = reg.finish_run(LEAD, 2)  # at the limit a material point does not block
    text = (reg.run_dir / out["completion"]).read_text()
    assert "(no review recorded)" not in text
    assert "- deciders-eye-reviewer: the pace figure omits Q4" in text
    assert "review of v2 by evidence-auditor" in text and "review of v1" not in text


# --- 7. gate_status reads the gate; reviews carry points; material points block ----

def test_gate_status_reads_the_same_list_finish_run_refuses_with(reg: Register) -> None:
    cid = ready_run(reg)  # the claim has no verdict yet
    g = reg.gate_status(LEAD, 1)
    assert not g["releasable"] and g["unmet_count"] == 1
    with pytest.raises(Refused) as exc:
        reg.finish_run(LEAD, 1)
    assert str(exc.value).split("\n", 1)[1] == g["unmet"]
    assert not (reg.run_dir / "deliverable/answer.md").exists()  # reading released nothing
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    assert reg.gate_status(LEAD, 1) == {
        "version": 1, "releasable": True, "unmet_count": 0, "unmet": "",
        "open_material_points": {"count": 0, "by_criterion": {}, "by_node": {}},
        "set_aside": [], "branch_limits": []}
    with pytest.raises(Refused, match="read the release gate"):
        reg.gate_status(WORKER, 1)


def test_a_material_point_blocks_release_below_the_limit(reg: Register) -> None:
    cid = ready_run(reg)
    reg.record_claim_verdict(checker_for(reg, cid), cid, "holds", "ok")
    draft(reg, 2, cid)
    review(reg, 2, [{"point": "the headline count is a different universe", "material": True,
                     "criterion": "C1", "node": "n1"},
                    {"point": "a typo in section 2", "material": False, "node": "n1",
                     "criterion": None}])
    unmet = reg.finish_failures(2)
    assert unmet == [("the deciders-eye-reviewer review of version 2 raises a material "
                      "point v2.de.1 (criterion C1, node n1): the headline count is a "
                      "different universe")]
    g = reg.gate_status(LEAD, 2)["open_material_points"]
    assert g["count"] == 1 and [p["id"] for p in g["by_criterion"]["C1"]] == ["v2.de.1"]
    assert [p["id"] for p in g["by_node"]["n1"]] == ["v2.de.1"]
    with pytest.raises(Refused, match="raises a material point"):
        reg.finish_run(LEAD, 2)
    facts = [r for r in reg._run_facts("review_recorded") if r["version"] == 2]
    assert facts[0]["points"][1] == {"id": "v2.de.2", "point": "a typo in section 2",
                                     "material": False, "node": "n1", "criterion": None}


@pytest.mark.parametrize("points", [None, "x", [{"point": "p"}], [{"point": "", "material": True}],
                                    [{"point": "p", "material": "yes"}],
                                    [{"point": "p", "material": True, "extra": 1}],
                                    [{"point": "p", "material": True}],
                                    [{"point": "p", "material": True, "node": None,
                                      "criterion": None}],
                                    [{"point": "p", "material": True, "node": "nope"}],
                                    [{"point": "p", "material": True, "criterion": "C9"}]])
def test_review_points_are_checked_for_shape(reg: Register, points: Any) -> None:
    cid = ready_run(reg)
    draft(reg, 2, cid)
    who = launched(reg, "deciders-eye-reviewer", "draft:2", "s-shape")
    with pytest.raises(Refused, match="point"):
        reg.record_review(who, 2, "text", points)
