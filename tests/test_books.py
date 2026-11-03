"""The books directory: which one, and what its `books.toml` may say.

runningtab ships no books of its own. Planning against an example chart
because nobody named the real one would report differences against books
nobody meant, so every way of not naming a directory, or of naming one that
says something unexpected, is a refusal by name.
"""

from __future__ import annotations

import pathlib
import shutil
from typing import Any

import pytest
import typer.main
from typer.testing import CliRunner

from runningtab import cli
from runningtab.books import BOOKS_ENV, BooksError, load_books, resolve_books
from runningtab.site import Target
from tests.conftest import EXAMPLE_BOOKS_DIR


def _copy_example(tmp_path: pathlib.Path) -> pathlib.Path:
    books = tmp_path / "books"
    shutil.copytree(EXAMPLE_BOOKS_DIR, books)
    return books


def _rewrite(books: pathlib.Path, old: str, new: str) -> None:
    path = books / "books.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} is not in the example books.toml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_neither_the_flag_nor_the_variable_refuses_naming_both(monkeypatch) -> None:
    monkeypatch.delenv(BOOKS_ENV, raising=False)
    with pytest.raises(BooksError) as excinfo:
        resolve_books(None)
    assert "--books" in str(excinfo.value)
    assert "RUNNINGTAB_BOOKS" in str(excinfo.value)


def test_an_empty_variable_is_not_a_directory(monkeypatch) -> None:
    monkeypatch.setenv(BOOKS_ENV, "")
    with pytest.raises(BooksError, match="RUNNINGTAB_BOOKS"):
        resolve_books(None)


def test_the_variable_names_the_books(monkeypatch) -> None:
    monkeypatch.setenv(BOOKS_ENV, str(EXAMPLE_BOOKS_DIR))
    assert resolve_books(None).directory == EXAMPLE_BOOKS_DIR.resolve()


def test_the_flag_wins_over_the_variable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(BOOKS_ENV, str(tmp_path / "nowhere"))
    assert resolve_books(EXAMPLE_BOOKS_DIR).directory == EXAMPLE_BOOKS_DIR.resolve()


def test_the_example_books_load() -> None:
    books = load_books(EXAMPLE_BOOKS_DIR)
    assert books.name == "Example"
    assert books.site.host == "ledger.example"
    assert books.site.test_hosts == frozenset({"test-ledger.example"})
    assert books.chart_path == EXAMPLE_BOOKS_DIR.resolve() / "chart.coa"
    assert books.mercury_path.name == "mercury.toml"
    assert books.forecast_path.name == "forecast.toml"
    assert books.qbo_path.name == "qbo.toml"
    assert books.chart_doc == EXAMPLE_BOOKS_DIR.resolve() / "chart-of-accounts.md"
    assert set(books.record_buckets) == {Target.BOOKS, Target.TEST}
    assert books.bot.user != books.reader.user


def test_a_directory_without_books_toml_is_refused_by_path(tmp_path) -> None:
    with pytest.raises(BooksError, match=r"books\.toml"):
        load_books(tmp_path)


def test_a_missing_data_file_is_refused_by_name(tmp_path) -> None:
    books = _copy_example(tmp_path)
    (books / "qbo.toml").unlink()
    with pytest.raises(BooksError, match=r"qbo\.toml"):
        load_books(books)


def test_an_unknown_top_level_key_is_refused_by_name(tmp_path) -> None:
    books = _copy_example(tmp_path)
    _rewrite(books, 'name = "Example"', 'name = "Example"\nchart_dcos = "x.md"')
    with pytest.raises(BooksError, match="chart_dcos"):
        load_books(books)


def test_an_unknown_nested_key_is_refused_by_its_dotted_name(tmp_path) -> None:
    books = _copy_example(tmp_path)
    _rewrite(books, "[site]", '[site]\nhots = "typo.example"')
    with pytest.raises(BooksError, match=r"site\.hots"):
        load_books(books)


def test_a_missing_key_is_refused_by_name(tmp_path) -> None:
    books = _copy_example(tmp_path)
    _rewrite(books, 'project = "example-project"', "")
    with pytest.raises(BooksError, match=r"secrets\.project"):
        load_books(books)


