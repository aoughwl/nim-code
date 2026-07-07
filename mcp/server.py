#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nimlang MCP server for the nim-code Claude Code plugin.

Zero-dependency (stdlib only), Python 3.7 compatible. Speaks JSON-RPC 2.0 over
stdio implementing the MCP subset the plugin needs: initialize,
notifications/initialized, tools/list, tools/call.

Supports BOTH Nim and Nimony toolchains (auto-detected per SPEC, overridable).
See mcp/README.md for the tool list and manual fallbacks.
"""

import sys
import os
import re
import json
import glob
import time
import difflib
import subprocess

# --------------------------------------------------------------------------
# Shared diagnostic parsing
# --------------------------------------------------------------------------

# Identical format for Nim and Nimony:  file(line, col) Sev: message
DIAG_RE = re.compile(
    r'^(?P<file>.+?)\((?P<line>\d+),\s*(?P<col>\d+)\)\s+'
    r'(?P<sev>Error|Warning|Hint|Trace):\s*(?P<msg>.*)$'
)

# Nimony build-driver noise that must be stripped from output.
NOISE_PREFIXES = ('nifmake:', 'FAILURE:', 'niflink', 'nifmake ', 'SUCCESS:')

DEFAULT_TIMEOUT = 120


def parse_diagnostics(text):
    """Return a list of {file,line,col,severity,message} from compiler output.

    Skips Nimony's nifmake/FAILURE/niflink noise lines.
    """
    diags = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(NOISE_PREFIXES):
            continue
        m = DIAG_RE.match(line)
        if m is None:
            continue
        diags.append({
            'file': m.group('file'),
            'line': int(m.group('line')),
            'col': int(m.group('col')),
            'severity': m.group('sev'),
            'message': m.group('msg'),
        })
    return diags


# --------------------------------------------------------------------------
# Terse mode (v0.2 aggressive token-saving layer)
# --------------------------------------------------------------------------

def _env_truthy(val):
    if val is None:
        return False
    return str(val).strip().lower() not in ('', '0', 'false', 'no', 'off')


def terse_default():
    """Default terse setting from env NIMLANG_AGGRESSIVE (truthy)."""
    return _env_truthy(os.environ.get('NIMLANG_AGGRESSIVE'))


def resolve_terse(args):
    """Per-call terse: explicit `terse` arg wins, else env default."""
    if isinstance(args, dict) and 'terse' in args and args['terse'] is not None:
        return bool(args['terse'])
    return terse_default()


def diag_to_str(d):
    """Terse one-line form of a diagnostic dict: 'file:line:col msg'."""
    return '%s:%s:%s %s' % (d.get('file', '?'), d.get('line', '?'),
                            d.get('col', '?'), d.get('message', ''))


def pos_to_str(obj):
    """Terse 'file:line' for a {file,line,col} dict (or None -> None)."""
    if not obj:
        return None
    return '%s:%s' % (obj.get('file', '?'), obj.get('line', '?'))


# --------------------------------------------------------------------------
# Binary resolution
# --------------------------------------------------------------------------

def _home(*parts):
    return os.path.join(os.path.expanduser('~'), *parts)


def find_bin(name, env_dir, default_dir):
    """Resolve a binary: PATH first, then env_dir, then default_dir.

    Returns the resolved path (absolute if found in a directory) or just the
    bare name so subprocess can still try PATH as a last resort.
    """
    # 1) PATH
    from shutil import which
    hit = which(name)
    if hit:
        return hit
    exe = name
    if os.name == 'nt' and not name.lower().endswith('.exe'):
        exe = name + '.exe'
    # 2) env override dir
    candidates = []
    override = os.environ.get(env_dir)
    if override:
        candidates.append(os.path.join(override, exe))
    # 3) default dir
    candidates.append(os.path.join(default_dir, exe))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return name


def nim_bin(name):
    return find_bin(name, 'NIM_BIN_DIR', _home('Nim', 'bin'))


def nimony_bin(name):
    return find_bin(name, 'NIMONY_BIN_DIR', _home('nimony', 'bin'))


# --------------------------------------------------------------------------
# Toolchain detection
# --------------------------------------------------------------------------

def detect_toolchain(file_path):
    """auto: walk up from file; nimony if a nimony marker is found, else nim.

    Honors NIMLANG_TOOLCHAIN env override.
    """
    env = os.environ.get('NIMLANG_TOOLCHAIN')
    if env in ('nim', 'nimony'):
        return env

    try:
        d = os.path.dirname(os.path.abspath(file_path))
    except Exception:
        return 'nim'

    prev = None
    while d and d != prev:
        # explicit nimony markers
        for marker in ('nimony.paths', 'nimony.cfg'):
            if os.path.isfile(os.path.join(d, marker)):
                return 'nimony'
        # nim.cfg that mentions nimony
        ncfg = os.path.join(d, 'nim.cfg')
        if os.path.isfile(ncfg):
            try:
                with open(ncfg, 'r', errors='replace') as fh:
                    if 'nimony' in fh.read().lower():
                        return 'nimony'
            except Exception:
                pass
        prev = d
        d = os.path.dirname(d)
    return 'nim'


def resolve_toolchain(file_path, toolchain):
    if toolchain in ('nim', 'nimony'):
        return toolchain
    return detect_toolchain(file_path)


# --------------------------------------------------------------------------
# subprocess helper
# --------------------------------------------------------------------------

def run(cmd, cwd=None, timeout=DEFAULT_TIMEOUT, stdin_data=None):
    """Run a command, return (returncode, combined_output, timed_out)."""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
    except OSError as e:
        return (127, 'failed to launch %s: %s' % (cmd[0], e), False)
    try:
        out, _ = proc.communicate(input=stdin_data, timeout=timeout)
        return (proc.returncode, out or '', False)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = ''
        return (proc.returncode if proc.returncode is not None else -1,
                out or '', True)


# --------------------------------------------------------------------------
# Tool: compile
# --------------------------------------------------------------------------

def tool_compile(args):
    file_path = args.get('file')
    if not file_path:
        return {'error': 'missing required arg: file'}
    toolchain = resolve_toolchain(file_path, args.get('toolchain', 'auto'))
    extra = args.get('extra_args') or []
    if not isinstance(extra, list):
        extra = [str(extra)]
    cwd = os.path.dirname(os.path.abspath(file_path)) or None

    if toolchain == 'nimony':
        cmd = [nimony_bin('nimony'), 'c'] + list(extra) + [file_path]
        stage = 'c'
    else:
        cmd = [nim_bin('nim'), 'check', '--hints:off', '--colors:off'] + \
            list(extra) + [file_path]
        stage = 'check'

    rc, out, timed_out = run(cmd, cwd=cwd, timeout=120)
    diags = parse_diagnostics(out)
    has_error = any(d['severity'] == 'Error' for d in diags)

    if toolchain == 'nimony':
        # exit code is unreliable; presence of any Error line == failure
        ok = not has_error
    else:
        ok = (rc == 0) and not has_error

    if timed_out:
        ok = False

    if resolve_terse(args):
        # drop Warning/Hint, diagnostics become "file:line:col msg" strings.
        kept = [d for d in diags if d['severity'] in ('Error', 'Trace')]
        result = {
            'ok': ok,
            'toolchain': toolchain,
            'stage': stage,
            'diagnostics': [diag_to_str(d) for d in kept],
        }
        if timed_out:
            result['timed_out'] = True
        return result

    result = {
        'ok': ok,
        'toolchain': toolchain,
        'stage': stage,
        'diagnostics': diags,
    }
    if timed_out:
        result['timed_out'] = True
    return result


# --------------------------------------------------------------------------
# Tool: outline
# --------------------------------------------------------------------------

OUTLINE_RE = re.compile(
    r'^\s*(?P<kind>proc|func|method|template|macro|iterator|converter|'
    r'type|const|var|let)\b[\s*]*(?P<name>[A-Za-z_`][\w`]*)'
)


def outline_regex(file_path):
    symbols = []
    try:
        with open(file_path, 'r', errors='replace') as fh:
            for idx, raw in enumerate(fh, start=1):
                m = OUTLINE_RE.match(raw)
                if m is None:
                    continue
                name = m.group('name').strip('`')
                col = raw.index(m.group('name')) + 1
                symbols.append({
                    'name': name,
                    'kind': m.group('kind'),
                    'line': idx,
                    'col': col,
                })
    except (IOError, OSError) as e:
        return None, str(e)
    return symbols, None


def outline_nimsuggest(file_path):
    """Try nimsuggest 'outline'; return list or None on any trouble."""
    cmd = [nim_bin('nimsuggest'), '--stdin', file_path]
    stdin_data = 'outline %s\nquit\n' % file_path
    rc, out, timed_out = run(cmd, timeout=60, stdin_data=stdin_data)
    if timed_out or not out:
        return None
    symbols = []
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) < 7 or parts[0] != 'outline':
            continue
        # outline \t symkind \t qualname \t sig \t file \t line \t col ...
        kind = parts[1]
        name = parts[2]
        try:
            ln = int(parts[5])
            col = int(parts[6])
        except (ValueError, IndexError):
            continue
        if kind.startswith('sk'):
            kind = kind[2:].lower()
        symbols.append({'name': name, 'kind': kind, 'line': ln, 'col': col})
    if not symbols:
        return None
    return symbols


def tool_outline(args):
    file_path = args.get('file')
    if not file_path:
        return {'error': 'missing required arg: file'}
    toolchain = resolve_toolchain(file_path, args.get('toolchain', 'auto'))

    symbols = None
    used_fallback = True
    if toolchain == 'nim':
        symbols = outline_nimsuggest(file_path)
        if symbols is not None:
            used_fallback = False
    if symbols is None:
        symbols, err = outline_regex(file_path)
        if symbols is None:
            return {'error': err or 'could not read file', 'toolchain': toolchain}
    if resolve_terse(args):
        return {'toolchain': toolchain,
                'symbols': ['%s:%s' % (s['name'], s['line']) for s in symbols]}
    result = {'toolchain': toolchain, 'symbols': symbols}
    if used_fallback:
        result['source'] = 'regex-fallback'
    return result


# --------------------------------------------------------------------------
# Minimal NIF S-expression scanner
# --------------------------------------------------------------------------

def nif_parse_forms(text):
    """Tiny paren-matching scanner over a NIF stream.

    Returns a flat list of forms, each: {start, end, depth, line, tokens}.
    tokens holds the atom/string tokens that are DIRECT children of the form
    (nested lists are their own forms). Handles NIF string literals ("..." with
    backslash escapes) so parens inside strings do not confuse the scanner.
    """
    forms = []
    stack = []
    i = 0
    n = len(text)
    line = 1
    while i < n:
        c = text[i]
        if c == '\n':
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        if c == '(':
            form = {'start': i, 'end': n, 'depth': len(stack),
                    'line': line, 'tokens': []}
            stack.append(form)
            forms.append(form)
            i += 1
            continue
        if c == ')':
            if stack:
                stack.pop()['end'] = i + 1
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '"':
                    break
                if text[j] == '\n':
                    line += 1
                j += 1
            tok = text[i:j + 1]
            i = j + 1
        else:
            j = i
            while j < n and (not text[j].isspace()) and text[j] not in '()':
                j += 1
            tok = text[i:j]
            i = j
        if stack:
            stack[-1]['tokens'].append(tok)
    return forms


def _base_tag(tok):
    """Strip NIF line-info suffix (starting at '@') from a tag token."""
    if tok is None:
        return ''
    return tok.split('@', 1)[0]


def _clean_name(tok):
    if tok is None:
        return ''
    name = tok.split('@', 1)[0]
    name = name.lstrip(':')
    return name


def _read_nif(nif_file):
    with open(nif_file, 'r', errors='replace') as fh:
        return fh.read()


# --------------------------------------------------------------------------
# NIF line-info decoding (base62 deltas, relative to parent node).
# Mirrors nifreader.handleLineInfo + nifstreams.rawNext: each node's absolute
# (line, col) = parent absolute + this node's delta; a filename resets to an
# absolute position. Columns are 0-based in NIF; source diagnostics are 1-based.
# --------------------------------------------------------------------------

def _b62(c):
    if '0' <= c <= '9':
        return ord(c) - 48
    if 'A' <= c <= 'Z':
        return ord(c) - 65 + 10
    if 'a' <= c <= 'z':
        return ord(c) - 97 + 36
    return None


def _clean_symbol(tok):
    """Strip line-info suffix and leading ':' from a NIF atom/tag token."""
    if tok is None:
        return ''
    m = re.search(r'[@~]', tok)
    if m is not None:
        tok = tok[:m.start()]
    return tok.lstrip(':')


def _parse_suffix(tok):
    """Decode a token's NIF line-info suffix.

    Returns (col_delta, line_delta, filename_or_None) or None if the token
    carries no suffix.
    """
    if tok is None:
        return None
    m = re.search(r'[@~]', tok)
    if m is None:
        return None
    s = tok[m.start():]
    if s[:1] == '@':
        s = s[1:]
    i = 0
    n = len(s)
    col = 0
    neg = False
    if i < n and s[i] == '~':
        neg = True
        i += 1
    while i < n and _b62(s[i]) is not None:
        col = col * 62 + _b62(s[i])
        i += 1
    if neg:
        col = -col
    line = 0
    if i < n and s[i] == ',':
        i += 1
        neg2 = False
        if i < n and s[i] == '~':
            neg2 = True
            i += 1
        while i < n and _b62(s[i]) is not None:
            line = line * 62 + _b62(s[i])
            i += 1
        if neg2:
            line = -line
    fname = None
    if i < n and s[i] == ',':
        fname = s[i + 1:]
    return (col, line, fname)


def nif_forms_with_pos(text):
    """Return the flat form list with an absolute source position on each.

    Each form gains f['src'] = (file, line, col) with a 1-based col (converted
    from NIF's 0-based). Uses the paren scanner then a pre-order pass that
    accumulates base62 deltas against the enclosing parent form.
    """
    forms = nif_parse_forms(text)
    stack = []  # (depth, file, line, col)
    for f in forms:
        d = f['depth']
        while stack and stack[-1][0] >= d:
            stack.pop()
        parent = stack[-1] if stack else (-1, None, 0, 0)
        tag_tok = f['tokens'][0] if f['tokens'] else ''
        suf = _parse_suffix(tag_tok)
        if suf is None:
            cd, ld, fn = 0, 0, None
        else:
            cd, ld, fn = suf
        if fn:
            afile, aline, acol = fn, ld, cd
        else:
            afile, aline, acol = parent[1], parent[2] + ld, parent[3] + cd
        f['src'] = (afile, aline, acol + 1)  # to 1-based col
        stack.append((d, afile, aline, acol))
    return forms


def tool_nif_outline(args):
    nif_file = args.get('nif_file')
    if not nif_file:
        return {'error': 'missing required arg: nif_file'}
    if not os.path.isfile(nif_file):
        return {'error': 'no such file: %s' % nif_file}
    try:
        text = _read_nif(nif_file)
    except (IOError, OSError) as e:
        return {'error': str(e)}

    forms = nif_parse_forms(text)
    # Find the top statement container (stmts). Its direct children are the
    # top-level nodes we want.
    stmts = None
    for f in forms:
        if f['tokens'] and _base_tag(f['tokens'][0]) == 'stmts':
            stmts = f
            break
    tags = []
    if stmts is not None:
        child_depth = stmts['depth'] + 1
        lo, hi = stmts['start'], stmts['end']
        for f in forms:
            if f['depth'] != child_depth:
                continue
            if f['start'] < lo or f['end'] > hi:
                continue
            toks = f['tokens']
            if not toks:
                continue
            tag = _base_tag(toks[0])
            name = _clean_name(toks[1]) if len(toks) > 1 else ''
            tags.append({'tag': tag, 'name': name, 'line': f['line']})
    else:
        # No stmts wrapper: list depth-0 forms as a fallback.
        for f in forms:
            if f['depth'] != 0 or not f['tokens']:
                continue
            tag = _base_tag(f['tokens'][0])
            name = _clean_name(f['tokens'][1]) if len(f['tokens']) > 1 else ''
            tags.append({'tag': tag, 'name': name, 'line': f['line']})
    if resolve_terse(args):
        # no null/empty fields
        terse = []
        for node in tags:
            item = {'tag': node['tag'], 'line': node['line']}
            if node['name']:
                item['name'] = node['name']
            terse.append(item)
        return {'tags': terse}
    return {'tags': tags}


def _truncate_snippet(text, max_lines=40, max_chars=2000):
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    snippet = '\n'.join(lines)
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars]
        truncated = True
    if truncated:
        snippet = snippet + '\n...'
    return snippet


def tool_nif_query(args):
    nif_file = args.get('nif_file')
    needle = args.get('needle')
    if not nif_file:
        return {'error': 'missing required arg: nif_file'}
    if not needle:
        return {'error': 'missing required arg: needle'}
    if not os.path.isfile(nif_file):
        return {'error': 'no such file: %s' % nif_file}
    try:
        text = _read_nif(nif_file)
    except (IOError, OSError) as e:
        return {'error': str(e)}

    terse = resolve_terse(args)
    max_lines = 15 if terse else 40
    needle_l = needle.lower()
    forms = nif_parse_forms(text)
    matches = []
    seen = set()
    cap = 50
    for f in forms:
        toks = f['tokens']
        if not toks:
            continue
        tag = _base_tag(toks[0])
        name = _clean_name(toks[1]) if len(toks) > 1 else ''
        head = ' '.join(toks[:2]).lower()
        if tag.lower() == needle_l or needle_l in head:
            key = (f['start'], f['end'])
            if key in seen:
                continue
            seen.add(key)
            snippet = _truncate_snippet(text[f['start']:f['end']],
                                        max_lines=max_lines)
            if terse:
                item = {'tag': tag, 'snippet': snippet}
                if name:
                    item['name'] = name
                matches.append(item)
            else:
                matches.append({'tag': tag, 'name': name, 'snippet': snippet})
            if len(matches) >= cap:
                break
    return {'matches': matches, 'count': len(matches)}


# --------------------------------------------------------------------------
# Tool: nif_diff
# --------------------------------------------------------------------------

def tool_nif_diff(args):
    a = args.get('file_a')
    b = args.get('file_b')
    if not a or not b:
        return {'error': 'missing required args: file_a, file_b'}
    for p in (a, b):
        if not os.path.isfile(p):
            return {'error': 'no such file: %s' % p}
    try:
        with open(a, 'r', errors='replace') as fh:
            la = fh.read().splitlines()
        with open(b, 'r', errors='replace') as fh:
            lb = fh.read().splitlines()
    except (IOError, OSError) as e:
        return {'error': str(e)}

    diff = difflib.unified_diff(
        la, lb, fromfile=os.path.basename(a), tofile=os.path.basename(b),
        n=1, lineterm='')
    changed = []
    for line in diff:
        # trim the ---/+++ file header lines (keep @@ hunk markers + edits)
        if line.startswith('--- ') or line.startswith('+++ '):
            continue
        changed.append(line)
    return {'changed': changed}


# --------------------------------------------------------------------------
# Tool: defs_uses
# --------------------------------------------------------------------------

def nimsuggest_query(section, file_path, line, col):
    """Run one nimsuggest def/use query. Returns list of {file,line,col}."""
    cmd = [nim_bin('nimsuggest'), '--stdin', file_path]
    stdin_data = '%s %s:%d:%d\nquit\n' % (section, file_path, line, col)
    rc, out, timed_out = run(cmd, timeout=60, stdin_data=stdin_data)
    if timed_out or not out:
        return None
    results = []
    for l in out.splitlines():
        parts = l.split('\t')
        if len(parts) < 7 or parts[0] != section:
            continue
        try:
            ln = int(parts[5])
            cl = int(parts[6])
        except (ValueError, IndexError):
            continue
        results.append({'file': parts[4], 'line': ln, 'col': cl})
    return results


def defs_uses_nim(file_path, line, col):
    defs = nimsuggest_query('def', file_path, line, col)
    uses = nimsuggest_query('use', file_path, line, col)
    if defs is None and uses is None:
        # nimsuggest unavailable/flaky -> degrade
        return {
            'def': None,
            'uses': [],
            'error': 'nimsuggest unavailable or timed out',
            'hint': 'run manually: nimsuggest --stdin %s then '
                    'def/use %s:%d:%d' % (file_path, file_path, line, col),
        }
    def_obj = defs[0] if defs else None
    return {'def': def_obj, 'uses': uses or []}


def _find_s_nif(cwd, basename):
    """Find the .s.nif in cwd/nimcache whose stmts header names basename."""
    ncache = os.path.join(cwd, 'nimcache')
    candidates = sorted(glob.glob(os.path.join(ncache, '*.s.nif')),
                        key=os.path.getmtime, reverse=True)
    header_marker = ',' + basename
    for path in candidates:
        try:
            with open(path, 'r', errors='replace') as fh:
                head = fh.read(4096)
        except (IOError, OSError):
            continue
        # first stmts line looks like: (stmts@,1,good.nim
        if header_marker in head.splitlines()[0] if head else False:
            return path
        for hl in head.splitlines()[:4]:
            if hl.startswith('(stmts') and header_marker in hl:
                return path
    return None


def _nimsem_idetools(mode_flag, nif_file, basename, line, col):
    """mode_flag: '--usages' or '--def'. Returns list of {file,line,col}."""
    track = '%s:%s,%d,%d' % (mode_flag, basename, line, col)
    cmd = [nimony_bin('nimsem'), track, 'idetools', nif_file]
    rc, out, timed_out = run(cmd, timeout=60)
    if timed_out or not out:
        return None
    results = []
    for l in out.splitlines():
        parts = l.split('\t')
        if len(parts) < 3:
            continue
        kind = parts[0].strip()
        if kind not in ('use', 'def'):
            continue
        # kind \t ... \t symname \t ... \t file \t line \t col
        try:
            cl = int(parts[-1])
            ln = int(parts[-2])
            fl = parts[-3]
        except (ValueError, IndexError):
            continue
        results.append({'file': fl, 'line': ln, 'col': cl})
    return results


def defs_uses_nimony(file_path, line, col):
    cwd = os.path.dirname(os.path.abspath(file_path)) or '.'
    basename = os.path.basename(file_path)
    hint = ('build the file first (nimony c %s), then run: nimsem '
            '--usages:%s,%d,%d idetools nimcache/<hash>.s.nif'
            % (file_path, basename, line, col))

    s_nif = _find_s_nif(cwd, basename)
    if s_nif is None:
        # try compiling to produce the artifact
        run([nimony_bin('nimony'), 'c', file_path], cwd=cwd, timeout=120)
        s_nif = _find_s_nif(cwd, basename)
    if s_nif is None:
        return {'def': None, 'uses': [],
                'error': 'no .s.nif artifact found in %s/nimcache' % cwd,
                'hint': hint}

    defs = _nimsem_idetools('--def', s_nif, basename, line, col)
    uses = _nimsem_idetools('--usages', s_nif, basename, line, col)
    if defs is None and uses is None:
        return {'def': None, 'uses': [],
                'error': 'nimsem idetools unavailable or timed out',
                'hint': hint}
    def_obj = defs[0] if defs else None
    return {'def': def_obj, 'uses': uses or [], 'nif': s_nif}


def tool_defs_uses(args):
    file_path = args.get('file')
    if not file_path:
        return {'error': 'missing required arg: file'}
    try:
        line = int(args.get('line'))
        col = int(args.get('col'))
    except (TypeError, ValueError):
        return {'error': 'line and col must be integers'}
    toolchain = resolve_toolchain(file_path, args.get('toolchain', 'auto'))
    if toolchain == 'nimony':
        result = defs_uses_nimony(file_path, line, col)
    else:
        result = defs_uses_nim(file_path, line, col)
    if resolve_terse(args):
        terse = {'def': pos_to_str(result.get('def')),
                 'uses': [pos_to_str(u) for u in result.get('uses', [])]}
        if 'error' in result:
            terse['error'] = result['error']
        return terse
    return result


# --------------------------------------------------------------------------
# Nimcache artifact discovery (shared by explain_failure / phase_report)
# --------------------------------------------------------------------------

def _artifact_names_basename(path, basename):
    """True if a NIF artifact's stmts header names the given source basename."""
    try:
        with open(path, 'r', errors='replace') as fh:
            head = fh.read(4096)
    except (IOError, OSError):
        return False
    for hl in head.splitlines()[:6]:
        if hl.startswith('(stmts') and (',' + basename) in hl:
            return True
    return False


def _find_module_nif(cwd, basename, phase):
    """Newest nimcache/*.<phase>.nif whose stmts header names basename."""
    ncache = os.path.join(cwd, 'nimcache')
    pat = os.path.join(ncache, '*.' + phase + '.nif')
    cands = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
    for path in cands:
        if '.deps.' in os.path.basename(path):
            continue
        if _artifact_names_basename(path, basename):
            return path
    return None


# --------------------------------------------------------------------------
# NIF -> pseudo-Nim rendering (for nif_render)
# --------------------------------------------------------------------------

def _nif_tokenize(text):
    toks = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == '(' or c == ')':
            toks.append(c)
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            toks.append(text[i:j + 1])
            i = j + 1
            continue
        j = i
        while j < n and (not text[j].isspace()) and text[j] not in '()':
            j += 1
        toks.append(text[i:j])
        i = j
    return toks


def _nif_build(toks, pos):
    """Build a nested node {tag, children} from tokens; children are nodes or
    raw atom strings. `pos` must point at a '('. Returns (node, next_pos)."""
    pos += 1  # skip '('
    tag = ''
    if pos < len(toks) and toks[pos] not in ('(', ')'):
        tag = toks[pos]
        pos += 1
    children = []
    while pos < len(toks) and toks[pos] != ')':
        if toks[pos] == '(':
            child, pos = _nif_build(toks, pos)
            children.append(child)
        else:
            children.append(toks[pos])
            pos += 1
    if pos < len(toks):
        pos += 1  # skip ')'
    return {'tag': tag, 'children': children}, pos


_ESCAPE_RE = re.compile(r'\\([0-9A-Fa-f]{2})')


def _demangle(atom):
    """Render a NIF atom as Nim-ish: strip line info, demangle sym.NN.mod->sym,
    decode \\HH operator escapes, map dot tokens to ''."""
    if not isinstance(atom, str):
        return ''
    if atom == '.':
        return ''
    if atom[:1] == '"':
        return atom
    if '\\' in atom:
        atom = _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), atom)
    cleaned = _clean_symbol(atom)
    m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\.\d+', cleaned)
    if m is not None:
        rest = cleaned[m.end():]
        if rest == '' or rest.startswith('.'):
            return m.group(1)
    return cleaned


