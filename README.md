# nim-code

**A Claude Code plugin that makes agent work on both Nim and Nimony codebases dramatically more token-efficient.**

Nim and [Nimony](https://github.com/nim-lang/nimony) (the new NIF-based Nim reimplementation) generate a lot of text that is expensive to feed through an agent verbatim: enormous `.nif` S-expression artifacts, noisy compiler chatter, and giant test diffs. `nim-code` puts a thin, structured layer between Claude and both toolchains so the agent sees compact diagnostics and targeted slices of NIF instead of megabytes of raw output.

---

## Why

On a bare Claude Code session, these codebases bleed tokens in predictable places:

- **Huge `.nif` S-expr artifacts.** A single lowered NIF file in `nimcache/` is routinely 160 KB / ~5,500 lines of parenthesized S-expression. `Read`-ing one to answer a small question burns tens of thousands of tokens.
- **Verbose, noisy compiler output.** `nimony c` / `hastur` wrap real diagnostics in `nifmake:`, `FAILURE:`, and `niflink` chatter. The signal (the actual `Error:` lines) is a few lines buried in screens of build logging.
- **`nimony c` returns exit 0 on failure.** The agent cannot trust the exit code — a build that errored still looks "successful," so it re-reads output (more tokens) or, worse, proceeds on a broken assumption.
- **Giant NIF test diffs.** Nimony's tests embed produced NIF, so `hastur --overwrite` diffs are thousands of lines of S-expr. Reviewing them as raw `git diff` is enormous.
- **A 123k-line codebase.** Grepping and re-reading source to locate a symbol's definition and uses, by hand, adds up fast.
- **Re-learning the tag vocabulary every session.** `doc/tags.md` (the NIF tag reference) gets re-read from scratch each time the agent touches NIF.
- **Conflating Nim and Nimony.** They share syntax but not feature sets or toolchains (`nim` vs `nimony`, `nimble` vs `hastur`). An agent that assumes "Nim 2" semantics on a Nimony file wastes turns on wrong answers.

`nim-code` addresses each of these directly: NIF is only ever read through targeted tools, build output is trimmed to diagnostics, failure is detected by parsing (not exit code), diffs are collapsed structurally, symbol lookup is one tool call, and the tag vocabulary plus the Nim-vs-Nimony distinction ship as skills.

---

## Architecture

```
nim-code/                      ${CLAUDE_PLUGIN_ROOT}
├── .claude-plugin/
│   └── plugin.json               manifest (name, version 0.1.0, author)
├── .mcp.json                     registers the `nimlang` MCP server
├── mcp/
│   ├── server.py                 zero-dependency MCP server (stdlib Python 3.7)
│   ├── test_server.py            starts server, checks a bad Nim + bad Nimony file
│   └── README.md                 manual nimsuggest / idetools fallback notes
├── hooks/
│   ├── hooks.json                wires the hooks below
│   ├── guard-nif-read.py         PreToolUse(Read): transform big .nif reads into an outline (v0.2)
│   ├── guard-nif-bash.py         PreToolUse(Bash): block cat/head of big .nif (v0.2)
│   └── trim-build-output.py      PostToolUse(Bash): strip build noise
├── commands/
│   ├── check.md                  /check [file]
│   ├── nif.md                    /nif <file.nif> [needle]
│   ├── phase-diff.md             /phase-diff <file.nim>
│   ├── nimony-bug.md             /nimony-bug [file]
│   ├── explain-failure.md        /explain-failure [file]           (v0.2)
│   ├── shrink.md                 /shrink [file]                    (v0.2)
│   ├── render.md                 /render <file.nif> [needle]       (v0.2)
│   └── aggressive.md             /aggressive [on|off]              (v0.2)
├── skills/
│   ├── nif-format/SKILL.md       condensed NIF tag vocab + phase pipeline
│   ├── nim-vs-nimony/SKILL.md    feature/toolchain differences
│   ├── debug-loop/SKILL.md       the AGENTS.md compiler debug workflow
│   └── token-thrift/SKILL.md     prefer recipe tools + terse mode (v0.2)
└── agents/
    ├── nif-inspector.md          subagent for heavy NIF reading
    └── nim-fixer.md              cheap-model fix-loop subagent, returns only a diff (v0.2)
```

**The `nimlang` MCP server** (`mcp/server.py`) is the core. It speaks JSON-RPC 2.0 over stdio, shells out to the right compiler/tools, and returns a single compact structured block per call — never a whole NIF file. It auto-detects the toolchain by walking up from the target file (a `nimony.paths` / `nimony.cfg` / nimony-flavored `nim.cfg` means Nimony, otherwise Nim), overridable via the `toolchain` argument or `NIMLANG_TOOLCHAIN`.

**3 hooks** keep raw noise out of the context window automatically:
- `guard-nif-read.py` (PreToolUse on `Read`) denies reading a `.nif` file over 15 KB — and, as of v0.2, transforms the denial into a compact outline in the same turn (see [Aggressive mode](#aggressive-mode-v02)).
- `guard-nif-bash.py` (PreToolUse on `Bash`, **v0.2**) blocks `cat`/`head`/`tail` etc. of a big `.nif`, closing the shell-level loophole around the Read guard.
- `trim-build-output.py` (PostToolUse on `Bash`) strips `nifmake:` / `FAILURE:` / `niflink` lines from `nimony`/`hastur`/`nim c`/`nimble` output and surfaces just the diagnostics.

**10 slash commands**: `/check`, `/nif`, `/phase-diff`, `/nimony-bug`, the v0.2 additions `/explain-failure`, `/shrink`, `/render`, `/aggressive`, and the navigation commands `/api`, `/symbols` (see below).

**5 skills**: `nif-format`, `nim-vs-nimony`, `debug-loop`, the v0.2 `token-thrift`, and `repo-map` — loaded on demand so the agent doesn't re-read `doc/tags.md`, re-derive toolchain differences, re-outline files, or forget the cheap path each session.

**2 subagents**: `nif-inspector` — does heavy NIF / phase-artifact reading in its own context and returns only the conclusion, keeping large S-expr out of the main thread (handles both Nim source and Nimony NIF); and the v0.2 `nim-fixer` — runs the fix loop on a cheap model in its own context and returns only the final diff.

---

## MCP tools

All twelve are exposed by the `nimlang` server. `compile`, `outline`, `defs_uses`, `explain_failure`, `phase_report`, `shrink`, `api`, and `symbols` work for **both** toolchains; the `nif_*` tools (including `nif_render`) operate on Nimony's NIF artifacts and are **Nimony-only** (`api` on a Nimony/`.nif` target renders the NIF equivalent). The last four tools are new in v0.2 — see [Aggressive mode](#aggressive-mode-v02).

| Tool | What it does | Toolchain(s) |
|------|--------------|--------------|
| `compile(file, toolchain="auto", extra_args=[])` | Runs the correct checker (`nim check` or `nimony c`), parses diagnostics, and reports a **correct** ok/fail by treating any `Error:` line as failure (not the unreliable exit code). Returns `{ok, toolchain, stage, diagnostics:[…]}`. | Nim **and** Nimony |
| `outline(file, toolchain="auto")` | Lists top-level symbols `{name, kind, line, col}`. Nim uses `nimsuggest outline`; Nimony (and the Nim fallback) uses a regex scan of the `.nim` source. | Nim **and** Nimony |
| `nif_outline(nif_file)` | Top-level `(tag name …)` nodes of a NIF artifact — names only, no bodies. Lets the agent map a 5,500-line file in a few hundred tokens. | Nimony only |
| `nif_query(nif_file, needle)` | Returns only the S-expr subtrees whose head tag or symbol matches `needle`, each snippet truncated to ~40 lines, via a paren-matching scanner. | Nimony only |
| `nif_diff(file_a, file_b)` | Compact structural/line diff between two NIF files (unified diff, context=1, unchanged regions collapsed). Turns a thousand-line raw diff into the parts that actually changed. | Nimony only |
| `defs_uses(file, line, col, toolchain="auto")` | Definition + all uses of the symbol at a position: `{def, uses:[…]}`. Nim via `nimsuggest def`/`use`; Nimony via `nimsem idetools --track` against the `.s.nif` in `nimcache` (best-effort, degrades gracefully). | Nim **and** Nimony |
| `explain_failure(file, toolchain="auto", terse=…)` **(v0.2)** | **Recipe tool.** Compiles and, on failure, returns a ≤5-line `verdict` plus a `culprit`: Nimony extracts the smallest NIF node spanning the error position from the phase artifact; Nim returns ±3 source lines around the first error. One call replaces the manual compile → list → outline → query sequence. | Nim **and** Nimony |
| `phase_report(file, toolchain="auto", terse=…)` **(v0.2)** | **Recipe tool.** Compiles, then for each `nimcache/*.<phase>.nif` (p, s, …) gives a 1-line summary (top tag counts + size) with **no raw NIF**. Nim returns `{ok, phases:[], note:"Nim C backend has no NIF phases"}`. | Nim **and** Nimony |
| `nif_render(nif_file, needle=None, terse=…)` **(v0.2)** | Renders matching NIF node(s) as compact **pseudo-Nim** (maps `proc`/`var`/`let`/`const`/`call`/`if`/`asgn`/`ret`/`type`/… to Nim-ish syntax, demangles `sym.NN.mod` → `sym`), falling back to a raw snippet for unknown tags. ~10× smaller than raw NIF. | Nimony only |
| `shrink(file, toolchain="auto")` **(v0.2)** | Delta-debugs a failing file: iteratively drops top-level statements/lines while the **first** `Error:` message is preserved, returning `{original_lines, minimal_lines, minimal_source, kept_error}` — the minimal still-failing repro. Iteration/time bounded. | Nim **and** Nimony |
| `api(module, toolchain="auto", needle=None, terse=…)` | Returns the **typed public API** of a module or third-party package **without reading its source**. Nim runs `nim jsondoc` on a `.nim` path, a nimble package name (e.g. `chroma`), or a stdlib module (e.g. `std/tables`) and returns `{toolchain, module, source, api:[{name, kind, sig}]}`. `needle` filters by name substring; terse collapses `api` to compact signature strings. | Nim (via `nim jsondoc`) |
| `api(module, …)` on a `.nif` / Nimony target | The typed API **is** the compiled artifact: renders the `.nif` via `nif_render`, or returns a note to compile first and then use `nif_render`/`nif_outline`. | Nimony (`.nif`) |
| `symbols(name, root=".", kind=None, uses=false, terse=…)` | Project-wide symbol search by **name substring**, regex-based and toolchain-agnostic — replaces raw `grep` for "where is X defined/used". Returns `{defs:[{name,kind,file,line}], root}`, plus `{uses:[{file,line}]}` when `uses:true`. Terse collapses to `defs:["file:line kind name"]`, `uses:["file:line"]`. | Nim **and** Nimony |

---

## Aggressive mode (v0.2)

v0.2 adds an aggressive token-saving layer on top of everything above: a **terse** output mode on every tool, four new tools (two of them "recipe" tools that collapse whole workflows into one call), a Bash guard, a transform-not-block upgrade to the Read guard, four commands, a cheap-model subagent, and a behavior-shaping skill. All of it works for **both** Nim and Nimony (except `nif_render`, which is Nimony-only).

### Terse mode

Every tool — old and new — accepts an optional `terse: bool`. It defaults to the truthiness of the `NIMLANG_AGGRESSIVE` environment variable, so exporting `NIMLANG_AGGRESSIVE=1` (or running `/aggressive on`) makes every call terse by default; you can still force it per call with `terse: true`.

When terse, output collapses to the smallest useful shape and warnings/hints are dropped:

- `compile` → drops `Warning`/`Hint`; diagnostics become bare `"file:line:col msg"` strings; `ok` is kept.
- `outline` → `["name:line", …]`.
- `defs_uses` → `{def:"file:line"|null, uses:["file:line", …]}`.
- `nif_query` / `nif_outline` / `nif_render` → tighter caps (~15 lines per snippet) and null fields omitted.

Non-terse output shapes are unchanged, so terse mode is fully back-compatible — it is purely opt-in.

### Recipe tools: orchestrate server-side, return only the answer

`explain_failure` and `phase_report` are **recipe** tools. Instead of the agent making a compile call, reading the diagnostics, calling `outline`, then `nif_query`-ing the culprit — each round-trip spending tokens on intermediate output — the server runs that whole orchestration itself and returns only the final answer. This is the same idea as running code-execution over MCP: keep the multi-step glue on the server and hand the model just the conclusion, not the byproducts of each step.

- `explain_failure` collapses compile → list diagnostics → outline → query into one call, returning a ≤5-line verdict and the smallest spanning `culprit`.
- `phase_report` compiles and summarizes every phase artifact in one call, one line each, without ever surfacing raw NIF.

### Hooks: transform, don't just block

- **`guard-nif-read.py` upgraded to transform-not-block.** A `Read` of a `.nif` file over 15 KB is still denied — but the hook now runs `nif_outline` on it and embeds the compact outline in `permissionDecisionReason`, so the agent gets the *useful* version in the **same turn** instead of just a "don't do that" message. If anything goes wrong it falls back to the plain deny message (it never crashes the tool).
- **`guard-nif-bash.py` (new, PreToolUse on `Bash`).** Blocks `cat`/`head`/`tail`/`less`/`more`/`bat` targeting a `.nif` path over 15 KB — the Bash-level escape hatch around the Read guard — and steers to `nif_outline` / `nif_query` / `nif_render` / `/nif`. No-op for every other command.

### Commands

- `/explain-failure [file]` → MCP `explain_failure`; the one-call "why did this fail" recipe.
- `/shrink [file]` → MCP `shrink`; shows the minimal still-failing repro.
- `/render <file.nif> [needle]` → MCP `nif_render`; pseudo-Nim view of a NIF node.
- `/aggressive [on|off]` → explains enabling terse mode (`NIMLANG_AGGRESSIVE=1` or per-call `terse:true`) and the trade-offs.

### Subagent: `nim-fixer`

`agents/nim-fixer.md` auto-delegates on "fix/iterate on a failing Nim/Nimony compile." It runs the whole compile → shrink → explain → edit → recompile loop **in its own context on a cheap model (`haiku`)** and returns **only the final diff plus a verdict** — keeping all the verbose intermediate compiler output out of the main thread.

### Skill: `token-thrift`

`skills/token-thrift/SKILL.md` shapes the agent's behavior toward the cheap path: prefer the recipe tools (`explain_failure` / `phase_report`) over hand-rolled multi-call sequences, enable terse mode, never `cat` a `.nif`, and offload verbose fix loops to the `nim-fixer` subagent.

### Beyond the MCP server

The token savings don't all live in the MCP tools — v0.2 leans on the other levers a Claude Code plugin exposes:

- **Transform-not-block hooks** — a denied big-`.nif` `Read` comes back with the compact outline already attached, so blocking a wasteful action also *supplies the cheap alternative* in the same turn.
- **Bash-level guards** — `guard-nif-bash.py` closes the `cat`/`head` loophole so raw NIF can't sneak into context through the shell.
- **Cheap-model subagents** — `nim-fixer` burns the verbose fix-loop tokens in an isolated `haiku` context and returns only a diff, so the expensive main thread never sees them.
- **Behavior-shaping skills** — `token-thrift` nudges the agent to reach for the recipe tools and terse mode by default instead of the naive multi-call path.

### Third-party APIs & project navigation

Two more tools attack the *other* big token sink — re-reading dependency source and grepping the 123k-line tree to place a symbol:

- **`api`** returns a dependency's **typed public interface** in one call, so the agent never `Read`s a library's source to learn its signatures. For Nim it runs `nim jsondoc` on a `.nim` path, a nimble package (`chroma`), or a stdlib module (`std/tables`) and hands back `{name, kind, sig}` triples; for a Nimony/`.nif` target the typed API *is* the compiled artifact, so it renders the NIF equivalent via `nif_render` (or tells you to compile first). Filter with `needle`, and terse mode collapses it to bare signature strings. Exposed as `/api <module> [needle]`.
- **`symbols`** replaces raw `grep` for project-wide navigation: a name-substring search that returns structured `{defs, uses}` (file + line, optionally usages) in a single call. It is regex-based and toolchain-agnostic, so the same query works across Nim and Nimony source. Exposed as `/symbols <name>`.
- **`repo-map` skill + lazy-incremental memory.** `skills/repo-map/SKILL.md` teaches the agent to maintain a `project-map` file-memory — one line per touched file (`path: key symbols; what it does`), grown lazily as files are outlined or edited, never via a big upfront scan — plus to reach for `symbols`/`api` before grep/Read and to persist non-obvious toolchain facts (e.g. `nimony c` exiting 0 on failure) as memories so they are never re-derived. It complements `token-thrift`: `repo-map` avoids re-discovering structure, `token-thrift` keeps each call cheap.

---

## Works for both Nim and Nimony

The point of the plugin is that the *same* commands work across both toolchains, and both return the same compact diagnostic shape instead of raw compiler output.

### Nim — `/check` on a bad Nim file

`greeter.nim`:

```nim
proc greet(name: string) =
  echo "hi ", nam   # typo: `nam`
```

`/check greeter.nim` detects Nim, runs `nim check --hints:off --colors:off`, and returns:

```json
{
  "ok": false,
  "toolchain": "nim",
  "stage": "check",
  "diagnostics": [
    { "file": "greeter.nim", "line": 2, "col": 18,
      "severity": "Error", "message": "undeclared identifier: 'nam'" }
  ]
}
```

### Nimony — `/check` on a bad Nimony file

`hello.nim` (a Nimony project — a `nimony.cfg`/`nimony.paths` upstream selects the Nimony toolchain automatically):

```nim
import std/syncio

echo "hello, world
```

`/check hello.nim` detects Nimony, runs `nimony c`, strips the `nifmake:` / `FAILURE:` / `niflink` chatter, and — critically — reports failure even though `nimony c` exited 0, because an `Error:` line was parsed:

```json
{
  "ok": false,
  "toolchain": "nimony",
  "stage": "check",
  "diagnostics": [
    { "file": "hello.nim", "line": 3, "col": 6,
      "severity": "Error", "message": "closing \" expected" }
  ]
}
```

In both cases the agent receives a handful of structured fields instead of a screen of compiler output, and can trust the `ok` field.

---

## Install

`nim-code` is a local plugin — you point Claude Code at this directory.

**Quickest (per-session, for trying it out):**

```bash
claude --plugin-dir /home/savant/nimony-code
```

**Persistent (via a local marketplace):**

```text
/plugin marketplace add /home/savant/nimony-code
/plugin install nim-code
```

Then enable it from the plugin manager (`/plugin`) if it isn't already. After edits to the plugin, run `/reload-plugins` to pick up changes without restarting.

Plugin skills are namespaced, so the slash commands appear as `/nim-code:check`, `/nim-code:nif`, etc.; `/help` lists them under the plugin. The `nimlang` MCP server and all hooks activate automatically once the plugin is enabled.

---

## Requirements

- **python3** (3.7+, standard library only — the MCP server and hooks have no third-party dependencies).
- **Nim** — the `nim` and `nimsuggest` binaries, e.g. from `~/Nim/bin`. Needed for the Nim side of `compile`, `outline`, and `defs_uses`.
- **Nimony toolchain** — `nimony`, `nimsem`, and `hastur`, e.g. from `~/nimony/bin` (built with `nim c -r src/hastur build all`). Needed for the Nimony side of every tool and for all `nif_*` tools.

The server resolves binaries from `PATH` first, falling back to `~/Nim/bin` and `~/nimony/bin`. Override the locations with the `NIM_BIN_DIR` and `NIMONY_BIN_DIR` environment variables.