def test_a_wrongly_typed_value_is_refused_by_name(tmp_path) -> None:
    books = _copy_example(tmp_path)
    _rewrite(books, 'test_hosts = ["test-ledger.example"]', 'test_hosts = "test-ledger.example"')
    with pytest.raises(BooksError, match=r"site\.test_hosts"):
        load_books(books)


def test_test_hosts_that_include_the_books_host_are_refused(tmp_path) -> None:
    # The allowlist exists so an alias of the ledger is refused. Naming the
    # ledger itself as a test host would make `--target test` reach it.
    books = _copy_example(tmp_path)
    _rewrite(books, 'test_hosts = ["test-ledger.example"]', 'test_hosts = ["test-ledger.example", "LEDGER.example."]')
    with pytest.raises(BooksError, match=r"ledger\.example"):
        load_books(books)


def test_a_bot_and_reader_sharing_a_user_are_refused(tmp_path) -> None:
    books = _copy_example(tmp_path)
    _rewrite(books, 'user = "reader@ledger.example"', 'user = "bot@ledger.example"')
    with pytest.raises(BooksError, match="different users"):
        load_books(books)


@pytest.mark.parametrize("role", ["Guest", "All", "Example Books Bot"])
def test_a_fence_probe_role_somebody_holds_is_refused(tmp_path, role) -> None:
    # Writing a held role as Company's bypass role would open the closed
    # quarter to whoever holds it.
    books = _copy_example(tmp_path)
    _rewrite(books, 'probe_role = "Example Books Fence Probe"', f'probe_role = "{role}"')
    with pytest.raises(BooksError, match="probe_role"):
        load_books(books)


def test_invalid_toml_is_a_books_error(tmp_path) -> None:
    books = _copy_example(tmp_path)
    (books / "books.toml").write_text("[site\n", encoding="utf-8")
    with pytest.raises(BooksError, match=r"books\.toml"):
        load_books(books)


def _commands() -> list[list[str]]:
    """Every runningtab command, as the words that invoke it, with each required option filled in."""
    required = {"target": ["--target", "test"], "env": ["--env", "sandbox"], "quarter": ["--quarter", "2026Q3"]}

    def walk(command: Any, words: list[str]) -> list[list[str]]:
        subcommands = getattr(command, "commands", None)
        if subcommands:
            return [found for name, sub in subcommands.items() for found in walk(sub, [*words, name])]
        options = [arg for param in command.params if param.required for arg in required[param.name]]
        return [[*words, *options]]

    return walk(typer.main.get_command(cli.app), [])


def test_every_command_is_covered_below() -> None:
    # A new command joins the refusal test automatically; this pins that the
    # walk actually found the tree rather than an empty one.
    assert len(_commands()) >= 21


@pytest.mark.parametrize("words", _commands(), ids=" ".join)
def test_every_command_refuses_without_books_naming_the_flag_and_the_variable(monkeypatch, words) -> None:
    monkeypatch.delenv(BOOKS_ENV, raising=False)

    def _touched(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reached for a credential before knowing whose books these are")

    for name in ("site_config", "admin_config", "ingest_config", "mercury_config"):
        monkeypatch.setattr(cli, name, _touched)
    result = CliRunner().invoke(cli.app, words)
    assert result.exit_code == 2, result.output
    flat = " ".join(result.output.split())
    assert "--books" in flat
    assert "RUNNINGTAB_BOOKS" in flat


def test_the_flag_names_the_books_for_a_command(monkeypatch) -> None:
    monkeypatch.delenv(BOOKS_ENV, raising=False)
    result = CliRunner().invoke(cli.app, ["--books", str(EXAMPLE_BOOKS_DIR), "chart", "render-doc", "--check"])
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output


def test_a_command_refuses_books_toml_with_an_unknown_key(monkeypatch, tmp_path) -> None:
    books = _copy_example(tmp_path)
    _rewrite(books, "[site]", '[site]\nhots = "typo.example"')
    result = CliRunner().invoke(cli.app, ["--books", str(books), "chart", "render-doc", "--check"])
    assert result.exit_code == 2
    assert "site.hots" in " ".join(result.output.split())


def test_a_name_that_cannot_be_a_directory_is_refused(tmp_path) -> None:
    # The name namespaces the ingest's local state, so it has to be usable
    # as one path segment.
    books = _copy_example(tmp_path)
    _rewrite(books, 'name = "Example"', 'name = "../elsewhere"')
    with pytest.raises(BooksError, match="name"):
        load_books(books)
