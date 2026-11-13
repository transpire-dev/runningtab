# How the ingest decides

The Mercury ingest brings settled bank activity into ERPNext so you can reconcile it. It preserves the bank’s statement lines, carries across coding hints and receipts, and prepares draft vouchers where it has enough information. You review, submit and match those vouchers.

For commands and options, see the [Mercury reference](../reference/mercury.md).

## What lands in ERPNext for each transaction?

Every settled Mercury transaction becomes a submitted Bank Transaction on the ERPNext Bank Account for the Mercury account it moved through. Checking, savings and the card each have their own Bank Account.

The mappings in `mercury.toml` connect each Mercury account to its chart account and ERPNext Bank Account. A missing mapping causes a refusal. A transaction with no category, or an unrecognised category, still becomes a Bank Transaction with whatever hint Mercury provides.

Bank Transactions appear in ERPNext’s Banking workspace for reconciliation. Some also receive a draft Journal Entry, under the rules below.

The ingest reads Mercury without changing it. The separate category-management command can change Mercury categories; its scope and usage are in the [Mercury reference](../reference/mercury.md).

## What text will I see on the row?

The workspace card shows the date, reference, description and amount.

The `reference_number` is Mercury’s ACH trace number, reported as `trackingNumber`. This lets an ingested transaction carry the same reference as a manually imported statement row. When there is no trace number, as with a card charge, the reference is the Mercury transaction id.

The description joins whichever of these exist, in this order, with a spaced middle dot:

- Your Mercury note.
- The row’s GL code.
- The bank’s own text.

The reference and description are fixed when posted. The note and GL code also live in separate custom fields that receive later Mercury edits. Those fields provide hints; they do not route the Bank Transaction.

## Where does the GL code come from?

The ingest uses Mercury’s `generalLedgerCodeName` when you have set one. Otherwise, it uses the transaction’s custom category.

The custom categories are the chart’s GL-code labels word for word. Selecting a category while coding a card transaction can leave `generalLedgerCodeName` empty, so the custom category supplies the code.

Mercury’s automatic category is excluded: it describes Mercury’s taxonomy and records no coding choice by you.

This single resolved value supplies the description, the `mercury_gl_code` custom field, the coded-voucher decision and the check for uncoded transactions.

## When does a draft voucher appear, and what do I do with it?

A coded transaction qualifies when its GL code parses as `<number> - <name>` and the number identifies a postable expense (`5xxx`) or member-capital (`31xx`) account in `chart.coa`.

The ingest drafts a Journal Entry of type `Bank Entry`, with two lines: the bank’s ledger account and the account named by the code. Its `cheque_no` carries the transaction’s reference. Its title contains the payee and code; its remark contains the note, code, payee and Mercury id.

Revenue codes are skipped because a customer payment clears a Sales Invoice through a Payment Entry. Internal transfers follow their own rules. Codes the chart cannot place are reported together once per run, indicating that Mercury’s uploaded code list has drifted.

You review the draft, submit it, and match it yourself. ERPNext’s Bank Reconciliation Tool offers it as a match after submission. You can submit reviewed drafts in bulk.

A draft someone has edited is left alone and omitted from reporting. If its Bank Transaction has been reconciled to something else, the draft appears under `delete`, with a link for review. The ingest never deletes it, because it may contain someone’s work.

See [daily reconciliation](../how-to/daily-reconciliation.md) for the workflow.

## How are transfers between our own accounts handled?

A transfer produces two Bank Transactions, one for each statement line, and one draft voucher for the movement.

The voucher debits the account receiving the money and credits the account sending it. It is identified by the outgoing half’s Mercury id and titled `Transfer to <account> · <amount>`. You submit the voucher and match both Bank Transactions to it.

Two halves pair only when each is the other’s sole match. They must have equal amounts with opposite signs, name each other’s account as counterparty, and be dated within five days.

A half with no match, or multiple possible matches, is reported for attention.

## Can payee history help when Mercury has no code?

First, the ingest checks whether a voucher already carries the transaction’s Mercury id or its Bank Transaction is reconciled. Either means someone has coded it in the ledger. It is left alone and stops appearing as `uncoded`.

Otherwise, the ingest looks at submitted vouchers carrying Mercury ids for transactions with the same `counterpartyId`. If all that history points to one account, it drafts a voucher using that account.

