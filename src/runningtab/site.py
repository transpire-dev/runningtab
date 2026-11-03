"""Which ERPNext site, and where its credentials come from.

Two targets, deliberately asymmetric. `test` is a disposable site whose
credentials come from the environment, falling back to a stored key for that
one site when nothing is exported, and it refuses to run against anything but
a host named in the books' `test_hosts`, no matter what the environment or the
secret says. `books` is the real ledger, and its
credential is fetched from Secret Manager at the moment it is used -- never
held during development, and never substitutable by an exported variable.

There is no default target. Reaching the ledger requires naming it.

Which hosts, which secrets and which project come from the books directory's
`books.toml` (`runningtab.books`), as a `Site`.
"""

from __future__ import annotations

import dataclasses
import enum
import os
from urllib.parse import urlsplit

from icedcoffee_sync.frappe.client import SiteConfig

from runningtab.mercury.client import MercuryConfig, MercuryEnv, config_for


@dataclasses.dataclass(frozen=True)
class Site:
    """Where the ledger and its test site are, and which Secret Manager secrets reach them.

    Every secret here is a Secret Manager secret ID in `secret_project`, not a
    credential.
    """

    # The ledger's host. Only `books_secret`'s and `books_readonly_secret`'s
    # credentials are sent there.
    host: str
    # Every host `--target test` may point at. An allowlist, so an alias of the
    # production site that shares nothing textually with its hostname is
    # refused by default; reaching a test site means naming it here.
    # `runningtab.books` refuses a list that names `host` itself.
    test_hosts: frozenset[str]
    secret_project: str
    # The bot's credential: the one that changes the ledger.
    books_secret: str
    # The reader's own credential, held in its own secret so that read access
    # can be granted to something write access is not: `coa plan` runs on every
    # merge request, where the branch decides what code runs, and it has no
    # business holding a key that can change the ledger.
    books_readonly_secret: str
    # Mercury's token, which is **read-only**: the integration never writes a
    # Mercury record, so nothing in production should hold a credential that
    # could.
    mercury_secret: str
    # A System Manager key for the test site, so anything running under
    # application default credentials -- an agent, a scheduled job -- reaches
    # the disposable site with nothing exported. It is a *site* API key: it
    # reaches that one site and cannot touch another, which is why one may be
    # stored while the Frappe Cloud team token, which can reach the books
    # site, stays with a human.
    test_admin_secret: str


# The sandbox token stays an environment variable. It reads seeded data on a
# host that is not the bank, and putting it in Secret Manager would suggest the
# two are interchangeable.
MERCURY_SANDBOX_ENV = "MERCURY_SANDBOX_TOKEN"


class Target(enum.StrEnum):
    """Which site a command acts on."""

    TEST = "test"
    BOOKS = "books"


class Profile(enum.StrEnum):
    """Which identity on that site: the one that changes the ledger, or the one that only reads it."""

    BOT = "bot"
    READER = "reader"


def _hostname(url: str) -> str:
    """`url`'s host, lowercased, trailing dot stripped -- the one thing that identifies a site.

    A fully-qualified "ledger.example." resolves to the same site,
    and a hostname mentioned in a path or query string is not that site.
    """
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def normalised_host(host: str) -> str:
    """A bare hostname as `_hostname` would read it out of a URL: lowercased, trailing dot stripped."""
    return host.lower().rstrip(".")


def is_books_site(site: Site, url: str) -> bool:
    """True when `url`'s host is the books site's. Identification only; `is_test_site` is the guard."""
    return _hostname(url) == normalised_host(site.host)


def is_test_site(site: Site, url: str) -> bool:
    """True when `url`'s host is in `site.test_hosts` -- the guard the test target and live fixtures share."""
    return _hostname(url) in {normalised_host(host) for host in site.test_hosts}


class SiteCredentialError(RuntimeError):
    """A credential this target needs could not be obtained, for any reason.

    Its own type so a command that reports rather than crashes can print what
    went wrong instead of handing an operator a traceback. A subclass of
    `RuntimeError`, so nothing that already catches one changes behaviour.
    """


