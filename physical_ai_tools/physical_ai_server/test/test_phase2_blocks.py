"""Roboter Studio Phase-2 quick-win block tests (interpreter side).

Covers the three new control/value blocks added to interpreter.py:
  - ``text_join``           — Blockly built-in string composition (VALUE).
  - ``edubotics_forever``   — intentionally infinite CONTROL C-block.
  - ``edubotics_wait_until``— poll-a-condition CONTROL statement.

Deps-free (no container, no ROS, no torch): drives the interpreter's value /
control branches directly with a minimal stub ctx, mirroring test_interpreter.py
plus the flippable-``should_stop`` pattern from test_while_visible_loop.py.
"""

from __future__ import annotations

import json

import pytest

from physical_ai_server.workflow import interpreter as interp_mod
from physical_ai_server.workflow.interpreter import Interpreter
from physical_ai_server.workflow.handlers.motion import WorkflowError


class _Ctx:
    """Minimal stub: variables + a collecting log + a swappable should_stop."""

    def __init__(self):
        self.variables: dict = {}
        self.logs: list[str] = []
        self._stop = False
        self.should_stop = lambda: self._stop

    def log(self, m):
        self.logs.append(m)


# ── block builders ────────────────────────────────────────────────────────────
def _text(s):
    return {'type': 'text', 'fields': {'TEXT': s}}


def _num(n):
    return {'type': 'math_number', 'fields': {'NUM': n}}


def _bool(b):
    return {'type': 'logic_boolean', 'fields': {'BOOL': 'TRUE' if b else 'FALSE'}}


def _join(*items):
    """text_join block. An item of None becomes a present-but-empty ADDk slot."""
    inputs = {}
    for i, it in enumerate(items):
        inputs[f'ADD{i}'] = {'block': it} if it is not None else {}
    return {'type': 'text_join', 'inputs': inputs}


def _eval(block, ctx=None):
    return Interpreter([])._eval_value(block, ctx or _Ctx())


# ── text_join (VALUE) ─────────────────────────────────────────────────────────
def test_text_join_numbers_render_clean():
    # math_number yields a float (3.0); _to_text must drop the '.0' so the
    # student sees „Ich sehe 3 Bananen", not „Ich sehe 3.0 Bananen".
    out = _eval(_join(_text('Ich sehe '), _num(3), _text(' Bananen')))
    assert out == 'Ich sehe 3 Bananen'


def test_text_join_float_keeps_decimal():
    assert _eval(_join(_num(3.5))) == '3.5'


def test_text_join_bool_is_german():
    assert _eval(_join(_bool(True), _text('/'), _bool(False))) == 'wahr/falsch'


def test_text_join_empty_and_gap_items_are_blank():
    # A present-but-empty middle slot contributes ''.
    assert _eval(_join(_text('a'), None, _text('b'))) == 'ab'
    # No inputs at all → ''.
    assert _eval({'type': 'text_join', 'inputs': {}}) == ''
    assert _eval({'type': 'text_join'}) == ''


def test_text_join_nested():
    inner = _join(_text('X'), _text('Y'))
    assert _eval(_join(inner, _text('Z'))) == 'XYZ'


def test_text_join_out_of_order_indices_concatenate_in_order():
    # ADD keys provided out of order must still concat by integer suffix.
    block = {'type': 'text_join', 'inputs': {
        'ADD2': {'block': _text('c')},
        'ADD0': {'block': _text('a')},
        'ADD1': {'block': _text('b')},
    }}
    assert _eval(block) == 'abc'


def test_to_text_helper_direct():
    assert Interpreter._to_text(None) == ''
    assert Interpreter._to_text(True) == 'wahr'
    assert Interpreter._to_text(False) == 'falsch'
    assert Interpreter._to_text(7.0) == '7'
    assert Interpreter._to_text(2.5) == '2.5'
    assert Interpreter._to_text('hallo') == 'hallo'
    assert Interpreter._to_text(4) == '4'


