"""The record bucket client, against an in-memory `Record`: a dict whose `create` refuses an existing name.

`FakeRecord` stands in for `GcsRecord` the way the real bucket's
`if_generation_match=0` precondition behaves: `create` never overwrites, it
refuses. No test here ever touches the network or `GcsRecord` -- that is
`tests/test_live_record.py`'s job.
"""

from __future__ import annotations

import re

import pytest

from runningtab.period import PeriodError
from runningtab.period.quarter import Quarter
from runningtab.period.record import (
    StoredPackage,
    SuspectPackage,
    latest,
    next_prefix,
    read_lock_version,
    read_sums,
    revision_of,
    stored_packages,
    upload_package,
)
from runningtab.site import Target
from tests.conftest import EXAMPLE_BOOKS, FakeRecord


class _CorruptingRecord(FakeRecord):
    """A `FakeRecord` that returns different bytes than it stored for names in `corrupt`, simulating a bad read-back."""

    def __init__(self, corrupt: set[str]) -> None:
        super().__init__()
        self._corrupt = corrupt

    def read(self, name: str) -> bytes:
        data = super().read(name)
        return data + b"!" if name in self._corrupt else data


QUARTER = Quarter.parse("2026Q3")

# --- EXAMPLE_BOOKS.record_buckets ----------------------------------------------------------


def test_record_buckets_names_both_targets():
    assert EXAMPLE_BOOKS.record_buckets == {
        Target.BOOKS: "example-books-record",
        Target.TEST: "example-books-record-test",
    }


# --- stored_packages -----------------------------------------------------


def test_stored_packages_finds_revision_one_from_a_bare_sha256sums():
    record = FakeRecord({"T/2026Q3/SHA256SUMS": b"sums", "T/2026Q3/trial-balance.csv": b"x"})

    assert stored_packages(record, "T") == [StoredPackage(QUARTER, 1, "T/2026Q3/")]


def test_stored_packages_finds_a_numbered_revision():
    record = FakeRecord({
        "T/2026Q3/SHA256SUMS": b"sums",
        "T/2026Q3/rev2/SHA256SUMS": b"sums2",
        "T/2026Q3/rev2/trial-balance.csv": b"x",
    })

    assert stored_packages(record, "T") == [
        StoredPackage(QUARTER, 1, "T/2026Q3/"),
        StoredPackage(QUARTER, 2, "T/2026Q3/rev2/"),
    ]


def test_stored_packages_sorted_by_quarter_then_revision():
    record = FakeRecord({
        "T/2026Q3/rev2/SHA256SUMS": b"x",
        "T/2026Q1/SHA256SUMS": b"x",
        "T/2026Q3/SHA256SUMS": b"x",
    })

    packages = stored_packages(record, "T")

    assert [(p.quarter.label, p.revision) for p in packages] == [
        ("2026Q1", 1),
        ("2026Q3", 1),
        ("2026Q3", 2),
    ]


def test_stored_packages_ignores_a_package_missing_sha256sums():
    record = FakeRecord({"T/2026Q3/trial-balance.csv": b"x", "T/2026Q3/MANIFEST.md": b"y"})

    assert stored_packages(record, "T") == []


def test_stored_packages_scoped_to_the_given_abbr():
    record = FakeRecord({"T/2026Q3/SHA256SUMS": b"x", "OTHER/2026Q3/SHA256SUMS": b"y"})

    assert [p.prefix for p in stored_packages(record, "T")] == ["T/2026Q3/"]


# --- latest ----------------------------------------------------------------


def test_latest_keeps_the_highest_revision_per_quarter():
    stored = [
        StoredPackage(QUARTER, 1, "T/2026Q3/"),
        StoredPackage(QUARTER, 2, "T/2026Q3/rev2/"),
        StoredPackage(Quarter.parse("2026Q2"), 1, "T/2026Q2/"),
    ]

    assert latest(stored) == {
        QUARTER: StoredPackage(QUARTER, 2, "T/2026Q3/rev2/"),
        Quarter.parse("2026Q2"): StoredPackage(Quarter.parse("2026Q2"), 1, "T/2026Q2/"),
    }


def test_latest_of_nothing_is_empty():
    assert latest([]) == {}


# --- next_prefix -------------------------------------------------------------


def test_next_prefix_with_no_existing_package_is_revision_one():
    assert next_prefix(FakeRecord(), "T", QUARTER) == "T/2026Q3/"


def test_next_prefix_with_one_existing_package_is_rev2():
    record = FakeRecord({"T/2026Q3/SHA256SUMS": b"sums"})

    assert next_prefix(record, "T", QUARTER) == "T/2026Q3/rev2/"


