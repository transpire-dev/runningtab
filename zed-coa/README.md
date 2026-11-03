# zed-coa

Syntax highlighting for runningtab's `chart.coa` chart-of-accounts outline in
Zed. Zed highlights via a tree-sitter grammar, so this is two things:

- `grammar/` — a tree-sitter grammar, `tree-sitter-coa`, that recognizes the
  format `src/runningtab/chart.py` parses (read that file first if
  the grammar ever needs to change — it is the authoritative source of the
  format, this grammar just re-describes it for highlighting). Only
  `grammar.js`, `package.json`, `package-lock.json`, and the corpus tests are
  tracked; `node_modules/` (from `npm ci`) and the generated `src/` (parser.c
  and friends, from `tree-sitter generate`) are both produced at build time and
  are git-ignored.
- `languages/coa/` — the Zed language config, `highlights.scm`, and
  `outline.scm`.
- `extension.toml.in` — a template; `install-dev.sh` renders it into a real
  `extension.toml` at build time (again, see below for why).

Dev-installed only; nothing here is published anywhere.

## Installing as a Zed dev extension

```sh
./zed-coa/install-dev.sh
```

This prints a path, e.g. `/tmp/zed-coa-dev/extension`. In Zed: **Extensions**
(`cmd-shift-x` or the command palette) → **Install Dev Extension** → pick that
path. Then open `examples/books/chart.coa` — it should be recognized as "COA" (check the
language indicator in Zed's status bar) and highlighted per
`languages/coa/highlights.scm`.

Re-run the script (and reload the dev extension in Zed) after editing anything
under `grammar/` or `languages/` — Zed loaded a copy in a scratch directory, not
this checkout, so it won't see a source edit on its own. `install-dev.sh --help`
explains more; it's idempotent, safe to run anytime.

**Toolchain**, per <https://zed.dev/docs/extensions/languages> and
<https://zed.dev/docs/extensions/developing-extensions>: on top of what
`install-dev.sh` needs (bash, git), Zed compiles the grammar itself once you
install the dev extension, so the machine needs the Rust toolchain (`rustup`)
with the `wasm32-wasip2` target, and **wasi-sdk**. Zed loaded and highlighted
this extension on 2026-08-25 on a machine whose `rustup target list --installed`
showed only `aarch64-apple-darwin` and where no wasi-sdk install was found
beforehand, so Zed evidently fetches what it needs for the grammar build itself;
if an install fails on compile, those two are still the first things to check.

## Why a build script instead of a checked-in `extension.toml`

A Zed extension's `[grammars.X]` section only supports two fields — `repository`
and `rev` — confirmed against <https://zed.dev/docs/extensions/languages>:

> The `repository` field must specify a repository where the Tree-sitter grammar
> should be loaded from, and the `rev` field must contain a Git revision to use,
> such as the SHA of a Git commit.
>
> If you're developing an extension locally and want to load a grammar from the
> local filesystem, you can use a `file://` URL for `repository`.

There is no `path` field for pointing a grammar at a subdirectory of a larger
repository (checked specifically for this, since this repository would
otherwise be the obvious `repository` to point at). So `rev` can't name a commit
of this repository itself — it has to name a commit of a repository whose
root **is** the grammar.

The grammar can't carry its own committed `.git/` either: a git repository
nested inside this one is tracked as a broken "embedded repository" gitlink
(confirmed — `git add` on it warns exactly this and stages a bare commit
reference, not the actual files), so the grammar source would never actually get
committed to this repository. `grammar/` therefore stays plain files, and
`install-dev.sh` builds the throwaway repo Zed needs on demand, outside this
checkout: it copies `grammar/` into `${TMPDIR:-/tmp}/zed-coa-dev/`, turns that
copy into a fresh one-commit git repo, and renders `extension.toml.in`'s
`@GRAMMAR_REPO@`/`@GRAMMAR_REV@` placeholders with that copy's `file://` path
and commit SHA — both specific to the machine and moment the script ran, which
is why they're a template rather than a checked-in file.

## After a `grammar.js` change

`install-dev.sh` regenerates the parser in its build copy every run, so a
grammar edit needs nothing beyond re-running it and reloading the dev extension
in Zed. To run the corpus tests or inspect a parse directly, or after bumping
`tree-sitter-cli` in `package.json` (re-run `npm install --package-lock-only` in
`grammar/` first, and commit the updated `package-lock.json` -- that lockfile is
what pins every package `npm ci` installs, not just `tree-sitter-cli` itself, so
a bump is not real until it's committed), use
`just -f zed-coa/justfile check` (or `generate`/`test` alone), or run the
same steps directly against a scratch build (the generated `src/` inside the
checkout is git-ignored, so generating there is harmless too):

```sh
cd zed-coa/grammar
npm ci                                                    # once, or after package-lock.json changes
npx tree-sitter generate
npx tree-sitter test                                      # runs test/corpus/chart.txt
npx tree-sitter parse ../../examples/books/chart.coa
```

`npm ci` installs exactly what `package-lock.json` resolved, so
`npx tree-sitter` afterwards resolves the local
`grammar/node_modules/.bin/tree-sitter` it just installed -- no `--yes`, no
registry lookup at run time. If the default `~/.npm` cache on your machine has
root-owned files and `npm ci` fails with `EPERM`, point it elsewhere:
`npm_config_cache=/some/writable/dir npm ci`.

## What's actually verified, and what isn't

- **Verified**, via the `tree-sitter` CLI directly (not through Zed):
  `tree-sitter generate` succeeds; `tree-sitter test` passes all of
  `test/corpus/chart.txt`; `tree-sitter parse` on the real chart
  produces **zero ERROR nodes and zero MISSING nodes**; a deliberately-broken
  line (garbage text where an account/comment/ note/setting line is expected)
  produces exactly one `ERROR` node scoped to that line, with every other line —
  before and after it — still parsing as normal `account_line`/`note_line`
  nodes; `highlights.scm` and `outline.scm`'s queries were run against the real
  file with `tree-sitter query` and produce the expected captures (numbers,
  tags, brackets, `root:`/type tags, settings, section header, comma-separated
  multi-tag lists, outline items); `install-dev.sh` runs cleanly end to end and
  is idempotent (re-running rebuilds and prints a new commit SHA).
- **Verified in Zed** (2026-08-25): installed as a dev extension from the
  `install-dev.sh` output, opened the real chart, and saw numbers, `root:`
  keywords, type tags, brackets, and muted note lines all coloured as
  `highlights.scm` intends. The generated `parser.c` is ABI 14 (generated
  without a `tree-sitter.json`, which ABI 15 would need); Zed's runtime accepts
  it. If a future Zed drops ABI 14, run `tree-sitter init` interactively to
  create `tree-sitter.json`, then regenerate.
