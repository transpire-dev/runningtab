"""Assembling a `PostingContext` from a live site, the chart, and the mapping.

Kept out of `runningtab.cli` so the wiring is testable without a terminal, and out of
`posting.py` so that module stays pure enough to exercise against captured
payloads alone.
"""

from __future__ import annotations

from icedcoffee_sync.apply.model import DesiredAccount
from icedcoffee_sync.erpnext.accounts import read_chart
from icedcoffee_sync.frappe.client import Client

from runningtab.coa import sole_company
from runningtab.mercury.client import MercuryClient
from runningtab.mercury.mapping import Mapping, MappingError, validate_against_chart
from runningtab.mercury.posting import PostingContext
from runningtab.mercury.vouchers import Chart
from runningtab.period.lock import read_visible_lock
from runningtab.site import Profile


def posting_context(
    ledger: Client,
    *,
    mapping: Mapping,
    chart: list[DesiredAccount],
    bot_user: str,
    mercury: MercuryClient | None = None,
    company: str | None = None,
) -> PostingContext:
    """Everything a Bank Transaction needs that is not on the transaction.

    Account document names come from the **live site**, not from rendering the
    chart: an account's document name derives from its number and title, so a
    site whose chart has not been applied yet would otherwise produce links that
    look right and resolve to nothing.

    Books Closed Through is read here too, alongside Company -- the one read
    this module already makes -- so the voucher pass can leave a closed
    quarter's rows to a person. A bot that cannot see the permlevel-1 lock
    fields (never re-provisioned with its Company read grant, or a provision
    that installed the fence and then failed) is refused by name, before
    anything is posted: Frappe deletes the fields rather than returning them
    empty, so reading on would treat a closed quarter as open. Nothing else
    would catch that. ERPNext checks the frozen date only when GL entries
    are made, on submit, and the ingest saves its vouchers as drafts.
    """
    unknown = validate_against_chart(mapping, {a.number for a in chart})
    if unknown:
        raise MappingError(f"mercury.toml names accounts the chart does not have: {', '.join(unknown)}")
    company = company or sole_company(ledger)
    # First, so a bot that cannot see the lock is refused before any other read.
    lock = read_visible_lock(ledger, company, Profile.BOT)
    account_names = {a.number: a.document_name for a in read_chart(ledger, company) if a.number}
    mercury_names = {a.id: a.name for a in mercury.accounts()} if mercury is not None else {}
    return PostingContext(
        company=company,
        account_names=account_names,
        mapping=mapping,
        bot_user=bot_user,
        chart=Chart.load(chart),
        mercury_account_names=mercury_names,
        lock=lock,
    )
