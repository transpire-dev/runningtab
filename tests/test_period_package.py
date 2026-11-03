"""The package as a whole: every file present, identical on rebuild, never written over."""

import dataclasses
import datetime as dt
import hashlib
import os
import pathlib
import tempfile

import pytest

from runningtab.period import PeriodError
from runningtab.period.manifest import ManifestFacts, lock_version_of, render_manifest
from runningtab.period.package import build_package, sha256sums, write_package
from runningtab.period.qbo_map import load_qbo_map
from runningtab.period.quarter import Quarter, fiscal_year_for
from tests.conftest import EXAMPLE_BOOKS, period_fiscal_years, period_gl_rows, period_inputs

Q3 = Quarter.parse("2026Q3")

# Loaded once and reused everywhere a QboMap is needed, so a test's
# ManifestFacts.qmap is the same object PackageInputs.qmap built the kit
# from -- render_manifest must never load its own copy.
QMAP = load_qbo_map(EXAMPLE_BOOKS.qbo_path)

EXPECTED = {
    "chart-of-accounts.csv",
    "trial-balance.csv",
    "general-ledger.csv",
    "profit-and-loss.csv",
    "balance-sheet.csv",
    "bank-reconciliation.csv",
    "open-receivables.csv",
    "open-payables.csv",
    "qbo/accounts.csv",
    "qbo/customers.csv",
    "qbo/vendors.csv",
    "qbo/opening-balances.csv",
    "qbo/journal-entries-01.csv",
    "documents/Journal Entry/JV-1/f1-r.pdf",
}


def test_every_file_is_built():
    assert set(build_package(period_inputs()).files) == EXPECTED


def test_two_builds_are_byte_identical():
    assert build_package(period_inputs()).files == build_package(period_inputs()).files


def test_a_quarter_with_no_activity_still_builds():
    quiet = Quarter.parse("2026Q1")
    rows = [r for r in period_gl_rows() if r.posting_date < dt.date(2026, 1, 1)]
    files = build_package(period_inputs(rows, quiet)).files
    assert "qbo/journal-entries-01.csv" not in files
    assert files["general-ledger.csv"].count(b"\n") == 1  # header only


def test_sha256sums_is_the_standard_format_sorted_by_path():
    sums = sha256sums({"b.csv": b"2", "a.csv": b"1"}).decode()
    assert sums == f"{hashlib.sha256(b'1').hexdigest()}  a.csv\n{hashlib.sha256(b'2').hexdigest()}  b.csv\n"


def _facts(**overrides) -> ManifestFacts:
    package = build_package(period_inputs())
    base = ManifestFacts(
        quarter=Q3,
        fiscal_year=fiscal_year_for(Q3, period_fiscal_years()),
        company="Transpire",
        books_name=EXAMPLE_BOOKS.name,
        site_url="https://test-ledger.example",
        chart_sha256=hashlib.sha256(EXAMPLE_BOOKS.chart_path.read_bytes()).hexdigest(),
        qmap=QMAP,
        generated_at=dt.datetime(2026, 10, 2, 17, 0, tzinfo=dt.UTC),
        generated_by="gavin",
        draft=True,
        today=dt.date(2026, 10, 2),
        bank=package.bank,
        split_vouchers=package.split_vouchers,
        has_journals=True,
    )
    return dataclasses.replace(base, **overrides)


def _section(text: str, heading: str) -> str:
    """The text of one `## <heading>` section, including its heading, up to the next `## `."""
    start = text.index(f"## {heading}")
    rest = text[start:]
    end = rest.find("\n\n## ")
    return rest if end == -1 else rest[:end]


def test_the_manifest_says_draft_and_warns_about_the_opening_file():
    text = render_manifest(_facts(), build_package(period_inputs()).files).decode()
    assert text.startswith("# Example books — 2026Q3 — DRAFT")
    assert "importing it twice doubles every balance" in text
    assert "sha256sum -c SHA256SUMS" in text


def test_the_builtin_count_comes_from_the_qbo_map_not_a_hardcoded_number():
    text = render_manifest(_facts(), build_package(period_inputs()).files).decode()
    assert f"does not create new QuickBooks accounts for {len(QMAP.builtin)} of ours" in text


