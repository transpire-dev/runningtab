"""Which site the CLI talks to, and how hard it is to reach the real one."""

import pytest

from runningtab.mercury.client import BASE_URLS, MercuryEnv
from runningtab.site import (
    MERCURY_ADMIN_ENV,
    MERCURY_SANDBOX_ENV,
    MissingSiteCredentialError,
    SiteCredentialError,
    Target,
    admin_config,
    ingest_config,
    is_books_site,
    is_test_site,
    mercury_admin_token,
    mercury_write_config,
    site_config,
    store_books_credential,
)
from tests.conftest import EXAMPLE_BOOKS

SITE = EXAMPLE_BOOKS.site


def test_test_target_reads_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    config = site_config(SITE, Target.TEST)
    assert config.url == "https://test-ledger.example"


def test_missing_test_credentials_say_which_variable(monkeypatch) -> None:
    # An exported URL puts the target on the environment, and a missing half
    # of that credential names itself rather than reaching for the stored one.
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.delenv("RUNNINGTAB_TEST_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="RUNNINGTAB_TEST_SECRET"):
        site_config(SITE, Target.TEST)


def test_the_test_target_allows_the_allowlisted_host(monkeypatch) -> None:
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    assert is_test_site(SITE, "https://test-ledger.example")
    config = site_config(SITE, Target.TEST)
    assert config.url == "https://test-ledger.example"


def test_the_test_target_refuses_the_books_host(monkeypatch) -> None:
    # Fail, never skip or warn: pointing the "test" target at the ledger is the
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", f"https://{SITE.host}")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    assert not is_test_site(SITE, f"https://{SITE.host}")
    with pytest.raises(RuntimeError, match="test_hosts"):
        site_config(SITE, Target.TEST)


def test_the_test_target_refuses_an_unknown_host_and_names_the_allowlist(monkeypatch) -> None:
    # Not the books host either -- some other custom domain or alias. The
    unknown = "https://ledger-alias.example"
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", unknown)
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    assert not is_test_site(SITE, unknown)
    with pytest.raises(RuntimeError, match="test_hosts") as excinfo:
        site_config(SITE, Target.TEST)
    assert "ledger-alias.example" in str(excinfo.value)


def test_a_refused_test_host_is_the_credential_error_a_report_can_print(monkeypatch) -> None:
    # Every other way this function refuses raises SiteCredentialError, which
    # `runningtab health` turns into an ERROR section. A bare RuntimeError from
    # this one branch escapes that and takes the whole report with it.
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://ledger-alias.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    with pytest.raises(SiteCredentialError, match="test_hosts"):
        site_config(SITE, Target.TEST)


def test_the_host_check_does_not_false_positive_on_an_unrelated_substring(monkeypatch) -> None:
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", f"https://test-ledger.example/hook?from={SITE.host}")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    config = site_config(SITE, Target.TEST)
    assert config.url == f"https://test-ledger.example/hook?from={SITE.host}"


def test_the_host_check_catches_a_fully_qualified_trailing_dot() -> None:
    assert is_books_site(SITE, f"https://{SITE.host}.")


def test_the_test_target_allows_a_fully_qualified_trailing_dot(monkeypatch) -> None:
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example.")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    config = site_config(SITE, Target.TEST)
    assert config.url == "https://test-ledger.example."


def test_the_host_check_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", f"https://{SITE.host.upper()}")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    with pytest.raises(RuntimeError, match="test_hosts"):
        site_config(SITE, Target.TEST)


def test_the_test_target_allows_the_allowlisted_host_case_insensitively(monkeypatch) -> None:
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://TEST-LEDGER.EXAMPLE")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    config = site_config(SITE, Target.TEST)
    assert config.url == "https://TEST-LEDGER.EXAMPLE"


def test_is_test_site_checks_the_allowlist_exactly() -> None:
    (only_host,) = SITE.test_hosts
    assert is_test_site(SITE, f"https://{only_host}")
    assert not is_test_site(SITE, "https://ledger-alias.example")
    assert not is_test_site(SITE, f"https://{SITE.host}")


def test_the_books_target_never_reads_the_test_environment(monkeypatch) -> None:
    # The books credential comes from Secret Manager at the moment it is used.
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    monkeypatch.setattr("runningtab.site._fetch_books_credential", lambda *_: ("https://books", "bk", "bs"))
    assert site_config(SITE, Target.BOOKS).url == "https://books"


def test_readonly_reads_the_readers_own_secret(monkeypatch) -> None:
    # Two secrets, so read access can be given to something write access is
    # not. Asserting on which secret is asked for is the whole point: the
    # separation is worth nothing if both configs resolve to the same key.
    asked: list[str] = []

    def _fetch(site, secret_id):
        asked.append(secret_id)
        return "https://books", "k", "s"

    monkeypatch.setattr("runningtab.site._fetch_books_credential", _fetch)
    site_config(SITE, Target.BOOKS, readonly=True)
    site_config(SITE, Target.BOOKS)
    assert asked == [SITE.books_readonly_secret, SITE.books_secret]


def test_store_books_credential_writes_the_key_secret_payload(monkeypatch) -> None:
    # Recorded, not mocked-and-trusted: what matters is the exact request
    requests = []

    class _FakeVersion:
        name = f"projects/{SITE.secret_project}/secrets/{SITE.books_secret}/versions/7"

    class _FakeSecretManagerClient:
        def add_secret_version(self, request):
            requests.append(request)
            return _FakeVersion()

    monkeypatch.setattr("google.cloud.secretmanager.SecretManagerServiceClient", lambda: _FakeSecretManagerClient())

    version = store_books_credential(SITE, "newkey", "newsecret", SITE.books_secret)

    assert version == f"projects/{SITE.secret_project}/secrets/{SITE.books_secret}/versions/7"
    assert requests == [
        {
            "parent": f"projects/{SITE.secret_project}/secrets/{SITE.books_secret}",
            "payload": {"data": b"newkey:newsecret"},
        }
    ]


def test_admin_config_for_books_reads_the_admin_token_only(monkeypatch) -> None:
    # Provisioning needs System Manager, which the stored books credential
    # is not; the administrator's token comes from the environment alone.
    monkeypatch.setenv("RUNNINGTAB_ADMIN_TOKEN", "adminkey:adminsecret")
    monkeypatch.setattr(
        "runningtab.site._fetch_books_credential", lambda *_: (_ for _ in ()).throw(AssertionError("touched"))
    )
    config = admin_config(SITE, Target.BOOKS)
    assert config.url == f"https://{SITE.host}"
    assert config.api_key == "adminkey"
    assert config.api_secret == "adminsecret"


def test_admin_config_accepts_frappe_token_prefix(monkeypatch) -> None:
    # Frappe hands the credential out as the Authorization header value,
    # "token <key>:<secret>"; pasting that verbatim must work.
    monkeypatch.setenv("RUNNINGTAB_ADMIN_TOKEN", "token adminkey:adminsecret")
    config = admin_config(SITE, Target.BOOKS)
    assert config.api_key == "adminkey"
    assert config.api_secret == "adminsecret"


def test_admin_config_for_books_names_the_missing_variable(monkeypatch) -> None:
    monkeypatch.delenv("RUNNINGTAB_ADMIN_TOKEN", raising=False)
    with pytest.raises(SiteCredentialError, match="RUNNINGTAB_ADMIN_TOKEN"):
        admin_config(SITE, Target.BOOKS)


def test_admin_config_for_books_refuses_a_token_without_a_colon(monkeypatch) -> None:
    monkeypatch.setenv("RUNNINGTAB_ADMIN_TOKEN", "adminkeyonly")
    with pytest.raises(SiteCredentialError, match="RUNNINGTAB_ADMIN_TOKEN"):
        admin_config(SITE, Target.BOOKS)


def test_admin_config_for_the_test_target_is_the_test_credential(monkeypatch) -> None:
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    monkeypatch.delenv("RUNNINGTAB_ADMIN_TOKEN", raising=False)
    assert admin_config(SITE, Target.TEST) == site_config(SITE, Target.TEST)


def test_the_ingest_credential_may_submit_where_the_plain_one_may_not(monkeypatch) -> None:
    # Same key, one more capability: the ingest submits the Bank Transactions it
    # creates and nothing else needs to. Everything else keeps the drafts-only
    # client, so a stray docstatus anywhere else still fails before it is sent.
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    plain = site_config(SITE, Target.TEST)
    ingest = ingest_config(SITE, Target.TEST)
    assert plain.may_submit is False
    assert ingest.may_submit is True
    assert (ingest.url, ingest.api_key, ingest.api_secret) == (plain.url, plain.api_key, plain.api_secret)


def test_the_test_target_prefers_the_environment_over_the_secret(monkeypatch) -> None:
    # The environment is the override, so a developer pointed at their own
    # bench never silently reaches the shared test site instead. Secret
    # Manager is not consulted at all -- not consulted-and-ignored.
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "envkey")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "envsecret")
    monkeypatch.setattr(
        "runningtab.site._fetch_test_credential", lambda *_: (_ for _ in ()).throw(AssertionError("touched"))
    )
    config = site_config(SITE, Target.TEST)
    assert (config.url, config.api_key, config.api_secret) == (
        "https://test-ledger.example",
        "envkey",
        "envsecret",
    )