_BINOP = {
    'add': '+', 'sub': '-', 'mul': '*', 'div': 'div', 'mod': 'mod',
    'shl': 'shl', 'shr': 'shr', 'bitand': 'and', 'bitor': 'or',
    'bitxor': 'xor', 'eq': '==', 'neq': '!=', 'lt': '<', 'le': '<=',
    'gt': '>', 'ge': '>=', 'and': 'and', 'or': 'or', 'xor': 'xor',
}


def _r(x):
    return _render_node(x) if isinstance(x, dict) else _demangle(x)


def _nonempty(children):
    out = []
    for c in children:
        r = _r(c)
        if r != '':
            out.append(r)
    return out


def _child_nodes(children, tag):
    return [c for c in children
            if isinstance(c, dict) and _base_tag(c['tag']) == tag]


def _render_node(node):
    """Map a NIF node to a compact pseudo-Nim string. Unknown tags fall back
    to a compact raw s-expr."""
    tag = _base_tag(node.get('tag', ''))
    ch = node.get('children', [])

    if tag == 'stmts':
        lines = []
        for c in ch:
            r = _r(c)
            if r != '':
                lines.append(r)
        return '\n'.join(lines)

    if tag in ('proc', 'func', 'method', 'macro', 'template', 'iterator',
               'converter'):
        name = _r(ch[0]) if ch else ''
        params = _child_nodes(ch, 'params')
        param_str = _render_node(params[0]) if params else ''
        body = _child_nodes(ch, 'stmts')
        ret = ''
        # return type: first non-dot atom / type node after params
        seen_params = False
        for c in ch[1:]:
            if isinstance(c, dict) and _base_tag(c['tag']) == 'params':
                seen_params = True
                continue
            if seen_params:
                r = _r(c)
                if r != '' and not (isinstance(c, dict) and
                                    _base_tag(c['tag']) == 'stmts'):
                    ret = r
                    break
        head = '%s %s(%s)' % (tag, name, param_str)
        if ret:
            head = head + ': ' + ret
        if body:
            inner = _render_node(body[0])
            inner = '\n'.join('  ' + l for l in inner.splitlines())
            return head + ' =\n' + inner
        return head

    if tag == 'params':
        return ', '.join(_r(c) for c in ch
                         if isinstance(c, dict) and
                         _base_tag(c['tag']) in ('param', 'fld'))

    if tag in ('param', 'fld'):
        name = _r(ch[0]) if ch else ''
        typ = ''
        for c in ch[1:]:
            r = _r(c)
            if r != '':
                typ = r
                break
        if typ:
            return '%s: %s' % (name, typ)
        return name

    if tag in ('let', 'var', 'const', 'glet', 'gvar', 'tvar', 'cursor'):
        kw = 'var' if tag in ('var', 'gvar', 'tvar') else \
            ('const' if tag == 'const' else 'let')
        name = _r(ch[0]) if ch else ''
        rest = _nonempty(ch[1:])
        if len(rest) >= 2:
            return '%s %s: %s = %s' % (kw, name, rest[0], rest[-1])
        if len(rest) == 1:
            return '%s %s = %s' % (kw, name, rest[0])
        return '%s %s' % (kw, name)

    if tag in ('call', 'cmd'):
        if not ch:
            return tag + '()'
        callee = _r(ch[0])
        args = [_r(c) for c in ch[1:]]
        return '%s(%s)' % (callee, ', '.join(a for a in args if a != ''))

    if tag == 'infix':
        if len(ch) >= 3:
            return '%s %s %s' % (_r(ch[1]), _r(ch[0]), _r(ch[2]))
        return ' '.join(_r(c) for c in ch)

    if tag in _BINOP and len(ch) >= 3:
        return '%s %s %s' % (_r(ch[-2]), _BINOP[tag], _r(ch[-1]))

    if tag == 'asgn' and len(ch) >= 2:
        return '%s = %s' % (_r(ch[0]), _r(ch[1]))

    if tag == 'ret':
        inner = _nonempty(ch)
        return 'return ' + (inner[0] if inner else '')

    if tag == 'result':
        return ''  # implicit result declaration

    if tag in ('if', 'when'):
        parts = []
        for c in ch:
            if not isinstance(c, dict):
                continue
            btag = _base_tag(c['tag'])
            cc = c['children']
            if btag in ('elif',) and len(cc) >= 2:
                parts.append('%s %s: %s' % (tag, _r(cc[0]), _r(cc[1])))
            elif btag == 'else' and cc:
                parts.append('else: %s' % _r(cc[0]))
        return '\n'.join(parts) if parts else tag

    if tag == 'type':
        name = _r(ch[0]) if ch else ''
        body = ''
        for c in ch[1:]:
            r = _r(c)
            if r != '':
                body = r
        return 'type %s = %s' % (name, body) if body else 'type %s' % name

    if tag == 'object':
        flds = _child_nodes(ch, 'fld')
        if flds:
            body = '\n'.join('  ' + _render_node(f) for f in flds)
            return 'object\n' + body
        return 'object'

    if tag in ('i', 'u'):
        return 'int' if tag == 'i' else 'uint'
    if tag == 'f':
        return 'float'

    if tag == 'suf':
        return _r(ch[0]) if ch else ''

    if tag in ('par', 'conv'):
        inner = _nonempty(ch)
        return inner[-1] if inner else ''

    # Unknown tag -> compact raw s-expr fallback.
    parts = [tag]
    for c in ch:
        parts.append(_r(c) if isinstance(c, dict) else _demangle(c))
    return '(' + ' '.join(p for p in parts if p != '') + ')'