def test_the_chart_file_is_described_as_of_this_export():
    text = render_manifest(_facts(), build_package(period_inputs()).files).decode()
    chart_line = next(line for line in text.splitlines() if line.startswith("- **`chart-of-accounts.csv`**"))
    assert "as of this export" in chart_line
    assert "as of the quarter's end" not in chart_line


def test_the_import_checklist_names_the_split_journal_no_length_and_dual_party_risk():
    text = render_manifest(_facts(), build_package(period_inputs()).files).decode()
    section = _section(text, "Importing into QuickBooks Online")
    assert "ACC-PINV-2026-00001/2" in section
    assert "21 characters" in section
    assert "customer and a vendor" in section


def test_the_import_checklist_turns_account_numbers_off_before_opening_balances_too():
    # opening-balances.csv is a journal import too, the same as
    # journal-entries-NN.csv -- the "turn account numbers off" instruction
    # must precede both steps, not just the second one.
    text = render_manifest(_facts(), build_package(period_inputs()).files).decode()
    section = _section(text, "Importing into QuickBooks Online")
    numbers_off_at = section.index("account numbers off")
    opening_at = section.index("`qbo/opening-balances.csv`")
    journal_at = section.index("`qbo/journal-entries-NN.csv`")
    assert numbers_off_at < opening_at < journal_at


def test_an_unfinished_quarter_is_said_so():
    text = render_manifest(_facts(today=dt.date(2026, 9, 15)), build_package(period_inputs()).files).decode()
    assert "quarter not yet ended" in text


def test_a_final_manifest_carries_no_draft_marker():
    text = render_manifest(
        _facts(draft=False, closed_by="gavin@carothers.name", lock_version="VER-1"),
        build_package(period_inputs()).files,
    ).decode()
    assert text.startswith("# Example books — 2026Q3\n")
    assert "DRAFT" not in text


def test_the_lock_version_reads_back_from_a_final_manifest_and_not_from_a_draft():
    # `period status` compares this against the Company's lock history, so it
    # must read back exactly what the renderer wrote.
    files = build_package(period_inputs()).files
    final = render_manifest(_facts(draft=False, closed_by="gavin@carothers.name", lock_version="VER-9"), files)
    assert lock_version_of(final.decode()) == "VER-9"
    assert lock_version_of(render_manifest(_facts(), files).decode()) is None


def test_a_final_manifest_refuses_without_a_closer_or_lock_version():
    files = build_package(period_inputs()).files
    with pytest.raises(PeriodError, match="closed_by and lock_version"):
        render_manifest(_facts(draft=False, lock_version="VER-1"), files)
    with pytest.raises(PeriodError, match="closed_by and lock_version"):
        render_manifest(_facts(draft=False, closed_by="gavin@carothers.name"), files)


def test_a_quarter_with_no_journal_entries_is_said_so():
    quiet = Quarter.parse("2026Q1")
    rows = [r for r in period_gl_rows() if r.posting_date < dt.date(2026, 1, 1)]
    package = build_package(period_inputs(rows, quiet))
    facts = _facts(
        quarter=quiet,
        fiscal_year=fiscal_year_for(quiet, period_fiscal_years()),
        today=quiet.end,
        bank=package.bank,
        split_vouchers=package.split_vouchers,
        has_journals=False,
    )
    text = render_manifest(facts, package.files).decode()
    assert "no journal entries this quarter" in text
    # Nothing implies a journal-entries file exists to import: no such file
    # is in this package, and the generic import-order step above never
    # claims one is "listed above".
    assert "journal-entries-01.csv" not in text
    assert "listed above" not in text


def test_no_split_vouchers_is_said_so():
    text = render_manifest(_facts(split_vouchers=()), build_package(period_inputs()).files).decode()
    assert "No voucher needed splitting this quarter." in text
    assert "were split into more than one" not in text


def test_a_fully_reconciled_bank_has_no_bold_differences():
    package = build_package(period_inputs())
    reconciled = tuple(dataclasses.replace(line, ledger=line.bank) for line in package.bank)
    text = render_manifest(_facts(bank=reconciled), package.files).decode()
    section = _section(text, "Bank reconciliation")
    assert "All accounts reconcile" in section
    assert "**" not in section


