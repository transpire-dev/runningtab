"""The bank's balance at the quarter's end next to the ledger's."""

import copy
import csv
import dataclasses
import datetime as dt
import io
import re
from decimal import Decimal

import pytest

from runningtab.chart import load_chart
from runningtab.health import reconcile, settled_total
from runningtab.mercury.mapping import parse_mapping
from runningtab.mercury.models import parse_account, parse_credit_account, parse_status
from runningtab.period import PeriodError
from runningtab.period.balances import trial_balance
from runningtab.period.bank import bank_balance_through, bank_reconciliation, bank_reconciliation_csv, read_ledgers
from runningtab.period.ledger import index_accounts
from runningtab.period.quarter import Quarter, fiscal_year_for
from tests.conftest import (
    EXAMPLE_BOOKS,
    PERIOD_ABBR,
    TEST_MAPPING_TOML,
    payload,
    period_fiscal_years,
    period_gl_rows,
    period_ledgers,
)
from tests.test_health import ledgers as unbalanced_ledgers


def test_the_ingest_fixtures_satisfy_the_balance_invariant():
    # Every test below that expects no refusal depends on this. If it fails,
    # stop and report to the controller: the fixtures, not the code, are wrong.
    assert all(r.reconciles for r in reconcile(period_ledgers()))


def test_no_mercury_accounts_at_all_is_refused_as_a_short_read():
    with pytest.raises(PeriodError, match="no accounts"):
        bank_reconciliation([], parse_mapping(TEST_MAPPING_TOML), _FakeTB({}), dt.date(2026, 9, 30))


class _FakeMercuryReader:
    """Only `accounts`/`credit_accounts`/`account_transactions`, answered from the ingest fixtures."""

    def __init__(self, accounts, credit_accounts) -> None:
        self._accounts = accounts
        self._credit_accounts = credit_accounts

    def accounts(self):
        return self._accounts

    def credit_accounts(self):
        return self._credit_accounts

    def account_transactions(self, account_id: str, *, start: dt.datetime | None = None):
        raise AssertionError(f"account_transactions({account_id!r}) called; the inception-date check must run first")


def test_a_mercury_account_with_no_inception_date_is_refused_by_name():
    # health.read_ledgers passes start=account.created_at; None silently
    # falls back to Mercury's own last-30-days default, reading a shorter
    # ledger than the quarter needs with nothing to say so.
    accounts_payload = copy.deepcopy(payload("accounts"))
    del accounts_payload["accounts"][0]["createdAt"]
    accounts = [parse_account(row) for row in accounts_payload["accounts"]]
    credit_accounts = [parse_credit_account(row) for row in payload("credit")["accounts"]]
    client = _FakeMercuryReader(accounts, credit_accounts)

    with pytest.raises(PeriodError, match=re.escape(accounts[0].id)):
        read_ledgers(client)


def test_a_credit_account_with_no_inception_date_is_refused_by_name():
    accounts = [parse_account(row) for row in payload("accounts")["accounts"]]
    credit_payload = copy.deepcopy(payload("credit"))
    del credit_payload["accounts"][0]["createdAt"]
    credit_accounts = [parse_credit_account(row) for row in credit_payload["accounts"]]
    client = _FakeMercuryReader(accounts, credit_accounts)

    with pytest.raises(PeriodError, match=re.escape(credit_accounts[0].id)):
        read_ledgers(client)


def test_every_account_with_an_inception_date_reads_the_ledgers():
    accounts = [parse_account(row) for row in payload("accounts")["accounts"]]
    credit_accounts = [parse_credit_account(row) for row in payload("credit")["accounts"]]

    class _Client(_FakeMercuryReader):
        def account_transactions(self, account_id: str, *, start: dt.datetime | None = None):
            assert start is not None
            return []

    ledgers = read_ledgers(_Client(accounts, credit_accounts))
    assert {ledger.account_id for ledger in ledgers} == {a.id for a in (*accounts, *credit_accounts)}


def test_the_account_lists_are_each_fetched_exactly_once():
    # The inception-date check and the ledger build both need
    # accounts()/credit_accounts(); a second, separate fetch inside
    # health.read_ledgers would double the read for no reason.
    accounts = [parse_account(row) for row in payload("accounts")["accounts"]]
    credit_accounts = [parse_credit_account(row) for row in payload("credit")["accounts"]]

    class _RecordingClient(_FakeMercuryReader):
        def __init__(self, accounts, credit_accounts) -> None:
            super().__init__(accounts, credit_accounts)
            self.accounts_calls = 0
            self.credit_accounts_calls = 0

        def accounts(self):
            self.accounts_calls += 1
            return super().accounts()

        def credit_accounts(self):
            self.credit_accounts_calls += 1
            return super().credit_accounts()

        def account_transactions(self, account_id: str, *, start: dt.datetime | None = None):
            assert start is not None
            return []

    client = _RecordingClient(accounts, credit_accounts)
    read_ledgers(client)
    assert client.accounts_calls == 1
    assert client.credit_accounts_calls == 1


def test_the_balance_through_a_date_counts_only_settled_rows_up_to_it():
    ledger = period_ledgers()[0]
    everything = bank_balance_through(ledger, dt.date(2100, 1, 1))
    assert everything == settled_total(ledger.transactions)
    earliest = min(t.accounting_date for t in ledger.transactions if t.is_postable)
    assert bank_balance_through(ledger, earliest - dt.timedelta(days=1)) == 0


