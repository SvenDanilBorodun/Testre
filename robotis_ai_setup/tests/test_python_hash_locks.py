# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The hash-locked Python requirement files (E1, 2026-11-11).

The cloud API image, W2's import probe, the EduBotics.exe build and
ci.yml::python-locks install ONLY a lock written by
.github/scripts/python_locks.sh, with ``pip install --require-hashes``. This
module holds the shape of that arrangement; ci.yml::python-locks regenerates
every lock with the pinned uv and fails when one differs from its input.

* every lock entry is ``name==version`` with at least one sha256 hash, and
  nothing else (no URL, no editable, no index option);
* every exact pin of an input appears in its lock at that version, and every
  other requirement of an input appears in its lock at all;
* every install site names a lock the script writes, with ``--require-hashes``
  and ``--only-binary :all:``, and none of them installs an unlocked file;
* uv's version in uv-requirements.txt is the version its lock pins.
"""

from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows"
SCRIPT = REPO / ".github" / "scripts" / "python_locks.sh"
API = REPO / "robotis_ai_setup" / "cloud_training_api"
GUI = REPO / "robotis_ai_setup" / "gui"

# lock -> its inputs (exactly the `lock` calls of python_locks.sh).
LOCKS = {
    API / "requirements.lock": [API / "requirements.txt"],
    GUI / "requirements.lock": [GUI / "requirements.txt", GUI / "requirements-build.txt"],
    GUI / "requirements-build-backend.lock": [GUI / "requirements-build-backend.txt"],
    REPO / ".github" / "scripts" / "uv-requirements.lock": [REPO / ".github" / "scripts" / "uv-requirements.txt"],
}

_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^\]]*\])?==([0-9][0-9A-Za-z.+!-]*) \\$")
_HASH = re.compile(r"^    --hash=sha256:[0-9a-f]{64}( \\)?$")
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^\]]*\])?\s*(?:(==|>=|<=|~=|!=|<|>)\s*([^\s;,]+))?")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_entries(path: Path) -> dict[str, tuple[str, int]]:
    """name -> (version, number of hashes); asserts the line shape as it goes."""
    entries: dict[str, tuple[str, int]] = {}
    current = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            current = None
            continue
        entry = _ENTRY.match(line)
        if entry:
            current = _norm(entry[1])
            if current in entries:
                raise AssertionError(f"{path}:{number}: {current} twice")
            entries[current] = (entry[2], 0)
            continue
        if _HASH.match(line) and current is not None:
            version, hashes = entries[current]
            entries[current] = (version, hashes + 1)
            continue
        raise AssertionError(f"{path}:{number}: not a hashed `name==version` entry: {line!r}")
    return entries


def _requirements(path: Path) -> dict[str, tuple[str | None, str | None]]:
    """name -> (operator, version) of every requirement line of an input."""
    reqs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _REQ.match(line)
        assert match, f"{path}: unparsed requirement {line!r}"
        reqs[_norm(match[1])] = (match[2], match[3])
    return reqs


def _script_locks() -> dict[Path, list[Path]]:
    """The `lock OUTPUT PY PLATFORM FLAGS INPUT...` calls of python_locks.sh."""
    text = SCRIPT.read_text(encoding="utf-8").replace("\\\n", " ")
    calls = {}
    for line in text.splitlines():
        if not line.startswith("lock "):
            continue
        args = shlex.split(line)[1:]
        out, _py, _platform, _flags, *inputs = args
        calls[REPO / out] = [REPO / i for i in inputs]
    return calls


# A `pip install` in command position (a RUN, a shell or PowerShell line), never
# the text of a message that merely names it (`throw "pip install … failed"`).
_PIP_COMMAND = re.compile(r"^\s*(?:RUN\s+)?(?:python3?\s+-m\s+)?pip\s+install\b(.*)$")
# Options whose value is the next argument.
_VALUED = {"-r", "--requirement", "--only-binary", "--no-binary", "-c", "--constraint"}


def _pip_installs(text: str) -> list[list[str]]:
    """The arguments of every `pip install` command in a run block or a RUN."""
    joined = text.replace("\\\n", " ")
    installs = []
    for line in joined.splitlines():
        line = line.split("#", 1)[0]
        command = _PIP_COMMAND.match(line)
        if command:
            installs.append(shlex.split(command[1].split("&&", 1)[0]))
    return installs


def _positional(args: list[str]) -> list[str]:
    """What an install names besides its options: packages, never option values."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in _VALUED:
            skip = True
        elif not arg.startswith("-"):
            out.append(arg)
    return out


def _workflow_runs(path: Path, job_id: str | None = None) -> str:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = doc["jobs"] if job_id is None else {job_id: doc["jobs"][job_id]}
    return "\n".join(step.get("run", "") for job in jobs.values() for step in job.get("steps") or [])


def _requirement_file(args: list[str]) -> str:
    return args[args.index("-r") + 1]


