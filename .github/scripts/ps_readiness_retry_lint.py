#!/usr/bin/env python3
"""ps-readiness-retry-lint — catch a single-shot readiness probe that exits hard.

A cold dockerd / WSL2 VM takes several seconds to come up. A readiness probe
(`docker info`, `wsl --status`, `docker version`) that is checked ONCE and then
hard-exits on failure races the cold start — the F3 class of failure that made
the installer abort on the very first poll of a freshly-imported distro. The
proven-safe pattern is a bounded retry loop that polls the exit code
(wsl_docker_ready.ps1::Wait-DockerReady, import_edubotics_wsl.ps1's poll).

This guard scans every *.ps1 under robotis_ai_setup/ and flags a readiness probe
that BOTH:
  (a) has a HARD EXIT on the probe line itself or within its enclosing block
      (see "Lookahead" below), AND
  (b) is NOT lexically inside a still-open `while`/`for`/`foreach`/`do` loop
      BODY at its position.

A HARD EXIT is any of:
  * `exit 1` / any non-zero literal / `exit $var` (value unknowable statically);
  * `throw` (a terminating error — the script dies exactly as hard);
  * a call to a HARD-EXIT HELPER — a `function` defined anywhere under the scan
    root whose body contains one of the above. This is discovered, not
    allowlisted: `Fail-WithNextAction` (finalize_install.ps1, ends in `exit 1`,
    4 call sites) is the house failure idiom and a literal-`exit`-only matcher
    walked straight past it. The scan is cross-file on purpose — these scripts
    dot-source each other (wsl_docker_ready.ps1).

Lookahead: the hard exit must appear within LOOKAHEAD_EXIT lines AND before the
probe's enclosing block closes (brace depth dropping below the probe's depth
ends the window early). The depth bound is what makes this robust rather than a
raw line count: install_prerequisites.ps1's `wsl --status` probe sits in a
`try { }` whose `exit 1` belongs to the LATER `wsl --install` failure path, and
it passed only because that `exit 1` happened to land 9 lines below an 8-line
window — one extra blank line would have flipped a correct pattern into a false
positive. Closing the `try` now ends the window regardless of spacing.

Loop detection is BRACE-AWARE, not proximity-based: braces are tracked across
the whole file and a `{` is marked as a loop brace when its opening line (or the
dangling keyword on the line above) carries a loop keyword. A loop that already
CLOSED above the probe therefore does not count — the original F3 bug in
pull_images.ps1 sat 9 lines below an unrelated, already-closed `foreach`, which
a naive lookbehind mistook for an enclosing loop. A do-while TERMINATOR
(`} while ($elapsed -lt 3)`) closes a loop instead of opening one and no longer
mis-tags the following `{` (_ps_lex's tail_veto).

All analysis runs on PREPROCESSED lines (_ps_lex.preprocess): the contents of
single-/double-quoted strings are blanked (quote chars survive) and `#` line
comments plus `<# … #>` block comments are removed. Prose can therefore neither
fake a loop keyword ("Waiting for Docker" above an `if {` must not mark that
brace as a loop) nor fake a probe (`Write-Host "run docker info"` must not be
flagged), and a `#` inside a string no longer truncates the line.

A one-shot diagnostic that merely records a failure (verify_system:
`else { Write-FAIL ... }`; uninstall: `if (...) { exit 0 }`) is not flagged
because it has no hard exit. For the genuinely one-shot cases that DO exit,
append `# ps-readiness-retry-lint: allow` to the probe line.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _ps_lex import brace_delta, consume_braces, preprocess  # noqa: E402

ROOT = pathlib.Path(os.environ.get("PS_LINT_ROOT") or
                    pathlib.Path(__file__).resolve().parents[2])
SCAN_DIRS = ["robotis_ai_setup"]

# A readiness probe. `docker\s+version` deliberately does NOT match
# `docker --version` (a log-only sanity print) because of the `--`.
PROBE = re.compile(r'(docker\s+info|wsl\s+--status|docker\s+version)', re.I)
# A hard exit: non-zero literal or a variable (`exit $rc` — its value is not
# knowable statically, so treat it as hard). `exit`/`exit 0` are not hard.
EXIT_LITERAL = re.compile(r'\bexit\s+(?:[1-9]\d*|\$\w+)\b', re.I)
# `throw` terminates just as hard as `exit 1`.
THROW = re.compile(r'\bthrow\b', re.I)
# PS function definition: `function Fail-WithNextAction {`, `function Emit {`.
FUNC_DEF = re.compile(r'\bfunction\s+([A-Za-z_][\w-]*)', re.I)
# PS loop keywords. `do` is anchored so `docker`/`dot-source` never match.
LOOP_KW = re.compile(r'\b(while|for|foreach|do)\b', re.I)
# `} while (...)` — a do-loop terminator, not a loop header (see _ps_lex).
DO_WHILE_TAIL = re.compile(r'^\s*while\s*\(', re.I)
ALLOW_LINE = "ps-readiness-retry-lint: allow"

LOOKAHEAD_EXIT = 8   # hard exit within this many lines after the probe


def _hard_exit_re(helpers: set[str]) -> re.Pattern:
    """`exit <hard>` | `throw` | a call to any discovered hard-exit helper."""
    alts = [EXIT_LITERAL.pattern, THROW.pattern]
    if helpers:
        alts.append(r'\b(?:' + '|'.join(re.escape(h) for h in sorted(helpers))
                    + r')\b')
    return re.compile('|'.join(alts), re.I)


def collect_hard_exit_helpers(paths: list[pathlib.Path]) -> set[str]:
    """Names of functions whose BODY contains a hard exit or a throw.

    Brace-matched over preprocessed lines, so a one-liner
    (`function Fail { exit 1 }`) and a multi-line body are both handled, and a
    nested helper closes before its parent.
    """
    names: set[str] = set()
    for p in paths:
        lines = preprocess(p.read_bytes().decode("utf-8-sig", errors="replace"))
        depth = 0
        frames: list[tuple[str, int, int, int]] = []  # name, depth, line, col
        pending_name: str | None = None
        for li, code in enumerate(lines):
            pending_from = 0
            m = FUNC_DEF.search(code)
            if m:
                pending_name, pending_from = m.group(1), m.end()
            for ci, c in enumerate(code):
                if c == '{':
                    depth += 1
                    if pending_name is not None and ci >= pending_from:
                        frames.append((pending_name, depth, li, ci + 1))
                        pending_name = None
                elif c == '}':
                    if frames and frames[-1][1] == depth:
                        name, _, sl, sc = frames.pop()
                        body = '\n'.join(
                            [lines[sl][sc:]] + lines[sl + 1:li] + [code[:ci]]
                        ) if li > sl else code[sc:ci]
                        if EXIT_LITERAL.search(body) or THROW.search(body):
                            names.add(name)
                    depth = max(0, depth - 1)
    return names


def scan_file(path: pathlib.Path, hard_exit: re.Pattern) -> list[tuple[int, str]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    raw_lines = text.splitlines()
    lines = preprocess(text)
    hits: list[tuple[int, str]] = []

    brace_is_loop: list[bool] = []
    pending_loop = False

    for idx, code in enumerate(lines):
        raw = raw_lines[idx]

        pm = PROBE.search(code)
        if pm and ALLOW_LINE not in raw:
            # Window: the rest of the probe line (the F3 same-line shape
            # `docker info ...; if (...) { exit 1 }`), then the following lines
            # until either LOOKAHEAD_EXIT is spent or the probe's ENCLOSING
            # block closes — whichever comes first.
            window = [code[pm.end():]]
            depth = 0
            for look in lines[idx + 1: idx + 1 + LOOKAHEAD_EXIT]:
                window.append(look)
                depth += brace_delta(look)
                if depth < 0:
                    break
            if any(hard_exit.search(w) for w in window):
                # In-loop-ness is decided at the probe's position: process the
                # braces opened EARLIER on this same line (a
                # `while (...) { probe ... }` one-liner opens its loop brace
                # before the probe) on top of the stack carried from prior
                # lines. Copies only — the real state advances once, below.
                local_stack = list(brace_is_loop)
                consume_braces(code[:pm.start()], local_stack, pending_loop,
                               LOOP_KW, DO_WHILE_TAIL)
                if not any(local_stack):
                    hits.append((idx + 1, raw.strip()))

        pending_loop = consume_braces(code, brace_is_loop, pending_loop,
                                      LOOP_KW, DO_WHILE_TAIL)

    return hits


def main() -> int:
    total = 0
    scanned = 0
    files: list[pathlib.Path] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        files.extend(sorted(base.rglob("*.ps1")))

    helpers = collect_hard_exit_helpers(files)
    hard_exit = _hard_exit_re(helpers)
    if helpers:
        # Printed BEFORE the findings: when a probe is flagged because of a
        # helper call rather than a literal `exit`, this line is what explains
        # the finding to whoever has to act on it.
        print(f"ps-readiness-retry-lint: hard-exit helpers discovered: "
              f"{', '.join(sorted(helpers))}")

    for p in files:
        scanned += 1
        rel = p.relative_to(ROOT).as_posix()
        for lineno, snippet in scan_file(p, hard_exit):
            total += 1
            print(
                f"::error file={rel},line={lineno}::single-shot readiness "
                f"probe followed by a hard exit (exit/throw/hard-exit helper) "
                f"outside any enclosing retry loop — a cold dockerd/WSL VM "
                f"races the first poll (F3). Wrap it in a bounded retry loop "
                f"(see wsl_docker_ready.ps1::Wait-DockerReady) or, for a "
                f"genuine one-shot diagnostic, append '# {ALLOW_LINE}'. "
                f"Offending: {snippet}"
            )
    # A rename of the scan dir (or a wrong PS_LINT_ROOT) used to make every
    # guard print OK having read ZERO files — false confidence, not a pass.
    if scanned == 0:
        print(f"::error::ps-readiness-retry-lint scanned 0 .ps1 files under "
              f"{ROOT}/{{{','.join(SCAN_DIRS)}}} — the scan root moved or "
              f"PS_LINT_ROOT is wrong. Refusing to report a green pass.")
        return 1
    if total:
        print(f"\nps-readiness-retry-lint FAILED: {total} un-retried probe(s).")
        return 1
    print(f"ps-readiness-retry-lint OK ({scanned} file(s) scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
