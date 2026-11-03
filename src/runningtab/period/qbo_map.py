"""What QuickBooks Online needs to know about our chart, from `qbo.toml`.

Explicit per account rather than a rule on root and account type: 62 lines a
person can read beat an engine whose output they have to predict.
"""

from __future__ import annotations

import dataclasses
import pathlib
import tomllib
from collections.abc import Sequence
from typing import Any

from icedcoffee_sync.apply.model import DesiredAccount

from runningtab.period import PeriodError

CLEARING_ACCOUNT_NAME = "QBO Import Clearing"

# What a [builtin] entry's `role` may say, and the ERPNext fact on the chart
# account it names that must then be true -- the cross-check
# `validate_qbo_map` runs so that an entry naming the wrong QBO built-in (the
# two swapped, say) is refused instead of quietly posting balances to the
# wrong QuickBooks account.
BUILTIN_ROLES = frozenset({"receivable", "payable", "retained_earnings"})


@dataclasses.dataclass(frozen=True)
class BuiltinAccount:
    """A chart account QBO already carries by default: its QBO name, and which of the three builtins it is."""

    name: str
    role: str  # one of BUILTIN_ROLES


@dataclasses.dataclass(frozen=True)
class QboMap:
    types: dict[str, frozenset[str]]
    accounts: dict[str, tuple[str, str]]
    builtin: dict[str, BuiltinAccount]
    clearing: tuple[str, str]


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise PeriodError(f"qbo.toml has no [{key}] table")
    return value


def _expect_list_of_str(value: Any, where: str, problems: list[str]) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    problems.append(f"{where}: expected a list of strings, got {value!r}")
    return None


def _expect_str(value: Any, where: str, problems: list[str]) -> str:
    """Return `value` if it is a string, else record the problem and return a placeholder.

    The placeholder is never read: every caller only uses the return value
    after `parse_qbo_map` has raised on a non-empty `problems`.
    """
    if isinstance(value, str):
        return value
    problems.append(f"{where}: expected a string, got {value!r}")
    return ""


def parse_qbo_map(text: str) -> QboMap:
    """Parse `qbo.toml`, or raise `PeriodError` naming every malformed entry at once.

    A hand edit that gets a shape wrong -- a bare string where `[Type, Detail
    Type]` belongs, a one-element list, a number -- must be refused by name,
    not silently misread or left to crash on a raw Python `IndexError` or
    `TypeError` a person editing the file would never connect to their typo.
    """
    data: dict[str, Any] = tomllib.loads(text)
    problems: list[str] = []

    types: dict[str, frozenset[str]] = {}
    for key, value in _table(data, "types").items():
        detail_types = _expect_list_of_str(value, f"[types] {key!r}", problems)
        if detail_types is not None:
            types[str(key)] = frozenset(detail_types)

    accounts: dict[str, tuple[str, str]] = {}
    for key, value in _table(data, "accounts").items():
        if isinstance(value, list) and len(value) == 2 and all(isinstance(item, str) for item in value):
            accounts[str(key)] = (value[0], value[1])
        else:
            problems.append(f"[accounts] {key!r}: expected [Type, Detail Type], got {value!r}")

    builtin: dict[str, BuiltinAccount] = {}
    for key, value in _table(data, "builtin").items():
        if not isinstance(value, dict):
            problems.append(f"[builtin] {key!r}: expected {{name = ..., role = ...}}, got {value!r}")
            continue
        name = _expect_str(value.get("name"), f"[builtin] {key!r} 'name'", problems)
        role = _expect_str(value.get("role"), f"[builtin] {key!r} 'role'", problems)
        if role and role not in BUILTIN_ROLES:
            problems.append(f"[builtin] {key!r} 'role': {role!r} is not one of {sorted(BUILTIN_ROLES)}")
        builtin[str(key)] = BuiltinAccount(name, role)

    clearing_raw = _table(data, "clearing")
    clearing_type = _expect_str(clearing_raw.get("type"), "[clearing] 'type'", problems)
    clearing_detail = _expect_str(clearing_raw.get("detail"), "[clearing] 'detail'", problems)

    if problems:
        raise PeriodError("qbo.toml is malformed:\n  " + "\n  ".join(problems))

    return QboMap(types, accounts, builtin, (clearing_type, clearing_detail))


def load_qbo_map(path: pathlib.Path) -> QboMap:
    return parse_qbo_map(path.read_text(encoding="utf-8"))


_ROLE_MATCHES: dict[str, Any] = {
    "receivable": lambda a: a.account_type == "Receivable",
    "payable": lambda a: a.account_type == "Payable",
    "retained_earnings": lambda a: a.root_type == "Equity",
}


def _builtin_role_problems(qmap: QboMap, by_number: dict[str, DesiredAccount]) -> list[str]:
    """A problem per [builtin] entry whose chart account isn't actually its `role`'s ERPNext type.

    `role` names which of QBO's three built-in accounts a chart account
    stands in for; independently of what its `name` says, the account must
    actually be that role's type -- otherwise the two could have been
    swapped (Receivable given Payable's role, say) with nothing else in this
    file catching it, and balances would post to the wrong QuickBooks
    account despite `check_replay` passing (it only checks the kit
    reproduces itself, not that qbo.toml named the right accounts to begin
    with).
    """
    problems: list[str] = []
    for number, entry in sorted(qmap.builtin.items()):
        account = by_number.get(number)
        matches = _ROLE_MATCHES.get(entry.role)
        if account is None or matches is None:
            continue  # already reported by the "not in chart.coa" or role-enum check
        if not matches(account):
            actual = account.account_type or account.root_type
            problems.append(f"{number} ({actual!r}): [builtin] role {entry.role!r} does not match")
    return problems


def validate_qbo_map(qmap: QboMap, chart: Sequence[DesiredAccount]) -> None:
    """Refuse, naming every problem, a map the import kit could not be built from."""
    by_number = {a.number: a for a in chart}
    postable = {a.number for a in chart if not a.is_group}
    problems: list[str] = []
    for number in sorted(postable - qmap.accounts.keys() - qmap.builtin.keys()):
        problems.append(f"{number} {by_number[number].name}: no entry in [accounts] or [builtin]")
    for number in sorted(qmap.accounts.keys() & qmap.builtin.keys()):
        problems.append(f"{number}: in both [accounts] and [builtin]")
    for number in sorted((qmap.accounts.keys() | qmap.builtin.keys()) - postable):
        problems.append(f"{number}: not a postable account in chart.coa")
    for number, (qtype, detail) in sorted([*qmap.accounts.items(), ("clearing", qmap.clearing)]):
        if detail not in qmap.types.get(qtype, frozenset()):
            problems.append(f"{number}: {qtype!r} / {detail!r} is not a Type and Detail Type listed in [types]")
    problems.extend(_builtin_role_problems(qmap, by_number))
    names = (
        [by_number[n].name for n in qmap.accounts if n in by_number]
        + [entry.name for entry in qmap.builtin.values()]
        + [CLEARING_ACCOUNT_NAME]
    )
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"{name!r}: more than one account would import under this name")
    if problems:
        raise PeriodError("qbo.toml cannot build the import kit:\n  " + "\n  ".join(problems))
