"""The books directory: whose books runningtab is keeping, and everything it reads about them.

runningtab is the tool; the books are data it is pointed at. A books
directory holds the chart (`chart.coa`), the Mercury account map
(`mercury.toml`), the forecast (`forecast.toml`), the QuickBooks import map
(`qbo.toml`), and `books.toml`, which says where the ledger is and which
identities and secrets reach it.

There is no default directory and no fallback to one inside the package:
`--books DIR` or `RUNNINGTAB_BOOKS` names it, or the command refuses. A
silent fallback would plan against books nobody meant, and every difference
it reported would look real.

`books.toml` is read strictly. An unknown key is refused by its dotted name,
and so is a missing one, because a typo that quietly became a default is the
kind of mistake nobody notices until the ledger does.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import tomllib
from collections.abc import Mapping
from typing import Any

from runningtab.access import Policy, bot_policy, reader_policy
from runningtab.site import Site, Target, normalised_host

BOOKS_ENV = "RUNNINGTAB_BOOKS"
BOOKS_FILE = "books.toml"
CHART_FILE = "chart.coa"
MERCURY_FILE = "mercury.toml"
FORECAST_FILE = "forecast.toml"
QBO_FILE = "qbo.toml"
_DATA_FILES = (CHART_FILE, MERCURY_FILE, FORECAST_FILE, QBO_FILE)

# One path segment: the name namespaces the Mercury ingest's local state.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Every key `books.toml` may hold, and its type. All of them are required.
_STR = "a string"
_STR_LIST = "a list of strings"
_SCHEMA: dict[str, Any] = {
    "name": _STR,
    "chart_doc": _STR,
    "site": {"host": _STR, "test_hosts": _STR_LIST},
    "secrets": {
        "project": _STR,
        "books": _STR,
        "books_readonly": _STR,
        "mercury": _STR,
        "test_admin": _STR,
    },
    "record_buckets": {"books": _STR, "test": _STR},
    "bot": {"role": _STR, "user": _STR, "first_name": _STR},
    "reader": {"role": _STR, "user": _STR, "first_name": _STR},
    "fence": {"probe_role": _STR},
}


class BooksError(ValueError):
    """No books directory was named, or the one named is not a set of books runningtab can read."""


@dataclasses.dataclass(frozen=True)
class Books:
    """One loaded books directory."""

    directory: pathlib.Path
    # Names the books in what runningtab writes for a person (the period
    # package's manifest) and namespaces its local state.
    name: str
    # `docs/chart-of-accounts.md`-style page whose account tables
    # `runningtab chart render-doc` regenerates from the chart.
    chart_doc: pathlib.Path
    site: Site
    # The record buckets a closed quarter's package is written to, one per
    # target, so a caller never spells a bucket name itself.
    record_buckets: Mapping[Target, str]
    bot: Policy
    reader: Policy
    # The role `runningtab.period.fence` writes as Company's bypass role while
    # proving the fence holds. Held by nobody; see that module.
    fence_probe_role: str

    @property
    def chart_path(self) -> pathlib.Path:
        """The chart of accounts."""
        return self.directory / CHART_FILE

    @property
    def mercury_path(self) -> pathlib.Path:
        """Which chart account and Bank Account each Mercury account is."""
        return self.directory / MERCURY_FILE

    @property
    def forecast_path(self) -> pathlib.Path:
        """The expected cash movements `runningtab forecast` reads."""
        return self.directory / FORECAST_FILE

    @property
    def qbo_path(self) -> pathlib.Path:
        """What QuickBooks Online needs to know about the chart."""
        return self.directory / QBO_FILE


def resolve_books(flag: pathlib.Path | None, environ: Mapping[str, str] = os.environ) -> Books:
    """Load the books directory `--books` names, else the one `RUNNINGTAB_BOOKS` names, else refuse.

    Raises:
        BooksError: Neither names a directory, or the one named does not load.
    """
    if flag is not None:
        return load_books(flag)
    named = environ.get(BOOKS_ENV, "")
    if not named:
        raise BooksError(
            f"no books directory named: pass --books DIR or set {BOOKS_ENV}. "
            "runningtab has no default books, so that it never plans against books nobody meant."
        )
    return load_books(pathlib.Path(named))


def load_books(directory: pathlib.Path) -> Books:
    """Read `directory/books.toml` and check the directory holds every file runningtab reads.

    Raises:
        BooksError: Naming the file, key or host that is wrong.
    """
    directory = directory.resolve()
    books_file = directory / BOOKS_FILE
    try:
        text = books_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise BooksError(f"{books_file}: cannot read the books directory's {BOOKS_FILE}: {exc}") from exc
    missing = [name for name in _DATA_FILES if not (directory / name).is_file()]
    if missing:
        raise BooksError(f"books directory {directory} is missing: {', '.join(missing)}")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BooksError(f"{books_file} is not valid TOML: {exc}") from exc
    problems = _schema_problems(data, _SCHEMA, prefix="")
    if problems:
        raise BooksError(f"{books_file}:\n  " + "\n  ".join(problems))
    return _books(directory, books_file, data)


def _schema_problems(data: Mapping[str, Any], schema: Mapping[str, Any], *, prefix: str) -> list[str]:
    """Every unknown, missing or wrongly typed key under `prefix`, by its dotted name."""
    problems: list[str] = []
    for key in sorted(data.keys() - schema.keys()):
        problems.append(f"{prefix}{key}: unknown key")
    for key, expected in schema.items():
        where = f"{prefix}{key}"
        if key not in data:
            problems.append(f"{where}: missing")
            continue
        value = data[key]
        if isinstance(expected, dict):
            if isinstance(value, dict):
                problems.extend(_schema_problems(value, expected, prefix=f"{where}."))
            else:
                problems.append(f"{where}: must be a table")
        elif expected == _STR and not (isinstance(value, str) and value.strip()):
            problems.append(f"{where}: must be {_STR}, not empty")
        elif expected == _STR_LIST and not (
            isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
        ):
            problems.append(f"{where}: must be {_STR_LIST}")
    return problems


def _books(directory: pathlib.Path, books_file: pathlib.Path, data: Mapping[str, Any]) -> Books:
    """Build `Books` from data `_schema_problems` already passed, refusing what only makes sense together."""
    name = data["name"]
    if not _NAME.match(name):
        raise BooksError(f"{books_file}: name {name!r} must be letters, digits, '-' and '_' only; it names a directory")
    site_data, secrets = data["site"], data["secrets"]
    site = Site(
        host=site_data["host"],
        test_hosts=frozenset(site_data["test_hosts"]),
        secret_project=secrets["project"],
        books_secret=secrets["books"],
        books_readonly_secret=secrets["books_readonly"],
        mercury_secret=secrets["mercury"],
        test_admin_secret=secrets["test_admin"],
    )
    # The allowlist exists so that an alias of the ledger is refused. A list
    # that names the ledger itself would let `--target test` reach it.
    ledger = normalised_host(site.host)
    if any(normalised_host(host) == ledger for host in site.test_hosts):
        raise BooksError(
            f"{books_file}: site.test_hosts names the books host {site.host}; "
            "the test target must never be able to reach the ledger"
        )
    bot, reader = data["bot"], data["reader"]
    if bot["user"] == reader["user"] or bot["role"] == reader["role"]:
        raise BooksError(
            f"{books_file}: bot and reader must be different users with different roles; "
            "the reader's whole point is a credential that cannot write"
        )
    # `runningtab.period.fence` writes this role as the one allowed past a
    # closed quarter, so it must be held by nobody. Frappe gives every user
    # "Guest" and "All", and the bot and reader hold their own roles.
    probe_role = data["fence"]["probe_role"]
    if probe_role in {"Guest", "All", bot["role"], reader["role"]}:
        raise BooksError(
            f"{books_file}: fence.probe_role {probe_role!r} is a role somebody holds; "
            "it must be one nobody is ever granted"
        )
    buckets = data["record_buckets"]
    return Books(
        directory=directory,
        name=name,
        chart_doc=(directory / data["chart_doc"]).resolve(),
        site=site,
        record_buckets={Target.BOOKS: buckets["books"], Target.TEST: buckets["test"]},
        bot=bot_policy(bot["role"], bot["user"], bot["first_name"]),
        reader=reader_policy(reader["role"], reader["user"], reader["first_name"]),
        fence_probe_role=probe_role,
    )
