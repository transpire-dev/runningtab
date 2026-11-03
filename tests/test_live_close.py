"""`runningtab period close` end to end against the disposable test site, and `period status`'s reopen detection.

Skipped unless RUNNINGTAB_LIVE is set (see `require_live_target` in conftest.py),
`MERCURY_SANDBOX_TOKEN` is set, and the test record bucket
(`books.toml`'s `record_buckets.test`) already exists -- all three
checked before the test ever opens a connection to the ERPNext site itself,
the same order `test_live_record.py` uses for the bucket check alone.

Closing a quarter is not read-only the way every other live test in this
directory is, so it cannot simply be re-run against whatever state it finds:
`close_quarter`'s own preconditions refuse a second close of a quarter
already locked through its own end. This test sidesteps that by restoring
Books Closed Through (and the frozen-entries role) to exactly what it read at
the very start, in a `finally`, regardless of what happened in between -- so
a genuine close here proves the ceremony works (the package it wrote stays in
the write-once bucket forever, exactly as it should) without leaving the
shared site's lock permanently moved for every other live test and every
other engineer using it. If a restore fails, the test fails loudly, naming
the value left behind, rather than quietly leaving the site changed.

That restore is exactly why a rerun cannot simply retry `period close`: once
the lock is back at its pre-test value, `close_quarter`'s own "already has a
stored package" precondition never fires again (it only fires when the lock
still shows the quarter closed), so an unguarded rerun would perform a real
second close -- a new `rev2/`, `rev3/`, ... package, forever, in a
write-once bucket, plus a real Company Version audit entry each time.
`_already_closed_reason` below is the guard: checked before any write, on
every run, independent of the lock's current value. It is a pure function
precisely so that guard can be unit-tested without RUNNINGTAB_LIVE -- see
`test_live_close_gate.py`.
"""

from __future__ import annotations

import datetime as dt
import os
import re

import pytest
from icedcoffee_sync.erpnext.accounts import company_abbr
from icedcoffee_sync.frappe.client import Client
from typer.testing import CliRunner

from runningtab import cli
from runningtab.period.fence import LOCK_FIELDS
from runningtab.period.lock import read_lock
from runningtab.period.record import GcsRecord, StoredPackage, stored_packages
from runningtab.site import Target, is_test_site
from tests.conftest import live_books, require_live_target, write_sandbox_mercury_mapping

pytestmark = pytest.mark.live_runningtab

QUARTER = "2026Q3"

_REFUSED_ITEMS = re.compile(r"^refused: cannot close \S+ \S+: (?P<items>.+)$", re.MULTILINE)


def _already_closed_reason(packages: list[StoredPackage], quarter_label: str) -> str | None:
    """None if `quarter_label` has no stored package among `packages`; otherwise a skip reason naming every
    matching prefix.

    See the module docstring for why this check exists and must run before
    any write, independent of the lock's current value.
    """
    matches = sorted(p.prefix for p in packages if p.quarter.label == quarter_label)
    if not matches:
        return None
    return (
        f"{quarter_label} is already stored at {', '.join(matches)} -- this test always restores the lock in "
        "its finally, so close_quarter's own already-stored refusal would never fire on a rerun and it would "
        "perform a real second close instead; wait for the test bucket's 7-day lifecycle rule to clear the "
        "stored prefix(es) above before rerunning this test"
    )


def _bank_difference_only(output: str) -> bool:
    """True only when every item in a `period close` refusal is a bank-difference line.

    `_readiness_failures` (`runningtab.period.close`) joins every blocker
    description and every bank-difference line into one refusal message. A
    bare `"bank difference on" in output` check would treat a refusal that
    combines a genuine blocker (an unbalanced trial balance, a draft
    voucher) with a bank difference as the known, legitimate sandbox-data
    case and silently swallow the other problem. This instead requires every
    semicolon-separated item in the refusal to be a bank-difference line.
    """
    match = _REFUSED_ITEMS.search(output)
    if match is None:
        return False
    items = match["items"].split("; ")
    return bool(items) and all(item.startswith("bank difference on") for item in items)


def _require_ready():
    """RUNNINGTAB_LIVE, then MERCURY_SANDBOX_TOKEN, then the record bucket -- each a skip, not a fail, if unmet, and
    none of them makes a request to the ERPNext site itself. Returns the test site's `SiteConfig`.
    """
    config = require_live_target()
    if not is_test_site(live_books().site, config.url):
        # Fail, never skip: the rest of this test provisions a real role/user
        # and writes Company's close lock.
        pytest.fail(f"refusing to run against a host not in the books' test_hosts: {config.url}")

    if not os.environ.get("MERCURY_SANDBOX_TOKEN"):
        pytest.skip("MERCURY_SANDBOX_TOKEN not set")

    bucket_name = live_books().record_buckets[Target.TEST]
    try:
        GcsRecord(bucket_name).list_names("contract/")
    except Exception as exc:
        from google.api_core.exceptions import NotFound

        if isinstance(exc, NotFound):
            pytest.skip(f"{bucket_name} does not exist yet -- it is created when whoever keeps these books creates it")
        raise
    return config


