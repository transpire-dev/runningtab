"""The CLI's contract: plan writes nothing, apply asks first, and reaching the
ledger requires naming it."""

import contextlib
from typing import Any

import pytest
import typer
from icedcoffee_sync.apply.diff import ChartPlan, CreateAccount, ExtraAccount
from icedcoffee_sync.apply.model import LiveAccount
from icedcoffee_sync.apply.park import RENUMBER_METHOD
from icedcoffee_sync.frappe.client import SiteConfig
from typer.testing import CliRunner

from runningtab.chart import load_chart, load_defaults
from runningtab.cli import _Context, _plan_against, _sole_company, app
from runningtab.site import Target
from tests.conftest import EXAMPLE_BOOKS

runner = CliRunner()


class RecordingClient:
    """Records every write. `plan` must produce an empty record.

    `company_doc` seeds what `get("Company", ...)` answers -- empty by
    default, so a test that doesn't care about Company defaults sees
    `clear_defaults_pointing_at` find nothing and never call `update`.
    `event_log` interleaves every write in the order it actually happened,
    which `writes`/`deleted`/`disabled` alone cannot show (they're separate
    lists) -- it's what proves Company gets cleared *before* the deletes.
    `newly_entered` seeds which document names `accounts_with_entries`
    should report as carrying GL entries when re-probed -- empty by
    default, so a test that doesn't care about the re-probe sees a clean
    result and proceeds exactly as it did before that guard existed.
    """

    def __init__(self, company_doc: dict[str, Any] | None = None, newly_entered: set[str] | None = None) -> None:
        self.writes: list[str] = []
        self.deleted: list[str] = []
        self.disabled: list[str] = []
        self.company_doc: dict[str, Any] = dict(company_doc or {})
        self.company_updates: list[dict[str, Any]] = []
        self.event_log: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.newly_entered: set[str] = set(newly_entered or ())

    def __enter__(self) -> RecordingClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        # coa_apply's post-confirmation recheck calls _plan_against a second
        return None

    def insert(self, *args: Any, **kwargs: Any) -> Any:
        self.writes.append("insert")
        self.event_log.append("insert")
        return {"name": "x"}

    def list(self, doctype: str, **kwargs: Any) -> Any:
        if doctype == "GL Entry":
            (name,) = (value for field, _op, value in kwargs.get("filters", []) if field == "account")
            self.event_log.append(f"probe:{name}")
            return [{"account": name}] if name in self.newly_entered else []
        raise AssertionError(f"unexpected list({doctype!r})")

    def get(self, doctype: str, name: str) -> Any:
        self.event_log.append(f"get:{doctype}:{name}")
        if doctype == "Company":
            return self.company_doc
        raise AssertionError(f"unexpected get({doctype!r}, {name!r})")

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        self.writes.append("update")
        self.event_log.append(f"update:{doctype}:{name}")
        if doctype == "Company":
            self.company_updates.append(dict(doc))
        if doc.get("disabled"):
            self.disabled.append(name)
        return {"name": "x"}

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        self.writes.append("call")
        self.event_log.append(f"call:{dotted_path}")
        self.calls.append({"dotted_path": dotted_path, "params": dict(params or {})})
        return "x"

    def delete(self, doctype: str, name: str) -> Any:
        self.writes.append("delete")
        self.event_log.append(f"delete:{doctype}:{name}")
        self.deleted.append(name)
        return {}


# _CompanyListClient defines a public `list` method, which shadows the
_Names = list[str]


class _CompanyListClient:
    """A `Company` list double that refuses a single capped `list()` call outright.

    `_sole_company` must read through `iter_all`, never `list` -- a single
    `list` call with a fixed limit can't distinguish "the site genuinely
    has one company" from "there are more, cut off by a server-side page
    cap", and inferring the wrong company's structure silently is the
    exact mistake `_sole_company` exists to refuse.
    """

    def __init__(self, names: _Names) -> None:
        self.names = names

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        assert doctype == "Company"
        return iter({"name": n} for n in self.names)

    def list(self, doctype: str, **kwargs: Any) -> Any:
        raise AssertionError("_sole_company must read through iter_all, not a single capped list() call")


def test_sole_company_returns_the_one_company() -> None:
    assert _sole_company(_CompanyListClient(["Valley Tool Co"])) == "Valley Tool Co"  # ty: ignore[invalid-argument-type]


def test_sole_company_refuses_when_there_are_no_companies() -> None:
    with pytest.raises(typer.BadParameter, match="no companies"):
        _sole_company(_CompanyListClient([]))  # ty: ignore[invalid-argument-type]


def test_sole_company_refuses_when_there_is_more_than_one() -> None:
    with pytest.raises(typer.BadParameter, match="more than one company") as exc_info:
        _sole_company(_CompanyListClient(["Valley Tool Co", "Other Co"]))  # ty: ignore[invalid-argument-type]
    assert "Valley Tool Co" in str(exc_info.value)
    assert "Other Co" in str(exc_info.value)


def test_sole_company_stops_after_the_second_row_seen() -> None:
    with pytest.raises(typer.BadParameter):
        _sole_company(_CompanyListClient(["A", "B", "C"]))  # ty: ignore[invalid-argument-type]


