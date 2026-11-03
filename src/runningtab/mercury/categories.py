"""Mercury's custom categories, mirrored from the chart of accounts.

Mercury's rule engine derives a GL code from a transaction's category, and its
GL-code list is ours (`runningtab.gl_codes`). So the shortest path from "somebody
picked something in Mercury" to "the Bank Transaction arrives carrying the
right account" is to make the two lists the same list, string for string, and
let a person write one rule per category. Neither the GL-code list nor the
rules have an API; categories do.

Reconciliation is by **name**, because a name is the only thing the two sides
share -- nothing here keeps state between runs, so Mercury's category UUIDs are
not ours to remember. Three rules follow from that, and all three are about not
destroying somebody's coding:

- A Mercury category the chart does not name is **unmanaged**: reported, and
  never renamed. It is what transactions may already be coded with, and no
  re-run reproduces the judgement behind that.
- An edit only ever sets visibility flags. The name is the match key, so
  changing it would change which account the edit was about.
- Two Mercury categories with one desired name is **refused** with both UUIDs
  named. One of them carries the coding and this module cannot tell which.

Pruning is the one deletion this module can do, and it is guarded by that same
sentence. `--prune` deletes an unmanaged category **only when no transaction on
any account carries it**, measured by UUID against an exhaustive read of every
transaction -- settled or not. One that is carried is reported with its count
and left standing: a person re-codes those rows in Mercury, or leaves them, and
until they do the two lists disagree and the exit code says so. A delete strips
somebody's coding off rows that nothing reproduces, so the guard is not an
optimisation and the read behind it is not allowed to be short.

`MercuryCategoryWriter` at the bottom holds the only three writes this repo can
make to Mercury. It is a separate class from `MercuryClient` deliberately:
`runningtab.mercury.ingest` never imports this module, so the ingest's client keeps
its "no verb but GET" guarantee intact. That three-method surface is a
courtesy, not a boundary -- Mercury offers no category-only scope, so the token
behind it is a full Read and Write token that could also move money. What
bounds it is its lifetime; see `runningtab.site.mercury_admin_token`.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import httpx2

from runningtab.chart import ChartEntry
from runningtab.gl_codes import gl_code_name, postable_accounts
from runningtab.mercury.client import MalformedResponseError, MercuryConfig, MercuryError
from runningtab.mercury.models import Category, Transaction, parse_category

# Expenses. Spend arrives on a card, as a reimbursement, and as a wire or ACH,
# so every picker offers these.
EXPENSE_PREFIX = "5"
# Member capital contributions (31xx) and revenue (4xxx): money coming in. It
# arrives as a wire or an ACH credit, never as card spend and never as a
# reimbursement, so offering them in those two pickers would only lengthen the
# list somebody scrolls at a card reader with answers that cannot be right.
#
# `31` rather than the three member accounts by name: a fourth member is a
# chart edit, and naming 3110/3120/3130 here would leave their contributions
# with nowhere to go.
INCOMING_PREFIXES = ("31", "4")

# The API's own field names, which are also what the plan prints. A person
# reading an `edit` line is looking for one of these three checkboxes in the
# dashboard, so the report says exactly what the dashboard says.
CARD_SPEND = "visibleForCardSpend"
REIMBURSEMENTS = "visibleForReimbursements"
OTHER = "visibleForOther"


@dataclasses.dataclass(frozen=True)
class DesiredCategory:
    """One chart account as Mercury should hold it: the GL code's own label, and three flags."""

    number: str
    name: str
    visible_for_card_spend: bool
    visible_for_reimbursements: bool
    visible_for_other: bool

    @property
    def flags(self) -> dict[str, bool]:
        """The three visibility flags under the API's field names, in the order the plan prints them."""
        return {
            CARD_SPEND: self.visible_for_card_spend,
            REIMBURSEMENTS: self.visible_for_reimbursements,
            OTHER: self.visible_for_other,
        }

    @property
    def flag_label(self) -> str:
        """`CRO`, with `-` for each flag that is off -- the plan's compact column."""
        return "".join(letter if value else "-" for letter, value in zip("CRO", self.flags.values(), strict=True))


def live_flags(category: Category) -> dict[str, bool]:
    """A Mercury category's flags under the same field names, so the two sides compare directly."""
    return {
        CARD_SPEND: category.visible_for_card_spend,
        REIMBURSEMENTS: category.visible_for_reimbursements,
        OTHER: category.visible_for_other,
    }


