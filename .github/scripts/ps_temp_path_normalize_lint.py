#!/usr/bin/env python3
"""ps-temp-path-normalize-lint — advisory: flag RAW $env:TEMP path construction.

$env:TEMP / $env:TMP are NOT guaranteed to be normalized long paths. On a
Windows account whose username contains a dot or non-ASCII characters (e.g.
"sven.d", "müller"), the profile temp dir collapses to an 8.3 SHORT path with a
tilde (C:\\Users\\SVEN~1.D\\AppData\\Local\\Temp). Building a path by string-
concatenating onto that raw value (`"$env:TEMP\\x"`, `Join-Path $env:TEMP ...`)
carries the tilde/short form forward, and some downstream cmdlet bindings then
raise a *terminating* PSArgumentException that -ErrorAction cannot suppress
(the F2 class of failure — see ps-temp-cmdlet-lint). The PREFERRED source of a
temp path is [System.IO.Path]::GetTempPath() / GetTempFileName() — one
consistent, well-formed absolute path per process, and GetTempFileName
pre-creates the file so later reads/deletes can't hit a missing target. NOTE:
both read the same TMP env var and do NOT expand an 8.3 profile path back to
its long form — the terminating-exception guard is the try/catch that
ps-temp-cmdlet-lint enforces; this advisory only steers path CONSTRUCTION
toward the consistent source.

This guard scans every *.ps1 under robotis_ai_setup/ and reports any RAW
$env:TEMP/$env:TMP path *construction* (env var immediately followed by a path
separator, or fed to Join-Path) that is NOT normalized via
GetTempPath()/GetFullPath()/GetTempFileName()/Resolve-Path in the SAME line.

This is ADVISORY ONLY: it emits ::warning and always EXITS 0. Param-default
sites (e.g. `[string]$LogPath = "$env:TEMP\\edubotics.log"`) are an accepted,
low-risk pattern (the caller usually overrides the default, and Start-Transcript
tolerates it) and must never fail the build — they are surfaced so a maintainer
can normalize them opportunistically.

Escape hatch: append `# ps-temp-path-normalize-lint: allow` to the flagged line.
"""
from __future__ import annotations

import os
import pathlib
import re

ROOT = pathlib.Path(os.environ.get("PS_LINT_ROOT") or
                    pathlib.Path(__file__).resolve().parents[2])
SCAN_DIRS = ["robotis_ai_setup"]

# RAW construction: $env:TEMP/$env:TMP immediately followed by a path separator
# (inside a string, e.g. "$env:TEMP\x"), OR fed as an argument to Join-Path.
# A bare comparison ($x -eq $env:TEMP) has no separator/Join-Path and is ignored.
CONSTRUCT = re.compile(
    r'\$env:(TEMP|TMP)\s*[\\/]'          # "$env:TEMP\..." / "$env:TMP/..."
    r'|Join-Path\s+.*\$env:(TEMP|TMP)\b',  # Join-Path $env:TEMP <child>
    re.I,
)
# In-line normalization that makes the construction safe.
NORMALIZED = re.compile(
    r'GetTempPath\(\)|GetFullPath|GetTempFileName\(\)|Resolve-Path',
    re.I,
)
ALLOW_LINE = "ps-temp-path-normalize-lint: allow"


def scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    hits: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith('#'):
            continue
        if ALLOW_LINE in raw:
            continue
        if not CONSTRUCT.search(raw):
            continue
        if NORMALIZED.search(raw):
            continue
        hits.append((lineno, stripped))
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
                    f"::warning file={rel},line={lineno}::raw $env:TEMP path "
                    f"construction not normalized in-line — an 8.3/tilde/non-ASCII "
                    f"temp path can propagate forward. Prefer "
                    f"[System.IO.Path]::GetTempPath() (one consistent, "
                    f"well-formed source — but still wrap consuming cmdlets in "
                    f"try/catch, see ps-temp-cmdlet-lint); "
                    f"append '# {ALLOW_LINE}' to silence. Line: {snippet}"
                )
    if total:
        print(f"\nps-temp-path-normalize-lint: {total} advisory finding(s) "
              f"(informational — build not failed).")
    else:
        print("ps-temp-path-normalize-lint OK")
    # Advisory only — never fail the build.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