def test_text_join_end_to_end_via_execute():
    # Proves the branch is reachable through the normal execute() path (a
    # variables_set whose VALUE is a text_join), not only via the private call.
    payload = json.dumps({'blocks': {'languageVersion': 0, 'blocks': [{
        'type': 'variables_set',
        'fields': {'VAR': {'name': 'msg'}},
        'inputs': {'VALUE': {'block': _join(_text('Anzahl: '), _num(2))}},
    }]}})
    ctx = _Ctx()
    Interpreter.from_json(payload).execute(ctx, lambda *a: None)
    assert ctx.variables['msg'] == 'Anzahl: 2'


# ── edubotics_forever (CONTROL) ───────────────────────────────────────────────
def _forever(do_block=None):
    inputs = {'DO': {'block': do_block}} if do_block is not None else {}
    return {'type': 'edubotics_forever', 'inputs': inputs}


def test_forever_runs_body_until_stop(monkeypatch):
    # Body runs repeatedly; should_stop flips True once it has run 3×.
    hits = {'n': 0}
    monkeypatch.setattr(
        interp_mod, 'STATEMENT_HANDLERS',
        {'edubotics_log': lambda c, a: hits.__setitem__('n', hits['n'] + 1)},
    )
    ctx = _Ctx()
    ctx.should_stop = lambda: hits['n'] >= 3
    block = _forever({'type': 'edubotics_log', 'fields': {'MESSAGE': 'x'}})
    with pytest.raises(WorkflowError, match='gestoppt'):
        Interpreter([])._exec_forever(block, ctx, lambda *a: None)
    assert hits['n'] == 3   # ran exactly until should_stop flipped — no over/undershoot


def test_forever_empty_body_is_stoppable_not_busy():
    # DO is None: the loop must NOT raise "leere Schleife" and must NOT hang —
    # it idles in an interruptible wait and stops promptly. should_stop returns
    # False on the first check (so we reach the empty-body branch) then True.
    calls = {'n': 0}

    def stop():
        calls['n'] += 1
        return calls['n'] > 1

    ctx = _Ctx()
    ctx.should_stop = stop
    with pytest.raises(WorkflowError, match='gestoppt'):
        Interpreter([])._exec_forever(_forever(None), ctx, lambda *a: None)


def test_forever_dispatched_via_exec_block(monkeypatch):
    # Proves the _exec_block btype ladder routes edubotics_forever (full
    # execute() path), not just a direct private call.
    hits = {'n': 0}
    monkeypatch.setattr(
        interp_mod, 'STATEMENT_HANDLERS',
        {'edubotics_log': lambda c, a: hits.__setitem__('n', hits['n'] + 1)},
    )
    ctx = _Ctx()
    ctx.should_stop = lambda: hits['n'] >= 2
    payload = json.dumps({'blocks': {'languageVersion': 0, 'blocks': [
        _forever({'type': 'edubotics_log', 'fields': {'MESSAGE': 'x'}}),
    ]}})
    with pytest.raises(WorkflowError, match='gestoppt'):
        Interpreter.from_json(payload).execute(ctx, lambda *a: None)
    assert hits['n'] == 2


# ── edubotics_wait_until (CONTROL) ────────────────────────────────────────────
def _wait_until(bool_block=None):
    inputs = {'BOOL': {'block': bool_block}} if bool_block is not None else {}
    return {'type': 'edubotics_wait_until', 'inputs': inputs}


