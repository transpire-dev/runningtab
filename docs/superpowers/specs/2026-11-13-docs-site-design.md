# runningtab documentation site

## Intent

runningtab's only documentation is a 711-line README. It mixes flag reference,
design rationale, step-by-step procedures and contributor internals in one
stream, and parts of it are already stale. It was written by and for its author.

The site is for **someone keeping their own company's books** who has heard of
runningtab and is deciding whether to use it, then setting it up. Transpire's
books are shown as one worked example, never assumed.

The site's argument, in Gavin's words: automating bookkeeping early means making
fewer mistakes as the company grows, and understanding the economics of your
software choices and how the company works, early, is how a company knows
whether it is going to work.

## Goals

- A Zensical site built from `docs/`, published to GitHub Pages at
  `https://transpire-dev.github.io/runningtab/` on every push to main.
- Content restructured for the adopter (Diátaxis: tutorial, how-to, reference,
  explanation), covering everything the README covers today.
- Every factual claim checked against the code as it stands, since the README
  has drifted (see Known stale facts).
- The README shrinks to what runningtab is, installation, and a link to the
  site. It stays the PyPI long description.

## Non-goals

- Python API reference (mkdocstrings). runningtab is a CLI; its library surface
  is not a product.
- Making the secret backend pluggable. The docs say Google Secret Manager is the
  current credential backend and may become pluggable; nothing more.
- Publishing Transpire's real chart, hosts, buckets or account ids.
- A custom domain.

## Anti-goals

- A site that is the README cut into pages: same density, same author-only
  voice. That is what this work replaces.
- Docs that state anything the code does not do. A wrong doc in a bookkeeping
  tool sends someone to the wrong account.
- Marketing voice. Plain, direct sentences.

## Approaches considered

**Scope.** (a) Build first with README content moved as-is, rewrite later;
(b) build and rewrite together. Gavin chose (b): "no point in a site with
nothing in it."

**Publishing.** (a) A separate `.github/workflows/docs.yml`, adapted from the
one `zensical new` generates; (b) a `docs` job inside `ci.yml`, deploy gated on
`check`/`test` like `publish`. Chosen: (a), Bot's call, stated to Gavin and not
objected to. It is what Zensical expects, keeps Pages permissions away from the
PyPI trusted-publishing job, and a docs fix does not wait on the test suite.

**The Transpire example.** (a) A story about how Transpire runs its books,
with the files being `examples/books/`; (b) the story plus Transpire's real
chart; (c) replace `examples/books/chart.coa` with the real chart. Gavin chose
(a): "story based on the example."

## Build and publishing

Modelled on `~/Programing/transpire/zensical.toml` and its `docs-*` just
recipes, trimmed.

- `zensical.toml` at the repo root: `docs_dir = "docs"`, `site_dir = "site"`,
  `repo_url = "https://github.com/transpire-dev/runningtab"`,
  `edit_uri = "edit/main/docs/"`, GitHub repo icon, an explicit `nav` matching
  the outline below. Theme features and the markdown extension set follow
  transpire's (copy, instant navigation, tabs, admonitions, `toc.follow`,
  `navigation.indexes`, etc.). Palette is the builder's call.
- Dropped from transpire: the macros plugin, mkdocstrings, the
  `overrides/` build-time footer marker, `docs-gen`, Mermaid (add it back only
  if a page actually needs a diagram).
- `zensical` in a new `docs` dependency group (`>=0.0.52`, the version transpire
  runs), so `just check`/`just test` do not install it.
- just recipes: `docs-build` (`uv run --group docs zensical build --clean
  --strict`) and `docs-serve`. `site/` is git-ignored.
- `.github/workflows/docs.yml`: on pull requests, build only (a broken link
  fails the PR); on push to main, build and deploy with
  `actions/configure-pages`, `actions/upload-pages-artifact`,
  `actions/deploy-pages`. Every action pinned by SHA with a version comment,
  `persist-credentials: false` on checkout, uv via `astral-sh/setup-uv`, as
  `ci.yml` does. Permissions `contents: read` at top level; `pages: write` and
  `id-token: write` only on the deploy job, under the `github-pages`
  environment.
- One manual step: the repo's Pages source must be set to "GitHub Actions"
  (Settings → Pages, or `gh api`). The builder states it to Gavin; it is
  outward-facing, so Gavin approves it before it is done.

`docs/superpowers/` (design specs and plans) lives under `docs/` and builds, but
stays out of the nav.

## Site outline