def tool_nif_render(args):
    nif_file = args.get('nif_file')
    needle = args.get('needle')
    if not nif_file:
        return {'error': 'missing required arg: nif_file'}
    if not os.path.isfile(nif_file):
        return {'error': 'no such file: %s' % nif_file}
    try:
        text = _read_nif(nif_file)
    except (IOError, OSError) as e:
        return {'error': str(e)}

    terse = resolve_terse(args)
    max_lines = 10 if terse else 15
    forms = nif_parse_forms(text)

    targets = []  # (start, end, tag, name)
    if needle:
        needle_l = needle.lower()
        seen = set()
        for f in forms:
            toks = f['tokens']
            if not toks:
                continue
            tag = _base_tag(toks[0])
            name = _clean_name(toks[1]) if len(toks) > 1 else ''
            head = ' '.join(toks[:2]).lower()
            if tag.lower() == needle_l or needle_l in head:
                key = (f['start'], f['end'])
                if key in seen:
                    continue
                seen.add(key)
                targets.append((f['start'], f['end'], tag, name))
    else:
        # render each top-level node under the stmts container.
        stmts = None
        for f in forms:
            if f['tokens'] and _base_tag(f['tokens'][0]) == 'stmts':
                stmts = f
                break
        if stmts is not None:
            cd = stmts['depth'] + 1
            for f in forms:
                if f['depth'] == cd and f['start'] >= stmts['start'] and \
                        f['end'] <= stmts['end'] and f['tokens']:
                    tag = _base_tag(f['tokens'][0])
                    name = _clean_name(f['tokens'][1]) if len(f['tokens']) > 1 \
                        else ''
                    targets.append((f['start'], f['end'], tag, name))

    rendered = []
    cap = 40
    for start, end, tag, name in targets[:cap]:
        node, _ = _nif_build(_nif_tokenize(text[start:end]), 0)
        pseudo = _truncate_snippet(_render_node(node), max_lines=max_lines,
                                   max_chars=1500)
        item = {'tag': tag, 'pseudo_nim': pseudo}
        if name and not terse:
            item['name'] = name
        elif name:
            item['name'] = name
        rendered.append(item)
    return {'rendered': rendered}


