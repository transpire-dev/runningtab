"""The Cash Forecast doctype, and writing a computed forecast into it.

Each run writes a new document and never edits an earlier one: the capital call
is money people wired against, so the record of what a past run said must not
change underneath them. A custom doctype (`custom: 1`) needs no developer mode
on the site, which is why every definition here carries it.

Installing needs an administrator, for the same reason `install_custom_fields`
does: the books bot may write forecasts, not reshape doctypes.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from icedcoffee_sync.frappe.errors import DoesNotExistError, PermissionError_

from runningtab.forecast.compute import Forecast
from runningtab.forecast.names import CASH_FORECAST
from runningtab.mercury.posting import Ledger, Outcome

MONTH_TABLE = "Cash Forecast Month"
MEMBER_TABLE = "Cash Forecast Member"
LINE_TABLE = "Cash Forecast Line"

_DOCTYPE = "DocType"
_STATUS_OPTIONS = "owed\nseen paid"


def _field(fieldname: str, fieldtype: str, options: str | None = None, *, in_list: bool = False) -> dict[str, Any]:
    field: dict[str, Any] = {
        "fieldname": fieldname,
        "label": fieldname.replace("_", " ").title(),
        "fieldtype": fieldtype,
        "in_list_view": int(in_list),
    }
    if options is not None:
        field["options"] = options
    return field


def _table(name: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "doctype": _DOCTYPE,
        "name": name,
        "module": "Accounts",
        "custom": 1,
        "istable": 1,
        "fields": fields,
    }


def doctype_documents() -> list[dict[str, Any]]:
    """The four DocType definitions, child tables first so the parent can name them."""
    month = _table(
        MONTH_TABLE,
        [
            _field("month", "Date", in_list=True),
            _field("opening", "Currency", in_list=True),
            _field("outflows", "Currency", in_list=True),
            _field("inflows", "Currency", in_list=True),
            _field("closing", "Currency", in_list=True),
        ],
    )
    member = _table(
        MEMBER_TABLE,
        [
            _field("account", "Link", "Account", in_list=True),
            _field("amount", "Currency", in_list=True),
        ],
    )
    line = _table(
        LINE_TABLE,
        [
            _field("month", "Date", in_list=True),
            _field("due", "Date", in_list=True),
            _field("line", "Data", in_list=True),
            _field("account", "Link", "Account"),
            _field("amount", "Currency", in_list=True),
            _field("status", "Select", _STATUS_OPTIONS, in_list=True),
        ],
    )
    parent = {
        "doctype": _DOCTYPE,
        "name": CASH_FORECAST,
        "module": "Accounts",
        "custom": 1,
        "istable": 0,
        "is_submittable": 0,
        "track_changes": 0,
        "autoname": "CF-.#####",
        "sort_field": "creation",
        "sort_order": "DESC",
        "fields": [
            # Data, not Datetime: Frappe reads a Datetime as site-local and would
            # silently shift an instant that carries its own UTC offset.
            _field("computed_at", "Data", in_list=True),
            _field("ledger_through", "Date"),
            _field("window_start", "Date"),
            _field("window_end", "Date"),
            _field("scenario", "Data"),
            _field("opening_cash", "Currency"),
            _field("floor", "Currency"),
            _field("floor_from", "Data"),
            _field("total_call", "Currency", in_list=True),
            _field("months", "Table", MONTH_TABLE),
            _field("members", "Table", MEMBER_TABLE),
            _field("lines", "Table", LINE_TABLE),
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1, "export": 1},
            {"role": "Accounts Manager", "read": 1},
        ],
    }
    return [month, member, line, parent]


def _shape(fields: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """What makes a field the right field, by name: Frappe fills the rest in itself.

    Keyed by fieldname so a different field order from Frappe is not a
    difference.
    """
    return {str(f["fieldname"]): (str(f["fieldtype"]), str(f.get("options") or "")) for f in fields}


def install_doctypes(admin: Ledger) -> list[tuple[str, Outcome]]:
    """Create each doctype if absent; replace its field list if it differs.

    Repair covers fields only. It does not touch permissions or `is_submittable`
    on an existing doctype.

    A doctype that exists with a field of the wrong type would accept the
    forecast write and then store the wrong thing, so presence is not enough.
    Repair replaces the whole field list with the desired one, so a field added
    by hand to the doctype is dropped from the definition (its column stays).
    It never deletes the doctype, which would take every stored forecast with
    it.
    """
    results: list[tuple[str, Outcome]] = []
    for document in doctype_documents():
        name = document["name"]
        body = {k: v for k, v in document.items() if k != "doctype"}
        if not admin.list(_DOCTYPE, fields=["name"], filters=[["name", "=", name]], limit=1):
            admin.insert(_DOCTYPE, body)
            results.append((name, Outcome.CREATED))
            continue
        existing = admin.get(_DOCTYPE, name)
        if _shape(existing.get("fields") or []) == _shape(body["fields"]):
            results.append((name, Outcome.UNCHANGED))
            continue
        admin.update(_DOCTYPE, name, {"fields": body["fields"]})
        results.append((name, Outcome.UPDATED))
    return results


def is_installed(ledger: Ledger) -> bool:
    """Whether the site has the Cash Forecast doctype, asked with the caller's own credential.

    Listing the doctype needs no permission on `DocType` itself, which the bot
    lacks. A site without the doctype answers DoesNotExist; a credential not
    yet granted read answers PermissionError, which only a site that has the
    doctype can say.
    """
    try:
        ledger.list(CASH_FORECAST, fields=["name"], limit=1)
    except DoesNotExistError:
        return False
    except PermissionError_:
        return True
    return True


def _money(value: Decimal) -> float:
    """A currency amount as JSON, converted once after all arithmetic is done in `Decimal`."""
    return float(value)


def forecast_document(forecast: Forecast, *, link: Callable[[str], str]) -> dict[str, Any]:
    """The Cash Forecast document for a computed forecast.

    `link` maps a chart account number to the company-suffixed name ERPNext
    links by.
    """
    if forecast.computed_at.utcoffset() is None:
        raise ValueError("computed_at must carry a time zone; a naive time is ambiguous once stored")
    return {
        "doctype": CASH_FORECAST,
        "computed_at": forecast.computed_at.isoformat(timespec="seconds"),
        "ledger_through": forecast.ledger_through.isoformat(),
        "window_start": forecast.window_start.isoformat(),
        "window_end": forecast.window_end.isoformat(),
        "scenario": forecast.scenario,
        "opening_cash": _money(forecast.opening_cash),
        "floor": _money(forecast.floor),
        "floor_from": forecast.floor_from,
        "total_call": _money(forecast.total_call),
        "months": [
            {
                "month": m.month.isoformat(),
                "opening": _money(m.opening),
                "outflows": _money(m.outflows),
                "inflows": _money(m.inflows),
                "closing": _money(m.closing),
            }
            for m in forecast.months
        ],
        "members": [{"account": link(number), "amount": _money(amount)} for number, amount in forecast.members],
        "lines": [
            {
                "month": d.due.replace(day=1).isoformat(),
                "due": d.due.isoformat(),
                "line": d.line.name,
                "account": link(d.line.account),
                "amount": _money(d.amount),
                "status": d.status,
            }
            for d in forecast.dues
        ],
    }


def write_forecast(ledger: Ledger, forecast: Forecast, *, link: Callable[[str], str]) -> str:
    """Insert the forecast as a new document and return its name."""
    created = ledger.insert(CASH_FORECAST, forecast_document(forecast, link=link))
    return str(created["name"])