- **Home** — what runningtab is and why start early. Opens with Gavin's
  argument above. Then what it automates to get you there (bank activity into
  the ledger with draft vouchers, a chart kept as a file, the capital-call
  forecast, quarter closes an outside accountant can read cold, `health`
  noticing silent failures). Then what you need: Python 3.14, an ERPNext site,
  Mercury, Google Secret Manager and Cloud Storage. Say plainly that `forecast`
  computes a members' capital call, which fits a member-funded company like an
  LLC.
- **Explanation**
  - How it works — the books directory in git; the three systems (ERPNext
    ledger, Mercury bank read-only except categories, record bucket); machines
    draft, humans post; `--target test` versus `books`.
  - Why write down the chart of accounts — it is the company's vocabulary for
    where money goes and the decision everything downstream rests on. As a
    versioned file it is stable documentation: reviewed in a diff, explained by
    the annotated chart doc, and reused across systems — ERPNext accounts,
    Mercury GL codes and categories, the QuickBooks import kit for an
    accountant. Change ledgers and the chart comes with you.
  - The access model — the bot's roles, the single submit on Bank Transaction
    and why it is safe, the proofs `access provision` runs.
  - How the ingest decides — Bank Transactions, a row's GL code, draft vouchers
    beside coded rows, transfers, payee history, adopting hand-imported rows,
    the sweep, receipts, the state file.
  - What a close guarantees — the ceremony, the lock, byte-identical rebuilds,
    the record bucket's retention, why `status` keeps re-checking.
- **Tutorial** — your chart in a test ledger: copy `examples/books/`, point it
  at a disposable ERPNext site with `--target test`, `coa plan`, `coa apply`,
  see the accounts. Needs only an ERPNext site: no Mercury, no GCP. (The
  `test` target can take `RUNNINGTAB_TEST_URL`/`_KEY`/`_SECRET` from the
  environment; the builder confirms nothing else in that path reaches Secret
  Manager.)
- **How-to guides** — bring up a new site in order; connect Mercury; daily
  reconciliation; close a quarter; reopen and re-close; late bank activity in
  a closed quarter; run the capital forecast; rotate the bot's key; run
  `health` on a schedule.
- **Reference** — one page per command group (`coa`, `chart`, `health`,
  `period`, `mercury`, `forecast`, `access`) with flags and exit codes; the
  books directory files (`books.toml` keys, `chart.coa` syntax, `mercury.toml`,
  `forecast.toml`, `qbo.toml`); targets, credentials and environment variables.
- **Example: how Transpire keeps its books** — a story using `examples/books/`
  as the files. Facts about Transpire's practice (who reconciles and how often,
  how a close goes, what runs on a schedule, why the chart is shaped as it is)
  come from Gavin; the builder collects them as one batch of questions rather
  than inventing them.
- **Contributing** — the module tour, the live tests (`just test-live`,
  `RUNNINGTAB_LIVE`), capturing fixtures, the Zed grammar (`zed-coa/`).

## Writing process

Prose is drafted by Astra (`codex exec --model gpt-6-astra -s read-only
--ephemeral`), which writes well but is a poor engineering agent. For each
page, the builder writes a brief to the scratchpad: the reader, the page's job,
the style rules below, and the source text inline (README sections and the
relevant code or `--help` output). Astra marks anything the source does not
support as `[GAVIN: …]`. The builder then checks every claim against the code,
fixes or removes what does not hold, and edits the page into `docs/`. The
builder does all config, build and CI work.

Style: plain direct sentences; no contrastive negation ("not X, but Y");
none of "load-bearing", "footgun", "blast radius", "wedge"; no emoji; no
marketing words.

Gavin's draft-page reaction is the check on voice. The first page (Home) goes to
him before the rest are drafted.

## Known stale facts in the README

- `health`: commit ce43d18 made uncoded spend and event cursor age run. The
  README still lists both as "not available", says `N/A` "is only ever the
  three sections", and the module tour says `health.py` has "five checks" while
  the section says six.

The builder checks every other claim the same way as it moves.

## Calls made without Gavin (check these)

- Links from pages to repo files outside `docs/` (`examples/books/`,
  `zed-coa/`) are absolute GitHub URLs on `main`, so `--strict` builds pass.
- The Example page is not published with `[GAVIN: …]` gaps. It merges once
  Gavin has answered the batch of Transpire questions; until then it stays out
  of the nav.

## Left to the builder

- Palette, page file names, splitting or merging pages within the outline.
- Whether `docs-build` runs in `just check` (it needs the `docs` group; keeping
  it out of `check` and in `docs.yml` only is fine).
- How the tutorial gets a disposable ERPNext site (a pointer to Frappe's own
  install docs is fine; runningtab does not provision one).
