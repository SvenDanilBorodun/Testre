#!/usr/bin/env python3
r"""_ps_lex — the shared PowerShell "just enough lexing" layer for the ps_* guards.

The four ps_* CI guards are line/regex scanners. Every one of them was, at some
point, fooled by the same two things: prose inside a string literal, and a `#`
that is NOT a comment. Concretely (all reproduced as fixtures in
robotis_ai_setup/tests/test_ps_guard_scripts.py):

  * `$retries = 3   # one more try`  →  a naive tail-scan sees a dangling `try`
    and marks the NEXT `{` as a try-brace, silencing every temp-cmdlet finding
    in that block. (The same bug class 8c36c42 fixed in ps_readiness_retry_lint
    with "prose like 'wait for docker' cannot suppress a finding".)
  * `Write-Host "closing brace in prose: }"`  →  the `}` pops a real, still-open
    try/loop brace, so a genuinely guarded cmdlet gets reported as unguarded.
  * `Write-Host "Bitte loeschen: Remove-Item $env:TEMP\edubotics.log"`  →  a
    cmdlet named inside prose is treated as an executed cmdlet.

`preprocess()` removes both hazards in one pass, preserving line and column
layout so `re.Match.start()` offsets stay usable against the raw line and
`::error file=…,line=…` annotations still point at the right line.

Two modes, and the distinction matters:

  blank_strings=True  (default) — string CONTENT becomes spaces; the quote
      characters survive. Use this for anything that asks "is this CODE?":
      cmdlet/probe/native-command detection and brace tracking.
  blank_strings=False — string content is preserved verbatim; only comments
      are removed. Use this for "is this VALUE referenced anywhere on the
      line?" questions, where the thing you are looking for legitimately lives
      inside a string: `Remove-Item "$env:TEMP\x"` is a real finding even
      though `$env:TEMP` sits inside the quotes.

ps_temp_cmdlet_lint uses BOTH: mode-True to decide "a file cmdlet actually
runs here" and to track try-braces, mode-False to decide "…on a TEMP-derived
path". ps_temp_path_normalize_lint deliberately uses NEITHER — its whole
target population (`[string]$LogPath = "$env:TEMP\edubotics.log"`) lives inside
string literals, so blanking them would silence the advisory completely.
"""
from __future__ import annotations

__all__ = ["preprocess", "consume_braces", "brace_delta"]


def preprocess(text: str, blank_strings: bool = True) -> list[str]:
    """Return `text.splitlines()` with comments removed (and, by default,
    string contents blanked), preserving line count and column positions.

    Handles PS 5.1 quoting: `''` escapes inside single-quoted strings, backtick
    escapes inside double-quoted strings, `#` line comments, and `<# … #>`
    block comments (which may span lines — state carries across the loop).
    Here-strings degrade to ordinary quote handling: their content is still
    processed, which is all the scanners need.
    """
    out: list[str] = []
    state: str | None = None  # None | 'sq' | 'dq' | 'block'
    for line in text.splitlines():
        buf: list[str] = []
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if state == 'sq':
                if c == "'" and i + 1 < n and line[i + 1] == "'":
                    buf.append("''" if not blank_strings else '  ')
                    i += 2
                    continue
                if c == "'":
                    state = None
                    buf.append("'")
                else:
                    buf.append(c if not blank_strings else ' ')
                i += 1
            elif state == 'dq':
                if c == '`' and i + 1 < n:
                    buf.append(line[i:i + 2] if not blank_strings else '  ')
                    i += 2
                    continue
                if c == '"' and i + 1 < n and line[i + 1] == '"':
                    buf.append('""' if not blank_strings else '  ')
                    i += 2
                    continue
                if c == '"':
                    state = None
                    buf.append('"')
                else:
                    buf.append(c if not blank_strings else ' ')
                i += 1
            elif state == 'block':
                if c == '#' and i + 1 < n and line[i + 1] == '>':
                    state = None
                    buf.append('  ')
                    i += 2
                    continue
                buf.append(' ')
                i += 1
            else:
                if c == '<' and i + 1 < n and line[i + 1] == '#':
                    state = 'block'
                    buf.append('  ')
                    i += 2
                    continue
                if c == '#':
                    buf.append(' ' * (n - i))
                    break
                if c == "'":
                    state = 'sq'
                    buf.append("'")
                elif c == '"':
                    state = 'dq'
                    buf.append('"')
                else:
                    buf.append(c)
                i += 1
        out.append(''.join(buf))
    return out


def consume_braces(code: str, stack: list[bool], pending: bool, marker,
                   tail_veto=None) -> bool:
    """Update a brace stack for ONE preprocessed line, tagging each `{`.

    `stack[k]` is True iff the k-th currently-open brace opened a body of
    interest (a loop body for ps_readiness_retry_lint, a `try` body for
    ps_temp_cmdlet_lint). A `{` is tagged True when the code segment before it
    on the same line matches `marker` (`while (...) {`, `try {`), or when the
    previous line ended with a dangling marker (`pending`).

    `marker` is a compiled regex; the caller decides whether it is anchored
    (`\\btry\\s*$` — only a trailing `try` opens a try body) or free-floating
    (`\\b(while|for|foreach|do)\\b` — a loop keyword anywhere in the header).

    `tail_veto` (optional regex) suppresses the returned `pending` when a `}`
    was seen earlier on the SAME line and the trailing segment matches it. That
    is the do-while TERMINATOR: in `} while ($elapsed -lt 3)` the `while` closes
    a loop rather than opening one, and without the veto the next `{` — often
    the `if (…) { … exit 1 }` immediately below — would be mis-tagged as a loop
    body and silence a real finding.

    Returns the new `pending` flag (a marker whose `{` opens on the next line).
    MUTATES `stack` in place — callers that only want to probe a prefix should
    pass a copy.
    """
    seg_start = 0
    saw_close = False
    for i, c in enumerate(code):
        if c == '{':
            seg = code[seg_start:i]
            stack.append(pending or bool(marker.search(seg)))
            pending = False
            seg_start = i + 1
        elif c == '}':
            if stack:
                stack.pop()
            saw_close = True
            seg_start = i + 1
    tail = code[seg_start:]
    if saw_close and tail_veto is not None and tail_veto.search(tail):
        return False
    return bool(marker.search(tail))


def brace_delta(code: str) -> int:
    """Net `{` − `}` on one PREPROCESSED line (strings/comments already gone)."""
    return code.count('{') - code.count('}')
