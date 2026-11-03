"""Load and validate a chart of accounts from a books directory's `chart.coa`.

The file is an indented outline: one account per line (`<number> <name>
[tags]`), two spaces per level, `>` lines carrying the account's note, and a
trailing `[defaults]` block. A group is any account with children -- an
empty group cannot be expressed. Every check here fails at load time so
that nothing fails halfway through an apply.

A tag block holds at most one `root:` tag, at most one ERPNext account type,
and the reserved word `no-bank-line`, which says the account only ever
receives a period-end or adjustment journal. The chart is the one place that
fact is written down; `runningtab.mercury.categories` reads it rather than keeping
a list of numbers beside the chart, because such a list goes stale invisibly.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

from icedcoffee_sync.apply.model import DesiredAccount
from icedcoffee_sync.erpnext.accounts import ROOT_TYPES, UnknownAccountTypeError, validate_account_type

# Every Company default field the chart must declare. Kept outside the file
# so a truncated [defaults] block is a load error, not a silently
# half-configured ledger; change it and chart.coa together.
REQUIRED_DEFAULT_FIELDS: frozenset[str] = frozenset({
    "default_bank_account",
    "default_cash_account",
    "default_receivable_account",
    "default_payable_account",
    "default_expense_account",
    "default_income_account",
    "round_off_account",
    "exchange_gain_loss_account",
    "write_off_account",
    "default_deferred_revenue_account",
    "default_deferred_expense_account",
    "accumulated_depreciation_account",
    "depreciation_expense_account",
    "disposal_account",
})

# Where the ingest puts a transaction it could not classify, and what
# `runningtab health` ages. Named here rather than only in `chart.coa` so that
# deleting it from the chart is a load error: the alternative is a health
# report that quietly stops looking at the one account nobody watches.
SUSPENSE_ACCOUNT_NUMBER = "5299"

_ACCOUNT_LINE = re.compile(r"^(?P<number>\d+)(?:\s+(?P<rest>.*))?$")
_ROOT_TAG_PREFIX = "root:"
# A reserved tag word, recognised before the account-type slot so it never
# reaches `validate_account_type`. It marks an account that only ever receives
# a period-end or adjustment journal -- depreciation, rounding, FX, write-offs,
# a disposal gain -- and so can never be what somebody picks against a bank
# line. `runningtab.mercury.categories` reads it and offers no category for those.
NO_BANK_LINE_TAG = "no-bank-line"


class ChartError(ValueError):
    """The chart file is not a chart we can apply."""


@dataclasses.dataclass(frozen=True)
class ChartEntry:
    """One account as the outline describes it, plus its note."""

    account: DesiredAccount
    note: str
    # Whether the line carried `no-bank-line`. Lives here and not on
    # `DesiredAccount` because it says nothing to ERPNext: the account is
    # ordinary there, and only the bank-facing pickers care.
    no_bank_line: bool = False


@dataclasses.dataclass(frozen=True)
class ChartDocument:
    """The parsed `chart.coa` file: accounts in file order, plus defaults."""

    entries: list[ChartEntry]
    defaults: dict[str, str]


def _split_name_and_tags(rest: str, lineno: int) -> tuple[str, list[str]]:
    """Split `<name> [tag, tag]` into the name and the tag list.

    Names may contain `&`, `/`, `(`, `)`, en dashes, `$`, and `,` -- only `[`
    is reserved -- so the first `[` unambiguously starts the tag block.
    """
    if "[" not in rest:
        name = rest.rstrip()
        return name, []
    name_part, _, tag_part = rest.partition("[")
    name = name_part.rstrip()
    if not tag_part.endswith("]"):
        raise ChartError(f"line {lineno}: tag block is missing its closing ']'")
    tag_content = tag_part[:-1].strip()
    tags = [t.strip() for t in tag_content.split(",") if t.strip()]
    if not tags:
        raise ChartError(f"line {lineno}: empty tag block '[]'")
    return name, tags


def _parse_tags(tags: list[str], lineno: int) -> tuple[str | None, str | None, bool]:
    """Split tags into (root type or None, account type or None, no-bank-line)."""
    root_declared: str | None = None
    account_type: str | None = None
    no_bank_line = False
    for tag in tags:
        if tag.startswith(_ROOT_TAG_PREFIX):
            if root_declared is not None:
                raise ChartError(f"line {lineno}: more than one 'root:' tag")
            root_declared = tag[len(_ROOT_TAG_PREFIX) :].strip()
        elif tag == NO_BANK_LINE_TAG:
            # Matched before the account-type slot: it is a word of our own,
            # not one of ERPNext's account types, and it shares the tag block
            # with one (`[Depreciation, no-bank-line]`).
            if no_bank_line:
                raise ChartError(f"line {lineno}: more than one {NO_BANK_LINE_TAG!r} tag")
            no_bank_line = True
        else:
            if account_type is not None:
                raise ChartError(f"line {lineno}: more than one account-type tag ({account_type!r}, {tag!r})")
            account_type = tag
    return root_declared, account_type, no_bank_line


@dataclasses.dataclass
class _ParseState:
    """Mutable state threaded through one pass over the file."""

    entries: list[ChartEntry] = dataclasses.field(default_factory=list)
    root_types_by_number: dict[str, str] = dataclasses.field(default_factory=dict)
    numbers_seen: set[str] = dataclasses.field(default_factory=set)
    ancestors: list[str] = dataclasses.field(default_factory=list)  # ancestors[level] = number at that level
    last_entry_index: int | None = None
    defaults: dict[str, str] = dataclasses.field(default_factory=dict)


def _parse_default_line(stripped: str, lineno: int, defaults: dict[str, str]) -> None:
    if "=" not in stripped:
        raise ChartError(f"line {lineno}: expected 'key = number' inside [defaults]")
    key, _, value = stripped.partition("=")
    key, value = key.strip(), value.strip()
    if not value.isdigit():
        raise ChartError(f"line {lineno}: default {key!r} must be an account number, got {value!r}")
    if key in defaults:
        raise ChartError(f"line {lineno}: duplicate default {key!r}")
    defaults[key] = value


def _append_note(state: _ParseState, stripped: str, lineno: int) -> None:
    if state.last_entry_index is None:
        raise ChartError(f"line {lineno}: note has no preceding account")
    note_text = stripped[1:].strip()
    previous = state.entries[state.last_entry_index]
    state.entries[state.last_entry_index] = dataclasses.replace(previous, note=f"{previous.note} {note_text}".strip())


def _account_level(raw: str, lineno: int) -> tuple[int, str]:
    indent = len(raw) - len(raw.lstrip(" "))
    if indent % 2 != 0:
        raise ChartError(f"line {lineno}: indent must be a whole number of two-space levels, got {indent} spaces")
    return indent // 2, raw[indent:]


def _account_root_type(
    level: int, parent_number: str | None, root_declared: str | None, number: str, lineno: int, state: _ParseState
) -> str:
    if level > 0:
        if root_declared is not None:
            raise ChartError(f"line {lineno}: account {number} is not a root; it must not carry 'root:'")
        if parent_number is None:
            # Unreachable in practice: _parse_account_line only calls this at
            # level > 0 once it has confirmed a parent slot exists. Guarded
            # explicitly anyway, so a future refactor that breaks that
            # invariant fails loudly instead of crashing on a bad dict key.
            raise ChartError(f"line {lineno}: account {number} has no parent to inherit a root type from")
        return state.root_types_by_number[parent_number]
    if root_declared is None:
        raise ChartError(f"line {lineno}: root account {number} must carry a 'root: <RootType>' tag")
    if root_declared not in ROOT_TYPES:
        raise ChartError(
            f"line {lineno}: root account {number} has root_type {root_declared!r}; expected one of {sorted(ROOT_TYPES)}"
        )
    return root_declared


def _parse_account_line(raw: str, lineno: int, state: _ParseState) -> None:
    level, body = _account_level(raw, lineno)

    match = _ACCOUNT_LINE.match(body)
    if not match:
        raise ChartError(f"line {lineno}: not a valid account line: {raw!r}")
    number = match.group("number")
    name, tags = _split_name_and_tags(match.group("rest") or "", lineno)
    if not name:
        raise ChartError(f"line {lineno}: account {number} has no name")

    if number in state.numbers_seen:
        raise ChartError(f"line {lineno}: duplicate account number {number}")
    state.numbers_seen.add(number)

    if level > len(state.ancestors):
        raise ChartError(f"line {lineno}: account {number} is indented more than one level past the previous account")
    del state.ancestors[level:]
    parent_number = state.ancestors[-1] if state.ancestors else None

    root_declared, account_type, no_bank_line = _parse_tags(tags, lineno)
    root_type = _account_root_type(level, parent_number, root_declared, number, lineno, state)

    try:
        validate_account_type(account_type)
    except UnknownAccountTypeError as exc:
        raise ChartError(f"line {lineno}: account {number}: {exc}") from exc

    state.ancestors.append(number)
    state.root_types_by_number[number] = root_type
    state.entries.append(
        ChartEntry(
            account=DesiredAccount(
                number=number,
                name=name,
                parent_number=parent_number,
                is_group=False,  # derived below, once every child is known
                root_type=root_type,
                account_type=account_type,
            ),
            note="",
            no_bank_line=no_bank_line,
        )
    )
    state.last_entry_index = len(state.entries) - 1


def _derive_is_group(entries: list[ChartEntry]) -> list[ChartEntry]:
    parents = {entry.account.parent_number for entry in entries if entry.account.parent_number}
    return [
        dataclasses.replace(entry, account=dataclasses.replace(entry.account, is_group=entry.account.number in parents))
        for entry in entries
    ]


def _parse(text: str) -> ChartDocument:
    state = _ParseState()
    in_defaults = False

    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped == "[defaults]":
            if in_defaults:
                raise ChartError(f"line {lineno}: a second '[defaults]' section; it must appear once, last")
            in_defaults = True
            continue

        if in_defaults:
            _parse_default_line(stripped, lineno, state.defaults)
        elif stripped.startswith(">"):
            _append_note(state, stripped, lineno)
        else:
            _parse_account_line(raw, lineno, state)

    return ChartDocument(entries=_derive_is_group(state.entries), defaults=state.defaults)


def load_chart_document(path: pathlib.Path) -> ChartDocument:
    """Parse `chart.coa`, or raise `ChartError` naming the offending line."""
    return _parse(path.read_text(encoding="utf-8"))


def parse_chart_entries(text: str) -> list[ChartEntry]:
    """`load_chart_entries`, from `chart.coa` text already in hand rather than read from `path` here.

    For a caller that must use one single read of the file across more than
    one step -- `period export` validates the live chart against it, builds
    the package from it, and hashes it for the manifest -- so that an edit
    landing between those reads cannot make one step see different content
    than another.
    """
    document = _parse(text)
    accounts = [entry.account for entry in document.entries]
    _check_match_keys(accounts)
    _check_roots(accounts)
    _check_defaults(accounts, document.defaults)
    _check_suspense(accounts)
    return document.entries


def load_chart_entries(path: pathlib.Path) -> list[ChartEntry]:
    """Read the chart and run every check, keeping each account's note and tags.

    `load_chart` is this with the notes and tags dropped. Anything that needs
    `no_bank_line` -- Mercury's category selection -- comes through here, so
    the flag reaches its reader from the same parse that validated the file.
    """
    return parse_chart_entries(path.read_text(encoding="utf-8"))


def parse_chart(text: str) -> list[DesiredAccount]:
    """`load_chart`, from `chart.coa` text already in hand -- see `parse_chart_entries`."""
    return [entry.account for entry in parse_chart_entries(text)]


def load_chart(path: pathlib.Path) -> list[DesiredAccount]:
    """Read the chart, or raise ChartError describing exactly what is wrong."""
    return [entry.account for entry in load_chart_entries(path)]


def suspense_account(accounts: list[DesiredAccount]) -> DesiredAccount:
    """The account a person parks a statement line in when they cannot yet say where it belongs.

    Present because `load_chart` insists: `runningtab health` ages what sits here.
    """
    return next(a for a in accounts if a.number == SUSPENSE_ACCOUNT_NUMBER)


def _check_suspense(accounts: list[DesiredAccount]) -> None:
    """The suspense account exists, is postable, and is an expense."""
    account = next((a for a in accounts if a.number == SUSPENSE_ACCOUNT_NUMBER), None)
    if account is None:
        raise ChartError(
            f"chart has no suspense account {SUSPENSE_ACCOUNT_NUMBER}; a person reconciling a statement line "
            f"they cannot yet code has nowhere to park it, and `runningtab health` has nothing to age"
        )
    if account.is_group:
        raise ChartError(f"suspense account {SUSPENSE_ACCOUNT_NUMBER} is a group and cannot be posted to")
    if account.root_type != "Expense":
        raise ChartError(
            f"suspense account {SUSPENSE_ACCOUNT_NUMBER} has root_type {account.root_type!r}; expected 'Expense'"
        )


def _check_roots(accounts: list[DesiredAccount]) -> None:
    """Exactly one root per ERPNext root type.

    A missing root type would make the site's whole subtree for it extras,
    which prune would delete after nothing but the usual prompt.
    """
    root_types = [a.root_type for a in accounts if a.parent_number is None]
    missing = sorted(ROOT_TYPES - set(root_types))
    if missing:
        raise ChartError(f"chart has no root account for root_type(s): {', '.join(missing)}")
    duplicated = sorted({rt for rt in root_types if root_types.count(rt) > 1})
    if duplicated:
        raise ChartError(f"chart declares more than one root for root_type(s): {', '.join(duplicated)}")


def _check_match_keys(accounts: list[DesiredAccount]) -> None:
    """Refuse two accounts `diff` cannot tell apart: same name under the same parent.

    Keyed on the parent's number, not its name, because the matching walk
    descends by parent identity; roots are identified by root type instead.
    """
    seen: dict[tuple[str, str], str] = {}
    for account in accounts:
        if account.parent_number is None:
            continue
        key = (account.name, account.parent_number)
        if key in seen:
            raise ChartError(
                f"accounts {seen[key]} and {account.number} share name {account.name!r} "
                f"under the same parent; the diff cannot tell them apart"
            )
        seen[key] = account.number


def _check_defaults(accounts: list[DesiredAccount], defaults: dict[str, str]) -> None:
    """Every default names a postable chart account, and the set is exactly `REQUIRED_DEFAULT_FIELDS`.

    Values are checked before the key set so a bad value is reported as
    itself rather than as a completeness complaint. ERPNext cannot post to
    a group, so a default naming one is refused.
    """
    by_number = {a.number: a for a in accounts}
    for field, number in defaults.items():
        account = by_number.get(number)
        if account is None:
            raise ChartError(f"default {field!r} names account {number}, which is not in the chart")
        if account.is_group:
            raise ChartError(
                f"default {field!r} names account {number} ({account.name!r}), which is a group and cannot be posted to"
            )

    missing_fields = sorted(REQUIRED_DEFAULT_FIELDS - defaults.keys())
    if missing_fields:
        raise ChartError(f"chart's [defaults] section is missing required field(s): {', '.join(missing_fields)}")
    unexpected_fields = sorted(defaults.keys() - REQUIRED_DEFAULT_FIELDS)
    if unexpected_fields:
        raise ChartError(
            f"chart's [defaults] section names field(s) not in REQUIRED_DEFAULT_FIELDS: {', '.join(unexpected_fields)}"
        )


def parse_defaults(text: str) -> dict[str, str]:
    """`load_defaults`, from `chart.coa` text already in hand -- see `parse_chart_entries`."""
    document = _parse(text)
    accounts = [entry.account for entry in document.entries]
    _check_defaults(accounts, document.defaults)
    return dict(document.defaults)


def load_defaults(path: pathlib.Path) -> dict[str, str]:
    """Company default-account fields, as account numbers."""
    return parse_defaults(path.read_text(encoding="utf-8"))
