#!/usr/bin/env python3
"""ps-cleanup-exit-guard — pin the "cleanup must not be the terminal statement".

A script's process exit code falls through to the $LASTEXITCODE of its LAST
executed native command. When that last statement is a best-effort CLEANUP —
`docker image prune`, `docker image rm`, or a Remove-Item of a temp/err/tmp
scratch file — a transient non-zero from the cleanup silently becomes the
script's exit code. Callers that key on it then falsely report failure: this is
exactly why pull_images.ps1 ends with an explicit `exit 0` AFTER its
`docker image prune -f` (the .iss Step 5 and finalize_install.ps1 both propagate
that exit code). Likewise `docker image prune -f; exit 1` on one line makes a
cleanup hiccup a hard failure.

This guard scans every *.ps1 under robotis_ai_setup/ and flags (::error, exit 1):
  (a) a script whose LAST non-comment/non-blank statement IS a cleanup command
      (no explicit `exit 0` follows it), and
  (b) any line where a cleanup command shares the line with `exit 1`.

Escape hatch: append `# ps-cleanup-exit-guard: allow` to the flagged line.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("PS_LINT_ROOT") or
                    pathlib.Path(__file__).resolve().parents[2])
SCAN_DIRS = ["robotis_ai_setup"]

# Cleanup native commands. Remove-Item counts ONLY when the same line also names
# a temp/err/tmp scratch path (variable or literal) — a Remove-Item of a real
# state file (e.g. $flagPath = .reboot_required) is not a fall-through hazard.
DOCKER_CLEANUP = re.compile(r'docker\s+image\s+(prune|rm)\b', re.I)
REMOVE_ITEM = re.compile(r'\bRemove-Item\b', re.I)
SCRATCH_TOKEN = re.compile(r'(temp|tmp|err)', re.I)
EXIT1 = re.compile(r'\bexit\s+1\b', re.I)
ALLOW_LINE = "ps-cleanup-exit-guard: allow"


def _strip_inline_comment(line: str) -> str:
    """Drop a trailing `# ...` comment (best-effort — no `#` inside a string on
    the cleanup lines we care about)."""
    idx = line.find('#')
    return line if idx == -1 else line[:idx]


def _is_cleanup(code: str) -> bool:
    if DOCKER_CLEANUP.search(code):
        return True
    if REMOVE_ITEM.search(code) and SCRATCH_TOKEN.search(code):
        return True
    return False


def scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []

    last_code_idx = -1
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue

        code = _strip_inline_comment(raw)

        # (b) cleanup sharing a line with `exit 1`.
        if _is_cleanup(code) and EXIT1.search(code) and ALLOW_LINE not in raw:
            hits.append((idx + 1, f"cleanup on the same line as `exit 1`: {stripped}"))

        last_code_idx = idx

    # (a) last statement is a cleanup command.
    if last_code_idx >= 0:
        raw = lines[last_code_idx]
        code = _strip_inline_comment(raw)
        if _is_cleanup(code) and ALLOW_LINE not in raw:
            hits.append((
                last_code_idx + 1,
                f"terminal statement is a cleanup command with no `exit 0` "
                f"after it: {raw.strip()}",
            ))

    return hits


def main() -> int:
    total = 0
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.ps1")):
            rel = p.relative_to(ROOT).as_posix()
            for lineno, detail in scan_file(p):
                total += 1
                print(
                    f"::error file={rel},line={lineno}::the script's exit code "
                    f"falls through to a best-effort cleanup — add an explicit "
                    f"`exit 0` after it (see pull_images.ps1) or remove the "
                    f"`exit 1`; append '# {ALLOW_LINE}' for a deliberate case. "
                    f"{detail}"
                )
    if total:
        print(f"\nps-cleanup-exit-guard FAILED: {total} cleanup-exit hazard(s).")
        return 1
    print("ps-cleanup-exit-guard OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