class EveryLockIsHashed(unittest.TestCase):
    def test_every_entry_is_pinned_and_hashed(self):
        for lock in LOCKS:
            with self.subTest(lock=lock.relative_to(REPO)):
                entries = _lock_entries(lock)
                self.assertTrue(entries)
                for name, (_version, hashes) in entries.items():
                    self.assertGreaterEqual(hashes, 1, name)

    def test_written_by_the_script(self):
        for lock in LOCKS:
            with self.subTest(lock=lock.relative_to(REPO)):
                head = lock.read_text(encoding="utf-8").splitlines()[:2]
                self.assertEqual(head[1].strip(), "#    .github/scripts/python_locks.sh write")


class TheScriptWritesExactlyTheseLocks(unittest.TestCase):
    def test_the_calls(self):
        self.assertEqual(_script_locks(), LOCKS)

    def test_the_gui_build_keeps_the_runners_setuptools(self):
        # PyInstaller bundles from the build interpreter; the runner's own
        # setuptools stays what it was for every installer up to v2.37.0.
        self.assertIn('"--no-emit-package setuptools"', SCRIPT.read_text(encoding="utf-8"))
        self.assertNotIn("setuptools", _lock_entries(GUI / "requirements.lock"))

    def test_uv_runs_at_the_version_its_lock_pins(self):
        wanted = _requirements(REPO / ".github" / "scripts" / "uv-requirements.txt")
        self.assertEqual(set(wanted), {"uv"})
        op, version = wanted["uv"]
        self.assertEqual(op, "==")
        self.assertEqual(_lock_entries(REPO / ".github" / "scripts" / "uv-requirements.lock")["uv"][0], version)


class EveryInputIsInItsLock(unittest.TestCase):
    def test_pins_and_names(self):
        for lock, inputs in LOCKS.items():
            entries = _lock_entries(lock)
            for path in inputs:
                for name, (op, version) in _requirements(path).items():
                    with self.subTest(lock=lock.relative_to(REPO), requirement=name):
                        self.assertIn(name, entries)
                        if op == "==":
                            self.assertEqual(entries[name][0], version)

    def test_the_inputs_pin_what_matters_exactly(self):
        # The API ships its requirements.txt pins (CloudApiPinsFollowRequirements);
        # the GUI build pins PyInstaller; the build backend pins wheel. The one
        # range is modal's `>=1.6,<2` (app/tests/test_modal_pin_1_6.py), which
        # the lock pins exactly like every other package.
        ranged = {API / "requirements.txt": {"modal"}}
        for path in (API / "requirements.txt", GUI / "requirements-build.txt",
                     GUI / "requirements-build-backend.txt"):
            with self.subTest(input=path.relative_to(REPO)):
                for name, (op, _version) in _requirements(path).items():
                    if name not in ranged.get(path, set()):
                        self.assertEqual(op, "==", name)


class EveryInstallSiteUsesALock(unittest.TestCase):
    SITES = {
        "cloud_training_api/Dockerfile": (API / "Dockerfile").read_text(encoding="utf-8"),
        "railway-deploy-cloud-api.yml": _workflow_runs(WF / "railway-deploy-cloud-api.yml"),
        "release-installer.yml": _workflow_runs(WF / "release-installer.yml"),
        "ci.yml::python-locks": _workflow_runs(WF / "ci.yml", "python-locks"),
    }

    def test_require_hashes_only_binary_and_a_written_lock(self):
        written = {p.name for p in LOCKS}
        for site, text in self.SITES.items():
            installs = _pip_installs(text)
            with self.subTest(site=site):
                self.assertTrue(installs)
            for args in installs:
                with self.subTest(site=site, install=" ".join(args)):
                    self.assertIn("--require-hashes", args)
                    self.assertEqual(args[args.index("--only-binary") + 1], ":all:")
                    self.assertTrue(_requirement_file(args).endswith(".lock"))
                    self.assertIn(Path(_requirement_file(args)).name, written)
                    self.assertEqual(_positional(args), [], "nothing beside the lock")
                    self.assertEqual(args.count("-r"), 1)

    def test_the_api_image_copies_only_the_lock(self):
        text = (API / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY requirements.lock .", text)
        self.assertNotIn("requirements.txt", re.sub(r"(?m)^\s*#.*$", "", text))

    def test_only_proxy_tools_is_built_from_source(self):
        # The GUI lock's one source-only package; built without isolation, so
        # its backend (wheel) comes from the hash-checked build-backend lock,
        # which must be installed first.
        installs = _pip_installs(self.SITES["release-installer.yml"])
        self.assertEqual([_requirement_file(a) for a in installs],
                         ["requirements-build-backend.lock", "requirements.lock"])
        backend, gui = installs
        self.assertNotIn("--no-binary", backend)
        self.assertEqual(gui[gui.index("--no-binary") + 1], "proxy-tools")
        self.assertIn("--no-build-isolation", gui)
        self.assertIn("proxy-tools", _lock_entries(GUI / "requirements.lock"))
        self.assertIn("wheel", _lock_entries(GUI / "requirements-build-backend.lock"))


class PythonLocksJobRunsTheCheck(unittest.TestCase):
    def test_the_job(self):
        runs = _workflow_runs(WF / "ci.yml", "python-locks")
        self.assertIn("bash .github/scripts/python_locks.sh check", runs)


if __name__ == "__main__":
    unittest.main()