def desired_categories(entries: list[ChartEntry]) -> list[DesiredCategory]:
    """One category per account a person can code a transaction to, in chart order.

    Derived from the chart rather than from a list kept beside it: a hand-kept
    list goes stale the first time an account moves, and its staleness is
    invisible. Groups are excluded for the reason `gl_codes` excludes them --
    nothing posts to a group, so offering one offers a mistake.

    So are accounts the chart tags `no-bank-line`. Depreciation, round-off, FX,
    write-offs and a disposal gain reach the ledger as a period-end or
    adjustment journal and never as a statement line, so a category for one is
    an answer that cannot be right in the only place the list is read -- the
    picker somebody scrolls at a card reader. They keep their GL codes:
    `coa export-gl-codes` still emits every postable account, because a person
    reconciling in ERPNext does reach them. What they lose is the category.
    """
    by_number = {entry.account.number: entry for entry in entries}
    wanted: list[DesiredCategory] = []
    for account in postable_accounts([entry.account for entry in entries]):
        if by_number[account.number].no_bank_line:
            continue
        if account.number.startswith(EXPENSE_PREFIX):
            card = reimbursements = other = True
        elif account.number.startswith(INCOMING_PREFIXES):
            card = reimbursements = False
            other = True
        else:
            continue
        wanted.append(
            DesiredCategory(
                number=account.number,
                name=gl_code_name(account),
                visible_for_card_spend=card,
                visible_for_reimbursements=reimbursements,
                visible_for_other=other,
            )
        )
    return wanted


@dataclasses.dataclass(frozen=True)
class CreateCategory:
    """A category the chart wants and Mercury does not have."""

    desired: DesiredCategory


@dataclasses.dataclass(frozen=True)
class FlagChange:
    """One visibility flag that disagrees, named as the dashboard names it."""

    field: str
    old: bool
    new: bool

    def __str__(self) -> str:
        return f"{self.field}: {str(self.old).lower()} -> {str(self.new).lower()}"


@dataclasses.dataclass(frozen=True)
class EditCategory:
    """A category Mercury has under the right name, with at least one flag wrong."""

    desired: DesiredCategory
    category_id: str
    changes: tuple[FlagChange, ...]


@dataclasses.dataclass(frozen=True)
class UnmanagedCategory:
    """A Mercury category the chart does not name. Reported, and without `--prune` left entirely alone."""

    category: Category


@dataclasses.dataclass(frozen=True)
class DeleteCategory:
    """An unmanaged category no transaction carries, so deleting it takes nobody's coding with it."""

    category: Category


@dataclasses.dataclass(frozen=True)
class InUseCategory:
    """An unmanaged category transactions carry. Counted, reported, and never deleted."""

    category: Category
    transactions: int

    def __str__(self) -> str:
        return f"{self.transactions} transaction(s)  {self.category.name}"


class CategoryUseUnreadableError(MercuryError):
    """Transactions carry custom categories whose UUID this read cannot see, so no deletion is safe.

    The one failure that would make `--prune` delete somebody's coding: if the
    id never parses, every category looks uncarried and the whole unmanaged
    list becomes a delete list. `categoryData`'s exact shape is recorded as an
    assumption rather than a measurement (see the fixture README), so the
    contradiction -- rows naming a custom category while none carries an id --
    is checked for and refused rather than trusted.
    """


def carried_categories(transactions: Iterable[Transaction]) -> dict[str, int]:
    """How many transactions carry each custom category, by UUID.

    Every transaction the read returned, whatever its status: a pending charge
    is somebody's coding too, and it settles next week onto a category this
    would otherwise have deleted underneath it.

    Raises:
        CategoryUseUnreadableError: if any transaction names a custom category
            without an id this can read. One such row means the read cannot
            answer "does anything carry this category", and a prune that ran
            anyway would delete on the strength of a field it failed to parse.
    """
    counts: dict[str, int] = {}
    unreadable: list[str] = []
    for transaction in transactions:
        if transaction.custom_category_id:
            counts[transaction.custom_category_id] = counts.get(transaction.custom_category_id, 0) + 1
        elif transaction.custom_category:
            unreadable.append(transaction.id)
    if unreadable:
        raise CategoryUseUnreadableError(
            f"{len(unreadable)} transaction(s) carry a custom category with no id this client can read "
            f"(first: {unreadable[0]}); which categories are in use cannot be answered, so nothing is deleted"
        )
    return counts