def test_the_test_target_still_requires_every_variable_once_the_url_is_set(monkeypatch) -> None:
    # Half an exported credential is a mistake, not a request for the stored
    # one: falling back here would use a key the developer did not ask for.
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.delenv("RUNNINGTAB_TEST_KEY", raising=False)
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    monkeypatch.setattr(
        "runningtab.site._fetch_test_credential", lambda *_: (_ for _ in ()).throw(AssertionError("touched"))
    )
    with pytest.raises(RuntimeError, match="RUNNINGTAB_TEST_KEY"):
        site_config(SITE, Target.TEST)


def test_the_test_target_falls_back_to_secret_manager(monkeypatch) -> None:
    # An agent running under ADC has no exported credential, and the test
    # site's own key is a *site* key: it reaches one disposable site.
    monkeypatch.delenv("RUNNINGTAB_TEST_URL", raising=False)
    monkeypatch.delenv("RUNNINGTAB_TEST_KEY", raising=False)
    monkeypatch.delenv("RUNNINGTAB_TEST_SECRET", raising=False)
    asked: list[str] = []

    def _payload(site, name):
        asked.append(name)
        return "secretkey:secretsecret"

    monkeypatch.setattr("runningtab.site._secret_payload", _payload)
    config = site_config(SITE, Target.TEST)
    assert asked == [SITE.test_admin_secret]
    assert (config.api_key, config.api_secret) == ("secretkey", "secretsecret")
    # The URL is the allowlist's one host, so the default and the guard
    # cannot drift apart into a default the guard then refuses.
    (only_host,) = SITE.test_hosts
    assert config.url == f"https://{only_host}"
    assert is_test_site(SITE, config.url)


