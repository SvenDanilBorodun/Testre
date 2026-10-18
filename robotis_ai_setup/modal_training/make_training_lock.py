"""Generate the ONE dependency lock every training image installs (CLAUDE.md Rule §5, R2).

    python robotis_ai_setup/modal_training/make_training_lock.py            # regenerate (needs docker)
    python robotis_ai_setup/modal_training/make_training_lock.py --check    # hash file == lock (no docker)

The recipe below is today's Modal sequence, written as quoted literals so
tests/test_lerobot_pin_lockstep.py can read it as the training site of Rule §5:
an apt build toolchain (LeRobot pulls in evdev, a C extension), the five PyPI
specs, then torch + torchvision from the CUDA 12.6 wheel index with
--force-reinstall, then LeRobot's numpy range and datasets' fsspec cap restored
from PyPI (E21), then torchcodec removed (LeRobot decodes with PyAV). `pip check`
must then be clean apart from that deliberate torchcodec removal.

It runs inside the pinned amd64 python base (the trainer image's base), freezes
everything that ended up installed (minus pip itself) and writes two files:

  modal_training/training-lock.txt                       one `name==version` per line
  cloud_training_api/app/services/training_lock_hash.py  TRAINING_LOCK_SHA256 of that file

Both images (the RunPod trainer and the Modal app) install the lock with exactly
INSTALL_COMMAND and --no-deps, then run check_training_lock.py. At every `start`
the worker hashes the lock it finds and the API compares it with
TRAINING_LOCK_SHA256, so an image built from another lock is refused.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import subprocess
import sys
from pathlib import Path

LEROBOT_VERSION = "0.5.1"

BASE_IMAGE = "python:3.12.14-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
PLATFORM = "linux/amd64"

APT_PACKAGES = ("gcc", "g++", "build-essential")

PYPI_SPECS = (
    f"lerobot[pi,smolvla,peft]=={LEROBOT_VERSION}",
    "scipy>=1.14.0,<1.18",
    "accelerate>=1.10.0,<2.0.0",
    "huggingface_hub",
    "supabase",
)

TORCH_SPECS = ("torch==2.7.0", "torchvision==0.22.0")
index_url = "https://download.pytorch.org/whl/cu126"

# E21: the force-reinstall above re-resolves torch's shared deps from the cu126 index (numpy 2.5.2,
# fsspec 2026.7.0 when this was written), outside LeRobot 0.5.1's numpy range and datasets 4.8.5's
# fsspec cap. One PyPI step restores both, exactly as modal_inference/inference_app.py does (V1-10).
REPIN_SPECS = ("numpy>=2.0.0,<2.3.0", "fsspec==2026.2.0")

# `pip check` of the resolved environment must be clean except for this one deliberate line:
# torchcodec is removed on purpose (LeRobot decodes with PyAV; CLAUDE.md Rule §5).
PIP_CHECK_ALLOWED = (
    "lerobot 0.5.1 requires torchcodec, which is not installed.",
)

# The one install command both images run (tests/test_training_lock.py holds both Dockerfiles to it).
INSTALL_COMMAND = (
    "python -m pip install --no-cache-dir --no-deps -r {lock} "
    "--index-url https://pypi.org/simple --extra-index-url " + index_url
)

HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE / "training-lock.txt"
HASH_PATH = HERE.parent / "cloud_training_api" / "app" / "services" / "training_lock_hash.py"

_FREEZE_MARK = "=====EDUBOTICS-FREEZE====="
_CHECK_MARK = "=====EDUBOTICS-PIP-CHECK====="


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def build_script() -> str:
    """The shell script run inside the base image (today's Modal sequence)."""
    lines = [
        "set -e",
        "export DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore",
        "apt-get update -q >/dev/null",
        "apt-get install -y -q --no-install-recommends " + " ".join(APT_PACKAGES) + " >/dev/null",
        "python -m pip install --no-cache-dir " + " ".join(_q(s) for s in PYPI_SPECS),
        "python -m pip install --no-cache-dir " + " ".join(_q(s) for s in TORCH_SPECS)
        + " --index-url " + index_url + " --force-reinstall",
        "python -m pip install --no-cache-dir " + " ".join(_q(s) for s in REPIN_SPECS),
        "python -m pip uninstall -y torchcodec || true",
        "echo " + _CHECK_MARK,
        "python -m pip check || true",
        "echo " + _FREEZE_MARK,
        "python -m pip freeze --all",
    ]
    return "\n".join(lines) + "\n"


def pip_check_problems(text: str) -> list[str]:
    """The `pip check` lines between the two markers that are not PIP_CHECK_ALLOWED."""
    if _CHECK_MARK not in text:
        raise ValueError("the build script printed no pip check section")
    section = text.split(_CHECK_MARK, 1)[1].split(_FREEZE_MARK, 1)[0]
    lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
    return [ln for ln in lines if ln not in PIP_CHECK_ALLOWED and ln != "No broken requirements found."]


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_freeze(text: str) -> list[str]:
    """`pip freeze --all` output (after the marker) -> sorted `name==version` lines, pip removed."""
    if _FREEZE_MARK in text:
        text = text.split(_FREEZE_MARK, 1)[1]
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line)
        if m is None:
            raise ValueError(f"unexpected freeze line (not name==version): {line!r}")
        name = pep503(m.group(1))
        if name == "pip":
            continue
        out[name] = m.group(2)
    if not out:
        raise ValueError("pip freeze produced no packages")
    return [f"{name}=={out[name]}" for name in sorted(out)]