# --------------------------------------------------------------------------
# Tool: explain_failure
# --------------------------------------------------------------------------

def _nimony_culprit(cwd, basename, err_line, err_col, max_lines):
    """Smallest NIF node spanning (err_line, err_col) in the module's phase
    artifact, rendered as a raw snippet. Prefers the .s.nif, falls back .p.nif."""
    nif_path = None
    for phase in ('s', 'p'):
        cand = _find_module_nif(cwd, basename, phase)
        if cand is not None:
            nif_path = cand
            break
    if nif_path is None:
        return None
    try:
        text = _read_nif(nif_path)
    except (IOError, OSError):
        return None
    forms = nif_forms_with_pos(text)
    best = None  # (span, start, end)
    for f in forms:
        src = f.get('src')
        if not src or src[1] != err_line:
            continue
        col = src[2]
        if col > err_col:
            continue
        span = f['end'] - f['start']
        cand = (err_col - col, span, f['start'], f['end'])
        if best is None or cand < best:
            best = cand
    if best is None:
        # relax: any form on the error line
        for f in forms:
            src = f.get('src')
            if not src or src[1] != err_line:
                continue
            span = f['end'] - f['start']
            cand = (0, span, f['start'], f['end'])
            if best is None or cand < best:
                best = cand
    if best is None:
        return None
    snippet = _truncate_snippet(text[best[2]:best[3]], max_lines=max_lines,
                                max_chars=1500)
    return {'artifact': os.path.basename(nif_path), 'snippet': snippet}