class MissingSiteCredentialError(SiteCredentialError):
    """A credential this target needs is not in the environment.

    Distinct from its parent because "nobody exported this" and "Secret
    Manager refused us" send whoever reads the message to different places.
    """


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingSiteCredentialError(f"{name} is not set; export the test site's credentials first")
    return value


def _split_token(value: str, source: str) -> tuple[str, str]:
    """Split a Frappe credential into the key and secret it joins with a colon.

    Accepts the value bare (`key:secret`) or exactly as ERPNext displays it in
    the Authorization header (`token key:secret`), so a paste from the app
    works without editing, wherever it was pasted. `source` names where the
    value came from, since that is what whoever reads the message has to fix.
    """
    value = value.strip().removeprefix("token ").strip()
    key, _, secret = value.partition(":")
    if not (key and secret):
        raise SiteCredentialError(f"{source} is not in the expected 'key:secret' form (a leading 'token ' is allowed)")
    return key, secret


def _fetch_books_credential(site: Site, secret_id: str) -> tuple[str, str, str]:
    """Read the books site's URL, key, and secret from Secret Manager.

    Every way the read can fail -- an absent secret, a refused permission, an
    unreachable API, a payload in the wrong shape -- becomes a
    `SiteCredentialError` carrying the underlying message, so a command that
    reports can print it instead of crashing halfway through.
    """
    project = site.secret_project
    path = f"projects/{project}/secrets/{secret_id}/versions/latest"
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        payload = client.access_secret_version(request={"name": path}).payload.data.decode()
    except Exception as exc:
        raise SiteCredentialError(f"could not read secret {secret_id} in {project}: {exc}") from exc
    key, _, secret = payload.partition(":")
    if not (key and secret):
        raise SiteCredentialError(f"secret {secret_id} in {project} is not in the expected 'key:secret' form")
    return f"https://{site.host}", key, secret


def _secret_payload(site: Site, secret_id: str) -> str:
    """The latest version of one secret in `site.secret_project`, decoded and stripped.

    Split out from its caller so a test can stub the read alone and still
    exercise the parsing, the empty check, and the error wrapping for real.
    """
    from google.cloud import secretmanager

    path = f"projects/{site.secret_project}/secrets/{secret_id}/versions/latest"
    client = secretmanager.SecretManagerServiceClient()
    return client.access_secret_version(request={"name": path}).payload.data.decode().strip()


def _fetch_test_credential(site: Site) -> tuple[str, str]:
    """Read the test site's stored System Manager key from Secret Manager.

    Every way the read can fail -- an absent secret, a refused permission, an
    unreachable API -- becomes a `SiteCredentialError` naming the secret, so a
    command that reports can print it instead of crashing partway through.
    """
    secret_id, project = site.test_admin_secret, site.secret_project
    try:
        payload = _secret_payload(site, secret_id)
    except Exception as exc:
        raise SiteCredentialError(
            f"could not read secret {secret_id} in {project}: {exc}. "
            f"That secret holds a System Manager API key for the test site."
        ) from exc
    if not payload:
        raise MissingSiteCredentialError(
            f"secret {secret_id} in {project} is empty; it should hold the test site's '<api_key>:<api_secret>'"
        )
    return _split_token(payload, f"secret {secret_id} in {project}")


def _test_site_url(site: Site) -> str:
    """The allowlisted test host as a URL.

    Derived from `site.test_hosts` rather than kept as a second setting: a
    default the allowlist then refuses is a stall whose cause nobody can read.
    A second allowlisted host makes the choice ambiguous, and saying so beats
    picking one.
    """
    if len(site.test_hosts) != 1:
        raise SiteCredentialError(
            f"test_hosts names {len(site.test_hosts)} hosts "
            f"({', '.join(sorted(site.test_hosts))}); set RUNNINGTAB_TEST_URL to say which one"
        )
    (host,) = site.test_hosts
    return f"https://{host}"


