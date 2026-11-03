"""The whole QuickBooks Online import kit: accounts, parties, opening balances, journal entries.

One QBO journal entry per ERPNext voucher. QBO allows one A/R or A/P line per
transaction, so a voucher with more is split through `QBO Import Clearing`,
whose lines net to zero across the voucher. Files stay under QBO's 1,000-row
import cap and never divide an entry. `check_replay` proves the kit's opening
balances plus its journal entries reproduce our trial balance account by
account, so a kit that would silently misstate the books is refused before
it ships.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable, Sequence
from decimal import Decimal

from icedcoffee_sync.apply.model import DesiredAccount

from runningtab.period import PeriodError
from runningtab.period.balances import TrialBalance
from runningtab.period.csvfmt import is_formula_like, money, render_csv, spreadsheet_safe_cell
from runningtab.period.ledger import AccountIndex, GLRow
from runningtab.period.open_items import open_items
from runningtab.period.qbo_map import CLEARING_ACCOUNT_NAME, QboMap, validate_qbo_map
from runningtab.period.quarter import Quarter

# Intuit's journal-import article confirms the CSV's columns but not the
# Journal Date format or the unused Debits/Credits cell's spelling -- neither
# is stated in its text, its UK/CA counterparts, or any community thread
# found, and both appear only inside the in-app "Download a sample file"
# link, unreachable without a signed-in QBO session (see qbo.toml's header).
# Controller's ruling (2026-09-22), UNVERIFIED pending a manual trial-company
# import: ISO `%Y-%m-%d`, because ISO cannot be misread, whereas MM/DD vs
# DD/MM would silently swap day and month; the empty side stays blank.
QBO_DATE_FORMAT = "%Y-%m-%d"
_AR_AP = ("Receivable", "Payable")


@dataclasses.dataclass(frozen=True)
class QboLine:
    account: str
    net: Decimal
    description: str
    party: str | None
    is_ar_ap: bool


@dataclasses.dataclass(frozen=True)
class QboEntry:
    journal_no: str
    date: dt.date
    lines: tuple[QboLine, ...]
    source: str


def qbo_account_name(account: DesiredAccount, qmap: QboMap) -> str:
    entry = qmap.builtin.get(account.number)
    return entry.name if entry is not None else account.name


def _clearing(net: Decimal) -> QboLine:
    return QboLine(CLEARING_ACCOUNT_NAME, net, "", None, False)


def split_entry(journal_no: str, date: dt.date, lines: Sequence[QboLine], source: str) -> list[QboEntry]:
    ar = [line for line in lines if line.is_ar_ap]
    rest = [line for line in lines if not line.is_ar_ap]
    if len(ar) <= 1:
        return [QboEntry(journal_no, date, (*ar, *rest), source)]
    first_lines = [ar[0], *rest]
    entries = [
        QboEntry(
            f"{journal_no}/1",
            date,
            (*first_lines, _clearing(-sum((line.net for line in first_lines), Decimal(0)))),
            source,
        )
    ]
    for n, line in enumerate(ar[1:], start=2):
        entries.append(QboEntry(f"{journal_no}/{n}", date, (line, _clearing(-line.net)), source))
    return entries


def journal_entries(rows: Sequence[GLRow], index: AccountIndex, qmap: QboMap, quarter: Quarter) -> list[QboEntry]:
    """One `QboEntry` per voucher in `quarter`, split where QBO's one-AR/AP-line rule demands.

    Vouchers come out in ledger read order (a voucher's position is where its
    first row appears in `rows`); within a voucher, lines follow GL order,
    with the AR/AP line moved first when a split has to isolate it; a split
    voucher's pieces come out in `/1`, `/2`, ... order.

    Every row of a voucher that has at least one row in `quarter` is grouped
    and date-checked before anything else runs: such a voucher straddling
    the quarter boundary (one row in the prior quarter, one row in this one)
    must be refused by name, not silently admitted as an unbalanced entry
    built from only its in-quarter rows. A voucher with no row in the
    quarter at all is ignored here even if its own history spans more than
    one date -- an old voucher that only feeds `opening_entries`'s running
    balance is not a reason to block this quarter's export; that date
    confusion is `opening_entries`'s to catch, if anything's.

    Grouped by `(voucher_type, voucher_no)`, not `voucher_no` alone: two
    vouchers of different types that happen to share a number (a Journal
    Entry and a Purchase Invoice, say) must stay separate QBO entries rather
    than having their GL rows merged into one. But the QBO CSV's Journal No.
    column is `voucher_no` alone, with no room for the type, so two such
    vouchers landing in the same quarter would collide there even though they
    never merged here; refused by name rather than shipped as a kit QBO would
    read back as one entry.
    """
    vouchers: dict[tuple[str, str], list[GLRow]] = {}
    for row in rows:
        vouchers.setdefault((row.voucher_type, row.voucher_no), []).append(row)
    in_quarter = {
        key: members
        for key, members in vouchers.items()
        if any(quarter.start <= m.posting_date <= quarter.end for m in members)
    }
    spanning = sorted(
        voucher_no
        for (_voucher_type, voucher_no), members in in_quarter.items()
        if len({m.posting_date for m in members}) > 1
    )
    if spanning:
        raise PeriodError(
            f"vouchers with GL entries on more than one date cannot become one QBO entry: {', '.join(spanning)}"
        )
    entries: list[QboEntry] = []
    types_by_number: dict[str, set[str]] = {}
    for (voucher_type, voucher_no), members in in_quarter.items():
        # `members` is single-date and in-quarter by construction: `spanning`
        # above already refused any surviving voucher with more than one
        # date, and `in_quarter` kept only vouchers with a row in range.
        date = members[0].posting_date
        types_by_number.setdefault(voucher_no, set()).add(voucher_type)
        lines = []
        for m in members:
            account = index[m.account]
            is_ar_ap = account.account_type in _AR_AP
            lines.append(
                QboLine(qbo_account_name(account, qmap), m.net, m.remarks, m.party if is_ar_ap else None, is_ar_ap)
            )
        entries.extend(split_entry(voucher_no, date, lines, voucher_no))
    colliding = sorted(no for no, types in types_by_number.items() if len(types) > 1)
    if colliding:
        detail = "; ".join(f"{no} ({', '.join(sorted(types_by_number[no]))})" for no in colliding)
        raise PeriodError(
            "Journal No. is voucher_no alone, so vouchers of different types sharing a number would collide "
            f"in the QBO import: {detail}"
        )
    unbalanced = sorted(e.journal_no for e in entries if sum((line.net for line in e.lines), Decimal(0)) != 0)
    if unbalanced:
        raise PeriodError(f"QBO entries do not balance to zero: {', '.join(unbalanced)}")
    return entries


def check_opening_size(entries: Sequence[QboEntry], max_rows: int = 999, max_bytes: int = 2_000_000) -> None:
    """Refuse, naming the line count or byte count, an opening kit too big for the one file it ships as.

    Unlike the quarter's journal entries, `opening-balances.csv` is never
    split across files (`build_qbo_kit` writes it as a single file, and
    importing it twice doubles every balance, which splitting would make
    easy to do by accident) -- so it must fit under both of QBO's import
    caps on its own: 1,000 rows AND 2 MB (`max_bytes` is conservative).
    Splitting is not needed today; this only refuses the kit that would
    need it.
    """
    total_rows = sum(len(e.lines) for e in entries)
    if total_rows > max_rows:
        raise PeriodError(
            f"the opening balances need {total_rows} rows, more than the {max_rows} a single QBO import file "
            "allows; opening-balances.csv is not split across files"
        )
    total_bytes = len(journal_csv(entries))
    if total_bytes > max_bytes:
        raise PeriodError(
            f"the opening balances render to {total_bytes:,} bytes, more than the {max_bytes:,} a single QBO "
            "import file allows; opening-balances.csv is not split across files"
        )


def chunk(entries: Sequence[QboEntry], max_rows: int = 999, max_bytes: int = 2_000_000) -> list[list[QboEntry]]:
    """Group `entries` into files of at most `max_rows` rows and `max_bytes` rendered bytes, splitting only at entry boundaries.

    Intuit's limit is 1,000 rows AND 2 MB per file, not rows alone; `max_bytes`
    is a conservative reading of the 2 MB figure. An entry's byte cost is
    measured by rendering it alone through `journal_csv` -- the same CSV
    writer `journal_csv` uses for the file itself -- and subtracting the
    header's own cost, so the estimate matches what actually lands in the
    file rather than a hand-rolled guess at CSV overhead.
    """
    too_long = [e.journal_no for e in entries if len(e.lines) > max_rows]
    if too_long:
        raise PeriodError(f"QBO entries longer than one import file allows ({max_rows} rows): {', '.join(too_long)}")
    header_bytes = len(journal_csv([]))
    costs = [len(journal_csv([entry])) - header_bytes for entry in entries]
    too_big = sorted({
        entry.journal_no for entry, cost in zip(entries, costs, strict=True) if header_bytes + cost > max_bytes
    })
    if too_big:
        raise PeriodError(
            f"QBO entries whose rendered size alone, plus header, exceeds one import file's {max_bytes:,}-byte "
            f"limit: {', '.join(too_big)}"
        )
    files: list[list[QboEntry]] = []
    current: list[QboEntry] = []
    rows = 0
    size = header_bytes
    for entry, cost in zip(entries, costs, strict=True):
        if current and (rows + len(entry.lines) > max_rows or size + cost > max_bytes):
            files.append(current)
            current, rows, size = [], 0, header_bytes
        current.append(entry)
        rows += len(entry.lines)
        size += cost
    if current:
        files.append(current)
    return files


def _sides(net: Decimal) -> tuple[str, str]:
    # The empty side's spelling is the controller's ruling above (blank, not
    # "0"): UNVERIFIED against a real QBO import.
    return (money(net), "") if net >= 0 else ("", money(-net))


def _refuse_formula_like_names(column: str, location: str, values: Iterable[str]) -> None:
    """Refuse, naming each, a `column` value in `location` a spreadsheet would read as a formula.

    QuickBooks matches Name and Account Name values by exact text, so the
    leading-apostrophe escape the human-facing CSVs use would itself break
    the match -- there is no way to make a formula-like value safe here,
    only to refuse it before it ships.
    """
    bad = sorted({v for v in values if is_formula_like(v)})
    if bad:
        raise PeriodError(
            f"{location} would write a {column} value a spreadsheet reads as a formula, which cannot be escaped "
            f"without breaking QuickBooks's exact-text match: {', '.join(bad)}"
        )


def journal_csv(entries: Sequence[QboEntry]) -> bytes:
    """Intuit's journal-import columns, one CSV row per line.

    Rows follow `entries` in the order given, and within each entry follow
    `entry.lines` in the order given -- this function reorders nothing, so
    `journal_entries`'s ordering (ledger read order, AR/AP line first, split
    pieces in `/1`, `/2`, ... order) is what lands in the file.

    Journal No., Account Name, and Name are keys QuickBooks matches by exact
    text, so a formula-like value there is refused, naming each, rather than
    escaped -- unlike the human-facing CSVs, an apostrophe here would itself
    break the match. That includes an `OPENING-...` number and a split
    piece's `/1`, `/2` suffix: both are written to Journal No. exactly like
    a voucher number is. Journal/Description is free text, not a key, so it
    is escaped the same way the human-facing CSVs are.
    """
    bad_journal_nos = sorted({e.journal_no for e in entries if is_formula_like(e.journal_no)})
    bad_names = sorted(
        {(line.account, e.journal_no) for e in entries for line in e.lines if is_formula_like(line.account)}
        | {
            (line.party, e.journal_no)
            for e in entries
            for line in e.lines
            if line.party and is_formula_like(line.party)
        }
    )
    if bad_journal_nos or bad_names:
        parts = []
        if bad_journal_nos:
            parts.append(f"Journal No. {', '.join(bad_journal_nos)}")
        if bad_names:
            parts.append(
                "Account Name/Name " + ", ".join(f"{value!r} in {journal_no}" for value, journal_no in bad_names)
            )
        raise PeriodError(
            "QuickBooks matches Journal No., Account Name, and Name by exact text; a value that would open "
            f"as a formula in a spreadsheet cannot be escaped without breaking that match: {'; '.join(parts)}"
        )
    header = ["Journal No.", "Journal Date", "Account Name", "Debits", "Credits", "Journal/Description", "Name"]
    rows = [
        [
            e.journal_no,
            e.date.strftime(QBO_DATE_FORMAT),
            line.account,
            *_sides(line.net),
            spreadsheet_safe_cell(line.description),
            line.party or "",
        ]
        for e in entries
        for line in e.lines
    ]
    return render_csv(header, rows, spreadsheet_safe=False)


def parties_csv(parties: Sequence[str], location: str) -> bytes:
    """One party per row, sorted, deduplicated: QBO refuses a contact it already has."""
    names = sorted(set(parties))
    _refuse_formula_like_names("Name", location, names)
    return render_csv(["Name"], [[p] for p in names], spreadsheet_safe=False)


def accounts_csv(chart: Sequence[DesiredAccount], qmap: QboMap) -> bytes:
    """Postable, non-builtin accounts in number order, then the clearing account.

    QBO already carries its own Accounts Receivable, Accounts Payable, and
    Retained Earnings -- the builtins in `qmap` -- so those are never
    imported as accounts; `opening_entries` and `journal_entries` still post
    to them by name.
    """
    rows = [
        [a.name, *qmap.accounts[a.number], a.number]
        for a in sorted(chart, key=lambda a: a.number)
        if not a.is_group and a.number not in qmap.builtin
    ]
    rows.append([CLEARING_ACCOUNT_NAME, *qmap.clearing, ""])
    _refuse_formula_like_names("Account Name", "accounts.csv", (row[0] for row in rows))
    return render_csv(["Account Name", "Type", "Detail Type", "Number"], rows, spreadsheet_safe=False)


def _retained_earnings_builtin(qmap: QboMap, index: AccountIndex) -> str:
    equity = [n for n in qmap.builtin if any(a.number == n and a.root_type == "Equity" for a in index.values())]
    if len(equity) != 1:
        raise PeriodError(f"qbo.toml [builtin] must map exactly one Equity account (Retained Earnings); found {equity}")
    return qmap.builtin[equity[0]].name


def _sole_account(index: AccountIndex, kind: str) -> DesiredAccount:
    """The one postable account of `kind` (Receivable or Payable) that QBO's single A/R or A/P builtin stands in for.

    QBO has exactly one Accounts Receivable and one Accounts Payable, so a
    second postable account of the same kind in `chart.coa` can't be told
    apart once its lines land there; refuse by name -- naming every
    candidate found -- rather than picking one with `next(...)` and quietly
    dropping the other's AR/AP lines, or crashing with a bare
    `StopIteration` when there is none.
    """
    candidates = sorted(
        (a for a in index.values() if a.account_type == kind and not a.is_group), key=lambda a: a.number
    )
    if len(candidates) != 1:
        numbers = ", ".join(a.number for a in candidates) or "none"
        raise PeriodError(f"expected exactly one postable {kind} account in chart.coa; found {numbers}")
    return candidates[0]


def opening_entries(tb: TrialBalance, rows: Sequence[GLRow], index: AccountIndex, qmap: QboMap) -> list[QboEntry]:
    """One entry dated the day before the quarter, opening every account QBO does not already carry from history.

    A non-AR/AP postable account with a non-zero opening balance gets one
    line; the A/R and A/P builtins are opened by party instead, one line per
    open item as of that day, because QBO demands a customer or vendor on
    every A/R or A/P line; and any prior-year P&L never closed by a Period
    Closing Voucher lands on the Retained Earnings builtin, the same figure
    `check_replay` expects it to carry.
    """
    day_before = tb.quarter.start - dt.timedelta(days=1)
    lines: list[QboLine] = []
    for line in tb.lines:
        if line.account.account_type in _AR_AP or line.opening == 0:
            continue
        lines.append(QboLine(qbo_account_name(line.account, qmap), line.opening, "Opening balance", None, False))
    for kind in _AR_AP:
        account = _sole_account(index, kind)
        sign = 1 if kind == "Receivable" else -1
        for item in open_items(rows, index, kind, day_before):
            lines.append(
                QboLine(qbo_account_name(account, qmap), sign * item.amount, f"Open {item.document}", item.party, True)
            )
    if tb.prior_year_unclosed:
        lines.append(
            QboLine(
                _retained_earnings_builtin(qmap, index),
                tb.prior_year_unclosed,
                "Prior-year earnings, unclosed",
                None,
                False,
            )
        )
    if not lines:
        return []
    return split_entry(f"OPENING-{tb.quarter.label}", day_before, lines, f"OPENING-{tb.quarter.label}")


def replay(entries: Sequence[QboEntry]) -> dict[str, Decimal]:
    """Every QBO account's net across `entries`, by QBO account name."""
    totals: dict[str, Decimal] = {}
    for entry in entries:
        for line in entry.lines:
            totals[line.account] = totals.get(line.account, Decimal(0)) + line.net
    return totals