def test_a_final_manifest_names_who_closed_it_the_lock_version_and_the_revision():
    text = render_manifest(
        _facts(draft=False, closed_by="gavin@carothers.name", lock_version="VER-9", revision=2),
        build_package(period_inputs()).files,
    ).decode()
    assert "- **Closed by:** gavin@carothers.name" in text
    assert "- **Lock record:** Company Version `VER-9`" in text
    assert "- **Revision:** 2" in text


def test_a_draft_manifest_never_names_a_closer_lock_or_revision():
    text = render_manifest(_facts(draft=True), build_package(period_inputs()).files).decode()
    assert "Closed by" not in text
    assert "Lock record" not in text
    assert "Revision" not in text


def test_verifying_this_package_explains_the_standing_check_and_late_bank_activity():
    text = render_manifest(_facts(), build_package(period_inputs()).files).decode()
    section = _section(text, "Verifying this package")
    assert "runningtab period status" in section
    assert "rebuilds this quarter's package from the ledger and compares" in section
    assert "bank-reconciliation.csv" in section


def test_writing_refuses_an_existing_directory(tmp_path):
    target = tmp_path / "2026Q3-DRAFT"
    target.mkdir()
    with pytest.raises(PeriodError, match="already exists"):
        write_package(target, build_package(period_inputs()), b"# m\n")


def test_written_files_verify_against_their_sums(tmp_path):
    target = tmp_path / "2026Q3-DRAFT"
    package = build_package(period_inputs())
    write_package(target, package, b"# m\n")
    for line in (target / "SHA256SUMS").read_text().splitlines():
        digest, path = line.split("  ", 1)
        assert hashlib.sha256((target / path).read_bytes()).hexdigest() == digest
    assert (target / "MANIFEST.md").read_bytes() == b"# m\n"


def test_a_successful_write_leaves_no_partial_sibling(tmp_path):
    # write_package stages the package in a sibling temporary directory and
    # renames it into place; a successful write must not leave that staging
    # directory behind for a person to find beside the real export.
    target = tmp_path / "2026Q3-DRAFT"
    write_package(target, build_package(period_inputs()), b"# m\n")
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_a_failure_mid_write_leaves_neither_destination_nor_temp_directory(tmp_path, monkeypatch):
    # A crash partway through the old straight write left a partial package
    # sitting at `target` -- one a retry refused (it exists) and a person
    # could mistake for a real export. Writing to a temp directory first and
    # cleaning it up on failure means a failed export leaves nothing at all.
    target = tmp_path / "2026Q3-DRAFT"
    package = build_package(period_inputs())
    real_write_bytes = pathlib.Path.write_bytes
    calls = 0

    def flaky_write_bytes(self: pathlib.Path, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("disk full")
        return real_write_bytes(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", flaky_write_bytes)
    with pytest.raises(OSError, match="disk full"):
        write_package(target, package, b"# m\n")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_file_dropped_into_the_reserved_destination_survives_a_failed_write(tmp_path, monkeypatch):
    # write_package reserves `target` with a plain mkdir before it ever
    # builds the staging directory, closing the window between the old
    # existence check and the final rename. If something else puts a file
    # into that reserved, still-empty `target` before the rename runs, the
    # rename must fail loudly rather than silently discard it.
    target = tmp_path / "2026Q3-DRAFT"
    package = build_package(period_inputs())
    real_replace = os.replace
    intruder = b"a file something else wrote into the reserved directory"

    def sneaky_replace(src, dst, *args, **kwargs):
        pathlib.Path(dst, "intruder.txt").write_bytes(intruder)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", sneaky_replace)
    with pytest.raises(OSError):
        write_package(target, package, b"# m\n")

    assert target.exists()
    assert (target / "intruder.txt").read_bytes() == intruder


def test_a_failed_mkdtemp_does_not_strand_the_reserved_destination(tmp_path, monkeypatch):
    # mkdtemp used to run outside write_package's cleanup block: if it
    # failed right after directory.mkdir() reserved the destination, that
    # empty reserved directory was left behind with nothing to clean it up,
    # blocking every retry (mkdir would refuse it as already existing).
    target = tmp_path / "2026Q3-DRAFT"
    package = build_package(period_inputs())

    def failing_mkdtemp(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", failing_mkdtemp)
    with pytest.raises(OSError, match="no space left on device"):
        write_package(target, package, b"# m\n")

    assert not target.exists()
