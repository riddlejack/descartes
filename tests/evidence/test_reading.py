"""Reading in parts, searching and quote checks over the run's store."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeTransport, Reply, html

from stemma.evidence import api, limits
from stemma.evidence.errors import EvidenceError
from stemma.evidence.quotes import find_quote
from stemma.evidence.service import EvidenceService

Factory = Callable[..., EvidenceService]
LONG = html(
    "<h1>Invented sector review</h1><p>Intro.</p>"
    + "".join(f"<h2>Region {i}</h2><p>Region {i} margins were {i}.5% and rising. "
              + "Background sentence for padding. " * 40 + "</p>" for i in range(1, 9))
    + "<p>The “headline” figure was 1,204.5 million.</p>"
)


@pytest.fixture
def stored(make_service: Factory) -> tuple[EvidenceService, dict[str, Any]]:
    t = FakeTransport({"https://example.org/review": Reply(200, [("Content-Type", "text/html")], LONG)})
    svc = make_service(t)
    return svc, svc.capture("https://example.org/review", "sector review")


def test_outline_and_read_section(stored: tuple[EvidenceService, dict[str, Any]]) -> None:
    svc, r = stored
    out = svc.outline(r["sha256"])
    assert [o["title"] for o in out["outline"]][:3] == ["Invented sector review", "Region 1", "Region 2"]
    sec = svc.read_section(r["sha256"], "h3")
    assert sec["title"] == "Region 2" and sec["text"].startswith("## Region 2")
    assert "Region 3" not in sec["text"]
    with pytest.raises(EvidenceError) as info:
        svc.read_section(r["sha256"], "h999")
    assert info.value.code == "unknown_anchor"


def test_read_in_parts_covers_text(stored: tuple[EvidenceService, dict[str, Any]],
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    svc, r = stored
    monkeypatch.setattr(limits, "READ_MAX_CHARS", 1000)
    parts, start = [], 0
    while start is not None:
        chunk = svc.read(r["sha256"], start, 10_000_000)  # asks for too much: capped
        assert len(chunk["text"]) <= 1000
        parts.append(chunk["text"])
        start = chunk["next_start"]
    assert "".join(parts) == api.read_text(svc.run_dir, r["receipt_id"])


def test_read_rejects_bad_input(stored: tuple[EvidenceService, dict[str, Any]]) -> None:
    svc, r = stored
    for args, code in ((("../../etc/passwd", 0, 10), "bad_sha256"),
                       ((r["sha256"], -1, 10), "bad_range"),
                       ((r["sha256"], 10**9, 10), "bad_range"),
                       ((r["sha256"], 0, 0), "bad_range"),
                       (("0" * 64, 0, 10), "no_text")):
        with pytest.raises(EvidenceError) as info:
            svc.read(*args)  # type: ignore[arg-type]
        assert info.value.code == code


def test_search_words_and_exact(stored: tuple[EvidenceService, dict[str, Any]]) -> None:
    svc, r = stored
    words = svc.search_store("margin rise", mode="words")
    assert words["hits"] and all(h["receipt_id"] == r["receipt_id"] for h in words["hits"])
    exact = svc.search_store("1,204.5", mode="exact")
    hit = exact["hits"][0]
    text = api.read_text(svc.run_dir, r["receipt_id"])
    assert text[hit["start"]:hit["end"]] == "1,204.5" and "[1,204.5]" in hit["snippet"]
    assert svc.search_store("7.5%", mode="exact")["hits"][0]["sha256"] == r["sha256"]
    assert svc.search_store("9.5%", mode="exact")["hits"] == []
    one = svc.search_store("Region", mode="words", sha256=r["sha256"], limit=3)
    assert len(one["hits"]) == 3


@pytest.mark.parametrize("query", ['" OR *', "NEAR(a b)", "a AND", '"""', "col:x", "*"])
def test_search_query_syntax_cannot_inject(stored: tuple[EvidenceService, dict[str, Any]],
                                           query: str) -> None:
    svc, _ = stored
    for mode in ("words", "exact"):
        try:
            result = svc.search_store(query, mode=mode)
            assert isinstance(result["hits"], list)
        except EvidenceError as exc:
            assert exc.code in ("bad_query", "query_too_short")


def test_search_errors(stored: tuple[EvidenceService, dict[str, Any]]) -> None:
    svc, _ = stored
    with pytest.raises(EvidenceError) as info:
        svc.search_store("12", mode="exact")
    assert info.value.code == "query_too_short"
    with pytest.raises(EvidenceError):
        svc.search_store("x", mode="fuzzy")


def test_check_quote_normalises_and_reports_offsets(
        stored: tuple[EvidenceService, dict[str, Any]]) -> None:
    svc, r = stored
    text = api.read_text(svc.run_dir, r["receipt_id"])
    result = api.check_quote(svc.run_dir, r["receipt_id"], 'The "headline"   figure\nwas 1,204.5 million')
    assert result["found"] and result["match_count"] == 1
    m = result["matches"][0]
    assert text[m["start"]:m["end"]] == "The “headline” figure was 1,204.5 million"
    assert svc.check_quote(r["receipt_id"], "figure was 1,204.6 million")["found"] is False
    # case, digits and dashes are not normalised
    assert svc.check_quote(r["receipt_id"], "the \"headline\" figure")["found"] is False


def test_check_quote_errors(make_service: Factory, run_dir: Path,
                            stored: tuple[EvidenceService, dict[str, Any]]) -> None:
    svc, r = stored
    failed = svc.capture("https://example.org/missing", "probe")  # 404 from the fake
    for receipt_id, quote, code in ((failed["receipt_id"], "x", "no_text"),
                                    ("r_20990101T000000_000000000000", "x", "unknown_receipt"),
                                    ("../../run.json", "x", "bad_receipt_id"),
                                    (r["receipt_id"], "   ", "empty_quote")):
        with pytest.raises(EvidenceError) as info:
            api.check_quote(run_dir, receipt_id, quote)
        assert info.value.code == code
    with pytest.raises(EvidenceError):
        api.read_text(run_dir, failed["receipt_id"])


def test_find_quote_whitespace_runs_map_back() -> None:
    text = "Alpha\n\n  beta gamma ‘delta’"
    m = find_quote(text, "alpha beta")
    assert m["found"] is False
    m = find_quote(text, " beta gamma 'delta' ")
    assert m["found"] and text[m["matches"][0]["start"]:m["matches"][0]["end"]] == (
        "beta gamma ‘delta’")
