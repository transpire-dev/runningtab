"""`GcsRecord` against the real test-target record bucket. Skipped unless RUNNINGTAB_LIVE is set.

Unlike the other `live_runningtab` tests, this one needs no ERPNext credential --
only Application Default Credentials against a real GCS bucket -- so it does
not call `require_live_target` (which resolves a Frappe `SiteConfig`).
It checks `RUNNINGTAB_LIVE` directly, the same gate, for the same reason: the
default suite (`just test`, CI) must never dial out.

The test record bucket (`books.toml`'s `record_buckets.test`) is created by
whoever keeps the books, not by this package. Until it exists
`GcsRecord.list_names` against it raises `google.api_core.exceptions.NotFound`,
which this test turns into a skip with that reason, rather than a failure.
"""

from __future__ import annotations

import os
import uuid

import pytest

from runningtab.period.record import GcsRecord
from runningtab.site import Target
from tests.conftest import live_books

pytestmark = pytest.mark.live_runningtab


def _test_record() -> GcsRecord:
    if not os.environ.get("RUNNINGTAB_LIVE"):
        pytest.skip("RUNNINGTAB_LIVE not set")
    bucket_name = live_books().record_buckets[Target.TEST]
    record = GcsRecord(bucket_name)
    try:
        record.list_names("contract/")
    except Exception as exc:
        from google.api_core.exceptions import NotFound

        if isinstance(exc, NotFound):
            pytest.skip(f"{bucket_name} does not exist yet -- it is created when whoever keeps these books creates it")
        raise
    return record


def test_create_then_read_back_round_trips_and_a_second_create_of_the_same_name_is_refused():
    record = _test_record()
    name = f"contract/{uuid.uuid4()}/probe.txt"
    data = b"books period record live contract probe"

    # No cleanup of `name` afterward: the bucket is write-once by design (a
    # week-old lifecycle rule clears it, not this test), and deleting here
    # would need a `Record.delete` this client deliberately does not have.
    record.create(name, data)
    assert record.read(name) == data

    with pytest.raises(Exception, match="already exists"):
        record.create(name, b"a second, different write")

    # The refused second write did not land -- the object still reads back
    # as the first write, never the second.
    assert record.read(name) == data
