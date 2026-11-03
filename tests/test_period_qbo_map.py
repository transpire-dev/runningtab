"""qbo.toml: every postable account mapped, every type real, no name QBO would reject."""

import dataclasses

import pytest

from runningtab.chart import load_chart
from runningtab.period import PeriodError
from runningtab.period.qbo_map import load_qbo_map, parse_qbo_map, validate_qbo_map
from tests.conftest import EXAMPLE_BOOKS


def test_the_shipped_file_covers_the_chart():
    validate_qbo_map(load_qbo_map(EXAMPLE_BOOKS.qbo_path), load_chart(EXAMPLE_BOOKS.chart_path))


def test_a_postable_account_with_no_entry_is_refused_by_number():
    qmap = load_qbo_map(EXAMPLE_BOOKS.qbo_path)
    missing = dict(qmap.accounts)
    del missing["1111"]
    with pytest.raises(PeriodError, match="1111"):
        validate_qbo_map(dataclasses.replace(qmap, accounts=missing), load_chart(EXAMPLE_BOOKS.chart_path))


def test_a_detail_type_intuit_does_not_list_is_refused():
    qmap = load_qbo_map(EXAMPLE_BOOKS.qbo_path)
    wrong = dict(qmap.accounts) | {"1111": (qmap.accounts["1111"][0], "Not A Detail Type")}
    with pytest.raises(PeriodError, match="Not A Detail Type"):
        validate_qbo_map(dataclasses.replace(qmap, accounts=wrong), load_chart(EXAMPLE_BOOKS.chart_path))


def test_a_group_account_entry_is_refused():
    qmap = load_qbo_map(EXAMPLE_BOOKS.qbo_path)
    grouped = dict(qmap.accounts) | {"1100": qmap.accounts["1111"]}
    with pytest.raises(PeriodError, match="1100"):
        validate_qbo_map(dataclasses.replace(qmap, accounts=grouped), load_chart(EXAMPLE_BOOKS.chart_path))


def test_an_account_both_imported_and_builtin_is_refused():
    qmap = load_qbo_map(EXAMPLE_BOOKS.qbo_path)
    both = dict(qmap.accounts) | {"1131": qmap.accounts["1111"]}
    with pytest.raises(PeriodError, match="1131"):
        validate_qbo_map(dataclasses.replace(qmap, accounts=both), load_chart(EXAMPLE_BOOKS.chart_path))


def test_a_file_missing_a_table_is_refused():
    with pytest.raises(PeriodError, match=r"\[builtin\]"):
        parse_qbo_map('[types]\n"Bank" = ["Checking"]\n[accounts]\n[clearing]\ntype = "Bank"\ndetail = "Checking"\n')


def _malformed(
    accounts_line: str = '"1111" = ["Bank", "Checking"]',
    builtin_line: str = '"1131" = { name = "A/R", role = "receivable" }',
    clearing_block: str = 'type = "Bank"\ndetail = "Checking"',
) -> str:
    return (
        '[types]\n"Bank" = ["Checking"]\n'
        f"[accounts]\n{accounts_line}\n"
        f"[builtin]\n{builtin_line}\n"
        f"[clearing]\n{clearing_block}\n"
    )


@pytest.mark.parametrize(
    ("bad_toml", "match"),
    [
        pytest.param(_malformed(accounts_line='"1111" = "Bank"'), "1111", id="account_value_is_a_bare_string"),
        pytest.param(_malformed(accounts_line='"1111" = ["Bank"]'), "1111", id="account_value_is_a_short_list"),
        pytest.param(_malformed(accounts_line='"1111" = 5'), "1111", id="account_value_is_a_number"),
        pytest.param(_malformed(builtin_line='"1131" = 5'), "1131", id="builtin_value_is_not_a_table"),
        pytest.param(
            _malformed(builtin_line='"1131" = { name = "A/R", role = "nonsense" }'),
            "nonsense",
            id="builtin_role_is_not_one_of_the_three",
        ),
        pytest.param(_malformed(clearing_block='detail = "Checking"'), "type", id="clearing_missing_type"),
        pytest.param(_malformed(clearing_block='type = "Bank"'), "detail", id="clearing_missing_detail"),
        pytest.param(
            _malformed(clearing_block='type = "Bank"\ndetail = 5'), "detail", id="clearing_detail_is_a_number"
        ),
    ],
)
def test_a_malformed_entry_is_refused_by_key_not_crashed_on(bad_toml: str, match: str) -> None:
    with pytest.raises(PeriodError, match=match):
        parse_qbo_map(bad_toml)


def test_a_malformed_types_entry_is_refused_by_key():
    bad = _malformed().replace('"Bank" = ["Checking"]', '"Bank" = "Checking"')
    with pytest.raises(PeriodError, match="Bank"):
        parse_qbo_map(bad)


def test_a_builtin_role_that_does_not_match_the_chart_accounts_erpnext_type_is_refused():
    # A [builtin] entry names which QuickBooks built-in a chart account
    # stands in for; check_replay only proves the kit reproduces itself, so
    # nothing else catches "1131" (actually Receivable in chart.coa) and
    # "2111" (actually Payable) having their roles swapped -- balances would
    # post to QBO's wrong built-in account while every other check passes.
    qmap = load_qbo_map(EXAMPLE_BOOKS.qbo_path)
    swapped = dict(qmap.builtin)
    swapped["1131"] = dataclasses.replace(swapped["1131"], role="payable")
    swapped["2111"] = dataclasses.replace(swapped["2111"], role="receivable")
    with pytest.raises(PeriodError) as excinfo:
        validate_qbo_map(dataclasses.replace(qmap, builtin=swapped), load_chart(EXAMPLE_BOOKS.chart_path))
    message = str(excinfo.value)
    assert "1131" in message and "'Receivable'" in message and "'payable'" in message
    assert "2111" in message and "'Payable'" in message and "'receivable'" in message