The remark names the account and the voucher copied, for example `4210 - Interest Income as ACC-JV-2026-00025`. History can support revenue drafts, including card cashback previously booked to income.

Draft and cancelled vouchers contribute no history. A payee whose history spans multiple accounts gets no proposal.

## What happens to rows I imported by hand?

A statement imported with `Tracking ID` mapped to `reference_number` can be adopted.

When a Mercury transaction has no document of its own and has an ACH trace number, the ingest searches the same Bank Account for that reference.

Exactly one match is adopted: the four Mercury fields are added, while the imported amount, date, description, reference and submission status stay unchanged. Future runs find it by Mercury id.

Multiple matches produce `ATTENTION` naming the rows. After you remove the duplicate, the next run can adopt the survivor. No match produces a new Bank Transaction.

A card charge without a trace number cannot be matched this way, so a hand-imported card charge can leave a duplicate for you to remove. A mistyped reference also produces no match; the ingest posts another row, and the balance check reports the account off by that amount.

## Why do notes, codes and receipts arrive by sweep?

Editing a note, code or attachment list at Mercury emits no event. Coding also leaves `resourceVersion` unchanged. Every sync therefore checks the settled transactions it read, including those absent from the latest events.

This sweep updates the note and GL-code custom fields on known submitted Bank Transactions, including adopted rows. It also brings across missing receipts.

Receipts are private attachments on the Bank Transaction. Each download is streamed and capped at 20 MB. The attachment UUID from Mercury’s URL is preserved in the filename, allowing later runs to identify files already present.

Receipts are never deleted or replaced, even if Mercury removes them. Files without a UUID prefix are left alone.

The sweep preserves submitted amounts, dates, descriptions and accounts. Disagreements are refused and reported. Description checks compare the bank’s own text, allowing the original composed note and code to remain. References are never compared.

A settled transaction with no ledger row is reported. The sweep never inserts missing Bank Transactions: the event log is the authority for changes, and a missing event needs investigation. Re-snapshotting repairs that gap.

## What does a run report, and when does it exit 1?

Every `mercury sync` and `mercury snapshot` prints the same report:

| Line | What it counts |
| --- | --- |
| `submitted` | New Bank Transactions, ready for reconciliation |
| `drafted` | Draft Journal Entries proposed for coded rows; a person submits them |
| `updated` | Bank Transactions and draft vouchers brought up to date |
| `adopted` | Hand-imported rows recognised by their trace number |
| `attached` | Receipts now on their Bank Transaction, each named |
| `planned` | On a `--dry-run`, what would have been written |
| `skipped` | Transactions not yet settled |
| `failed` | Transactions that failed at Mercury in an account runningtab cannot read |
| `uncoded` | Settled, non-transfer rows with no GL code, with their age in days |
| `balance` | Per account, the settled transactions summed against Mercury's `currentBalance` |
| `closed` | Rows dated in a closed quarter, which get no draft voucher |
| `delete` | Draft Journal Entries no longer needed, each with a link for a person to delete it |
| `ATTENTION` | Rows the ingest would not decide on, each with the reason |
| `events`, `cursor` | How much of Mercury's event log the run read and applied, and where it stopped |

The run exits 1 whenever it wants a person: anything under `ATTENTION`, an uncoded row, a settled transaction the ledger holds no row for, a row in a closed quarter, a draft to delete, or a balance that does not reconcile. Each of those leaves the ledger incomplete while every individual step looks like it worked.

An uncoded row gives the person reconciling nothing to go on. Code it in Mercury, so the hint reaches the Bank Transaction on the next sync, or code it at reconciliation. A row in a closed quarter needs that quarter reopened; see [late bank activity](../how-to/late-bank-activity.md).

## Where does the ingest keep its state?

Each environment and ledger target pair has its own state file:

`$XDG_STATE_HOME/<name>-books/mercury-<env>-<target>.sqlite3`

Here, `<name>` is the lowercased name from `books.toml`. The file stores the cursor into one bank’s event log and accounts for rows in one ledger.

The environment and target are also recorded inside the file. A state path belonging to another pair is refused, preventing a cursor from one bank environment from skipping events in another.

See the [Mercury reference](../reference/mercury.md) for environment, target and state options.