---
description: Find a symbol's definitions (and optionally uses) across the whole project by name.
argument-hint: "<name> [--uses]"
---

Find where the symbol `$ARGUMENTS` is defined — and, optionally, used — across
the whole project in one call, instead of grepping and re-reading source by hand.

Steps:

1. Parse `$ARGUMENTS` into the `name` to search for (a substring match on symbol
   names) and note whether usages were requested.

2. Call the MCP tool **`nimlang.symbols`** with `{name, root, kind, uses}`.
   `root` defaults to `.`; `kind` optionally filters by symbol kind. It returns
   `{defs:[{name, kind, file, line}], root}`, and when `uses: true` also
   `{uses:[{file, line}]}`. The scan is regex-based and toolchain-agnostic, so it
   works for **both** Nim and Nimony source.

   Example:
   - `/symbols parseHtmlColor` → every definition of `parseHtmlColor` in the
     project, with file and line.

3. Report the definitions (and usages, if asked) as `file:line kind name`. In
   terse mode (`NIMLANG_AGGRESSIVE=1`, `/aggressive on`, or `terse: true`) the
   result collapses to `defs:["file:line kind name"]` and `uses:["file:line"]`.

This replaces raw `grep` for "where is X defined / used across the repo" — one
structured tool call instead of a grep sweep plus manual `Read`s.
