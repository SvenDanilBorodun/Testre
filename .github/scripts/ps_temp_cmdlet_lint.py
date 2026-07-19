#!/usr/bin/env python3
"""ps-temp-cmdlet-lint — catch a terminating PSArgumentException from a TEMP path.

On a Windows account whose username contains a dot (e.g. "sven.d"), $env:TEMP
resolves to an 8.3 short path with a tilde (C:\\Users\\SVEN~1.D\\AppData\\...).
Binding such a path to a file cmdlet's -LiteralPath/-Path raises a *terminating*
System.Management.Automation.PSArgumentException that -ErrorAction
SilentlyContinue does NOT suppress. So a file cmdlet (Remove-Item / Set-Content /
Test-Path / …) that operates on a $env:TEMP-derived path OUTSIDE a try/catch can
hard-abort an installer script mid-run even though EAP is 'Continue' — exactly
the F2 class of failure.

The LOAD-BEARING guard is (a): wrap the cmdlet in `try { } catch { }` — that
holds regardless of what the temp path looks like. (b) building the path from
[System.IO.Path]::GetTempPath() / GetTempFileName() is defense-in-depth, NOT a
substitute: both read the same TMP env var and do NOT expand an 8.3 profile
path back to its long form (GetTempFileName's added value is that it
pre-creates the file, so a later Get-Content/Remove-Item can't hit a missing
target). See wsl_docker_ready.ps1, import_edubotics_wsl.ps1 for both layers
combined. Until the dotted-username rig test pins down the exact trigger of
the original PSArgumentException, only the try/catch is treated as proven.

This guard scans every *.ps1 under robotis_ai_setup/ and fails any file cmdlet
that touches a $env:TEMP/$env:TMP-derived path (the env var directly on the line,
or a variable previously assigned from it) and is NOT lexically inside a
`try { }` block.

Escape hatch: append `# ps-temp-cmdlet-lint: allow` to the flagged line.
"""
from __future__ import annotations

import os
import pathlib
import re

ROOT = pathlib.Path(os.environ.get("PS_LINT_ROOT") or
                    pathlib.Path(__file__).resolve().parents[2])
SCAN_DIRS = ["robotis_ai_setup"]

# File cmdlets whose path binding throws on an 8.3/tilde temp path.
CMDLETS = re.compile(
    r'\b(Remove-Item|New-Item|Set-Content|Add-Content|Get-Content|'
    r'Out-File|Move-Item|Copy-Item|Test-Path|Get-FileHash)\b',
    re.I,
)
# $env:TEMP / $env:TMP referenced directly on a line.
ENV_TEMP = re.compile(r'\$env:(TEMP|TMP)\b', re.I)
# A variable assignment that pulls its value from $env:TEMP/$env:TMP.
# Matches param defaults ([string]$LogPath = "$env:TEMP\...") and plain
# assignments ($x = Join-Path $env:TEMP ...). Deliberately does NOT match a
# comparison ($x -eq $env:TEMP) because there is no '=' immediately after $x.
ASSIGN_FROM_TEMP = re.compile(r'\$(\w+)\s*=\s*[^=].*\$env:(TEMP|TMP)\b', re.I)
# A dangling `try` at end of a line (its `{` opens on the next line).
TRAILING_TRY = re.compile(r'\btry\s*$', re.I)  # PS is case-insensitive: `Try {` is legal
ALLOW_LINE = "ps-temp-cmdlet-lint: allow"


def _consume_braces(line: str, stack: list[bool], pending_try: bool) -> bool:
    """Update the brace stack for one line, marking each `{` as a try-brace or
    not. `stack[k]` is True iff the k-th currently-open brace opened a `try`
    body. Returns the new `pending_try` flag (a `try` whose `{` is on the next
    line). Full-line comments are handled by the caller."""
    seg_start = 0
    for i, c in enumerate(line):
        if c == '{':
            seg = line[seg_start:i]
            is_try = pending_try or bool(TRAILING_TRY.search(seg))
            stack.append(is_try)
            pending_try = False
            seg_start = i + 1
        elif c == '}':
            if stack:
                stack.pop()
            seg_start = i + 1
    tail = line[seg_start:]
    return bool(TRAILING_TRY.search(tail))


def scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    temp_vars: set[str] = set()
    brace_is_try: list[bool] = []
    pending_try = False
    hits: list[tuple[int, str]] = []

    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        is_comment = stripped.startswith('#')

        cm = CMDLETS.search(raw) if not is_comment else None
        if cm and ALLOW_LINE not in raw:
            touches_temp = bool(ENV_TEMP.search(raw)) or any(
                re.search(r'\$' + re.escape(v) + r'\b', raw, re.I)
                for v in temp_vars
            )
            if touches_temp:
                # Guarded-ness is decided at the cmdlet's position: process the
                # braces opened EARLIER on this same line (a `try { cmdlet }`
                # one-liner opens its try-brace before the cmdlet) on top of the
                # stack carried from prior lines. Use copies so the real state is
                # only advanced once, for the full line, below.
                local_stack = list(brace_is_try)
                _consume_braces(raw[:cm.start()], local_stack, pending_try)
                if not any(local_stack):
                    hits.append((lineno, stripped))

        if not is_comment:
            m = ASSIGN_FROM_TEMP.search(raw)
            if m:
                temp_vars.add(m.group(1))
            pending_try = _consume_braces(raw, brace_is_try, pending_try)

    return hits


def main() -> int:
    total = 0
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.ps1")):
            rel = p.relative_to(ROOT).as_posix()
            for lineno, snippet in scan_file(p):
                total += 1
                print(
                    f"::error file={rel},line={lineno}::file cmdlet on a "
                    f"$env:TEMP-derived path outside try/catch — an 8.3/tilde "
                    f"temp path throws a terminating PSArgumentException that "
                    f"-ErrorAction cannot suppress. Wrap the call in try/catch "
                    f"(the load-bearing guard); building the path from "
                    f"[System.IO.Path]::GetTempPath() is additional hygiene, "
                    f"not a substitute. Offending: {snippet}"
                )
    if total:
        print(f"\nps-temp-cmdlet-lint FAILED: {total} unguarded temp cmdlet(s).")
        return 1
    print("ps-temp-cmdlet-lint OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