def _nim_culprit(file_path, err_line, context=3):
    try:
        with open(file_path, 'r', errors='replace') as fh:
            lines = fh.read().splitlines()
    except (IOError, OSError):
        return None
    lo = max(0, err_line - 1 - context)
    hi = min(len(lines), err_line + context)
    out = []
    for idx in range(lo, hi):
        marker = '>' if (idx + 1) == err_line else ' '
        out.append('%s %d: %s' % (marker, idx + 1, lines[idx]))
    return '\n'.join(out)


def tool_explain_failure(args):
    file_path = args.get('file')
    if not file_path:
        return {'error': 'missing required arg: file'}
    toolchain = resolve_toolchain(file_path, args.get('toolchain', 'auto'))
    terse = resolve_terse(args)

    comp = tool_compile({'file': file_path, 'toolchain': toolchain,
                         'extra_args': args.get('extra_args'), 'terse': False})
    if 'error' in comp:
        return comp
    diags = comp.get('diagnostics', [])
    errors = [d for d in diags if d['severity'] == 'Error']
    ok = comp.get('ok', False)

    if ok or not errors:
        verdict = 'OK (%s): compiles clean' % toolchain
        result = {'ok': True, 'toolchain': toolchain, 'verdict': verdict,
                  'diagnostics': []}
        return result

    first = errors[0]
    vlines = ['FAIL (%s): %d error(s)' % (toolchain, len(errors))]
    for d in errors[:4]:
        vlines.append(diag_to_str(d))
    verdict = '\n'.join(vlines[:5])

    culprit = None
    max_lines = 10 if terse else 15
    if toolchain == 'nimony':
        cwd = os.path.dirname(os.path.abspath(file_path)) or '.'
        culprit = _nimony_culprit(cwd, os.path.basename(file_path),
                                  first['line'], first['col'], max_lines)
    else:
        snippet = _nim_culprit(file_path, first['line'])
        if snippet is not None:
            culprit = {'source': snippet}

    if terse:
        diag_out = [diag_to_str(d) for d in errors]
    else:
        diag_out = errors
    result = {'ok': False, 'toolchain': toolchain, 'verdict': verdict,
              'diagnostics': diag_out}
    if culprit is not None:
        result['culprit'] = culprit
    return result


