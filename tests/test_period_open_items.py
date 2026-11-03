"""Who owed what at the quarter's end, by party and by document."""

import csv
import dataclasses
import datetime as dt
import io
from decimal import Decimal

import pytest

from runningtab.chart import load_chart
from runningtab.period import PeriodError
from runningtab.period.balances import trial_balance
from runningtab.period.ledger import index_accounts
from runningtab.period.open_items import check_open_items_against, open_items, open_items_csv
from runningtab.period.quarter import Quarter, fiscal_year_for
from tests.conftest import EXAMPLE_BOOKS, PERIOD_ABBR, period_fiscal_years, period_gl_rows

Q3 = Quarter.parse("2026Q3")
INDEX = index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR)


def _receivables():
    return open_items(period_gl_rows(), INDEX, "Receivable", Q3.end)


def test_a_part_paid_invoice_shows_what_remains():
    best = [i for i in _receivables() if i.party == '"Best" Bikes' and not i.advance]
    # 1,500 invoiced, 600 paid against it; the 100 moved off Best Bikes in JE #9 has no reference
    assert [i.amount for i in best if i.due_date == dt.date(2026, 8, 9)] == [Decimal("900.00")]


def test_unapplied_credits_are_their_own_advance_rows():
    # JE #7: 250 received from Müller against nothing. JE #9: 100 credited to
    # Best Bikes against nothing. Each is its own document.
    advances = {(i.party, i.amount) for i in _receivables() if i.advance}
    assert advances == {("Müller, Inc.", Decimal("-250.00")), ('"Best" Bikes', Decimal("-100.00"))}


def test_every_open_receivable_by_party_and_amount():
    # By hand from the scenario table: Müller JE #4 900, JE #7 -250, JE #9 100;
    # Best Bikes JE #5 1,500 - 600 = 900, JE #9 -100. Total 1,550.
    assert sorted((i.party, i.amount) for i in _receivables()) == sorted([
        ("Müller, Inc.", Decimal("900.00")),
        ("Müller, Inc.", Decimal("-250.00")),
        ("Müller, Inc.", Decimal("100.00")),
        ('"Best" Bikes', Decimal("900.00")),
        ('"Best" Bikes', Decimal("-100.00")),
    ])


def test_the_open_items_total_equals_the_receivable_closing_balance():
    tb = trial_balance(period_gl_rows(), INDEX, Q3, fiscal_year_for(Q3, period_fiscal_years()))
    check_open_items_against(_receivables(), tb, INDEX, "Receivable")
    payables = open_items(period_gl_rows(), INDEX, "Payable", Q3.end)
    check_open_items_against(payables, tb, INDEX, "Payable")
    assert [(i.party, i.amount) for i in payables] == [("Cloud Co", Decimal("400.00"))]


def test_a_receivable_row_with_no_party_is_refused_by_name():
    rows = period_gl_rows()
    ar = next(r for r in rows if r.party)
    orphan = dataclasses.replace(ar, name="ACC-GLE-ORPHAN", party=None, party_type=None)
    with pytest.raises(PeriodError, match="ACC-GLE-ORPHAN"):
        open_items([*rows, orphan], INDEX, "Receivable", Q3.end)


def test_a_mismatch_with_the_trial_balance_is_refused():
    tb = trial_balance(period_gl_rows(), INDEX, Q3, fiscal_year_for(Q3, period_fiscal_years()))
    short = _receivables()[1:]
    with pytest.raises(PeriodError, match="Receivable"):
        check_open_items_against(short, tb, INDEX, "Receivable")


def test_the_csv_names_party_document_dates_and_amount():
    rows = list(csv.DictReader(io.StringIO(open_items_csv(_receivables()).decode("utf-8"))))
    assert set(rows[0]) == {
        "party_type",
        "party",
        "document_type",
        "document",
        "document_date",
        "due_date",
        "amount",
        "advance",
    }
