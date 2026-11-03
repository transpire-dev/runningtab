; Zed's outline panel: one entry per account line, since the grammar has no
; nesting to hang a hierarchical outline on (see grammar.js's top comment).
; `number` is shown as context so the panel reads "1111  Primary Checking
; Account" rather than just the name.
; Capture names verified against https://zed.dev/docs/extensions/languages.

(account_line
  number: (number) @context
  name: (account_name) @name) @item
