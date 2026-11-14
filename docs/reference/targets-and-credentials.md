# Targets and credentials

`--target` selects the ERPNext site. `--env` selects the Mercury environment. Both require an explicit value when used; neither has a default.

## Targets and environments

| Option | Value | Destination and requirements |
|---|---|---|
| `--target` | `test` | A disposable ERPNext site. Its host must appear in `books.toml` under `site.test_hosts`. The allowlist may not include `site.host`. |
| `--target` | `books` | Your real ledger at `site.host`. |
| `--env` | `sandbox` | Mercury’s sandbox. Requires `MERCURY_SANDBOX_TOKEN`. |
| `--env` | `production` | Mercury’s production environment. Reads use the token named by `secrets.mercury`. |

Combining `--env sandbox` with `--target books` is a usage error and exits with status `2`.

## ERPNext credentials

### Test target

Every role on `--target test` uses the same administrator credential.

| Condition | Credential source | Site selection |
|---|---|---|
| `RUNNINGTAB_TEST_URL` is set | Both `RUNNINGTAB_TEST_KEY` and `RUNNINGTAB_TEST_SECRET` are required. No stored credential is read. | The URL’s host must be in `site.test_hosts`. |
| `RUNNINGTAB_TEST_URL` is unset | Secret Manager secret named by `secrets.test_admin`, in project `secrets.project`. | Uses the single host in `site.test_hosts`. If several hosts are listed, set `RUNNINGTAB_TEST_URL` and both credential variables to select one. |

### Books target

| Role | Credential source | Requirements |
|---|---|---|
| Reader | Secret Manager secret named by `secrets.books_readonly` | Fetched when used. Environment variables cannot supply this credential. |
| Bot | Secret Manager secret named by `secrets.books` | Fetched when used. Environment variables cannot supply this credential. |
| Administrator | `RUNNINGTAB_ADMIN_TOKEN` | Your own System Manager `key:secret`. A leading `token ` is accepted. |

Administrator commands on `--target books` refuse to proceed before any request when `RUNNINGTAB_ADMIN_TOKEN` is unset. The error names the missing variable.

### Credentials by command

The roles below select separate credentials on `--target books`. On `--target test`, they all use the test administrator credential.

| Role | Commands |
|---|---|
| Reader | `coa plan`, `health`, `period export`, `period status`, `forecast plan` |
| Bot | `coa apply`, `coa prune`, `forecast apply` |
| Bot with submit permission | `mercury sync`, `mercury snapshot` |
| Administrator | `access provision`, `mercury install-fields`, `mercury install-bank-accounts`, `forecast install-doctype`, `period close` |

`access verify` checks the stored `books` credential `--profile` names: the bot's key (`secrets.books`) by default, or the reader's (`secrets.books_readonly`) with `--profile reader`. It refuses `--target test`, whose bot key is never stored.

See the [access model](../explanation/access-model.md) and [access reference](access.md) for related information.

## Mercury credentials

Mercury commands use an ERPNext target and a Mercury environment. The Mercury token is separate from the ERPNext credential.

| Environment and use | Credential source | Requirements |
|---|---|---|
| Production reads | Secret Manager secret named by `secrets.mercury` | A production read-only token. |
| Sandbox access, including `mercury categories apply` | `MERCURY_SANDBOX_TOKEN` | Create the token inside the Mercury sandbox dashboard. A production token does not work in the sandbox. |
| Production `mercury categories apply` | `MERCURY_ADMIN_TOKEN` | A full Read and Write token. runningtab never stores it. |

See [Connect Mercury](../how-to/connect-mercury.md) and the [Mercury reference](mercury.md).

## Google credentials and record buckets

runningtab uses Google Application Default Credentials for Secret Manager access and record bucket operations. To configure local Application Default Credentials, run:

```sh
gcloud auth application-default login
```

| Resource | Configuration in `books.toml` | Used by |
|---|---|---|
| Secret Manager project | `secrets.project` | Reads of the stored ERPNext and Mercury credentials listed above |
| Books record bucket | `record_buckets.books` | `period close`, `period status`, and the closed-period checks in `health` with `--target books` |
| Test record bucket | `record_buckets.test` | `period close`, `period status`, and the closed-period checks in `health` with `--target test` |

The Google identity used for record bucket access needs object-create and object-viewer permissions on the buckets.

## Environment variables

| Variable | Value and source | Used by |
|---|---|---|
| `RUNNINGTAB_BOOKS` | Path to your books directory. Used when `--books` is omitted. There is no default. | Commands that load the books directory; the live test suite |
| `RUNNINGTAB_TEST_URL` | URL of your disposable ERPNext site. The host must be in `site.test_hosts`. | Commands using `--target test` |
| `RUNNINGTAB_TEST_KEY` | Test-site administrator API key. Required when `RUNNINGTAB_TEST_URL` is set. | Commands using `--target test` with an explicit test URL |
| `RUNNINGTAB_TEST_SECRET` | Test-site administrator API secret. Required when `RUNNINGTAB_TEST_URL` is set. | Commands using `--target test` with an explicit test URL |
| `RUNNINGTAB_ADMIN_TOKEN` | Your System Manager `key:secret`; a leading `token ` is accepted. | `access provision`, `mercury install-fields`, `mercury install-bank-accounts`, `forecast install-doctype`, and `period close` on `--target books` |
| `MERCURY_SANDBOX_TOKEN` | Token created inside the Mercury sandbox dashboard. | Mercury commands using `--env sandbox` |
| `MERCURY_ADMIN_TOKEN` | Full Read and Write production Mercury token. Never stored. | `mercury categories apply` using `--env production` |
| `XDG_STATE_HOME` | Base directory for ingest state. Falls back to `~/.local/state`. | Mercury ingest state, unless `--state` is supplied |
| `RUNNINGTAB_LIVE` | Set to `1` to enable the live test suite. | Contributor tests marked `live_runningtab` |

The ingest’s default state path is:

```text
$XDG_STATE_HOME/<name>-books/mercury-<env>-<target>.sqlite3
```

With `XDG_STATE_HOME` unset, it is:

```text
~/.local/state/<name>-books/mercury-<env>-<target>.sqlite3
```

`--state` overrides this path.

`<name>` is `books.toml`'s `name`, lowercased.

See the [books directory reference](books-directory.md) for configuration and [contributing](../contributing.md) for live tests. `RUNNINGTAB_TEST_URL` alone does not enable live tests; they require `RUNNINGTAB_LIVE=1` and are excluded from `just test` and CI’s default suite.