def check_replay(entries: Sequence[QboEntry], tb: TrialBalance, qmap: QboMap) -> None:
    """Refuse unless every QBO account's replayed net equals what our books expect it to close at.

    The Retained Earnings builtin carries the ordinary account's closing
    balance plus the prior-year figure `opening_entries` posted to it, and
    the clearing account must always net to zero -- its lines exist only to
    balance a split voucher.
    """
    expected: dict[str, Decimal] = {CLEARING_ACCOUNT_NAME: Decimal(0)}
    for line in tb.lines:
        name = qbo_account_name(line.account, qmap)
        expected[name] = expected.get(name, Decimal(0)) + line.closing
    retained = next(
        (
            qmap.builtin[n].name
            for n in qmap.builtin
            if any(tbline.account.number == n and tbline.account.root_type == "Equity" for tbline in tb.lines)
        ),
        None,
    )
    if retained is not None:
        expected[retained] = expected.get(retained, Decimal(0)) + tb.prior_year_unclosed
    actual = replay(entries)
    wrong = sorted(
        f"{name}: kit {actual.get(name, Decimal(0))}, books {expected.get(name, Decimal(0))}"
        for name in expected.keys() | actual.keys()
        if actual.get(name, Decimal(0)) != expected.get(name, Decimal(0))
    )
    if wrong:
        raise PeriodError("the QBO kit would not reproduce our trial balance:\n  " + "\n  ".join(wrong))


