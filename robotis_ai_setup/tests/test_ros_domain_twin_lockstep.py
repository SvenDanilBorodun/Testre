"""Cross-boundary lockstep for the ROS_DOMAIN_ID twins (deps-free, AST-only).

``_resolve_ros_domain_id`` and its helpers exist twice — once for Windows
(``gui/app/config_generator.py``) and once natively for the Orange Pi
(``pi_agent/config_generator.py``) — for the same reason as the three
``ROBOT_PROFILES`` copies and the two ``fast_rehydrate_arms`` twins: neither
platform can import the other (``gui/app`` is Windows/usbipd-bound, ``pi_agent``
ships as a standalone tarball with no import path to it). A lockstep test is the
correct coupling, not a shared module. This file AST-parses both — it cannot
import ``pi_agent`` any more than the production code can.

What is fenced is the CONTRACT, four properties that must hold on both sides:

  * both define ``_read_machine_id`` and ``_resolve_ros_domain_id``;
  * ``EDUBOTICS_ROS_DOMAIN`` is read before ANY statement that can return a
    value — the documented one-variable rollback must not be shadowed, on the
    Pi by the persisted read, on Windows by the derive;
  * the legal DDS range [0, 232] is clamped on both;
  * the hash seed is spelled ``_read_machine_id() or str(uuid.getnode())`` —
    the SAME EXPRESSION on both sides, so the twins read as twins.

Two differences are legitimate, and BOTH are asserted POSITIVELY below so they
read as intent rather than drift:

  * the SEED SOURCE. The Pi reads a FILE (``/etc/machine-id``, via
    ``constants.MACHINE_ID_FILE``); Windows has no such file and composes the
    identity from the ``MachineGuid`` REGISTRY value plus the computer name.
  * the CACHE. The Pi persists its resolved value; Windows deliberately does
    NOT, and its docstring carries the reason. Deleting the Pi's cache to
    "harmonise" the twins is the mistake that assertion exists to catch.

Statement ORDER, never string indices: ``ast.unparse`` keeps the docstring, and
both docstrings name the resolution order in prose, so an ``src.index(...)``
comparison always found the DOCSTRING's mention first and passed no matter how
the body was reordered. Reordering either twin's real body left the whole repo
green (2026-08-02).

Zero-file floor: every twin test in this repo carries one, because a directory
move would otherwise make the guard pass having read nothing (see
``test_arm_scan_twin_lockstep.py``, and the four ``ps_*`` guards).
"""

import ast
import unittest
from pathlib import Path

_SETUP_DIR = Path(__file__).resolve().parent.parent  # robotis_ai_setup/
_WINDOWS = _SETUP_DIR / "gui" / "app" / "config_generator.py"
_PI = _SETUP_DIR / "pi_agent" / "config_generator.py"

_TWINS = (("windows", _WINDOWS), ("pi", _PI))

# The symbols BOTH platforms must spread the resolution across. Written out
# rather than derived from one side so a change has to be made deliberately on
# BOTH — deriving would let the two agree on whatever one of them happened to
# say. The persist helpers are deliberately NOT here: they exist only on the Pi
# (see test_the_cache_divergence_is_intended_not_drift).
_EXPECTED_FUNCS = (
    "_read_machine_id",
    "_resolve_ros_domain_id",
)

# The Pi-only persist pair.
_PI_ONLY_FUNCS = (
    "_read_persisted_ros_domain_id",
    "_persist_ros_domain_id",
)

# The seed expression, spelled identically on both platforms.
_EXPECTED_SEED = "_read_machine_id() or str(uuid.getnode())"

_ENV_OVERRIDE = "EDUBOTICS_ROS_DOMAIN"
_DDS_MAX = 232

# Anything that could PERSIST the resolved domain, spelled as substrings of
# ``ast.unparse`` output. Matched against every function reachable from the
# Windows ``_resolve_ros_domain_id``, so the check survives a rename of the
# helpers. Registry writes are in the list because a cache under HKCU is still
# a cache, and it is the one storage a Windows-minded reader reaches for after
# being told not to use a file.
_PERSIST_TOKENS = (
    "open(", "os.replace", "os.rename", "os.makedirs", "os.remove",
    "read_text", "write_text", "read_bytes", "write_bytes", "Path(",
    "shelve", "pickle", "json.dump", "json.load",
    "SetValue", "CreateKey", "DeleteValue", "KEY_WRITE", "KEY_SET_VALUE",
)


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions(path: Path) -> dict:
    """{name: FunctionDef} for every top-level def in ``path``."""
    return {n.name: n for n in _module(path).body
            if isinstance(n, ast.FunctionDef)}


def _func(path: Path, name: str) -> ast.FunctionDef:
    fn = _functions(path).get(name)
    if fn is None:
        raise AssertionError(f"{name} not found in {path}")
    return fn


def _body_without_docstring(fn: ast.FunctionDef) -> list:
    """The function's top-level statements with a leading docstring dropped.

    THE reason this helper exists: ``ast.unparse(fn)`` KEEPS the docstring, so
    any assertion phrased as "symbol A appears before symbol B in the source"
    is really asserting something about the prose."""
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _docstring(fn: ast.FunctionDef) -> str:
    return ast.get_docstring(fn) or ""


