# tests/test_chart.py
"""The chart file, and the validations that keep a typo out of the ledger."""

import pytest
from icedcoffee_sync.apply.model import DesiredAccount
from icedcoffee_sync.apply.order import depth
from icedcoffee_sync.erpnext.accounts import ACCOUNT_TYPES, ROOT_TYPES

from runningtab.chart import (
    NO_BANK_LINE_TAG,
    REQUIRED_DEFAULT_FIELDS,
    SUSPENSE_ACCOUNT_NUMBER,
    ChartError,
    load_chart,
    load_chart_document,
    load_chart_entries,
    load_defaults,
    suspense_account,
)
from tests.conftest import EXAMPLE_BOOKS


@pytest.fixture(scope="module")
def chart() -> list[DesiredAccount]:
    return load_chart(EXAMPLE_BOOKS.chart_path)


def test_the_real_chart_loads(chart) -> None:
    assert len(chart) > 80


def test_numbers_are_unique(chart) -> None:
    numbers = [a.number for a in chart]
    assert len(numbers) == len(set(numbers))


def test_match_keys_are_unique(chart) -> None:
    # Two accounts with the same name under the same parent would make every
    by_number = {a.number: a for a in chart}
    keys = [(a.name, by_number[a.parent_number].name if a.parent_number else None) for a in chart]
    assert len(keys) == len(set(keys))


def test_every_parent_exists(chart) -> None:
    numbers = {a.number for a in chart}
    for account in chart:
        assert account.parent_number is None or account.parent_number in numbers


def test_no_cycles(chart) -> None:
    by_number = {a.number: a for a in chart}
    for account in chart:
        depth(account, by_number)


def test_root_types_are_inherited_from_the_root(chart) -> None:
    by_number = {a.number: a for a in chart}
    for account in chart:
        assert account.root_type in ROOT_TYPES
        if account.parent_number:
            assert account.root_type == by_number[account.parent_number].root_type


def test_every_account_type_is_one_erpnext_accepts(chart) -> None:
    for account in chart:
        assert account.account_type is None or account.account_type in ACCOUNT_TYPES


def test_only_leaves_hold_postings(chart) -> None:
    parents = {a.parent_number for a in chart if a.parent_number}
    for account in chart:
        if account.number in parents:
            assert account.is_group, f"{account.number} has children but is not a group"


def test_defaults_all_name_accounts_in_the_chart() -> None:
    numbers = {a.number for a in load_chart(EXAMPLE_BOOKS.chart_path)}
    for field, number in load_defaults(EXAMPLE_BOOKS.chart_path).items():
        assert number in numbers, f"{field} points at {number}, which is not in the chart"


def test_defaults_never_point_at_a_group() -> None:
    by_number = {a.number: a for a in load_chart(EXAMPLE_BOOKS.chart_path)}
    for field, number in load_defaults(EXAMPLE_BOOKS.chart_path).items():
        assert not by_number[number].is_group, f"{field} points at group {number}"


# --- grammar ------------------------------------------------------------


def _write(tmp_path, text: str):
    path = tmp_path / "chart.coa"
    path.write_text(text)
    return path


def test_duplicate_number_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "1 B                                                            [root: Asset]\n",
    )
    with pytest.raises(ChartError, match="duplicate"):
        load_chart(path)


def test_bad_root_type_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "1 A                                                            [root: Vibes]\n")
    with pytest.raises(ChartError, match="Vibes"):
        load_chart(path)


def test_root_without_root_tag_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "1 A\n")
    with pytest.raises(ChartError, match="root:"):
        load_chart(path)


def test_an_empty_chart_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "")
    with pytest.raises(ChartError, match="no root account") as exc_info:
        load_chart(path)
    for root_type in ROOT_TYPES:
        assert root_type in str(exc_info.value)


def test_a_chart_missing_some_root_types_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 Assets                                                       [root: Asset]\n"
        "2 Expenses                                                     [root: Expense]\n",
    )
    with pytest.raises(ChartError, match="no root account") as exc_info:
        load_chart(path)
    message = str(exc_info.value)
    assert "Liability" in message
    assert "Income" in message
    assert "Equity" in message
    assert "Asset" not in message
    assert "Expense" not in message