# --------------------------------------------------------------------------
# Tool: phase_report
# --------------------------------------------------------------------------

_PHASE_ORDER = ['p', 's', 'x', 'dce', 'c']
_PHASE_SKIP = ('deps', 'idx', 'build', 'final')


def _phase_summary(path, max_tags=4):
    try:
        text = _read_nif(path)
    except (IOError, OSError) as e:
        return 'unreadable: %s' % e
    size = len(text)
    forms = nif_parse_forms(text)
    counts = {}
    for f in forms:
        if not f['tokens']:
            continue
        tag = _base_tag(f['tokens'][0])
        if not tag or tag.startswith('.'):
            continue
        counts[tag] = counts.get(tag, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:max_tags]
    tagstr = ', '.join('%s=%d' % (t, c) for t, c in top)
    return '%d bytes, %d nodes; top: %s' % (size, len(forms), tagstr)


def tool_phase_report(args):
    file_path = args.get('file')
    if not file_path:
        return {'error': 'missing required arg: file'}
    toolchain = resolve_toolchain(file_path, args.get('toolchain', 'auto'))

    comp = tool_compile({'file': file_path, 'toolchain': toolchain,
                         'extra_args': args.get('extra_args'), 'terse': False})
    ok = comp.get('ok', False)

    if toolchain != 'nimony':
        return {'ok': ok, 'phases': [],
                'note': 'Nim C backend has no NIF phases'}

    cwd = os.path.dirname(os.path.abspath(file_path)) or '.'
    basename = os.path.basename(file_path)
    # find the module hash prefix from any phase artifact naming basename.
    prefix = None
    for phase in ('p', 's'):
        cand = _find_module_nif(cwd, basename, phase)
        if cand is not None:
            bn = os.path.basename(cand)
            prefix = bn[:bn.index('.' + phase + '.nif')]
            break

    phases = []
    if prefix is not None:
        ncache = os.path.join(cwd, 'nimcache')
        found = {}
        for path in glob.glob(os.path.join(ncache, prefix + '.*.nif')):
            bn = os.path.basename(path)
            mid = bn[len(prefix) + 1:-4]  # strip 'prefix.' and '.nif'
            if '.' in mid:  # multi-segment like s.idx, p.deps
                continue
            if mid in _PHASE_SKIP:
                continue
            found[mid] = path
        ordered = [p for p in _PHASE_ORDER if p in found]
        for p in sorted(found):
            if p not in ordered:
                ordered.append(p)
        for p in ordered:
            phases.append({'phase': p,
                           'artifact': os.path.basename(found[p]),
                           'summary': _phase_summary(found[p])})
    return {'ok': ok, 'phases': phases}


# --------------------------------------------------------------------------
# Tool: shrink (delta-debug)
# --------------------------------------------------------------------------

def _compile_first_error(work_dir, tmp_name, content, toolchain):
    """Write content to work_dir/tmp_name, compile, return first Error message
    (str) or None if no Error."""
    tmp_path = os.path.join(work_dir, tmp_name)
    try:
        with open(tmp_path, 'w') as fh:
            fh.write(content)
    except (IOError, OSError):
        return None
    try:
        if toolchain == 'nimony':
            cmd = [nimony_bin('nimony'), 'c', tmp_name]
        else:
            cmd = [nim_bin('nim'), 'check', '--hints:off', '--colors:off',
                   tmp_name]
        rc, out, timed_out = run(cmd, cwd=work_dir, timeout=60)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    for d in parse_diagnostics(out):
        if d['severity'] == 'Error':
            return d['message']
    return None


