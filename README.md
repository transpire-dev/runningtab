# runningtab

Keeps a company's books in ERPNext: applies a versioned chart of accounts, runs
the Mercury ingest that posts bank activity into the ledger as Bank Transactions
for a person to reconcile, forecasts cash, and closes quarters.

[Source on GitHub](https://github.com/transpire-dev/runningtab)

## Installation

Requires Python 3.14. To include pre-releases:

```sh
pip install --pre runningtab
# or
uv add --prerelease allow runningtab
```

## The books directory

runningtab ships no books. Every command reads one **books directory**, named by
`--books DIR` (before the command: `runningtab --books DIR coa plan ...`) or by
`RUNNINGTAB_BOOKS`. There is no default: with neither set, every command refuses
and names both, because a fallback would plan against books nobody meant.
`examples/books/` is a complete example, and the one this package's test suite
runs against. A books directory holds:

| File            | What it is                                                                                                                                                                |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `chart.coa`     | the chart of accounts                                                                                                                                                     |
| `mercury.toml`  | which chart account and ERPNext Bank Account each Mercury account is                                                                                                      |
| `forecast.toml` | the expected cash movements `forecast` reads                                                                                                                              |
| `qbo.toml`      | what QuickBooks Online needs to know about the chart, for `period export`                                                                                                 |
| `books.toml`    | the books' name, the ledger and test-site hosts, Secret Manager secret ids, record buckets, the bot and reader identities, the fence probe role, and the chart doc's path |

`books.toml` is read strictly: an unknown key is refused by its dotted name, and
so is a missing one. Its `site.test_hosts` may not name `site.host`. See
`examples/books/books.toml` for every key.

The chart is a versioned file (`chart.coa`), not something edited in the ERPNext
UI. It is an indented outline, not TOML: each account is one line — number,
name, and an optional `[tag]` block holding its root type, its ERPNext account
type, and the reserved word `no-bank-line` (this account is only ever reached by
a period-end or adjustment journal, so `mercury categories` offers no category
for it) — nested two spaces per level under its parent, with `>`-prefixed lines
carrying a note. The page `books.toml`'s `chart_doc` names is the annotated
version — what each account is for, and how to choose between neighbours — and
its account tables are **generated** from `chart.coa` by `chart render-doc`, not
maintained by hand; only its prose sections are edited directly.

- `chart.py` — loads and validates the chart: unique numbers, every parent
  present, no cycles, a root type inherited from each of the five roots, and
  account types ERPNext actually accepts. Raises `ChartError` describing exactly
  what is wrong rather than failing partway through an apply.
- `load_defaults` — the Company default-account fields (default bank account,
  default receivable account, and so on), as account numbers, from the chart's
  `[defaults]` block.
- `books.py` — finds and loads the books directory, and refuses a `books.toml`
  it cannot trust.
- `doc.py` — renders the chart doc's account tables from the chart, rewriting
  only the content between `<!-- chart:table N -->` / `<!-- chart:defaults -->`
  and `<!-- chart:end -->` marker pairs.
- `coa.py`, `access.py` — the chart orchestration and the bot policy behind the
  commands below; `cli.py` is the typer wiring that prints and prompts.
- `mercury/` — the bank. `client.py` reads Mercury (GET only), `categories.py`
  mirrors the chart into Mercury's custom categories and holds the only three
  writes this package can make, `models.py` parses its payloads into `Decimal`
  money, `classify.py` decides with no I/O at all whether a transaction is
  settled enough to post, `posting.py` builds and submits the Bank Transactions,
  `vouchers.py` drafts the Journal Entry a coded row will be matched to,
  `receipts.py` puts the receipt a person uploaded at the bank onto that row,
  `state.py` holds the cursor and the dedup set, and `ingest.py` is the applier
  a webhook will later feed. `mercury.toml` names the chart account and the
  ERPNext Bank Account behind each Mercury account. There is one client:
  `runningtab health` reads the bank through the same `client.py` and the same
  `ingest.reconcile` the ingest does, so the two cannot drift apart.
- `health.py` — the five checks behind `runningtab health`, and the rule that
  turns them into an exit code.

For `chart.coa` syntax highlighting in Zed, see `zed-coa/README.md`.

Depends on `icedcoffee-sync` for the account model (`DesiredAccount`), the
diff/apply/ prune engine, and ERPNext facts (`ROOT_TYPES`, `ACCOUNT_TYPES`);
`icedcoffee-sync` does not depend back.

## The `coa` commands

- `coa plan --target <target> [--company NAME]` — diff the chart against the
  site and print what would change. Writes nothing; exits non-zero if the plan
  carries a refusal, so it also works as a CI-style gate.
- `coa apply --target <target> [--company NAME] [--yes]` — apply the plan
  (create, renumber, retype, re-enable), then point the Company record's
  default-account fields at the chart. Prompts unless `--yes`; re-plans after
  the prompt and refuses if anything changed meanwhile; checks the default
  fields exist on Company before touching any account. Run it before
  `coa prune`.
- `coa prune --target <target> [--company NAME] [--yes] [--allow-unapplied]` —
  **the only destructive command.** Deletes (or, where ERPNext refuses,
  disables) accounts the chart does not describe. Refuses until the chart is
  applied, since pruning first would strand the Company defaults; refuses
  outright on a `ROOT_NOT_UNIQUE` or `AMBIGUOUS_MATCH` refusal, the two that
  make the extras set untrustworthy. `--allow-unapplied` is the teardown path
  for a pre-existing chart whose numbers collide with ours: it clears the
  Company defaults pointing at accounts it deletes and parks any survivor still
  holding a chart number onto a 9-prefixed one (ERPNext keeps some sample
  accounts alive by name until apply repoints the defaults), so `coa apply` can
  run and a closing `coa prune` retires them.
- `coa export-gl-codes [--output PATH]` — emit the single-column CSV of
  non-group account names the bank accepts for its GL-code picker. Writes to
  stdout by default; the upload into the bank's dashboard is manual. Every label
  is **comma-free** — Mercury's importer splits a line at the first comma
  whatever the quoting says, so a thousands separator is dropped and any other
  comma becomes a spaced slash. The ERPNext account name is untouched, so a GL
  code in the bank can read differently from the account it stands for.

## The `chart` commands

- `chart render-doc [--check]` — regenerate the chart doc's (`books.toml`'s
  `chart_doc`) account tables from `chart.coa`. Without `--check`, writes the
  file and reports whether it changed. With `--check`, writes nothing and exits
  1 with a unified diff if the doc is stale.

