#!/usr/bin/env python3
"""ps-readiness-retry-lint — catch a single-shot readiness probe that exits hard.

A cold dockerd / WSL2 VM takes several seconds to come up. A readiness probe
(`docker info`, `wsl --status`, `docker version`) that is checked ONCE and then
`exit 1`s on failure races the cold start — the F3 class of failure that made the
installer abort on the very first poll of a freshly-imported distro. The
proven-safe pattern is a bounded retry loop that polls the exit code
(wsl_docker_ready.ps1::Wait-DockerReady, import_edubotics_wsl.ps1's poll).

This guard scans every *.ps1 under robotis_ai_setup/ and flags a readiness probe
that BOTH:
  (a) has an `exit 1` within the next few lines, AND
  (b) has NO enclosing `while`/`for`/`foreach`/`do` loop just above it.

A one-shot diagnostic that merely records a failure (verify_system:
`else { Write-FAIL ... }`; uninstall: `if (...) { exit 0 }`) is not flagged
because it has no hard `exit 1`. For the genuinely one-shot cases that DO exit,
append `# ps-readiness-retry-lint: allow` to the probe line.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("PS_LINT_ROOT") or
                    pathlib.Path(__file__).resolve().parents[2])
SCAN_DIRS = ["robotis_ai_setup"]

# A readiness probe. `docker\s+version` deliberately does NOT match
# `docker --version` (a log-only sanity print) because of the `--`.
PROBE = re.compile(r'(docker\s+info|wsl\s+--status|docker\s+version)', re.I)
EXIT1 = re.compile(r'\bexit\s+1\b', re.I)
LOOP = re.compile(r'\b(while|for|foreach|do)\b', re.I)
ALLOW_LINE = "ps-readiness-retry-lint: allow"

LOOKAHEAD_EXIT = 4   # `exit 1` within this many lines after the probe
LOOKBEHIND_LOOP = 10  # enclosing loop within this many lines above the probe


def scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []

    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith('#'):
            continue
        if ALLOW_LINE in raw:
            continue
        if not PROBE.search(raw):
            continue

        ahead = lines[idx + 1: idx + 1 + LOOKAHEAD_EXIT]
        if not any(EXIT1.search(l) for l in ahead):
            continue

        behind = lines[max(0, idx - LOOKBEHIND_LOOP): idx]
        if any(LOOP.search(l) for l in behind):
            continue

        hits.append((idx + 1, stripped))

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
                    f"::error file={rel},line={lineno}::single-shot readiness "
                    f"probe followed by `exit 1` with no enclosing retry loop — "
                    f"a cold dockerd/WSL VM races the first poll (F3). Wrap it in "
                    f"a bounded retry loop (see wsl_docker_ready.ps1::"
                    f"Wait-DockerReady) or, for a genuine one-shot diagnostic, "
                    f"append '# {ALLOW_LINE}'. Offending: {snippet}"
                )
    if total:
        print(f"\nps-readiness-retry-lint FAILED: {total} un-retried probe(s).")
        return 1
    print("ps-readiness-retry-lint OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