def test_wait_until_returns_when_condition_flips_true(monkeypatch):
    # No-op the inter-poll wait so the test is instant; real monotonic for the
    # cap (never hit in a microsecond test). should_stop drives the "ticks" and
    # flips the watched variable true on the 3rd poll.
    monkeypatch.setattr(
        Interpreter, '_interruptible_wait', staticmethod(lambda *a, **k: None),
    )
    ctx = _Ctx()
    ticks = {'n': 0}

    def stop():
        ticks['n'] += 1
        if ticks['n'] >= 3:
            ctx.variables['ready'] = True
        return False

    ctx.should_stop = stop
    cond = {'type': 'variables_get', 'fields': {'VAR': {'name': 'ready'}}}
    Interpreter([])._exec_wait_until(_wait_until(cond), ctx, lambda *a: None)
    assert ctx.variables.get('ready') is True
    assert not any('Zeitlimit' in m for m in ctx.logs)   # returned via condition, not cap


def test_wait_until_raises_on_stop():
    ctx = _Ctx()
    ctx.should_stop = lambda: True
    with pytest.raises(WorkflowError, match='gestoppt'):
        Interpreter([])._exec_wait_until(_wait_until(_bool(False)), ctx, lambda *a: None)


def test_wait_until_wall_clock_cap_breaks_with_warning(monkeypatch):
    # Condition never true; the wall-clock cap breaks the wait (workflow
    # continues) with a German [WARNUNG] — it does NOT raise or hang.
    monkeypatch.setattr(interp_mod, 'WAIT_UNTIL_MAX_SECONDS', -1.0)  # already over budget
    ctx = _Ctx()
    Interpreter([])._exec_wait_until(_wait_until(_bool(False)), ctx, lambda *a: None)
    assert any('Zeitlimit' in m for m in ctx.logs)


def test_wait_until_missing_condition_breaks_at_cap(monkeypatch):
    # No BOOL wired → condition defaults False → breaks at the cap, never hangs.
    monkeypatch.setattr(interp_mod, 'WAIT_UNTIL_MAX_SECONDS', -1.0)
    ctx = _Ctx()
    Interpreter([])._exec_wait_until(_wait_until(None), ctx, lambda *a: None)
    assert any('Zeitlimit' in m for m in ctx.logs)


def test_wait_until_dispatched_via_exec_block(monkeypatch):
    # Proves the _exec_block btype ladder routes edubotics_wait_until.
    monkeypatch.setattr(interp_mod, 'WAIT_UNTIL_MAX_SECONDS', -1.0)
    ctx = _Ctx()
    payload = json.dumps({'blocks': {'languageVersion': 0, 'blocks': [
        _wait_until(_bool(False)),
    ]}})
    Interpreter.from_json(payload).execute(ctx, lambda *a: None)  # breaks at cap, returns clean
    assert any('Zeitlimit' in m for m in ctx.logs)


# ── H1: forever fast-body rate floor ─────────────────────────────────────────
def test_forever_fast_body_applies_rate_floor(monkeypatch):
    """A fast (instant) forever body must pay the per-iteration rate floor
    (FOREVER_MIN_CYCLE_S) so it can't flood the WorkflowStatus channel (#H1)."""
    waits: list[float] = []
    # Record rate-floor sleeps instead of actually sleeping.
    monkeypatch.setattr(
        Interpreter, '_interruptible_wait',
        staticmethod(lambda ctx, s: waits.append(s)),
    )
    interp = Interpreter([])
    calls = {'n': 0}
    interp._exec_chain = lambda do, ctx, obc: calls.__setitem__('n', calls['n'] + 1)
    ctx = _Ctx()
    ctx.should_stop = lambda: calls['n'] >= 3   # stop after 3 instant body runs
    block = {'type': 'edubotics_forever',
             'inputs': {'DO': {'block': {'type': 'noop'}}}}
    with pytest.raises(WorkflowError):
        interp._exec_forever(block, ctx, lambda *a: None)
    assert calls['n'] == 3
    # Each instant iteration paid a positive rate-floor wait.
    assert len(waits) == 3 and all(w > 0 for w in waits)


