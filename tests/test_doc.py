"""`runningtab.doc.render_doc` -- the generator that keeps a books directory's chart doc's
account tables in sync with chart.coa, and the drift backstop that replaces
the old hand-maintained-table comparison."""

import pathlib

import pytest

from runningtab.chart import ChartError, load_chart_document
from runningtab.doc import render_doc
from tests.conftest import EXAMPLE_BOOKS


def _chart_path(tmp_path, text: str) -> pathlib.Path:
    path = tmp_path / "chart.coa"
    path.write_text(text)
    return path


_SAMPLE_CHART = (
    "1000 Assets                                                   [root: Asset]\n"
    "     > Root.\n"
    "  1100 Current Assets\n"
    "    1111 Primary Checking Account                             [Bank]\n"
    "         > The operating account.\n"
    "\n"
    "[defaults]\n"
    "default_bank_account = 1111\n"
)


def test_render_doc_fills_in_an_account_table(tmp_path) -> None:
    chart = load_chart_document(_chart_path(tmp_path, _SAMPLE_CHART))
    doc = "# Heading\n\n<!-- chart:table 1000 -->\nstale content\n<!-- chart:end -->\n\nTrailing prose.\n"

    rendered = render_doc(doc, chart)

    assert "stale content" not in rendered
    assert "# Heading" in rendered
    assert "Trailing prose." in rendered
    assert "<!-- chart:table 1000 -->" in rendered
    assert "<!-- chart:end -->" in rendered
    assert "| Number | Account                  | Type  | What goes here         |" in rendered
    assert "| 1000   | Assets                   | Group | Root.                  |" in rendered
    assert "| 1100   | Current Assets           | Group |                        |" in rendered
    assert "| 1111   | Primary Checking Account | Bank  | The operating account. |" in rendered


def test_render_doc_fills_in_the_defaults_table(tmp_path) -> None:
    chart = load_chart_document(_chart_path(tmp_path, _SAMPLE_CHART))
    doc = "<!-- chart:defaults -->\nstale\n<!-- chart:end -->\n"

    rendered = render_doc(doc, chart)

    assert "| Company setting      | Account |" in rendered
    assert "| Default bank account | 1111    |" in rendered


def test_render_doc_only_touches_marked_regions(tmp_path) -> None:
    chart = load_chart_document(_chart_path(tmp_path, _SAMPLE_CHART))
    doc = "Before.\n\n<!-- chart:table 1000 -->\nold\n<!-- chart:end -->\n\nAfter.\n"

    rendered = render_doc(doc, chart)

    assert rendered.startswith("Before.\n\n")
    assert rendered.rstrip("\n").endswith("After.")


def test_render_doc_is_idempotent(tmp_path) -> None:
    chart = load_chart_document(_chart_path(tmp_path, _SAMPLE_CHART))
    doc = "<!-- chart:table 1000 -->\nold\n<!-- chart:end -->\n<!-- chart:defaults -->\nold\n<!-- chart:end -->\n"

    once = render_doc(doc, chart)
    twice = render_doc(once, chart)

    assert once == twice


def test_render_doc_escapes_a_literal_pipe_in_a_note(tmp_path) -> None:
    text = (
        "1 A                                                            [root: Asset]\n"
        "  > Contains a | pipe character.\n"
    )
    chart = load_chart_document(_chart_path(tmp_path, text))
    doc = "<!-- chart:table 1 -->\nold\n<!-- chart:end -->\n"

    rendered = render_doc(doc, chart)

    assert "Contains a \\| pipe character." in rendered


def test_render_doc_escapes_a_literal_pipe_in_an_account_name(tmp_path) -> None:
    text = "1 A | B                                                        [root: Asset]\n"
    chart = load_chart_document(_chart_path(tmp_path, text))
    doc = "<!-- chart:table 1 -->\nold\n<!-- chart:end -->\n"

    rendered = render_doc(doc, chart)

    assert "A \\| B" in rendered
    # Escaped in place, not shown twice as if it were two rows.
    assert rendered.count("| 1 ") == 1


def test_unknown_marker_is_rejected(tmp_path) -> None:
    chart = load_chart_document(_chart_path(tmp_path, _SAMPLE_CHART))
    doc = "<!-- chart:nonsense -->\n<!-- chart:end -->\n"
    with pytest.raises(ChartError, match="unknown marker"):
        render_doc(doc, chart)


def test_unbalanced_start_marker_is_rejected(tmp_path) -> None:
    chart = load_chart_document(_chart_path(tmp_path, _SAMPLE_CHART))
    doc = "<!-- chart:table 1000 -->\nno end marker here\n"
    with pytest.raises(ChartError, match="no matching 'chart:end'"):
        render_doc(doc, chart)


def test_stray_end_marker_is_rejected(tmp_path) -> None:
    chart = load_chart_document(_chart_path(tmp_path, _SAMPLE_CHART))
    doc = "<!-- chart:end -->\n"
    with pytest.raises(ChartError, match="no matching start marker"):
        render_doc(doc, chart)


def test_table_naming_an_unknown_number_is_rejected(tmp_path) -> None:
    chart = load_chart_document(_chart_path(tmp_path, _SAMPLE_CHART))
    doc = "<!-- chart:table 9999 -->\n<!-- chart:end -->\n"
    with pytest.raises(ChartError, match="9999"):
        render_doc(doc, chart)


def test_defaults_field_with_no_label_mapping_is_rejected(tmp_path) -> None:
    text = _SAMPLE_CHART + "unmapped_field = 1111\n"
    chart = load_chart_document(_chart_path(tmp_path, text))
    doc = "<!-- chart:defaults -->\n<!-- chart:end -->\n"
    with pytest.raises(ChartError, match="unmapped_field"):
        render_doc(doc, chart)


# --- the example books' chart doc drift backstop ----------------------------


def test_docs_chart_tables_are_generated() -> None:
    doc_path = EXAMPLE_BOOKS.chart_doc
    chart = load_chart_document(EXAMPLE_BOOKS.chart_path)
    original = doc_path.read_text(encoding="utf-8")
    rendered = render_doc(original, chart)

    assert rendered == original, (
        f"{doc_path} is stale; run `runningtab --books {EXAMPLE_BOOKS.directory} chart render-doc`"
    )