@pytest.fixture
def recorded(monkeypatch):
    """Patch _plan_against to a one-create plan over a recording client."""
    client = RecordingClient()
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[],
        refusals=[],
    )
    context = _Context(
        client=client,  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number=by_number,
        plan=plan,
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    return client


def test_plan_asks_for_the_readonly_credential_and_apply_does_not(monkeypatch) -> None:
    """The backstop for the whole reader split, stated where it can fail loudly.

    `plan:books-chart` runs on merge requests, where the branch under review
    chooses what code the job executes. That is only safe while plan holds a
    credential the server refuses every write for, so a change routing plan
    back through the writing credential has to fail a test rather than pass
    review.
    """
    asked: list[bool] = []
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    context = _Context(
        client=RecordingClient(),  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number={a.number: a for a in chart},
        plan=ChartPlan(creates=[], renames=[], retypes=[], extras=[], refusals=[]),
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )

    def _site_config(site, target, *, readonly=False):
        asked.append(readonly)
        return SiteConfig(url="https://books.example.test", api_key="k", api_secret="s")  # noqa: S106

    monkeypatch.setattr("runningtab.cli.site_config", _site_config)
    monkeypatch.setattr("runningtab.cli.coa.plan_against", lambda config, company, chart, defaults: context)

    runner.invoke(app, ["coa", "plan", "--target", "books"])
    assert asked == [True], "plan must take the reader's credential"

    asked.clear()
    runner.invoke(app, ["coa", "apply", "--target", "books"], input="n\n")
    assert asked == [False], "apply changes the ledger and needs the writing credential"


def test_plan_writes_nothing(recorded) -> None:
    result = runner.invoke(app, ["coa", "plan", "--target", "test"])
    assert result.exit_code == 0
    assert recorded.writes == []


def test_plan_exits_non_zero_when_refused(monkeypatch) -> None:
    from icedcoffee_sync.apply.diff import Refusal, RefusalReason

    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    context = _Context(
        client=contextlib.nullcontext(),  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number={a.number: a for a in chart},
        plan=ChartPlan([], [], [], [], [Refusal(("x", None), RefusalReason.HAS_GL_ENTRIES, "5202 - X - VT")]),
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "plan", "--target", "test"])
    assert result.exit_code == 1
    assert "5202 - X - VT" in result.output


def test_plan_require_empty_exits_non_zero_while_work_remains(recorded) -> None:
    result = runner.invoke(app, ["coa", "plan", "--target", "test", "--require-empty"])
    assert result.exit_code == 1
    assert "does not match" in result.output
    assert recorded.writes == []


def test_plan_require_empty_passes_once_the_site_matches(monkeypatch) -> None:
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    context = _Context(
        client=contextlib.nullcontext(),  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number={a.number: a for a in chart},
        plan=ChartPlan([], [], [], [], []),
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "plan", "--target", "test", "--require-empty"])
    assert result.exit_code == 0
    assert "already matches" in result.output


