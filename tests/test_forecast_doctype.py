"""The Cash Forecast doctype: its install, its repair, and the document written into it."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from decimal import Decimal

import httpx2
import pytest
from icedcoffee_sync.frappe.client import Client

from runningtab.forecast.compute import Forecast, compute
from runningtab.forecast.doctype import (
    CASH_FORECAST,
    LINE_TABLE,
    MEMBER_TABLE,
    MONTH_TABLE,
    doctype_documents,
    forecast_document,
    install_doctypes,
    write_forecast,
)
from runningtab.mercury.posting import Outcome
from tests.test_forecast_compute import MEMBERS, NOW, A, B, forecast_file
from tests.test_mercury_posting import SITE, _Site


class _DocTypeSite(_Site):
    """The fake site, naming a DocType the way Frappe does: by its `name` field."""

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and request.url.path.endswith("/document/DocType"):
            self.requests.append(request)
            body = json.loads(request.content)
            self.documents[("DocType", body["name"])] = {**body, "docstatus": 0, "modified_by": self.user}
            return httpx2.Response(200, json={"data": {"name": body["name"], **body}})
        return super().handler(request)


@pytest.fixture
def site() -> _DocTypeSite:
    return _DocTypeSite()


@pytest.fixture
def admin(site: _DocTypeSite):
    with Client(SITE, transport=httpx2.MockTransport(site.handler)) as client:
        yield client


def link(number: str) -> str:
    return f"{number} - Account {number} - T"


def a_forecast() -> Forecast:
    return compute(
        forecast_file(A, B),
        now=NOW,
        opening_cash=Decimal("300"),
        ledger_through=dt.date(2026, 10, 25),
        transactions=(),
    )


def methods(site: _DocTypeSite) -> list[tuple[str, str]]:
    return [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in site.requests if r.method in ("POST", "PUT")]


def test_the_parent_is_a_plain_custom_record_not_a_submittable_one() -> None:
    docs = {d["name"]: d for d in doctype_documents()}
    parent = docs[CASH_FORECAST]
    assert parent["is_submittable"] == 0
    assert parent["autoname"] == "CF-.#####"
    assert parent["track_changes"] == 0
    assert all(d["custom"] == 1 and d["module"] == "Accounts" for d in docs.values())
    assert [docs[t]["istable"] for t in (MONTH_TABLE, MEMBER_TABLE, LINE_TABLE)] == [1, 1, 1]
    roles = {p["role"]: p for p in parent["permissions"]}
    assert roles["Accounts Manager"]["read"] == 1 and not roles["Accounts Manager"].get("write")
    assert not any(p.get("submit") or p.get("cancel") or p.get("amend") for p in parent["permissions"])


def test_child_tables_are_installed_before_the_parent(admin, site: _DocTypeSite) -> None:
    results = install_doctypes(admin)
    assert [outcome for _, outcome in results] == [Outcome.CREATED] * 4
    assert [name for _, name in methods(site)] == ["DocType"] * 4
    inserted = [name for (_, name) in site.documents]
    assert inserted[-1] == CASH_FORECAST
    assert set(inserted[:-1]) == {MONTH_TABLE, MEMBER_TABLE, LINE_TABLE}


def test_reinstalling_an_unchanged_doctype_writes_nothing(admin, site: _DocTypeSite) -> None:
    install_doctypes(admin)
    before = len(methods(site))
    results = install_doctypes(admin)
    assert [outcome for _, outcome in results] == [Outcome.UNCHANGED] * 4
    assert len(methods(site)) == before


def test_a_field_with_the_wrong_type_is_repaired(admin, site: _DocTypeSite) -> None:
    install_doctypes(admin)
    stored = site.documents[("DocType", CASH_FORECAST)]
    stored["fields"] = [{**f, "fieldtype": "Data"} if f["fieldname"] == "total_call" else f for f in stored["fields"]]
    results = dict(install_doctypes(admin))
    assert results[CASH_FORECAST] is Outcome.UPDATED
    assert results[MONTH_TABLE] is Outcome.UNCHANGED
    assert {f["fieldname"]: f["fieldtype"] for f in stored["fields"]}["total_call"] == "Currency"
    assert [m for m, _ in methods(site)].count("PUT") == 1


def test_a_missing_field_is_repaired(admin, site: _DocTypeSite) -> None:
    install_doctypes(admin)
    stored = site.documents[("DocType", LINE_TABLE)]
    stored["fields"] = stored["fields"][:-1]
    assert dict(install_doctypes(admin))[LINE_TABLE] is Outcome.UPDATED


def test_the_document_carries_every_month_member_and_line() -> None:
    forecast = a_forecast()
    doc = forecast_document(forecast, link=link)
    assert doc["doctype"] == CASH_FORECAST
    assert doc["total_call"] == float(forecast.total_call)
    assert doc["opening_cash"] == 300.0
    assert doc["floor_from"] == forecast.floor_from
    assert doc["scenario"] == forecast.scenario
    assert doc["ledger_through"] == "2026-10-25"
    assert doc["computed_at"] == "2026-10-26T09:00:00+00:00"
    assert (doc["window_start"], doc["window_end"]) == (
        forecast.window_start.isoformat(),
        forecast.window_end.isoformat(),
    )
    assert doc["floor"] == float(forecast.floor)
    for sent, month in zip(doc["months"], forecast.months, strict=True):
        assert (sent["opening"], sent["outflows"], sent["inflows"]) == (
            float(month.opening),
            float(month.outflows),
            float(month.inflows),
        )
    assert [m["month"] for m in doc["months"]] == [m.month.isoformat() for m in forecast.months]
    assert [m["closing"] for m in doc["months"]] == [float(m.closing) for m in forecast.months]
    assert [(m["account"], m["amount"]) for m in doc["members"]] == [
        (link(number), float(amount)) for number, amount in forecast.members
    ]
    assert len(doc["members"]) == len(MEMBERS)
    assert len(doc["lines"]) == len(forecast.dues)
    first, due = doc["lines"][0], forecast.dues[0]
    assert (first["line"], first["account"], first["status"]) == (due.line.name, link(due.line.account), due.status)
    assert first["due"] == due.due.isoformat()
    assert first["month"] == due.due.replace(day=1).isoformat()


def test_applying_twice_writes_two_documents_and_edits_neither(admin, site: _DocTypeSite) -> None:
    forecast = a_forecast()
    first = write_forecast(admin, forecast, link=link)
    second = write_forecast(admin, forecast, link=link)
    assert first != second
    assert [m for m, _ in methods(site)] == ["POST", "POST"]
    assert len([k for k in site.documents if k[0] == CASH_FORECAST]) == 2


def test_every_amount_survives_the_wire_exactly() -> None:
    forecast = a_forecast()
    doc = forecast_document(forecast, link=link)
    sent = [
        (doc["opening_cash"], forecast.opening_cash),
        (doc["floor"], forecast.floor),
        (doc["total_call"], forecast.total_call),
    ]
    for row, month in zip(doc["months"], forecast.months, strict=True):
        sent += [
            (row["opening"], month.opening),
            (row["outflows"], month.outflows),
            (row["inflows"], month.inflows),
            (row["closing"], month.closing),
        ]
    sent += [(row["amount"], amount) for row, (_, amount) in zip(doc["members"], forecast.members, strict=True)]
    sent += [(row["amount"], due.amount) for row, due in zip(doc["lines"], forecast.dues, strict=True)]
    assert all(Decimal(str(wire)) == original for wire, original in sent)


def test_the_computed_time_keeps_its_offset() -> None:
    forecast = a_forecast()
    eastern = dt.timezone(dt.timedelta(hours=-4))
    aware = dataclasses.replace(forecast, computed_at=dt.datetime(2026, 10, 26, 5, 0, 7, tzinfo=eastern))
    sent = forecast_document(aware, link=link)["computed_at"]
    assert sent == "2026-10-26T05:00:07-04:00"
    assert dt.datetime.fromisoformat(sent) == aware.computed_at


def test_a_naive_computed_time_is_refused() -> None:
    naive = dataclasses.replace(a_forecast(), computed_at=dt.datetime(2026, 10, 26, 9, 0))
    with pytest.raises(ValueError, match="computed_at"):
        forecast_document(naive, link=link)


def test_a_different_field_order_is_not_a_difference(admin, site: _DocTypeSite) -> None:
    install_doctypes(admin)
    stored = site.documents[("DocType", CASH_FORECAST)]
    stored["fields"] = list(reversed(stored["fields"]))
    assert [o for _, o in install_doctypes(admin)] == [Outcome.UNCHANGED] * 4