@dataclasses.dataclass(frozen=True)
class DuplicateName:
    """Two Mercury categories wearing one desired name, which this module will not choose between."""

    name: str
    category_ids: tuple[str, ...]

    def __str__(self) -> str:
        return (
            f"{len(self.category_ids)} Mercury categories are named {self.name!r} "
            f"({', '.join(self.category_ids)}); transactions carry one of them and this cannot tell which. "
            f"Merge or rename them in the dashboard."
        )


@dataclasses.dataclass(frozen=True)
class CategoryPlan:
    """What the chart wants against what Mercury has, as work rather than as a diff."""

    creates: tuple[CreateCategory, ...]
    edits: tuple[EditCategory, ...]
    matches: tuple[DesiredCategory, ...]
    unmanaged: tuple[UnmanagedCategory, ...]
    refusals: tuple[DuplicateName, ...]
    # Both empty unless the caller asked to prune, which is the only way an
    # unmanaged category is ever classified rather than merely listed.
    deletes: tuple[DeleteCategory, ...] = ()
    in_use: tuple[InUseCategory, ...] = ()

    @property
    def has_work(self) -> bool:
        """Whether there is anything for `apply` to send."""
        return bool(self.creates or self.edits or self.deletes)

    @property
    def has_drift(self) -> bool:
        """Whether the two lists disagree at all.

        Unmanaged categories on their own are the steady state, not drift. An
        unmanaged category a prune could not take because transactions carry it
        *is* drift: the dropdown still holds it, and only a person re-coding
        those rows in Mercury resolves that.
        """
        return self.has_work or bool(self.refusals or self.in_use)


def plan_categories(
    desired: list[DesiredCategory], live: list[Category], *, carried: Mapping[str, int] | None = None
) -> CategoryPlan:
    """Reconcile the chart's categories against Mercury's, by name.

    Args:
        desired: What the chart wants, in chart order, from `desired_categories`.
        live: Every category Mercury holds, read to the end of the list.
        carried: Transaction counts per category UUID, from `carried_categories`,
            when the caller asked to prune. `None` -- the default -- plans no
            deletion at all, and every unmanaged category is simply reported.
            An empty mapping is not the same thing: it says the bank was read
            and nothing carries any category.

    Returns:
        The plan. Creates and edits keep chart order so the report reads as a
        checklist; unmanaged categories keep Mercury's own order.
    """
    by_name: dict[str, list[Category]] = {}
    for category in live:
        by_name.setdefault(category.name, []).append(category)

    creates: list[CreateCategory] = []
    edits: list[EditCategory] = []
    matches: list[DesiredCategory] = []
    refusals: list[DuplicateName] = []
    claimed: set[str] = set()

    for want in desired:
        found = by_name.get(want.name, [])
        if len(found) > 1:
            # Named, not chosen between. Both are claimed so neither is also
            # reported as unmanaged -- one report line per category, and this
            # one already says what is wrong with them.
            refusals.append(DuplicateName(name=want.name, category_ids=tuple(c.id for c in found)))
            claimed.update(c.id for c in found)
            continue
        if not found:
            creates.append(CreateCategory(desired=want))
            continue
        current = found[0]
        claimed.add(current.id)
        held = live_flags(current)
        changes = tuple(
            FlagChange(field=field, old=held[field], new=value)
            for field, value in want.flags.items()
            if held[field] != value
        )
        if changes:
            edits.append(EditCategory(desired=want, category_id=current.id, changes=changes))
        else:
            matches.append(want)

    unmanaged = tuple(UnmanagedCategory(category=c) for c in live if c.id not in claimed)
    deletes: list[DeleteCategory] = []
    in_use: list[InUseCategory] = []
    if carried is not None:
        for one in unmanaged:
            count = carried.get(one.category.id, 0)
            if count:
                in_use.append(InUseCategory(category=one.category, transactions=count))
            else:
                deletes.append(DeleteCategory(category=one.category))

    return CategoryPlan(
        creates=tuple(creates),
        edits=tuple(edits),
        matches=tuple(matches),
        unmanaged=unmanaged,
        refusals=tuple(refusals),
        deletes=tuple(deletes),
        in_use=tuple(in_use),
    )


@dataclasses.dataclass(frozen=True)
class AppliedCategory:
    """One write, with the UUID it concerned -- the only record of it this repo sees.

    `error` is set only on a delete Mercury refused. A refused create or edit
    stops the run, because everything after it was planned against a state that
    did not happen; a refused delete stops nothing, because each delete stands
    alone and Mercury may simply protect some of its own defaults.
    """

    action: str
    name: str
    category_id: str
    error: str | None = None