def test_an_account_that_fails_the_invariant_is_refused_by_name():
    # `unbalanced_ledgers()` bare reconciles cleanly (see test_health's
    # test_every_account_reconciles_including_the_one_accounts_omits); the
    # fixture that deliberately fails is the unpaged read.
    failing = unbalanced_ledgers(checking_fixture="transactions-checking-unpaged")
    with pytest.raises(PeriodError, match="balance invariant"):
        bank_reconciliation(failing, parse_mapping(TEST_MAPPING_TOML), _FakeTB({}), dt.date(2026, 9, 30))


def test_an_unclassified_transaction_status_is_refused_by_name():
    # `_with_status` from test_health.py, inlined: a transaction wearing a
    # status nobody has classified must not be silently dropped from
    # `bank_balance_through` with an unknown accounting treatment.
    accounts = period_ledgers()
    parsed, raw = parse_status("cancelled")
    extra = dataclasses.replace(
        accounts[0].transactions[0], id="added", amount=Decimal(0), status=parsed, raw_status=raw
    )
    accounts[0] = dataclasses.replace(accounts[0], transactions=(*accounts[0].transactions, extra))
    with pytest.raises(PeriodError, match=accounts[0].label + ".*cancelled"):
        bank_reconciliation(accounts, parse_mapping(TEST_MAPPING_TOML), _FakeTB({}), dt.date(2026, 9, 30))


def test_an_unmapped_account_is_refused_by_name():
    ledgers = period_ledgers()
    with pytest.raises(PeriodError, match=ledgers[0].account_id):
        bank_reconciliation(ledgers, parse_mapping("[accounts]\n"), _FakeTB({}), dt.date(2100, 1, 1))


def test_two_mercury_accounts_mapped_to_the_same_chart_account_are_refused():
    # Each would be compared against 1111's whole closing balance rather
    # than its own share of it -- a false difference for at least one.
    dup_toml = TEST_MAPPING_TOML.replace('"acct_chk_2" = "1112"', '"acct_chk_2" = "1111"')
    with pytest.raises(PeriodError, match=r"1111.*acct_chk_1.*acct_chk_2"):
        bank_reconciliation(period_ledgers(), parse_mapping(dup_toml), _FakeTB({}), dt.date(2100, 1, 1))


class _FakeTB:
    """Only `closing(number)`; the trial balance itself is tested in test_period_balances."""

    def __init__(self, closings: dict[str, Decimal]) -> None:
        self._c = closings

    def closing(self, number: str) -> Decimal:
        return self._c.get(number, Decimal(0))


def test_a_mapped_group_account_is_refused_by_name():
    # "1110" (Bank Accounts) is a group in chart.coa, not a postable account,
    # so it has no line in the trial balance and today surfaces as a bare
    # KeyError. TEST_MAPPING_TOML's acct_chk_2 is retargeted to it.
    q3 = Quarter.parse("2026Q3")
    tb = trial_balance(
        period_gl_rows(),
        index_accounts(load_chart(EXAMPLE_BOOKS.chart_path), PERIOD_ABBR),
        q3,
        fiscal_year_for(q3, period_fiscal_years()),
    )
    bad_toml = TEST_MAPPING_TOML.replace('"acct_chk_2" = "1112"', '"acct_chk_2" = "1110"')
    with pytest.raises(PeriodError, match=r"acct_chk_2.*1110"):
        bank_reconciliation(period_ledgers(), parse_mapping(bad_toml), tb, dt.date(2026, 9, 30))


def test_a_reconciled_credit_card_shows_no_false_difference_from_its_sign():
    # Evidence against a dismissed review finding: the claim was that the
    # card account shows a false difference because Mercury reports the
    # amount owed as positive. It does not -- see
    # tests/fixtures/mercury/ingest/credit.json (currentBalance: -159.0;
    # card charges are negative), and the balance invariant would refuse
    # any sign disagreement between the settled sum and currentBalance
    # before bank_reconciliation ever ran. A trial balance stand-in whose
    # 2121 closing equals the card's real settled sum (-159.00) proves a
    # reconciled card with a non-zero balance reports difference 0.00, not
    # a false 318.00 (double the balance) from a sign mismatch.
    ledgers = period_ledgers()
    tb = _FakeTB({"2121": Decimal("-159.00")})
    lines = bank_reconciliation(ledgers, parse_mapping(TEST_MAPPING_TOML), tb, dt.date(2100, 1, 1))
    card = next(line for line in lines if line.number == "2121")
    assert card.bank == Decimal("-159.00")
    assert card.ledger == Decimal("-159.00")
    assert card.difference == Decimal("0.00")


def test_a_difference_is_reported_not_hidden():
    ledgers = period_ledgers()
    end = dt.date(2100, 1, 1)
    lines = bank_reconciliation(ledgers, parse_mapping(TEST_MAPPING_TOML), _FakeTB({}), end)
    assert {line.number for line in lines} == {"1111", "1112", "2121"}
    assert all(line.difference == line.bank - line.ledger for line in lines)
    rows = list(csv.DictReader(io.StringIO(bank_reconciliation_csv(lines).decode("utf-8"))))
    assert set(rows[0]) == {"number", "account", "mercury_account", "bank_balance", "ledger_balance", "difference"}