def tool_shrink(args):
    file_path = args.get('file')
    if not file_path:
        return {'error': 'missing required arg: file'}
    if not os.path.isfile(file_path):
        return {'error': 'no such file: %s' % file_path}
    toolchain = resolve_toolchain(file_path, args.get('toolchain', 'auto'))
    work_dir = os.path.dirname(os.path.abspath(file_path)) or '.'
    ext = os.path.splitext(file_path)[1] or '.nim'
    # Must be a valid Nim module identifier (no leading dot): Nim rejects
    # dotfile module names before it ever type-checks, masking the real error.
    tmp_name = 'shrink_%d%s' % (os.getpid(), ext)

    try:
        with open(file_path, 'r', errors='replace') as fh:
            original = fh.read()
    except (IOError, OSError) as e:
        return {'error': str(e)}
    lines = original.splitlines()
    orig_count = len(lines)

    target = _compile_first_error(work_dir, tmp_name,
                                  '\n'.join(lines) + '\n', toolchain)
    if target is None:
        return {'error': 'file does not fail with an Error (nothing to shrink)',
                'toolchain': toolchain, 'original_lines': orig_count}

    # Budget: bound compiles and wall time.
    max_compiles = 200
    deadline = time.time() + 90.0
    compiles = [0]

    def still_fails(cand_lines):
        if compiles[0] >= max_compiles or time.time() > deadline:
            return False
        compiles[0] += 1
        msg = _compile_first_error(work_dir, tmp_name,
                                   '\n'.join(cand_lines) + '\n', toolchain)
        return msg == target

    units = list(lines)
    # Coarse pass: try dropping shrinking chunk sizes; then fine per-line.
    chunk = max(1, len(units) // 2)
    while chunk >= 1:
        i = 0
        while i < len(units):
            if compiles[0] >= max_compiles or time.time() > deadline:
                break
            cand = units[:i] + units[i + chunk:]
            if cand and still_fails(cand):
                units = cand
            else:
                i += chunk
        if compiles[0] >= max_compiles or time.time() > deadline:
            break
        if chunk == 1:
            break
        chunk = chunk // 2

    minimal = '\n'.join(units) + '\n' if units else ''
    return {
        'toolchain': toolchain,
        'original_lines': orig_count,
        'minimal_lines': len(units),
        'minimal_source': minimal,
        'kept_error': target,
    }


# --------------------------------------------------------------------------
# Tool: api  (typed API of a module or third-party package)
# --------------------------------------------------------------------------

# Strip trailing pragma blocks / bodies so a signature stays one compact line.
_PRAGMA_RE = re.compile(r'\s*\{\.[^}]*\.\}')


def _clean_sig(code):
    """Collapse a jsondoc `code` blob into one compact signature line."""
    if not code:
        return ''
    # Keep only up to the '=' that starts a body (if any), first line only.
    line = code.replace('\n', ' ')
    line = _PRAGMA_RE.sub('', line)
    eq = line.find(' = ')
    if eq != -1:
        line = line[:eq]
    return ' '.join(line.split())


def _nimble_pkg_dir(pkg):
    """Resolve an installed nimble package to its source dir, or None."""
    rc, out, timed = run([nim_bin('nimble'), 'path', pkg], timeout=30)
    if rc != 0 or timed:
        return None
    for ln in reversed(out.splitlines()):
        ln = ln.strip()
        if ln and os.path.isdir(ln):
            return ln
    return None


def _resolve_module(module, toolchain):
    """Map `module` (a path or a bare package/module name) to a source file.
    Returns (path, error). Prefers an existing file; else a nimble package's
    `<pkg>.nim`; else a stdlib `std/<name>` module under the Nim lib."""
    if os.path.isfile(module):
        return module, None
    # bare package name -> <dir>/<pkg>.nim
    base = module.split('/')[-1]
    pkgdir = _nimble_pkg_dir(base)
    if pkgdir:
        cand = os.path.join(pkgdir, base + '.nim')
        if os.path.isfile(cand):
            return cand, None
    # stdlib std/<name>
    libname = module[4:] if module.startswith('std/') else module
    for sub in ('pure', 'std', '', 'core'):
        cand = _home('Nim', 'lib', sub, libname + '.nim')
        if os.path.isfile(cand):
            return cand, None
    return None, 'could not resolve module %r (not a file, nimble pkg, or ' \
                 'stdlib module)' % module


def tool_api(args):
    module = args.get('module')
    if not module:
        return {'error': 'missing required arg: module'}
    toolchain = args.get('toolchain', 'auto')
    needle = args.get('needle')
    terse = resolve_terse(args)

    # Nimony (or a raw .nif): the typed API IS the compiled artifact.
    if module.endswith('.nif') or toolchain == 'nimony':
        if module.endswith('.nif') and os.path.isfile(module):
            r = tool_nif_render({'nif_file': module, 'needle': needle,
                                 'terse': terse})
            r['toolchain'] = 'nimony'
            r['module'] = module
            return r
        return {'toolchain': 'nimony', 'module': module,
                'note': 'Nimony typed API = the compiled .s.nif. Compile the '
                        'module, then call nif_render/nif_outline on its '
                        'nimcache artifact.'}

    # Nim: jsondoc gives a typed API for any module, incl. nimble packages.
    src, err = _resolve_module(module, toolchain)
    if err:
        return {'error': err}
    out_json = os.path.join(
        os.path.dirname(os.path.abspath(src)) or '.',
        '.api_%d.json' % os.getpid())
    cmd = [nim_bin('nim'), 'jsondoc', '--hints:off', '--colors:off',
           '-o:' + out_json, src]
    rc, out, timed = run(cmd, cwd=os.path.dirname(src) or None, timeout=120)
    entries = []
    try:
        if os.path.isfile(out_json):
            with open(out_json, 'r', errors='replace') as fh:
                doc = json.load(fh)
            entries = doc.get('entries', []) if isinstance(doc, dict) else []
    except (ValueError, IOError, OSError):
        entries = []
    finally:
        try:
            os.remove(out_json)
        except OSError:
            pass

    if not entries:
        return {'error': 'jsondoc produced no entries for %s' % src,
                'toolchain': 'nim', 'module': module}

    needle_l = needle.lower() if needle else None
    items = []
    for e in entries:
        name = e.get('name') or ''
        if needle_l and needle_l not in name.lower():
            continue
        kind = e.get('type') or ''
        if kind.startswith('sk'):
            kind = kind[2:].lower()
        sig = _clean_sig(e.get('code') or '')
        if terse:
            items.append(sig or ('%s %s' % (kind, name)))
        else:
            items.append({'name': name, 'kind': kind, 'sig': sig})

    return {'toolchain': 'nim', 'module': module, 'source': src,
            'api': items}


# --------------------------------------------------------------------------
# Tool: symbols  (project-wide symbol search by NAME)
# --------------------------------------------------------------------------

# Directories that never hold hand-written project sources worth indexing.
_SKIP_DIRS = set(['nimcache', '.git', 'htmldocs', 'nimblecache',
                  '__pycache__'])
_MAX_FILES = 4000
_MAX_HITS = 400


def _iter_nim_files(root):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith('.nimble')]
        for fn in filenames:
            if fn.endswith('.nim') or fn.endswith('.nims'):
                count += 1
                if count > _MAX_FILES:
                    return
                yield os.path.join(dirpath, fn)


def tool_symbols(args):
    name = args.get('name')
    if not name:
        return {'error': 'missing required arg: name'}
    root = args.get('root') or '.'
    if not os.path.isdir(root):
        root = os.path.dirname(os.path.abspath(root)) or '.'
    kind_filter = args.get('kind')
    want_uses = bool(args.get('uses'))
    terse = resolve_terse(args)

    name_l = name.lower()
    use_re = re.compile(r'(?<![\w`])' + re.escape(name) + r'(?![\w`])')

    defs = []
    uses = []
    truncated = False
    for path in _iter_nim_files(root):
        if len(defs) >= _MAX_HITS:
            truncated = True
            break
        try:
            with open(path, 'r', errors='replace') as fh:
                lines = fh.readlines()
        except (IOError, OSError):
            continue
        rel = os.path.relpath(path, root)
        for idx, raw in enumerate(lines, start=1):
            m = OUTLINE_RE.match(raw)
            if m is not None:
                dname = m.group('name').strip('`')
                if name_l in dname.lower():
                    if kind_filter and m.group('kind') != kind_filter:
                        pass
                    else:
                        defs.append({'name': dname, 'kind': m.group('kind'),
                                     'file': rel, 'line': idx})
            if want_uses and len(uses) < _MAX_HITS and use_re.search(raw):
                uses.append({'file': rel, 'line': idx})

    if terse:
        d = ['%s:%s %s %s' % (x['file'], x['line'], x['kind'], x['name'])
             for x in defs]
        result = {'defs': d}
        if want_uses:
            result['uses'] = ['%s:%s' % (u['file'], u['line']) for u in uses]
    else:
        result = {'defs': defs}
        if want_uses:
            result['uses'] = uses
    result['root'] = root
    if truncated:
        result['truncated'] = True
    return result


# --------------------------------------------------------------------------
# Tool registry / schemas
# --------------------------------------------------------------------------

TOOLCHAIN_ENUM = {'type': 'string', 'enum': ['auto', 'nim', 'nimony'],
                  'default': 'auto',
                  'description': 'Toolchain: auto-detect (default) or force.'}

TERSE_PROP = {'type': 'boolean',
              'description': 'Aggressive token-saving output. Defaults to the '
                             'truthy env var NIMLANG_AGGRESSIVE.'}

