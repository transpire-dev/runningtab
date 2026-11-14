# What a close guarantees

Closing a quarter gives you a locked ledger and a stored package whose contents have been checked against that ledger and read back from storage. It also gives you a way to keep checking whether the quarter still matches its recorded close.

These guarantees come from several separate checks. The ledger lock controls posting, rebuilding checks the package’s contents, and storage verification checks what reached the record bucket. `period status` continues checking closed quarters as the ledger and bank data change.

## A package for someone outside your books

A quarter’s package is intended for an accountant or auditor who has never worked with you. It brings together the reports and supporting documents they need to examine that quarter:

- `MANIFEST.md`, which explains the package in plain sentences, and `SHA256SUMS`, which records its file checksums.
- The trial balance, general ledger, profit and loss statement, and balance sheet.
- Open receivables and payables, plus the bank reconciliation.
- A QuickBooks Online import kit under `qbo/`.
- Receipts, invoices, and other attachments under `documents/`.

The package draws on both ERPNext, which holds your ledger, and Mercury, which holds your bank accounts. The ledger reads include GL Entries, Fiscal Years, every voucher and Bank Transaction dated in the quarter, and their attachments. The bank reads include each Mercury account’s balance and transactions.

You can build a draft with `period export` before closing. Export reads both systems and writes a new local directory, normally `<quarter>-DRAFT/`. It refuses an existing destination because someone may already be working in it.

A bank difference still produces a draft for investigation, with a failing exit code. If the package cannot be built—for example, because an account is missing from `chart.coa` or a required system is unreachable—export writes nothing.

## What `period close` does

The close ceremony connects the package to a specific locked state of the ledger:

1. **Read the current state.** runningtab reads Books Closed Through and the ledger, then builds the pre-lock package.
2. **Cross-check the reports.** It checks the draft against ERPNext’s own Trial Balance and accounts receivable and payable reports, account by account and party by party.
3. **Lock the quarter.** It moves Books Closed Through to the quarter’s last day.
4. **Build again.** It rebuilds the package and requires the result to be byte-identical to the pre-lock build.
5. **Store and verify.** It uploads the package to the record bucket, then reads every object back to verify it.

The second build checks whether the ledger changed between the checks and the lock. If it did, the close refuses to proceed with a differing package. The refusal identifies changed files and the vouchers whose general-ledger rows differ.

Closing requires administrator access. For `--target books`, you must supply your own System Manager token through `RUNNINGTAB_ADMIN_TOKEN`. runningtab refuses the command before making a request if that variable is unset. A stored credential cannot close a quarter.

For preparation, commands, and recovery procedures, see [Close a quarter](../how-to/close-a-quarter.md).

## What a successful close guarantees

After a successful close, Books Closed Through is set to the quarter’s end. The stored package has passed the report cross-check, matched byte for byte across the lock, and been read back from the record bucket for verification.

The package is stored under `<company-abbr>/<quarter>/`. When you reopen and re-close a quarter, the new package becomes a revision under `revN/` beside the original. The bucket has a retention policy, and runningtab never deletes or overwrites the stored packages. Earlier versions remain part of the record.

The point at which a failure happens matters. A failure before the lock leaves nothing changed. A failure after the lock leaves the lock in place: there is no rollback. runningtab prints Books Closed Through and names the next step. A locked quarter can therefore exist without a completed stored package, and an error can arrive after a package has reached storage.

Those states are why recovery begins by checking status. The [close procedure](../how-to/close-a-quarter.md) covers the appropriate recovery for a first close and a re-close.

## Why closed quarters keep getting rebuilt

A successful close records what passed the checks at that moment. `period status` checks whether the current data still produces the recorded package.

For every closed quarter, it rebuilds the latest revision through the same read-and-build path as `period export` and compares the package bytes against the stored `SHA256SUMS`. It writes nothing. The `closed periods` section of `health` runs the same check and shares the same report.

Status also checks the surrounding state. It detects a reopened quarter, a lock with no stored package, an unfinished re-close, and `role_allowed_for_frozen_entries` left set. Company `Version` history helps it identify who reopened a quarter and when, and distinguish a newer lock from the lock recorded in the stored manifest. This can expose an unfinished re-close even when every package file still matches.

Bank data can change after closing. Activity dated in a closed quarter may reach Mercury later and change that quarter’s rebuilt `bank-reconciliation.csv`. `mercury sync` identifies those rows and drafts no vouchers for them. They need to be booked in their original quarter after reopening. See [Handle late bank activity](../how-to/late-bank-activity.md).

## What reopening means

Reopening is a deliberate action by a person in the ERPNext UI. You move Books Closed Through back to the previous quarter’s end. There is no `period reopen` command.

Any stored quarter can be reopened. After correcting it, you re-close that quarter and every stored quarter after it, in order. Each receives a new revision. This repeats the close checks for the affected sequence and records packages built from the corrected books.

Until that sequence is complete, `period status` and `health` fail and name the required order. The original packages remain in storage, preserving the earlier closes alongside their revisions.

For the exact sequence and recovery steps, see [Reopen and re-close](../how-to/reopen-and-reclose.md).