"""Render a chart-of-accounts page's account tables from `chart.coa`.

Two marker pairs are rewritten in place, nothing else is touched:
`<!-- chart:table NUMBER -->` … `<!-- chart:end -->` (the subtree rooted at
NUMBER, in file order) and `<!-- chart:defaults -->` … `<!-- chart:end -->`
(the Company defaults). Tables are padded the way prettier pads them, so the
pre-commit hook is a no-op on the output; if prettier disagrees, prettier wins.

Which constrains the markdown a chart note may use: prettier rewrites
`*italic*` to `_italic_`, so a note containing one renders, gets rewritten by
the hook, and then fails `test_docs_chart_tables_are_generated` forever -- the
generator keeps emitting what `chart.coa` says. Bold (`**`) and code spans
survive untouched; use those.
"""

from __future__ import annotations

import re

from runningtab.chart import ChartDocument, ChartEntry, ChartError

# Mostly a literal transcription of the [defaults] field name: replace `_`
# with a space, capitalize the first letter. Three rows are not, because the
# ERPNext field name does not describe the setting the way a person reading
# the doc does:
#
# - default_expense_account: ERPNext's own field is named as a general
#   "expense account" default, but the doc already calls out (in "Default
#   accounts") that nothing else falls back to it -- it *is* the
#   cost-of-goods-sold default, so the label says that instead.
# - exchange_gain_loss_account: a literal transform gives "Exchange gain
#   loss account"; the doc reads it as "gain / loss", matching how the pair
#   is written everywhere else on the page (e.g. "Gain / Loss on Asset
#   Disposal").
# - disposal_account: the field name alone ("disposal account") does not say
#   what kind of disposal event or what gets posted; the label spells out
#   that this is the gain-or-loss side of an asset disposal.
_DEFAULT_LABELS: dict[str, str] = {
    "default_bank_account": "Default bank account",
    "default_cash_account": "Default cash account",
    "default_receivable_account": "Default receivable account",
    "default_payable_account": "Default payable account",
    "default_expense_account": "Default cost of goods sold account",
    "default_income_account": "Default income account",
    "round_off_account": "Round off account",
    "exchange_gain_loss_account": "Exchange gain / loss account",
    "write_off_account": "Write off account",
    "default_deferred_revenue_account": "Default deferred revenue account",
    "default_deferred_expense_account": "Default deferred expense account",
    "accumulated_depreciation_account": "Accumulated depreciation account",
    "depreciation_expense_account": "Depreciation expense account",
    "disposal_account": "Gain / loss account on asset disposal",
}

_ACCOUNT_HEADERS = ["Number", "Account", "Type", "What goes here"]
_DEFAULTS_HEADERS = ["Company setting", "Account"]

_TABLE_RE = re.compile(r"chart:table\s+(\d+)")
_DEFAULTS_RE = re.compile(r"chart:defaults")
_END_RE = re.compile(r"chart:end")
_ANY_MARKER_RE = re.compile(r"<!--\s*(chart:[a-z:0-9 ]*?)\s*-->")


def _escape(cell: str) -> str:
    """Escape a literal `|` so it cannot be read as a table column break."""
    return cell.replace("|", "\\|")


def _pad_table(headers: list[str], rows: list[list[str]]) -> str:
    """A GFM pipe table, each column padded to its widest cell -- prettier's own rule.

    Escapes every row cell before measuring or emitting it: `chart.coa`
    permits a literal `|` in an account name just as freely as in a note,
    and an unescaped one reads as a column break -- silently producing a
    table row with an extra column instead of the cell text it was meant
    to hold. Escaping here, once, centrally, means no future column ever
    has to remember to call `_escape` itself.
    """
    escaped_rows = [[_escape(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in escaped_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def format_row(cells: list[str]) -> str:
        return "| " + " | ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)) + " |"

    lines = [format_row(headers), format_row(["-" * width for width in widths])]
    lines.extend(format_row(row) for row in escaped_rows)
    return "\n".join(lines) + "\n"


def _children_by_parent(entries: list[ChartEntry]) -> dict[str | None, list[ChartEntry]]:
    children: dict[str | None, list[ChartEntry]] = {}
    for entry in entries:
        children.setdefault(entry.account.parent_number, []).append(entry)
    return children


def _subtree(
    number: str, by_number: dict[str, ChartEntry], children: dict[str | None, list[ChartEntry]]
) -> list[ChartEntry]:
    if number not in by_number:
        raise ChartError(f"chart:table {number}: no account {number} in the chart")
    ordered: list[ChartEntry] = []

    def walk(current: str) -> None:
        ordered.append(by_number[current])
        for child in children.get(current, []):
            walk(child.account.number)

    walk(number)
    return ordered


def _render_account_table(entries: list[ChartEntry]) -> str:
    rows = []
    for entry in entries:
        account = entry.account
        type_cell = "Group" if account.is_group else (account.account_type or "")
        rows.append([account.number, account.name, type_cell, entry.note])
    return _pad_table(_ACCOUNT_HEADERS, rows)


def _render_defaults_table(defaults: dict[str, str]) -> str:
    rows = []
    for field, number in defaults.items():
        label = _DEFAULT_LABELS.get(field)
        if label is None:
            raise ChartError(f"chart:defaults: no doc label mapped for default field {field!r}")
        rows.append([label, number])
    return _pad_table(_DEFAULTS_HEADERS, rows)


def render_doc(doc_text: str, chart: ChartDocument) -> str:
    """Rewrite every marked table in `doc_text`, leaving everything else untouched.

    Pure: takes the chart already parsed and the doc's current text, returns
    the doc's new text. Raises `ChartError` if a marker is unknown,
    unbalanced (a start with no `chart:end`, or a stray `chart:end`), or a
    `chart:table` names a number absent from the chart.
    """
    by_number = {entry.account.number: entry for entry in chart.entries}
    children = _children_by_parent(chart.entries)

    lines = doc_text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if _END_RE.search(stripped):
            raise ChartError(f"line {i + 1}: 'chart:end' with no matching start marker")

        table_match = _TABLE_RE.search(stripped)
        is_defaults = _DEFAULTS_RE.search(stripped) is not None
        if not table_match and not is_defaults:
            unknown = _ANY_MARKER_RE.search(stripped)
            if unknown:
                raise ChartError(f"line {i + 1}: unknown marker {unknown.group(1)!r}")
            out.append(line)
            i += 1
            continue

        out.append(line)  # the start marker itself, verbatim
        start_line = i
        i += 1
        while i < len(lines) and not _END_RE.search(lines[i].strip()):
            i += 1
        if i >= len(lines):
            raise ChartError(f"line {start_line + 1}: {stripped!r} has no matching 'chart:end'")

        rendered = (
            _render_account_table(_subtree(table_match.group(1), by_number, children))
            if table_match
            else _render_defaults_table(chart.defaults)
        )
        out.append("\n")
        out.append(rendered)
        out.append("\n")
        out.append(lines[i])  # the end marker itself, verbatim
        i += 1

    return "".join(out)
