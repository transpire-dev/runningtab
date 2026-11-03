"""Mercury's categories against the chart: what is selected, what is planned, and what is never written.

The selection tests run against the **real** `load_chart_entries(EXAMPLE_BOOKS.chart_path)` rather than a
constructed chart. A hand-built chart agrees with whatever the test expects,
and the property under test is exactly "the right accounts come out of the file
we ship".
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from decimal import Decimal

import httpx2
import pytest

from runningtab.chart import load_chart, load_chart_entries
from runningtab.gl_codes import gl_code_csv, gl_code_name
from runningtab.mercury.categories import (
    CARD_SPEND,
    OTHER,
    REIMBURSEMENTS,
    CategoryUseUnreadableError,
    DesiredCategory,
    MercuryCategoryWriter,
    apply_plan,
    carried_categories,
    desired_categories,
    plan_categories,
)
from runningtab.mercury.client import BASE_URLS, MercuryClient, MercuryEnv, MercuryError, config_for
from runningtab.mercury.models import Category, Status, Transaction
from tests.conftest import EXAMPLE_BOOKS

SANDBOX = config_for(MercuryEnv.SANDBOX, "SANDBOX-TOKEN")


def _wanted() -> dict[str, DesiredCategory]:
    return {c.number: c for c in desired_categories(load_chart_entries(EXAMPLE_BOOKS.chart_path))}


def _live(name: str, *, card: bool, reimbursements: bool, other: bool, uuid: str = "cat-1") -> Category:
    return Category(
        id=uuid,
        name=name,
        visible_for_card_spend=card,
        visible_for_reimbursements=reimbursements,
        visible_for_other=other,
    )


# --- which accounts become categories -------------------------------------


def test_every_postable_expense_account_a_bank_line_can_reach_becomes_a_category() -> None:
    expenses = [
        entry.account
        for entry in load_chart_entries(EXAMPLE_BOOKS.chart_path)
        if not entry.account.is_group and entry.account.number.startswith("5") and not entry.no_bank_line
    ]
    assert expenses, "the chart must carry expense accounts for this test to mean anything"
    assert {a.number for a in expenses} <= set(_wanted())


def test_an_account_the_chart_tags_no_bank_line_is_never_offered() -> None:
    # Depreciation, round-off, FX, write-offs and a disposal gain reach the
    # ledger as a period-end or adjustment journal. None of them is ever a
    # statement line, so a category for one is an answer that cannot be right
    # in the only place the list is read.
    tagged = {entry.account.number for entry in load_chart_entries(EXAMPLE_BOOKS.chart_path) if entry.no_bank_line}
    assert tagged == {"5203", "5217", "5218", "5220", "5221"}
    assert not tagged & set(_wanted())


def test_the_suspense_account_is_still_offered() -> None:
    # 5299 is the opposite case and the reason the exclusion is a per-account
    # tag rather than a rule about expense accounts: a person who cannot code a
    # statement line needs somewhere to put it, and this is that somewhere.
    assert "5299" in _wanted()


def test_the_member_capital_accounts_become_categories() -> None:
    # Named here only to assert the outcome; the selection itself uses the
    # 31xx range, so a fourth member is a chart edit and nothing else.
    assert {"3110", "3120", "3130"} <= set(_wanted())


def test_every_postable_revenue_account_becomes_a_category() -> None:
    revenue = [a for a in load_chart(EXAMPLE_BOOKS.chart_path) if not a.is_group and a.number.startswith("4")]
    assert revenue, "the chart must carry revenue accounts for this test to mean anything"
    assert {a.number for a in revenue} <= set(_wanted())


def test_groups_are_never_offered() -> None:
    # A transaction cannot post to a group, so a category naming one is a
    # category that codes a row to an account the ledger will refuse.
    groups = {a.number for a in load_chart(EXAMPLE_BOOKS.chart_path) if a.is_group}
    assert groups, "the chart must carry groups for this test to mean anything"
    assert not groups & set(_wanted())


def test_assets_liabilities_and_distributions_are_not_offered() -> None:
    wanted = set(_wanted())
    assert not {n for n in wanted if n.startswith(("1", "2"))}
    # 32xx is member distributions: not an expense, not payroll, and not
    # something a person codes at a card reader.
    assert not {n for n in wanted if n.startswith("32")}
    assert not {n for n in wanted if n.startswith("33")}


def test_an_expense_category_is_visible_everywhere() -> None:
    # Spend reaches us on a card, as a reimbursement, and as a wire or ACH.
    suspense = _wanted()["5299"]
    assert suspense.flags == {CARD_SPEND: True, REIMBURSEMENTS: True, OTHER: True}
    assert suspense.flag_label == "CRO"


def test_money_coming_in_is_visible_for_other_only() -> None:
    # A customer's invoice payment and a member's capital contribution both
    # arrive as a wire or an ACH credit. Neither is ever card spend or a
    # reimbursement, and offering them there only lengthens the list somebody
    # scrolls at a card reader with answers that cannot be right.
    for number in ("3110", "4110", "4210"):
        category = _wanted()[number]
        assert category.flags == {CARD_SPEND: False, REIMBURSEMENTS: False, OTHER: True}
        assert category.flag_label == "--O"


def test_a_category_is_named_exactly_as_the_gl_code_is() -> None:
    # The whole design rests on this: Mercury's rule engine maps a category to
    # a GL code, and the two lists have to agree string for string.
    by_number = {a.number: a for a in load_chart(EXAMPLE_BOOKS.chart_path)}
    for number, category in _wanted().items():
        assert category.name == gl_code_name(by_number[number])


def test_every_category_name_is_in_the_gl_code_list() -> None:
    # The category list is the shorter of the two: an account tagged
    # `no-bank-line` keeps its GL code, because a person reconciling in ERPNext
    # does reach it, and loses only the picker entry. A category naming a code
    # that is *not* in the list would be the broken direction.
    codes = set(gl_code_csv(load_chart(EXAMPLE_BOOKS.chart_path)).splitlines())
    assert {c.name for c in _wanted().values()} <= codes


def test_no_category_name_carries_a_comma() -> None:
    # The GL-code CSV a person uploads cannot carry one (Mercury's importer
    # splits the line), so a category that did could never be matched to a code.
    assert not [c.name for c in _wanted().values() if "," in c.name]


def test_the_selection_is_in_chart_order() -> None:
    # The plan doubles as a checklist somebody works through by hand.
    numbers = [c.number for c in desired_categories(load_chart_entries(EXAMPLE_BOOKS.chart_path))]
    assert numbers == sorted(numbers)


# --- planning --------------------------------------------------------------


def test_a_name_mercury_does_not_have_is_planned_as_a_create() -> None:
    want = _wanted()["5299"]
    plan = plan_categories([want], [])
    assert [c.desired.name for c in plan.creates] == [want.name]
    assert not plan.edits and not plan.matches and not plan.refusals


def test_matching_flags_are_neither_created_nor_edited() -> None:
    want = _wanted()["5299"]
    plan = plan_categories([want], [_live(want.name, card=True, reimbursements=True, other=True)])
    assert plan.matches == (want,)
    assert not plan.creates and not plan.edits
    assert not plan.has_drift


def test_a_differing_flag_is_planned_as_an_edit_naming_the_field() -> None:
    want = _wanted()["4210"]
    plan = plan_categories([want], [_live(want.name, card=True, reimbursements=False, other=True)])
    (edit,) = plan.edits
    assert edit.category_id == "cat-1"
    assert [str(c) for c in edit.changes] == ["visibleForCardSpend: true -> false"]
    assert not plan.creates


def test_an_edit_reports_every_flag_that_differs() -> None:
    want = _wanted()["5299"]
    plan = plan_categories([want], [_live(want.name, card=False, reimbursements=False, other=True)])
    (edit,) = plan.edits
    assert [c.field for c in edit.changes] == [CARD_SPEND, REIMBURSEMENTS]


def test_a_category_the_chart_does_not_name_is_unmanaged_and_never_planned_away() -> None:
    # Mercury's own ~23 defaults land here, and so does anything a person made.
    # Deleting one would unpick the coding of every transaction carrying it.
    mercurys_own = _live("Advertising & Marketing", card=True, reimbursements=True, other=True, uuid="cat-9")
    plan = plan_categories([], [mercurys_own])
    assert [u.category.id for u in plan.unmanaged] == ["cat-9"]
    assert not plan.creates and not plan.edits


def test_unmanaged_categories_alone_are_not_drift() -> None:
    # They are the steady state. A standing check that failed on them would be
    # a check nobody keeps running.
    want = _wanted()["5299"]
    plan = plan_categories(
        [want],
        [
            _live(want.name, card=True, reimbursements=True, other=True),
            _live("Travel", card=True, reimbursements=True, other=True, uuid="cat-9"),
        ],
    )
    assert plan.unmanaged
    assert not plan.has_drift


def test_a_renamed_account_creates_the_new_name_and_leaves_the_old_one_alone() -> None:
    want = _wanted()["5299"]
    stale = _live("5299 - Uncategorised", card=True, reimbursements=True, other=True, uuid="cat-old")
    plan = plan_categories([want], [stale])
    assert [c.desired.name for c in plan.creates] == [want.name]
    assert [u.category.id for u in plan.unmanaged] == ["cat-old"]
    # No rename is inferred from the shared number: a renumbered account keeps
    # its name and a re-purposed number keeps its number, so the guess is wrong
    # exactly when it matters.
    assert not plan.edits


def test_two_categories_with_one_desired_name_are_refused_with_both_uuids() -> None:
    want = _wanted()["5299"]
    plan = plan_categories(
        [want],
        [
            _live(want.name, card=True, reimbursements=True, other=True, uuid="cat-a"),
            _live(want.name, card=False, reimbursements=False, other=False, uuid="cat-b"),
        ],
    )
    (refusal,) = plan.refusals
    assert refusal.category_ids == ("cat-a", "cat-b")
    assert "cat-a" in str(refusal) and "cat-b" in str(refusal)
    # Neither is edited and neither is created: this cannot tell which one
    # transactions carry, and guessing would re-flag the wrong one.
    assert not plan.creates and not plan.edits
    assert plan.has_drift


def test_a_refusal_does_not_stop_the_rest_of_the_plan() -> None:
    duplicated = _wanted()["5299"]
    other = _wanted()["4210"]
    plan = plan_categories(
        [duplicated, other],
        [
            _live(duplicated.name, card=True, reimbursements=True, other=True, uuid="cat-a"),
            _live(duplicated.name, card=True, reimbursements=True, other=True, uuid="cat-b"),
        ],
    )
    assert plan.refusals
    assert [c.desired.number for c in plan.creates] == ["4210"]


def test_a_duplicated_name_is_not_also_reported_as_unmanaged() -> None:
    # One report line per category; the refusal already says what is wrong.
    want = _wanted()["5299"]
    plan = plan_categories(
        [want],
        [
            _live(want.name, card=True, reimbursements=True, other=True, uuid="cat-a"),
            _live(want.name, card=True, reimbursements=True, other=True, uuid="cat-b"),
        ],
    )
    assert not plan.unmanaged


def test_the_real_chart_plans_a_create_for_every_category_against_an_empty_mercury() -> None:
    wanted = desired_categories(load_chart_entries(EXAMPLE_BOOKS.chart_path))
    plan = plan_categories(wanted, [])
    assert len(plan.creates) == len(wanted)
    assert plan.has_work and plan.has_drift


# --- the writer ------------------------------------------------------------


def test_the_read_only_client_has_no_write_verb() -> None:
    # The ingest and the health report hold `MercuryClient`. The three writes
    # live on `MercuryCategoryWriter`, which holds a different token; a
    # refactor that moved any verb here would hand the ingest a capability it
    # has no use for -- `delete_category` most of all, since the ingest reads
    # the very rows a deletion strips the coding off.
    for verb in ("create_category", "edit_category", "delete_category", "post", "patch", "delete", "request"):
        assert not hasattr(MercuryClient, verb)


def test_the_writer_can_write_exactly_three_things() -> None:
    writes = {
        name
        for name in dir(MercuryCategoryWriter)
        if not name.startswith("_") and name not in {"close"} and callable(getattr(MercuryCategoryWriter, name))
    }
    assert writes == {"create_category", "edit_category", "delete_category"}


def _writer(handler) -> MercuryCategoryWriter:
    return MercuryCategoryWriter(SANDBOX, transport=httpx2.MockTransport(handler))


def _created(request: httpx2.Request, category_id: str) -> httpx2.Response:
    body = json.loads(request.content)
    return httpx2.Response(
        201,
        json={
            "id": category_id,
            "name": body.get("name", "unnamed"),
            "visibleForCardSpend": body[CARD_SPEND],
            "visibleForReimbursements": body[REIMBURSEMENTS],
            "visibleForOther": body[OTHER],
        },
    )


def test_a_create_sends_the_name_and_all_three_flags_and_returns_the_uuid() -> None:
    want = _wanted()["4110"]
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _created(request, "cat-new")

    with _writer(handler) as writer:
        written = writer.create_category(want)

    assert written.id == "cat-new"
    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{BASE_URLS[MercuryEnv.SANDBOX]}/categories"
    assert seen[0].headers["authorization"] == "Bearer SANDBOX-TOKEN"

    assert json.loads(seen[0].content) == {
        "name": want.name,
        CARD_SPEND: False,
        REIMBURSEMENTS: False,
        OTHER: True,
    }


def test_an_edit_sends_the_flags_and_never_the_name() -> None:
    # The name is the match key. Renaming here would be editing a different
    # account than the plan was about, and would silently re-label every
    # transaction already carrying the category.
    want = _wanted()["5299"]
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "cat-1",
                "name": want.name,
                CARD_SPEND: True,
                REIMBURSEMENTS: True,
                OTHER: True,
            },
        )

    with _writer(handler) as writer:
        writer.edit_category("cat-1", want)

    assert str(seen[0].url).endswith("/categories/cat-1")
    assert "name" not in json.loads(seen[0].content)
    assert set(json.loads(seen[0].content)) == {CARD_SPEND, REIMBURSEMENTS, OTHER}


def test_a_refused_write_raises_with_the_status() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, json={"error": "insufficient scope"})

    with _writer(handler) as writer, pytest.raises(MercuryError) as exc:
        writer.create_category(_wanted()["5299"])
    assert exc.value.status == 403


# --- applying --------------------------------------------------------------


def test_apply_sends_every_create_before_any_edit() -> None:
    # A run Mercury cuts short should have added categories rather than
    # half-edited existing ones: a missing category is the state the plan
    # already describes, a partly-edited one is a state nobody asked for.
    wanted = [_wanted()["5299"], _wanted()["4210"]]
    plan = plan_categories(wanted, [_live(wanted[1].name, card=True, reimbursements=False, other=True)])
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/categories"):
            return _created(request, "cat-new")
        return httpx2.Response(
            200, json={"id": "cat-1", "name": wanted[1].name, CARD_SPEND: False, REIMBURSEMENTS: False, OTHER: True}
        )

    with _writer(handler) as writer:
        applied = list(apply_plan(writer, plan))

    assert [a.action for a in applied] == ["created", "edited"]
    assert [a.category_id for a in applied] == ["cat-new", "cat-1"]
    assert paths == ["/api/v1/categories", "/api/v1/categories/cat-1"]


def test_apply_reports_what_landed_before_a_failure() -> None:
    # Silence is the worst failure mode: a run that wrote one thing and then
    # failed must not read as a run that wrote nothing.
    wanted = [_wanted()["5210"], _wanted()["5299"]]
    plan = plan_categories(wanted, [])
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return _created(request, "cat-new")
        return httpx2.Response(500, json={"error": "nope"})

    applied = []
    with _writer(handler) as writer, pytest.raises(MercuryError):
        for one in apply_plan(writer, plan):
            applied.append(one)

    assert [a.category_id for a in applied] == ["cat-new"]


def test_apply_writes_nothing_for_a_plan_with_no_work() -> None:
    want = _wanted()["5299"]
    plan = plan_categories([want], [_live(want.name, card=True, reimbursements=True, other=True)])

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"apply sent {request.method} {request.url.path} for a plan with no work")

    with _writer(handler) as writer:
        assert list(apply_plan(writer, plan)) == []


# --- which categories transactions carry ----------------------------------


def _transaction(transaction_id: str, *, category: str | None = None, category_id: str | None = None) -> Transaction:
    return Transaction(
        id=transaction_id,
        account_id="acct-1",
        amount=Decimal("-1.00"),
        status=Status.SENT,
        raw_status="sent",
        kind="outgoingPayment",
        created_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        custom_category=category,
        custom_category_id=category_id,
    )


def test_a_category_is_counted_once_per_transaction_carrying_it() -> None:
    counts = carried_categories([
        _transaction("t1", category="Insurance", category_id="cat-9"),
        _transaction("t2", category="Insurance", category_id="cat-9"),
        _transaction("t3", category="Transfer", category_id="cat-8"),
        _transaction("t4"),
    ])
    assert counts == {"cat-9": 2, "cat-8": 1}


def test_an_unsettled_transaction_counts_too() -> None:
    # A pending charge is somebody's coding as much as a settled one, and it
    # settles next week onto a category a prune would otherwise have deleted
    # underneath it.
    pending = dataclasses.replace(
        _transaction("t1", category="Insurance", category_id="cat-9"), status=Status.PENDING, raw_status="pending"
    )
    assert carried_categories([pending]) == {"cat-9": 1}


def test_a_category_named_with_no_readable_id_refuses_the_whole_count() -> None:
    # The one failure that would delete somebody's coding: if the id never
    # parses, every category looks uncarried and the unmanaged list becomes a
    # delete list. `categoryData`'s exact shape is an assumption, so the
    # contradiction is refused rather than trusted.
    with pytest.raises(CategoryUseUnreadableError) as exc:
        carried_categories([
            _transaction("t1", category="Insurance", category_id="cat-9"),
            _transaction("t2", category="Transfer"),
        ])
    assert "t2" in str(exc.value)


# --- pruning ---------------------------------------------------------------


def test_without_a_count_nothing_is_ever_a_delete() -> None:
    # `carried=None` is the default and is not the same as "nothing carries
    # anything": it says the bank was never asked.
    plan = plan_categories([], [_live("Travel", card=True, reimbursements=True, other=True, uuid="cat-9")])
    assert plan.unmanaged and not plan.deletes and not plan.in_use
    assert not plan.has_work and not plan.has_drift


def test_an_unmanaged_category_no_transaction_carries_is_planned_as_a_delete() -> None:
    plan = plan_categories([], [_live("Travel", card=True, reimbursements=True, other=True, uuid="cat-9")], carried={})
    assert [d.category.id for d in plan.deletes] == ["cat-9"]
    assert not plan.in_use
    assert plan.has_work


def test_an_unmanaged_category_transactions_carry_is_reported_with_its_count_and_kept() -> None:
    plan = plan_categories(
        [],
        [_live("Insurance", card=True, reimbursements=True, other=True, uuid="cat-9")],
        carried={"cat-9": 4},
    )
    (one,) = plan.in_use
    assert one.transactions == 4
    assert str(one) == "4 transaction(s)  Insurance"
    assert not plan.deletes
    # Not resolved: the dropdown still holds it, and only a person re-coding
    # those rows in Mercury changes that.
    assert plan.has_drift and not plan.has_work


def test_a_category_the_chart_manages_is_never_a_prune_candidate() -> None:
    want = _wanted()["5299"]
    plan = plan_categories(
        [want], [_live(want.name, card=True, reimbursements=True, other=True, uuid="cat-1")], carried={}
    )
    assert plan.matches == (want,)
    assert not plan.deletes and not plan.in_use and not plan.unmanaged


def test_a_duplicate_name_refusal_is_never_pruned() -> None:
    # Both UUIDs are claimed by the refusal, so neither becomes a delete --
    # one of them is what transactions carry and this cannot tell which.
    want = _wanted()["5299"]
    plan = plan_categories(
        [want],
        [
            _live(want.name, card=True, reimbursements=True, other=True, uuid="cat-a"),
            _live(want.name, card=True, reimbursements=True, other=True, uuid="cat-b"),
        ],
        carried={},
    )
    assert plan.refusals
    assert not plan.deletes and not plan.in_use


def test_apply_sends_every_delete_after_every_create_and_edit() -> None:
    # Everything before a delete can be undone by running again. A delete
    # cannot, so it goes last.
    want = _wanted()["5299"]
    plan = plan_categories(
        [want], [_live("Travel", card=True, reimbursements=True, other=True, uuid="cat-9")], carried={}
    )
    calls: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx2.Response(204)
        return _created(request, "cat-new")

    with _writer(handler) as writer:
        applied = list(apply_plan(writer, plan))

    assert calls == [("POST", "/api/v1/categories"), ("DELETE", "/api/v1/categories/cat-9")]
    assert [a.action for a in applied] == ["created", "deleted"]
    assert applied[-1].name == "Travel" and applied[-1].error is None


def test_a_refused_delete_is_reported_and_the_rest_still_go() -> None:
    # Mercury may protect some of its own defaults. One it will not part with
    # says nothing about the next, and stopping there would make the rest of
    # the list a mystery instead of a report.
    plan = plan_categories(
        [],
        [
            _live("Protected", card=True, reimbursements=True, other=True, uuid="cat-a"),
            _live("Travel", card=True, reimbursements=True, other=True, uuid="cat-b"),
        ],
        carried={},
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("cat-a"):
            return httpx2.Response(422, json={"error": "category is a Mercury default"})
        return httpx2.Response(204)

    with _writer(handler) as writer:
        applied = list(apply_plan(writer, plan))

    assert [a.action for a in applied] == ["refused", "deleted"]
    assert applied[0].error is not None and "422" in applied[0].error
    assert applied[1].category_id == "cat-b"


def test_a_delete_sends_no_body_and_names_the_uuid() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(204)

    with _writer(handler) as writer:
        writer.delete_category("cat-9")

    assert seen[0].method == "DELETE"
    assert str(seen[0].url) == f"{BASE_URLS[MercuryEnv.SANDBOX]}/categories/cat-9"
    assert seen[0].headers["authorization"] == "Bearer SANDBOX-TOKEN"
    assert not seen[0].content


def test_a_refused_delete_raises_with_the_status() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, json={"error": "insufficient scope"})

    with _writer(handler) as writer, pytest.raises(MercuryError) as exc:
        writer.delete_category("cat-9")
    assert exc.value.status == 403
