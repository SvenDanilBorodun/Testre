#!/usr/bin/env python3
"""A test may not restate the implementation — the mechanical half.

THE RULE (B-0). For every value the code CHOOSES, at least one assertion in the
suite must be written with a LITERAL. Never the symbol under test on both sides
of an assertion, and never the symbol as the EXPECTED side of an assertion whose
ACTUAL side was produced by running the code that reads it.

Measured on this tree, all five mutations green across the FULL suites:

    MAX_BROADCAST_BACKLOG      32 -> 1 000 000   SURVIVED (1482 passed / 4 skipped)
    MAX_HAT_CONSECUTIVE_ERRORS  5 -> 1           SURVIVED
    MAX_HAT_HANDLERS           16 -> 0           SURVIVED both boundary tests
    _MAX_VAR_PAYLOAD_CHARS   2000 -> 4500        SURVIVED pytest AND vitest
    TAP_COUNT_FALLBACK (JS)     4 -> 3           SURVIVED all 1071 vitest tests

`test_the_broadcast_backlog_is_bounded` was the worked example: production wrote
``consumed[tid] = count - MAX_BROADCAST_BACKLOG`` and the assertion was
``consumed[tid] == count - MAX_BROADCAST_BACKLOG``, seeded ``count = cap * 10``,
which makes the `else` branch unreachable for EVERY positive cap.

WHICH PIN SHAPE YOU NEED depends on whether the constant is env-derived:

  a plain literal (``MAX_BROADCAST_BACKLOG = 32``)
      ``assert WM.MAX_BROADCAST_BACKLOG == 32``
  ENV-DERIVED (``WHILE_EMPTY_FRAMES = _env_int('EDUBOTICS_…', 3)``)
      a SOURCE FENCE — regex the DEFAULT out of the module text. The attribute
      is contaminated by the environment and monkeypatched by half the suite, so
      ``assert M.X == 3`` passes with the env var set.
  a NON-NUMERIC decision (an exception class, a branch, a message)
      an assertion naming the decision plus a behavioural PAIR — see
      ``test_exception_class_is_a_decision.py``. ``raises(Base)`` cannot tell a
      subclass apart.

HONEST WEAKNESS #1, and it is the BIGGER one: this guard implements
``referenced => pinned``, not the RULE above. A constant NO test mentions at all
is outside it entirely — measured on this tree, 83 of 167 production numeric
constants, 46 of them in the Roboter-Studio files. Adding
``NEUE_UNGEPINNTE_KONSTANTE = 7`` to workflow_manager.py passes all four tests in
0.46 s. That is by construction (the guard cannot demand a pin for a constant it
cannot name a home for), but it means a GREEN run here says „every constant a
test already touches is pinned", never „every constant is pinned". Three
survivors found by mutating the blind spot are now pinned above and in
``test_dof_n6::test_path_guard_inflation_is_not_secretly_shrunk``.

HONEST WEAKNESS #2. The source-fence detector credits any constant NAME followed
by ``=`` inside a string literal in a test, so it OVER-credits: the allowlist is
an optimistic lower bound on the real gap. Tightening it to "a regex literal that names the
constant AND captures a number" is a follow-up, not a blocker — a loose detector
that flags dozens of real gaps is worth more than an exact one that ships next
month.

THE ALLOWLIST MAY ONLY SHRINK. Every entry carries a written reason and a STALE
entry is a hard error, exactly like ``compose_env_parity.INTENTIONAL`` and
``sessionScope.IGNORED_STORAGE_KEYS``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import physical_ai_server


_PKG = Path(physical_ai_server.__file__).parent
_TESTS = Path(__file__).parent
_NAME_RE = re.compile(r'^_?[A-Z][A-Z0-9_]*$')
_NUMERIC_WRAPPERS = {'max', 'min', 'int', 'float', 'round',
                     '_env_int', '_env_float', '_safe_float'}

_TIER2 = ('Tier 2 — referenced by a test but never literal-pinned. Pin it the '
          'next time this constant is touched; this list may only shrink.')

# name -> why it is not pinned yet. See _TIER2 for the bulk reason.
ALLOWLIST: dict[str, str] = {
    # ── workflow/handlers/motion.py ──────────────────────────────────────
    'DEFAULT_APPROACH_HEIGHT_M': _TIER2,
    'DEFAULT_HOME_DURATION_S': _TIER2,
    'DEFAULT_MOVE_DURATION_S': _TIER2,
    'DROP_HEIGHT_M': _TIER2,
    'GRASP_HELD_MARGIN_RAD': _TIER2,
    'GRASP_HELD_MAX_RAD': _TIER2,
    'GRASP_HELD_MIN_TRAVEL_FRAC': _TIER2,
    'GRIPPER_CLOSED_RAD': (
        'profile-owned: the shipped value per arm is pinned by '
        'test_robot_profiles / test_robot_profile_lockstep, and the motion '
        'module constant is the OMX mirror of it. ' + _TIER2),
    'GRIPPER_OPEN_RAD': (
        'profile-owned, same as GRIPPER_CLOSED_RAD. ' + _TIER2),
    'PICKUP_CLOSE_RAD': _TIER2,
    '_HELD_BLOCK_OFFSET_RAD': _TIER2,
    '_MAX_STRETCHED_DURATION_S': _TIER2,
    '_TEMPO_MAX': _TIER2,
    '_TEMPO_MIN': _TIER2,
    # ── workflow/handlers/perception_blocks.py ───────────────────────────
    '_RECLAIM_ABSENT_S': _TIER2,
    '_TAG_EDGE_TOL_FRAC': _TIER2,
    '_TAG_YAW_FRAME_INTERVAL_S': _TIER2,
    '_TAG_YAW_MIN_RESULTANT': _TIER2,
    # ── workflow/handlers/trajectory.py ──────────────────────────────────
    'REPLAY_SPEED_MAX': _TIER2,
    'REPLAY_SPEED_MIN': _TIER2,
    # ── workflow/interpreter.py ──────────────────────────────────────────
    'WHILE_EMPTY_SECONDS': _TIER2,
    'WHILE_MAX_SECONDS': _TIER2,
    'WHILE_SETTLE_S': _TIER2,
    'WHILE_STALL_PASSES': _TIER2,
    '_COUNTER_MAX': _TIER2,
    # ── workflow/calibration_manager.py ──────────────────────────────────
    'EXTRINSIC_BURST_FRAMES': _TIER2,
    'EXTRINSIC_BURST_INTERVAL_S': _TIER2,
    'EXTRINSIC_BURST_MIN_PASS': _TIER2,
    'TABLE_TOUCH_MAX_TILT_DEG': _TIER2,
    'VERIFY_SCALE_MAX': _TIER2,
    # ── geometry / kinematics: pinned by the golden fixtures instead ─────
    'BASE_AXIS_X_WORLD': (
        'kinematics: any drift shows up as a golden-fixture failure '
        '(test/fixtures/dof_golden.json, tolerance 1e-9) long before a literal '
        'pin would. ' + _TIER2),
    '_FK_TOL_M': (
        'a SOLVER tolerance, exercised by every exact-FK assertion in '
        'test_edu1_ik / test_edu6_ik. ' + _TIER2),
    '_L_TOOL': (
        'kinematics, pinned by the golden fixtures. ' + _TIER2),
    '_R_MAX_M': (
        'kinematics, pinned by the golden fixtures. ' + _TIER2),
    '_QUINTIC_PEAK_VELOCITY_FACTOR': (
        'the quintic peak factor 15/8 is a property of the polynomial, not a '
        'choice — it is pinned by the velocity-floor assertions. ' + _TIER2),
    '_VELOCITY_SAFETY_FRACTION': _TIER2,
    '_MAX_SEGMENT_SAMPLES': _TIER2,
    # ── workflow/path_guard.py + home_planner.py ─────────────────────────
    'SAFE_TRAVEL_Z': (
        'profile-driven (ArmProfile.safe_travel_z_m); the module constant is '
        'the OMX default. ' + _TIER2),
    'STEP_M': _TIER2,
    '_CLEAR_EPS_M': _TIER2,
    '_TOOL_CLEAR_M': (
        'profile-driven (ArmProfile.tool_clear_m). ' + _TIER2),
    'FLOOR_TOL_M': _TIER2,
    'WORKSPACE_FLOOR_MARGIN_M': _TIER2,
    # ── node / manager ───────────────────────────────────────────────────
    '_MANUAL_TORQUE_FAIL_LIMIT': _TIER2,
    '_REINIT_MAX_ATTEMPTS': _TIER2,
    '_REINIT_RETRY_PERIOD_S': _TIER2,
    'MAX_SIM_OBJECTS': _TIER2,
    'MAX_WORKFLOW_JSON_BYTES': _TIER2,
}


# ── the Tier-1 pins this round owns ─────────────────────────────────────────
# SOURCE fences, not attribute reads: several of these are env-derived, and the
# env-derived ones are exactly where an attribute read measures the environment
# instead of the shipped default. One file, one shape, so a value moving is one
# obvious diff.
_SHIPPED_DEFAULTS = [
    ('workflow/interpreter.py', 'MAX_LIST_ITEMS', '1000'),
    ('workflow/interpreter.py', 'WAIT_UNTIL_MAX_SECONDS', '300.0'),
    ('workflow/interpreter.py', '_MAX_VAR_PAYLOAD_CHARS', '2000'),
    ('workflow/interpreter.py', '_MAX_VAR_PAYLOAD_ITEMS', '200'),
    ('workflow/interpreter.py', 'MAX_LOOP_ITERATIONS', '10000'),
    # New this round, pinned on the way in rather than after a mutation sweep —
    # the C2-1 lesson: a constant no test NAMES is outside this guard entirely.
    ('workflow/student_text.py', 'STUDENT_TEXT_MAX_ITEMS', '200'),
    ('workflow/student_text.py', 'STUDENT_TEXT_MAX_DEPTH', '5'),
    ('workflow/handlers/output.py', 'MAX_LOG_CHARS', '2000'),
    ('workflow/handlers/output.py', 'MAX_TOAST_CHARS', '240'),
    ('workflow/handlers/output.py', 'OUTPUT_BURST', '50'),
    # 2.5x the React Protokoll's own 200-line cap — see OUTPUT_BURST_LOG's
    # comment. At 50 a „wiederhole 100 mal { melde }" looked like it stopped.
    ('workflow/handlers/output.py', 'OUTPUT_BURST_LOG', '500'),
    ('workflow/handlers/motion.py', 'MOTION_LOCK_NOTICE_S', '10.0'),
    ('workflow/handlers/motion.py', '_MOTION_LOCK_POLL_S', '0.05'),
    ('workflow/handlers/motion.py', '_APPROACH_WARN_FRAC', '0.25'),
    ('workflow/handlers/motion.py', 'GRASP_SETTLE_MAX_S', '2.0'),
    ('workflow/handlers/perception_blocks.py', '_TAG_YAW_FRAMES_MAX', '30'),
    ('workflow/calibration_manager.py', 'VERIFY_MIN_POINTS', '3'),
    # Both added after a mutation sweep found them SURVIVING the full suite:
    # no test named either, so `referenced => pinned` never saw them.
    # INTRINSIC 20 -> 1 fits the RATIONAL_MODEL's 8 distortion terms from one
    # view, reports success, and silently wrongs every later grasp on that rig.
    # REPROJ 1.5 -> 1e9 passes every frame of the extrinsic burst, including a
    # mirrored or 90deg-rotated solve _check_extrinsic_orientation cannot catch.
    ('workflow/calibration_manager.py', 'INTRINSIC_FRAMES_REQUIRED', '20'),
    ('workflow/calibration_manager.py', 'SCENE_EXTRINSIC_MAX_REPROJ_PX', '1.5'),
    ('workflow/calibration_manager.py', 'TABLE_TOUCH_POINTS_REQUIRED', '4'),
    ('workflow/calibration_manager.py', 'TABLE_TOUCH_MAX_RESIDUAL_M', '0.008'),
    ('workflow/workflow_manager.py', 'MAX_BROADCAST_BACKLOG', '32'),
    ('workflow/workflow_manager.py', 'MAX_HAT_CONSECUTIVE_ERRORS', '5'),
    ('workflow/workflow_manager.py', 'MAX_HAT_HANDLERS', '16'),
    ('workflow/workflow_manager.py', 'HAT_KEEPALIVE_MAX_S', '300.0'),
    ('workflow/workflow_manager.py', 'HAT_MIN_CYCLE_S', '0.05'),
    # The Sammlung's per-run cap on the payload ``destinations`` sibling — the
    # bound on what a crafted /workflow/start can put into ctx.destinations.
    ('workflow/workflow_manager.py', 'MAX_PAYLOAD_DESTINATIONS', '64'),
    # Promoted OUT of the ALLOWLIST: it is the number the velocity floor
    # extends every segment against, so a silent drift changes how fast the
    # arm is allowed to move on every path in the package.
    ('workflow/trajectory_builder.py', 'JOINT_VELOCITY_LIMIT_RAD_S', '4.8'),
]

# env-derived: the DEFAULT is what ships, so the fence names the env var too.
_SHIPPED_ENV_DEFAULTS = [
    ('workflow/interpreter.py', 'WHILE_EMPTY_FRAMES', 'EDUBOTICS_WHILE_EMPTY_FRAMES', '3'),
    ('workflow/handlers/output.py', 'OUTPUT_MAX_PER_S', 'EDUBOTICS_OUTPUT_MAX_PER_S', '5.0'),
    ('workflow/handlers/perception_blocks.py', '_RECLAIM_MOVE_M', 'EDUBOTICS_RECLAIM_MOVE_M', '0.02'),
    ('workflow/handlers/perception_blocks.py', '_TAG_YAW_FRAMES', 'EDUBOTICS_TAG_YAW_FRAMES', '7.0'),
    ('workflow/handlers/motion.py', 'GRASP_RETRY', 'EDUBOTICS_GRASP_RETRY', '1.0'),
    ('workflow/handlers/motion.py', 'GRASP_SETTLE_S', 'EDUBOTICS_GRASP_SETTLE_S', '0.3'),
]


def _src(rel: str) -> str:
    return (_PKG / rel).read_text(encoding='utf-8')


def test_the_shipped_numeric_defaults_are_the_numbers_we_chose():
    """LITERAL pins for every constant this round introduced or touched."""
    for rel, name, literal in _SHIPPED_DEFAULTS:
        m = re.search(rf'^{name}\s*=\s*([-0-9_.]+)\s*(?:#.*)?$', _src(rel), re.M)
        assert m, f'{rel}::{name} moved or stopped being a plain literal'
        assert m.group(1) == literal, (
            f'{rel}::{name} ships as {m.group(1)}, not {literal}')


def test_the_shipped_env_knob_defaults_are_the_numbers_we_chose():
    """Same, for the env-derived ones — where an attribute read would measure
    the environment rather than what ships."""
    for rel, name, env, literal in _SHIPPED_ENV_DEFAULTS:
        src = _src(rel)
        m = re.search(rf"'{env}',\s*([-0-9_.]+)\s*\)", src)
        assert m, f'{rel}::{name} no longer reads {env} with a literal default'
        assert m.group(1) == literal, (
            f'{rel}::{name} defaults to {m.group(1)}, not {literal}')


# ── the detector ────────────────────────────────────────────────────────────

def _is_numeric(node) -> bool:
    if (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_numeric(node.operand)
    if isinstance(node, ast.Call):
        fn = node.func
        name = (fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute) else '')
        return name in _NUMERIC_WRAPPERS
    if isinstance(node, ast.BinOp):
        return _is_numeric(node.left) or _is_numeric(node.right)
    return False


def _production_constants() -> dict[str, str]:
    found: dict[str, str] = {}
    for p in sorted(_PKG.rglob('*.py')):
        try:
            tree = ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
        except SyntaxError:                       # pragma: no cover
            continue
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            for t in targets:
                if (isinstance(t, ast.Name) and _NAME_RE.match(t.id)
                        and _is_numeric(value)):
                    found.setdefault(t.id, p.name)
    return found


def _referenced_and_pinned(consts):
    referenced: set[str] = set()
    pinned: set[str] = set()
    for p in sorted(_TESTS.glob('*.py')):
        text = p.read_text(encoding='utf-8')
        try:
            tree = ast.parse(text, filename=str(p))
        except SyntaxError:                       # pragma: no cover
            continue
        literals: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in consts:
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in consts:
                referenced.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value)
                # …the string ARGUMENT of setattr/monkeypatch.setattr, which is
                # how most tests actually touch a constant.
                if node.value in consts:
                    referenced.add(node.value)
            if (isinstance(node, ast.Compare) and len(node.ops) == 1
                    and isinstance(node.ops[0], ast.Eq)):
                left, right = node.left, node.comparators[0]
                for a, b in ((left, right), (right, left)):
                    name = (a.id if isinstance(a, ast.Name)
                            else a.attr if isinstance(a, ast.Attribute) else None)
                    if name not in consts:
                        continue
                    if isinstance(b, ast.UnaryOp):
                        b = b.operand
                    if (isinstance(b, ast.Constant)
                            and isinstance(b.value, (int, float))
                            and not isinstance(b.value, bool)):
                        pinned.add(name)
        for lit in literals:
            for m in re.finditer(r'(_?[A-Z][A-Z0-9_]*)\s*(?:\\s\*)?=', lit):
                if m.group(1) in consts:
                    pinned.add(m.group(1))
        # …and the PIN-TABLE shape: a tuple carrying the constant's NAME beside
        # a numeric literal, which is how a whole module's defaults get fenced
        # in one place (see _SHIPPED_DEFAULTS above, and
        # test_hat_lifecycle::test_the_shipped_hat_constants_…).
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Tuple, ast.List)):
                continue
            names = [e.value for e in node.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)
                     and e.value in consts]
            nums = [e.value for e in node.elts
                    if isinstance(e, ast.Constant)
                    and (isinstance(e.value, (int, float))
                         and not isinstance(e.value, bool)
                         or isinstance(e.value, str)
                         and re.fullmatch(r'-?[0-9][0-9_]*(\.[0-9]+)?', e.value))]
            if names and nums:
                pinned.update(names)
    return referenced, pinned


def test_every_referenced_numeric_constant_is_literal_pinned():
    consts = _production_constants()
    referenced, pinned = _referenced_and_pinned(consts)
    gap = sorted(referenced - pinned - set(ALLOWLIST))
    assert gap == [], (
        'these numeric constants are referenced by a test but never asserted '
        'against a LITERAL, so a mutation of the shipped value passes the whole '
        'suite. Add a pin (or a source fence for an env-derived one), or add an '
        'ALLOWLIST entry with a written reason:\n  '
        + '\n  '.join(f'{n}  ({consts[n]})' for n in gap))


def test_the_allowlist_has_no_stale_entries():
    """A stale exemption claims a gap that no longer exists — and hides a
    constant that HAS since been pinned behind an excuse. The list may only
    shrink."""
    consts = _production_constants()
    referenced, pinned = _referenced_and_pinned(consts)
    stale = sorted(n for n in ALLOWLIST if n not in (referenced - pinned))
    assert stale == [], (
        'these ALLOWLIST entries are no longer needed — delete them:\n  '
        + '\n  '.join(stale))
    for name, reason in ALLOWLIST.items():
        assert reason.strip(), f'{name} is exempted with no reason'