# ── H2: wait_until releases + reacquires motion_lock around the poll ──────────
def test_wait_until_releases_motion_lock_during_poll():
    """A wait_until nested under a held motion_lock (the when_* hat case) must
    RELEASE the lock during the poll and REACQUIRE it on exit, so it can't
    starve other motion for up to the 300 s cap (#H2)."""
    import threading
    from types import SimpleNamespace

    lock = threading.RLock()
    lock.acquire()   # simulate the hat handler holding motion_lock (this thread)
    other_acquired: list[bool] = []

    def _probe():
        got = lock.acquire(timeout=0.4)
        other_acquired.append(got)
        if got:
            lock.release()

    interp = Interpreter([])
    polls = {'n': 0}

    def fake_eval(blk, ctx):
        polls['n'] += 1
        t = threading.Thread(target=_probe)   # another thread must grab the lock now
        t.start()
        t.join()
        return True   # condition satisfied → wait_until returns after one poll

    interp._eval_value = fake_eval
    ctx = SimpleNamespace(
        should_stop=lambda: False,
        motion_lock=lock,
        log=lambda m: None,
        variables={},
    )
    block = {'type': 'edubotics_wait_until',
             'inputs': {'BOOL': {'block': {'type': 'logic_boolean'}}}}
    interp._exec_wait_until(block, ctx, lambda *a: None)

    assert polls['n'] == 1
    assert other_acquired == [True], 'motion_lock was NOT released during the poll'

    after: list[bool] = []

    def _probe2():
        got = lock.acquire(timeout=0.2)
        after.append(got)
        if got:
            lock.release()

    t2 = threading.Thread(target=_probe2)
    t2.start()
    t2.join()
    assert after == [False], 'motion_lock was NOT reacquired after wait_until'
    lock.release()


# ── #P1: [VAR:] payload cap (forever + text_join self-concat flood guard) ─────
def test_set_variable_caps_var_payload():
    """A huge variable value is truncated in the [VAR:] inspector emission so a
    `forever { setze x = verbinde(x, …) }` can't re-emit an unbounded growing
    string into the realtime channel (#P1)."""
    interp = Interpreter([])
    ctx = _Ctx()
    interp._set_variable(ctx, 'huge', 'x' * 50000)
    var_lines = [m for m in ctx.logs if m.startswith('[VAR:huge=')]
    assert var_lines, 'no [VAR:] emission'
    assert ' …' in var_lines[0]          # truncation marker present
    assert len(var_lines[0]) < 5000      # capped well below the 50k payload


# The React side's OWN cap on a [VAR:] value, restated here — the ONE place in
# this suite where a JS constant is written in Python, and acceptable only
# because this test's whole job is to notice when the two disagree.
#
# `useRosTopicSubscription`: `if (rawName.length > VAR_NAME_MAX_LEN ||
# rawValue.length > 4096) return { intercepted: true };` — an over-long value is
# CONSUMED with NO dispatch, NO log line and NO error. The variable simply stops
# updating and nothing anywhere says why. Measured by driving the real hook with
# frames the real emitter built: at the shipped 2000 the value is stored; at a
# mutated 4500 it is discarded silently, and BOTH suites stay green.
REACT_VAR_VALUE_CAP = 4096          # useRosTopicSubscription.js
REACT_VAR_NAME_MAX_LEN = 64         # utils/variableName.js::VAR_NAME_MAX_LEN


def test_the_var_payload_cap_stays_inside_the_react_gate():
    """*Kills:* raising `_MAX_VAR_PAYLOAD_CHARS` past the React gate — which is
    exactly the change somebody makes when a student complains their list is
    being truncated, i.e. the symptom appears on the variables that motivate it.
    """
    worst = (interp_mod._MAX_VAR_PAYLOAD_CHARS
             + len('[VAR:=]') + REACT_VAR_NAME_MAX_LEN + len(' …'))
    assert worst < REACT_VAR_VALUE_CAP, (
        f'a [VAR:] frame can reach {worst} chars against React\'s '
        f'{REACT_VAR_VALUE_CAP} value cap — over it the frame is consumed with '
        'no dispatch, no log line and no error anywhere')