## `health`

`health --target <target> [--company NAME] [--strict] [--mercury-mapping PATH]`
— whether the books are actually current. Reads only. The failures it looks for
are silent ones: a webhook the bank disabled, a cursor that stopped advancing,
entries piling up in suspense. `--mercury-mapping` is
`period export`/`period status`'s own option, needed for the same reason there:
the `closed periods` section rebuilds each stored quarter, and `--target test`
reads the Mercury sandbox, whose account ids are not in the books directory's
`mercury.toml`.

Six named sections, always printed, in this order:

| Section                       | State                                                                                                                                                                                                                                                                    |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `balance invariant`           | **runs.** Per account, the transactions that are neither pending nor failed must sum to the bank's `currentBalance`                                                                                                                                                      |
| `uncoded spend, aged`         | not available: settled rows with no GL code at Mercury; `runningtab mercury sync` reports them per run as `uncoded`, and this section has nothing persisted to read them from between runs                                                                               |
| `suspense contents`           | **runs.** What sits in `5299 UNCATEGORISED (needs review)`, and how long it has been there                                                                                                                                                                               |
| `webhook subscription health` | not available: needs a registered subscription to report the status of                                                                                                                                                                                                   |
| `event cursor age`            | not available: needs the ingest's persisted last-processed event id                                                                                                                                                                                                      |
| `closed periods`              | **runs.** `runningtab period status`'s own report, folded in: a reopened quarter, a quarter locked with no stored package, an unfinished re-close, `role_allowed_for_frozen_entries` left set, or a closed quarter's rebuilt files no longer matching its stored package |

The balance invariant reads **per account**, not through the bank's org-wide
transaction endpoint: only the per-account response carries a `total`, and
without one a short read is undetectable. Pages are joined on transaction id, so
a server that ignores `offset` produces a reported short read rather than a sum
counting the same charge twice. It reads the credit endpoint too, because the
account list omits credit accounts entirely.

Which statuses `currentBalance` counts was **measured**, not read off the enum:
`sent` counts, `pending` and `failed` do not. `cancelled`, `reversed`, and
`blocked` exist and have never been observed. The sum treats them as counting,
which is what "the balance is the bank's number" implies and what the ingest
already does, and this check **fails by name** the moment one appears — deciding
silently either way is how a real discrepancy gets explained away.

Suspense ages from the **last time the account was empty**, not from its oldest
entry, so a reclassified account is not reported as ancient, and it lists only
the entries behind the current balance. It reads the ledger, which holds
submitted documents only. Nothing machine-made lands in suspense: the ingest
posts Bank Transactions, which reach the general ledger only when a person
reconciles them, so an entry against `5299` is a voucher a person reconciled a
statement line to because they could not yet say where it belongs — and a
person's voucher is submitted, so this section sees it.

`closed periods` is `runningtab period status`'s report (see below), reached as
the reader — the same credential `period export` uses, its effective grants
proven the same way before anything is compared. It rebuilds every closed
quarter through that exact same path and diffs it byte for byte against the
stored package, so a section that passes here means every handed-off quarter
still reconciles with the ledger as it stands today, not merely as it stood when
it was closed.

Exit codes:

| Outcome                                                                                                                  | Default | `--strict` |
| ------------------------------------------------------------------------------------------------------------------------ | ------- | ---------- |
| every section passes                                                                                                     | `0`     | `0`        |
| a section is out of bounds (`FAIL`)                                                                                      | `1`     | `1`        |
| a section could not run (`ERROR`) — credential unset or refused, site unreachable, no account, a response it cannot read | `1`     | `1`        |
| a section is not built yet (`N/A`)                                                                                       | `0`     | `1`        |

