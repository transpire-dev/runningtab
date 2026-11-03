set dotenv-load := false

# List available recipes
default:
    @just --list

# Install the virtualenv
install:
    uv sync

# Install dependencies exactly as locked (CI)
install-ci:
    uv sync --frozen

# Check lock file consistency
lock-check:
    uv lock --locked

# Lint (check-only, CI-safe)
lint:
    uv run ruff check --no-fix .
    uv run ruff format --check .

# Lint with auto-fix (local dev)
lint-fix:
    uv run ruff check --fix .
    uv run ruff format .

# Type check (src and tests only)
typecheck:
    uv run ty check src tests

# Dependency audit
deps:
    uv run deptry src

# Run tests (excludes live_runningtab, which writes to a real ERPNext site); extra args go to pytest
test *args:
    uv run pytest -m "not live_runningtab" {{args}}

# Full static validation: lock, lint, typecheck, deps
check: lock-check lint typecheck deps

# Check the Zed grammar parses and highlights examples/books/chart.coa (needs Node.js and a C compiler)
zed-coa-check:
    {{just_executable()}} -f zed-coa/justfile check

# The two recipes below write to a real ERPNext site, the disposable test
# site `books.toml`'s `secrets.test_admin` (or RUNNINGTAB_TEST_*) reaches.
# They take the books directory from RUNNINGTAB_BOOKS, which names your own
# books, not examples/books: the example's hosts and secrets are fake.

# Run the live suite against the disposable test site. Writes to it; takes several minutes
test-live:
    @test -n "${RUNNINGTAB_BOOKS:-}" || { echo "test-live: set RUNNINGTAB_BOOKS to the books directory whose test site to use" >&2; exit 2; }
    env RUNNINGTAB_LIVE=1 uv run pytest tests -m live_runningtab -q

# Seed the period scenarios on the disposable test site and capture their GL into tests/fixtures/period/. Run once.
capture-period-fixtures:
    @test -n "${RUNNINGTAB_BOOKS:-}" || { echo "capture-period-fixtures: set RUNNINGTAB_BOOKS to the books directory whose test site to use" >&2; exit 2; }
    uv run python tests/capture_period_fixtures.py