def test_apply_declined_at_the_prompt_writes_nothing(recorded) -> None:
    result = runner.invoke(app, ["coa", "apply", "--target", "test"], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    assert recorded.writes == []


def test_apply_refused_writes_nothing_even_with_yes(monkeypatch) -> None:
    # --yes removes the confirmation prompt as a possible explanation: this
    from icedcoffee_sync.apply.diff import Refusal, RefusalReason

    client = RecordingClient()
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[],
        refusals=[Refusal(("x", None), RefusalReason.HAS_GL_ENTRIES, "5202 - X - VT")],
    )
    context = _Context(
        client=client,  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number=by_number,
        plan=plan,
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "apply", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert "5202 - X - VT" in result.output
    assert client.writes == []


def test_plan_output_separates_a_renumber_from_a_rename(monkeypatch) -> None:
    # Both are one ERPNext rename, but a reader approving a chart change needs
    # to see which accounts move their GL code and which only get a new label.
    from icedcoffee_sync.apply.diff import RenameAccount

    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    relabelled = LiveAccount("5219 - R&D Software - VT", "5219", "R&D Software", None, False, "Expense", None, False)
    moved = LiveAccount("5150 - Bank Charges - VT", "5150", "Bank Charges", None, False, "Expense", None, False)
    plan = ChartPlan(
        creates=[],
        renames=[
            RenameAccount(relabelled, by_number["5219"]),
            RenameAccount(moved, by_number["5202"]),
        ],
        retypes=[],
        extras=[],
        refusals=[],
    )
    context = _Context(
        client=contextlib.nullcontext(),  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number=by_number,
        plan=plan,
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)

    result = runner.invoke(app, ["coa", "plan", "--target", "test"])

    assert result.exit_code == 0
    assert "renumber    1" in result.output
    assert "rename      1" in result.output
    renumber_line = next(line for line in result.output.splitlines() if "5150 - Bank Charges - VT" in line)
    rename_line = next(line for line in result.output.splitlines() if "5219 - R&D Software - VT" in line)
    # What each line says it is becoming: a renumber leads with the new number,
    # a rename shows the label alone rather than a number that did not move.
    assert renumber_line.split("-> ")[1].startswith("5202 ")
    assert rename_line.split("-> ")[1] == "Engineering Tools (GitHub, JetBrains, Linear)"


def test_apply_declined_at_the_prompt_writes_nothing_even_with_an_empty_plan(monkeypatch) -> None:
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    client = RecordingClient()
    plan = ChartPlan(creates=[], renames=[], retypes=[], extras=[], refusals=[])
    assert plan.is_empty
    context = _Context(
        client=client,  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number=by_number,
        plan=plan,
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)

    result = runner.invoke(app, ["coa", "apply", "--target", "test"], input="n\n")

    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    assert client.writes == []


def test_apply_refuses_before_any_write_when_a_default_field_is_missing(monkeypatch) -> None:
    client = RecordingClient()  # empty company_doc: every default field is "missing"
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    plan = ChartPlan(creates=[CreateAccount(by_number["5299"])], renames=[], retypes=[], extras=[], refusals=[])
    context = _Context(
        client=client,  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number=by_number,
        plan=plan,
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)

    result = runner.invoke(app, ["coa", "apply", "--target", "test", "--yes"])

    assert result.exit_code == 1
    assert client.writes == []
    assert "default" in result.output.lower()


def test_apply_reports_parked_accounts_when_a_renumber_cycle_is_broken(monkeypatch) -> None:
    # Measured live: a plan can legitimately contain a renumber cycle (two
    from icedcoffee_sync.apply.diff import RenameAccount
    from icedcoffee_sync.apply.model import DesiredAccount
    from icedcoffee_sync.erpnext.accounts import document_name as build_document_name

    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    # Seed the fake Company document with exactly the defaults `coa apply`
    company_doc = {
        field: build_document_name(by_number[number].name, "VT", number)
        for field, number in load_defaults(EXAMPLE_BOOKS.chart_path).items()
    }
    client = RecordingClient(company_doc=company_doc)

    a_live = LiveAccount("A - VT", "10", "A", None, False, "Expense", None, False)
    b_live = LiveAccount("B - VT", "20", "B", None, False, "Expense", None, False)
    a_desired = DesiredAccount("20", "A", None, False, "Expense", None)
    b_desired = DesiredAccount("10", "B", None, False, "Expense", None)
    plan = ChartPlan(
        creates=[],
        renames=[RenameAccount(a_live, a_desired), RenameAccount(b_live, b_desired)],
        retypes=[],
        extras=[],
        refusals=[],
    )
    context = _Context(
        client=client,  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number=by_number,
        plan=plan,
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)

    result = runner.invoke(app, ["coa", "apply", "--target", "test", "--yes"])

    assert result.exit_code == 0, result.output
    assert "parked 1" in result.output


def test_apply_refuses_a_renumber_target_that_gained_entries_since_planning(monkeypatch) -> None:
    from icedcoffee_sync.apply.diff import Refusal, RefusalReason, RenameAccount
    from icedcoffee_sync.apply.model import DesiredAccount
    from icedcoffee_sync.erpnext.accounts import document_name as build_document_name

    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    company_doc = {
        field: build_document_name(by_number[number].name, "VT", number)
        for field, number in load_defaults(EXAMPLE_BOOKS.chart_path).items()
    }
    client = RecordingClient(company_doc=company_doc)
    a_live = LiveAccount("A - VT", "10", "A", None, False, "Expense", None, False)
    a_desired = DesiredAccount("11", "A", None, False, "Expense", None)
    before_plan = ChartPlan(creates=[], renames=[RenameAccount(a_live, a_desired)], retypes=[], extras=[], refusals=[])
    after_plan = ChartPlan(
        creates=[],
        renames=[],
        retypes=[],
        extras=[],
        refusals=[
            Refusal(
                ("A", None),
                RefusalReason.HAS_GL_ENTRIES,
                "A - VT carries ledger entries and cannot be modified",
                ("A - VT",),
            )
        ],
    )
    contexts = [
        _Context(
            client=client,  # ty: ignore[invalid-argument-type]
            company="Valley Tool Co",
            abbr="VT",
            chart=chart,
            by_number=by_number,
            plan=plan,
            live=[],
            with_entries=with_entries,
            defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
        )
        for plan, with_entries in ((before_plan, set()), (after_plan, {"A - VT"}))
    ]
    calls = iter(contexts)
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company: next(calls))

    result = runner.invoke(app, ["coa", "apply", "--target", "test", "--yes"])

    assert result.exit_code == 1
    assert "a - vt" in result.output.lower()
    assert "changed since planning" in result.output.lower()
    assert client.writes == []


def test_apply_reports_no_parked_count_when_nothing_was_parked(monkeypatch) -> None:
    from icedcoffee_sync.erpnext.accounts import document_name as build_document_name

    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    company_doc = {
        field: build_document_name(by_number[number].name, "VT", number)
        for field, number in load_defaults(EXAMPLE_BOOKS.chart_path).items()
    }
    client = RecordingClient(company_doc=company_doc)
    plan = ChartPlan(creates=[CreateAccount(by_number["5299"])], renames=[], retypes=[], extras=[], refusals=[])
    context = _Context(
        client=client,  # ty: ignore[invalid-argument-type]
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number=by_number,
        plan=plan,
        live=[],
        with_entries=set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)

    result = runner.invoke(app, ["coa", "apply", "--target", "test", "--yes"])

    assert result.exit_code == 0, result.output
    assert "parked" not in result.output.lower()


