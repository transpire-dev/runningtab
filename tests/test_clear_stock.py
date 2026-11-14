"""`coa clear-stock`: retire ERPNext's stock chart from a site whose ledger is empty.

A fresh site's setup wizard creates ERPNext's "Standard" chart, and some of
its accounts collide with ours in ways `coa apply` refuses to resolve. Only
accounts that are recognisably the stock chart -- a name from that template,
no account number, not a root -- are candidates, and nothing is touched while
the company's ledger holds a single entry.
"""

import builtins
from typing import Any

import pytest
from icedcoffee_sync.apply.diff import ChartPlan
from icedcoffee_sync.apply.model import LiveAccount
from icedcoffee_sync.frappe.errors import LinkExistsError
from typer.testing import CliRunner

from runningtab import coa
from runningtab.chart import load_chart, load_defaults
from runningtab.cli import _Context, app
from runningtab.stock_chart import STANDARD_CHART_ACCOUNT_NAMES, STANDARD_CHART_PARENTS
from tests.conftest import EXAMPLE_BOOKS

runner = CliRunner()


def _live(name: str, parent: str | None, *, number: str = "", is_group: bool = False) -> LiveAccount:
    return LiveAccount(
        document_name=f"{number + ' - ' if number else ''}{name} - VT",
        number=number,
        name=name,
        parent_document_name=f"{parent} - VT" if parent else None,
        is_group=is_group,
        root_type="Asset",
    )


ROOT = _live("Application of Funds (Assets)", None, is_group=True)
CURRENT = _live("Current Assets", "Application of Funds (Assets)", is_group=True)
CASH_GROUP = _live("Cash In Hand", "Current Assets", is_group=True)
CASH = _live("Cash", "Cash In Hand")
OURS = _live("Operating Checking", "Current Assets", number="1111")


def test_the_known_names_are_erpnext_standard_chart_without_its_roots() -> None:
    assert {"Cash", "Stock Adjustment", "Stock Received But Not Billed", "Retained Earnings"} <= (
        STANDARD_CHART_ACCOUNT_NAMES
    )
    assert "Application of Funds (Assets)" not in STANDARD_CHART_ACCOUNT_NAMES
    assert len(STANDARD_CHART_ACCOUNT_NAMES) == 92


def test_plan_deletes_stock_accounts_children_first_and_never_a_root() -> None:
    plan = coa.plan_stock_clear([ROOT, CURRENT, CASH_GROUP, CASH])
    assert [a.document_name for a in plan.deletes] == ["Cash - VT", "Cash In Hand - VT", "Current Assets - VT"]
    assert plan.kept == ()


def test_plan_leaves_numbered_and_unknown_accounts_alone() -> None:
    numbered_namesake = _live("Cash", "Current Assets", number="1121")
    unknown = _live("Petty Cash Jar", "Current Assets")
    plan = coa.plan_stock_clear([ROOT, CURRENT, numbered_namesake, unknown])
    assert [a.document_name for a in plan.deletes] == []
    assert [(a.document_name, why) for a, why in plan.kept] == [
        ("Current Assets - VT", "holds accounts outside ERPNext's stock chart")
    ]


def test_plan_keeps_a_stock_group_that_holds_one_of_ours() -> None:
    plan = coa.plan_stock_clear([ROOT, CURRENT, CASH_GROUP, CASH, OURS])
    assert [a.document_name for a in plan.deletes] == ["Cash - VT", "Cash In Hand - VT"]
    assert [a.document_name for a, _ in plan.kept] == ["Current Assets - VT"]


class _Client:
    def __init__(self, *, entries: builtins.list[dict[str, Any]] | None = None, refuse: set[str] | None = None) -> None:
        self.entries = entries or []
        self.refuse = refuse or set()
        self.log: builtins.list[str] = []
        self.company_updates: builtins.list[dict[str, Any]] = []
        self.accounts: set[str] = set()
        self.company_doc: dict[str, Any] = {"name": "VT", "default_cash_account": "Cash - VT"}

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None

    def list(self, doctype: str, **kwargs: Any) -> Any:
        if doctype == "Account":
            (name,) = (value for field, _op, value in kwargs["filters"] if field == "name")
            return [{"name": name}] if name in self.accounts else []
        assert doctype == "GL Entry"
        assert ["company", "=", "VT"] in kwargs["filters"]
        self.log.append("probe ledger")
        return self.entries[: kwargs["limit"]]

    def get(self, doctype: str, name: str) -> Any:
        assert (doctype, name) == ("Company", "VT")
        return self.company_doc

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        self.log.append(f"update {doctype} {sorted(doc)}")
        self.company_updates.append(dict(doc))
        self.company_doc.update(doc)
        return doc

    def delete(self, doctype: str, name: str) -> Any:
        assert doctype == "Account"
        if name in self.refuse:
            raise LinkExistsError(f"{name} is linked")
        self.log.append(f"delete {name}")
        return None