def refuse_duplicate_journal_numbers(entries: Sequence[QboEntry]) -> None:
    """Refuse, naming each, any finished Journal No. shared by more than one entry.

    `journal_entries`'s own collision check compares voucher numbers before
    splitting adds `/1`, `/2` -- so a voucher literally named `JV-1/1` passes
    that check even though it collides with the first half of a split
    `JV-1`. This runs after opening and journal entries are both built, once
    every entry (split or not, opening or quarter) carries the Journal No.
    that will actually land in a file, and catches what the earlier check
    cannot see.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.journal_no] = counts.get(entry.journal_no, 0) + 1
    duplicated = sorted(no for no, count in counts.items() if count > 1)
    if duplicated:
        raise PeriodError(f"QBO Journal No. is not unique across the kit: {', '.join(duplicated)}")


def check_file_size(name: str, row_count: int, data: bytes, max_rows: int = 999, max_bytes: int = 2_000_000) -> None:
    """Refuse, naming `name`, a QBO import file over either of Intuit's caps: 1,000 rows and 2 MB.

    `check_opening_size` and `chunk` cover the entries-shaped files
    (`opening-balances.csv`, `journal-entries-NN.csv`) by measuring an entry
    at a time; `customers.csv` and `vendors.csv` are a plain party list with
    no entry to measure that way, so this checks the finished file's own row
    count and rendered size directly instead -- the same two caps, reused
    rather than re-derived. Splitting is not needed today: this only refuses
    the file that would need it.
    """
    if row_count > max_rows:
        raise PeriodError(f"{name} needs {row_count} rows, more than the {max_rows} a single QBO import file allows")
    if len(data) > max_bytes:
        raise PeriodError(
            f"{name} renders to {len(data):,} bytes, more than the {max_bytes:,} a single QBO import file allows"
        )


@dataclasses.dataclass(frozen=True)
class QboKit:
    files: dict[str, bytes]
    split_vouchers: tuple[str, ...]


def build_qbo_kit(
    tb: TrialBalance,
    rows: Sequence[GLRow],
    index: AccountIndex,
    chart: Sequence[DesiredAccount],
    qmap: QboMap,
    quarter: Quarter,
) -> QboKit:
    """Assemble the whole `qbo/` import directory and prove it replays to our books before returning it."""
    validate_qbo_map(qmap, chart)
    opening = opening_entries(tb, rows, index, qmap)
    check_opening_size(opening)
    journals = journal_entries(rows, index, qmap, quarter)
    refuse_duplicate_journal_numbers([*opening, *journals])
    check_replay([*opening, *journals], tb, qmap)
    receivable_name = qbo_account_name(_sole_account(index, "Receivable"), qmap)
    payable_name = qbo_account_name(_sole_account(index, "Payable"), qmap)
    customer_parties = [
        line.party for e in [*opening, *journals] for line in e.lines if line.party and line.account == receivable_name
    ]
    vendor_parties = [
        line.party for e in [*opening, *journals] for line in e.lines if line.party and line.account == payable_name
    ]
    customers_csv = parties_csv(customer_parties, "customers.csv")
    vendors_csv = parties_csv(vendor_parties, "vendors.csv")
    check_file_size("customers.csv", len(set(customer_parties)), customers_csv)
    check_file_size("vendors.csv", len(set(vendor_parties)), vendors_csv)
    accounts_data = accounts_csv(chart, qmap)
    # +1 for the clearing account row accounts_csv always appends.
    accounts_row_count = sum(1 for a in chart if not a.is_group and a.number not in qmap.builtin) + 1
    check_file_size("accounts.csv", accounts_row_count, accounts_data)
    files = {
        "accounts.csv": accounts_data,
        "customers.csv": customers_csv,
        "vendors.csv": vendors_csv,
        "opening-balances.csv": journal_csv(opening),
    }
    for n, group in enumerate(chunk(journals), start=1):
        files[f"journal-entries-{n:02d}.csv"] = journal_csv(group)
    # A voucher is split when it produced more than one QboEntry -- counted
    # by source voucher, not by "/" in journal_no: a voucher whose own
    # number happens to contain "/" is not split, and checking the
    # character would misreport it as one to the accountant reading
    # MANIFEST.md.
    source_counts: dict[str, int] = {}
    for e in journals:
        source_counts[e.source] = source_counts.get(e.source, 0) + 1
    split = tuple(sorted(source for source, count in source_counts.items() if count > 1))
    return QboKit(files, split)