def test_apply_refuses_when_the_plan_changed_since_confirmation(monkeypatch) -> None:
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    client = RecordingClient()
    original_plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])], renames=[], retypes=[], extras=[], refusals=[]
    )
    changed_plan = ChartPlan(creates=[], renames=[], retypes=[], extras=[], refusals=[])
    contexts = [
        _Context(
            client=client,  # ty: ignore[invalid-argument-type]
            company="Valley Tool Co",
            abbr="VT",
            chart=chart,
            by_number=by_number,
            plan=plan,
            live=[],
            with_entries=set(),
            defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
        )
        for plan in (original_plan, changed_plan)
    ]
    calls = iter(contexts)
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company: next(calls))

    result = runner.invoke(app, ["coa", "apply", "--target", "test", "--yes"])

    assert result.exit_code == 1
    assert "changed since planning" in result.output.lower()
    assert client.writes == []


def test_apply_requires_the_target_spelled_out() -> None:
    # There is no default target. Reaching the ledger means typing its name.
    result = runner.invoke(app, ["coa", "apply"])
    assert result.exit_code != 0
    assert "target" in result.output.lower()


def test_export_gl_codes_goes_to_stdout_by_default() -> None:
    result = runner.invoke(app, ["coa", "export-gl-codes"])
    assert result.exit_code == 0
    assert "5299" in result.output


def _extra(name: str = "Old Stock - VT", disabled: bool = False) -> LiveAccount:
    return LiveAccount(name, "", "Old Stock", "5000 - Expenses - VT", False, "Expense", None, disabled)


def _prune_context(
    client: Any, plan: ChartPlan, live: list[LiveAccount], with_entries: set[str] | None = None
) -> _Context:
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    return _Context(
        client=client,
        company="Valley Tool Co",
        abbr="VT",
        chart=chart,
        by_number={a.number: a for a in chart},
        plan=plan,
        live=live,
        with_entries=with_entries if with_entries is not None else set(),
        defaults=load_defaults(EXAMPLE_BOOKS.chart_path),
    )


def test_prune_declined_at_the_prompt_deletes_nothing(monkeypatch) -> None:
    client = RecordingClient()
    extra = _extra()
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test"], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    assert client.deleted == []


def test_prune_deletes_with_yes(monkeypatch) -> None:
    client = RecordingClient()
    extra = _extra()
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == ["Old Stock - VT"]


def test_prune_recheck_excludes_a_delete_candidate_that_gained_entries_since_planning(monkeypatch) -> None:
    client = RecordingClient()
    extra = _extra()
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    before = _prune_context(client, plan, [extra])
    after = _prune_context(client, plan, [extra], with_entries={"Old Stock - VT"})
    contexts = iter([before, after])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company: next(contexts))
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert client.deleted == []
    assert "old stock - vt" in result.output.lower()
    assert "changed since planning" in result.output.lower()


def test_prune_allow_unapplied_does_not_clear_defaults_for_a_recheck_excluded_candidate(monkeypatch) -> None:
    client = RecordingClient(company_doc={"name": "Valley Tool Co", "default_expense_account": "Old Stock - VT"})
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    extra = _extra()
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[ExtraAccount(extra)],
        refusals=[],
    )
    before = _prune_context(client, plan, [extra])
    after = _prune_context(client, plan, [extra], with_entries={"Old Stock - VT"})
    contexts = iter([before, after])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company: next(contexts))
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 1
    assert client.company_updates == []
    assert client.deleted == []


def test_prune_recheck_does_not_exclude_a_candidate_that_stayed_clear(monkeypatch) -> None:
    # The re-probe must not become a blanket refusal: a candidate that was
    client = RecordingClient()
    extra = _extra()
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == ["Old Stock - VT"]
    assert "changed since planning" not in result.output.lower()


def test_prune_refuses_when_the_chart_plan_itself_changed_since_confirmation(monkeypatch) -> None:
    client = RecordingClient()
    extra = _extra()
    before = _prune_context(client, ChartPlan([], [], [], [ExtraAccount(extra)], []), [extra])
    after = _prune_context(client, ChartPlan([], [], [], [], []), [])
    contexts = iter([before, after])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company: next(contexts))
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert client.deleted == []
    assert "changed since planning" in result.output.lower()


def test_prune_refuses_on_root_not_unique(monkeypatch) -> None:
    # ROOT_NOT_UNIQUE breaks the top-down tree walk, so every classification
    from icedcoffee_sync.apply.diff import Refusal, RefusalReason

    client = RecordingClient()
    plan = ChartPlan([], [], [], [], [Refusal(("x", None), RefusalReason.ROOT_NOT_UNIQUE, "5202 - X - VT")])
    context = _prune_context(client, plan, [])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert "5202 - X - VT" in result.output
    assert client.deleted == []


