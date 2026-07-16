"""Rule §5 LeRobot pin lockstep across all five install sites (deps-free).

CLAUDE.md Rule §5 says the LeRobot version pin "must agree across" five files
and that bumping it is "a one-PR multi-site change". Nothing enforced that.
Before this test the repo had:

  - no test that reads ANY Dockerfile (the five test suites all stub lerobot
    out via sys.modules, so none of them can see a pin);
  - image-source-parity, which DELETES all three base Dockerfiles before it
    diffs, so its byte-equality check is blind to them by construction;
  - docker-publish's smoke-test, which asserts the image has ROS and is under a
    size ceiling but never asserts a package version.

The gap that matters most is modal_app.py, because it is the one site whose
drift is invisible until a student pays for it: the trainer and the recorder
must agree on the dataset codebase version, so a modal-only bump surfaces at
TRAINING time as the German preflight error from training_handler.py — after
the student recorded a dataset, queued a job and spent GPU credit.

This is a TEXT test on purpose. It cannot import lerobot (CI's python-tests
installs only pyyaml) and it must not need docker. It scrapes the pins the same
way a reviewer would read them, and it is deliberately strict about
`[pi,smolvla,peft]`: `pi0` is NOT a real lerobot extra, pip treats an unknown
extra as a non-fatal WARNING, and the only thing `[pi]` uniquely contributes is
scipy — which every one of these files also pins explicitly, so the mistake is
invisible until someone "tidies up" the redundant-looking scipy pin and
pi0_fast inference starts raising TypeError on idct=None.

Comment lines are stripped before scraping. That is not cosmetic: these files
carry comments that mention `lerobot[pi]`, `scipy>=1.14.0,<2.0.0` and
`numpy==1.26.4` while describing history, and a naive grep matches those and
passes while the real install line says something else entirely.
"""

from pathlib import Path
import re
import unittest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[1]
_PAS_DIR = _REPO_ROOT / "physical_ai_tools" / "physical_ai_server"

# The five Rule §5 sites, keyed by the repo-relative label used in failures.
MODAL_SITE = "robotis_ai_setup/modal_training/modal_app.py"
SITES = {
    MODAL_SITE: _REPO_ROOT / "robotis_ai_setup" / "modal_training" / "modal_app.py",
    "physical_ai_tools/physical_ai_server/Dockerfile.amd64": _PAS_DIR / "Dockerfile.amd64",
    "physical_ai_tools/physical_ai_server/Dockerfile.arm64": _PAS_DIR / "Dockerfile.arm64",
    "physical_ai_tools/physical_ai_server/Dockerfile.arm64cpu": _PAS_DIR / "Dockerfile.arm64cpu",
    "robotis_ai_setup/docker/physical_ai_server/Dockerfile": (
        _REPO_ROOT / "robotis_ai_setup" / "docker" / "physical_ai_server" / "Dockerfile"
    ),
}
# The four SERVER images. Everything except the Modal worker — the numpy floor
# applies here and ONLY here (see test_numpy_floor_*).
SERVER_SITES = [name for name in SITES if name != MODAL_SITE]

# Expected values. These are ASSERTED absolutely, not just cross-checked, so a
# LeRobot bump has to come here and tick the list off — which is exactly the
# "one-PR multi-site change" Rule §5 asks for. If you are bumping deliberately,
# update these AND the two derived consts that move with the pin:
# meta/info.json codebase_version and training_handler.EXPECTED_CODEBASE_VERSION.
EXPECTED_LEROBOT_VERSION = "0.5.1"
EXPECTED_EXTRAS = "pi,smolvla,peft"
EXPECTED_ACCELERATE = "accelerate>=1.10.0,<2.0.0"
EXPECTED_SCIPY = "scipy>=1.14.0,<1.18"
EXPECTED_NUMPY = "numpy==1.26.4"

_RULE5 = (
    "CLAUDE.md Rule §5: the LeRobot pin must agree across all five sites and "
    "moves in ONE PR. Divergence here ships a student image whose lerobot "
    "disagrees with the Modal trainer's."
)

