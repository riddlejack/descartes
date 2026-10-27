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
    assert "No final review was recorded." in crit


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
    assert "Only points the reviewers marked not material" in c1
    assert "1 open material point in the last review round (draft v1)" in c2
    assert "Today&#x27;s count has no date." in c2
    reviews = site.reviews_html(r)
    assert "Draft v1: 2 points, 1 material. No later draft was submitted." in reviews
    assert "Mechanical reader: not recorded." in reviews
    assert "<code>C2</code>" in reviews


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