def test_close_then_status_then_reopen_is_detected_and_restored(tmp_path):
    config = _require_ready()
    with Client(config) as client:
        rows = client.list("Company", fields=["name"], limit=2)
        if len(rows) != 1:
            pytest.fail(f"expected exactly one company on the test site, found {len(rows)}")
        company = str(rows[0]["name"])
        abbr = company_abbr(client, company)
        original = read_lock(client, company)

        # Before any write: a rerun within the bucket's retention window must
        # skip, never re-close. See the module docstring and
        # `_already_closed_reason`.
        record = GcsRecord(live_books().record_buckets[Target.TEST])
        already_closed = _already_closed_reason(stored_packages(record, abbr), QUARTER)
        if already_closed:
            pytest.skip(already_closed)

        try:
            # Provision the bot (installs/repairs the fence too), the same
            # order a person closing a quarter for the first time follows.
            provision_result = CliRunner().invoke(
                cli.app, ["access", "provision", "--target", "test", "--profile", "bot", "--yes"]
            )
            assert provision_result.exit_code == 0, provision_result.output

            # The sandbox's own account ids, never the shipped mercury.toml
            # (which names only the real Mercury accounts) -- same helper
            # test_live_period.py's export tests use.
            mapping_path = tmp_path / "mercury.toml"
            write_sandbox_mercury_mapping(mapping_path)

            close_result = CliRunner().invoke(
                cli.app,
                [
                    "period",
                    "close",
                    "--target",
                    "test",
                    "--quarter",
                    QUARTER,
                    "--company",
                    company,
                    "--mercury-mapping",
                    str(mapping_path),
                ],
            )

            if close_result.exit_code == 0:
                print(f"close succeeded:\n{close_result.output}")
            elif _bank_difference_only(close_result.output):
                # Readiness refused on the sandbox's own bank data, and
                # nothing else -- exactly the outcome the task brief calls
                # out as legitimate. Nothing was locked or stored; there is
                # nothing further to check.
                print(
                    "close refused at readiness on the sandbox's own bank data -- a legitimate outcome, "
                    f"not a bug:\n{close_result.output}"
                )
                return
            elif "already has a stored package" in close_result.output:
                # Only reachable as a race: something else closed this
                # quarter between the skip-check above and this attempt.
                pytest.skip(f"{QUARTER} was closed by a concurrent run just now:\n{close_result.output}")
            else:
                pytest.fail(
                    f"period close failed for a reason other than a bank-data readiness refusal:\n{close_result.output}"
                )

            packages = {p.quarter.label: p for p in stored_packages(record, abbr)}
            assert QUARTER in packages, (
                f"no stored package for {QUARTER} under {abbr}/ in {live_books().record_buckets[Target.TEST]}"
            )
            stored = packages[QUARTER]
            assert stored.prefix.startswith(f"{abbr}/{QUARTER}/"), stored.prefix
            print(f"stored package: {stored.prefix} (revision {stored.revision})")

            status_result = CliRunner().invoke(cli.app, ["period", "status", "--target", "test"])
            assert status_result.exit_code == 0, status_result.output

            # Move Books Closed Through back one day directly, as an
            # administrator reopening the quarter in ERPNext would --
            # set_closed_through refuses a backwards move by design, so this
            # cannot go through it.
            current = read_lock(client, company)
            assert current.closed_through is not None, "Books Closed Through is unset right after a successful close"
            reopen_date = current.closed_through - dt.timedelta(days=1)
            client.update("Company", company, {LOCK_FIELDS[0]: reopen_date.isoformat()})

            reopened_status = CliRunner().invoke(cli.app, ["period", "status", "--target", "test"])
            assert reopened_status.exit_code == 1, reopened_status.output
            assert "reopen" in reopened_status.output.lower(), reopened_status.output
            print(
                f"status correctly failed after the lock moved back to {reopen_date.isoformat()}:\n{reopened_status.output}"
            )
        finally:
            client.update(
                "Company",
                company,
                {
                    LOCK_FIELDS[0]: original.closed_through.isoformat() if original.closed_through else None,
                    LOCK_FIELDS[1]: original.bypass_role,
                },
            )
            confirmed = read_lock(client, company)
            if confirmed != original:
                raise AssertionError(
                    f"could not restore {company}'s close lock to its original value: left at "
                    f"closed_through={confirmed.closed_through}, bypass_role={confirmed.bypass_role!r}; "
                    f"original was closed_through={original.closed_through}, bypass_role={original.bypass_role!r} "
                    "-- fix this on the test site now"
                )
