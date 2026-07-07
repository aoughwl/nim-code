---
name: repo-map
description: >-
  Read this when navigating a Nim or Nimony codebase across a session and you
  want to stop re-outlining the same files: how to keep a lazy, incremental
  project map in Claude Code's file-memory, locate symbols with the `symbols`
  tool before grepping, pull dependency signatures with the `api` tool instead
  of reading library source, and persist non-obvious toolchain facts so they are
  never re-derived.
---

# Lazy, incremental repo map

Do NOT scan or outline the whole codebase up front — it is large and most of it
is irrelevant to any one task. Instead build the map *as you go* and keep it in
memory so the next session starts warm.

## Keep a `project-map` memory

Maintain a `project-map` file-memory: one line per file you have actually
touched, in the shape

    path: key symbols; what it does

Append or update a line whenever you outline or edit a file. Never do a big
upfront pass — the map grows lazily, only covering ground you have already
walked. Before re-`outline`-ing a file, check the map first.

## Locate symbols with `symbols`, not grep

To find where a symbol is defined or used project-wide, prefer the MCP
**`symbols`** tool (`mcp__nimlang__symbols`) over `grep` + `Read`. It returns
structured `{defs, uses}` in one call and works for **both** Nim and Nimony.
Fall back to grep/Read only when a substring search genuinely can't express what
you need.

## Get dependency APIs with `api`, not source reads

For a third-party package or stdlib module's signatures, prefer the MCP
**`api`** tool (`mcp__nimlang__api`) over `Read`-ing the dependency's source. One
call returns the typed public interface (`nim jsondoc` for Nim; the rendered
`.nif` artifact for Nimony). Filter with `needle` and use terse mode when you
only need the shapes.

## Persist non-obvious toolchain facts

When you derive a fact that cost effort and won't change — e.g. `nimony c` can
exit 0 on failure (trust the tool's `ok` field, not the exit code), or which of
`nimony` / `nimsem` / `hastur` does what — write it to a memory so it is never
re-derived next session. Facts that are cheap to re-check don't belong here;
non-obvious, stable ones do.

## Complements `token-thrift`

This skill is about *not re-discovering structure*; the `token-thrift` skill is
about *not flooding context* when you do compile/inspect work. Use them together:
`repo-map` keeps you from re-outlining, `token-thrift` keeps each outline/compile
call cheap.
