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
  (b) is NOT lexically inside a still-open `while`/`for`/`foreach`/`do` loop
      BODY at its position.

Loop detection is BRACE-AWARE, not proximity-based: braces are tracked across
the whole file and a `{` is marked as a loop brace when its opening line (or the
dangling keyword on the line above) carries a loop keyword outside comments. A
loop that already CLOSED above the probe therefore does not count — the original
F3 bug in pull_images.ps1 sat 9 lines below an unrelated, already-closed
`foreach`, which a naive lookbehind mistook for an enclosing loop. Comment
lines are ignored entirely, so prose like "wait for docker" cannot suppress a
finding.

A one-shot diagnostic that merely records a failure (verify_system:
`else { Write-FAIL ... }`; uninstall: `if (...) { exit 0 }`) is not flagged
because it has no hard `exit 1`. For the genuinely one-shot cases that DO exit,
append `# ps-readiness-retry-lint: allow` to the probe line.
"""
from __future__ import annotations

import os
import pathlib
import re

ROOT = pathlib.Path(os.environ.get("PS_LINT_ROOT") or
                    pathlib.Path(__file__).resolve().parents[2])
SCAN_DIRS = ["robotis_ai_setup"]

# A readiness probe. `docker\s+version` deliberately does NOT match
# `docker --version` (a log-only sanity print) because of the `--`.
PROBE = re.compile(r'(docker\s+info|wsl\s+--status|docker\s+version)', re.I)
EXIT1 = re.compile(r'\bexit\s+1\b', re.I)
# PS loop keywords. `do` is anchored so `docker`/`dot-source` never match.
LOOP_KW = re.compile(r'\b(while|for|foreach|do)\b', re.I)
ALLOW_LINE = "ps-readiness-retry-lint: allow"

LOOKAHEAD_EXIT = 4   # `exit 1` within this many lines after the probe


def _strip_inline_comment(line: str) -> str:
    """Drop a trailing `# ...` comment (best-effort — good enough for the
    loop-keyword and brace scan; a `#` inside a string is rare on these lines)."""
    idx = line.find('#')
    return line if idx == -1 else line[:idx]


def _consume_braces(code: str, stack: list[bool], pending_loop: bool) -> bool:
    """Update the brace stack for one comment-stripped line.

    `stack[k]` is True iff the k-th currently-open brace opened a LOOP body.
    A `{` is a loop brace when the code segment before it on the same line
    contains a loop keyword (`while (...) {`, `foreach ($x in $y) {`, `do {`),
    or when the previous line ended with a dangling loop keyword/condition
    (`pending_loop`). Returns the new pending_loop flag.
    """
    seg_start = 0
    for i, c in enumerate(code):
        if c == '{':
            seg = code[seg_start:i]
            is_loop = pending_loop or bool(LOOP_KW.search(seg))
            stack.append(is_loop)
            pending_loop = False
            seg_start = i + 1
        elif c == '}':
            if stack:
                stack.pop()
            seg_start = i + 1
    tail = code[seg_start:]
    return bool(LOOP_KW.search(tail))


def scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []

    brace_is_loop: list[bool] = []
    pending_loop = False

    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith('#'):
            continue
        code = _strip_inline_comment(raw)

        pm = PROBE.search(code)
        if pm and ALLOW_LINE not in raw:
            ahead = lines[idx + 1: idx + 1 + LOOKAHEAD_EXIT]
            if any(EXIT1.search(_strip_inline_comment(l)) for l in ahead):
                # In-loop-ness is decided at the probe's position: process the
                # braces opened EARLIER on this same line (a
                # `while (...) { probe ... }` one-liner opens its loop brace
                # before the probe) on top of the stack carried from prior
                # lines. Copies only — the real state advances once, below.
                local_stack = list(brace_is_loop)
                _consume_braces(code[:pm.start()], local_stack, pending_loop)
                if not any(local_stack):
                    hits.append((idx + 1, stripped))

        pending_loop = _consume_braces(code, brace_is_loop, pending_loop)

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
                    f"probe followed by `exit 1` outside any enclosing retry "
                    f"loop — a cold dockerd/WSL VM races the first poll (F3). "
                    f"Wrap it in a bounded retry loop (see wsl_docker_ready.ps1"
                    f"::Wait-DockerReady) or, for a genuine one-shot "
                    f"diagnostic, append '# {ALLOW_LINE}'. Offending: {snippet}"
                )
    if total:
        print(f"\nps-readiness-retry-lint FAILED: {total} un-retried probe(s).")
        return 1
    print("ps-readiness-retry-lint OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
