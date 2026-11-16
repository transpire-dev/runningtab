"""The documentation site and README against the CLI and PyPI.

Every `runningtab ...` invocation a page shows, in a code block or an inline
code span, must name a real command and only flags that command takes, so a
renamed command or a dropped flag fails here instead of in a reader's shell.
Flag lists, defaults and exit codes live in each command's --help, not on the
site, so there is no per-command reference to keep in step.
"""

import re
from pathlib import Path
from typing import Any

import typer

from runningtab.cli import app

_CODE_SPAN = re.compile(r"`([^`]+)`")
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")


# Typer vendors its own click, so commands and groups are told apart by shape:
# a group is a command with `.commands`.
def _cli() -> Any:
    return typer.main.get_command(app)


def _subcommands(command: Any) -> dict[str, Any] | None:
    return getattr(command, "commands", None)


def _options(command: Any) -> set[str]:
    return {opt for param in command.params for opt in (*param.opts, *param.secondary_opts) if opt.startswith("--")}


def test_readme_links_are_absolute() -> None:
    """PyPI renders README.md as the project page and resolves no relative link."""
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    relative = [target for target in re.findall(r"\]\(([^)\s]+)", readme) if not target.startswith("https://")]
    assert relative == []


DOCS = Path(__file__).parent.parent / "docs"


def command_lines(text: str) -> list[str]:
    """Every `runningtab ...` invocation a page shows: lines of code blocks and inline code spans."""
    found: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            stripped = line.strip()
            if stripped.startswith("runningtab"):
                found.append(stripped)
        else:
            found.extend(
                span for span in _CODE_SPAN.findall(line) if span.startswith("runningtab ") or span == "runningtab"
            )
    return found


def invocation_problems(line: str) -> list[str]:
    """What in one shown invocation the CLI would not accept: an unknown command or an option it does not take."""
    tokens = line.split()[1:]
    if tokens[:1] == ["--books"]:
        tokens = tokens[2:]
    command = _cli()
    path: list[str] = []
    while tokens and (children := _subcommands(command)) is not None and tokens[0] in children:
        command = children[tokens[0]]
        path.append(tokens.pop(0))
    if tokens[:1] and tokens[0].startswith("<"):
        # A placeholder such as `<command>` stands for any command, so there
        # is nothing to resolve the rest of the line against.
        return []
    if tokens and _subcommands(command) is not None and not tokens[0].startswith("-"):
        return [f"{line}: no command {tokens[0]!r} under {' '.join(['runningtab', *path])}"]
    allowed = _options(command) | {"--help"}
    return [
        f"{line}: {' '.join(path)} takes no {flag}" for flag in _FLAG.findall(" ".join(tokens)) if flag not in allowed
    ]


def test_finds_invocations_in_blocks_and_spans() -> None:
    page = """Run `runningtab coa plan --target test` first.

```sh
export RUNNINGTAB_BOOKS=my-books
runningtab --books my-books coa apply --target test --yes
```

`runningtab.mercury.client` is a module, not a command.
"""
    assert command_lines(page) == [
        "runningtab coa plan --target test",
        "runningtab --books my-books coa apply --target test --yes",
    ]


def test_invocation_problems_name_what_the_cli_rejects() -> None:
    assert invocation_problems("runningtab coa plan --target test") == []
    assert invocation_problems("runningtab --books DIR health --target books --strict") == []
    assert invocation_problems("runningtab <command> --help") == []
    assert invocation_problems("runningtab coa plann --target test") == [
        "runningtab coa plann --target test: no command 'plann' under runningtab coa"
    ]
    assert invocation_problems("runningtab access verify --target test --prune") == [
        "runningtab access verify --target test --prune: access verify takes no --prune"
    ]


_LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")


def broken_links(page: Path, docs: Path = DOCS) -> list[str]:
    """Relative link targets on a page that name no file."""
    problems: list[str] = []
    for target in _LINK_TARGET.findall(page.read_text()):
        if "://" in target or target.startswith(("#", "mailto:")):
            continue
        path = target.split("#", 1)[0]
        if not (page.parent / path).resolve().is_file():
            problems.append(f"{page.relative_to(docs)}: {target}")
    return problems


def test_broken_links_names_a_target_with_no_page(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    (tmp_path / "other.md").write_text("")
    page.write_text("[ok](other.md#part) [web](https://example.com) [bad](other.mdNone)")
    assert broken_links(page, tmp_path) == ["page.md: other.mdNone"]


def test_every_relative_link_on_every_page_names_a_page() -> None:
    assert [problem for page in sorted(DOCS.rglob("*.md")) for problem in broken_links(page)] == []


def test_every_invocation_on_every_page_is_one_the_cli_accepts() -> None:
    pages = sorted(DOCS.rglob("*.md"))
    problems = [
        f"{page.relative_to(DOCS)}: {problem}"
        for page in pages
        for line in command_lines(page.read_text())
        for problem in invocation_problems(line)
    ]
    assert problems == []
