"""Hostile tests for figure tags (CONTRACTS section 5, "Tags").

The rule under test is fail closed: when the splitter is in doubt it makes more
segments, so more numbers must carry a tag, never fewer.
"""

from __future__ import annotations

import pytest

from stemma.register import tags

GOOD_C = "[[c:0123456789ab]]"


def untagged(markdown: str) -> list[str]:
    return [s.text for s in tags.untagged_number_segments(markdown)]


# --- 1. Malformed tags ----------------------------------------------------------------

MALFORMED = [
    "[[c:ABCDEF123456]]",
    "[[c:zzzzzzzzzzzz]]",
    "[[c:abc]]",
    "[[c: abcdef123456]]",
    "[[c:]]",
    "[[c:abcdef1234567]]",
    "[[C:abcdef123456]]",
    "[[NC:date]]",
    "[[Nc:date]]",
    "[[nc:]]",
    "[[nc:   ]]",
    "[[ c:abcdef123456]]",
    "[[c :abcdef123456]]",
    "[[nc :date]]",
    "[[c:abcdef12345]]",
    "[[c:abcdef123456]",
]


@pytest.mark.parametrize("tag", MALFORMED)
def test_malformed_tag_does_not_tag_its_segment(tag: str) -> None:
    doc = f"Revenue was 9.9bn {tag}.\n"
    assert untagged(doc) == [f"Revenue was 9.9bn {tag}."]
    assert tags.malformed_tags(doc) == [(1, tag)]


@pytest.mark.parametrize("tag", MALFORMED)
def test_malformed_tag_text_is_not_removed_before_counting(tag: str) -> None:
    seg = tags.segments(f"See {tag} above.")[0]
    assert not seg.tagged
    assert seg.number_tokens == tags.NUMBER_TOKEN.findall(seg.text)


@pytest.mark.parametrize("tag", [GOOD_C, "[[nc:date]]", "[[nc:a label, 2024]]",
                                 "[[nc: date]]"])
def test_strict_tags_tag_and_are_removed(tag: str) -> None:
    seg = tags.segments(f"Revenue was 9.9bn {tag}.")[0]
    assert seg.tagged
    assert tags.malformed_tags(f"Revenue was 9.9bn {tag}.") == []
    assert seg.number_tokens == ["9.9bn"]


def test_any_tag_is_strict() -> None:
    for tag in MALFORMED:
        assert not tags.ANY_TAG.fullmatch(tag), tag
    assert tags.ANY_TAG.fullmatch(GOOD_C)
    assert tags.ANY_TAG.fullmatch("[[nc:date]]")


def test_valid_tag_beside_a_malformed_one_does_not_tag() -> None:
    doc = f"Revenue was 9.9bn {GOOD_C} and 3bn [[c:XYZ]].\n"
    assert untagged(doc) == [doc.strip()]
    assert tags.malformed_tags(doc) == [(1, "[[c:XYZ]]")]


def test_nc_reason_cannot_swallow_a_claim_tag() -> None:
    doc = f"Revenue 5bn [[nc:see {GOOD_C}.\n"
    assert tags.malformed_tags(doc) == [(1, "[[nc:see ")]
    assert untagged(doc) == [doc.strip()]


def test_malformed_tags_reports_each_with_its_line() -> None:
    doc = "# Head [[c:abc]]\n\nFine 5 [[nc:x]].\n\n| a | 2 [[C:0123456789ab]] |\n"
    assert tags.malformed_tags(doc) == [(1, "[[c:abc]]"), (5, "[[C:0123456789ab]]")]


def test_unclosed_tag_is_malformed() -> None:
    doc = "Revenue was 9.9bn [[nc:date\n"
    assert untagged(doc) == ["Revenue was 9.9bn [[nc:date"]
    assert tags.malformed_tags(doc) == [(1, "[[nc:date")]


def test_claim_tags_only_strict() -> None:
    doc = f"A 1 {GOOD_C}. B 2 [[c:0123456789AB]]. C 3 [[c:0123456789abc]]."
    assert tags.claim_tags(doc) == ["0123456789ab"]


# --- 2. Pipe-less GFM tables ----------------------------------------------------------

def test_pipeless_gfm_table_cells_are_segments() -> None:
    doc = ("Metric | 2024 | 2025\n--- | --- | ---\nRevenue | 5bn [[nc:label]] | 7bn\n"
           "Profit | 1bn | 2bn\n")
    segs = tags.segments(doc)
    assert all(s.kind == "table_cell" for s in segs)
    assert untagged(doc) == ["2024", "2025", "7bn", "1bn", "2bn"]