def store_books_credential(site: Site, key: str, secret: str, secret_id: str) -> str:
    """Store a freshly minted credential as the latest version of `secret_id`.

    Mirrors `_fetch_books_credential`'s `"key:secret"` payload shape exactly --
    that is the only format `site_config` knows how to read back.

    Args:
        site: Which project holds the secret.
        key: The bot's API key.
        secret: The bot's API secret.
        secret_id: `site.books_secret` for the bot, `site.books_readonly_secret` for the reader.

    Returns:
        The created version's resource name (`.../versions/<n>`).
    """
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{site.secret_project}/secrets/{secret_id}"
    payload = f"{key}:{secret}".encode()
    response = client.add_secret_version(request={"parent": parent, "payload": {"data": payload}})
    return response.name


def mercury_token(site: Site, env: MercuryEnv) -> str:
    """The Mercury token for one deployment.

    Asymmetric on purpose, the same way the ERPNext targets are: the sandbox's
    token is an exported variable because it reads seeded data, and the real
    one is fetched from Secret Manager at the moment it is used.

    Every way this can fail is a `SiteCredentialError` carrying the underlying
    message, so a report can print it instead of crashing: `runningtab health` has
    five sections to print, and a traceback out of the middle of one takes the
    other four with it. It does not claim to know *why* -- a revoked permission
    and a secret nobody has created yet both arrive here, and the API's own
    error says which.
    """
    if env is MercuryEnv.SANDBOX:
        if not os.environ.get(MERCURY_SANDBOX_ENV):
            raise MissingSiteCredentialError(
                f"{MERCURY_SANDBOX_ENV} is not set. Create an API token inside the Mercury sandbox dashboard "
                f"(a production token does not work there) and export it."
            )
        return _require(MERCURY_SANDBOX_ENV)
    secret_id, project = site.mercury_secret, site.secret_project
    path = f"projects/{project}/secrets/{secret_id}/versions/latest"
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        token = client.access_secret_version(request={"name": path}).payload.data.decode().strip()
    except Exception as exc:
        raise SiteCredentialError(
            f"could not read secret {secret_id} in {project}: {exc}. That secret holds the read-only Mercury token."
        ) from exc
    if not token:
        raise MissingSiteCredentialError(
            f"secret {secret_id} in {project} is empty; it should hold a read-only Mercury token"
        )
    return token


def mercury_config(site: Site, env: MercuryEnv) -> MercuryConfig:
    """Where Mercury is and which token reads it. Never defaults to production."""
    return config_for(env, mercury_token(site, env))


# The administrator's own credential, as one variable because that is how
# ERPNext hands it out: a single `<api_key>:<api_secret>` token.
RUNNINGTAB_ADMIN_ENV = "RUNNINGTAB_ADMIN_TOKEN"

# Mercury's write token, and the one credential here deliberately kept *out* of
# Secret Manager. Mercury issues no category-only write scope -- its custom-token
# picker offers "Fetch Categories" and nothing else for categories -- so the only
# token that can create a category is a full Read and Write one, whose
# server-side boundary is "may send money" rather than "may edit categories".
# It is minted for one sitting at a person's keyboard, exported by hand, and
# revoked in the dashboard afterwards. Storing it would put a money-moving
# credential exactly where every read-only one lives, and Mercury would downgrade
# it to read-only after 45 unused days anyway.
MERCURY_ADMIN_ENV = "MERCURY_ADMIN_TOKEN"


def mercury_admin_token(env: MercuryEnv) -> str:
    """The token allowed to write a Mercury category, from the environment and nowhere else.

    Sandbox reuses `MERCURY_SANDBOX_TOKEN`: it reads and writes seeded data on
    a host that is not the bank, so it needs no ceremony. Production takes
    `MERCURY_ADMIN_TOKEN`, which no code stores and nothing but an interactive
    shell sets.

    Raises:
        MissingSiteCredentialError: The variable is unset, named in the message
            along with what it is for, so the refusal lands before any request.
    """
    if env is MercuryEnv.SANDBOX:
        sandbox = os.environ.get(MERCURY_SANDBOX_ENV, "").strip()
        if not sandbox:
            raise MissingSiteCredentialError(
                f"{MERCURY_SANDBOX_ENV} is not set. Create an API token inside the Mercury sandbox dashboard "
                f"(a production token does not work there) and export it."
            )
        return sandbox
    value = os.environ.get(MERCURY_ADMIN_ENV, "").strip()
    if not value:
        raise MissingSiteCredentialError(
            f"{MERCURY_ADMIN_ENV} is not set. Writing a Mercury category needs a Read and Write token, "
            f"which Mercury will only issue with an IP allowlist and which can also send money -- so it is "
            f"never stored. Mint one in the dashboard, export it into this shell for this run, and revoke it "
            f"afterwards."
        )
    return value