def test_the_stored_test_credential_accepts_the_frappe_token_prefix(monkeypatch) -> None:
    # Frappe shows the credential as the Authorization header value,
    # "token <key>:<secret>"; a payload pasted that way must still parse.
    monkeypatch.delenv("RUNNINGTAB_TEST_URL", raising=False)
    monkeypatch.setattr("runningtab.site._secret_payload", lambda *_: "token secretkey:secretsecret")
    config = site_config(SITE, Target.TEST)
    assert (config.api_key, config.api_secret) == ("secretkey", "secretsecret")


def test_an_empty_stored_test_credential_names_the_secret(monkeypatch) -> None:
    # The same shape of message the books target gives for its own secret:
    # whoever reads it has to know which secret to go and fill in.
    monkeypatch.delenv("RUNNINGTAB_TEST_URL", raising=False)
    monkeypatch.setattr("runningtab.site._secret_payload", lambda *_: "   ")
    with pytest.raises(SiteCredentialError, match=SITE.test_admin_secret):
        site_config(SITE, Target.TEST)


def test_an_unreadable_test_secret_is_a_credential_error_naming_it(monkeypatch) -> None:
    # `runningtab health` prints a SiteCredentialError as an ERROR section; a raw
    # google.api_core exception out of the middle of it takes the report down.
    monkeypatch.delenv("RUNNINGTAB_TEST_URL", raising=False)

    def _boom(site, name):
        raise PermissionError("403 caller lacks secretmanager.versions.access")

    monkeypatch.setattr("runningtab.site._secret_payload", _boom)
    with pytest.raises(SiteCredentialError, match=SITE.test_admin_secret) as excinfo:
        site_config(SITE, Target.TEST)
    assert "403" in str(excinfo.value)