def test_pipeless_table_ends_at_blank_or_pipeless_line() -> None:
    doc = "a | b\n:-- | --:\n1 [[nc:x]] | 2 [[nc:y]]\nAfter 3 rows.\n"
    segs = tags.segments(doc)
    assert [(s.kind, s.text) for s in segs][-1] == ("sentence", "After 3 rows.")
    assert untagged(doc) == ["After 3 rows."]


def test_escaped_pipe_and_tag_pipe_do_not_hide_cells() -> None:
    doc = "| a \\| 5 | 6 [[nc:x]] |\n"
    assert untagged(doc) == ["a \\| 5"]


# --- 3. Sentence ends -----------------------------------------------------------------

@pytest.mark.parametrize("doc, bad", [
    ("Profit was 2bn [[nc:x]]. €3bn went to capex.", ["€3bn went to capex."]),
    ("Profit was 2bn [[nc:x]]. margins fell to 3%.", ["margins fell to 3%."]),
    ("Profit was 2bn [[nc:x]]! 4bn next.", ["4bn next."]),
    ("Profit was 2bn [[nc:x]]? 4bn next.", ["4bn next."]),
    ("Profit was 2bn [[nc:x]]. 4bn next.", ["4bn next."]),
    ('He said "revenue rose 5%." Costs rose 9% [[nc:x]].', ['He said "revenue rose 5%."']),
    ("Costs rose 9% [[nc:x]]. (Revenue rose 5%.) Done.", ["(Revenue rose 5%.)"]),
    ("Costs (rose 9%.) Revenue 5 [[nc:x]].", ["Costs (rose 9%.)"]),
    ("Costs rose 9% [[nc:x]]。売上は5億。", ["売上は5億。"]),
])
def test_sentence_ends(doc: str, bad: list[str]) -> None:
    assert untagged(doc) == bad


def test_decimals_do_not_split() -> None:
    assert untagged("Revenue was 1.2bn [[nc:x]] and 3.4% up.") == []
    assert [s.text for s in tags.segments("A 1.2 b. C 3.4 d.")] == ["A 1.2 b.", "C 3.4 d."]


def test_tag_with_a_full_stop_inside_is_not_split() -> None:
    assert untagged("Revenue 5bn [[nc:fiscal yr. label]].") == []


# --- 4. Soft line breaks --------------------------------------------------------------

def test_each_line_is_a_segment() -> None:
    doc = "Profit was 2bn [[nc:x]]\nCosts were 3bn\n"
    assert untagged(doc) == ["Costs were 3bn"]
    assert [s.line for s in tags.segments(doc)] == [1, 2]


def test_list_item_continuation_line_is_its_own_segment() -> None:
    doc = "- Profit 2bn [[nc:x]]\n  and costs 3bn\n"
    assert untagged(doc) == ["and costs 3bn"]


def test_blockquote_lines_are_segments() -> None:
    doc = "> Profit 2bn [[nc:x]]. Costs 3bn.\n> | a | 4 |\n"
    assert untagged(doc) == ["Costs 3bn.", "4"]


# --- 5. HTML tables -------------------------------------------------------------------

def test_html_table_cells_are_segments() -> None:
    doc = ("<table><tr><td>Revenue 5bn</td><td>7bn [[nc:x]]</td></tr>\n"
           "<tr><td>Profit 1bn</td></tr></table>")
    assert untagged(doc) == ["Revenue 5bn", "Profit 1bn"]
    cells = [s for s in tags.segments(doc) if s.kind == "table_cell"]
    assert [c.text for c in cells] == ["Revenue 5bn", "7bn [[nc:x]]", "Profit 1bn"]


def test_html_cells_attributes_and_case() -> None:
    doc = '<TR><TH class="h">Year 2024</TH><Td align="right">5 [[nc:x]]</tD></TR>'
    assert untagged(doc) == ["Year 2024"]


def test_html_cell_without_close_ends_at_next_cell() -> None:
    doc = "<tr><td>Revenue 5bn [[nc:x]]<td>Profit 1bn\n"
    assert untagged(doc) == ["Profit 1bn"]


def test_html_attribute_digits_are_not_dropped() -> None:
    doc = '<tr><td title="5bn">Revenue</td></tr>'
    assert untagged(doc) != []


