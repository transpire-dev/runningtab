; Highlighting queries for `chart.coa`. Zed's capture-name set is fixed
; (docs: https://zed.dev/docs/extensions/languages) -- there is no
; `@markup.quote` for note lines, so notes use `@comment` alongside real
; comments: both are non-authoritative prose attached to the chart, and
; @comment's muted styling is the right visual weight for that. Account
; names are left uncaptured (plain text): they are the one piece of the
; line a reader actually needs to read at a glance, and every capture name
; Zed has (@string, @label, @property, ...) would visually mark them as
; something more structured than free-form text -- competing with the
; numbers and tags for attention instead of getting out of the way.

(comment) @comment

(note_line) @comment

(number) @number

(section_header
  "[" @punctuation.bracket
  name: (identifier) @title
  "]" @punctuation.bracket)

(setting
  key: (identifier) @property
  "=" @operator)

; Two separate patterns, not one: a tag_list with a single tag has no ","
; child at all, and an unanchored query pattern still requires every literal
; it names to actually exist -- folding "," into the bracket pattern would
; silently stop matching brackets on every single-tag account line.
(tag_list
  "[" @punctuation.bracket
  "]" @punctuation.bracket)

(tag_list "," @punctuation.delimiter)

(root_tag
  "root" @keyword
  ":" @punctuation.delimiter
  value: (identifier) @constant)

(type_tag) @type

; A reserved word of ours, not an ERPNext account type -- @keyword, the same
; weight `root` gets, so the two kinds of tag do not read alike.
(flag_tag) @keyword
