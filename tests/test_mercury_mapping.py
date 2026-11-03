"""`mercury.toml`: which chart account and which ERPNext Bank Account each Mercury account is."""

from __future__ import annotations

import pytest

from runningtab.chart import load_chart
from runningtab.mercury.mapping import MappingError, load_mapping, parse_mapping, validate_against_chart
from tests.conftest import EXAMPLE_BOOKS

EXAMPLE_MERCURY_ACCOUNT_IDS = (
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
    "00000000-0000-4000-8000-000000000003",
)


def test_the_example_mapping_parses() -> None:
    mapping = load_mapping(EXAMPLE_BOOKS.mercury_path)
    assert mapping.accounts
    assert mapping.bank_accounts


def test_every_account_the_example_mapping_names_is_in_the_chart() -> None:
    # A typo'd number would otherwise surface as an ERPNext link error halfway
    # through an ingest, naming a document rather than the line that is wrong.
    mapping = load_mapping(EXAMPLE_BOOKS.mercury_path)
    assert validate_against_chart(mapping, {a.number for a in load_chart(EXAMPLE_BOOKS.chart_path)}) == []


def test_validation_names_the_account_the_chart_lacks() -> None:
    mapping = parse_mapping('[accounts]\n"acct_x" = "9999"\n[bank_accounts]\n')
    assert validate_against_chart(mapping, {"1111"}) == ["9999"]


def test_the_example_mapping_names_every_example_mercury_account() -> None:
    mapping = load_mapping(EXAMPLE_BOOKS.mercury_path)
    assert [mapping.account_for_mercury_account(account_id) for account_id in EXAMPLE_MERCURY_ACCOUNT_IDS] == [
        "1111",
        "1112",
        "2121",
    ]


def test_a_mercury_account_id_wins_over_its_name() -> None:
    mapping = parse_mapping("""
[accounts]
"acct_chk_1" = "1111"
"Transpire Checking" = "9999"
""")
    assert mapping.account_for_mercury_account("acct_chk_1", "Transpire Checking") == "1111"


def test_an_unknown_mercury_account_is_never_guessed_at(mercury_mapping) -> None:
    assert mercury_mapping.knows_account("acct_nobody") is False
    assert mercury_mapping.bank_account_for("acct_nobody") is None


def test_an_account_number_that_is_not_a_string_is_refused() -> None:
    # Chart numbers are strings; a bare 1111 in TOML is an int and would miss
    # every lookup silently.
    with pytest.raises(MappingError):
        parse_mapping('[accounts]\n"acct_chk_1" = 1111\n')


def test_bank_accounts_are_keyed_the_way_mercury_accounts_are() -> None:
    mapping = parse_mapping(
        "[accounts]\n[bank_accounts]\n"
        '"acct_chk_1" = "Mercury Checking - T"\n'
        '"Transpire Savings" = "Mercury Savings - T"\n'
    )
    assert mapping.bank_account_for("acct_chk_1") == "Mercury Checking - T"
    # Names stand in until real ids exist, and the id wins when both are there.
    assert mapping.bank_account_for("acct_chk_2", "Transpire Savings") == "Mercury Savings - T"
    assert mapping.bank_account_for("acct_credit_1") is None


def test_a_categories_table_is_refused_rather_than_ignored() -> None:
    # Nothing routes on a category any more. A file still carrying the table
    # reads as configured while every entry in it does nothing, and the person
    # who just added a line expects it to have an effect.
    with pytest.raises(MappingError) as exc:
        parse_mapping('[accounts]\n[categories]\n"Payroll" = "5210"\n[bank_accounts]\n')
    assert "[categories]" in str(exc.value)
    assert "Bank Transaction" in str(exc.value)


def test_a_posting_table_is_refused_rather_than_ignored() -> None:
    # Nothing posts to suspense any more, and the single `bank_account`
    # setting that once lived here was retired before that.
    with pytest.raises(MappingError) as exc:
        parse_mapping('[accounts]\n[bank_accounts]\n[posting]\nsuspense = "5299"\n')
    assert "[posting]" in str(exc.value)


def test_the_example_mapping_names_a_bank_account_for_every_example_mercury_account() -> None:
    # `runningtab mercury install-bank-accounts` creates these; an [accounts] entry
    # without one would leave that account's activity with nowhere to post.
    mapping = load_mapping(EXAMPLE_BOOKS.mercury_path)
    for account_id in EXAMPLE_MERCURY_ACCOUNT_IDS:
        name = mapping.bank_account_for(account_id)
        assert name is not None and name.endswith(" - Mercury"), account_id
