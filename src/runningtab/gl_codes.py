"""The GL code list Mercury shows on every transaction: our postable accounts, as a CSV.

The code a person picks in the bank rides along on the Bank Transaction as a
hint for whoever reconciles it, so the vocabulary they pick from has to be the
ledger's. There is no upload API; a person uploads it.
"""

from __future__ import annotations

import csv
import io
import re

from icedcoffee_sync.apply.model import DesiredAccount

_THOUSANDS_SEPARATOR = re.compile(r"(?<=\d),(?=\d)")
_LIST_SEPARATOR = re.compile(r",\s*")


def postable_accounts(chart: list[DesiredAccount]) -> list[DesiredAccount]:
    """Accounts a transaction can actually post to. Groups cannot."""
    return [a for a in chart if not a.is_group]


def gl_code_name(account: DesiredAccount) -> str:
    """One entry of the list: `{number} - {name}` with every comma spent.

    Mercury's GL-code importer splits each line at the first comma no matter how
    the CSV quotes it, so a label carrying one reaches the bank truncated and
    every transaction coded with it names an account that does not exist. A
    comma between two digits is a thousands separator and simply goes; any other
    comma separates list items and becomes ` / `. The ERPNext account name is
    left alone, so a label can differ from the account it stands for.
    """
    name = _LIST_SEPARATOR.sub(" / ", _THOUSANDS_SEPARATOR.sub("", account.name))
    return f"{account.number} - {name}"


def gl_code_csv(chart: list[DesiredAccount]) -> str:
    """A single column of `gl_code_name`, no header, and no comma or quote on any line.

    Quoting would be the RFC 4180 answer to a comma in a field, and Mercury's
    importer does not honour it — so the labels carry no commas to quote and no
    line here is ever quoted.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for account in postable_accounts(chart):
        writer.writerow([gl_code_name(account)])
    return out.getvalue()
