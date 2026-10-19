"""Check the running interpreter's packages against a training lock (spec §3.10, R2). Stdlib only.

    python check_training_lock.py [LOCK] [--strict]

LOCK defaults to the training-lock.txt next to this file (/app in the trainer
image, /root on Modal). Every lock entry must be installed at exactly its
version, and torchcodec must be absent (LeRobot decodes with PyAV). --strict
(the trainer image) also refuses any installed distribution that is neither in
the lock nor one of pip, setuptools, wheel. Exit 0 on success, 1 on a mismatch,
2 on a usage or lock-file error.
"""

from __future__ import annotations

import importlib.metadata
import re
import sys
from pathlib import Path

STRICT_EXTRA_ALLOWED = frozenset({"pip", "setuptools", "wheel"})
FORBIDDEN = frozenset({"torchcodec"})
DEFAULT_LOCK = Path(__file__).resolve().parent / "training-lock.txt"

_LINE_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.fullmatch(line)
        if m is None:
            raise ValueError(f"{path}:{n}: not a name==version line: {line!r}")
        name = pep503(m.group(1))
        if name in entries:
            raise ValueError(f"{path}:{n}: {name} is listed twice")
        entries[name] = m.group(2)
    if not entries:
        raise ValueError(f"{path}: the lock is empty")
    return entries


def installed_distributions() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"] if dist.metadata is not None else None
        if not name:
            continue
        found.setdefault(pep503(name), []).append(dist.version)
    return found


def compare(lock: dict[str, str], installed: dict[str, list[str]], strict: bool) -> list[str]:
    problems: list[str] = []
    for name, want in sorted(lock.items()):
        have = installed.get(name)
        if not have:
            problems.append(f"missing: {name}=={want}")
        elif len(set(have)) > 1:
            problems.append(f"installed more than once: {name} {sorted(set(have))}")
        elif have[0] != want:
            problems.append(f"wrong version: {name}=={have[0]} (lock: {want})")
    for name in sorted(FORBIDDEN & set(installed)):
        problems.append(f"forbidden: {name} is installed")
    for name in sorted(FORBIDDEN & set(lock)):
        problems.append(f"forbidden: {name} is in the lock")
    if strict:
        for name in sorted(set(installed) - set(lock) - STRICT_EXTRA_ALLOWED - FORBIDDEN):
            problems.append(f"not in the lock: {name}=={installed[name][0]}")
    return problems


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    rest = [a for a in argv if a != "--strict"]
    if len(rest) > 1 or any(a.startswith("-") for a in rest):
        print(__doc__, file=sys.stderr)
        return 2
    lock_path = Path(rest[0]) if rest else DEFAULT_LOCK
    try:
        lock = read_lock(lock_path)
    except (OSError, ValueError) as e:
        print(f"check_training_lock: {e}", file=sys.stderr)
        return 2
    problems = compare(lock, installed_distributions(), strict)
    if problems:
        for p in problems:
            print(f"check_training_lock: {p}", file=sys.stderr)
        print(f"check_training_lock: FAILED, {len(problems)} problem(s) against {lock_path}", file=sys.stderr)
        return 1
    print(f"check_training_lock: OK, {len(lock)} packages match {lock_path}"
          + (" (strict)" if strict else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