def render_lock(entries: list[str], today: str) -> str:
    header = f"# generated by make_training_lock.py on {today}; do not edit\n"
    return header + "".join(e + "\n" for e in entries)


def render_hash_module(lock_bytes: bytes) -> str:
    digest = hashlib.sha256(lock_bytes).hexdigest()
    return (
        '"""Generated by robotis_ai_setup/modal_training/make_training_lock.py; do not edit.\n\n'
        "sha256 of modal_training/training-lock.txt. The worker sends the hash of the lock it finds\n"
        "in POST .../start; any other value is refused as wrong_image.\n"
        '"""\n\n'
        f'TRAINING_LOCK_SHA256 = "{digest}"\n'
    )


def check() -> int:
    lock = LOCK_PATH.read_bytes()
    want = render_hash_module(lock)
    have = HASH_PATH.read_text(encoding="utf-8") if HASH_PATH.is_file() else ""
    if have != want:
        print(f"MISMATCH: {HASH_PATH} does not carry sha256({LOCK_PATH.name}) = "
              f"{hashlib.sha256(lock).hexdigest()}", file=sys.stderr)
        return 1
    print(f"OK: TRAINING_LOCK_SHA256 = {hashlib.sha256(lock).hexdigest()} "
          f"({len(lock.splitlines()) - 1} packages)")
    return 0


def generate() -> int:
    cmd = ["docker", "run", "--rm", "-i", "--platform", PLATFORM, BASE_IMAGE, "sh", "-s"]
    print("running:", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, input=build_script(), capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:] + "\n" + proc.stderr[-4000:] + "\n")
        print(f"docker run failed with exit {proc.returncode}", file=sys.stderr)
        return proc.returncode or 1
    problems = pip_check_problems(proc.stdout)
    if problems:
        print("pip check found broken requirements:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print("pip check: clean apart from " + "; ".join(PIP_CHECK_ALLOWED))
    entries = parse_freeze(proc.stdout)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    lock_text = render_lock(entries, today)
    LOCK_PATH.write_text(lock_text, encoding="utf-8", newline="\n")
    HASH_PATH.write_text(render_hash_module(lock_text.encode("utf-8")), encoding="utf-8", newline="\n")
    print(f"wrote {LOCK_PATH} ({len(entries)} packages) and {HASH_PATH}")
    return check()


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        return check()
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    return generate()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
