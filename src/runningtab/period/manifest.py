"""`MANIFEST.md`: the one file in the package written for a person, not a program.

Nothing parses it (`SHA256SUMS` and `period status` carry the machine-readable
facts). The reader is an outside accountant or auditor who has never touched
these books, so every section says what it means in plain sentences rather than
pointing back at our own vocabulary.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence

from runningtab.period import PeriodError
from runningtab.period.bank import BankLine
from runningtab.period.csvfmt import money
from runningtab.period.qbo_map import QboMap
from runningtab.period.quarter import FiscalYear, Quarter

# One sentence per file, keyed by its exact path, or by a prefix for a family
# of files (`qbo/journal-entries-NN.csv`, `documents/...`). Keeping this a
# plain dict rather than a rule on the path means a new file with no entry
# here raises `KeyError` in `_describe` instead of shipping undescribed.
FILE_DESCRIPTIONS: dict[str, str] = {
    "chart-of-accounts.csv": (
        "Every account in the chart, with its number, name, parent, and type, as of this export."
    ),
    "trial-balance.csv": "Each account's opening balance, this quarter's debits and credits, and its closing balance.",
    "general-ledger.csv": "Every transaction posted in the quarter, one line per debit or credit.",
    "profit-and-loss.csv": "Income and expenses for the quarter and for the year to date.",
    "balance-sheet.csv": "Assets, liabilities, and equity as of the quarter's end.",
    "bank-reconciliation.csv": (
        "Each bank account's balance next to its matching chart account's balance, and the difference between them."
    ),
    "open-receivables.csv": "What each customer owed as of the quarter's end.",
    "open-payables.csv": "What we owed each vendor as of the quarter's end.",
    "qbo/accounts.csv": (
        "The chart accounts to create in QuickBooks Online, each with the Type and Detail Type QuickBooks requires."
    ),
    "qbo/customers.csv": "Every customer QuickBooks needs before the opening balances and journal entries import.",
    "qbo/vendors.csv": "Every vendor QuickBooks needs before the opening balances and journal entries import.",
    "qbo/opening-balances.csv": (
        "Every account's balance the day before the quarter began, written as journal entries. "
        "Import this file once only."
    ),
    "qbo/journal-entries-": (
        "One QuickBooks journal entry per transaction posted in the quarter, split into files of at most "
        "1,000 rows apiece."
    ),
    "documents/": "A receipt, invoice, or other file attached to a transaction or bank record dated in the quarter.",
}


# The final manifest's line naming the Company Version that locked the
# quarter. `lock_version_of` reads it back; one constant keeps the two from
# drifting apart.
LOCK_RECORD_PREFIX = "- **Lock record:** Company Version `"


def lock_version_of(manifest: str) -> str | None:
    """The Company Version a final manifest says locked its quarter, or None when it records none (a draft)."""
    for line in manifest.splitlines():
        if line.startswith(LOCK_RECORD_PREFIX) and line.endswith("`"):
            return line[len(LOCK_RECORD_PREFIX) : -1]
    return None


def _describe(path: str) -> str:
    if path in FILE_DESCRIPTIONS:
        return FILE_DESCRIPTIONS[path]
    for prefix in ("qbo/journal-entries-", "documents/"):
        if path.startswith(prefix):
            return FILE_DESCRIPTIONS[prefix]
    raise KeyError(path)


@dataclasses.dataclass(frozen=True)
class ManifestFacts:
    quarter: Quarter
    fiscal_year: FiscalYear
    company: str
    # `books.toml`'s name: the title says whose books these are.
    books_name: str
    site_url: str
    chart_sha256: str
    qmap: QboMap
    generated_at: dt.datetime
    generated_by: str
    draft: bool
    today: dt.date
    bank: tuple[BankLine, ...]
    split_vouchers: tuple[str, ...]
    has_journals: bool
    closed_by: str | None = None
    lock_version: str | None = None
    revision: int = 1


def _title(facts: ManifestFacts) -> str:
    suffix = " — DRAFT" if facts.draft else ""
    return f"# {facts.books_name} books — {facts.quarter.label}{suffix}"


def _facts_section(facts: ManifestFacts) -> str:
    lines = [
        f"- **Quarter:** {facts.quarter.label}, {facts.quarter.start.isoformat()} through "
        f"{facts.quarter.end.isoformat()}",
        f"- **Fiscal year:** {facts.fiscal_year.name}, {facts.fiscal_year.start.isoformat()} through "
        f"{facts.fiscal_year.end.isoformat()}",
        f"- **Company:** {facts.company}",
        f"- **Site:** {facts.site_url}",
        f"- **Chart of accounts:** `chart.coa`, SHA-256 `{facts.chart_sha256}`",
        f"- **Generated:** {facts.generated_at.astimezone(dt.UTC).strftime('%Y-%m-%d %H:%M UTC')} "
        f"by {facts.generated_by}",
    ]
    if not facts.draft:
        lines.append(f"- **Closed by:** {facts.closed_by}")
        lines.append(f"{LOCK_RECORD_PREFIX}{facts.lock_version}`")
        lines.append(f"- **Revision:** {facts.revision}")
    if facts.today <= facts.quarter.end:
        lines.append("- **Note:** this quarter not yet ended; these figures will change before the quarter closes.")
    return "\n".join(lines)


def _files_section(files: dict[str, bytes]) -> str:
    lines = [
        "- **`MANIFEST.md`** — this document.",
        "- **`SHA256SUMS`** — a checksum for every other file, to prove nothing in the package changed.",
    ]
    lines.extend(f"- **`{path}`** — {_describe(path)}" for path in sorted(files))
    return "## Files\n\n" + "\n".join(lines)


_SIGN_CONVENTIONS = (
    "## Sign conventions\n\n"
    "The trial balance and the general ledger split each account's net balance into separate debit and credit "
    "columns: a debit balance appears in the debit column, a credit balance in the credit column. The profit and "
    "loss statement shows income and expenses both as positive numbers. The balance sheet shows assets, "
    "liabilities, and equity each as a positive number. A text cell in these CSVs that begins with a formula "
    "character (`=`, `+`, `@`, or `-` unless it is a plain negative number) is prefixed with an apostrophe so a "
    "spreadsheet shows it as text instead of running it as a formula. In the `qbo/` files, description text is "
    "escaped the same way; an account or party name that begins with a formula character is refused instead, "
    "since QuickBooks matches those by exact text and an added apostrophe would break the match."
)


def _bank_section(bank: Sequence[BankLine]) -> str:
    if not bank:
        return "## Bank reconciliation\n\nNo Mercury account was reconciled this quarter."
    if all(line.difference == 0 for line in bank):
        summary = "All accounts reconcile: the difference is zero for every Mercury account."
    else:
        summary = "Not every account reconciles. The differences below that are not zero appear in **bold**."
    rows = [
        "| Account | Mercury account | Bank balance | Ledger balance | Difference |",
        "| --- | --- | --- | --- | --- |",
    ]
    for line in bank:
        diff = money(line.difference)
        cell = f"**{diff}**" if line.difference != 0 else diff
        rows.append(f"| {line.number} | {line.label} | {money(line.bank)} | {money(line.ledger)} | {cell} |")
    return "## Bank reconciliation\n\n" + summary + "\n\n" + "\n".join(rows)


def _qbo_section(facts: ManifestFacts) -> str:
    qmap = facts.qmap
    builtin_lines = "\n".join(
        f"- Our account `{number}` maps onto QuickBooks's built-in `{entry.name}`."
        for number, entry in sorted(qmap.builtin.items(), key=lambda pair: int(pair[0]))
    )
    account_count = len(qmap.accounts)
    if facts.has_journals:
        journal_note = "Every `qbo/journal-entries-NN.csv` file listed above belongs in this step."
    else:
        journal_note = "There is nothing to import here: no journal entries this quarter."
    if facts.split_vouchers:
        split_note = (
            "The following vouchers touched more than one customer or vendor and were split into more than one "
            "journal entry, balanced through the `QBO Import Clearing` account so the split nets to zero: "
            + ", ".join(facts.split_vouchers)
            + "."
        )
    else:
        split_note = "No voucher needed splitting this quarter."
    return (
        "## Importing into QuickBooks Online\n\n"
        "Import the files in this order:\n\n"
        "1. `qbo/accounts.csv`\n"
        "2. `qbo/customers.csv` and `qbo/vendors.csv`\n"
        "3. Turn account numbers off in QuickBooks. Both `qbo/opening-balances.csv` and "
        "`qbo/journal-entries-NN.csv` use the same journal import, so numbers must stay off through both steps "
        "below; turn them back on only once step 5 finishes, as Intuit's own instructions say to.\n"
        "4. `qbo/opening-balances.csv` — **first import only: importing it twice doubles every balance.**\n"
        "5. `qbo/journal-entries-NN.csv`\n\n"
        f"{journal_note}\n\n"
        f"{split_note}\n\n"
        f"This kit does not create new QuickBooks accounts for {len(qmap.builtin)} of ours. It posts to the "
        "accounts QuickBooks already provides on every company:\n\n"
        f"{builtin_lines}\n\n"
        "Several details of this format are unverified against a real QuickBooks import. Check each on the "
        "first import:\n\n"
        "- The journal date is written as `YYYY-MM-DD` (ISO 8601).\n"
        "- The unused side of each journal line — the Debits or Credits cell that does not apply to that "
        "line — is left blank rather than filled with a zero.\n"
        "- A split voucher's Journal No. can run to 21 characters (for example `ACC-PINV-2026-00001/2`); "
        "check that QuickBooks accepts a Journal No. that long.\n"
        "- Whether a party that is both a customer and a vendor imports under one name or two.\n\n"
        f"`qbo/accounts.csv` assigns each account a QuickBooks Type and Detail Type chosen account by account "
        f"in `qbo.toml`; nobody has yet checked the {account_count} choices against a live QuickBooks company. "
        "Check them on the first import, and correct `qbo.toml` if any are wrong."
    )


_WHAT_THE_KIT_LEAVES_OUT = (
    "## What the QBO kit leaves out\n\n"
    "It carries every amount, account, date and party, and reproduces our trial balance. It does not carry: "
    "the chart's hierarchy (group accounts and their nesting), ERPNext's voucher types (every voucher becomes "
    "a QBO journal entry, not an invoice or bill), cost centres, the invoice-to-payment links, the split "
    "vouchers' original single-entry shape, or attachments. Those stay in ERPNext and in the rest of the "
    "package, which remain the record."
)


_VERIFYING_THIS_PACKAGE = (
    "## Verifying this package\n\n"
    "From inside this directory, run `sha256sum -c SHA256SUMS`. Every file it lists should print `OK`. "
    "`SHA256SUMS` covers every file in the package except this one: `MANIFEST.md` records the moment it was "
    "generated, so a checksum that covered it would never match between two otherwise identical builds. "
    "After a close, `runningtab period status` rebuilds this quarter's package from the ledger and compares it "
    "against this one; a bank transaction that settles later but is dated inside this quarter will show up "
    "there as a change to `bank-reconciliation.csv`."
)


def render_manifest(facts: ManifestFacts, files: dict[str, bytes]) -> bytes:
    """`MANIFEST.md`, as UTF-8 Markdown ending in a newline.

    A final manifest (`draft=False`) with `closed_by` or `lock_version`
    still `None` would print "Closed by: None" or a backtick-quoted `None`
    as the lock's own Version -- a confident wrong answer about who closed
    the quarter, printed into the one record an outside accountant is
    handed. Refusing here means that can never reach a package.
    """
    if not facts.draft and (facts.closed_by is None or facts.lock_version is None):
        raise PeriodError(
            "a final manifest needs closed_by and lock_version; render_manifest was given draft=False without them"
        )
    sections = [
        _title(facts),
        _facts_section(facts),
        _files_section(files),
        _SIGN_CONVENTIONS,
        _bank_section(facts.bank),
        _qbo_section(facts),
        _WHAT_THE_KIT_LEAVES_OUT,
        _VERIFYING_THIS_PACKAGE,
    ]
    return ("\n\n".join(sections) + "\n").encode("utf-8")
