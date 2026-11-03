"""Unit tests for `test_live_close.py`'s rerun gate: never re-close an already-stored quarter.

`_already_closed_reason` and `_bank_difference_only` are plain functions, not
fixtures or test bodies, precisely so the decision they make can be proven
here with fabricated `StoredPackage`s and CLI output strings -- no
`RUNNINGTAB_LIVE`, no network. Deliberately unmarked (no `live_runningtab`): these run
in the default suite, the same reason `test_live_gate.py` is unmarked.
"""

from runningtab.period.quarter import Quarter
from runningtab.period.record import StoredPackage
from tests.test_live_close import _already_closed_reason, _bank_difference_only


def _package(quarter_label: str, revision: int = 1) -> StoredPackage:
    quarter = Quarter.parse(quarter_label)
    prefix = f"VT/{quarter.label}/" if revision == 1 else f"VT/{quarter.label}/rev{revision}/"
    return StoredPackage(quarter=quarter, revision=revision, prefix=prefix)


def test_no_stored_package_for_the_quarter_returns_none() -> None:
    assert _already_closed_reason([_package("2026Q2")], "2026Q3") is None


def test_no_packages_at_all_returns_none() -> None:
    assert _already_closed_reason([], "2026Q3") is None


def test_a_stored_package_for_the_quarter_returns_a_reason_naming_its_prefix() -> None:
    reason = _already_closed_reason([_package("2026Q3")], "2026Q3")
    assert reason is not None
    assert "VT/2026Q3/" in reason
    assert "2026Q3" in reason


def test_multiple_revisions_are_all_named() -> None:
    reason = _already_closed_reason([_package("2026Q3", revision=1), _package("2026Q3", revision=2)], "2026Q3")
    assert reason is not None
    assert "VT/2026Q3/" in reason
    assert "VT/2026Q3/rev2/" in reason


def test_bank_difference_only_is_true_for_a_single_bank_line() -> None:
    output = "refused: cannot close VT 2026Q3: bank difference on Checking (1111): -12.34\n"
    assert _bank_difference_only(output) is True


def test_bank_difference_only_is_true_for_multiple_bank_lines() -> None:
    output = (
        "refused: cannot close VT 2026Q3: bank difference on Checking (1111): -12.34; "
        "bank difference on Savings (1112): 5.00\n"
    )
    assert _bank_difference_only(output) is True


def test_bank_difference_only_is_false_when_a_blocker_is_mixed_in() -> None:
    output = (
        "refused: cannot close VT 2026Q3: suspense GL Entry ACC-GL-001: Journal Entry JV-1 posted "
        "$10.00 to suspense on 2026-09-15; bank difference on Checking (1111): -12.34\n"
    )
    assert _bank_difference_only(output) is False


def test_bank_difference_only_is_false_for_a_precondition_refusal_naming_no_bank_line() -> None:
    output = "refused: cannot close VT 2026Q3: 2026Q3 has not ended: today is 2026-09-01, it ends 2026-09-30\n"
    assert _bank_difference_only(output) is False


def test_bank_difference_only_is_false_when_the_output_has_no_refusal_line() -> None:
    assert _bank_difference_only("closed 2026Q3: stored at VT/2026Q3/\n") is False
