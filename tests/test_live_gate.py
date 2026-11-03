"""Unit tests for the live-suite gate: RUNNINGTAB_LIVE, not RUNNINGTAB_TEST_URL.

`require_live_target` is what every live fixture in this directory calls
before touching the network -- see the `client` fixtures in
`test_live_access.py` and `test_live_coa.py`. It is a plain function, not a
fixture, because pytest refuses to let a test call a `@pytest.fixture`
function directly ("Fixtures are not meant to be called directly"), and that
is exactly what these tests need to do to prove the gate works without
actually reaching a live site.

Deliberately unmarked (no `live_runningtab`): these run in the default suite, which
is the whole point -- they are what proves the default suite never dials out.
"""

import pytest

from runningtab.site import SiteCredentialError
from tests.conftest import live_books, require_live_target


def test_gate_skips_without_runningtab_live_even_with_test_url_set(monkeypatch) -> None:
    monkeypatch.delenv("RUNNINGTAB_LIVE", raising=False)
    monkeypatch.setenv("RUNNINGTAB_TEST_URL", "https://test-ledger.example")
    monkeypatch.setenv("RUNNINGTAB_TEST_KEY", "k")
    monkeypatch.setenv("RUNNINGTAB_TEST_SECRET", "s")
    with pytest.raises(pytest.skip.Exception, match="RUNNINGTAB_LIVE not set"):
        require_live_target()


def test_gate_reaches_site_config_once_runningtab_live_is_set(monkeypatch) -> None:
    # No RUNNINGTAB_TEST_* exported either, so site_config falls to its Secret
    # Manager path -- stub that read so this stays a unit test, never a live
    # one, and assert the failure names the secret the way site_config's own
    # tests do for the same fallback.
    monkeypatch.setenv("RUNNINGTAB_LIVE", "1")
    monkeypatch.delenv("RUNNINGTAB_TEST_URL", raising=False)
    monkeypatch.delenv("RUNNINGTAB_TEST_KEY", raising=False)
    monkeypatch.delenv("RUNNINGTAB_TEST_SECRET", raising=False)

    def _boom(site, name):
        raise PermissionError("403 caller lacks secretmanager.versions.access")

    monkeypatch.setattr("runningtab.site._secret_payload", _boom)
    with pytest.raises(SiteCredentialError, match=live_books().site.test_admin_secret):
        require_live_target()
