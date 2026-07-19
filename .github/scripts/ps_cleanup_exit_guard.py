#!/usr/bin/env python3
r"""ps-cleanup-exit-guard — pin the "cleanup must not be the terminal statement".

A script's process exit code falls through to the $LASTEXITCODE of its LAST
executed NATIVE command. When that command is a best-effort CLEANUP —
`docker image prune`, `docker image rm` — a transient non-zero from the cleanup
silently becomes the script's exit code. Callers that key on it then falsely
report failure: this is exactly why pull_images.ps1 ends with an explicit
`exit 0` AFTER its `docker image prune -f` (the .iss Step 5 and
finalize_install.ps1 both propagate that exit code). Likewise
`docker image prune -f; exit 1` on one line makes a cleanup hiccup a hard
failure.

TWO RULES:

  (a) FALL-THROUGH. A file containing a NATIVE cleanup command must contain an
      explicit `exit` AFTER the last one.

  (b) CONFLATION. A cleanup command must not share a line with `exit 1`.

Why (a) is phrased as "an exit must FOLLOW", not "the last line must not be a
cleanup". The rule used to be the latter, and it could not catch the very
regression its own docstring cites. Delete the trailing `exit 0` from the real
pull_images.ps1 and the tail becomes

    wsl -d $DistroName -- docker image prune -f *>$null   <- the hazard
    Write-OK  "…"
    Write-Step "…"

— the last *line* is a `Write-Step`, so the old rule stayed silent while
$LASTEXITCODE still fell through to the prune. `Write-Host`/`Write-Step`/
`Write-OK` are CMDLETS: they do not touch $LASTEXITCODE. Catching that with a
"walk backwards to the last native command" rule would require classifying
every PowerShell line as native-vs-cmdlet, and BOTH error directions of that
classification are bad — an unrecognised native command after the cleanup
becomes a false positive, an over-broad list becomes exactly the false negative
being fixed. The inverted rule needs no such classification: it asks only
"is there an `exit` after the cleanup", the fix is always the same one line,
and it is unconditionally correct to apply.

Why (a) is NATIVE-only. Only native commands set $LASTEXITCODE. `Remove-Item`
is a cmdlet, so it can never be the fall-through source — the old rule counted
it, which made wsl_docker_ready.ps1 (a dot-sourced FUNCTION LIBRARY that has no
`exit` anywhere, by design) a false positive the moment the rule was inverted.
Rule (b) still counts a Remove-Item of a scratch file, because that rule is
about `exit 1` conflation, not exit-code fall-through.

SCRATCH_TOKEN was `(temp|tmp|err)` anywhere on the line — which matched the
`Err` in `-ErrorAction`, so
`Remove-Item $flagPath -Force -ErrorAction SilentlyContinue` was classified a
"cleanup", flatly contradicting this file's own carve-out that a Remove-Item of
a real state file (finalize_install.ps1's `.reboot_required` flag) is not one.
It now requires an actual scratch marker: `$env:TEMP`/`$env:TMP`, a `*file`
identifier (`$tmpFile`, `$dockerErrFile`), or a `.tmp`/`.err` extension.

All matching runs on _ps_lex.preprocess'd lines (comments removed, string
contents blanked), so neither a `# docker image prune` note nor a
`Write-Host "… docker image rm …"` diagnostic can fabricate a finding.

Escape hatch: append `# ps-cleanup-exit-guard: allow` to the flagged line.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _ps_lex import preprocess  # noqa: E402

ROOT = pathlib.Path(os.environ.get("PS_LINT_ROOT") or
                    pathlib.Path(__file__).resolve().parents[2])
SCAN_DIRS = ["robotis_ai_setup"]

# NATIVE cleanup commands — the only kind that can set $LASTEXITCODE.
DOCKER_CLEANUP = re.compile(r'docker\s+image\s+(prune|rm)\b', re.I)
# Cmdlet cleanup — rule (b) only (see the module docstring).
REMOVE_ITEM = re.compile(r'\bRemove-Item\b', re.I)
SCRATCH_TOKEN = re.compile(
    r'\$env:(TEMP|TMP)\b'          # $env:TEMP / $env:TMP
    r'|\b\w*(temp|tmp|err)file\b'  # $tmpFile, $dockerErrFile, $tempFile
    r'|\.tmp\b|\.err\b',           # foo.tmp / foo.err
    re.I,
)
EXIT1 = re.compile(r'\bexit\s+1\b', re.I)
# Any explicit exit terminates the fall-through (exit 0 / exit $rc /
# exit $LASTEXITCODE). `(?!-)` keeps `Exit-PSSession` out.
EXIT_ANY = re.compile(r'\bexit\b(?!-)', re.I)
ALLOW_LINE = "ps-cleanup-exit-guard: allow"


def _is_native_cleanup(code: str) -> bool:
    return bool(DOCKER_CLEANUP.search(code))


def _is_any_cleanup(code: str) -> bool:
    return bool(DOCKER_CLEANUP.search(code)) or bool(
        REMOVE_ITEM.search(code) and SCRATCH_TOKEN.search(code)
    )


def scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    raw_lines = text.splitlines()
    lines = preprocess(text)
    hits: list[tuple[int, str]] = []

    last_native_idx = -1
    last_native_end = 0
    for idx, code in enumerate(lines):
        raw = raw_lines[idx]

        # (b) cleanup sharing a line with `exit 1`.
        if _is_any_cleanup(code) and EXIT1.search(code) and ALLOW_LINE not in raw:
            hits.append((idx + 1,
                         f"cleanup on the same line as `exit 1`: {raw.strip()}"))

        m = DOCKER_CLEANUP.search(code)
        if m and ALLOW_LINE not in raw:
            last_native_idx, last_native_end = idx, m.end()

    # (a) no explicit `exit` anywhere after the LAST native cleanup.
    if last_native_idx >= 0:
        tail = [lines[last_native_idx][last_native_end:]]
        tail.extend(lines[last_native_idx + 1:])
        if not any(EXIT_ANY.search(t) for t in tail):
            hits.append((
                last_native_idx + 1,
                f"no explicit `exit` follows this cleanup, so the script's exit "
                f"code falls through to it (cmdlets like Write-Host/Write-Step "
                f"below it do NOT reset $LASTEXITCODE): "
                f"{raw_lines[last_native_idx].strip()}",
            ))

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
            for lineno, detail in scan_file(p):
                total += 1
                print(
                    f"::error file={rel},line={lineno}::the script's exit code "
                    f"falls through to a best-effort cleanup — add an explicit "
                    f"`exit 0` after it (see pull_images.ps1) or remove the "
                    f"`exit 1`; append '# {ALLOW_LINE}' for a deliberate case. "
                    f"{detail}"
                )
    # A rename of the scan dir (or a wrong PS_LINT_ROOT) used to make every
    # guard print OK having read ZERO files — false confidence, not a pass.
    if scanned == 0:
        print(f"::error::ps-cleanup-exit-guard scanned 0 .ps1 files under "
              f"{ROOT}/{{{','.join(SCAN_DIRS)}}} — the scan root moved or "
              f"PS_LINT_ROOT is wrong. Refusing to report a green pass.")
        return 1
    if total:
        print(f"\nps-cleanup-exit-guard FAILED: {total} cleanup-exit hazard(s).")
        return 1
    print(f"ps-cleanup-exit-guard OK ({scanned} file(s) scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