def test_other_html_is_not_skipped() -> None:
    assert untagged("<div>Revenue 5bn</div>") == ["<div>Revenue 5bn</div>"]
    assert untagged("<p>Revenue 5bn [[nc:x]]</p>") == []


# --- 6. Number tokens -----------------------------------------------------------------

@pytest.mark.parametrize("doc", [
    "Margin rose by ½ in the year.",
    "Up ¾ on the year.",
    "Growth of Ⅻ units.",
    "Share of ⑤ points.",
    "Area of 10² km.",
    "Revenue ١٢٣ units.",
    "Revenue １２３ units.",
    "Revenue 𝟓 units.",
    "Revenue &frac12; units.",
    "Revenue &#xbd; units.",
])
def test_unicode_numbers_are_tokens(doc: str) -> None:
    assert untagged(doc) == [doc]


@pytest.mark.parametrize("doc", [
    "Plan for FY2024 now.",
    "Use `42` here.",
    "See [link](http://x.y/42) now.",
    "As noted[^1] before.",
])
def test_numbers_kept_as_seen(doc: str) -> None:
    assert untagged(doc) == [doc]


# --- Structure ------------------------------------------------------------------------

def test_long_ordered_marker_is_text_not_a_list_marker() -> None:
    assert untagged("1234567890. Revenue rose.") == ["1234567890."]
    assert untagged("3. Revenue rose.") == []


def test_fence_info_string_and_non_closing_fence() -> None:
    doc = "```python\nx = 5\n```not-a-close 7\nstill code 8\n```\nAfter.\n"
    assert untagged(doc) == ["x = 5", "```not-a-close 7", "still code 8"]


def test_longer_fence_needs_longer_close() -> None:
    doc = "````\n```\nx = 5\n````\n"
    assert untagged(doc) == ["x = 5"]


def test_heading_sentences_split() -> None:
    assert untagged("## Revenue 5bn [[nc:x]]. Costs 3bn") == ["Costs 3bn"]


def test_list_item_sentences_split() -> None:
    assert untagged("- Revenue 5bn [[nc:x]]. Costs 3bn.") == ["Costs 3bn."]


# --- Found by a hostile pass over the splitter ------------------------------------------

def test_pipe_row_after_a_pipeless_line_is_still_split() -> None:
    doc = "a | b\n--- | ---\n1 [[nc:x]] | 2 [[nc:y]]\nplain 3 [[nc:z]]\nRev 5bn | 7bn [[nc:w]]\n"
    assert untagged(doc) == ["Rev 5bn"]


def test_table_header_in_a_list_item_is_split() -> None:
    doc = "- | 2024 | 2025 [[nc:x]] |\n  |---|---|\n  | 1 [[nc:a]] | 2 [[nc:b]] |\n"
    assert untagged(doc) == ["2024"]


def test_blockquoted_fence_closes_and_tables_after_it_split() -> None:
    doc = "> ```\n> x\n> ```\n\n| 5bn | 7bn [[nc:x]] |\n"
    assert untagged(doc) == ["5bn"]


def test_unclosed_fence_still_splits_pipes() -> None:
    doc = "```\n| 5bn | 7bn [[nc:x]] |\n"
    assert untagged(doc) == ["5bn"]


def test_html_line_break_splits() -> None:
    assert untagged("Revenue 5bn<br>Costs 3bn [[nc:x]].") == ["Revenue 5bn<br>"]
    assert untagged("Revenue 5bn<BR />Costs 3bn [[nc:x]].") == ["Revenue 5bn<BR />"]


def test_full_stop_then_capital_without_space_splits() -> None:
    assert untagged("Profit 2bn [[nc:x]].Costs 3bn.") == ["Costs 3bn."]
    assert untagged("Profit 2bn [[nc:x]].€3bn capex.") == ["€3bn capex."]


def test_link_with_digits_does_not_split_at_lowercase() -> None:
    assert untagged("See [r](https://x.com/report-2024.pdf) [[nc:source]].") == []


def test_a_scale_suffix_needs_no_letter_after_it() -> None:
    from stemma.register.tags import NUMBER_TOKEN

    assert NUMBER_TOKEN.findall("7 more, 3 kinds, 5 xyz") == ["7", "3", "5"]
    assert NUMBER_TOKEN.findall("1.2bn, 2 m, 3k and 4x faster") == ["1.2bn", "2 m", "3k", "4x"]
