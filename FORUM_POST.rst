====================================================================
nim-code: a token-efficient Claude Code plugin for Nim *and* Nimony
====================================================================

:Author: savannt
:Repo: https://github.com/aoughwl/nim-code

I have been running Claude Code against both mainline **Nim** and the new
**Nimony** compiler, and the single biggest cost was not reasoning — it was
*tokens spent staring at bytes the model never needed*. NIF artifacts are
verbose S-expression token streams (I routinely hit ``.nif`` files of
160 KB–713 KB); ``nimony`` / ``hastur`` output is buried in ``nifmake:`` /
``FAILURE:`` / ``niflink`` noise; and ``nimony c`` cheerfully **exits 0 even on
failure**. A vanilla agent reads the whole file, re-reads ``tags.md``, and
conflates Nim-2 semantics with Nimony's. Every one of those is pure token waste.

``nim-code`` is a Claude Code *plugin* that fixes this at the tool-call layer. It
works for **both** toolchains via auto-detection (walk up for
``nimony.paths`` / ``nimony.cfg`` / ``nim.cfg``; env ``NIMLANG_TOOLCHAIN``;
explicit override), resolving binaries from ``PATH`` then ``~/Nim/bin`` /
``~/nimony/bin`` (overridable via ``NIM_BIN_DIR`` / ``NIMONY_BIN_DIR``).

Architecture
============

Four layers, each a distinct token lever.

1. **MCP server** — zero-dependency Python (stdlib only, 3.7-safe; nothing to
   ``pip install``). Shells out to the real toolchain and returns *only
   structured answers*, never raw NIF. Ten tools:

   :``compile``: runs ``nim check`` / ``nimony c``, parses the shared diagnostic
     grammar ``file(line, col) Error|Warning|Hint|Trace: msg``, strips
     build-driver noise, and reports correct ``ok`` by treating *any* ``Error:``
     line as failure (working around ``nimony c`` exit-0).
   :``outline``: symbols via ``nimsuggest`` (Nim) or a regex fallback (Nimony
     source too).
   :``nif_outline`` / ``nif_query`` / ``nif_diff``: a tiny paren-matching scanner
     returns just the top-level tags, the matching subtrees, or a collapsed
     structural diff. The 700 KB file never enters context.
   :``defs_uses``: ``nimsuggest def/use`` (Nim) or ``nimsem --def/--usages ...
     idetools`` on the ``.s.nif`` (Nimony).
   :``explain_failure``: **one call replacing** compile→list→outline→query. On
     Nimony it extracts the *smallest NIF node spanning the error position*; on
     Nim, ±3 source lines around the first error.
   :``phase_report``: per-phase (``p`` → ``s`` → …) one-line summaries (top tag
     counts + size), never raw NIF.
   :``nif_render``: renders NIF nodes as compact **pseudo-Nim** (demangling
     ``sym.NN.mod`` → ``sym``), ~10× smaller than raw.
   :``shrink``: delta-debugs a failing file to a minimal still-failing repro
     that preserves the first ``Error``.

2. **Hooks** — the part that pays off even without the MCP:

   - ``guard-nif-read`` (**PreToolUse**/Read) does *transform-not-block*: a raw
     read of a big ``.nif`` is denied, **but the compact outline is returned in
     the same turn**, so the model gets the useful version for free.
   - ``guard-nif-bash`` (**PreToolUse**/Bash) blocks ``cat`` / ``head`` /
     ``tail`` / ``less`` / ``more`` / ``bat`` on a large ``.nif`` before a
     megabyte sneaks into context behind the Read tool's back.
   - ``trim-build-output`` (**PostToolUse**/Bash) strips ``nifmake:`` /
     ``FAILURE:`` / ``niflink`` noise, surfacing only diagnostics.

3. **Subagents** — ``nif-inspector`` does heavy NIF spelunking in its *own*
   context and returns only the conclusion; ``nim-fixer`` runs the whole
   compile→fix→recompile loop on a **cheap model (Haiku)** and hands back only
   the final diff + verdict. Verbose grunt work never touches — or bills — your
   main thread.

4. **Skills + commands** — skills ``nif-format`` (condensed tag vocab + the
   nifler→nimony→hexer→lengc pipeline), ``nim-vs-nimony`` ("don't assume Nim 2's
   feature set"), ``debug-loop`` (the ``AGENTS.md`` workflow), ``token-thrift``
   (bias toward the recipe tools). Commands: ``/check``, ``/nif``,
   ``/phase-diff``, ``/nimony-bug``, ``/explain-failure``, ``/shrink``,
   ``/render``, ``/aggressive``.

Aggressive mode
===============

Every tool takes a **terse** flag (default from ``NIMLANG_AGGRESSIVE=1``) that
collapses output to compact shapes — ``["file:line:col msg"]``,
``["name:line"]``, ``{def:"file:line", uses:[...]}`` — dropping warnings/hints
and tightening NIF snippet caps. The recipe tools (``explain_failure``,
``phase_report``) are **code-execution-over-MCP**: orchestrate server-side,
return only the answer.

Why this generalizes
=====================

The lesson (and my own token accounting) is blunt: *don't hand the model bytes
to filter — filter first, then hand it the answer.* Server-side aggregation,
transform-not-block hooks, and cheap-model offload are large reductions on the
NIF-heavy paths, and none require the model to be smarter. Nimony's NIF-centric
pipeline is an ideal case because the IR is both huge and highly structured —
exactly what a scanner summarizes losslessly.

Status: the MCP server ships with a self-test (``mcp/test_server.py``) that
starts the server and exercises all ten tools against live ``nim`` and
``nimony`` compiles (compile/outline/NIF tools + explain_failure for both
toolchains, terse mode, shrink, nif_render) — currently **12/12 green**.

Install via ``/plugin`` or ``claude --plugin-dir``. Issues, NIF-tag corrections,
and Nimony-toolchain edge cases very welcome — the Nimony CLI surface is a
moving target and I would rather track it in the open.
