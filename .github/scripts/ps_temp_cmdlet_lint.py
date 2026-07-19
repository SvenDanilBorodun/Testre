#!/usr/bin/env python3
r"""ps-temp-cmdlet-lint — catch a terminating PSArgumentException from a TEMP path.

On a Windows account whose username contains a dot (e.g. "sven.d"), $env:TEMP
resolves to an 8.3 short path with a tilde (C:\Users\SVEN~1.D\AppData\...).
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

TWO LEXING MODES, and the split is load-bearing (_ps_lex.preprocess):
  * "is a cmdlet actually EXECUTED here" + try-brace tracking run on the
    STRING-BLANKED line. Without that,
    `Write-Host "Bitte loeschen: Remove-Item $env:TEMP\edubotics.log"` was
    reported as an unguarded cmdlet, and a `}` inside prose
    (`Write-Host "closing brace: }"`) popped a real, still-open try-brace and
    turned a guarded cmdlet into a finding.
  * "…on a TEMP-derived path" runs on the COMMENT-STRIPPED line with strings
    KEPT, because the path legitimately lives inside the quotes
    (`Remove-Item "$env:TEMP\x"`).
Comment removal is string-aware in both modes, which is what fixes the third
misfire: `$retries = 3   # one more try` used to leave a dangling `try` in the
tail scan, marking the NEXT `{` as a try-body and silencing every finding
inside it — the same "prose cannot suppress a finding" bug 8c36c42 fixed in
ps_readiness_retry_lint.

Escape hatch: append `# ps-temp-cmdlet-lint: allow` to the flagged line.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _ps_lex import consume_braces, preprocess  # noqa: E402

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
# A dangling `try` at end of a code segment (its `{` opens on the next line).
TRAILING_TRY = re.compile(r'\btry\s*$', re.I)  # PS is case-insensitive: `Try {` is legal
ALLOW_LINE = "ps-temp-cmdlet-lint: allow"


def scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    raw_lines = text.splitlines()
    code_lines = preprocess(text)                        # strings blanked
    val_lines = preprocess(text, blank_strings=False)    # strings kept
    temp_vars: set[str] = set()
    brace_is_try: list[bool] = []
    pending_try = False
    hits: list[tuple[int, str]] = []

    for idx, code in enumerate(code_lines):
        raw = raw_lines[idx]
        vals = val_lines[idx]

        cm = CMDLETS.search(code)
        if cm and ALLOW_LINE not in raw:
            touches_temp = bool(ENV_TEMP.search(vals)) or any(
                re.search(r'\$' + re.escape(v) + r'\b', vals, re.I)
                for v in temp_vars
            )
            if touches_temp:
                # Guarded-ness is decided at the cmdlet's position: process the
                # braces opened EARLIER on this same line (a `try { cmdlet }`
                # one-liner opens its try-brace before the cmdlet) on top of the
                # stack carried from prior lines. Use copies so the real state is
                # only advanced once, for the full line, below.
                local_stack = list(brace_is_try)
                consume_braces(code[:cm.start()], local_stack, pending_try,
                               TRAILING_TRY)
                if not any(local_stack):
                    hits.append((idx + 1, raw.strip()))

        m = ASSIGN_FROM_TEMP.search(vals)
        if m:
            temp_vars.add(m.group(1))
        pending_try = consume_braces(code, brace_is_try, pending_try,
                                     TRAILING_TRY)

    return hits


def main() -> int:
    total = 0
    scanned = 0
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.ps1")):
            scanned += 1
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
    # A rename of the scan dir (or a wrong PS_LINT_ROOT) used to make every
    # guard print OK having read ZERO files — false confidence, not a pass.
    if scanned == 0:
        print(f"::error::ps-temp-cmdlet-lint scanned 0 .ps1 files under "
              f"{ROOT}/{{{','.join(SCAN_DIRS)}}} — the scan root moved or "
              f"PS_LINT_ROOT is wrong. Refusing to report a green pass.")
        return 1
    if total:
        print(f"\nps-temp-cmdlet-lint FAILED: {total} unguarded temp cmdlet(s).")
        return 1
    print(f"ps-temp-cmdlet-lint OK ({scanned} file(s) scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