def test_prune_refuses_on_ambiguous_match(monkeypatch) -> None:
    # AMBIGUOUS_MATCH marks every colliding candidate as matched, which
    from icedcoffee_sync.apply.diff import Refusal, RefusalReason

    client = RecordingClient()
    plan = ChartPlan([], [], [], [], [Refusal(("x", None), RefusalReason.AMBIGUOUS_MATCH, "5202 - X - VT")])
    context = _prune_context(client, plan, [])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert "5202 - X - VT" in result.output
    assert client.deleted == []


def test_prune_number_collision_alone_does_not_block(monkeypatch) -> None:
    # NUMBER_COLLISION attaches to a create, not to a live account already
    from icedcoffee_sync.apply.diff import Refusal, RefusalReason

    client = RecordingClient()
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    extra = _extra()
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[ExtraAccount(extra)],
        refusals=[Refusal(("5299", None), RefusalReason.NUMBER_COLLISION, "5299 held by Old Stock - VT")],
    )
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == ["Old Stock - VT"]
    assert "1 refusal(s)" in result.output


def test_prune_group_flag_changed_alone_does_not_block(monkeypatch) -> None:
    # GROUP_FLAG_CHANGED attaches to a matched account, not an extra, so
    from icedcoffee_sync.apply.diff import Refusal, RefusalReason

    client = RecordingClient()
    extra = _extra()
    plan = ChartPlan(
        [],
        [],
        [],
        [ExtraAccount(extra)],
        [Refusal(("x", None), RefusalReason.GROUP_FLAG_CHANGED, "5000 - Expenses - VT")],
    )
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == ["Old Stock - VT"]


def test_prune_refuses_when_the_chart_has_not_been_applied(monkeypatch) -> None:
    # A non-empty-but-not-refused plan means the chart itself has pending
    client = RecordingClient()
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[],
        refusals=[],
    )
    context = _prune_context(client, plan, [])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert "apply" in result.output.lower()
    assert client.writes == []
    assert client.deleted == []
    assert client.disabled == []


def test_prune_allow_unapplied_proceeds_with_a_non_empty_plan(monkeypatch) -> None:
    client = RecordingClient()
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    extra = _extra()
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[ExtraAccount(extra)],
        refusals=[],
    )
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == ["Old Stock - VT"]


def test_prune_allow_unapplied_does_not_bypass_refusals(monkeypatch) -> None:
    # The important case: --allow-unapplied must not be read as a general
    from icedcoffee_sync.apply.diff import Refusal, RefusalReason

    client = RecordingClient()
    plan = ChartPlan([], [], [], [], [Refusal(("x", None), RefusalReason.ROOT_NOT_UNIQUE, "5202 - X - VT")])
    context = _prune_context(client, plan, [])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 1
    assert "5202 - X - VT" in result.output
    assert client.writes == []
    assert client.deleted == []


def test_prune_allow_unapplied_prints_a_warning(monkeypatch) -> None:
    client = RecordingClient()
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    extra = _extra()
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[ExtraAccount(extra)],
        refusals=[],
    )
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    assert "warning" in result.output.lower()
    assert "unapplied" in result.output.lower()


def test_prune_allow_unapplied_clears_company_defaults_before_deleting(monkeypatch) -> None:
    # The cycle this exists to break: ERPNext refuses to disable/delete an
    client = RecordingClient(company_doc={"name": "Valley Tool Co", "default_expense_account": "Old Stock - VT"})
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    extra = _extra()
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[ExtraAccount(extra)],
        refusals=[],
    )
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    assert client.company_updates == [{"default_expense_account": None}]
    assert client.deleted == ["Old Stock - VT"]
    # Order matters: the clear must land before the delete it unblocks.
    assert client.event_log.index("update:Company:Valley Tool Co") < client.event_log.index(
        "delete:Account:Old Stock - VT"
    )
    assert "cleared company defaults" in result.output.lower()
    assert "default_expense_account" in result.output.lower()


def test_prune_allow_unapplied_prints_nothing_when_no_default_points_at_a_delete(monkeypatch) -> None:
    client = RecordingClient(company_doc={"name": "Valley Tool Co", "default_expense_account": "Something Else"})
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    extra = _extra()
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[ExtraAccount(extra)],
        refusals=[],
    )
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    assert client.company_updates == []
    assert "cleared company defaults" not in result.output.lower()


def test_prune_without_allow_unapplied_never_touches_company(monkeypatch) -> None:
    # The default path's protection stays: without the flag, Company is
    client = RecordingClient(company_doc={"name": "Valley Tool Co", "default_expense_account": "Old Stock - VT"})
    extra = _extra()
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert client.company_updates == []
    assert not any(event.startswith("get:Company") for event in client.event_log)


def test_prune_declined_at_the_prompt_never_touches_company_even_with_allow_unapplied(monkeypatch) -> None:
    client = RecordingClient(company_doc={"name": "Valley Tool Co", "default_expense_account": "Old Stock - VT"})
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    extra = _extra()
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[ExtraAccount(extra)],
        refusals=[],
    )
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied"], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    assert client.company_updates == []
    assert client.deleted == []


