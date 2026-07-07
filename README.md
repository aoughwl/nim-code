# nim-code

A Claude Code plugin that mediates agent access to the **Nim** and **Nimony**
toolchains through structured tools, so an agent works from compact diagnostics,
outlines, and targeted NIF slices instead of raw compiler output and multi‑hundred‑kilobyte
S‑expression artifacts.

The plugin supports both toolchains from a single interface: the same commands
and tools operate on Nim (`nim`, `nimsuggest`, `nimble`) and on
[Nimony](https://github.com/nim-lang/nimony), the NIF‑based Nim reimplementation
(`nimony`, `nimsem`, `hastur`, and the `nimcache/*.nif` artifacts its pipeline
emits). Toolchain selection is automatic and overridable.

## Contents

- [Motivation](#motivation)
- [Installation](#installation)
- [Configuration](#configuration)
- [Toolchain detection](#toolchain-detection)
- [Components](#components)
- [MCP tool reference](#mcp-tool-reference)
- [Terse mode](#terse-mode)
- [Hooks](#hooks)
- [Commands](#commands)
- [Skills and subagents](#skills-and-subagents)
- [Examples](#examples)
- [Requirements](#requirements)
- [Design notes](#design-notes)
- [Changelog](#changelog)

## Motivation

Both toolchains produce output that is costly to pass through an agent verbatim.
The plugin targets six recurring sources of token waste:

| Source | Cost | Mitigation |
|--------|------|------------|
| NIF artifacts in `nimcache/` | A single lowered `.nif` is commonly 160 KB–700 KB of parenthesized S‑expression. | NIF is read only through `nif_outline`/`nif_query`/`nif_diff`/`nif_render`; direct reads are intercepted by hooks. |
| Noisy compiler output | `nimony c` / `hastur` interleave `nifmake:`, `FAILURE:`, and `niflink` lines with real diagnostics. | `compile` parses diagnostics; a `PostToolUse` hook strips the noise from ad‑hoc build commands. |
| `nimony c` exits 0 on failure | The exit code is unreliable, so failure is easy to miss. | Failure is determined by parsing for an `Error:` diagnostic, not by exit status. |
| Large NIF test diffs | `hastur --overwrite` diffs embed produced NIF and run to thousands of lines. | `nif_diff` collapses unchanged regions to a structural diff. |
| Symbol lookup across a large tree | Locating a definition and its uses by grep is repetitive and unbounded. | `symbols` (name search) and `defs_uses` (position‑based) return structured results in one call. |
| Repeated context loss | The NIF tag vocabulary and the Nim/Nimony distinction are re‑derived each session. | Shipped as on‑demand skills; a project map is maintained in persistent memory. |

## Installation

The plugin is loaded from this directory; nothing is published to a registry.

Per session:

```bash
claude --plugin-dir /home/savant/nimony-code
```

From the GitHub marketplace (the repo is its own marketplace):

```text
/plugin marketplace add aoughwl/nim-code
/plugin install nim-code@nim-code
```

`nim-code@nim-code` is `<plugin>@<marketplace>`; both are named `nim-code`. A
local checkout works as a marketplace too — `/plugin marketplace add
/home/savant/nimony-code`.

Enabling the plugin auto‑registers the `nimlang` MCP server and activates all
hooks. Run `/reload-plugins` after editing plugin files to reload without
restarting. Commands are namespaced under the plugin — `/nim-code:check`,
`/nim-code:nif`, and so on — and listed by `/help`.

## Configuration

All configuration is via environment variables; none is required.

| Variable | Effect | Default |
|----------|--------|---------|
| `NIMLANG_TOOLCHAIN` | Forces `nim` or `nimony` for every call. | unset (auto‑detect) |
| `NIM_BIN_DIR` | Directory holding `nim`, `nimsuggest`, `nimble`. | `PATH`, then `~/Nim/bin` |
| `NIMONY_BIN_DIR` | Directory holding `nimony`, `nimsem`, `hastur`. | `PATH`, then `~/nimony/bin` |
| `NIMLANG_AGGRESSIVE` | When truthy, every tool defaults to [terse](#terse-mode) output. | unset (verbose) |

Binaries resolve from `PATH` first, then the corresponding directory.

## Toolchain detection

With `toolchain="auto"` (the default on every tool that takes it), the server
walks up from the target file's directory. It selects Nimony if it finds a
`nimony.paths`, a `nimony.cfg`, or a `nim.cfg` referencing nimony; otherwise Nim.
`NIMLANG_TOOLCHAIN` overrides detection globally, and an explicit `toolchain`
argument overrides it per call.

## Components

```
nim-code/                         ${CLAUDE_PLUGIN_ROOT}
├── .claude-plugin/plugin.json    manifest
├── .mcp.json                     registers the `nimlang` MCP server
├── mcp/
│   ├── server.py                 MCP server — stdlib-only Python 3.7, zero dependencies
│   ├── test_server.py            self-test: exercises all tools against live nim/nimony
│   └── README.md                 manual nimsuggest / nimsem fallback notes
├── hooks/
│   ├── hooks.json                hook wiring
│   ├── guard-nif-read.py         PreToolUse(Read)  — intercept large .nif reads
│   ├── guard-nif-bash.py         PreToolUse(Bash)  — intercept .nif dumps
│   └── trim-build-output.py      PostToolUse(Bash) — strip build noise
├── commands/                     10 slash commands (see Commands)
├── skills/                       5 skills (see Skills and subagents)
└── agents/                       2 subagents (see Skills and subagents)
```

The `nimlang` MCP server is the core. It speaks JSON‑RPC 2.0 over stdio, shells
out to the appropriate toolchain, and returns one compact structured block per
call. It never returns a whole NIF file.

## MCP tool reference

Twelve tools are exposed by the `nimlang` server. `compile`, `outline`,
`defs_uses`, `explain_failure`, `phase_report`, `shrink`, `api`, and `symbols`
support both toolchains. The `nif_*` tools operate on Nimony NIF artifacts and
are Nimony‑only. Every tool accepts `terse` (see [Terse mode](#terse-mode)).

| Tool | Signature | Result / behavior | Toolchains |
|------|-----------|-------------------|------------|
| `compile` | `(file, toolchain="auto", extra_args=[])` | Runs `nim check` or `nimony c`, parses diagnostics, and reports `ok` by the presence of an `Error:` line rather than the exit code. Returns `{ok, toolchain, stage, diagnostics}`. | both |
| `outline` | `(file, toolchain="auto")` | Top‑level symbols `{name, kind, line, col}`. Nim via `nimsuggest outline`; Nimony (and the Nim fallback) via a source regex scan. | both |
| `defs_uses` | `(file, line, col, toolchain="auto")` | Definition and uses of the symbol at a position: `{def, uses}`. Nim via `nimsuggest def`/`use`; Nimony via `nimsem --def:FILE,LINE,COL idetools` and `--usages:` against the module's `.s.nif` in `nimcache`. Degrades to `{error, hint}` if the artifact is absent. | both |
| `explain_failure` | `(file, toolchain="auto")` | Compiles and, on failure, returns a short `verdict` and a `culprit`. Nimony extracts the smallest NIF node spanning the error position from the phase artifact; Nim returns ±3 source lines around the first error. Collapses the compile → outline → query sequence into one call. | both |
| `phase_report` | `(file, toolchain="auto")` | Compiles, then summarizes each `nimcache/*.<phase>.nif` (p, s, …) in one line (top tag counts and size), with no raw NIF. Nim returns an empty phase list with a note. | both |
| `shrink` | `(file, toolchain="auto")` | Delta‑debugs a failing file, dropping top‑level statements while the first `Error:` message is preserved. Returns `{original_lines, minimal_lines, minimal_source, kept_error}`. Iteration‑ and time‑bounded. | both |
| `api` | `(module, toolchain="auto", needle=None)` | Typed public API of a module or dependency without reading its source. Nim runs `nim jsondoc` on a `.nim` path, an installed nimble package (e.g. `chroma`), or a stdlib module (e.g. `std/tables`), returning `{name, kind, sig}` entries. For a `.nif`/Nimony target the typed API is the compiled artifact, rendered via `nif_render`. `needle` filters by name substring. | both |
| `symbols` | `(name, root=".", kind=None, uses=false)` | Project‑wide symbol search by name substring; regex‑based and toolchain‑agnostic. Returns `{defs, root}`, and `{uses}` when `uses:true`. Skips `nimcache`, `.git`, `htmldocs`, and nimble dirs; bounded for large trees. | both |
| `nif_outline` | `(nif_file)` | Top‑level `(tag name …)` nodes of a NIF artifact — names only, no bodies. | Nimony |
| `nif_query` | `(nif_file, needle)` | S‑expr subtrees whose head tag or symbol matches `needle`, each snippet truncated, via a paren‑matching scanner. | Nimony |
| `nif_render` | `(nif_file, needle=None)` | Renders NIF node(s) as compact pseudo‑Nim (`proc`/`var`/`let`/`call`/`if`/`type`/… mapped to Nim‑like syntax; `sym.NN.mod` demangled to `sym`), falling back to a raw snippet for unknown tags. Roughly an order of magnitude smaller than raw NIF. | Nimony |
| `nif_diff` | `(file_a, file_b)` | Structural/line diff between two NIF files (unified diff, context 1, unchanged regions collapsed). | Nimony |

## Terse mode

Every tool accepts an optional `terse` boolean, defaulting to the truthiness of
`NIMLANG_AGGRESSIVE`. Terse output collapses to the smallest useful shape and
drops warnings and hints; verbose shapes are unchanged, so the flag is
back‑compatible and opt‑in.

| Tool | Terse shape |
|------|-------------|
| `compile` | `diagnostics` become `"file:line:col msg"` strings; warnings/hints dropped; `ok` kept. |
| `outline` | `["name:line", …]` |
| `defs_uses` | `{def: "file:line" \| null, uses: ["file:line", …]}` |
| `symbols` | `{defs: ["file:line kind name", …], uses: ["file:line", …]}` |
| `api` | `api` becomes a list of bare signature strings. |
| `nif_query` / `nif_outline` / `nif_render` | Tighter per‑snippet caps (~15 lines); null fields omitted. |

`/aggressive [on|off]` documents enabling terse mode and its trade‑offs.

## Hooks

Three hooks keep raw output out of the context window without agent involvement.
All are stdlib‑only Python and fail open (any error exits 0, never blocking the
tool).

| Hook | Event / matcher | Behavior |
|------|-----------------|----------|
| `guard-nif-read.py` | PreToolUse / `Read` | Denies reading a `.nif` over 15 KB, and attaches a compact `nif_outline` of the file to the denial reason so the agent receives the useful form in the same turn. |
| `guard-nif-bash.py` | PreToolUse / `Bash` | Denies `cat`/`head`/`tail`/`less`/`more`/`bat` targeting a `.nif` over 15 KB — the shell path around the `Read` guard — and points to the NIF tools. No‑op otherwise. |
| `trim-build-output.py` | PostToolUse / `Bash` | For `nimony`/`hastur`/`nim c`/`nimble` commands, strips `nifmake:`/`FAILURE:`/`niflink` lines and surfaces the diagnostics as additional context. No‑op otherwise. |

The `Read` hook illustrates the plugin's preferred pattern: rather than only
blocking a wasteful action, it supplies the cheap alternative in the same
response.

## Commands

| Command | Tool | Purpose |
|---------|------|---------|
| `/check [file]` | `compile` | Compile and report structured diagnostics. |
| `/explain-failure [file]` | `explain_failure` | One‑call "why did this fail," with the culprit. |
| `/shrink [file]` | `shrink` | Minimal still‑failing reproduction. |
| `/api <module> [needle]` | `api` | Typed API of a module or dependency. |
| `/symbols <name>` | `symbols` | Project‑wide symbol search. |
| `/nif <file.nif> [needle]` | `nif_outline` / `nif_query` | Outline or query a NIF artifact. |
| `/render <file.nif> [needle]` | `nif_render` | Pseudo‑Nim view of a NIF node. |
| `/phase-diff <file.nim>` | `nif_diff` | Diff a file's NIF phase artifacts. |
| `/nimony-bug [file]` | — | Run the Nimony compiler debug loop and report only diagnostics. |
| `/aggressive [on\|off]` | — | Explain and toggle terse mode. |

## Skills and subagents

Skills load on demand; subagents run in their own context and return only a
conclusion.

| Skill | Purpose |
|-------|---------|
| `nif-format` | Condensed NIF tag vocabulary and the nifler → nimony → hexer → lengc pipeline; points to `doc/tags.md` for the long tail. |
| `nim-vs-nimony` | Feature‑set and toolchain differences; which binary handles what. |
| `debug-loop` | The `AGENTS.md` compiler debug workflow (build → bug → nimcache diff → rep → `--overwrite`). |
| `token-thrift` | Prefer recipe tools and terse mode; never dump a `.nif`; offload fix loops to `nim-fixer`. |
| `repo-map` | Maintain a lazy, incremental `project-map` in file‑memory (one line per touched file); prefer `symbols`/`api` over grep/reads; persist non‑obvious toolchain facts. |

| Subagent | Model | Purpose |
|----------|-------|---------|
| `nif-inspector` | default | Heavy NIF and phase‑artifact reading in an isolated context; returns only the conclusion. |
| `nim-fixer` | `haiku` | Runs the compile → shrink → explain → edit → recompile loop in its own context and returns only the final diff and a verdict. |

## Examples

The same command works across both toolchains and returns the same diagnostic
shape.

### Nim

`greeter.nim`:

```nim
proc greet(name: string) =
  echo "hi ", nam   # typo: `nam`
```

`/check greeter.nim` detects Nim, runs `nim check`, and returns:

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

### Nimony

`hello.nim`, in a project whose `nimony.cfg`/`nimony.paths` selects Nimony:

```nim
import std/syncio

echo "hello, world
```

`/check hello.nim` detects Nimony, runs `nimony c`, strips the build chatter,
and reports failure despite `nimony c` exiting 0, because an `Error:` line was
parsed:

```json
{
  "ok": false,
  "toolchain": "nimony",
  "stage": "c",
  "diagnostics": [
    { "file": "hello.nim", "line": 3, "col": 6,
      "severity": "Error", "message": "closing \" expected" }
  ]
}
```

## Requirements

- **python3** 3.7+, standard library only. The MCP server and hooks have no
  third‑party dependencies.
- **Nim** — `nim` and `nimsuggest` (e.g. `~/Nim/bin`). Required for the Nim side
  of `compile`, `outline`, `defs_uses`, and for `api` (`nim jsondoc`).
- **Nimony** — `nimony`, `nimsem`, `hastur` (e.g. `~/nimony/bin`, built with
  `nim c -r src/hastur build all`). Required for the Nimony side of every tool
  and for all `nif_*` tools.

`mcp/test_server.py` starts the server and exercises all twelve tools against
live `nim` and `nimony` compiles; run it to verify the environment.

## Design notes

- **Zero dependencies.** The server and hooks are stdlib‑only Python 3.7, so the
  plugin runs wherever `python3` and the toolchains are present, with no install
  step.
- **Server‑side orchestration.** `explain_failure` and `phase_report` run a
  multi‑step workflow inside one call and return only the conclusion, keeping the
  intermediate output out of the transcript.
- **Fail open.** Hooks and best‑effort tool paths degrade to a plain message or a
  structured `{error, hint}` rather than blocking the agent or crashing a tool.
- **Both toolchains, one interface.** Detection, binary resolution, a shared
  diagnostic grammar, and a common result shape mean the same commands serve Nim
  and Nimony without the agent tracking which is in use.

## Changelog

- **0.2** — Terse mode on all tools (`NIMLANG_AGGRESSIVE`); `explain_failure`,
  `phase_report`, `nif_render`, `shrink`, `api`, `symbols`; `guard-nif-bash`
  hook and the transform‑not‑block upgrade to `guard-nif-read`; `nim-fixer`
  subagent; `token-thrift` and `repo-map` skills.
- **0.1** — `nimlang` MCP server (`compile`, `outline`, `nif_outline`,
  `nif_query`, `nif_diff`, `defs_uses`); `guard-nif-read` and
  `trim-build-output` hooks; `nif-inspector` subagent; `nif-format`,
  `nim-vs-nimony`, `debug-loop` skills.
