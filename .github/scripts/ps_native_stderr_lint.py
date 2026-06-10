#!/usr/bin/env python3
"""ps-native-stderr-lint — catch the Windows PowerShell 5.1 NativeCommandError trap.

Under a file-level `$ErrorActionPreference = 'Stop'`, ANY native command
(wsl / docker-via-wsl / usbipd / nvidia-smi / msiexec) that writes to stderr is
promoted to a *terminating* NativeCommandError — for EVERY redirection form
(`2>&1`, `2>$null`, `2>$file`, `*>$null 2>&1`), and EVEN ON SUCCESS (`docker info`
prints "WARNING: No swap limit support" to stderr on a healthy daemon; `docker
image inspect <missing>` prints "No such image"; `docker pull`/`docker load`
stream progress to stderr). This silently aborted the offline installer: the
dockerd-readiness poll in import_edubotics_wsl.ps1 threw on iteration 1, the
import "failed", and Inno skipped the offline `docker load`.

The proven-safe pattern (verify_system.ps1, uninstall_stop_containers.ps1, …) is
`$ErrorActionPreference = 'Continue'` + explicit `$LASTEXITCODE` checks. This
guard fails any installer .ps1 that combines a file-level EAP=Stop with a native
command whose stderr is captured/redirected.

Escape hatches (use sparingly, with a reason in the surrounding comment):
  * `# ps-native-stderr-lint: file-ok`  on its own line anywhere in the file
  * `# ps-native-stderr-lint: allow`    at the end of a specific native-call line
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("PS_LINT_ROOT") or
                    pathlib.Path(__file__).resolve().parents[2])
SCAN_DIRS = ["robotis_ai_setup", "physical_ai_tools"]

# A native command we route through wsl/docker, or a host-native exe.
NATIVE = re.compile(r'(?<![\w.\-])(wsl|wsl\.exe|docker|usbipd|nvidia-smi)(?![\w.\-])')
# Any form that pulls a native command's stderr into PowerShell's view.
STDERR_REDIR = re.compile(r'2>&1|2>\s*\$|2>\s*["\']|2>\s*[\w.]|\*>\s*\$null|\*>&1')
EAP_STOP = re.compile(r'\$ErrorActionPreference\s*=\s*["\']Stop["\']')
ALLOW_LINE = "ps-native-stderr-lint: allow"
ALLOW_FILE = "ps-native-stderr-lint: file-ok"


def file_level_eap_stop(text: str) -> bool:
    """True if the script sets EAP=Stop at top level (column 0, not indented inside
    a function/try block). Conservative: a single column-0 Stop assignment counts."""
    for line in text.splitlines():
        if EAP_STOP.match(line.strip()) and not line.startswith((" ", "\t")):
            return True
    return False


def main() -> int:
    errors: list[str] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.ps1")):
            text = p.read_bytes().decode("utf-8-sig", errors="replace")
            if ALLOW_FILE in text:
                continue
            if not file_level_eap_stop(text):
                continue  # EAP=Continue (or unset) — the trap can't fire
            for i, line in enumerate(text.splitlines(), 1):
                if ALLOW_LINE in line:
                    continue
                if NATIVE.search(line) and STDERR_REDIR.search(line):
                    rel = p.relative_to(ROOT).as_posix()
                    errors.append(f"{rel}:{i}: native command with captured stderr "
                                  f"under file-level $ErrorActionPreference='Stop' — "
                                  f"throws a terminating NativeCommandError in PS 5.1, "
                                  f"even on success.\n      {line.strip()}")
    if errors:
        sys.stderr.write("ps-native-stderr-lint FAILED:\n\n")
        sys.stderr.write("\n".join(errors) + "\n\n")
        sys.stderr.write("Fix: set $ErrorActionPreference='Continue' at file scope "
                         "(see verify_system.ps1) and check $LASTEXITCODE explicitly, "
                         "or wrap the call in try/catch.\n")
        return 1
    print("ps-native-stderr-lint OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
