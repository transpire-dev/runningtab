/**
 * Tree-sitter grammar for `chart.coa`, runningtab's chart-of-accounts outline
 * (see src/runningtab/chart.py, the authoritative parser).
 *
 * This is a highlighting-only grammar: the real file's indentation encodes
 * a tree (deeper indent nests under the nearest shallower account), but
 * `chart.py` derives that structure itself and a tree-sitter grammar has no
 * need to re-derive it just to color text. So every line is parsed on its
 * own, independent of the lines around it, and indentation is treated as
 * insignificant whitespace like any other. This keeps the grammar a plain
 * context-free grammar with no external scanner.
 */

module.exports = grammar({
  name: "coa",

  extras: ($) => [/[ \t]/],

  // `account_line`'s `optional($.tag_list)` is a textbook trailing-optional
  // ambiguity: after `number account_name`, seeing `[` could start
  // `tag_list` or (if the parser hadn't yet decided) end the line. There is
  // exactly one input that actually parses at runtime, so GLR resolves it
  // without help; this just tells tree-sitter to allow the ambiguity
  // instead of rejecting the grammar at generation time.
  conflicts: ($) => [[$.account_line]],

  rules: {
    source_file: ($) =>
      repeat(
        choice(
          $.comment,
          $.section_header,
          $.setting,
          $.account_line,
          $.note_line,
          /\r?\n/,
        ),
      ),

    comment: ($) => token(seq("#", /[^\n]*/)),

    // `[defaults]` is the only section header the real file has, but the
    // grammar doesn't special-case its name -- chart.py would reject any
    // other section anyway, and highlighting doesn't need to enforce that.
    section_header: ($) => seq("[", field("name", $.identifier), "]"),

    // `key = 1234`, only ever seen inside `[defaults]`. chart.py enforces
    // that placement; the grammar just recognizes the shape.
    setting: ($) =>
      seq(field("key", $.identifier), "=", field("value", $.number)),

    // A note is `>` to end of line, at any indentation. chart.py doesn't
    // care what precedes it on the line (`stripped.startswith(">")`), so
    // neither does this grammar -- leading indentation is `extras`.
    note_line: ($) => token(seq(">", /[^\n]*/)),

    // `<number> <name>[  <tags>]`. The real file right-aligns `[tags]` with
    // long runs of spaces, which `account_name`'s trailing-whitespace trim
    // (below) absorbs.
    account_line: ($) =>
      seq(
        field("number", $.number),
        field("name", $.account_name),
        optional($.tag_list),
      ),

    // Everything up to the first `[`, minus leading/trailing whitespace --
    // matches chart.py's `_split_name_and_tags`, which does the same rstrip
    // (and relies on `extras` to have already consumed the one space after
    // the account number). Both ends are anchored to a non-space character
    // so the padding around the name -- especially the long run of spaces
    // before a right-aligned `[tags]` block -- doesn't get swallowed into
    // (and highlighted as part of) the name.
    account_name: ($) => token(/[^\[\n \t]([^\[\n]*[^\[\n \t])?/),

    tag_list: ($) => seq("[", $.tag, repeat(seq(",", $.tag)), "]"),

    tag: ($) => choice($.root_tag, $.flag_tag, $.type_tag),

    // `root: <RootType>`, only legal on a level-0 (unindented) account.
    // chart.py enforces that; the grammar accepts it anywhere a tag can
    // appear, same tradeoff as `section_header` above.
    root_tag: ($) => seq("root", ":", field("value", $.identifier)),

    // A reserved word of ours rather than one of ERPNext's account types --
    // today only `no-bank-line`, which says the account is reached solely by
    // a period-end or adjustment journal, so `runningtab mercury categories`
    // offers no category for it. Listed literally, and before `type_tag`:
    // the hyphen already puts it outside `type_tag`'s character set, but an
    // account type is a name ERPNext chose and these are not, so a reader
    // (and the highlighter) should be able to tell them apart. Written as a
    // regex, like `type_tag`, so it lexes as one named leaf token; the
    // lexer's longest-match rule is what keeps `type_tag` from taking the
    // leading "no" and leaving "-bank-line" unparsed.
    flag_tag: ($) => /no-bank-line/,

    // An ERPNext account type: free words with internal spaces but no
    // commas or brackets ("Fixed Asset", "Accumulated Depreciation",
    // "Round Off"). Excludes ":" so it can't be confused with `root_tag`,
    // and "-" so it can't swallow a `flag_tag`.
    type_tag: ($) => /[A-Za-z][A-Za-z ]*[A-Za-z]|[A-Za-z]/,

    identifier: ($) => /[A-Za-z_][A-Za-z0-9_-]*/,

    number: ($) => /\d+/,
  },
});