def test_two_roots_of_the_same_type_are_rejected_even_with_every_type_present(tmp_path) -> None:
    # Every ROOT_TYPES member is covered here, so the missing-type check
    path = _write(
        tmp_path,
        "1 Assets                                                       [root: Asset]\n"
        "2 Assets Two                                                   [root: Asset]\n"
        "3 Liabilities                                                  [root: Liability]\n"
        "4 Income                                                       [root: Income]\n"
        "5 Expenses                                                     [root: Expense]\n"
        "6 Equity                                                       [root: Equity]\n",
    )
    with pytest.raises(ChartError, match="more than one root"):
        load_chart(path)


def test_two_siblings_with_the_same_name_are_rejected(tmp_path) -> None:
    # True ambiguity: diff() matches children within an already-resolved
    path = _write(
        tmp_path,
        "1 Assets                                                       [root: Asset]\n  2 Other\n  3 Other\n",
    )
    with pytest.raises(ChartError, match="cannot tell them apart"):
        load_chart(path)


def test_siblings_sharing_a_name_under_differently_identified_parents_are_accepted(tmp_path) -> None:
    defaults = "".join(f"{field} = 2\n" for field in sorted(REQUIRED_DEFAULT_FIELDS))
    path = _write(
        tmp_path,
        "1 Category                                                     [root: Asset]\n"
        "  2 Sub\n"
        "3 Liabilities                                                  [root: Liability]\n"
        "4 Income                                                       [root: Income]\n"
        "5 Expenses                                                     [root: Expense]\n"
        f"  {SUSPENSE_ACCOUNT_NUMBER} Suspense\n"
        "6 Category                                                     [root: Equity]\n"
        "  7 Sub\n"
        f"\n[defaults]\n{defaults}",
    )
    chart = load_chart(path)
    assert {a.number for a in chart} == {"1", "2", "3", "4", "5", SUSPENSE_ACCOUNT_NUMBER, "6", "7"}


def test_child_with_root_tag_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "  2 B                                                          [root: Asset]\n",
    )
    with pytest.raises(ChartError, match="not a root"):
        load_chart(path)


def test_unknown_account_type_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "  2 B                                                          [Not A Real Type]\n",
    )
    with pytest.raises(ChartError, match="Not A Real Type"):
        load_chart(path)


def test_multiple_account_type_tags_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "  2 B                                                          [Bank, Cash]\n",
    )
    with pytest.raises(ChartError, match="more than one account-type tag"):
        load_chart(path)


def test_odd_indent_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n   2 B\n",
    )
    with pytest.raises(ChartError, match="two-space levels"):
        load_chart(path)


def test_indenting_more_than_one_level_deeper_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n    2 B\n",
    )
    with pytest.raises(ChartError, match="indented more than one level"):
        load_chart(path)


def test_note_without_a_preceding_account_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "> stray note\n")
    with pytest.raises(ChartError, match="no preceding account"):
        load_chart(path)


def test_empty_tag_block_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "1 A []\n")
    with pytest.raises(ChartError, match="empty tag block"):
        load_chart(path)


def test_unterminated_tag_block_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "1 A [root: Asset\n")
    with pytest.raises(ChartError, match="closing"):
        load_chart(path)


def test_duplicate_default_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "\n[defaults]\ndefault_bank_account = 1\ndefault_bank_account = 1\n",
    )
    with pytest.raises(ChartError, match="duplicate default"):
        load_chart(path)


def test_non_numeric_default_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "\n[defaults]\ndefault_bank_account = one\n",
    )
    with pytest.raises(ChartError, match="account number"):
        load_chart(path)


def test_default_naming_a_missing_account_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "3 L                                                            [root: Liability]\n"
        "4 I                                                            [root: Income]\n"
        "5 E                                                            [root: Expense]\n"
        "6 Q                                                            [root: Equity]\n"
        "\n[defaults]\ndefault_bank_account = 2\n",
    )
    with pytest.raises(ChartError, match="not in the chart"):
        load_chart(path)


def test_default_naming_a_group_is_rejected(tmp_path) -> None:
    # A default must point at something ERPNext can actually post to.
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "  2 B                                                          \n"
        "3 L                                                            [root: Liability]\n"
        "4 I                                                            [root: Income]\n"
        "5 E                                                            [root: Expense]\n"
        "6 Q                                                            [root: Equity]\n"
        "\n[defaults]\ndefault_bank_account = 1\n",
    )
    with pytest.raises(ChartError, match="group"):
        load_chart(path)