def _source_of(path: Path, func: str) -> str:
    """Unparsed BODY of one top-level def — docstring excluded on purpose."""
    return "\n".join(ast.unparse(st) for st in
                     _body_without_docstring(_func(path, func)))


def _reachable_from(path: Path, root: str) -> dict:
    """{name: body-source} for ``root`` and every module-local function it can
    transitively call.

    A name-based "these helpers must not exist" check only catches a cache that
    comes back under the names it was deleted under; a persist re-added as
    ``_load_cached_domain`` / ``_store_cached_domain``, with the ``open()``
    inside the HELPERS rather than in the resolver body, satisfies every such
    check. Following the call graph is what makes the assertion about the
    BEHAVIOUR instead of the vocabulary."""
    funcs = _functions(path)
    seen: set = set()
    stack = [root]
    while stack:
        name = stack.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        for node in ast.walk(funcs[name]):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in funcs):
                stack.append(node.func.id)
    return {n: _source_of(path, n) for n in sorted(seen)}


class TestRosDomainTwinLockstep(unittest.TestCase):

    # ── zero-file floor ──────────────────────────────────────────────────
    def test_both_twins_were_actually_found_and_parsed(self):
        """A guard that cannot fail is worse than no guard."""
        for label, path in _TWINS:
            with self.subTest(label):
                self.assertTrue(path.is_file(),
                                f"{path} is missing — did a directory move? "
                                "Every assertion below would pass vacuously")
                funcs = _functions(path)
                self.assertTrue(funcs,
                                f"{path} parsed to ZERO top-level functions")
                # ... and the function under test must have a real body: an
                # emptied resolver would satisfy every "X appears before Y"
                # assertion below by having neither.
                body = _body_without_docstring(funcs["_resolve_ros_domain_id"])
                self.assertGreaterEqual(
                    len(body), 2,
                    f"{path}::_resolve_ros_domain_id has {len(body)} "
                    "statement(s) outside its docstring — the ordering "
                    "assertions cannot mean anything")

    # ── 1. the shared symbols exist on both sides ────────────────────────
    def test_both_twins_define_the_same_shared_symbols(self):
        for label, path in _TWINS:
            with self.subTest(label):
                names = set(_functions(path))
                missing = [f for f in _EXPECTED_FUNCS if f not in names]
                self.assertEqual(
                    missing, [],
                    f"{path} is missing {missing} — the ROS_DOMAIN_ID "
                    "resolution must be spread across the same symbols on "
                    "both platforms")

    # ── 2. the env rollback is read FIRST ────────────────────────────────
    def test_both_twins_check_the_env_override_before_anything_else(self):
        """``EDUBOTICS_ROS_DOMAIN`` is the documented one-variable rollback, so
        nothing that can RETURN A VALUE may run before it is read — on the Pi
        that means before the persisted read, on Windows before the derive.

        Phrased as statement ORDER over the docstring-stripped body. The
        previous string-index form compared positions inside ``ast.unparse``
        output, which begins with the docstring, and both docstrings spell out
        "EDUBOTICS_ROS_DOMAIN env override → …" — so the first hit was always
        the prose and reordering EITHER twin's body left the test green."""
        for label, path in _TWINS:
            with self.subTest(label):
                body = _body_without_docstring(
                    _func(path, "_resolve_ros_domain_id"))
                env_at = [i for i, st in enumerate(body)
                          if _ENV_OVERRIDE in ast.unparse(st)]
                returns_at = [i for i, st in enumerate(body)
                              if any(isinstance(n, ast.Return)
                                     for n in ast.walk(st))]
                self.assertTrue(env_at, "the env override branch is gone")
                self.assertTrue(returns_at,
                                "_resolve_ros_domain_id returns nothing")
                self.assertLess(
                    min(env_at), min(returns_at),
                    f"{path}: a statement that can return a value runs BEFORE "
                    f"{_ENV_OVERRIDE} is read — the documented rollback no "
                    "longer wins")

    # ── 3. the legal DDS range is enforced on both sides ─────────────────
    def test_both_twins_clamp_the_override_to_the_legal_dds_range(self):
        """[0, 232] is the DDS domain range; a value outside it makes the whole
        stack unroutable. Both twins clamp the override to it."""
        for label, path in _TWINS:
            with self.subTest(label):
                resolve = _source_of(path, "_resolve_ros_domain_id")
                self.assertIn(str(_DDS_MAX), resolve,
                              "the override clamp lost its upper bound")

    # ── 4. one seed expression, spelled identically ──────────────────────
    def test_both_twins_seed_the_hash_from_the_same_expression(self):
        """``_read_machine_id() or str(uuid.getnode())`` — same words on both
        platforms. The Windows twin used to seed from ``uuid.getnode()`` alone,
        which is unstable on multi-NIC / VPN / docking-station PCs; matching the
        Pi's spelling is what makes the two readable as twins at a glance."""
        for label, path in _TWINS:
            with self.subTest(label):
                src = _source_of(path, "_resolve_ros_domain_id")
                self.assertIn(
                    _EXPECTED_SEED, src,
                    f"{path} does not seed the hash from "
                    f"`{_EXPECTED_SEED}` — keep the two spellings identical")

    # ── divergence 1: the seed SOURCE, asserted positively ───────────────
    def test_the_seed_source_divergence_is_intended_not_drift(self):
        """The twins get their machine identity from DIFFERENT places, and that
        is correct: the Pi has ``/etc/machine-id``, Windows does not.

        Asserting it here means a future reader sees an intended difference
        rather than wondering whether one side fell behind — and it fails loudly
        if someone "harmonises" one platform onto the other's mechanism."""
        pi_src = _source_of(_PI, "_read_machine_id")
        self.assertIn("MACHINE_ID_FILE", pi_src,
                      "the Pi twin no longer reads /etc/machine-id")
        self.assertIn("open(", pi_src,
                      "the Pi twin's identity must come from a FILE read")

        win_src = _source_of(_WINDOWS, "_read_machine_id")
        self.assertIn("winreg", win_src,
                      "the Windows twin no longer probes the registry")
        self.assertIn("MachineGuid", win_src,
                      "the Windows twin lost its MachineGuid probe")
        self.assertIn("ActiveComputerName", win_src,
                      "the Windows twin lost the hostname half of its "
                      "composite identity — MachineGuid alone survives a "
                      "non-generalized disk clone, which is the collision the "
                      "composite exists to break")
        self.assertNotIn("MACHINE_ID_FILE", win_src,
                         "Windows has no /etc/machine-id equivalent to read")

    # ── divergence 2: the CACHE, asserted positively ─────────────────────
    def test_the_cache_divergence_is_intended_not_drift(self):
        """The Pi persists its resolved domain id. Windows deliberately does
        NOT, and BOTH halves of that are load-bearing.

        Windows has no path a golden image does not copy — ``%LOCALAPPDATA%``
        travels with a roaming/FSLogix/redirected profile and ``%ProgramData%``
        is captured by the image just the same — so a cache there has to be
        either copy-proof or a stabiliser for the ``uuid.getnode()`` fallback,
        and it cannot be both: the fingerprint that would make it copy-proof IS
        ``_read_machine_id()``, which is exactly what is missing whenever
        getnode() is the seed. Copy-proof it and it can only return what
        re-deriving returns.

        The Pi's ``/var/lib/edubotics/.ros_domain_id`` is machine-local, cannot
        travel, and legitimately stabilises the Pi's own getnode() fallback —
        so deleting it in the name of symmetry would be a regression, not a
        cleanup."""
        pi_names = set(_functions(_PI))
        for name in _PI_ONLY_FUNCS:
            self.assertIn(name, pi_names,
                          f"the Pi twin lost {name} — its cache is machine-"
                          "local and is NOT the Windows problem")
        pi_resolve = _source_of(_PI, "_resolve_ros_domain_id")
        self.assertIn("_read_persisted_ros_domain_id", pi_resolve)
        self.assertIn("_persist_ros_domain_id", pi_resolve)
        self.assertIn(str(_DDS_MAX),
                      _source_of(_PI, "_read_persisted_ros_domain_id"),
                      "the Pi's persisted-value range check lost its bound")

        win_names = set(_functions(_WINDOWS))
        for name in _PI_ONLY_FUNCS + ("_machine_fingerprint",):
            self.assertNotIn(
                name, win_names,
                f"{name} is back on the Windows twin — read the no-cache "
                "paragraph in _resolve_ros_domain_id's docstring first")

        # ... and the same thing said in a way a RENAME cannot slip past: no
        # function the Windows resolver can reach may touch a file or write the
        # registry. The name check above only knows the vocabulary of the cache
        # that was deleted.
        reachable = _reachable_from(_WINDOWS, "_resolve_ros_domain_id")
        self.assertIn("_read_machine_id", reachable,
                      "the call-graph walk found nothing — it would pass "
                      "vacuously")
        for name, src in reachable.items():
            for token in _PERSIST_TOKENS:
                self.assertNotIn(
                    token, src,
                    f"{_WINDOWS}::{name} contains `{token}` and is reachable "
                    "from _resolve_ros_domain_id — the Windows twin is caching "
                    "again. Read the no-cache paragraph in the resolver's "
                    "docstring before re-adding one under any name.")

        # The absence is the non-obvious part, so the reason must travel with
        # it — otherwise the next reader re-adds the cache on first principles.
        doc = _docstring(_func(_WINDOWS, "_resolve_ros_domain_id"))
        self.assertIn("NO CACHE", doc,
                      "the Windows resolver no longer says it has no cache")
        self.assertIn("%LOCALAPPDATA%", doc,
                      "the no-cache rationale lost the roaming-path reason")
        self.assertIn("PI TWIN KEEPS ITS CACHE", doc,
                      "the docstring no longer tells the reader NOT to delete "
                      "the Pi's cache for symmetry")


if __name__ == "__main__":
    unittest.main()