def test_a_stored_test_credential_without_a_colon_names_the_secret(monkeypatch) -> None:
    monkeypatch.delenv("RUNNINGTAB_TEST_URL", raising=False)
    monkeypatch.setattr("runningtab.site._secret_payload", lambda *_: "keyonly")
    with pytest.raises(SiteCredentialError, match=SITE.test_admin_secret):
        site_config(SITE, Target.TEST)


def test_the_test_credential_is_not_in_the_config_repr(monkeypatch) -> None:
    # The config lands in log lines, tracebacks and failing assertions.
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "envkey")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "envsecret")
    printed = repr(site_config(SITE, Target.TEST))
    assert "envsecret" not in printed
    assert "envkey" not in printed


def test_the_mercury_write_token_is_never_a_secret_manager_secret() -> None:
    # Mercury issues no category-only write scope, so this is a Read and Write
    # token that can also send money. It exists for one sitting at a person's
    # keyboard and is revoked afterwards; storing it would put a money-moving
    # credential exactly where every read-only one lives.
    import inspect

    import runningtab.site

    source = inspect.getsource(runningtab.site.mercury_admin_token)
    assert "secretmanager" not in source
    assert MERCURY_ADMIN_ENV == "MERCURY_ADMIN_TOKEN"


def test_the_mercury_write_token_names_the_variable_it_wants(monkeypatch) -> None:
    monkeypatch.delenv(MERCURY_ADMIN_ENV, raising=False)
    with pytest.raises(MissingSiteCredentialError, match=MERCURY_ADMIN_ENV):
        mercury_admin_token(MercuryEnv.PRODUCTION)


def test_the_sandbox_write_token_is_the_sandbox_variable(monkeypatch) -> None:
    monkeypatch.setenv(MERCURY_SANDBOX_ENV, "sandbox-token")
    monkeypatch.delenv(MERCURY_ADMIN_ENV, raising=False)
    config = mercury_write_config(MercuryEnv.SANDBOX)
    assert config.token == "sandbox-token"
    assert config.base_url == BASE_URLS[MercuryEnv.SANDBOX]


def test_a_blank_mercury_write_token_is_not_a_token(monkeypatch) -> None:
    monkeypatch.setenv(MERCURY_ADMIN_ENV, "   ")
    with pytest.raises(MissingSiteCredentialError, match=MERCURY_ADMIN_ENV):
        mercury_admin_token(MercuryEnv.PRODUCTION)


def test_the_production_write_config_never_defaults_to_the_sandbox(monkeypatch) -> None:
    monkeypatch.setenv(MERCURY_ADMIN_ENV, "write-token")
    config = mercury_write_config(MercuryEnv.PRODUCTION)
    assert config.base_url == BASE_URLS[MercuryEnv.PRODUCTION]
    assert config.token == "write-token"
