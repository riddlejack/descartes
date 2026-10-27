"""The leakage audit, on invented run folders: one clean, others with planted leaks."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from stemma.audit import leakage
from stemma.audit.leakage import AuditError, Hit, audit, load_targets
from stemma.driver.verify import slug

AS_OF = "2026-10-19"
RUN_ID = "20261027-031500-abc123"
SID = "11111111-2222-4333-8444-555555555555"
SHA = "a" * 64
RID = "r_20261027T031500_abcdefabcdef"
TARGETS: dict[str, Any] = {
    "as_of": AS_OF,
    "figures": [{"id": "revenue", "value": "4.52", "scale": "billion"},
                {"id": "margin", "value": "38.4", "unit": "percent"}],
    "phrases": [{"id": "share_class", "text": "Class C common stock"}],
}
CLEAN_TEXT = (
    "Northwind Robotics is expected to file in October 2026, after a quiet summer.\n"
    "Analysts estimate revenue near $4.5 billion and a margin around 38%.\n"
    "The bank's note of 2026-10-12 put the valuation at 14.52 billion.\n"
)


def _line(**kw: Any) -> str:
    return json.dumps(kw)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


def make_run(tmp: Path, *, capture: str = CLEAN_TEXT, search: str = "",
             mcp_text: str = "", model_text: str = "I will estimate revenue first.",
             transcript: bool = True) -> Path:
    """An invented run folder and harness folder. Every run-time stamp is after as_of,
    as in any real run graded later, so a clean run shows the stamps are skipped."""
    tmp = tmp.resolve()
    run, config = tmp / "runs" / RUN_ID, tmp / "config"
    config.mkdir(parents=True)
    _write(run / "run.json", json.dumps({
        "run_id": RUN_ID, "as_of": AS_OF, "harness": {"config_dir": str(config)}}))
    _write(run / "task" / "question.md",
           f"Predict what Northwind's S-1 will show, as of {AS_OF}.\n")
    url = "https://web.archive.org/web/20261015120000/https://news.example/2026/10/15/ipo"
    receipt = {
        "receipt_id": RID, "created_at": "2026-10-27T03:15:00.000000Z", "kind": "archive",
        "reason": "IPO coverage", "as_of": AS_OF, "status": "ok", "sha256": SHA,
        "content_type": "text/html", "charset": "utf-8", "text_path": f"store/text/{SHA}.txt",
        "final_url": url, "requested_url": "https://news.example/2026/10/15/ipo",
        "http": {"status": 200, "headers": [["Date", "Tue, 27 Oct 2026 03:15:00 GMT"],
                                            ["Memento-Datetime",
                                             "Thu, 15 Oct 2026 12:00:00 GMT"]]},
        "archive": {"timestamp": "20261015120000", "fetched_at": "2026-10-27T03:15:01Z"},
    }
    _write(run / "store" / "receipts" / f"{RID}.json", json.dumps(receipt))
    _write(run / "store" / "text" / f"{SHA}.txt", capture)
    _write(run / "store" / "bytes" / SHA, f"<html><body><p>{capture}</p></body></html>")
    _write(run / "events.jsonl", _line(
        event="session_launched", at="2026-10-27T03:14:00.000Z", source="driver",
        session="lead", role="lead", prompt="lead", session_id=SID, resume=False,
        model="m", effort="high", request_id=None, argv_sha256="b" * 64) + "\n")
    result = json.dumps({**receipt, "warnings": []}) if not mcp_text else json.dumps(
        {"sha256": SHA, "start": 0, "text": mcp_text}, ensure_ascii=True)
    stream = [
        _line(type="system", subtype="init", session_id=SID, cwd=str(run),
              model="m", tools=["mcp__plugin_stemma_stemma__capture"]),
        _line(type="assistant", session_id=SID, message={
            "id": "msg_1", "role": "assistant", "model": "m",
            "usage": {"input_tokens": 4520, "output_tokens": 452},
            "content": [{"type": "text", "text": model_text},
                        {"type": "tool_use", "id": "toolu_1",
                         "name": "mcp__plugin_stemma_stemma__read",
                         "input": {"sha256": SHA, "start": 0}}]}),
        _line(type="user", session_id=SID, timestamp="2026-10-27T03:16:00Z", message={
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                         "content": [{"type": "text", "text": result}]}]}),
        _line(type="result", subtype="success", session_id=SID, result="Done.",
              usage={"input_tokens": 4520}, total_cost_usd=4.52, duration_ms=45200),
    ]
    _write(run / ".stemma" / "sessions" / "lead.stream.jsonl", "\n".join(stream) + "\n")
    if transcript:
        main = config / "projects" / slug(run) / f"{SID}.jsonl"
        _write(main, "\n".join(
            _line(**{**json.loads(x), "timestamp": "2026-10-27T03:16:00.000Z",
                     "uuid": "u", "cwd": str(run)}) for x in stream[1:3]) + "\n")
        hits = search or "Northwind is said to be preparing a listing; no figures yet."
        sub = [
            _line(type="assistant", isSidechain=True, timestamp="2026-10-27T03:17:00Z",
                  message={"role": "assistant", "content": [
                      {"type": "tool_use", "id": "toolu_9", "name": "WebSearch",
                       "input": {"query": "Northwind Robotics IPO revenue"}}]}),
            _line(type="user", isSidechain=True, timestamp="2026-10-27T03:17:05Z",
                  message={"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "toolu_9",
                       "content": f"Web search results for query: Northwind\n\n{hits}"}]},
                  toolUseResult={"query": "Northwind Robotics IPO revenue",
                                 "results": [{"title": "Northwind listing",
                                              "url": "https://news.example/ipo"}],
                                 "durationSeconds": 2.1}),
        ]
        _write(main.with_suffix("") / "subagents" / "agent-a1.jsonl", "\n".join(sub) + "\n")
    return run


def targets_file(tmp: Path, **over: Any) -> Path:
    p = tmp / "targets.json"
    p.write_text(json.dumps({**TARGETS, **over}), "utf-8")
    return p


def run_audit(tmp: Path, run: Path, **over: Any) -> leakage.Report:
    return audit(run, date.fromisoformat(AS_OF), load_targets(targets_file(tmp, **over)))


def only(hits: list[Hit], **kw: Any) -> list[Hit]:
    return [h for h in hits if all(getattr(h, k) == v for k, v in kw.items())]


# The clean fixture -------------------------------------------------------------------

def test_a_clean_run_has_no_hit_and_is_complete(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    out = tmp_path / "out"
    code = leakage.main([str(run), "--as-of", AS_OF, "--targets",
                         str(targets_file(tmp_path)), "--out", str(out)])
    report = json.loads((out / "leakage.json").read_text())
    assert report["hits"] == [] and report["complete"] is True and code == 0
    assert report["read"] == {"receipt": 1, "capture_text": 1, "capture_bytes": 1,
                              "task": 1, "session_stream": 1, "transcript": 1,
                              "subagent_transcript": 1, "driver_log": 1}
    # The run-time stamps were there, after as_of, and were skipped by rule, counted.
    skipped = report["skipped"]
    for rule in ("run-time field created_at", "run-time field fetched_at",
                 "run-time field timestamp", "HTTP date header", "run-time identifier",
                 "harness measurement usage", "harness field message.usage",
                 "the as_of date in a task, prompt or model text"):
        assert skipped.get(rule, 0) > 0, rule
    md = (out / "leakage.md").read_text()
    assert "Every record the audit expected was read." in md
    assert "- revenue: not found anywhere the audit read." in md


# Planted leaks -----------------------------------------------------------------------

def test_a_date_after_as_of_in_a_capture_is_reported_where_it_is(tmp_path: Path) -> None:
    text = CLEAN_TEXT + "The registration statement was filed on\nOctober 26, 2026.\n"
    report = run_audit(tmp_path, make_run(tmp_path, capture=text))
    got = only(report.hits, kind="date", source="capture_text")
    assert [(h.found, h.reading, h.line) for h in got] == [
        ("October 26, 2026", "2026-10-26", 5)]
    h = got[0]
    assert h.origin == "captured" and h.file == f"store/text/{SHA}.txt"
    assert h.receipts == [(f"{RID} https://web.archive.org/web/20261015120000/"
                           "https://news.example/2026/10/15/ipo")]
    assert "after as_of" in h.detail and "filed on October 26, 2026" in h.snippet
    assert only(report.hits, kind="date", source="capture_bytes")  # the stored HTML too


def test_a_target_number_in_a_search_result_is_reported(tmp_path: Path) -> None:
    run = make_run(tmp_path, search="Northwind posted revenue of $4.52 billion in its S-1.")
    report = run_audit(tmp_path, run)
    got = only(report.hits, target="revenue")
    assert [(h.source, h.origin, h.tool, h.found) for h in got] == [
        ("subagent_transcript", "tool_result", "WebSearch", "$4.52 billion")]
    assert got[0].path == "message.content[0].content"
    assert got[0].file.endswith(f"{SID}/subagents/agent-a1.jsonl") and got[0].line == 2
    out = tmp_path / "out"
    assert leakage.main([str(run), "--as-of", AS_OF, "--targets",
                         str(tmp_path / "targets.json"), "--out", str(out)]) == 1
    assert ("- revenue: found in 1 places; 1 in material the run took in, 0 in text a "
            "model wrote.") in (out / "leakage.md").read_text()


def test_a_figure_written_in_words_inside_a_tool_result_is_reported(tmp_path: Path) -> None:
    words = "Sales reached four point five two billion dollars — a record."
    report = run_audit(tmp_path, make_run(tmp_path, mcp_text=words))
    got = only(report.hits, target="revenue")
    assert {(h.source, h.found) for h in got} == {
        ("session_stream", "four point five two billion"),
        ("transcript", "four point five two billion")}
    for h in got:
        assert h.origin == "tool_result" and h.tool == "mcp__plugin_stemma_stemma__read"
        assert h.path == "message.content[0].content[0].text<json>.text"


@pytest.mark.parametrize("written", [
    "4.52 billion", "$4,520 million", "US$4.52bn", "4,524 million", "4.52-billion",
    "four billion five hundred twenty million", "4.52"])
def test_every_way_of_writing_the_target_is_found(tmp_path: Path, written: str) -> None:
    report = run_audit(tmp_path, make_run(tmp_path, capture=f"Revenue: {written}.\n"))
    assert only(report.hits, target="revenue", source="capture_text")


@pytest.mark.parametrize("written", [
    "4.5 billion", "14.52 billion", "4.52 million", "$5 billion", "4.53 billion",
    "45.2 billion", "in 2026"])
def test_near_figures_are_not_the_target(tmp_path: Path, written: str) -> None:
    report = run_audit(tmp_path, make_run(tmp_path, capture=f"Revenue: {written}.\n"))
    assert not only(report.hits, target="revenue")


def test_a_registered_table_form_and_a_percent(tmp_path: Path) -> None:
    figures = [{"id": "revenue", "value": "4.52", "scale": "billion", "forms": ["4,520"]},
               {"id": "margin", "value": "38.4", "unit": "percent"}]
    text = "| Revenue | 4,520 |\nGross margin was 38.4 percent; churn 38.45%.\n"
    report = run_audit(tmp_path, make_run(tmp_path, capture=text), figures=figures)
    got = only(report.hits, source="capture_text")
    assert sorted((h.target, h.found, h.detail) for h in got) == [
        ("margin", "38.4 percent", "as written"),
        ("revenue", "4,520", "a registered form"),
        ("revenue", "4,520", "written without a scale; equal if in millions")]


def test_a_phrase_across_emphasis_and_a_line_break(tmp_path: Path) -> None:
    text = "Holders of **Class C**\ncommon stock have no vote. Class CD common stock.\n"
    report = run_audit(tmp_path, make_run(tmp_path, capture=text))
    got = only(report.hits, kind="phrase", source="capture_text")
    assert [(h.found, h.line) for h in got] == [("Class C**\ncommon stock", 1)]


def test_dates_in_other_forms(tmp_path: Path) -> None:
    text = ("Snapshot https://web.archive.org/web/20261021093000/https://x.example/ ;"
            " story https://news.example/2026/10/20/draft ; dated 11/12/26 ;"
            " due November 2026 ; on 19 October 2026 ; hash a20261027b ; 2026-10-18.\n")
    report = run_audit(tmp_path, make_run(tmp_path, capture=text))
    got = only(report.hits, kind="date", source="capture_text")
    assert [(h.found, h.reading) for h in got] == [
        ("2026/10/20", "2026-10-20"), ("20261021093000", "2026-10-21"),
        ("19 October 2026", "2026-10-19"), ("11/12/26", "2026-12-11"),
        ("November 2026", "2026-11-01")]
    assert "on as_of" in got[2].detail and "2026-11-12 or 2026-12-11" in got[3].detail


def test_a_figure_only_the_model_wrote_is_told_apart(tmp_path: Path) -> None:
    run = make_run(tmp_path, model_text="My estimate: revenue of $4.52 billion.")
    report = run_audit(tmp_path, run)
    got = only(report.hits, target="revenue")
    assert {h.origin for h in got} == {"model"} and len(got) == 2  # stream and transcript
    md = leakage.summary(report, load_targets(tmp_path / "targets.json"))
    assert "found in 2 places; 0 in material the run took in, 2 in text a model wrote" in md


# Loud failures -----------------------------------------------------------------------

def test_a_missing_transcript_makes_the_report_incomplete(tmp_path: Path) -> None:
    run = make_run(tmp_path, transcript=False)
    out = tmp_path / "out"
    code = leakage.main([str(run), "--as-of", AS_OF, "--targets",
                         str(targets_file(tmp_path)), "--out", str(out)])
    assert code == 3
    report = json.loads((out / "leakage.json").read_text())
    assert report["complete"] is False
    assert report["gaps"] == [(f"transcript of lead ({SID}): not found under "
                               f"{(tmp_path / 'config').resolve()}")]
    assert "**This audit is incomplete.**" in (out / "leakage.md").read_text()


def test_a_missing_stream_or_event_log_is_a_gap(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    (run / ".stemma" / "sessions" / "lead.stream.jsonl").unlink()
    assert run_audit(tmp_path, run).gaps == [
        ".stemma/sessions/lead.stream.jsonl: missing for launched session lead"]
    (run / "events.jsonl").unlink()
    assert run_audit(tmp_path, run).gaps == [
        "events.jsonl: missing, so the run's sessions cannot be listed"]


def test_a_different_as_of_refuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = make_run(tmp_path)
    t = targets_file(tmp_path)
    args = ["--targets", str(t), "--out", str(tmp_path / "o")]
    assert leakage.main([str(run), "--as-of", "2026-10-20", *args]) == 2
    assert "not the audit's 2026-10-20" in capsys.readouterr().err
    with pytest.raises(AuditError, match="run.json as_of is '2026-10-19', not 2026-10-20"):
        audit(run, date(2026, 10, 20), load_targets(targets_file(tmp_path, as_of="2026-10-20")))


def test_bad_targets_refuse(tmp_path: Path) -> None:
    for over in ({"figures": [{"id": "x", "value": "about four"}]},
                 {"figures": [{"id": "x", "value": "4", "scale": "lots"}]},
                 {"phrases": [{"id": "revenue", "text": "dup"}]},
                 {"figures": [], "phrases": []}, {"extra": 1}):
        with pytest.raises(AuditError):
            load_targets(targets_file(tmp_path, **over))


def test_a_report_is_never_overwritten(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    args = [str(run), "--as-of", AS_OF, "--targets", str(targets_file(tmp_path)),
            "--out", str(tmp_path / "out")]
    assert leakage.main(args) == 0
    assert leakage.main(args) == 2
