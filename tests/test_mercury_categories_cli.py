"""`runningtab mercury categories`: what it prints, what it refuses, and what it never sends.

Both commands are wired to a fake Mercury through `httpx2.MockTransport`, by
patching the two client names on `runningtab.cli` -- the same way the rest of this
suite reaches the CLI's collaborators.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from typer.testing import CliRunner

from runningtab.chart import load_chart_entries
from runningtab.cli import app
from runningtab.mercury.categories import CARD_SPEND, OTHER, REIMBURSEMENTS, MercuryCategoryWriter, desired_categories
from runningtab.mercury.client import MercuryClient, config_for
from runningtab.site import MERCURY_ADMIN_ENV, MERCURY_SANDBOX_ENV
from tests.conftest import EXAMPLE_BOOKS

runner = CliRunner()

SANDBOX = ["--env", "sandbox"]


def _category(name: str, *, card: bool, reimbursements: bool, other: bool, uuid: str) -> dict[str, Any]:
    return {
        "id": uuid,
        "name": name,
        CARD_SPEND: card,
        REIMBURSEMENTS: reimbursements,
        OTHER: other,
    }


def _wanted(number: str) -> Any:
    return next(c for c in desired_categories(load_chart_entries(EXAMPLE_BOOKS.chart_path)) if c.number == number)


def _wire(monkeypatch: pytest.MonkeyPatch, handler, *, seen: list[httpx2.Request] | None = None) -> None:
    """Point both the read client and the writer at one fake Mercury."""

    def recording(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    transport = httpx2.MockTransport(recording)
    monkeypatch.setattr("runningtab.cli.MercuryClient", lambda config: MercuryClient(config, transport=transport))
    monkeypatch.setattr(
        "runningtab.cli.MercuryCategoryWriter", lambda config: MercuryCategoryWriter(config, transport=transport)
    )
    monkeypatch.setattr("runningtab.cli.mercury_config", lambda site, env: config_for(env, "READ-TOKEN"))
    monkeypatch.setenv(MERCURY_SANDBOX_ENV, "SANDBOX-TOKEN")


def _listing(*rows: dict[str, Any]):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json={"categories": list(rows), "page": {}})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    return handler


# --- plan ------------------------------------------------------------------


def test_reaching_mercury_requires_naming_the_deployment() -> None:
    result = runner.invoke(app, ["mercury", "categories", "plan"])
    assert result.exit_code != 0
    assert "--env" in result.output


def test_plan_lists_every_chart_category_as_a_create_against_an_empty_bank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _listing())
    result = runner.invoke(app, ["mercury", "categories", "plan", *SANDBOX])
    assert result.exit_code == 1, result.output
    wanted = desired_categories(load_chart_entries(EXAMPLE_BOOKS.chart_path))
    assert f"create    {len(wanted):4}" in result.output
    # Each line carries what a person would have to type in the dashboard:
    # the exact name and the three flags.
    assert f"create    CRO  {_wanted('5299').name}" in result.output
    assert f"create    --O  {_wanted('4110').name}" in result.output


def test_plan_sends_no_write(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx2.Request] = []
    _wire(monkeypatch, _listing(), seen=seen)
    runner.invoke(app, ["mercury", "categories", "plan", *SANDBOX])
    assert seen
    assert {request.method for request in seen} == {"GET"}


def test_plan_exits_zero_when_the_two_lists_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _category(
            c.name,
            card=c.visible_for_card_spend,
            reimbursements=c.visible_for_reimbursements,
            other=c.visible_for_other,
            uuid=f"cat-{i}",
        )
        for i, c in enumerate(desired_categories(load_chart_entries(EXAMPLE_BOOKS.chart_path)))
    ]
    _wire(monkeypatch, _listing(*rows))
    result = runner.invoke(app, ["mercury", "categories", "plan", *SANDBOX])
    assert result.exit_code == 0, result.output
    assert f"matches   {len(rows):4}" in result.output


def test_plan_reports_an_unmanaged_category_without_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _category(
            c.name,
            card=c.visible_for_card_spend,
            reimbursements=c.visible_for_reimbursements,
            other=c.visible_for_other,
            uuid=f"cat-{i}",
        )
        for i, c in enumerate(desired_categories(load_chart_entries(EXAMPLE_BOOKS.chart_path)))
    ]
    rows.append(_category("Advertising & Marketing", card=True, reimbursements=True, other=True, uuid="cat-mercury"))
    _wire(monkeypatch, _listing(*rows))
    result = runner.invoke(app, ["mercury", "categories", "plan", *SANDBOX])
    assert result.exit_code == 0, result.output
    assert "unmanaged CRO  Advertising & Marketing" in result.output


def test_plan_names_both_uuids_of_a_duplicated_category(monkeypatch: pytest.MonkeyPatch) -> None:
    want = _wanted("5299")
    _wire(
        monkeypatch,
        _listing(
            _category(want.name, card=True, reimbursements=True, other=True, uuid="cat-a"),
            _category(want.name, card=True, reimbursements=True, other=True, uuid="cat-b"),
        ),
    )
    result = runner.invoke(app, ["mercury", "categories", "plan", *SANDBOX])
    assert result.exit_code == 1
    assert "REFUSED" in result.output
    assert "cat-a" in result.output and "cat-b" in result.output


# --- apply -----------------------------------------------------------------


def test_apply_refuses_without_the_write_variable_and_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # The refusal lands before any client is built: the token is the whole
    # ceremony, and a command that reads the bank first would make a person
    # think they are further along than they are.
    monkeypatch.delenv(MERCURY_ADMIN_ENV, raising=False)

    def never(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("apply reached Mercury without a write token")

    monkeypatch.setattr("runningtab.cli.MercuryClient", never)
    monkeypatch.setattr("runningtab.cli.MercuryCategoryWriter", never)
    result = runner.invoke(app, ["mercury", "categories", "apply", "--env", "production"])
    assert result.exit_code == 1
    assert MERCURY_ADMIN_ENV in result.output


def test_apply_creates_then_edits_and_reports_each_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = _wanted("5299")
    wrong_flags = _wanted("4210")
    written: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/api/v1")
        if request.method == "GET":
            return httpx2.Response(
                200,
                json={
                    "categories": [
                        _category(
                            c.name,
                            card=c.visible_for_card_spend,
                            reimbursements=c.visible_for_reimbursements,
                            other=c.visible_for_other,
                            uuid=f"cat-{i}",
                        )
                        for i, c in enumerate(desired_categories(load_chart_entries(EXAMPLE_BOOKS.chart_path)))
                        if c.number not in {missing.number, wrong_flags.number}
                    ]
                    + [_category(wrong_flags.name, card=True, reimbursements=False, other=True, uuid="cat-4210")],
                    "page": {},
                },
            )
        body = json.loads(request.content)
        written.append((path, json.dumps(body, sort_keys=True)))
        if path == "/categories":
            return httpx2.Response(
                201, json=_category(body["name"], card=True, reimbursements=True, other=True, uuid="cat-created")
            )
        return httpx2.Response(
            200, json=_category(wrong_flags.name, card=False, reimbursements=False, other=True, uuid="cat-4210")
        )

    _wire(monkeypatch, handler)
    result = runner.invoke(app, ["mercury", "categories", "apply", *SANDBOX, "--yes"])
    assert result.exit_code == 0, result.output
    assert [path for path, _ in written] == ["/categories", "/categories/cat-4210"]
    assert f"created  cat-created  {missing.name}" in result.output
    assert f"edited   cat-4210  {wrong_flags.name}" in result.output


def test_apply_without_yes_asks_before_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx2.Request] = []
    _wire(monkeypatch, _listing(), seen=seen)
    result = runner.invoke(app, ["mercury", "categories", "apply", *SANDBOX], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.output
    assert {request.method for request in seen} == {"GET"}


def test_apply_writes_nothing_when_the_lists_already_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _category(
            c.name,
            card=c.visible_for_card_spend,
            reimbursements=c.visible_for_reimbursements,
            other=c.visible_for_other,
            uuid=f"cat-{i}",
        )
        for i, c in enumerate(desired_categories(load_chart_entries(EXAMPLE_BOOKS.chart_path)))
    ]
    seen: list[httpx2.Request] = []
    _wire(monkeypatch, _listing(*rows), seen=seen)
    result = runner.invoke(app, ["mercury", "categories", "apply", *SANDBOX, "--yes"])
    assert result.exit_code == 0, result.output
    assert "nothing to write" in result.output
    assert {request.method for request in seen} == {"GET"}


def test_apply_reports_what_landed_before_a_refused_write(monkeypatch: pytest.MonkeyPatch) -> None:
    # A run that wrote one thing and then failed must not read as a run that
    # wrote nothing.
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json={"categories": [], "page": {}})
        calls.append(request.url.path)
        if len(calls) == 1:
            body = json.loads(request.content)
            return httpx2.Response(
                201, json=_category(body["name"], card=True, reimbursements=True, other=True, uuid="cat-first")
            )
        return httpx2.Response(403, json={"error": "insufficient scope"})

    _wire(monkeypatch, handler)
    result = runner.invoke(app, ["mercury", "categories", "apply", *SANDBOX, "--yes"])
    assert result.exit_code == 1
    assert "created  cat-first" in result.output
    assert "FAILED" in result.output and "403" in result.output


def test_apply_never_sends_a_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mercury's own defaults come back as unmanaged, and nothing retires them.
    seen: list[httpx2.Request] = []
    rows = [_category("Advertising & Marketing", card=True, reimbursements=True, other=True, uuid="cat-mercury")]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json={"categories": rows, "page": {}})
        body = json.loads(request.content)
        return httpx2.Response(
            201, json=_category(body["name"], card=True, reimbursements=True, other=True, uuid="cat-new")
        )

    _wire(monkeypatch, handler, seen=seen)
    runner.invoke(app, ["mercury", "categories", "apply", *SANDBOX, "--yes"])
    assert seen
    assert "DELETE" not in {request.method for request in seen}
    assert "cat-mercury" not in {request.url.path.rsplit("/", 1)[-1] for request in seen}


# --- pruning ---------------------------------------------------------------

PRUNE = [*SANDBOX, "--prune"]


def _row(transaction_id: str, *, category: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": transaction_id,
        "amount": -1.0,
        "status": "sent",
        "kind": "outgoingPayment",
        "createdAt": "2026-09-01T00:00:00.000Z",
        "categoryData": category,
    }


def _bank(*rows: dict[str, Any], transactions: list[dict[str, Any]] | None = None):
    """A fake Mercury the whole prune path can read: one account, its transactions, the category list.

    The deposit account is the only one; `/credit` answers empty. Everything
    `read_everything` asks for is served, so the plan is built from a real read
    rather than from a patched-out one.
    """
    transactions = transactions or []

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/api/v1")
        if request.method != "GET":
            body = json.loads(request.content) if request.content else {}
            if request.method == "DELETE":
                return httpx2.Response(204)
            return httpx2.Response(
                201, json=_category(body.get("name", "unnamed"), card=True, reimbursements=True, other=True, uuid="new")
            )
        if path == "/categories":
            return httpx2.Response(200, json={"categories": list(rows), "page": {}})
        if path == "/accounts":
            return httpx2.Response(
                200,
                json={
                    "accounts": [
                        {
                            "id": "acct-1",
                            "name": "Checking",
                            "currentBalance": -float(len(transactions)),
                            "createdAt": "2026-01-01T00:00:00.000Z",
                        }
                    ]
                },
            )
        if path == "/credit":
            return httpx2.Response(200, json={"accounts": []})
        if path == "/account/acct-1/transactions":
            return httpx2.Response(200, json={"total": len(transactions), "transactions": transactions})
        raise AssertionError(f"unexpected GET {path}")

    return handler


def test_plan_with_prune_lists_the_deletions_and_sends_no_write(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx2.Request] = []
    _wire(
        monkeypatch,
        _bank(_category("Travel", card=True, reimbursements=True, other=True, uuid="cat-travel")),
        seen=seen,
    )
    result = runner.invoke(app, ["mercury", "categories", "plan", *PRUNE])
    assert result.exit_code == 1, result.output
    assert "delete    Travel" in result.output
    assert {request.method for request in seen} == {"GET"}


def test_plan_with_prune_reports_an_in_use_category_with_its_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(
        monkeypatch,
        _bank(
            _category("Insurance", card=True, reimbursements=True, other=True, uuid="cat-ins"),
            transactions=[
                _row("t1", category={"customCategory": {"id": "cat-ins", "name": "Insurance"}}),
                _row("t2", category={"customCategory": {"id": "cat-ins", "name": "Insurance"}}),
            ],
        ),
    )
    result = runner.invoke(app, ["mercury", "categories", "plan", *PRUNE])
    # Still drift: the dropdown holds it, and only a person re-coding those
    # rows in Mercury resolves that.
    assert result.exit_code == 1, result.output
    assert "in use    2 transaction(s)  Insurance" in result.output
    assert "delete    Insurance" not in result.output
    assert "delete       0" in result.output


def test_plan_without_prune_reads_no_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    # The transaction read is the expensive half and exists only to answer a
    # question nobody asked without `--prune`.
    seen: list[httpx2.Request] = []
    _wire(
        monkeypatch,
        _bank(_category("Travel", card=True, reimbursements=True, other=True, uuid="cat-travel")),
        seen=seen,
    )
    runner.invoke(app, ["mercury", "categories", "plan", *SANDBOX])
    assert [r.url.path.removeprefix("/api/v1") for r in seen] == ["/categories"]


def test_apply_with_prune_deletes_an_unmanaged_category_nothing_carries(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx2.Request] = []
    _wire(
        monkeypatch,
        _bank(_category("Travel", card=True, reimbursements=True, other=True, uuid="cat-travel")),
        seen=seen,
    )
    result = runner.invoke(app, ["mercury", "categories", "apply", *PRUNE, "--yes"])
    assert result.exit_code == 0, result.output
    assert "deleted  cat-travel  Travel" in result.output
    deletes = [r.url.path for r in seen if r.method == "DELETE"]
    assert deletes == ["/api/v1/categories/cat-travel"]


def test_apply_with_prune_leaves_an_in_use_category_alone_and_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx2.Request] = []
    _wire(
        monkeypatch,
        _bank(
            _category("Insurance", card=True, reimbursements=True, other=True, uuid="cat-ins"),
            transactions=[_row("t1", category={"customCategory": {"id": "cat-ins", "name": "Insurance"}})],
        ),
        seen=seen,
    )
    result = runner.invoke(app, ["mercury", "categories", "apply", *PRUNE, "--yes"])
    assert result.exit_code == 1, result.output
    assert "in use    1 transaction(s)  Insurance" in result.output
    assert "DELETE" not in {request.method for request in seen}


def test_apply_sends_its_deletes_after_its_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Everything before a delete can be undone by running again; a delete
    # cannot, so it goes last.
    seen: list[httpx2.Request] = []
    _wire(
        monkeypatch,
        _bank(_category("Travel", card=True, reimbursements=True, other=True, uuid="cat-travel")),
        seen=seen,
    )
    runner.invoke(app, ["mercury", "categories", "apply", *PRUNE, "--yes"])
    methods = [request.method for request in seen if request.method != "GET"]
    assert methods
    assert methods == sorted(methods, key=lambda m: m == "DELETE")
    assert methods[-1] == "DELETE"


def test_a_refused_delete_is_one_line_and_the_run_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mercury may protect some of its own defaults; one it will not part with
    # says nothing about the next.
    rows = [
        _category("Protected", card=True, reimbursements=True, other=True, uuid="cat-a"),
        _category("Travel", card=True, reimbursements=True, other=True, uuid="cat-b"),
    ]
    underlying = _bank(*rows)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE" and request.url.path.endswith("cat-a"):
            return httpx2.Response(422, json={"error": "category is a Mercury default"})
        return underlying(request)

    _wire(monkeypatch, handler)
    result = runner.invoke(app, ["mercury", "categories", "apply", *PRUNE, "--yes"])
    assert result.exit_code == 1, result.output
    assert "REFUSED  cat-a  Protected" in result.output
    assert "422" in result.output
    assert "deleted  cat-b  Travel" in result.output


def test_prune_refuses_entirely_when_a_carried_category_has_no_readable_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the id never parses, every category looks uncarried and the whole
    # unmanaged list becomes a delete list. A visible refusal is the cheaper
    # failure.
    seen: list[httpx2.Request] = []
    _wire(
        monkeypatch,
        _bank(
            _category("Insurance", card=True, reimbursements=True, other=True, uuid="cat-ins"),
            transactions=[_row("t1", category={"customCategory": {"name": "Insurance"}})],
        ),
        seen=seen,
    )
    result = runner.invoke(app, ["mercury", "categories", "apply", *PRUNE, "--yes"])
    assert result.exit_code == 1, result.output
    assert "REFUSED" in result.output and "t1" in result.output
    assert "DELETE" not in {request.method for request in seen}


def test_the_mercury_group_does_not_claim_it_never_writes() -> None:
    # The group help used to say "Never writes to Mercury", truthfully.
    # `categories apply` writes, so the blanket claim went false the moment it
    # landed -- and help text that overstates what a command cannot do is
    # exactly what stops somebody checking.
    help_text = runner.invoke(app, ["mercury", "--help"]).output.lower()
    assert "never writes to mercury" not in " ".join(help_text.split())