_LEROBOT_RE = re.compile(r"lerobot\[([^\]]+)\]==([^\"'\s]+)")
_ACCELERATE_RE = re.compile(r"accelerate\s*(>=[^\"'\s]+)")
_SCIPY_RE = re.compile(r"scipy\s*(>=[^\"'\s]+)")
_NUMPY_RE = re.compile(r"numpy==([0-9][^\"'\s]*)")
_MODAL_VERSION_CONST_RE = re.compile(r"^LEROBOT_VERSION\s*=\s*[\"']([^\"']+)[\"']", re.M)


def _code_text(path: Path) -> str:
    """File contents with pure-comment lines removed.

    Both Dockerfiles and Python use `#`, and a stripped-leading-# line is a
    comment in either. Inline `#` inside a shell RUN continuation is a comment
    too and is dropped by the same rule. We do NOT try to strip trailing
    comments after code — no pin line in these files has one, and a naive
    split('#') would corrupt URLs like .../whl/cpu#egg.
    """
    kept = [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if not ln.strip().startswith("#")]
    return "\n".join(kept)


class LeRobotPinLockstepTest(unittest.TestCase):
    """Every Rule §5 pin site agrees, and agrees with the documented values."""

    @classmethod
    def setUpClass(cls):
        cls.code = {}
        for name, path in SITES.items():
            if not path.is_file():
                raise AssertionError(
                    f"Rule §5 site missing: {name} ({path}). If a site was "
                    f"renamed or removed, update SITES here AND Rule §5 in "
                    f"CLAUDE.md in the same change."
                )
            cls.code[name] = _code_text(path)

    def _lerobot_pin(self, name):
        """(extras, version) for a site, resolving modal_app's f-string."""
        m = _LEROBOT_RE.search(self.code[name])
        self.assertIsNotNone(
            m, f"{name}: no `lerobot[...]==<version>` install line found. {_RULE5}"
        )
        extras, version = m.group(1), m.group(2)
        if version.startswith("{") and version.endswith("}"):
            # modal_app.py builds the spec as f"lerobot[...]=={LEROBOT_VERSION}".
            const = _MODAL_VERSION_CONST_RE.search(self.code[name])
            self.assertIsNotNone(
                const,
                f"{name}: install line interpolates {version} but no "
                f"`LEROBOT_VERSION = \"...\"` constant was found. {_RULE5}",
            )
            version = const.group(1)
        return extras, version

    def test_lerobot_version_agrees_across_all_five_sites(self):
        found = {name: self._lerobot_pin(name)[1] for name in SITES}
        divergent = sorted(n for n, v in found.items() if v != EXPECTED_LEROBOT_VERSION)
        self.assertFalse(
            divergent,
            f"LeRobot version drift. Expected {EXPECTED_LEROBOT_VERSION!r} at every "
            f"site; got {found!r}. Divergent: {divergent}. {_RULE5} If this is an "
            f"intentional bump, change EXPECTED_LEROBOT_VERSION here too, and move "
            f"the derived consts with it (meta/info.json codebase_version + "
            f"training_handler.EXPECTED_CODEBASE_VERSION).",
        )

    def test_extras_are_exactly_pi_smolvla_peft(self):
        # Guards the `[pi0]` trap in CODE, not just in the comments that F5
        # fixed. `pi0` is not a real extra: pip warns and moves on, silently
        # dropping scipy — masked today only by the explicit scipy pin below.
        found = {name: self._lerobot_pin(name)[0] for name in SITES}
        divergent = sorted(n for n, e in found.items() if e != EXPECTED_EXTRAS)
        self.assertFalse(
            divergent,
            f"LeRobot extras drift. Expected [{EXPECTED_EXTRAS}] at every site; got "
            f"{found!r}. Divergent: {divergent}. NOTE: `pi0` is NOT a declared extra "
            f"of lerobot 0.5.1 — pip accepts it with only a warning and drops scipy, "
            f"which breaks pi0_fast inference (idct=None -> TypeError). The extra is "
            f"`pi`. {_RULE5}",
        )

    def test_accelerate_constraint_agrees_across_all_five_sites(self):
        found = {}
        for name in SITES:
            m = _ACCELERATE_RE.search(self.code[name])
            self.assertIsNotNone(m, f"{name}: no accelerate constraint found. {_RULE5}")
            found[name] = f"accelerate{m.group(1)}"
        divergent = sorted(n for n, v in found.items() if v != EXPECTED_ACCELERATE)
        self.assertFalse(
            divergent,
            f"accelerate constraint drift. Expected {EXPECTED_ACCELERATE!r}; got "
            f"{found!r}. Divergent: {divergent}. {_RULE5}",
        )

    def test_scipy_cap_agrees_across_all_five_sites(self):
        # The <1.18 cap is load-bearing on the four server images: scipy 1.18.0
        # dropped numpy-1.x support and references np.long, which explodes
        # against the numpy==1.26.4 cv_bridge floor (it broke the v2.9.2 build).
        # Modal runs numpy 2.x so it would survive a widen — which is precisely
        # why it is pinned there too: identical inputs, reproducible resolves.
        found = {}
        for name in SITES:
            m = _SCIPY_RE.search(self.code[name])
            self.assertIsNotNone(m, f"{name}: no scipy constraint found. {_RULE5}")
            found[name] = f"scipy{m.group(1)}"
        divergent = sorted(n for n, v in found.items() if v != EXPECTED_SCIPY)
        self.assertFalse(
            divergent,
            f"scipy constraint drift. Expected {EXPECTED_SCIPY!r}; got {found!r}. "
            f"Divergent: {divergent}. Do NOT widen back to <2.0.0 while numpy is "
            f"floored at 1.26.4. {_RULE5}",
        )

    def test_numpy_floor_pinned_in_every_server_image(self):
        # numpy==1.26.4 is the cv_bridge ABI floor: cv_bridge_boost.so is built
        # against NumPy 1.x and numpy 2.x SIGSEGVs the rgb8 decode path used by
        # BOTH recording and inference — an uncatchable crash that kills the ROS
        # node on the first camera frame.
        missing, wrong = [], {}
        for name in SERVER_SITES:
            m = _NUMPY_RE.search(self.code[name])
            if m is None:
                missing.append(name)
            elif f"numpy=={m.group(1)}" != EXPECTED_NUMPY:
                wrong[name] = f"numpy=={m.group(1)}"
        self.assertFalse(
            missing,
            f"numpy floor missing from server image(s): {missing}. Expected "
            f"{EXPECTED_NUMPY} — without it lerobot pulls numpy>=2 and the "
            f"cv_bridge rgb8 decode SEGFAULTS on the first camera frame. {_RULE5}",
        )
        self.assertFalse(
            wrong,
            f"numpy floor drift: {wrong!r}, expected {EXPECTED_NUMPY} "
            f"(cv_bridge ABI). {_RULE5}",
        )

    def test_numpy_floor_absent_from_modal_worker(self):
        # The inverse assertion, and it is not pedantry: the Modal worker has no
        # cv_bridge, so it has no reason to carry the 1.x floor — and lerobot
        # 0.5.1 REQUIRES numpy>=2.0. Copying the floor across "for consistency"
        # would make the trainer image unresolvable. Rule §5 calls numpy
        # correctly absent there; this keeps it that way.
        m = _NUMPY_RE.search(self.code[MODAL_SITE])
        self.assertIsNone(
            m,
            f"{MODAL_SITE} pins numpy ({m.group(0) if m else ''}). It must NOT: "
            f"the numpy==1.26.4 floor exists only for the server images' cv_bridge "
            f"ABI, there is no cv_bridge on Modal, and lerobot 0.5.1 requires "
            f"numpy>=2.0 — pinning 1.x here makes the resolve unsatisfiable.",
        )


if __name__ == "__main__":
    unittest.main()
