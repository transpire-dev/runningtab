"""`runningtab access provision` and `runningtab access verify`: store only on a proven refusal.

The whole point of this command pair is that a credential is written to
Secret Manager only after a real server-side proof that it cannot submit --
never on trust in `provision` alone. Every test here drives that decision
through a recording fake storer, never a mock that would pass regardless of
what `probe_submit` actually said.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from icedcoffee_sync.frappe.access import ApiKeyPair, SubmitProbe, UserChange
from icedcoffee_sync.frappe.client import SiteConfig
from icedcoffee_sync.frappe.errors import DoesNotExistError, PermissionError_, RateLimitError, ValidationError
from typer.testing import CliRunner

import runningtab.access as access_module
import runningtab.cli as cli_module
from runningtab.access import KeyRotationFailedError, ProvisionReport
from runningtab.period import PeriodError
from runningtab.period.fence import LOCK_FIELDS, FenceProof
from tests.conftest import EXAMPLE_BOOKS

runner = CliRunner()

FAKE_CONFIG = SiteConfig(url="https://fake.example.test", api_key="old", api_secret="olds")  # noqa: S106

FAKE_REPORT = ProvisionReport(
    role_created=True,
    role_desk_access_repaired=False,
    role_disabled_repaired=False,
    grants={"Account": [("read", "1"), ("submit", "0")]},
    revoked=[],
    user=UserChange(created=True, added_roles=frozenset({EXAMPLE_BOOKS.bot.role}), removed_roles=frozenset()),
    keys=ApiKeyPair(api_key="newkey", api_secret="newsecret"),  # noqa: S106
)


class _FakeAccessClient:
    """Enough of `Client` for `_sole_company` to run against a fixed company."""

    def __init__(self, config: SiteConfig) -> None:
        self.config = config

    def __enter__(self) -> _FakeAccessClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def list(self, doctype: str, **kwargs: Any) -> Any:
        if doctype == "Company":
            return [{"name": "Valley Tool Co"}]
        if doctype == "Cash Forecast":
            return []  # installed
        raise AssertionError(f"unexpected list({doctype!r})")

    def iter_all(self, doctype: str, **kwargs: Any) -> Any:
        # _sole_company pages through iter_all rather than a single capped
        return iter(self.list(doctype, **kwargs))


@pytest.fixture(autouse=True)
def _stub_site_and_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "site_config", lambda site, target, **_: FAKE_CONFIG)
    monkeypatch.setattr(cli_module, "admin_config", lambda site, target: FAKE_CONFIG)
    monkeypatch.setattr(cli_module, "Client", lambda config: _FakeAccessClient(config))
    # Default: every prohibited flag on every submittable doctype comes back
    monkeypatch.setattr(
        access_module,
        "prohibited_flags_allowed",
        lambda config, *, doctype, ptypes: dict.fromkeys(ptypes, False),
    )
    # Default: every GRANTS ptype on every doctype comes back effective, so a
    monkeypatch.setattr(
        access_module,
        "granted_flags_effective",
        lambda config, *, doctype, ptypes: dict.fromkeys(ptypes, True),
    )
    # Default: the fence installs cleanly and the bot's key is refused on both
    # lock fields -- the passing case. `runningtab.period.fence` has its own unit
    # tests (test_period_fence.py); these CLI tests are about what `provision`
    # does with the result, not about re-proving the fence logic itself.
    monkeypatch.setattr(
        cli_module,
        "install_fence",
        lambda client, probe_role: (
            [(f"Company.{field}", "unchanged") for field in LOCK_FIELDS]
            + [(EXAMPLE_BOOKS.fence_probe_role, "unchanged")]
        ),
    )
    monkeypatch.setattr(cli_module, "check_fence_probe", lambda admin, company, probe_role: None)
    monkeypatch.setattr(
        cli_module,
        "prove_fence",
        lambda admin, bot, company, probe_role: FenceProof(
            date_refused=True, role_refused=True, date_how="reset", role_how="reset", restored=()
        ),
    )


def _stub_storer(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli_module,
        "store_books_credential",
        lambda site, key, secret, *_: (stored.append((key, secret)), "projects/x/secrets/erpnext-api-key/versions/2")[
            1
        ],
    )
    return stored


def test_provision_stores_the_credential_when_the_server_refuses_submit_for_books(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert stored == [("newkey", "newsecret")]
    assert "newkey" not in result.output
    assert "newsecret" not in result.output
    # The two server-evaluated flags must be visible, not just the verdict --
    assert "create=allowed" in result.output.lower()
    assert "submit=denied" in result.output.lower()


def test_provision_announces_submit_on_bank_transaction_as_the_one_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The policy printed before the prompt has to say what the bot may submit,
    # so nobody reads "never submit" and approves a role that can.
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    policy = result.output.lower().split("role created")[0]
    assert "submit only on bank transaction" in policy
    assert "never: cancel, amend" in policy
    assert "never: submit, cancel, amend" not in policy


def test_provision_reports_desk_access_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_module, "provision", lambda client, *_: dataclasses.replace(FAKE_REPORT, role_desk_access_repaired=True)
    )
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert "desk access repaired" in result.output.lower()


def test_provision_reports_a_re_enabled_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_module, "provision", lambda client, *_: dataclasses.replace(FAKE_REPORT, role_disabled_repaired=True)
    )
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert "role re-enabled" in result.output.lower()


def test_provision_says_nothing_about_role_re_enabling_when_not_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert "role re-enabled" not in result.output.lower()


def test_provision_reports_a_re_enabled_user(monkeypatch: pytest.MonkeyPatch) -> None:
    repaired_user = dataclasses.replace(FAKE_REPORT.user, enabled_repaired=True)
    monkeypatch.setattr(
        cli_module, "provision", lambda client, *_: dataclasses.replace(FAKE_REPORT, user=repaired_user)
    )
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert "user re-enabled" in result.output.lower()


def test_provision_says_nothing_about_desk_access_when_not_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert "desk access repaired" not in result.output.lower()


def test_provision_refuses_to_store_when_another_doctype_still_allows_a_prohibited_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    monkeypatch.setattr(
        access_module,
        "prohibited_flags_allowed",
        lambda config, *, doctype, ptypes: (
            {"submit": True, "cancel": False, "amend": False}
            if doctype == "Purchase Invoice"
            else dict.fromkeys(ptypes, False)
        ),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "purchase invoice" in result.output.lower()
    assert "submit" in result.output.lower()
    assert "already rotated" in result.output.lower()


def test_provision_refuses_to_store_when_a_granted_flag_is_not_effective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    monkeypatch.setattr(
        access_module,
        "granted_flags_effective",
        lambda config, *, doctype, ptypes: (
            {"read": True, "write": False, "create": True, "delete": True}
            if doctype == "Account"
            else dict.fromkeys(ptypes, True)
        ),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "account" in result.output.lower()
    assert "write" in result.output.lower()
    assert "already rotated" in result.output.lower()


def test_provision_stores_when_every_grant_is_effective(monkeypatch: pytest.MonkeyPatch) -> None:
    # The positive case _missing_grants_report exists to leave alone: the
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert stored == [("newkey", "newsecret")]


def test_provision_warns_after_rotation_when_checking_other_doctypes_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    monkeypatch.setattr(
        access_module,
        "prohibited_flags_allowed",
        lambda config, *, doctype, ptypes: (_ for _ in ()).throw(RuntimeError("transport error")),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "checking prohibited permissions" in result.output.lower()
    assert "already rotated" in result.output.lower()


def test_provision_warns_after_rotation_when_checking_granted_permissions_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    monkeypatch.setattr(
        access_module,
        "granted_flags_effective",
        lambda config, *, doctype, ptypes: (_ for _ in ()).throw(RuntimeError("transport error")),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "checking granted permissions" in result.output.lower()
    assert "already rotated" in result.output.lower()


def test_provision_warns_after_rotation_when_minting_the_key_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "provision",
        lambda client, *_: (_ for _ in ()).throw(KeyRotationFailedError("connection reset")),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "minting a fresh key" in result.output.lower()
    assert "already rotated" in result.output.lower()


def test_provision_never_mints_a_secret_when_company_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class _AmbiguousCompanyClient(_FakeAccessClient):
        def list(self, doctype: str, **kwargs: Any) -> Any:
            if doctype == "Company":
                return [{"name": "Valley Tool Co"}, {"name": "Other Co"}]
            raise AssertionError(f"unexpected list({doctype!r})")

    called: list[str] = []
    monkeypatch.setattr(cli_module, "Client", lambda config: _AmbiguousCompanyClient(config))
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: called.append("provision") or FAKE_REPORT)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code != 0
    assert called == []


def test_provision_never_mints_a_secret_when_the_fence_probe_could_not_prove_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Found only inside prove_fence, this refusal would come after the key
    # rotated and leave the stored credential dead. Checked first, it costs
    # nothing: no key minted, nothing stored, no rotation warning.
    def _held(admin: Any, company: str, probe_role: str) -> None:
        raise PeriodError(f"{EXAMPLE_BOOKS.fence_probe_role!r} is held by sam@example.com; it must be held by nobody")

    called: list[str] = []
    monkeypatch.setattr(cli_module, "check_fence_probe", _held)
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: called.append("provision") or FAKE_REPORT)
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert called == []
    assert stored == []
    assert "sam@example.com" in result.output
    assert "no key was minted" in result.output


def test_provision_reports_revoked_grants_when_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    report_with_revocation = dataclasses.replace(FAKE_REPORT, revoked=["DocType"])
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: report_with_revocation)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert "grants revoked" in result.output.lower()
    assert "doctype" in result.output.lower()


def test_provision_says_nothing_about_revocation_when_nothing_was_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert "revoked" not in result.output.lower()


def test_provision_does_not_store_for_the_test_target_even_when_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(access_module, "probe_submit", lambda config, *, company: SubmitProbe(refused=True))
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "test", "--yes"])

    assert result.exit_code == 0, result.output
    assert stored == []
    assert "not stored" in result.output.lower()
    assert "newkey" not in result.output
    assert "newsecret" not in result.output


def test_provision_exits_1_and_stores_nothing_when_the_server_accepts_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The catastrophic case: the bot's fresh credential can submit. Nothing
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module, "probe_submit", lambda config, *, company: SubmitProbe(refused=False, document_name="ACC-JV-1")
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "newkey" not in result.output
    assert "newsecret" not in result.output


def test_provision_warns_the_books_target_is_broken_when_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module, "probe_submit", lambda config, *, company: SubmitProbe(refused=False, document_name="ACC-JV-1")
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert "already rotated" in result.output.lower()
    assert "re-run" in result.output.lower()


def test_provision_does_not_warn_the_test_target_is_broken_when_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The test target never persists a bot credential anywhere -- the fresh
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module, "probe_submit", lambda config, *, company: SubmitProbe(refused=False, document_name="ACC-JV-1")
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "test", "--yes"])

    assert result.exit_code == 1
    assert "already rotated" not in result.output.lower()


def test_provision_reports_why_when_validation_stopped_the_probe_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=False, error=ValidationError("Accounts is mandatory")),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "accounts is mandatory" in result.output.lower()


def test_provision_reports_a_permission_disagreement_distinctly_from_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(
            refused=False, can_create=True, submit_flag=True, error=PermissionError_("no permission")
        ),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "disagree" in result.output.lower()
    assert "no permission" in result.output.lower()


def test_provision_reports_an_inconclusive_error_distinctly_from_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(
            refused=False, can_create=True, submit_flag=False, error=RateLimitError("too many requests")
        ),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "inconclusively" in result.output.lower()
    assert "too many requests" in result.output.lower()
    assert "validation stopped it" not in result.output.lower()


def test_provision_does_not_store_when_the_credential_cannot_even_create(monkeypatch: pytest.MonkeyPatch) -> None:
    # The false-positive a live run actually hit: a PermissionError from the
    from icedcoffee_sync.frappe.errors import PermissionError_

    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(
            refused=False,
            can_create=False,
            submit_flag=False,
            error=PermissionError_("User bot@ledger.example does not have doctype access via role permission"),
        ),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert "cannot create" in result.output.lower()
    assert "not storing" in result.output.lower()


# --- fencing the lock fields (runningtab.period.fence) ---------------------------


def _passing_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(refused=True, can_create=True, submit_flag=False),
    )


def test_provision_reports_the_fence_install_and_proof_and_still_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    _passing_probe(monkeypatch)
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 0, result.output
    assert stored == [("newkey", "newsecret")]
    lowered = result.output.lower()
    assert "fence:" in lowered
    assert "unchanged company.accounts_frozen_till_date" in lowered
    assert LOCK_FIELDS[0] in lowered
    assert LOCK_FIELDS[1] in lowered
    assert "not refused" not in lowered


def test_provision_refuses_to_store_when_the_bots_key_moves_the_frozen_date(monkeypatch: pytest.MonkeyPatch) -> None:
    _passing_probe(monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "prove_fence",
        lambda admin, bot, company, probe_role: FenceProof(
            date_refused=False, role_refused=True, date_how="took", role_how="reset", restored=(LOCK_FIELDS[0],)
        ),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    lowered = result.output.lower()
    assert LOCK_FIELDS[0] in lowered
    assert "not refused" in lowered
    assert "already rotated" in lowered


def test_provision_refuses_to_store_when_the_bots_key_moves_the_bypass_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _passing_probe(monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "prove_fence",
        lambda admin, bot, company, probe_role: FenceProof(
            date_refused=True, role_refused=False, date_how="reset", role_how="took", restored=(LOCK_FIELDS[1],)
        ),
    )
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    assert LOCK_FIELDS[1] in result.output.lower()


def test_provision_fails_after_rotation_when_proving_the_fence_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A failure inside prove_fence itself -- e.g. the restore-verification
    # PeriodError, or any other exception a real write/read can raise -- must
    # not look like a passed check, and the operator must be told both that
    # the secret already rotated and that a lock field may still be moved.
    _passing_probe(monkeypatch)

    def _broken_prove_fence(admin, bot, company, probe_role):
        raise RuntimeError("the fence probe could not restore accounts_frozen_till_date")

    monkeypatch.setattr(cli_module, "prove_fence", _broken_prove_fence)
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert stored == []
    lowered = result.output.lower()
    assert "already rotated" in lowered
    assert "lock field may have been left moved" in lowered
    assert "could not restore accounts_frozen_till_date" in lowered


def test_provision_reader_never_touches_the_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    # "The reader profile only gains its grant" -- no Property Setter install,
    # no proof write, on a profile that never holds a Company write at all.
    calls: list[str] = []
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: FAKE_REPORT)
    monkeypatch.setattr(access_module, "probe_submit", lambda config, *, company: SubmitProbe(refused=True))
    monkeypatch.setattr(cli_module, "install_fence", lambda client, probe_role: calls.append("install_fence") or [])
    monkeypatch.setattr(
        cli_module,
        "prove_fence",
        lambda admin, bot, company, probe_role: (
            calls.append("prove_fence")
            or FenceProof(date_refused=True, role_refused=True, date_how="reset", role_how="reset", restored=())
        ),
    )
    _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--profile", "reader", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "fence:" not in result.output.lower()


def test_provision_declined_at_the_prompt_provisions_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: called.append(1) or FAKE_REPORT)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books"], input="n\n")

    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    assert called == []


def test_provision_requires_the_target_spelled_out() -> None:
    result = runner.invoke(cli_module.app, ["access", "provision"])
    assert result.exit_code != 0
    assert "target" in result.output.lower()


def test_verify_reports_refused_and_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access_module, "probe_submit", lambda config, *, company: SubmitProbe(refused=True))

    result = runner.invoke(cli_module.app, ["access", "verify", "--target", "books"])

    assert result.exit_code == 0
    assert "refused" in result.output.lower()


def test_verify_exits_1_when_another_doctype_still_allows_a_prohibited_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access_module, "probe_submit", lambda config, *, company: SubmitProbe(refused=True))
    monkeypatch.setattr(
        access_module,
        "prohibited_flags_allowed",
        lambda config, *, doctype, ptypes: (
            {"submit": False, "cancel": False, "amend": True}
            if doctype == "Bank Transaction"
            else dict.fromkeys(ptypes, False)
        ),
    )

    result = runner.invoke(cli_module.app, ["access", "verify", "--target", "books"])

    assert result.exit_code == 1
    assert "bank transaction" in result.output.lower()
    assert "amend" in result.output.lower()


def test_verify_exits_1_when_a_granted_flag_is_not_effective(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access_module, "probe_submit", lambda config, *, company: SubmitProbe(refused=True))
    monkeypatch.setattr(
        access_module,
        "granted_flags_effective",
        lambda config, *, doctype, ptypes: (
            {"read": True, "write": False, "create": True, "delete": True}
            if doctype == "Account"
            else dict.fromkeys(ptypes, True)
        ),
    )

    result = runner.invoke(cli_module.app, ["access", "verify", "--target", "books"])

    assert result.exit_code == 1
    assert "account" in result.output.lower()
    assert "write" in result.output.lower()


def test_verify_reports_can_submit_and_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access_module, "probe_submit", lambda config, *, company: SubmitProbe(refused=False, document_name="ACC-JV-1")
    )

    result = runner.invoke(cli_module.app, ["access", "verify", "--target", "books"])

    assert result.exit_code == 1
    assert "can submit" in result.output.lower()
    assert "acc-jv-1" in result.output.lower()


def test_verify_reports_inconclusive_rather_than_can_submit_when_locked_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from icedcoffee_sync.frappe.errors import PermissionError_

    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: SubmitProbe(
            refused=False,
            can_create=False,
            submit_flag=False,
            error=PermissionError_("User bot@ledger.example does not have doctype access via role permission"),
        ),
    )

    result = runner.invoke(cli_module.app, ["access", "verify", "--target", "books"])

    assert result.exit_code == 1
    assert "can submit" not in result.output.lower()
    assert "could not be proven" in result.output.lower()


def test_verify_refuses_the_test_target(monkeypatch: pytest.MonkeyPatch) -> None:
    # `access provision --target test` never persists the bot key it
    monkeypatch.setattr(
        access_module,
        "probe_submit",
        lambda config, *, company: (_ for _ in ()).throw(AssertionError("must not probe for --target test")),
    )

    result = runner.invoke(cli_module.app, ["access", "verify", "--target", "test"])

    assert result.exit_code != 0
    assert "not supported" in result.output.lower()
    assert "provision" in result.output.lower()


def test_verify_uses_the_given_company_without_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_probe(config: SiteConfig, *, company: str) -> SubmitProbe:
        seen.append(company)
        return SubmitProbe(refused=True)

    monkeypatch.setattr(access_module, "probe_submit", fake_probe)

    class _NoCompanyListing(_FakeAccessClient):
        def list(self, doctype: str, **kwargs: Any) -> Any:
            if doctype == "Company":
                raise AssertionError("must not list companies when --company is given")
            return super().list(doctype, **kwargs)

    monkeypatch.setattr(cli_module, "Client", lambda config: _NoCompanyListing(config))

    result = runner.invoke(cli_module.app, ["access", "verify", "--target", "books", "--company", "Explicit Co"])

    assert result.exit_code == 0
    assert seen == ["Explicit Co"]


class _SiteWithoutCashForecast(_FakeAccessClient):
    def list(self, doctype: str, **kwargs: Any) -> Any:
        if doctype == "Cash Forecast":
            raise DoesNotExistError("not found", error_type="DoesNotExistError", status=404)
        return super().list(doctype, **kwargs)


def test_provision_refuses_in_one_sentence_when_the_cash_forecast_doctype_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "Client", lambda config: _SiteWithoutCashForecast(config))
    touched: list[str] = []
    monkeypatch.setattr(cli_module, "install_fence", lambda client, probe_role: touched.append("fence") or [])
    monkeypatch.setattr(cli_module, "provision", lambda client, *_: touched.append("provision") or FAKE_REPORT)
    stored = _stub_storer(monkeypatch)

    result = runner.invoke(cli_module.app, ["access", "provision", "--target", "books", "--yes"])

    assert result.exit_code == 1
    assert "the Cash Forecast doctype is not installed on https://fake.example.test" in result.output
    assert "runningtab forecast install-doctype --target books" in result.output
    assert touched == []
    assert stored == []


def test_verify_refuses_in_one_sentence_when_the_cash_forecast_doctype_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "Client", lambda config: _SiteWithoutCashForecast(config))
    asked: list[str] = []
    monkeypatch.setattr(access_module, "probe_submit", lambda config, *, company: asked.append(company))

    result = runner.invoke(cli_module.app, ["access", "verify", "--target", "books", "--company", "X"])

    assert result.exit_code == 1
    assert "runningtab forecast install-doctype --target books" in result.output
    assert asked == []
