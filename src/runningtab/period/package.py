"""The package: every content file from one set of inputs, plus how it is checked and written."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import pathlib
import shutil
import tempfile

from icedcoffee_sync.apply.model import DesiredAccount

from runningtab.health import AccountLedger
from runningtab.mercury.mapping import Mapping
from runningtab.period import PeriodError
from runningtab.period.balances import (
    balance_sheet_csv,
    chart_of_accounts_csv,
    general_ledger_csv,
    profit_and_loss_csv,
    trial_balance,
    trial_balance_csv,
)
from runningtab.period.bank import BankLine, bank_reconciliation, bank_reconciliation_csv
from runningtab.period.ledger import GLRow, index_accounts
from runningtab.period.open_items import check_open_items_against, open_items, open_items_csv
from runningtab.period.qbo import build_qbo_kit
from runningtab.period.qbo_map import QboMap
from runningtab.period.quarter import FiscalYear, Quarter


@dataclasses.dataclass(frozen=True)
class PackageInputs:
    quarter: Quarter
    fiscal_year: FiscalYear
    company: str
    site_url: str
    chart: tuple[DesiredAccount, ...]
    abbr: str
    rows: tuple[GLRow, ...]
    ledgers: tuple[AccountLedger, ...]
    mapping: Mapping
    qmap: QboMap
    documents: dict[str, bytes]


@dataclasses.dataclass(frozen=True)
class Package:
    files: dict[str, bytes]
    bank: tuple[BankLine, ...]
    split_vouchers: tuple[str, ...]


def build_package(inputs: PackageInputs) -> Package:
    """Every content file for the quarter, plus the bank reconciliation and split vouchers `MANIFEST.md` reports on.

    A bank difference is reported, not refused -- a draft package is for
    investigating one, and the CLI decides what a non-zero difference means
    for the exit code.
    """
    index = index_accounts(inputs.chart, inputs.abbr)
    tb = trial_balance(inputs.rows, index, inputs.quarter, inputs.fiscal_year)
    receivables = open_items(inputs.rows, index, "Receivable", inputs.quarter.end)
    payables = open_items(inputs.rows, index, "Payable", inputs.quarter.end)
    check_open_items_against(receivables, tb, index, "Receivable")
    check_open_items_against(payables, tb, index, "Payable")
    bank = bank_reconciliation(inputs.ledgers, inputs.mapping, tb, inputs.quarter.end)
    kit = build_qbo_kit(tb, inputs.rows, index, inputs.chart, inputs.qmap, inputs.quarter)
    files = {
        "chart-of-accounts.csv": chart_of_accounts_csv(inputs.chart),
        "trial-balance.csv": trial_balance_csv(tb),
        "general-ledger.csv": general_ledger_csv(inputs.rows, index, inputs.quarter),
        "profit-and-loss.csv": profit_and_loss_csv(tb),
        "balance-sheet.csv": balance_sheet_csv(tb),
        "bank-reconciliation.csv": bank_reconciliation_csv(bank),
        "open-receivables.csv": open_items_csv(receivables),
        "open-payables.csv": open_items_csv(payables),
        **{f"qbo/{name}": data for name, data in kit.files.items()},
        **inputs.documents,
    }
    return Package(dict(sorted(files.items())), tuple(bank), kit.split_vouchers)


def sha256sums(files: dict[str, bytes]) -> bytes:
    """The standard `sha256sum` format, sorted by path, over exactly the files given.

    `MANIFEST.md` is never in `files`: it carries a generation timestamp, so
    including it here would make every rebuild's checksums differ even when
    the content is byte-identical. `write_package` relies on that.
    """
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {path}\n" for path, data in sorted(files.items())).encode()


def write_package(directory: pathlib.Path, package: Package, manifest: bytes) -> None:
    """Write a new package directory. Never over an existing one: a person may be working in it.

    `directory` is reserved with a plain `mkdir` before anything else --
    that is what raises the "already exists" refusal, atomically. An
    existence check followed later by a rename onto `directory` is two
    separate operations with a window between them: something else (another
    run, a person) could create an empty `directory` in that window, and the
    rename at the end would then silently replace it instead of refusing.
    Reserving the name first closes that window.

    The package itself is still written into a sibling temporary directory,
    then renamed onto the now-reserved `directory` as the last step: a
    straight write that failed part-way (disk full, the process killed)
    would leave a partial package sitting at `directory`, which a retry
    refuses because it exists and a person could mistake for a real export.
    The rename only succeeds onto an empty directory, so anything that has
    since put a file into the reserved `directory` makes it fail loudly
    instead of silently discarding that file. On any failure the temporary
    directory is removed, and the reserved `directory` too, but only if it
    is still the empty one this call created -- never one something else
    has since written into. `tempfile.mkdtemp` itself runs inside that same
    cleanup block: failing outside it would leave the reserved `directory`
    behind with nothing to build into it, blocking every retry the same way
    a straight write's partial package used to.
    """
    try:
        directory.mkdir()
    except FileExistsError:
        raise PeriodError(f"{directory} already exists; move it aside or choose another --output") from None
    staging: pathlib.Path | None = None
    try:
        staging = pathlib.Path(tempfile.mkdtemp(dir=directory.parent, prefix=f".{directory.name}.partial-"))
        for path, data in package.files.items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (staging / "MANIFEST.md").write_bytes(manifest)
        (staging / "SHA256SUMS").write_bytes(sha256sums(package.files))
        os.replace(staging, directory)
    except Exception:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
        raise