def mercury_write_config(env: MercuryEnv) -> MercuryConfig:
    """Where Mercury is and which token may write a category there. Never defaults to production."""
    return config_for(env, mercury_admin_token(env))


def _admin_token() -> tuple[str, str]:
    """`RUNNINGTAB_ADMIN_TOKEN`, split into the key and secret Frappe joined with a colon."""
    value = os.environ.get(RUNNINGTAB_ADMIN_ENV, "").strip()
    if not value:
        raise MissingSiteCredentialError(
            f"{RUNNINGTAB_ADMIN_ENV} is not set; export your own System Manager API token "
            f"(the 'key:secret' ERPNext generates for your user) first"
        )
    return _split_token(value, RUNNINGTAB_ADMIN_ENV)


def admin_config(site: Site, target: Target) -> SiteConfig:
    """A System Manager credential for provisioning the bot -- a human's token, read from the environment.

    The stored books credential is the bot's own and cannot provision.
    The test site's environment credential is already an administrator's.
    """
    if target is Target.TEST:
        return site_config(site, target)
    key, secret = _admin_token()
    return SiteConfig(url=f"https://{site.host}", api_key=key, api_secret=secret, timeout=60.0)


def site_config(site: Site, target: Target, *, readonly: bool = False) -> SiteConfig:
    """Credentials for one target. May not submit; `ingest_config` is the one caller that needs to.

    `readonly` picks the reader's credential, which the server refuses every
    write for. Only the books target has two: the test site is disposable, so
    its one credential serves both.

    The test target reads the environment first and the stored key only when
    nothing is exported, so a developer pointed at their own bench never
    silently reaches the shared test site instead. An exported
    `RUNNINGTAB_TEST_URL` therefore commits to the environment: a missing half of
    that credential is a mistake to name, not a reason to fall back.
    """
    if target is Target.BOOKS:
        secret_id = site.books_readonly_secret if readonly else site.books_secret
        url, key, secret = _fetch_books_credential(site, secret_id)
        return SiteConfig(url=url, api_key=key, api_secret=secret, timeout=60.0)

    url = os.environ.get("RUNNINGTAB_TEST_URL", "")
    if url:
        key, secret = _require("RUNNINGTAB_TEST_KEY"), _require("RUNNINGTAB_TEST_SECRET")
    else:
        url, (key, secret) = _test_site_url(site), _fetch_test_credential(site)
    if not is_test_site(site, url):
        raise SiteCredentialError(
            f"RUNNINGTAB_TEST_URL ({url}) does not name a host in books.toml's test_hosts "
            f"({', '.join(sorted(site.test_hosts))}); the test target refuses anything not allowlisted there"
        )
    return SiteConfig(url=url, api_key=key, api_secret=secret, timeout=60.0)


def ingest_config(site: Site, target: Target) -> SiteConfig:
    """The bot's credential with submit capability, for the Mercury ingest alone.

    The ingest submits the Bank Transactions it creates: a Bank Transaction
    posts nothing to the general ledger until a person reconciles it, and the
    Bank Reconciliation Tool only sees submitted rows. `icedcoffee-sync`'s client gates
    every submit route behind one flag, so this is the narrowest grant it
    offers; what keeps it to Bank Transactions is that the posting module sets
    a `docstatus` on that one document and nothing else, and that the bot's
    role on the server holds submit on that one doctype and nothing else.
    """
    return dataclasses.replace(site_config(site, target), may_submit=True)