def test_the_react_caps_are_still_what_this_test_says_they_are():
    """The two literals above are a SECOND opinion about numbers React owns, so
    they have to be checked against the source. Skips when the React tree is
    absent (the container ships only this package), like
    ``test_model_root_agreement``."""
    import re
    from pathlib import Path
    root = (Path(__file__).resolve().parents[3] / 'physical_ai_tools'
            / 'physical_ai_manager' / 'src')
    if not root.is_dir():
        import pytest as _pytest
        _pytest.skip('React source tree not present')
    hook = (root / 'hooks' / 'useRosTopicSubscription.js').read_text(encoding='utf-8')
    m = re.search(r'rawValue\.length > (\d+)', hook)
    assert m, 'the [VAR:] value gate moved or was renamed'
    assert int(m.group(1)) == REACT_VAR_VALUE_CAP
    names = (root / 'utils' / 'variableName.js').read_text(encoding='utf-8')
    m = re.search(r'VAR_NAME_MAX_LEN = (\d+)', names)
    assert m and int(m.group(1)) == REACT_VAR_NAME_MAX_LEN


# ══════════════════════════════════════════════════════════════════════════
# D-1 — a variable name containing the frame's own delimiters
# ══════════════════════════════════════════════════════════════════════════

def _var_lines(ctx):
    return [m for m in ctx.logs if m.startswith('[VAR:')]


@pytest.mark.parametrize('name', ['mein Wert=2', 'x=5', 'a[0]', 'b]c'])
def test_a_variable_name_with_a_frame_character_is_not_emitted(name):
    """Measured end to end through the real hook and the real reducer: a
    variable „mein Wert=2" set to 5 emits `[VAR:mein Wert=2=5.0]`, which React's
    `^\\[VAR:([^=]+)=(.*)\\]$` parses as name „mein Wert", value „2=5.0". The
    panel grows a row the student never created and, if a real „mein Wert"
    exists, its value is SILENTLY CLOBBERED — with nothing in the Protokoll.

    `variableName.js`'s own `=` refusal is structurally unreachable on this
    path: by the time it runs, `[^=]+` has already cut the name. This is the
    only layer that still holds the whole name."""
    interp = Interpreter([])
    ctx = _Ctx()
    interp._set_variable(ctx, name, 5)
    assert _var_lines(ctx) == [], f'a corrupt frame was emitted: {ctx.logs}'
    warns = [m for m in ctx.logs if m.startswith('[WARNUNG]')]
    assert len(warns) == 1, f'the absence must be EXPLAINED, got {ctx.logs}'
    assert name in warns[0] and 'Variablen-Tafel' in warns[0]
    assert 'läuft normal weiter' in warns[0], (
        'the program is unaffected — only the panel cannot show it')
    # …and the variable itself is still set and readable.
    assert ctx.variables[name] == 5


def test_the_unshowable_warning_fires_once_per_name_per_run():
    """*Kills:* dropping the latch, which would flood a loop."""
    interp = Interpreter([])
    ctx = _Ctx()
    for _ in range(3):
        interp._set_variable(ctx, 'x=5', 1)
    interp._set_variable(ctx, 'y=6', 1)
    warns = [m for m in ctx.logs if m.startswith('[WARNUNG]')]
    assert len(warns) == 2, f'one per NAME per run, got {ctx.logs}'


def test_a_value_containing_an_equals_sign_still_shows():
    """*Kills:* "fixing" D-1 by splitting on the LAST `=` like `[CNT:]`. The
    `[VAR:]` value is JSON and can legitimately contain `=` inside a string;
    `[CNT:]`'s value is digits-only, which is the entire reason last-`=` is
    unambiguous there. The two frames may never be harmonised."""
    interp = Interpreter([])
    ctx = _Ctx()
    interp._set_variable(ctx, 'x', 'a=b')
    lines = _var_lines(ctx)
    assert lines == ['[VAR:x="a=b"]'], ctx.logs
    # …and it survives React's own capture, name first.
    import re
    m = re.match(r'^\[VAR:([^=]+)=(.*)\]$', lines[0], re.S)
    assert m and m.group(1) == 'x' and m.group(2) == '"a=b"'


