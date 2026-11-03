"""How every file in the package writes money and rows."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence
from decimal import Decimal

from runningtab.period import PeriodError

_CENT = Decimal("0.01")

# A cell starting with one of these opens as a formula, not text, the moment
# a spreadsheet opens the file -- and a bank counterparty's remarks are text
# from outside our control, not something we authored. A tab or carriage
# return can carry a formula character into a cell a naive check on the
# first character would miss (some readers treat leading whitespace as part
# of the formula prefix), so both lead the same way `=`, `+`, and `@` do.
_LEADING_FORMULA_CHARS = ("=", "+", "@", "\t", "\r")

# `-` alone is not a formula character in any spreadsheet, but Excel's own
# CSV import treats a `-` cell as one anyway unless what follows is a bare
# number -- so a plain negative amount like `-12.00` (money()'s own output)
# must stay untouched while anything else starting with `-` gets escaped.
_PLAIN_NEGATIVE_NUMBER = re.compile(r"^-\d+(\.\d+)?$")


def money(value: Decimal) -> str:
    """Two places, no separator. Refuses a value that is not a whole number of cents.

    The ledger stores cents; a third decimal place means an upstream parse
    went wrong, and rounding it away would hide that.
    """
    quantized = value.quantize(_CENT)
    if quantized != value:
        raise PeriodError(f"amount {value} is not a whole number of cents")
    if quantized.is_zero():
        quantized = abs(quantized)
    return f"{quantized:f}"


def is_formula_like(cell: str) -> bool:
    """Whether a spreadsheet would read `cell` as a formula instead of text.

    Exported (not just used by `spreadsheet_safe_cell` below) so
    `runningtab.period.qbo` can apply the identical rule to the QBO kit's Name
    and Account Name columns -- which QuickBooks matches by exact text and
    so cannot be escaped the way a human-facing cell can, only refused. One
    predicate, so the two rules cannot drift apart.
    """
    if cell.startswith(_LEADING_FORMULA_CHARS):
        return True
    return cell.startswith("-") and not _PLAIN_NEGATIVE_NUMBER.match(cell)


def spreadsheet_safe_cell(cell: str) -> str:
    """`cell`, prefixed with `'` if a spreadsheet would otherwise read it as a formula.

    The apostrophe is Excel's and Sheets' own text-marker convention: it is
    never itself shown, so a person reading the cell sees exactly the text
    that was there, just no longer live. Exported for `runningtab.period.qbo`'s
    Journal/Description column: free text, not a key QuickBooks matches on,
    so it is escaped the same way the human-facing CSVs are rather than
    refused.
    """
    return f"'{cell}" if is_formula_like(cell) else cell


def render_csv(header: Sequence[str], rows: Iterable[Sequence[str]], *, spreadsheet_safe: bool = True) -> bytes:
    """UTF-8 CSV with a header row and `\\n` line endings, identical for identical input.

    `spreadsheet_safe` (the default) escapes any cell that would open as a
    formula in a spreadsheet -- remarks carry bank counterparty text, which
    is input from outside this system, not something we authored. Pass
    `False` for the `qbo/` import files: QBO must receive exactly what was
    computed, with nothing added.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    if spreadsheet_safe:
        rows = ([spreadsheet_safe_cell(cell) for cell in row] for row in rows)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