_FIVE_ROOTS = (
    "1 Assets                                                       [root: Asset]\n"
    "2 Liabilities                                                  [root: Liability]\n"
    "3 Income                                                       [root: Income]\n"
    "4 Expenses                                                     [root: Expense]\n"
    "5 Equity                                                       [root: Equity]\n"
)


def test_an_empty_defaults_section_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, _FIVE_ROOTS + "\n[defaults]\n")
    with pytest.raises(ChartError, match="missing required field") as exc_info:
        load_chart(path)
    for field in REQUIRED_DEFAULT_FIELDS:
        assert field in str(exc_info.value)


def test_a_defaults_section_missing_some_required_fields_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, _FIVE_ROOTS + "\n[defaults]\ndefault_bank_account = 1\n")
    with pytest.raises(ChartError, match="missing required field") as exc_info:
        load_chart(path)
    message = str(exc_info.value)
    assert "default_cash_account" in message
    assert "default_bank_account" not in message


def test_a_defaults_section_with_an_unexpected_field_is_rejected(tmp_path) -> None:
    # The two lists (chart.coa's [defaults] and REQUIRED_DEFAULT_FIELDS)
    lines = "".join(f"{field} = 1\n" for field in sorted(REQUIRED_DEFAULT_FIELDS))
    path = _write(tmp_path, _FIVE_ROOTS + f"\n[defaults]\n{lines}unexpected_extra_field = 1\n")
    with pytest.raises(ChartError, match="not in REQUIRED_DEFAULT_FIELDS") as exc_info:
        load_chart(path)
    assert "unexpected_extra_field" in str(exc_info.value)


def test_second_defaults_section_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "\n[defaults]\ndefault_bank_account = 1\n\n[defaults]\n",
    )
    with pytest.raises(ChartError, match="second"):
        load_chart(path)


def test_notes_join_with_a_single_space(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n"
        "     > First line of the note,\n"
        "     > continued on a second line.\n",
    )
    doc = load_chart_document(path)
    assert doc.entries[0].note == "First line of the note, continued on a second line."


def test_account_without_a_note_has_an_empty_string(tmp_path) -> None:
    path = _write(tmp_path, "1 A                                                            [root: Asset]\n")
    doc = load_chart_document(path)
    assert doc.entries[0].note == ""


def test_is_group_is_derived_from_children(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n  2 B                [Bank]\n",
    )
    doc = load_chart_document(path)
    by_number = {e.account.number: e.account for e in doc.entries}
    assert by_number["1"].is_group is True
    assert by_number["2"].is_group is False


def test_blank_lines_and_comments_are_ignored(tmp_path) -> None:
    path = _write(
        tmp_path,
        "# a comment\n\n1 A                                                            [root: Asset]\n\n# trailing\n",
    )
    doc = load_chart_document(path)
    assert len(doc.entries) == 1


def test_file_order_is_preserved(tmp_path) -> None:
    path = _write(
        tmp_path,
        "5 A                                                            [root: Asset]\n"
        "1 B                                                            [root: Asset]\n",
    )
    doc = load_chart_document(path)
    assert [e.account.number for e in doc.entries] == ["5", "1"]


def test_load_defaults_returns_strings(tmp_path) -> None:
    path = _write(
        tmp_path,
        "1 A                                                            [root: Asset]\n\n[defaults]\ndefault_bank_account = 1\n",
    )
    doc = load_chart_document(path)
    assert doc.defaults == {"default_bank_account": "1"}


# --- the suspense account -------------------------------------------------


def _chart_with(tmp_path, expenses: str):
    """A minimal chart with all five roots, valid defaults, and a chosen expense subtree."""
    defaults = "".join(f"{field} = 2\n" for field in sorted(REQUIRED_DEFAULT_FIELDS))
    return _write(
        tmp_path,
        "1 Assets                                                       [root: Asset]\n"
        "  2 Bank                                                       [Bank]\n"
        "3 Liabilities                                                  [root: Liability]\n"
        "4 Income                                                       [root: Income]\n"
        "5 Expenses                                                     [root: Expense]\n"
        f"{expenses}"
        "6 Equity                                                       [root: Equity]\n"
        f"\n[defaults]\n{defaults}",
    )


def test_the_real_chart_declares_the_suspense_account(chart) -> None:
    # A person reconciling a statement line they cannot yet code parks it
    # here, and `runningtab health` ages what lands.
    account = suspense_account(chart)

    assert account.number == SUSPENSE_ACCOUNT_NUMBER
    assert account.root_type == "Expense"
    assert not account.is_group
    # Named to be uncomfortable: anything sitting in it is unfinished
    # bookkeeping, and a comfortable name invites leaving things there.
    assert "UNCATEGORISED" in account.name