def test_next_prefix_with_rev2_existing_is_rev3():
    record = FakeRecord({"T/2026Q3/SHA256SUMS": b"sums", "T/2026Q3/rev2/SHA256SUMS": b"sums2"})

    assert next_prefix(record, "T", QUARTER) == "T/2026Q3/rev3/"


def test_next_prefix_only_considers_the_named_quarter():
    record = FakeRecord({"T/2026Q2/rev3/SHA256SUMS": b"sums"})

    assert next_prefix(record, "T", QUARTER) == "T/2026Q3/"


def test_next_prefix_skips_a_leftover_prefix_with_no_sha256sums():
    # A half-failed upload left a content object at revision 1 with no
    # SHA256SUMS. `stored_packages` calls that "not there," but the prefix
    # is under a retention policy and cannot be deleted, so a retry must not
    # recompute it -- `create` would refuse on the leftover object forever.
    record = FakeRecord({"VT/2026Q3/trial-balance.csv": b"x"})

    assert next_prefix(record, "VT", QUARTER) == "VT/2026Q3/rev2/"


def test_next_prefix_skips_a_complete_package_and_a_leftover_revision():
    record = FakeRecord({
        "VT/2026Q3/SHA256SUMS": b"sums",
        "VT/2026Q3/rev2/trial-balance.csv": b"x",
    })

    assert next_prefix(record, "VT", QUARTER) == "VT/2026Q3/rev3/"


# --- revision_of --------------------------------------------------------------


def test_revision_of_reads_back_every_revision_next_prefix_can_produce():
    # Walks the exact sequence `next_prefix` produces against a growing
    # record, rather than hand-writing "rev2/" and "rev3/" strings that could
    # drift from what `next_prefix` actually builds.
    record = FakeRecord()
    for expected_revision in range(1, 5):
        prefix = next_prefix(record, "T", QUARTER)
        assert revision_of(prefix, "T", QUARTER) == expected_revision
        record.objects[f"{prefix}SHA256SUMS"] = b"sums"


def test_revision_of_scoped_to_the_given_abbr_and_quarter():
    # A prefix that is a real revision prefix, just not this one -- proves
    # `revision_of` checks the whole prefix against `abbr`/`quarter`, not
    # only that it ends in `revN/`.
    with pytest.raises(PeriodError, match=re.escape("T/2026Q3/rev2/")):
        revision_of("T/2026Q3/rev2/", "VT", QUARTER)
    with pytest.raises(PeriodError, match=re.escape("T/2026Q2/rev2/")):
        revision_of("T/2026Q2/rev2/", "T", QUARTER)


def test_revision_of_refuses_a_prefix_that_is_not_the_named_layout_at_all():
    with pytest.raises(PeriodError, match=re.escape("T/2026Q3/extra/")):
        revision_of("T/2026Q3/extra/", "T", QUARTER)


# --- upload_package ----------------------------------------------------------


def test_upload_package_writes_sha256sums_last():
    record = FakeRecord()

    upload_package(record, "T/2026Q3/", {"b.csv": b"2", "a.csv": b"1", "SHA256SUMS": b"sums"})

    assert record.create_order[-1] == "T/2026Q3/SHA256SUMS"
    assert set(record.create_order) == {"T/2026Q3/a.csv", "T/2026Q3/b.csv", "T/2026Q3/SHA256SUMS"}


def test_upload_package_writes_every_object_at_the_given_prefix():
    record = FakeRecord()

    upload_package(record, "T/2026Q3/", {"a.csv": b"1", "SHA256SUMS": b"sums"})

    assert record.objects == {"T/2026Q3/a.csv": b"1", "T/2026Q3/SHA256SUMS": b"sums"}


def test_upload_package_refuses_a_read_back_mismatch_naming_the_object():
    record = _CorruptingRecord(corrupt={"T/2026Q3/a.csv"})

    with pytest.raises(PeriodError, match=re.escape("T/2026Q3/a.csv")):
        upload_package(record, "T/2026Q3/", {"a.csv": b"1", "b.csv": b"2", "SHA256SUMS": b"sums"})

    # SHA256SUMS is only ever created after every content file's read-back
    # is confirmed good -- a corrupt package must never become visible as
    # complete to `stored_packages`.
    assert "T/2026Q3/SHA256SUMS" not in record.objects