`N/A` is only ever the three sections above that need the ingest. A section that
exists and did not run is an `ERROR`, an unset credential included: the report
cannot say whether the books are current without it, and every such message
names what to set or create.

The bank credential pairs with the target. `--target test` reads the bank's
sandbox, with the token in `MERCURY_SANDBOX_TOKEN` — created inside the sandbox
dashboard, since a production token does not work there. `--target books` reads
the real organisation through the read-only token in the Secret Manager secret
`books.toml` names as `secrets.mercury`.

## The `period` commands

`period export --target <target> --quarter <YYYYQn> [--company NAME] [--output DIR] [--mercury-mapping PATH]`
builds one quarter's books into a package for an accountant or auditor who has
never worked with you. Reads only, against both outside systems — the ledger (GL
Entries, Fiscal Years, and every voucher and Bank Transaction dated in the
quarter, plus the receipts and invoices attached to them) and the bank (each
Mercury account's balance and transactions) — and writes one new local
directory, `<quarter>-DRAFT/` by default (`--output` names another). It never
overwrites: an existing directory at that path is refused rather than mixed with
or replaced, since a person may already be working in it.

`--mercury-mapping PATH` loads a `mercury.toml`-shaped mapping from `PATH`
instead of the books directory's `mercury.toml`, which names only the real
Mercury accounts; `--target test` reads the Mercury sandbox, whose account ids
are not in that file, so exporting against it needs a mapping of its own.

The directory holds `MANIFEST.md` (the package explained in plain sentences, for
the reader), `SHA256SUMS`, the trial balance and general ledger, the profit and
loss statement and balance sheet, open receivables and payables, the bank
reconciliation, a QuickBooks Online import kit under `qbo/`, and the attachments
under `documents/`.

Exit codes:

| Outcome                                                               | Exit code |
| --------------------------------------------------------------------- | --------- |
| every Mercury account reconciles with the ledger at the quarter's end | `0`       |
| the bank and the ledger disagree at the quarter's end                 | `1`       |
| the package could not be built at all (nothing is written)            | `1`       |
| `--quarter` is not `YYYYQn`                                           | `2`       |

A bank difference still writes the draft — it is what a person investigates with
— but a package that cannot be built (an account `chart.coa` does not describe,
a credential problem, an unreachable site) writes nothing at all, the same
"refuse rather than hand over something wrong" rule `coa apply` follows.

`period close --target <target> --quarter <YYYYQn> [--company NAME] [--mercury-mapping PATH]`
runs the close ceremony and locks the quarter: read Books Closed Through and the
ledger, cross-check the draft against ERPNext's own Trial Balance and AR/AP
reports account by account and party by party, move Books Closed Through to the
quarter's last day, rebuild the package and require it byte-identical to the
pre-lock build, then upload it to the record bucket and read every object back
to verify it. Runs as an administrator: `--target books` needs
`RUNNINGTAB_ADMIN_TOKEN` (your own System Manager `key:secret`), refused by name
before any request when it is unset — no stored credential can close a quarter;
`--target test`'s environment credential is already an administrator's. A
failure before the lock leaves nothing changed. A failure after it leaves the
lock in place (there is no rollback), prints Books Closed Through, and names the
next step. On a quarter's first close, running `period close` again is the
recovery: it finds the lock already at the quarter's end, re-runs readiness and
the cross-check against the now-locked ledger, and stores the package. Check
`period status` first: an error can arrive after the package landed, and a
re-run would then only be refused as already stored. On a re-close of a quarter
that already has a stored package, a re-run is refused ("already has a stored
package"), so the recovery is to check `period status` (an error can arrive
after the new revision landed), then reopen the quarter, close it again, and
re-close every stored quarter after it in order. When the ledger moved between
the check and the lock, the refusal names every differing file and the vouchers
whose general-ledger rows differ. If what posted belongs in the quarter, run
`close` again (reopening first on a re-close); if it does not, reopen the
quarter and reverse it first. The finished package lands in
`<company-abbr>/<quarter>/` (`revN/` beside the original for a reopened
quarter's re-close; nothing is ever deleted or overwritten, because the bucket
is under a retention policy) in the bucket `books.toml`'s `record_buckets.books`
names (`record_buckets.test` for `--target test`).

`period close`, `period status`, and `health` (whose `closed periods` section
reads the record bucket) need Application Default Credentials
(`gcloud auth application-default login`) for an identity in the engineering
group, which holds the buckets' object-create and object-viewer grants.

`period status --target <target> [--company NAME] [--mercury-mapping PATH]`
reads only, as the reader — the same credential and effective-grant proof
`period export` uses. Prints Books Closed Through, then one line per closed
quarter (`ok`, or the changed files when its rebuild disagrees with the stored
`SHA256SUMS`), then every problem found; exits 1 unless everything comes back
clean. Every closed quarter's latest revision is rebuilt through the exact same
read-and-build path `period export` uses and compared byte for byte against what
is stored — package bytes only, nothing is written anywhere. Also detects a
reopened quarter (Books Closed Through moved backward, naming who and when from
the Company `Version` history), a quarter locked with no stored package (Books
Closed Through past every stored quarter, which a close that locked and then
failed to store leaves behind), an unfinished re-close (Books Closed Through set
to a stored quarter's end by a newer Company `Version` than the one its stored
`MANIFEST.md` records, so every file can still match the old revision), and
`role_allowed_for_frozen_entries` left set. Reopening is a person, in the
ERPNext UI, and nothing else — there is no `period reopen` command. To reopen a
quarter, move Books Closed Through back to the previous quarter's end. Any
stored quarter can be reopened, not only the latest: re-close it, then every
stored quarter after it, in order (reopening 2026Q2 while 2026Q3 is stored means
closing 2026Q2, then 2026Q3, each as a new revision). Until then `status` (and
`health`'s `closed periods` section, above) fail and name that order. The two
share one report: `health --target <target>`'s `closed periods` section, above,
is this same check run the same way.

Bank activity dated in a closed quarter that reaches Mercury after the close
changes that quarter's rebuilt `bank-reconciliation.csv`. `mercury sync` names
each such row and drafts no voucher for it. Reopen that quarter, book the row
there (the next sync drafts its voucher once the quarter is open), and close it
again, then re-close any stored quarter after it. Booking it in the open quarter
instead would leave the closed quarter's package differing from its rebuild for
good.

`period export` runs as the reader, the same credential `coa plan` and `health`
use. Its role needs more than they do — the ledger's parties, the fiscal years,
and every voucher doctype whose attachments a private-file download is checked
against — so after this lands, re-provision the reader with
`runningtab access provision --target books --profile reader` (and
`--target test`) with `RUNNINGTAB_ADMIN_TOKEN` set, or its stored credential
will still be missing the new grants.

## The `mercury` commands

Read the bank and post every settled transaction into ERPNext as a **Bank
Transaction** on the Bank Account of the Mercury account it moved through,
submitted, for a person to reconcile in the Banking workspace (Bank Statement
Import and the Bank Reconciliation Tool). The ingest is read-only against
Mercury — `runningtab.mercury.client` has no verb but GET. The one command here
that writes to Mercury is `mercury categories apply`, which touches categories
and nothing else; see below. Each row is written to be read on the workspace's
card, which shows the date, `reference_number`, `description` and the amount and
nothing else. The reference is the ACH trace number Mercury reports as
`trackingNumber`, so an ingested row and one imported by hand carry the same
"Ref"; a transaction with no trace number — a card charge never went through the
ACH network — carries the Mercury transaction id instead. The description is the
person's Mercury note, then the GL code, then the bank's own text, joined by a
spaced middle dot from whichever of the three exist, so a coded row reads
without being opened and an uncoded one reads exactly as the bank wrote it. Both
are fixed when the row is posted. Mercury's GL code and note also ride along in
custom fields of their own, which stay the live copy — a later edit at Mercury
lands there by sweep — and they route nothing. An internal transfer is two
statement lines on two accounts and becomes two Bank Transactions, which the
tool matches to one transfer voucher. A row with a category nobody mapped, or no
category at all, is just a Bank Transaction with whatever hint Mercury has. The
event log is the authority for every change.

- `mercury install-fields --target <target> [--yes]` — create the four custom
  fields on Bank Transaction: `mercury_transaction_id`,
  `mercury_resource_version`, `mercury_gl_code` and `mercury_note`. Run once,
  before the first ingest, as an administrator (`RUNNINGTAB_ADMIN_TOKEN`, see
  Access): the books bot has no Custom Field permission. The transaction id is
  **unique**, so the server itself refuses a second document for the same
  transaction. The GL code and note are what a person coded in Mercury — the
  account they expect to pick at reconciliation, carried verbatim as
  `<number> - <name>`, and their free-text note. The ingest fills both; a
  statement export imported by hand through Bank Statement Import maps its
  `GL Code` and `Note` columns onto them, which is why they are writable rather
  than read-only. Nothing acts on either; they are hints for the person
  reconciling, and the row's `description` carries both so the hint reaches the
  card as well as the field. All four are `allow_on_submit`, so the ingest can
  stamp them onto a row a person already submitted (see adoption below). It also
  creates `mercury_transaction_id` on **Journal Entry**, which is how the draft
  voucher beside a coded row is found again (see below); that one is
  deliberately **not** `allow_on_submit`, because nothing ever writes to a
  voucher a person has submitted, and it carries no `mercury_resource_version`
  because a GL code edit bumps none. It is also **not unique**: ERPNext's Amend
  copies it onto the amendment while the cancelled original still holds it, so a
  unique index would refuse every correction to our vouchers. The ingest refuses
  a second _live_ voucher for one transaction instead, and treats a cancelled
  one as history. Re-running compares and repairs an existing field whose flags
  differ, so fields installed before a flag was added are brought up to date
  rather than assumed correct.
- `mercury install-bank-accounts --target <target> [--yes]` — create the Bank
  "Mercury" and one Bank Account per Mercury account named in `mercury.toml`'s
  `[bank_accounts]`, each linked to the chart account `[accounts]` gives it.
  Also administrator-only, for the same reason. Re-running compares and repairs
  the links rather than duplicating; an entry that does not follow ERPNext's
  `<account name> - Mercury` naming is refused before anything is created.
- `mercury snapshot --env <env> --target <target> [--state PATH] [--dry-run]` —
  establish initial state from the transaction lists and record where in the
  event log that state was true. Also the recovery path: events are retained 90
  days, so a gap wider than that is closed by re-snapshotting.
- `mercury sync --env <env> --target <target> [--state PATH] [--dry-run]` —
  apply every event after the cursor, then refresh Mercury's GL code and note on
  every settled row the run read, because those two are edited with no event
  behind them (see below). Deduplicates on the event id and applies only when
  `resourceVersion` advances, so a redelivery does nothing and an out-of-order
  delivery cannot rewind. Refuses a cursor older than the retention window
  instead of resuming into a hole. **The webhook receiver is not built yet**;
  when it is, its consumer calls the same
  `runningtab.mercury.ingest.apply_event` with the same state file, and the two
  paths need no coordination.
- `mercury status --env <env> --target <target> [--state PATH]` — the cursor,
  its age, and how many events have been applied. Exits 1 when the cursor is
  past retention.
- `mercury categories plan --env <env> [--prune]` and
  `mercury categories apply --env <env> [--yes] [--prune]` — mirror the chart's
  codeable accounts as Mercury's **custom categories**, named with exactly the
  GL-code label `coa export-gl-codes` emits, so one dashboard rule per category
  maps a category to the GL code of the same name and rows arrive coded.
  Postable `5xxx` accounts get all three visibility flags; postable `31xx`
  (member capital) and `4xxx` (revenue) are money coming in and get
  `visibleForOther` only. An account the chart tags `no-bank-line` gets no
  category at all — 5203 depreciation, 5217 round-off, 5218 FX, 5220 write-offs
  and 5221 disposal gain are fed by period-end and adjustment journals, never by
  a statement line, so offering them at a card reader offers an answer that
  cannot be right; they keep their GL codes, because ERPNext reconciliation does
  reach them. Reconciled **by name**: a missing name is a create, a wrong flag
  is an edit, a category the chart does not name is reported as `unmanaged` and
  never renamed, and two categories sharing one desired name are refused with
  both UUIDs named rather than guessed between. Renaming a chart account
  therefore creates a new category and leaves the old one unmanaged; no rename
  is inferred. `plan` writes nothing, uses the read-only token, and exits 1 on
  any drift, so it works as a standing check; its output is also a checklist a
  person can work through by hand.
- **`--prune` shortens the dropdown, and never at the cost of somebody's
  coding.** It deletes an unmanaged category — Mercury's 22 shipped defaults
  among them — only when **no transaction on any account carries it**, measured
  by category UUID against the same exhaustive, short-read-refusing read the
  ingest uses, counting unsettled rows too. One that is carried is reported as
  `in use  <n> transaction(s)  <name>` and left standing; re-code those rows in
  Mercury (or leave them) and run again. The exit code stays 1 while any remain,
  because the dropdown still holds them. Deletes go last, after the creates and
  edits, one request each; a delete Mercury refuses is one report line and the
  rest proceed. Without `--prune` nothing is ever deleted. `plan --prune` shows
  the same list and writes nothing.

**`apply` is a ceremony, not an automation.** Mercury's custom-token scope
picker offers no category **write** scope — only "Fetch Categories" — so the
three write endpoints are reachable solely from a full **Read and Write** token,
whose server-side boundary is "may send money" rather than "may edit
categories". That token is therefore never stored: not in Secret Manager, not in
a file. `apply` reads it from `MERCURY_ADMIN_TOKEN` (or, for the sandbox,
`MERCURY_SANDBOX_TOKEN`) and from nowhere else, and refuses by name before any
request when it is unset. Mercury also requires an **IP allowlist** on any token
holding a write scope, downgrades an unused write permission after 45 days, and
deletes an unused token after 45 — all of which suit a token minted for one
sitting and revoked afterwards, and none of which suit a stored one. The
sequence: mint the token and allowlist your IP in the dashboard,
`read -s -x MERCURY_ADMIN_TOKEN`, run `plan`, run `apply`,
`set -e MERCURY_ADMIN_TOKEN`, revoke the token. `--prune` runs under the same
ceremony and relaxes nothing about it. The three-method write surface
(`MercuryCategoryWriter.create_category`, `.edit_category` and
`.delete_category`, the only three writes in this package) is a courtesy that
keeps a stray call from compiling; what actually bounds that token is its
lifetime. `MercuryClient` — what the ingest and `runningtab health` hold — still
has no verb but GET, `delete_category` least of all: it reads the very rows a
deletion would strip the coding off.

**Adopting hand-imported rows.** A statement CSV imported through Bank Statement
Import with `Tracking ID` mapped to `reference_number` produces Bank
Transactions that carry none of the Mercury fields, and the ingest would post a
second row for each. Mercury's API reports the same ACH trace number as
`trackingNumber` — the number the ingest writes as its own rows'
`reference_number` — so when a transaction routes to a Bank Transaction, has no
document of its own, and carries a trace number, the ingest looks for rows on
the same Bank Account whose `reference_number` equals it. Exactly one match is
**adopted**: the four Mercury fields are written onto it and nothing else — the
person's amount, date, description and reference stay as imported, submitted or
not — and later events find it by id like any other row. More than one match (a
statement imported twice, a number typed onto the wrong row) goes to `ATTENTION`
naming the rows rather than guessing; a person removes the duplicate and the
next run adopts the survivor. No match posts a new row. Adoptions are reported
as `adopted`, apart from `submitted` and `updated`, and a dry run plans them the
same way. Two limits: a row with no trace number — a card charge never went
through the ACH network — cannot be matched and is posted, so a hand-imported
card charge is still a duplicate for a person to remove; and a mistyped
reference is simply no match, so the ingest posts a second row and the balance
check reports the account off by that amount.

**A row's GL code.** Mercury's `generalLedgerCodeName` when a person set one,
else the row's custom category. The custom categories are the chart's GL-code
labels word for word (`mercury categories apply --prune` leaves Mercury offering
nothing else), and coding a row at the card reader picks a category while
leaving `generalLedgerCodeName` empty — so the category is the code. Mercury's
automatic category is not: nobody chose it, and it names Mercury's taxonomy
rather than the chart. This one value is what the description, the
`mercury_gl_code` hint, the draft voucher and `uncoded` all read.

**A draft voucher beside a coded row.** Every settled transaction is already a
submitted Bank Transaction, so reconciling one means opening the create-voucher
dialog and typing what Mercury already knows. Where a person has coded a row —
its GL code parses as `<number> - <name>` and the number is a postable expense
(`5xxx`) or member-capital (`31xx`) account in `chart.coa` — the ingest also
drafts the Journal Entry that dialog would have produced: a `Bank Entry` whose
`cheque_no` is the row's reference, with two lines, the bank account the money
moved through and the account the code names. Its list title is the payee and
the code, and its description (note, code, payee, Mercury id) is in `remark`
with `custom_remark` set: ERPNext's `user_remark` on Journal Entry is hidden
from every list, report and form, and without `custom_remark` ERPNext rebuilds
`remark` from the reference on save. **A proposal and nothing else.** It sets no
`docstatus` in any request, so a voucher is a draft the Bank Reconciliation Tool
does not even offer as a match; a person reviews it, submits it — in bulk if
they like — and matches it themselves. Revenue codes are skipped (a customer
payment clears a Sales Invoice through a Payment Entry), an internal transfer
gets no coded voucher (it has its own, below), and a code the chart cannot place
is reported once per run, naming every such code, because the cause is one
thing: the list uploaded to Mercury has drifted. Drafts are reported as
`drafted`, apart from `submitted`. A draft somebody else has since edited is
left alone **and not reported** — a person taking the proposal over is what the
proposal is for. A draft whose Bank Transaction has since been reconciled to
something else is listed under `delete`, with a link to its page on the site,
and never deleted by the ingest: deleting would occasionally destroy a draft
somebody had edited. The voucher arrives by the same sweep the GL-code hint
does, for the same reason — coding a transaction at Mercury emits no event.

**A transfer between our own accounts** — checking paying the card, or moving
money to savings — is two statement lines and one movement, so the ingest drafts
one voucher for both: debit the account the money arrived on, credit the one it
left, identified by the outgoing half's Mercury id and titled
`Transfer to <account> · <amount>`. Both Bank Transactions are matched to it.
Two halves pair only when each is the other's one match: the same amount with
opposite signs, each naming the other's account as counterparty, dated within
five days. A half with no match, or with more than one, is reported and never
guessed at.

**A row with no code at Mercury.** If a voucher already carries its Mercury id,
or its Bank Transaction is reconciled, a person coded it in the ledger: it is
left alone and no longer reported as `uncoded`. Otherwise the ingest looks at
the payee's history — every submitted voucher carrying the Mercury id of a row
with the same `counterpartyId` — and when all of them went to one account,
drafts the same voucher, its remark naming the account and the voucher it copied
(`4210 - Interest Income as ACC-JV-2026-00025`). Drafts and cancelled vouchers
are not history, and a payee booked to more than one account gets nothing.
Unlike a Mercury code, history drafts revenue too: it repeats what a person
already did for that payee, such as card cashback booked to income.

On any submitted Bank Transaction the ingest already knows — adopted or its own
— a later note or GL-code change at Mercury still lands, because those two
fields are `allow_on_submit`. It arrives by **sweep, not by event**: coding a
transaction at Mercury emits no event and bumps no `resourceVersion`, so a run
that waited for the log would never hear about it. Every `mercury sync`
therefore re-plans those two fields on every settled transaction it read, not
only the ones events named, and reports what changed as `updated`. A settled
transaction the ledger holds no row for is reported instead of posted — the
sweep never inserts, because a row appearing with nothing in the log having said
so would hide the missing event; re-snapshotting is the repair. Anything that
would change the amount, date, description or account of a submitted row is
refused and reported with the disagreement named, as before; the person's
commitment stands and they decide. The description is held to the bank's own
text — the part of it the bank said — so a row keeps whatever note and GL code
were composed into it when it was posted, and a person recoding a transaction at
Mercury afterwards gets the custom fields updated rather than the row refused.
`reference_number` is compared nowhere: the ingest has spelled its own two ways,
and an adopted row's is whatever reference the person meant by it.

The state file holds a cursor into one bank's event log and accounts for rows in
one ledger, so there is one file per `(--env, --target)` pair:
`$XDG_STATE_HOME/<name>-books/mercury-<env>-<target>.sqlite3`, where `<name>` is
`books.toml`'s name, lowercased. The pair is also recorded inside the file, so a
`--state` path pointing at another pair's state is refused rather than resumed —
a sandbox cursor read as a production one would start the next sync after an
event the bank never emitted and skip everything before it.

Every run reports what it posted (`submitted`, `updated`, `adopted`), the
receipts it brought across (`attached` — each one named with the Bank
Transaction it landed on), the balance invariant (each account's settled
transactions summed against `currentBalance`), and the settled non-transfer rows
carrying no GL code at Mercury, aged (`uncoded`). An uncoded row gives the
person reconciling nothing to go on, and somebody will have to code it — in
Mercury, so the hint lands on the Bank Transaction, or by hand at reconciliation
— so the run names it rather than leaving it to be found. A run that wants a
person exits 1.

Receipts come across on the same pass. A Mercury transaction carries the files
somebody uploaded against it as presigned S3 URLs good for twelve hours, and
each one is fetched — streamed, capped at 20 MB — and attached, private, to that
transaction's Bank Transaction. Identity is the attachment UUID out of the URL
path, kept in the `File`'s name as `<uuid>-<file name>`, so a run compares what
is on the row against what Mercury reports and uploads only what is missing.
**Nothing is ever deleted or replaced**, including when Mercury drops an
attachment: a receipt on the bank's record is a person's record, and the URL
behind it is long expired. A `File` on the row with no UUID prefix is somebody
else's and is left alone without a word. Like the GL code, the attachment list
emits no event, which is why it is compared on every run rather than waited for.

`--env` takes `sandbox` or `production`, with no default — they are different
hosts and a client that picks one eventually picks the bank. The sandbox token
comes from `MERCURY_SANDBOX_TOKEN`; the real one from the Secret Manager secret
`books.toml` names as `secrets.mercury`. `--env sandbox` with `--target books`
is refused: seeded data has no business in the company's ledger.

`mercury.toml`, beside `chart.coa`, says what each Mercury account is here —
which chart account holds its balance (`[accounts]`, which
`install-bank-accounts` links each Bank Account to) and which ERPNext Bank
Account its Bank Transactions attach to (`[bank_accounts]`). There is one Bank
Account per Mercury account rather than one for all of them: the ingest reads
checking, savings and the card, and a savings withdrawal posted against the
checking Bank Account is wrong in the ledger and invisible to the reconciliation
the doctype exists for. An account with no entry refuses. There is no category
table: a file that still carries `[categories]` or `[posting]` is refused rather
than read past, because a table that reads as configured while doing nothing is
exactly what a person editing it cannot see.

## The forecast commands

Compute the capital call: how much each member puts in so cash stays at or above
a floor (the largest single charge) at its lowest point through the end of the
third full month ahead. The inputs are `forecast.toml` (in the books directory
beside the chart; validated against it on load), Mercury's balances and
transactions, and the ledger's reconciliation frontier. Money is `Decimal`
throughout; each line costs its `high` amount, a charge not yet seen in the bank
is still owed, and each member's share rounds up to the cent.

Install order on a site: `forecast install-doctype` (administrator), then
`access provision` (grants the bot `read` and `create` on Cash Forecast), then
`forecast plan` and `forecast apply`. `access provision` and `access verify`
refuse with one sentence, writing nothing, if the doctype is missing.

| Command                                 | Credential    | Reads                                      | Writes                          |
| --------------------------------------- | ------------- | ------------------------------------------ | ------------------------------- |
| `forecast plan --env <e> --target <t>`  | reader        | Mercury, Bank Transaction, `forecast.toml` | nothing                         |
| `forecast apply --env <e> --target <t>` | bot           | the same, Company and Cash Forecast        | one new Cash Forecast           |
| `forecast install-doctype --target <t>` | administrator | DocType                                    | the four Cash Forecast doctypes |

`install-doctype` takes `--yes` to skip the prompt and prints one line per
doctype with its outcome (`created`, `updated`, `unchanged`). Re-running repairs
a doctype whose field list drifted and never deletes one.

`plan` and `apply` print the window, opening cash (the sum of every Mercury
account's balance, net of the card), the ledger-through date (the day before the
oldest unreconciled Bank Transaction, or today), a table of months, each due
with its status (`owed` or `seen paid`), the floor and what set it (a line name,
`[call] floor`, or `no owed charges`), the total call, and each member's share.
`apply` then prints `wrote CF-00001` and the document's desk URL. Every run adds
a document and never edits an earlier one. `now` is local time.

Exit status is 0 on success and 1 on a refusal, which prints one sentence:

- Mercury's balance for an account differs from the sum of its transactions, or
  Mercury returned no accounts.
- A Bank Transaction has no readable date.
- `forecast.toml` does not validate.
- The site is in maintenance mode (`SessionStopped`).
- `apply` only: the Cash Forecast doctype is not installed on the site.
- Mercury's API refuses a request.
- `apply` only: the bot lacks permission to create Cash Forecast documents; run
  `access provision`.

`--env sandbox` with `--target books` is refused before any request as a usage
error, with exit status 2.

## Targets

`--target` takes one of two values, and there is no default — reaching a real
ledger requires naming it:

- `test` — a disposable ERPNext site. Credentials come from the environment
  first (`RUNNINGTAB_TEST_URL`, `RUNNINGTAB_TEST_KEY`, `RUNNINGTAB_TEST_SECRET`,
  all three required once the URL is set, so a developer pointed at their own
  bench is never silently redirected); with none exported it falls back to the
  `secrets.test_admin` secret and the one allowlisted host, so anything running
  under application default credentials reaches the site with nothing set up. A
  stored **site** key is allowed there because it reaches that one disposable
  site and nothing else; the Frappe Cloud team token, which serves the books
  site by name however reassuringly `press.api.site.all` lists only the test
  one, stays with a human. Either way the target checks the host against an
  **allowlist** (`books.toml`'s `site.test_hosts`), not a denylist on the books
  hostname, and refuses anything not named there — a custom domain or alias can
  never slip through by looking unlike production.
- `books` — the real ledger. Its credential is fetched from Secret Manager at
  the moment it is used, never held during development and never substitutable
  by an exported variable.

The tests that actually exercise the `test` target (`pytest -m live_runningtab`)
are skipped unless `RUNNINGTAB_LIVE=1` is set — `RUNNINGTAB_TEST_URL` alone no
longer runs them, since `site_config` now falls back to the stored test-site key
when it is unset, and that fallback must not turn a normal test run into a real
network call. Like the CLI, they take the books directory from
`RUNNINGTAB_BOOKS`. Run them with `just test-live`, or export
`RUNNINGTAB_LIVE=1`, `RUNNINGTAB_BOOKS`, and your own `RUNNINGTAB_TEST_*`
variables to point at a personal bench instead of the shared site. They never
run as part of `just test` or CI's default suite.

## Access

"Machines draft, humans post" holds only if the credential a machine uses cannot
commit money — `icedcoffee-sync`'s client-side guard is a courtesy any caller
can bypass, so the boundary is the API key's server-side roles.
`runningtab access` provisions a dedicated bot user whose role holds
create/write permission for drafting and the chart of accounts, **never** cancel
or amend on anything, and submit on exactly one doctype: **Bank Transaction**.
That is the one exception, and the reason it is safe is what a Bank Transaction
is: submitting it posts nothing to the general ledger. ERPNext writes the GL
entries when a person reconciles the row to a voucher in the Bank Reconciliation
Tool, which only works on submitted rows — so the ingest submitting the
statement lines it creates commits the bank's record of what happened, not the
books' account of why. Every other submittable doctype the bot can reach
(Journal Entry, Purchase Invoice) commits money on submit and stays draft-only.
The ingest is the one command that takes the credential with submit capability
(`ingest_config`); everything else keeps the drafts-only client, so a stray
`docstatus` anywhere else fails before it is sent.

- `access provision --target <target> [--yes]` — as an administrator (for
  `books`, your own System Manager API token in `RUNNINGTAB_ADMIN_TOKEN`, the
  `key:secret` ERPNext shows for your user, with or without its `token ` prefix;
  the stored books credential is the bot's and cannot provision), create or
  repair the bot role and user `books.toml`'s `[bot]` names, reconcile its
  grants to exactly:

  - `Account`: read, write, create, delete (the chart apply/prune)
  - `Company`: read, write (default-account links)
  - `GL Entry`: read (the ledger guard)
  - `Journal Entry`, `Purchase Invoice`, `Supplier`, `File`: read, write, create
    (AP drafting and attachments)
  - `Bank Transaction`: read, write, create, **submit** (the Mercury ingest; see
    above for why this one submit is safe)
  - `Cash Forecast`: read, create (the capital forecast; each run writes a new
    document, and the bot can neither change nor delete one once written)

  then mint a fresh key and prove it with the server before storing it: a real
  submit attempt on Journal Entry is refused (after a real draft proved the key
  can create), `submit`/`cancel`/`amend` are denied on Journal Entry and
  Purchase Invoice, `cancel`/`amend` on Bank Transaction, and `write`/`delete`
  on Cash Forecast, and every granted flag is actually in effect — `submit` on
  Bank Transaction included, since a grant on paper the server ignores would
  fail the ingest on its first row, and a permission row that exists is not
  proof it works. Only then is the key stored (to Secret Manager for `books`;
  the `test` target keeps its credential in the environment); anything else
  exits 1 naming the doctype and flag. Re-running rotates the bot's secret every
  time, and the command says so when a failure leaves the stored one stale.

- `access verify --target <target> [--company NAME]` — run the same three proofs
  against the target's current credential; exits 0 only on a full pass, usable
  as a gate like `coa plan`.
