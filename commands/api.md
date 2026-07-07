---
description: Fetch the typed public API of a module or third-party package without reading its source.
argument-hint: "<module> [needle]"
---

Fetch the typed public API of `$ARGUMENTS` — a stdlib module, a nimble package,
or a `.nim`/`.nif` path — in one call, instead of `Read`-ing the dependency's
source to figure out its signatures.

Steps:

1. Parse `$ARGUMENTS` into the target `module` and an optional `needle` (a
   substring to filter symbol names by). `module` can be a `.nim` path, a nimble
   package name (e.g. `chroma`), or a stdlib module (e.g. `std/tables`).

2. Call the MCP tool **`nimlang.api`** with `{module, toolchain, needle}`.
   Toolchain defaults to `auto`. For Nim it runs `nim jsondoc` under the hood and
   returns `{toolchain, module, source, api:[{name, kind, sig}]}` — the typed,
   public interface, no source reading required. `needle` narrows the result to
   symbols whose name contains that substring.

   Examples:
   - `/api chroma` → the public API of the `chroma` package.
   - `/api std/tables` → the stdlib `tables` module's procs/types.
   - `/api chroma parseHtmlColor` → only the matching symbols.

3. For Nimony (or a `.nif` path), the typed API *is* the compiled artifact: the
   tool renders the `.nif` via `nif_render`, or returns a note telling you to
   compile first and then use `nif_render`/`nif_outline` (see `/render`, `/nif`).

4. In terse mode (`NIMLANG_AGGRESSIVE=1`, `/aggressive on`, or `terse: true` per
   call) `api` collapses to a list of compact signature strings — reach for it
   when you only need the shapes, not the metadata.

Do NOT `Read` the library's source to learn its API — that is exactly the token
cost this tool avoids.