def test_ledger_is_empty_asks_for_one_entry_of_the_company() -> None:
    assert coa.ledger_is_empty(_Client(), "VT")  # ty: ignore[invalid-argument-type]
    assert not coa.ledger_is_empty(_Client(entries=[{"name": "GLE-1"}]), "VT")  # ty: ignore[invalid-argument-type]


def test_clear_points_defaults_away_first_then_deletes_in_plan_order() -> None:
    client = _Client()
    plan = coa.plan_stock_clear([ROOT, CURRENT, CASH_GROUP, CASH])
    outcome = coa.clear_stock_confirmed(client, "VT", plan)  # ty: ignore[invalid-argument-type]
    assert client.log == [
        "probe ledger",
        "update Company ['default_cash_account']",
        "delete Cash - VT",
        "delete Cash In Hand - VT",
        "delete Current Assets - VT",
    ]
    assert outcome.cleared_defaults == ["default_cash_account"]
    assert outcome.deleted == ["Cash - VT", "Cash In Hand - VT", "Current Assets - VT"]
    assert outcome.failed == []


def test_clear_continues_past_a_refusal_and_skips_that_accounts_parents() -> None:
    client = _Client(refuse={"Cash - VT"})
    outcome = coa.clear_stock_confirmed(client, "VT", coa.plan_stock_clear([ROOT, CURRENT, CASH_GROUP, CASH]))  # ty: ignore[invalid-argument-type]
    assert outcome.deleted == []
    assert [name for name, _ in outcome.failed] == ["Cash - VT", "Cash In Hand - VT", "Current Assets - VT"]


def test_clear_refuses_outright_when_the_ledger_has_entries() -> None:
    client = _Client(entries=[{"name": "GLE-1"}])
    with pytest.raises(coa.LedgerNotEmptyError, match="VT"):
        coa.clear_stock_confirmed(client, "VT", coa.plan_stock_clear([ROOT, CURRENT, CASH_GROUP, CASH]))  # ty: ignore[invalid-argument-type]
    assert [line for line in client.log if line != "probe ledger"] == []


def _patch_context(
    monkeypatch, client: _Client, live: list[LiveAccount], then: list[LiveAccount] | None = None
) -> None:
    """Patch planning to read `live`, and `then` (default: `live` again) on the re-read after the prompt."""
    chart = load_chart(EXAMPLE_BOOKS.chart_path)

    def context_for(accounts: list[LiveAccount]) -> _Context:
        return _Context(
            client=client,  # ty: ignore[invalid-argument-type]
            company="VT",
            abbr="VT",
            chart=chart,
            by_number={a.number: a for a in chart},
            plan=ChartPlan(creates=[], renames=[], retypes=[], extras=[], refusals=[]),
            live=accounts,
            with_entries=set(),
            defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
        )

    reads = iter([context_for(live), context_for(live if then is None else then)])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: next(reads))


STOCK = [ROOT, CURRENT, CASH_GROUP, CASH]