def test_prune_without_the_flag_prints_no_warning(monkeypatch) -> None:
    client = RecordingClient()
    chart = load_chart(EXAMPLE_BOOKS.chart_path)
    by_number = {a.number: a for a in chart}
    plan = ChartPlan(
        creates=[CreateAccount(by_number["5299"])],
        renames=[],
        retypes=[],
        extras=[],
        refusals=[],
    )
    context = _prune_context(client, plan, [])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert "warning" not in result.output.lower()


def test_prune_reports_nothing_to_prune_when_the_site_already_matches(monkeypatch) -> None:
    client = RecordingClient()
    plan = ChartPlan([], [], [], [], [])
    context = _prune_context(client, plan, [])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert "nothing to prune" in result.output.lower()
    assert client.deleted == []


def test_prune_falls_back_to_disable_when_delete_fails(monkeypatch) -> None:
    # ERPNext refuses to delete some accounts outright, naming disable as
    from icedcoffee_sync.frappe.errors import LinkExistsError

    class UndeletableClient(RecordingClient):
        def delete(self, doctype: str, name: str) -> Any:
            raise LinkExistsError("You can disable this Account instead of deleting it")

    client = UndeletableClient()
    extra = _extra()
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == []
    assert client.disabled == ["Old Stock - VT"]
    assert "disabled 1" in result.output


def test_prune_exits_non_zero_when_disabling_also_fails(monkeypatch) -> None:
    from icedcoffee_sync.frappe.errors import LinkExistsError, ValidationError

    class HopelessClient(RecordingClient):
        def delete(self, doctype: str, name: str) -> Any:
            raise LinkExistsError("still linked")

        def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
            raise ValidationError("cannot disable either")

    client = HopelessClient()
    extra = _extra()
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert "cannot disable either" in result.output
    assert client.deleted == []
    assert client.disabled == []