def test_upload_package_refuses_a_sha256sums_read_back_mismatch_naming_the_prefix_as_suspect():
    record = _CorruptingRecord(corrupt={"T/2026Q3/SHA256SUMS"})

    with pytest.raises(SuspectPackage) as exc:
        upload_package(record, "T/2026Q3/", {"a.csv": b"1", "SHA256SUMS": b"sums"})

    # By now the package is already listed as complete, so a retry would
    # only hit "already has a stored package": the message names the prefix
    # as suspect and points at `period status`, never at a retry.
    assert stored_packages(record, "T") == [StoredPackage(QUARTER, 1, "T/2026Q3/")]
    message = str(exc.value)
    assert "T/2026Q3/SHA256SUMS" in message
    assert "treat 'T/2026Q3/' as suspect" in message
    assert "`runningtab period status`" in message
    assert "again" not in message


def test_a_content_read_back_mismatch_is_not_a_suspect_package():
    # SHA256SUMS was never written, so nothing is listed and a retry lands
    # on the next revision: an ordinary refusal, retry advice and all.
    record = _CorruptingRecord(corrupt={"T/2026Q3/a.csv"})

    with pytest.raises(PeriodError) as exc:
        upload_package(record, "T/2026Q3/", {"a.csv": b"1", "SHA256SUMS": b"sums"})

    assert not isinstance(exc.value, SuspectPackage)


def test_upload_package_never_overwrites_an_existing_object():
    record = FakeRecord({"T/2026Q3/a.csv": b"old"})

    with pytest.raises(PeriodError, match=re.escape("T/2026Q3/a.csv")):
        upload_package(record, "T/2026Q3/", {"a.csv": b"new", "SHA256SUMS": b"sums"})

    assert record.objects["T/2026Q3/a.csv"] == b"old"
    # Nothing after the refused object was written either.
    assert "T/2026Q3/SHA256SUMS" not in record.objects


# --- read_sums ---------------------------------------------------------------


def test_read_sums_parses_the_stored_sha256sums():
    sums = "aaa  trial-balance.csv\nbbb  qbo/journal.iif\n"
    record = FakeRecord({"T/2026Q3/SHA256SUMS": sums.encode()})
    stored = StoredPackage(QUARTER, 1, "T/2026Q3/")

    assert read_sums(record, stored) == {"trial-balance.csv": "aaa", "qbo/journal.iif": "bbb"}


def test_read_sums_refuses_a_line_with_no_two_space_separator_by_name():
    # The retention lock means the bucket wrote this file itself and nothing
    # can have touched it since -- unlikely, but a read failure is a named
    # refusal here the same as everywhere else, never a bare traceback.
    sums = "aaa  trial-balance.csv\nnot-the-right-shape\n"
    record = FakeRecord({"T/2026Q3/SHA256SUMS": sums.encode()})
    stored = StoredPackage(QUARTER, 1, "T/2026Q3/")

    with pytest.raises(PeriodError, match=r"T/2026Q3/SHA256SUMS"):
        read_sums(record, stored)


def test_read_sums_refuses_undecodable_bytes_by_name():
    record = FakeRecord({"T/2026Q3/SHA256SUMS": b"\xff\xfe not utf-8"})
    stored = StoredPackage(QUARTER, 1, "T/2026Q3/")

    with pytest.raises(PeriodError, match=r"T/2026Q3/SHA256SUMS"):
        read_sums(record, stored)


def test_read_lock_version_reads_the_manifest_s_company_version():
    manifest = "# Transpire books — 2026Q3\n\n- **Closed by:** gavin\n- **Lock record:** Company Version `VER-9`\n"
    record = FakeRecord({"T/2026Q3/rev2/MANIFEST.md": manifest.encode()})

    assert read_lock_version(record, StoredPackage(QUARTER, 2, "T/2026Q3/rev2/")) == "VER-9"


def test_read_lock_version_refuses_a_manifest_with_no_lock_by_name():
    # A stored package is always a final close; reading "no lock" as fine
    # would let a stored draft pass for a close.
    record = FakeRecord({"T/2026Q3/MANIFEST.md": "# Transpire books — 2026Q3 — DRAFT\n".encode()})

    with pytest.raises(PeriodError, match=r"T/2026Q3/MANIFEST.md records no lock"):
        read_lock_version(record, StoredPackage(QUARTER, 1, "T/2026Q3/"))


def test_read_lock_version_refuses_undecodable_bytes_by_name():
    record = FakeRecord({"T/2026Q3/MANIFEST.md": b"\xff\xfe not utf-8"})

    with pytest.raises(PeriodError, match=r"T/2026Q3/MANIFEST.md"):
        read_lock_version(record, StoredPackage(QUARTER, 1, "T/2026Q3/"))