TOOLS = [
    {
        'name': 'compile',
        'description': 'Type-check/compile a Nim or Nimony file and return '
                       'structured diagnostics (no verbose noise).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file': {'type': 'string', 'description': 'Path to .nim file.'},
                'toolchain': TOOLCHAIN_ENUM,
                'extra_args': {'type': 'array', 'items': {'type': 'string'},
                               'description': 'Extra compiler args.'},
                'terse': TERSE_PROP,
            },
            'required': ['file'],
        },
        'handler': tool_compile,
    },
    {
        'name': 'outline',
        'description': 'List top-level symbols (procs/types/vars...) of a Nim '
                       'or Nimony source file.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file': {'type': 'string'},
                'toolchain': TOOLCHAIN_ENUM,
                'terse': TERSE_PROP,
            },
            'required': ['file'],
        },
        'handler': tool_outline,
    },
    {
        'name': 'nif_outline',
        'description': 'Top-level tag/name nodes of a Nimony NIF artifact '
                       '(no bodies). Use instead of reading whole .nif files.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'nif_file': {'type': 'string',
                             'description': 'Path to a .nif file.'},
                'terse': TERSE_PROP,
            },
            'required': ['nif_file'],
        },
        'handler': tool_nif_outline,
    },
    {
        'name': 'nif_query',
        'description': 'Return only the NIF subtrees whose head tag or symbol '
                       'matches a needle (each snippet truncated).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'nif_file': {'type': 'string'},
                'needle': {'type': 'string',
                           'description': 'Tag or symbol substring to find.'},
                'terse': TERSE_PROP,
            },
            'required': ['nif_file', 'needle'],
        },
        'handler': tool_nif_query,
    },
    {
        'name': 'nif_diff',
        'description': 'Compact structural/line diff between two NIF (or text) '
                       'files, collapsing unchanged regions.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file_a': {'type': 'string'},
                'file_b': {'type': 'string'},
            },
            'required': ['file_a', 'file_b'],
        },
        'handler': tool_nif_diff,
    },
    {
        'name': 'defs_uses',
        'description': 'Definition + usages of the symbol at file:line:col. '
                       'Nim via nimsuggest, Nimony via nimsem idetools.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file': {'type': 'string'},
                'line': {'type': 'integer'},
                'col': {'type': 'integer'},
                'toolchain': TOOLCHAIN_ENUM,
            },
            'required': ['file', 'line', 'col'],
        },
        'handler': tool_defs_uses,
    },
    {
        'name': 'explain_failure',
        'description': 'Compile a Nim/Nimony file and, on failure, return a '
                       'short verdict plus the culprit (Nim: +/-3 source lines; '
                       'Nimony: smallest NIF node spanning the error). One call '
                       'replaces compile->list->outline->query.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file': {'type': 'string'},
                'toolchain': TOOLCHAIN_ENUM,
                'extra_args': {'type': 'array', 'items': {'type': 'string'}},
                'terse': TERSE_PROP,
            },
            'required': ['file'],
        },
        'handler': tool_explain_failure,
    },
    {
        'name': 'phase_report',
        'description': 'Compile with Nimony and give a 1-line summary (top tag '
                       'counts + size) of each nimcache phase artifact (p, s, '
                       '...), never raw NIF. Nim has no NIF phases.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file': {'type': 'string'},
                'toolchain': TOOLCHAIN_ENUM,
                'extra_args': {'type': 'array', 'items': {'type': 'string'}},
                'terse': TERSE_PROP,
            },
            'required': ['file'],
        },
        'handler': tool_phase_report,
    },
    {
        'name': 'nif_render',
        'description': 'Render matching Nimony NIF node(s) as compact '
                       'pseudo-Nim (demangled symbols), ~10x smaller than raw '
                       'NIF. Nimony artifacts only.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'nif_file': {'type': 'string'},
                'needle': {'type': 'string',
                           'description': 'Optional tag/symbol substring.'},
                'terse': TERSE_PROP,
            },
            'required': ['nif_file'],
        },
        'handler': tool_nif_render,
    },
    {
        'name': 'shrink',
        'description': 'Delta-debug a failing Nim/Nimony file to a minimal '
                       'still-failing repro that preserves the first Error.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'file': {'type': 'string'},
                'toolchain': TOOLCHAIN_ENUM,
                'terse': TERSE_PROP,
            },
            'required': ['file'],
        },
        'handler': tool_shrink,
    },
    {
        'name': 'api',
        'description': 'Typed public API of a module or third-party package. '
                       'Nim: nim jsondoc (resolves nimble packages & stdlib) -> '
                       'compact signatures. Nimony/.nif: render the compiled '
                       'artifact. Avoids reading dependency source.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'module': {'type': 'string',
                           'description': 'A .nim path, a nimble package name '
                                          '(e.g. "chroma"), a std module '
                                          '("std/tables"), or a .nif file.'},
                'toolchain': TOOLCHAIN_ENUM,
                'needle': {'type': 'string',
                           'description': 'Only return symbols whose name '
                                          'contains this substring.'},
                'terse': TERSE_PROP,
            },
            'required': ['module'],
        },
        'handler': tool_api,
    },
    {
        'name': 'symbols',
        'description': 'Find a symbol across the whole project by NAME '
                       '(substring). Returns definitions (file:line:kind) and '
                       'optionally usages. Replaces raw grep for navigation.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string',
                         'description': 'Symbol name or substring to find.'},
                'root': {'type': 'string',
                         'description': 'Directory to search (default cwd).'},
                'kind': {'type': 'string',
                         'description': 'Optional filter: proc/type/const/...'},
                'uses': {'type': 'boolean',
                         'description': 'Also return usage sites (default '
                                        'false).'},
                'terse': TERSE_PROP,
            },
            'required': ['name'],
        },
        'handler': tool_symbols,
    },
]

TOOLS_BY_NAME = {}
for _t in TOOLS:
    TOOLS_BY_NAME[_t['name']] = _t


def tools_list_payload():
    out = []
    for t in TOOLS:
        out.append({
            'name': t['name'],
            'description': t['description'],
            'inputSchema': t['inputSchema'],
        })
    return out


# --------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# --------------------------------------------------------------------------

PROTOCOL_VERSION = '2024-11-05'
SERVER_INFO = {'name': 'nimlang', 'version': '0.1.0'}


def make_response(req_id, result):
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def make_error(req_id, code, message):
    return {'jsonrpc': '2.0', 'id': req_id,
            'error': {'code': code, 'message': message}}


def handle_tools_call(params):
    name = params.get('name')
    arguments = params.get('arguments') or {}
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {'content': [{'type': 'text',
                             'text': json.dumps({'error': 'unknown tool: %s'
                                                 % name})}],
                'isError': True}
    try:
        result = tool['handler'](arguments)
    except Exception as e:
        result = {'error': 'tool %s crashed: %s' % (name, e)}
    is_error = isinstance(result, dict) and 'error' in result
    text = json.dumps(result, separators=(',', ':'), ensure_ascii=False)
    payload = {'content': [{'type': 'text', 'text': text}]}
    if is_error:
        payload['isError'] = True
    return payload


def dispatch(msg):
    """Return a response dict, or None for notifications."""
    method = msg.get('method')
    req_id = msg.get('id')
    params = msg.get('params') or {}

    if method == 'initialize':
        return make_response(req_id, {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {'tools': {}},
            'serverInfo': SERVER_INFO,
        })
    if method == 'notifications/initialized' or method == 'initialized':
        return None
    if method == 'ping':
        return make_response(req_id, {})
    if method == 'tools/list':
        return make_response(req_id, {'tools': tools_list_payload()})
    if method == 'tools/call':
        return make_response(req_id, handle_tools_call(params))

    if req_id is None:
        return None  # unknown notification, ignore
    return make_error(req_id, -32601, 'method not found: %s' % method)


def main():
    stdin = sys.stdin
    stdout = sys.stdout
    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = make_error(None, -32700, 'parse error')
            stdout.write(json.dumps(resp) + '\n')
            stdout.flush()
            continue
        # Support batch arrays defensively.
        if isinstance(msg, list):
            for sub in msg:
                resp = dispatch(sub)
                if resp is not None:
                    stdout.write(json.dumps(resp) + '\n')
            stdout.flush()
            continue
        resp = dispatch(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + '\n')
            stdout.flush()


if __name__ == '__main__':
    main()