def test_prune_attempts_an_already_disabled_extra_and_does_not_re_disable_it(monkeypatch) -> None:
    from icedcoffee_sync.frappe.errors import LinkExistsError

    class StillLinkedClient(RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.delete_attempts: list[str] = []

        def delete(self, doctype: str, name: str) -> Any:
            self.delete_attempts.append(name)
            raise LinkExistsError("still linked")

    client = StillLinkedClient()
    extra = _extra(disabled=True)
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert client.delete_attempts == ["Old Stock - VT"]  # the delete was genuinely retried
    assert client.deleted == []
    assert client.disabled == []
    assert client.writes == []  # no update call -- it was already disabled, not re-disabled
    assert "already-disabled 1" in result.output.lower()


def test_prune_deletes_a_previously_disabled_extra_that_now_succeeds(monkeypatch) -> None:
    client = RecordingClient()
    extra = _extra(disabled=True)
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == ["Old Stock - VT"]
    assert "already-disabled" not in result.output.lower()


def _colliding_survivor(number: str = "5299", document_name: str = "5299 - Old Stock - VT") -> LiveAccount:
    """A live account whose number the real chart also uses -- a `NUMBER_COLLISION` waiting to happen."""
    return LiveAccount(document_name, number, "Old Stock", "5000 - Expenses - VT", False, "Expense", None, False)


class _HopelessClient(RecordingClient):
    """Both the delete and the disable fallback fail -- the account survives, unresolved."""

    def delete(self, doctype: str, name: str) -> Any:
        from icedcoffee_sync.frappe.errors import LinkExistsError

        self.event_log.append(f"delete_attempt:{doctype}:{name}")
        raise LinkExistsError("still linked")

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        from icedcoffee_sync.frappe.errors import ValidationError

        raise ValidationError("cannot disable either")


def test_prune_allow_unapplied_parks_a_colliding_survivor(monkeypatch) -> None:
    # The final step of the cycle-breaking chain: a survivor ERPNext refuses
    client = _HopelessClient()
    survivor = _colliding_survivor()
    plan = ChartPlan([], [], [], [ExtraAccount(survivor)], [])
    context = _prune_context(client, plan, [survivor])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    renumber_calls = [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert renumber_calls == [
        {
            "dotted_path": RENUMBER_METHOD,
            "params": {"name": "5299 - Old Stock - VT", "account_name": "Old Stock", "account_number": "95299"},
        }
    ]
    assert "parked 5299 - Old Stock - VT -> 95299" in result.output


def test_prune_allow_unapplied_does_not_park_a_survivor_that_gained_entries(monkeypatch) -> None:
    client = _HopelessClient(newly_entered={"5299 - Old Stock - VT"})
    survivor = _colliding_survivor()
    plan = ChartPlan([], [], [], [ExtraAccount(survivor)], [])
    context = _prune_context(client, plan, [survivor])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert not [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert "not parking 5299 - old stock - vt" in result.output.lower()


class _DisablesInsteadOfDeleting(RecordingClient):
    """Delete is always refused; the disable fallback always succeeds.

    Distinct from `_HopelessClient`, where both fail: this models the more
    common outcome (the account lands in `report.disabled`, not
    `report.failed`) so the exit-code check has something in `disabled` to
    miss if it only ever looks at `failed`.
    """

    def delete(self, doctype: str, name: str) -> Any:
        from icedcoffee_sync.frappe.errors import LinkExistsError

        self.event_log.append(f"delete_attempt:{doctype}:{name}")
        raise LinkExistsError("still linked")


def test_prune_allow_unapplied_exits_non_zero_when_a_disabled_survivor_still_collides(monkeypatch) -> None:
    client = _DisablesInsteadOfDeleting(newly_entered={"5299 - Old Stock - VT"})
    survivor = _colliding_survivor()
    plan = ChartPlan([], [], [], [ExtraAccount(survivor)], [])
    context = _prune_context(client, plan, [survivor])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert client.disabled == ["5299 - Old Stock - VT"]
    assert not [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert "not parking 5299 - old stock - vt" in result.output.lower()
    assert result.exit_code == 1


def test_prune_allow_unapplied_parks_only_after_all_deletes_complete(monkeypatch) -> None:
    class MixedClient(_HopelessClient):
        def delete(self, doctype: str, name: str) -> Any:
            if name == "5299 - Old Stock - VT":
                return super().delete(doctype, name)
            self.writes.append("delete")
            self.event_log.append(f"delete:{doctype}:{name}")
            self.deleted.append(name)
            return {}

    client = MixedClient()
    survivor = _colliding_survivor()
    normal = _extra("Deletable - VT")
    plan = ChartPlan([], [], [], [ExtraAccount(survivor), ExtraAccount(normal)], [])
    context = _prune_context(client, plan, [survivor, normal])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == ["Deletable - VT"]
    park_index = client.event_log.index(f"call:{RENUMBER_METHOD}")
    assert park_index > client.event_log.index("delete:Account:Deletable - VT")
    assert park_index > client.event_log.index("delete_attempt:Account:5299 - Old Stock - VT")


def test_prune_allow_unapplied_parks_a_survivor_refused_for_a_live_child(monkeypatch) -> None:
    # Ruling: parking candidates are report's own outcomes PLUS every extra
    client = RecordingClient()
    survivor = _colliding_survivor()
    child = LiveAccount("Child - VT", "", "Child", survivor.document_name, False, "Expense", None, False)
    normal = _extra("Deletable - VT")
    plan = ChartPlan([], [], [], [ExtraAccount(survivor), ExtraAccount(normal)], [])
    context = _prune_context(client, plan, [survivor, child, normal])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == ["Deletable - VT"]
    renumber_calls = [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert renumber_calls == [
        {
            "dotted_path": RENUMBER_METHOD,
            "params": {"name": survivor.document_name, "account_name": "Old Stock", "account_number": "95299"},
        }
    ]
    assert "parked 5299 - Old Stock - VT -> 95299" in result.output


def test_prune_allow_unapplied_parks_even_when_nothing_is_deletable(monkeypatch) -> None:
    # The "nothing to delete" early return must not skip parking: with every
    client = RecordingClient()
    survivor = _colliding_survivor()
    child = LiveAccount("Child - VT", "", "Child", survivor.document_name, False, "Expense", None, False)
    plan = ChartPlan([], [], [], [ExtraAccount(survivor)], [])
    context = _prune_context(client, plan, [survivor, child])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == []
    assert "nothing to delete" in result.output
    renumber_calls = [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert renumber_calls == [
        {
            "dotted_path": RENUMBER_METHOD,
            "params": {"name": survivor.document_name, "account_name": "Old Stock", "account_number": "95299"},
        }
    ]
    assert "parked 5299 - Old Stock - VT -> 95299" in result.output


def test_prune_allow_unapplied_declined_at_the_park_only_prompt_renumbers_nothing(monkeypatch) -> None:
    client = RecordingClient()
    survivor = _colliding_survivor()
    child = LiveAccount("Child - VT", "", "Child", survivor.document_name, False, "Expense", None, False)
    plan = ChartPlan([], [], [], [ExtraAccount(survivor)], [])
    context = _prune_context(client, plan, [survivor, child])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied"], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    assert not [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert "parked" not in result.output.lower()


def test_prune_allow_unapplied_park_only_recheck_refuses_on_a_stale_plan(monkeypatch) -> None:
    # The park-only path now runs through the same
    client = RecordingClient()
    survivor = _colliding_survivor()
    child = LiveAccount("Child - VT", "", "Child", survivor.document_name, False, "Expense", None, False)
    before = _prune_context(client, ChartPlan([], [], [], [ExtraAccount(survivor)], []), [survivor, child])
    after = _prune_context(client, ChartPlan([], [], [], [], []), [])
    contexts = iter([before, after])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company: next(contexts))
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 1
    assert not [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert "changed since planning" in result.output.lower()


def test_prune_without_the_flag_does_not_park_when_nothing_is_deletable(monkeypatch) -> None:
    client = RecordingClient()
    survivor = _colliding_survivor()
    child = LiveAccount("Child - VT", "", "Child", survivor.document_name, False, "Expense", None, False)
    plan = ChartPlan([], [], [], [ExtraAccount(survivor)], [])
    context = _prune_context(client, plan, [survivor, child])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert not [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert "parked" not in result.output.lower()


def test_prune_allow_unapplied_never_parks_an_account_with_gl_entries(monkeypatch) -> None:
    client = RecordingClient()
    survivor = _colliding_survivor()
    normal = _extra("Deletable - VT")
    plan = ChartPlan([], [], [], [ExtraAccount(survivor), ExtraAccount(normal)], [])
    context = _prune_context(client, plan, [survivor, normal])
    context.with_entries.add(survivor.document_name)
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert client.deleted == ["Deletable - VT"]
    assert not [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert "parked" not in result.output.lower()
    assert result.exit_code == 1


def test_prune_allow_unapplied_does_not_fail_a_non_colliding_gl_entries_refusal(monkeypatch) -> None:
    # Control for the fix above: a GL-entry-carrying extra that does NOT
    client = RecordingClient()
    busy = _extra("Busy - VT")  # number="" -- not a chart number, nothing to collide with
    plan = ChartPlan([], [], [], [ExtraAccount(busy)], [])
    context = _prune_context(client, plan, [busy])
    context.with_entries.add("Busy - VT")
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 0
    assert not [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]


def test_prune_allow_unapplied_does_not_park_a_non_colliding_failure(monkeypatch) -> None:
    client = _HopelessClient()
    extra = _extra()  # number="" -- not a chart number, nothing to collide with
    plan = ChartPlan([], [], [], [ExtraAccount(extra)], [])
    context = _prune_context(client, plan, [extra])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--allow-unapplied", "--yes"])
    assert result.exit_code == 1
    assert not [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert "parked" not in result.output.lower()


def test_prune_without_allow_unapplied_never_parks_survivors(monkeypatch) -> None:
    # The default path never renames, even when a colliding survivor is
    client = _HopelessClient()
    survivor = _colliding_survivor()
    plan = ChartPlan([], [], [], [ExtraAccount(survivor)], [])
    context = _prune_context(client, plan, [survivor])
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 1
    assert not [c for c in client.calls if c["dotted_path"] == RENUMBER_METHOD]
    assert "parked" not in result.output.lower()


def test_prune_a_refused_account_does_not_block_the_rest(monkeypatch) -> None:
    client = RecordingClient()
    busy = _extra("Busy - VT")
    idle = _extra("Idle - VT")
    plan = ChartPlan([], [], [], [ExtraAccount(busy), ExtraAccount(idle)], [])
    context = _prune_context(client, plan, [busy, idle])
    context.with_entries.add("Busy - VT")
    monkeypatch.setattr("runningtab.cli._plan_against", lambda books, target, company, **_: context)
    result = runner.invoke(app, ["coa", "prune", "--target", "test", "--yes"])
    assert result.exit_code == 0
    assert client.deleted == ["Idle - VT"]
    assert "has_gl_entries" in result.output


class _FakeAccountsClient:
    """Enough of `Client`'s surface for `_plan_against`'s real code path.

    Every other test in this file monkeypatches `_plan_against` itself, which
    means none of them ever exercise the real function -- including its call
    into `accounts_with_entries`, which probes `GL Entry`. This fake stands in
    for `icedcoffee_sync.frappe.client.Client` (constructed by `_plan_against`, not
    monkeypatched away) so that call happens for real.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.gl_entry_queries: list[str] = []

    def list(self, doctype: str, **kwargs: Any) -> Any:
        if doctype == "Company":
            return [{"name": "Valley Tool Co", "abbr": "VT"}]
        if doctype == "Account":
            return [
                {
                    "name": "1131 - Accounts Receivable - VT",
                    "account_number": "1131",
                    "account_name": "Accounts Receivable",
                    "parent_account": None,
                    "is_group": 0,
                    "root_type": "Asset",
                    "account_type": "Receivable",
                    "disabled": 0,
                },
                {
                    "name": "5299 - UNCATEGORISED - VT",
                    "account_number": "5299",
                    "account_name": "UNCATEGORISED",
                    "parent_account": None,
                    "is_group": 0,
                    "root_type": "Expense",
                    "account_type": None,
                    "disabled": 0,
                },
            ]
        if doctype == "GL Entry":
            account = kwargs["filters"][0][2]
            self.gl_entry_queries.append(account)
            if account == "1131 - Accounts Receivable - VT":
                return [{"account": account}]
            return []
        raise AssertionError(f"unexpected list({doctype!r})")

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        # read_chart pages through iter_all rather than a single capped
        return iter(self.list(doctype, **kwargs))

    def get(self, doctype: str, name: str) -> Any:
        raise AssertionError(f"unexpected get({doctype!r}, {name!r})")


def test_plan_against_really_probes_gl_entries(monkeypatch) -> None:
    # No monkeypatching of _plan_against here: this calls the real function,
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "key")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "secret")
    fake = _FakeAccountsClient(None)
    monkeypatch.setattr("runningtab.coa.Client", lambda config: fake)

    context = _plan_against(EXAMPLE_BOOKS, Target.TEST, "Valley Tool Co")

    assert fake.gl_entry_queries, "no GL Entry query happened"
    assert context.with_entries == {"1131 - Accounts Receivable - VT"}