def test_cli_refuses_a_site_whose_ledger_has_entries(monkeypatch) -> None:
    client = _Client(entries=[{"name": "GLE-1"}])
    _patch_context(monkeypatch, client, STOCK)
    result = runner.invoke(app, ["coa", "clear-stock", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert "refusing to clear: VT has entries in its ledger" in result.output
    assert [line for line in client.log if line.startswith("delete")] == []


def test_cli_declined_at_the_prompt_deletes_nothing(monkeypatch) -> None:
    client = _Client()
    _patch_context(monkeypatch, client, STOCK)
    result = runner.invoke(app, ["coa", "clear-stock", "--target", "test"], input="n\n")
    assert result.exit_code == 1
    assert "  - Cash - VT" in result.output
    assert [line for line in client.log if line.startswith("delete")] == []


def test_cli_deletes_with_yes_and_reports(monkeypatch) -> None:
    client = _Client()
    _patch_context(monkeypatch, client, STOCK)
    result = runner.invoke(app, ["coa", "clear-stock", "--target", "test", "--yes"])
    assert result.exit_code == 0, result.output
    assert "company defaults cleared: default_cash_account" in result.output
    assert "deleted 3, failed 0" in result.output


def test_cli_exits_1_when_erpnext_refuses_an_account(monkeypatch) -> None:
    client = _Client(refuse={"Cash - VT"})
    _patch_context(monkeypatch, client, STOCK)
    result = runner.invoke(app, ["coa", "clear-stock", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert "  ! Cash - VT: Cash - VT is linked" in result.output


def test_cli_with_nothing_to_clear_says_so(monkeypatch) -> None:
    _patch_context(monkeypatch, _Client(), [ROOT, OURS])
    result = runner.invoke(app, ["coa", "clear-stock", "--target", "test"])
    assert result.exit_code == 0
    assert "no stock-chart accounts to clear" in result.output


def test_a_refused_account_gets_its_company_defaults_back() -> None:
    client = _Client(refuse={"Cash - VT"})
    outcome = coa.clear_stock_confirmed(client, "VT", coa.plan_stock_clear([ROOT, CURRENT, CASH_GROUP, CASH]))  # ty: ignore[invalid-argument-type]
    assert client.log[:3] == [
        "probe ledger",
        "update Company ['default_cash_account']",
        "update Company ['default_cash_account']",
    ]
    assert client.company_updates == [{"default_cash_account": None}, {"default_cash_account": "Cash - VT"}]
    assert outcome.cleared_defaults == []


def test_cli_refuses_when_the_tree_changed_while_the_prompt_was_open(monkeypatch) -> None:
    client = _Client()
    _patch_context(monkeypatch, client, STOCK, then=[*STOCK, _live("Petty Cash Jar", "Cash In Hand")])
    result = runner.invoke(app, ["coa", "clear-stock", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert "refusing to clear: the accounts changed since planning" in result.output
    assert [line for line in client.log if line.startswith(("delete", "update"))] == []


def test_a_stock_name_under_a_parent_the_template_does_not_use_is_not_stock() -> None:
    own_group = _live("Petty Funds", "Current Assets", is_group=True)
    own_cash = _live("Cash", "Petty Funds")
    plan = coa.plan_stock_clear([ROOT, CURRENT, own_group, own_cash])
    assert [a.document_name for a in plan.deletes] == []
    assert [a.document_name for a, _ in plan.kept] == ["Current Assets - VT"]


def test_the_template_parents_are_erpnext_standard_chart() -> None:
    assert STANDARD_CHART_PARENTS["Cash"] == "Cash In Hand"
    assert STANDARD_CHART_PARENTS["Current Assets"] == "Application of Funds (Assets)"
    assert set(STANDARD_CHART_PARENTS) == STANDARD_CHART_ACCOUNT_NAMES


def test_a_stock_name_under_one_of_our_numbered_accounts_is_not_stock() -> None:
    our_group = _live("Cash In Hand", "Current Assets", number="1120", is_group=True)
    own_cash = _live("Cash", "1120 - Cash In Hand")
    plan = coa.plan_stock_clear([ROOT, CURRENT, our_group, own_cash])
    assert [a.document_name for a in plan.deletes] == []
    assert [a.document_name for a, _ in plan.kept] == ["Current Assets - VT"]


class _TransportError(Exception):
    pass


def test_a_delete_that_fails_in_transport_puts_the_defaults_back_and_raises() -> None:
    client = _Client()

    def delete(doctype: str, name: str) -> None:
        raise _TransportError("connection reset")

    client.delete = delete
    client.accounts = {"Cash - VT"}
    with pytest.raises(_TransportError):
        coa.clear_stock_confirmed(client, "VT", coa.plan_stock_clear([ROOT, CURRENT, CASH_GROUP, CASH]))  # ty: ignore[invalid-argument-type]
    assert client.company_updates == [{"default_cash_account": None}, {"default_cash_account": "Cash - VT"}]