# ══════════════════════════════════════════════════════════════════════════
# D-2 — an empty variable NAME
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('name', ['', '   ', None])
@pytest.mark.parametrize('btype', ['variables_set', 'math_change'])
def test_a_nameless_variable_block_is_refused_in_german(name, btype):
    """`_read_variable_name` ends `if isinstance(value, str): return value`, so a
    bare-string field `{"VAR": ""}` yielded '' and both guards tested `is None`.
    The frame `[VAR:=5]` matches NEITHER React regex (`[^=]+` needs ≥1 char),
    falls through `interceptToken` and is rendered VERBATIM in the Protokoll.
    The two neighbouring call sites (`controls_for`, `controls_forEach`) were
    already truthiness-guarded; these two were the odd ones out."""
    from physical_ai_server.workflow.interpreter import InterpreterError
    fields = {} if name is None else {'VAR': name}
    key = 'VALUE' if btype == 'variables_set' else 'DELTA'
    block = {'type': btype, 'fields': fields,
             'inputs': {key: {'block': _num(5)}}}
    interp = Interpreter([])
    ctx = _Ctx()
    with pytest.raises(InterpreterError) as exc:
        if btype == 'variables_set':
            interp._exec_variables_set(block, ctx)
        else:
            interp._exec_math_change(block, ctx)
    assert 'Variable hat keinen Namen' in str(exc.value)
    assert _var_lines(ctx) == [], f'the raw sentinel leaked: {ctx.logs}'


# ══════════════════════════════════════════════════════════════════════════
# E-4 — an unknown object on a STUDENT surface
# ══════════════════════════════════════════════════════════════════════════

def test_a_greifziel_variable_renders_as_student_text():
    """`_jsonable`'s fallback was `repr(value)`, so the student's `ziel`
    variable — the prescribed split-grasp idiom, every loop pass — rendered as
    `Detection(centroid_px=(267, 305), bbox_px=…, corners_px=array([[…]]),
    extras={…})`. The SAME round added `output._student_text`, which renders the
    SAME object as „Greifziel (Marker 22) bei x=… m, …". Two stringifiers for
    student-visible values, and only one of them knew what a Greifziel was."""
    from physical_ai_server.workflow.perception import Detection
    interp = Interpreter([])
    ctx = _Ctx()
    det = Detection(centroid_px=(267, 305), bbox_px=(0, 0, 1, 1),
                    confidence=1.0, label='tag22', aruco_id=22,
                    world_xyz_m=(0.158, -0.050, 0.015))
    interp._set_variable(ctx, 'ziel', det)
    line = _var_lines(ctx)[0]
    assert 'Greifziel (Marker 22)' in line, line
    assert 'Detection(' not in line and 'centroid_px' not in line


@pytest.mark.parametrize('value', [1, 2.5, 'text', [1, 2], {'a': 1}, True, None])
def test_a_plain_value_still_renders_unchanged(value):
    """*Kills:* routing everything through `_student_text`."""
    import json
    from physical_ai_server.workflow import interpreter as _i
    assert _i._jsonable(value) == json.loads(json.dumps(value))


def test_a_stringifier_that_raises_still_produces_a_frame():
    """*Kills:* deleting the `except`. Observability never breaks a run."""
    from physical_ai_server.workflow import interpreter as _i

    class _Boom:
        def __repr__(self):
            return 'BOOM-REPR'

        @property
        def aruco_id(self):
            raise RuntimeError('no')

    out = _i._jsonable(_Boom())
    assert isinstance(out, str) and out
