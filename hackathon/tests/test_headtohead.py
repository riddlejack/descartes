"""The head-to-head harness: the bare arm's launcher (never launched here) and the scorer,
with a fake web and a fake judge. Every figure and source here is invented."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from hackathon.headtohead import bare, score
from stemma.register import tags

REPO = Path(__file__).resolve().parents[2]

SOURCE_A = "Lowmarsh Parks Report. The parks office counted 212 benches in 2026, up 18%."
SOURCE_B = "Council minutes. The council funds 40 benches a year; 1,250 were planned."
STEMMA_MD = ("# Answer\n\nThere are 212 benches today [[c:aaaaaaaaaaaa]] in 2026 "
             "[[nc:a year]].\n\n- Funding: 45 a year [[c:bbbbbbbbbbbb]].\n\n"
             "## Limits\n\nWe did not check removals.\n")
BARE_MD = ("# Memo\n\nLowmarsh has 212 benches ([parks](https://parks.example/report)), "
           "18% more than before [1].\n\nThe council funds 30 a year [2]. "
           "See https://nowhere.example/x for 7 more.\n\n"
           "## Sources\n\n[1]: https://parks.example/report\n"
           "2. Council minutes, https://council.example/minutes\n")


def fake_capture(url: str) -> score.Captured:
    pages = {"https://parks.example/report": SOURCE_A,
             "https://council.example/minutes": SOURCE_B}
    if url in pages:
        return score.Captured(url, "ok", "r_x", "0" * 64, "store/text/x.txt", pages[url])
    return score.Captured(url, "not_found")


def test_figures_are_counted_by_one_rule_for_both_answers() -> None:
    s = [t for _seg, ts in score.figures_in(STEMMA_MD, "s") for t in ts]
    b = [t for _seg, ts in score.figures_in(BARE_MD, "b") for t in ts]
    assert s == ["212", "45"]  # the year and the claim tags are not figures
    # Links, URLs and [1] markers are not figures. The scorer uses the register's own
    # tokenizer, which read "7 more" as "7 m" until W2's fix (a word boundary after the
    # scale suffix) and as "7" after it; containment also tries the bare numeral.
    assert b[:3] == ["212", "18%", "30"] and b[3] in ("7", "7 m") and len(b) == 4


def test_bare_citations_come_from_links_urls_and_reference_markers() -> None:
    refs = score.bare_references(BARE_MD)
    assert refs == {"1": ["https://parks.example/report"],
                    "2": ["https://council.example/minutes"]}
    segs = {seg.text[:12]: seg for seg, _t in score.figures_in(BARE_MD, "b")}
    first = next(v for k, v in segs.items() if k.startswith("Lowmarsh"))
    assert score.bare_sources(first.text, refs) == ["https://parks.example/report"]


def _score(judge: Any, tmp_path: Path) -> score.Scoring:
    def stemma_src(seg: tags.Segment) -> list[str]:
        return {"aaaaaaaaaaaa": ["https://parks.example/report"],
                "bbbbbbbbbbbb": ["https://council.example/minutes"]}.get(
            (seg.claim_tags or [""])[0], [])

    refs = score.bare_references(BARE_MD)
    return score.score([("stemma", STEMMA_MD, stemma_src),
                        ("bare", BARE_MD, lambda seg: score.bare_sources(seg.text, refs))],
                       fake_capture, judge, tmp_path)


def test_containment_and_resolution_are_decided_by_code(tmp_path: Path) -> None:
    s = _score(None, tmp_path)
    c = s.as_json()["counts"]
    assert c["stemma"]["figures_stated"] == 2
    assert c["stemma"]["figures_a_cited_source_contains"] == 1  # 212; 45 is not in it
    assert c["bare"]["figures_stated"] == 4
    assert c["bare"]["figures_a_cited_source_contains"] == 2  # 212 and 18%
    assert c["bare"]["citations"] == 3 and c["bare"]["citations_that_resolve"] == 2
    assert c["stemma"]["figures_a_cited_source_contradicts"] == 0  # no judge, no count
    assert "No judge was used" in score.summary(s)


def test_the_judge_counts_only_what_its_quotes_prove(tmp_path: Path) -> None:
    seen: list[str] = []

    def judge(prompt: str, folder: Path) -> str:
        seen.append(prompt)
        if "admits a gap" in prompt:
            ans = (folder / prompt.split("`")[1]).read_text()
            real = "We did not check removals." if "removals" in ans else None
            return json.dumps([q for q in [real, "A sentence the answer never says."] if q])
        ids = [int(x) for x in __import__("re").findall(r"- id (\d+):", prompt)]
        out = []
        for i in ids:
            quote = ("The council funds 40 benches a year" if "council" in prompt
                     else "words that are not in the source")
            out.append({"id": i, "verdict": "contradicts", "quote": quote})
        out.append({"id": 999, "verdict": "contradicts", "quote": "The council funds"})
        return "```json\n" + json.dumps(out) + "\n```"

    s = _score(judge, tmp_path)
    c = s.as_json()["counts"]
    assert c["stemma"]["figures_a_cited_source_contradicts"] == 1  # 45 vs 40, quote found
    assert c["bare"]["figures_a_cited_source_contradicts"] == 1  # 30 vs 40
    assert c["stemma"]["gaps_and_limits_admitted"] == 1
    gaps = s.as_json()["gaps"]["stemma"]
    assert gaps["quote_not_found"] == ["A sentence the answer never says."]
    assert all(call["prompt"] and call["reply"] for call in s.as_json()["judge_calls"])
    assert all("Do not use outside knowledge" in p or "admits a gap" in p for p in seen)


def test_a_judge_reply_that_is_not_json_stops_the_scoring(tmp_path: Path) -> None:
    with pytest.raises(score.ScoreError, match="no JSON"):
        _score(lambda p, f: "I think so.", tmp_path)


def test_the_cli_writes_a_json_record_and_a_plain_summary(tmp_path: Path) -> None:
    sys.path.insert(0, str(REPO / "tests" / "report"))
    from report_fixture import fresh_copy  # an invented run

    run = tmp_path / "run"
    ids = fresh_copy(tmp_path / "base", run)
    stemma = tmp_path / "stemma.md"
    stemma.write_text(f"There are 212 benches [[c:{ids['now'][:12]}]].\n")
    bare_md = tmp_path / "bare.md"
    bare_md.write_text("There are 212 benches ([s](https://parks.example/report)).\n")
    out = tmp_path / "score"

    def cap(url: str) -> score.Captured:
        return score.Captured(url, "ok", text="counted 212 public benches")

    code = score.main(["--stemma", str(stemma), "--stemma-run", str(run), "--bare",
                       str(bare_md), "--out", str(out), "--judge", "none"], capture=cap)
    assert code == 0
    data = json.loads((out / "score.json").read_text())
    # The invented run's sources are attachments, not web pages: no web citation.
    assert data["counts"]["stemma"]["figures_with_a_cited_source"] == 0
    assert data["counts"]["bare"]["figures_a_cited_source_contains"] == 1
    text = (out / "summary.txt").read_text()
    assert "Figures stated: stemma 1; bare 1." in text and "|" not in text
    assert score.main(["--stemma", str(stemma), "--stemma-run", str(run), "--bare",
                       str(bare_md), "--out", str(out), "--judge", "none"]) == 2


# --------------------------------------------------------------- the bare arm
def test_the_bare_arm_gets_exactly_run_threes_task() -> None:
    t = bare.task()
    run3 = bare.run3_launcher().TASK
    assert t == run3
    p = bare.prompt_for(t)
    assert run3["question"] in p and run3["as_of"] in p
    for o in run3["options"]:
        assert o in p
    for k, v in run3["intake"].items():
        assert f"{k} {v}" in p


def test_the_dry_run_creates_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]
                                     ) -> None:
    assert bare.main(["--dry-run", "--out", str(tmp_path / "bare")]) == 0
    assert not (tmp_path / "bare").exists()
    assert '"WebSearch"' in capsys.readouterr().out


class _Proc:
    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self.stdout = io.StringIO("".join(json.dumps(x) + "\n" for x in lines))
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return 0


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("STEMMA_CLAUDE_BIN", str(binary))
    monkeypatch.setenv("STEMMA_CLAUDE_CONFIG", str(tmp_path / "config"))


def test_a_session_that_fails_the_canary_is_stopped_and_not_used(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    proc = _Proc([{"type": "system", "subtype": "init", "tools": ["Bash"]},
                  {"type": "result", "result": "an answer"}])
    with pytest.raises(bare.BareError, match="did not start clean"):
        bare.launch(tmp_path / "out", bare.task(), popen=lambda *a, **k: proc,
                    version="2.1.308 (Claude Code)")
    folder = next((tmp_path / "out").iterdir())
    assert proc.killed and not (folder / "answer.md").exists()
    assert (folder / "prompt.txt").exists() and (folder / "stream.jsonl").exists()


def test_a_clean_session_writes_its_answer_and_usage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stemma.driver import clean

    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(clean, "check_canary", lambda init, exp: [])
    proc = _Proc([{"type": "system", "subtype": "init", "session_id": "s1"},
                  {"type": "result", "subtype": "success", "is_error": False,
                   "result": "# Memo\n\nKeep the segment.", "total_cost_usd": 1.5,
                   "usage": {"output_tokens": 10}}])
    folder = bare.launch(tmp_path / "out", bare.task(), popen=lambda *a, **k: proc,
                         version="2.1.308 (Claude Code)")
    assert (folder / "answer.md").read_text() == "# Memo\n\nKeep the segment."
    assert json.loads((folder / "usage.json").read_text())["total_cost_usd"] == 1.5
    launch = json.loads((folder / "launch.json").read_text())
    assert launch["tools"] == ["WebSearch", "WebFetch"] and launch["effort"] == "high"
    assert "env" not in launch


def test_calculated_figures_are_counted_apart_and_the_judge_allows_for_drift(
        tmp_path: Path) -> None:
    prompts: list[str] = []

    def judge(prompt: str, folder: Path) -> str:
        prompts.append(prompt)
        return "[]"

    s = score.score([("stemma", STEMMA_MD, lambda seg: [],
                      lambda seg: "bbbbbbbbbbbb" in seg.claim_tags),
                     ("bare", BARE_MD, lambda seg: [])], fake_capture, judge, tmp_path)
    c = s.as_json()["counts"]
    assert c["stemma"]["figures_backed_by_a_recorded_calculation"] == 1
    assert c["bare"]["figures_backed_by_a_recorded_calculation"] == 0
    assert "outside the shared comparison" in score.summary(s)
    assert "may be newer than the answer" in score.CONTRADICT_PROMPT
