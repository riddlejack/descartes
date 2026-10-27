"""The static report viewer: answer first, every figure opens its stored passage, and
nothing from a run's texts or sources can run as markup on the page."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from report_fixture import fresh_copy

from stemma.report import md, site

RUN1 = Path(os.environ.get("STEMMA_RUN1_DIR") or Path.home() / ".private/runs/"
            "stemma2-walking-skeleton/20261026-070412-2b3c98")


@pytest.fixture
def run(tmp_path_factory: pytest.TempPathFactory, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    dest = tmp_path / "run"
    return dest, fresh_copy(tmp_path_factory.getbasetemp(), dest)


def test_the_page_is_labelled_recorded_and_puts_the_answer_first(
        run: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    run_dir, _ids = run
    page = site.write(run_dir, tmp_path / "out").read_text()
    assert (tmp_path / "out" / ".nojekyll").exists()
    assert "RECORDED · run 20300101-000000-abcdef · 1 January 2030" in page
    order = [page.index(f'id="{s}"') for s in ("answer", "criteria", "tree", "checked",
                                                "facts", "verify", "claims")]
    assert order == sorted(order)
    assert "Not released." in page and "latest draft submitted for review, v1" in page


def test_every_tagged_figure_opens_its_claim_with_the_exact_passage(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    page = site.build(run_dir)
    answer = page[page.index('id="answer"'):page.index('id="criteria"')]
    chips = re.findall(r'class="chip [a-z]+[^"]*" href="#c-([0-9a-f]{12})"', answer)
    assert set(chips) == {ids["now"][:12], ids["rate"][:12], ids["proj"][:12]}
    for s in chips:
        assert f'id="c-{s}"' in page
    now = page[page.index(f'id="c-{ids["now"][:12]}"'):]
    now = now[:now.index("</section>")]
    assert "<mark>counted 212 public benches on 1 March 2026</mark>" in now
    assert ids["r1"] in now and "characters " in now
    proj = page[page.index(f'id="c-{ids["proj"][:12]}"'):]
    proj = proj[:proj.index("</section>")]
    assert "analysis/proj.py" in proj and "years_left" in proj
    assert f'href="#c-{ids["now"][:12]}"' in proj  # its inputs link to their claims
    assert "No checker verdict" in now  # this invented run recorded no checker verdicts


def test_the_tree_shows_states_and_why_a_node_was_set_aside(
        run: tuple[Path, dict[str, str]]) -> None:
    page = site.build(run[0])
    tree = page[page.index('id="tree"'):page.index('id="checked"')]
    assert "Set aside because: Bench removals are too few to change the answer." in tree
    assert tree.count(">Answered</span>") == 2
    checked = page[page.index('id="checked"'):page.index('id="facts"')]
    assert "Q2</code> set aside: Bench removals" in checked


def test_the_criteria_are_listed_with_what_the_reviewers_recorded(
        run: tuple[Path, dict[str, str]]) -> None:
    page = site.build(run[0])
    crit = page[page.index('id="criteria"'):page.index('id="tree"')]
    assert "C1</strong> Says whether 400 benches is reached by 2030" in crit
    assert "Not checked: no final review of the shown draft was recorded." in crit
    assert 'class="said ok"' not in crit


def test_criteria_md_wins_over_the_task(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    (run_dir / "criteria.md").write_text("# Criteria\n\n- **C1.** Answers yes or no.\n"
                                         "- C2: Gives the source of today's count.\n")
    crit = site.build(run_dir)
    crit = crit[crit.index('id="criteria"'):crit.index('id="tree"')]
    assert "C1</strong> Answers yes or no." in crit and "from criteria.md" in crit


def test_hostile_markup_in_a_source_a_draft_or_a_tree_never_runs(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, ids = run
    rec = json.loads((run_dir / "store/receipts" / f"{ids['r1']}.json").read_text())
    text = run_dir / "store/text" / f"{rec['text_id']}.txt"
    os.chmod(text, 0o644)
    text.write_text(text.read_text().replace("Lowmarsh Parks Report 2026",
                                             "<script>alert('src')</script><img src=x>"))
    draft = run_dir / "register/drafts/answer.v1.md"
    os.chmod(draft, 0o644)
    draft.write_text(draft.read_text() + "\n<script>alert('draft')</script> "
                     "[click](javascript:alert(1)) <img src=x onerror=alert(2)>\n")
    tree = json.loads((run_dir / "tree.json").read_text())
    tree["nodes"][0]["statement"] = "<iframe src=//evil></iframe>"
    (run_dir / "tree.json").write_text(json.dumps(tree))
    page = site.build(run_dir)
    assert "<script>alert" not in page and "<img" not in page and "<iframe" not in page
    assert 'href="javascript:' not in page
    assert page.count("<script>") == 1  # the page's own
    assert "&lt;script&gt;alert(&#x27;src&#x27;)" in page or "&lt;script&gt;alert('src')" in page


def test_the_page_loads_nothing_from_elsewhere(run: tuple[Path, dict[str, str]]) -> None:
    page = site.build(run[0])
    assert not re.search(r'<(script|link|img|iframe)[^>]+(src|href)="(https?:)?//', page)


def test_a_folder_that_is_not_a_run_is_a_loud_error(tmp_path: Path) -> None:
    assert site.main([str(tmp_path), str(tmp_path / "out")]) == 2


def test_markdown_escapes_first_and_links_only_http() -> None:
    out = md.render("# T <b>\n\n- a **b** [x](https://e.org/?a=1&b=2)\n- [y](javascript:z)\n\n"
                    "| h | v |\n|---|---|\n| <i>1</i> | 2 [[c:0123456789ab]] |\n",
                    tag=lambda s: f"<a href='#c-{s}'>{s}</a>")
    assert "<h1>T &lt;b&gt;</h1>" in out
    assert '<a href="https://e.org/?a=1&amp;b=2" rel="noopener noreferrer">x</a>' in out
    assert "javascript" in out and 'href="javascript' not in out
    assert "<td>&lt;i&gt;1&lt;/i&gt;</td>" in out and "href='#c-0123456789ab'" in out


@pytest.mark.skipif(not (RUN1 / "run.json").exists(), reason="run 1's folder is not here")
def test_run_one_renders_every_cited_figure(tmp_path: Path) -> None:
    page = site.write(RUN1, tmp_path / "out").read_text()
    answer = page[page.index('id="answer"'):page.index('id="criteria"')]
    chips = set(re.findall(r'href="#c-([0-9a-f]{12})"', answer))
    assert len(chips) > 100 and "chip broken" not in answer
    assert all(f'id="c-{s}"' in page for s in chips)
    assert page.count('class="passage missing"') == 0
    assert "RECORDED · run 20261026-070412-2b3c98" in page
    assert len(page) < 15_000_000


def test_each_criterion_shows_the_open_material_points_raised_against_it(
        run: tuple[Path, dict[str, str]]) -> None:
    from stemma.report import runread

    r = runread.load(run[0])
    pt = {"point": "Today's count has no date.", "material": True, "criterion": "C2",
          "node": None}
    minor = {"point": "Say 'benches' once.", "material": False, "criterion": "C1", "node": None}
    r.run_facts["review_recorded"] = [
        {"version": 1, "role": "stemma:evidence-auditor", "sha256": "x", "points": [pt, minor]},
        {"version": 1, "role": "stemma:deciders-eye-reviewer", "sha256": "y", "points": []}]
    out = site.criteria_html(r)
    c1, c2 = out.split("<strong>C2</strong>")
    assert "Not checked: the review of draft v1 by Mechanical reader is not recorded." in c1
    assert 'class="said ok"' not in out
    assert "1 open material point raised against it in the reviews of draft v1" in c2
    assert "Today&#x27;s count has no date." in c2
    r.run_facts["review_recorded"].append(
        {"version": 1, "role": "stemma:mechanical-reader", "sha256": "z", "points": []})
    full = site.criteria_html(r)
    assert "Only points the reviewers marked not material" in full.split("<strong>C2")[0]
    reviews = site.reviews_html(r)
    assert "Draft v1: 2 points, 1 material. No later draft was submitted." in reviews
    assert "<code>C2</code>" in reviews


def test_one_reviewer_with_no_points_never_makes_a_criterion_green(
        run: tuple[Path, dict[str, str]]) -> None:
    """Codex probe: one review with no points, two missing, used to show green."""
    from stemma.report import runread

    r = runread.load(run[0])
    r.run_facts["review_recorded"] = [
        {"version": 1, "role": "stemma:evidence-auditor", "sha256": "x", "points": []}]
    out = site.criteria_html(r)
    assert 'class="said ok"' not in out and out.count("Not checked: the reviews of draft v1") == 2


def test_a_branch_answered_with_bounded_points_says_so_and_the_lead_response_shows(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    vdir = run_dir / "branches/b1/verdicts"
    vdir.mkdir(parents=True)
    (vdir / "1.json").write_text(json.dumps({
        "branch_id": "b1", "round": 2, "verdict": "answered", "reason": "Done.",
        "resolutions": [{"point_id": "b1.r1.1", "status": "bounded", "note": "range given"}]}))
    tree = site.build(run_dir)
    tree = tree[tree.index('id="tree"'):tree.index('id="checked"')]
    assert '<span class="state s-bounded">Bounded</span>' in tree
    assert "Branch reviewer: answered" in tree


XSS = '[click](https://example.org/`" onmouseover="alert(1)//`)'


def test_the_markdown_link_attribute_payload_stays_text(
        run: tuple[Path, dict[str, str]]) -> None:
    """Codex probe: a code span inside a link URL used to close the href attribute."""
    out = md.inline(XSS)
    assert "<a " not in out and 'onmouseover="' not in out
    assert "<code>&quot; onmouseover=&quot;alert(1)//</code>" in out
    run_dir, _ids = run
    draft = run_dir / "register/drafts/answer.v1.md"
    os.chmod(draft, 0o644)
    draft.write_text(draft.read_text() + f"\n{XSS}\n")
    page = site.build(run_dir)
    assert 'onmouseover="' not in page and "href=\"https://example.org/`" not in page


def test_nul_delimited_digits_do_not_crash_the_renderer() -> None:
    assert "1" in md.render("a \x001\x00 b `x` \x00999\x00")


def test_the_page_carries_a_strict_csp_matching_its_own_script_and_style(
        run: tuple[Path, dict[str, str]]) -> None:
    import base64
    import hashlib

    page = site.build(run[0])
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page)
    assert csp is not None
    policy = csp.group(1)
    assert "default-src 'none'" in policy and "base-uri 'none'" in policy
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    style = re.search(r"<style>(.*?)</style>", page, re.DOTALL)
    assert script and style

    def h(t: str) -> str:
        return "'sha256-" + base64.b64encode(hashlib.sha256(t.encode()).digest()).decode() + "'"

    assert f"script-src {h(script.group(1))}" in policy
    assert f"style-src {h(style.group(1))}" in policy
    assert " style=" not in page and " on" + "click=" not in page


@pytest.mark.parametrize("script", ["../../outside.py", "/etc/passwd", "analysis/../x.py",
                                    "analysis/x.txt"])
def test_a_script_path_outside_analysis_is_never_read(run: tuple[Path, dict[str, str]],
                                                       script: str) -> None:
    text = site._script_text(run[0], script)
    assert text.startswith("(") and "root:" not in text


def test_a_symlinked_script_or_stored_text_is_not_embedded(
        run: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    run_dir, ids = run
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 'outside the run'\n")
    target = run_dir / "analysis/proj.py"
    target.unlink()
    target.symlink_to(outside)
    rec = json.loads((run_dir / "store/receipts" / f"{ids['r1']}.json").read_text())
    text = run_dir / "store/text" / f"{rec['text_id']}.txt"
    text.unlink()
    text.symlink_to(outside)
    page = site.build(run_dir)
    assert "outside the run" not in page
    assert "the script cannot be shown" in page


def test_a_deep_or_cyclic_tree_renders_without_recursion(
        run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    nodes = [{"id": f"N{i}", "parent": f"N{i - 1}" if i else None, "kind": "question",
              "status": "open", "statement": f"step {i}"} for i in range(1100)]
    nodes += [{"id": "A", "parent": "B", "kind": "question", "status": "open",
               "statement": "a"}, {"id": "B", "parent": "A", "kind": "question",
                                   "status": "open", "statement": "b"}]
    (run_dir / "tree.json").write_text(json.dumps({"nodes": nodes}))
    page = site.build(run_dir)
    tree = page[page.index('id="tree"'):page.index('id="checked"')]
    assert "deeper than 64 levels are not drawn" in tree
    assert "their parents form a cycle" in tree and "<code>A</code>" in tree


def test_the_banner_says_released_only_for_a_valid_release(
        run: tuple[Path, dict[str, str]]) -> None:
    from report_fixture import release

    run_dir, _ids = run
    (run_dir / "deliverable").mkdir()
    (run_dir / "deliverable/answer.md").write_text("# Planted answer\n")
    page = site.build(run_dir)
    assert "Planted answer" not in page and "<strong>Not released.</strong>" in page
    assert "nothing was edited after the run" not in page
    (run_dir / "deliverable/answer.md").unlink()
    release(run_dir)
    assert "<strong>Released</strong> (draft v1)" in site.build(run_dir)
    (run_dir / "deliverable/answer.md").write_text("# Changed after release\n")
    page = site.build(run_dir)
    assert "Release record does not match." in page and "Changed after release" not in page


def test_a_huge_file_is_refused_by_its_limit(run: tuple[Path, dict[str, str]],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir, _ids = run
    monkeypatch.setattr(site, "MAX_SCRIPT_BYTES", 10)
    assert "more than the 10 read" in site._script_text(run_dir, "analysis/proj.py")


def test_a_frame_only_run_shows_its_frame_and_exit_8(tmp_path: Path) -> None:
    run_dir = tmp_path / "frame-run"
    (run_dir / "register").mkdir(parents=True)
    (run_dir / "register/log.jsonl").write_bytes(b"")
    (run_dir / "run.json").write_text(json.dumps({"run_id": "20300101-000000-f00000",
                                                  "created_at": "2030-01-01T00:00:00Z"}))
    (run_dir / "task").mkdir()
    (run_dir / "task/task.json").write_text(json.dumps({
        "question": "Is the invented river Wend getting wider?", "scope": "question_only",
        "stop_after": "frame", "options": ["dredge", "wait"],
        "intake": {"Question type": "fact: whether a measure changes"}}))
    (run_dir / "frame.md").write_text("# Frame\n\nWorking answer: **no**.\n")
    (run_dir / "events.jsonl").write_text(json.dumps({
        "at": "2030-01-01T01:00:00Z", "event": "driver_finished", "exit_code": 8,
        "outcome": "frame_complete"}) + "\n")
    page = site.build(run_dir)
    assert "<strong>A frame-only run.</strong>" in page
    assert "<h2>The frame</h2>" in page and "Working answer: <strong>no</strong>." in page
    assert "a frame-only run: it ended once the frame" in page and "(exit 8)" in page
    assert "Options the user named, compared on the same criteria: dredge; wait." in page
    assert "Asked for the frame only" in page and "Question type" in page


def test_a_run_waiting_for_the_usage_reset_says_so(run: tuple[Path, dict[str, str]]) -> None:
    run_dir, _ids = run
    (run_dir / "status.json").write_text(json.dumps({
        "state": "waiting_for_usage_reset", "resume_at": "2030-01-01T06:00:00+00:00"}))
    page = site.build(run_dir)
    assert "Paused for the usage limit." in page and "2030-01-01 06:00:00" in page


def test_the_released_record_and_set_aside_nodes_are_shown(
        run: tuple[Path, dict[str, str]]) -> None:
    from report_fixture import release

    run_dir, _ids = run
    release(run_dir)
    (run_dir / "deliverable/record.json").write_text(json.dumps({
        "version": 1, "criteria": {"C1": "Says yes or no"},
        "set_aside": [{"id": "Q2", "statement": "Removals?", "reason": "too few to matter"},
                      {"id": "Q2a", "statement": "Removals in parks?", "under": "Q2"}],
        "assumptions": [{"branch_id": "b1", "question": "Which year?",
                         "assumption": "Calendar 2030"}],
        "branch_limits": [{"branch_id": "b1", "id": "b1.r1.1", "point": "One source only"}],
        "open_material_points": {"by_criterion": {"C1": [
            {"id": "v1.de.1", "by": "deciders-eye-reviewer", "point": "No date given"}]},
            "by_node": {}},
        "review_rounds": {"used": 1, "limit": 3, "note": "a resource limit"}}))
    tree = json.loads((run_dir / "tree.json").read_text())
    tree["nodes"].append({"id": "Q2a", "parent": "Q2", "kind": "question",
                          "status": "set_aside", "closes_by": "ancestor_set_aside",
                          "set_aside_by": "Q2", "statement": "Removals in parks?"})
    (run_dir / "tree.json").write_text(json.dumps(tree))
    page = site.build(run_dir)
    left = page[page.index("What the released answer left open"):]
    assert "set aside: too few to matter" in left and "under Q2, which was set aside" in left
    assert "Assumed in place of the user's answer (branch b1): Calendar 2030" in left
    assert "An open limit of branch b1: One source only" in left
    assert "deciders-eye-reviewer, C1): No date given" in left
    assert "Review rounds used: 1 of 3" in left
    assert "it sits under Q2, which was set aside" in page