class MercuryCategoryWriter:
    """The only three writes this repo can make to Mercury: create a category, set one's flags, delete one.

    Separate from `MercuryClient` on purpose. That class has no verb but GET
    and is what the ingest and the health report hold; this one exists for a
    single command and holds a different token.

    The three-method surface stops a stray call at the type level, which is
    worth having, but it is not the safety property. Mercury issues no
    category-only write scope, so the token here is a full Read and Write token
    that can also send money -- `runningtab.site.mercury_admin_token` reads it from
    the environment and never from Secret Manager, and it is revoked after the
    run.

    `delete_category` is the one irreversible verb here, and nothing in this
    class guards it: the guard is in the plan. `plan_categories` proposes a
    delete only for a category an exhaustive transaction read found nobody
    carrying, and `runningtab mercury categories apply` sends one only under
    `--prune`. Calling this method on a category transactions carry would strip
    their coding, and no re-run reproduces it.
    """

    def __init__(self, config: MercuryConfig, *, transport: httpx2.BaseTransport | None = None) -> None:
        self._config = config
        self._http = httpx2.Client(timeout=config.timeout, transport=transport)

    def __enter__(self) -> MercuryCategoryWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._http.close()

    def _post(self, path: str, body: dict[str, Any]) -> Category:
        response = self._http.request(
            "POST",
            f"{self._config.base_url.rstrip('/')}{path}",
            json=body,
            headers={"Authorization": f"Bearer {self._config.token}", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise MercuryError(
                f"POST {path} -> {response.status_code}: {response.text[:300]}", status=response.status_code
            )
        try:
            return parse_category(json.loads(response.text))
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponseError(
                f"{path} answered {response.status_code} with a body this client cannot read: {exc!r}"
            ) from exc

    def create_category(self, desired: DesiredCategory) -> Category:
        """Create one category. Mercury answers with it, including the UUID it assigned."""
        return self._post("/categories", {"name": desired.name, **desired.flags})

    def edit_category(self, category_id: str, desired: DesiredCategory) -> Category:
        """Set one category's three visibility flags.

        Sends the flags and never `name`. The name is how this module found the
        category in the first place, so renaming it here would be editing a
        different account than the one the plan was about -- and would silently
        re-label every transaction already coded with it.
        """
        return self._post(f"/categories/{category_id}", dict(desired.flags))

    def delete_category(self, category_id: str) -> None:
        """Delete one category. Irreversible, and it takes the coding off every transaction carrying it.

        Only ever called for a `DeleteCategory` the plan produced, which means
        an exhaustive read of every transaction on every account found none
        carrying this UUID.
        """
        response = self._http.request(
            "DELETE",
            f"{self._config.base_url.rstrip('/')}/categories/{category_id}",
            headers={"Authorization": f"Bearer {self._config.token}", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise MercuryError(
                f"DELETE /categories/{category_id} -> {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )


def apply_plan(writer: MercuryCategoryWriter, plan: CategoryPlan) -> Iterator[AppliedCategory]:
    """Send the plan's creates, then its edits, then its deletes, yielding each result.

    Creates first so that a run Mercury cuts short has added categories rather
    than half-edited them: a missing category is the state the plan already
    describes, while a partly-edited one is a new state nobody asked for.
    Deletes last for the sharper version of the same reason -- everything
    before them is recoverable by running again, and they are not.

    A generator rather than a list so the caller prints each write as it lands.
    A refused create or edit raises `MercuryError`, and everything before it
    has already been reported -- a run that wrote four things and then failed
    must not look like a run that wrote nothing.

    A refused **delete** is yielded as a report line carrying the error and the
    run continues. Mercury may protect some of its own defaults, and one
    category it will not part with says nothing about the next; stopping there
    would make the rest of the list a mystery instead of a report. The caller
    exits 1 on one.
    """
    for create in plan.creates:
        written = writer.create_category(create.desired)
        yield AppliedCategory(action="created", name=written.name, category_id=written.id)
    for edit in plan.edits:
        written = writer.edit_category(edit.category_id, edit.desired)
        yield AppliedCategory(action="edited", name=written.name, category_id=written.id)
    for delete in plan.deletes:
        category = delete.category
        try:
            writer.delete_category(category.id)
        except MercuryError as exc:
            yield AppliedCategory(action="refused", name=category.name, category_id=category.id, error=str(exc))
            continue
        yield AppliedCategory(action="deleted", name=category.name, category_id=category.id)
