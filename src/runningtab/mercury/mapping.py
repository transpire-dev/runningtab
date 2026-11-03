"""What each Mercury account is in our books.

Checked in beside the chart, as `mercury.toml`, because it is our data and not
the client's. Two tables:

- `[accounts]` -- a Mercury account, by the id or the name Mercury reports for
  it, to the chart account holding its balance. Read by
  `runningtab mercury install-bank-accounts`, which links each Bank Account to it.
- `[bank_accounts]` -- a Mercury account to the ERPNext Bank Account document
  its Bank Transactions attach to. Keyed the same way `[accounts]` is.

There is no category table. Every settled transaction becomes a Bank
Transaction on its own account, and the person reconciling it picks the
account; Mercury's GL code rides along as a hint. A file that still carries
`[categories]` or `[posting]` is refused rather than read past, because a table
that reads as configured while doing nothing is exactly the silence a person
editing it cannot see.

Account entries may be keyed on the name Mercury reports as well as the id, and
the id wins when both are present. Names stood in while the real ids were
unknown; the ids are the durable key.
"""

from __future__ import annotations

import dataclasses
import pathlib
import tomllib


class MappingError(ValueError):
    """`mercury.toml` is not a mapping we can post with."""


def _normalise(name: str) -> str:
    """Account names are compared case-insensitively with runs of whitespace collapsed."""
    return " ".join(name.split()).casefold()


def _by_id_or_name(table: dict[str, str], account_id: str, account_name: str | None) -> str | None:
    """A Mercury account's entry in a table keyed on either its id or its name.

    The id is looked up first, so filling real ids in later takes precedence
    over the names standing in for them today.
    """
    by_id = table.get(account_id)
    if by_id is not None:
        return by_id
    if account_name:
        return table.get(_normalise(account_name))
    return None


@dataclasses.dataclass(frozen=True)
class Mapping:
    """What each Mercury account means in our chart and in ERPNext."""

    accounts: dict[str, str]
    bank_accounts: dict[str, str] = dataclasses.field(default_factory=dict)

    def account_for_mercury_account(self, account_id: str, account_name: str | None = None) -> str | None:
        """The chart account holding a Mercury account's balance."""
        return _by_id_or_name(self.accounts, account_id, account_name)

    def bank_account_for(self, account_id: str, account_name: str | None = None) -> str | None:
        """The ERPNext Bank Account document a Mercury account's activity attaches to.

        One per Mercury account, never one for all of them: the ingest reads
        checking, savings and the card, and a Bank Transaction posted against
        another account's Bank Account is both wrong in the ledger and invisible
        to the reconciliation the doctype exists for.
        """
        return _by_id_or_name(self.bank_accounts, account_id, account_name)

    def knows_account(self, account_id: str, account_name: str | None = None) -> bool:
        """Whether this Mercury account has a chart account. An unknown one is never guessed at."""
        return self.account_for_mercury_account(account_id, account_name) is not None


def _table(data: dict[str, object], key: str) -> dict[str, str]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise MappingError(f"[{key}] must be a table of string values")
    out: dict[str, str] = {}
    for name, number in value.items():
        if not isinstance(number, str):
            raise MappingError(f"[{key}] {name!r} must map to an account number as a string, got {number!r}")
        out[str(name)] = number
    return out


def _account_keys(table: dict[str, str]) -> dict[str, str]:
    """A Mercury-account-keyed table, readable by both id and normalised name.

    Keys are left as written as well as normalised, because one of them is a
    Mercury account id -- opaque, and it must match exactly.
    """
    return {**table, **{_normalise(name): value for name, value in table.items()}}


def parse_mapping(text: str) -> Mapping:
    """Parse `mercury.toml` text. Account numbers are strings, matching the chart."""
    data = tomllib.loads(text)
    if "categories" in data:
        raise MappingError(
            "[categories] is no longer read: every settled transaction becomes a Bank Transaction on its "
            "own account, with Mercury's GL code carried as a hint, and the person reconciling picks the "
            "account; remove the table"
        )
    if "posting" in data:
        raise MappingError("[posting] is no longer read: nothing posts to suspense; remove the table")
    return Mapping(
        accounts=_account_keys(_table(data, "accounts")),
        bank_accounts=_account_keys(_table(data, "bank_accounts")),
    )


def load_mapping(path: pathlib.Path) -> Mapping:
    """Load the mapping at `path`: the books directory's `mercury.toml`, or a `--mercury-mapping` file."""
    return parse_mapping(path.read_text())


def validate_against_chart(mapping: Mapping, chart_numbers: set[str]) -> list[str]:
    """Every account number the mapping names that the chart does not have.

    Cheap and worth doing at load: a typo'd account number would otherwise
    surface as an ERPNext link error when the Bank Account is installed, naming
    a document rather than the line of `mercury.toml` that is wrong.
    """
    return sorted(set(mapping.accounts.values()) - chart_numbers)