def test_a_chart_without_a_suspense_account_is_rejected(tmp_path) -> None:
    path = _chart_with(tmp_path, "  5100 Hosting\n")

    with pytest.raises(ChartError, match="no suspense account 5299"):
        load_chart(path)


def test_a_suspense_account_that_is_a_group_is_rejected(tmp_path) -> None:
    path = _chart_with(tmp_path, f"  {SUSPENSE_ACCOUNT_NUMBER} Suspense\n    5300 Child\n")

    with pytest.raises(ChartError, match="is a group and cannot be posted to"):
        load_chart(path)


def test_a_suspense_account_outside_the_expense_tree_is_rejected(tmp_path) -> None:
    defaults = "".join(f"{field} = 2\n" for field in sorted(REQUIRED_DEFAULT_FIELDS))
    path = _write(
        tmp_path,
        "1 Assets                                                       [root: Asset]\n"
        "  2 Bank                                                       [Bank]\n"
        f"  {SUSPENSE_ACCOUNT_NUMBER} Suspense\n"
        "3 Liabilities                                                  [root: Liability]\n"
        "4 Income                                                       [root: Income]\n"
        "5 Expenses                                                     [root: Expense]\n"
        "  5100 Hosting\n"
        "6 Equity                                                       [root: Equity]\n"
        f"\n[defaults]\n{defaults}",
    )

    with pytest.raises(ChartError, match="expected 'Expense'"):
        load_chart(path)


# --- the no-bank-line tag -------------------------------------------------


def _expenses_with(tags: str) -> str:
    """An expense subtree whose one extra account carries `tags`, plus the suspense account."""
    return f"  5100 Depreciation {tags}\n  {SUSPENSE_ACCOUNT_NUMBER} Suspense\n"


def test_the_no_bank_line_tag_is_carried_on_the_entry(tmp_path) -> None:
    path = _chart_with(tmp_path, _expenses_with(f"[{NO_BANK_LINE_TAG}]"))
    by_number = {entry.account.number: entry for entry in load_chart_entries(path)}

    assert by_number["5100"].no_bank_line
    assert not by_number[SUSPENSE_ACCOUNT_NUMBER].no_bank_line


def test_the_tag_shares_its_block_with_an_account_type(tmp_path) -> None:
    # `[Depreciation, no-bank-line]` is how the real chart writes it: one
    # ERPNext account type, plus a word of our own. Without the reserved-word
    # branch the word reaches `validate_account_type` and the chart stops
    # loading.
    path = _chart_with(tmp_path, _expenses_with(f"[Depreciation, {NO_BANK_LINE_TAG}]"))
    (entry,) = [e for e in load_chart_entries(path) if e.account.number == "5100"]

    assert entry.no_bank_line
    assert entry.account.account_type == "Depreciation"


def test_repeating_the_tag_is_rejected(tmp_path) -> None:
    path = _chart_with(tmp_path, _expenses_with(f"[{NO_BANK_LINE_TAG}, {NO_BANK_LINE_TAG}]"))

    with pytest.raises(ChartError, match=f"more than one '{NO_BANK_LINE_TAG}' tag"):
        load_chart(path)


def test_the_tag_says_nothing_to_erpnext(tmp_path) -> None:
    # It is a fact about the bank's pickers, not about the account. The
    # `DesiredAccount` the apply sends is the same either way.
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    tagged_dir = tmp_path / "tagged"
    tagged_dir.mkdir()
    plain = _chart_with(plain_dir, _expenses_with(""))
    tagged = _chart_with(tagged_dir, _expenses_with(f"[{NO_BANK_LINE_TAG}]"))

    assert load_chart(plain) == load_chart(tagged)


def test_the_real_chart_tags_the_five_adjustment_only_accounts() -> None:
    # Depreciation, round-off, FX, write-offs and a disposal gain are posted
    # by a period-end or adjustment journal, never by a statement line, so
    # `runningtab mercury categories` offers no category for any of them.
    tagged = {entry.account.number for entry in load_chart_entries(EXAMPLE_BOOKS.chart_path) if entry.no_bank_line}

    assert tagged == {"5203", "5217", "5218", "5220", "5221"}
