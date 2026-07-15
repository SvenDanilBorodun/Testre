"""Boot-time robot-profile self-init (T2 / D1) + degraded-boot re-init retry.

Extracts ``_init_robot_profile`` (and its degraded-path helpers) by ``ast`` and
execs them onto a stub node — the deps-free pattern used across this suite
(physical_ai_server.py imports rclpy and cannot be imported in CI). Verifies the
node comes up with identity + a live communicator on success, self-heals to the
default profile on a bad env value, and — critically — NEVER raises out of
__init__ (a raise = respawn crash-loop), tearing down a half-built communicator
on the degraded path. The audit-fix additions verify the DEGRADED state is
unambiguous (communicator=None even on a blank config), that a bounded re-init
retry timer is armed and heals the node when the init path recovers, and that
exhaustion stays degraded with one final German [FEHLER] — without crashing.
"""

from __future__ import annotations

import ast
import os
import textwrap
import types
from pathlib import Path

from physical_ai_server import robot_profiles

_SERVER_PY = (
    Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
)

# _init_robot_profile now delegates its degraded path to sibling methods
# (teardown / bounded re-init retry). Extract the whole family so the stub
# node exercises the real production wiring.
_METHOD_NAMES = (
    '_init_robot_profile',
    '_teardown_to_degraded',
    '_destroy_stale_notice_timer',
    '_schedule_reinit_retry',
    '_reinit_retry_cb',
    '_cancel_reinit_timer',
)


class _FakeCallbackGroup:
    """Stands in for rclpy's MutuallyExclusiveCallbackGroup in the exec ns."""

    pass


def _load_methods():
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)

    # Module-level retry constants (period/attempts) — read from the source so
    # the tests can't drift from production values.
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.startswith('_REINIT'):
                consts[target.id] = ast.literal_eval(node.value)
    assert '_REINIT_RETRY_PERIOD_S' in consts, 'retry period const not found'
    assert '_REINIT_MAX_ATTEMPTS' in consts, 'retry attempts const not found'

    ns = {
        'robot_profiles': robot_profiles,
        'os': os,
        'MutuallyExclusiveCallbackGroup': _FakeCallbackGroup,
        **consts,
    }
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in _METHOD_NAMES:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            found[node.name] = ns[node.name]
    missing = set(_METHOD_NAMES) - set(found)
    assert not missing, f'methods not found: {missing}'
    return found, consts


_METHODS, _CONSTS = _load_methods()
_REINIT_MAX_ATTEMPTS = _CONSTS['_REINIT_MAX_ATTEMPTS']


class _Logger:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.infos = []

    def error(self, msg):
        self.errors.append(msg)

    def info(self, msg=None, *a, **k):
        self.infos.append(msg)

    def warning(self, msg=None, *a, **k):
        self.warnings.append(msg)


class _FakeComm:
    def __init__(self):
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


class _FakeTimer:
    def __init__(self, period, callback, callback_group=None):
        self.period = period
        self.callback = callback
        self.callback_group = callback_group
        self.canceled = False

    def cancel(self):
        self.canceled = True


class _Node:
    def __init__(self, init_impl):
        self.communicator = None
        self.params = None
        self._logger = _Logger()
        self._init_impl = init_impl
        self.created_timers = []
        self.destroyed_timers = []
        # Bind the production methods extracted from physical_ai_server.py.
        for name, fn in _METHODS.items():
            setattr(self, name, types.MethodType(fn, self))

    def get_logger(self):
        return self._logger

    def init_ros_params(self, robot_type):
        self._init_impl(self, robot_type)

    def create_timer(self, period, callback, callback_group=None):
        timer = _FakeTimer(period, callback, callback_group)
        self.created_timers.append(timer)
        return timer

    def destroy_timer(self, timer):
        self.destroyed_timers.append(timer)


def _run(node):
    node._init_robot_profile()


def _success(node, robot_type):
    node.params = {'camera_topic_list': ['gripper:/x'], 'joint_list': ['leader']}
    node.communicator = _FakeComm()


# --- happy path ------------------------------------------------------------

def test_boot_defaults_to_omx_full_when_env_absent(monkeypatch):
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)
    node = _Node(_success)
    _run(node)
    assert node.operation_mode == 'collection'
    assert node.robot_profile == 'omx_full'
    assert node.robot_type == 'omx_f'
    assert node.capabilities_json == \
        robot_profiles.capabilities_json(robot_profiles.resolve('omx_full'))
    assert node._arm_profile.profile_id == 'omx_full'
    assert isinstance(node.communicator, _FakeComm)   # built, NOT torn down
    assert node._logger.errors == []
    # No degraded state -> no re-init retry timer armed.
    assert getattr(node, '_reinit_timer', None) is None


def test_boot_reads_omx_follower_but_keeps_omx_f_data_type(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_ROBOT_TYPE', 'omx_follower')
    node = _Node(_success)
    _run(node)
    assert node.robot_profile == 'omx_follower'
    assert node.robot_type == 'omx_f'     # dataset repo naming must not shift


def test_boot_garbage_env_defaults_without_crash(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_ROBOT_TYPE', 'not-a-real-type')
    node = _Node(_success)
    _run(node)
    assert node.robot_profile == 'omx_full'
    assert isinstance(node.communicator, _FakeComm)


# --- degraded paths (must never raise) -------------------------------------

def test_boot_degraded_when_init_raises_before_communicator(monkeypatch):
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    def _raise_before(node, robot_type):
        raise RuntimeError('boom')

    node = _Node(_raise_before)
    _run(node)   # must NOT raise
    assert node.communicator is None
    assert node.robot_type == 'omx_f'     # identity still stamped
    assert node.robot_profile == 'omx_full'
    assert any('[FEHLER]' in e for e in node._logger.errors)


def test_boot_degraded_tears_down_partial_communicator(monkeypatch):
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)
    comm = _FakeComm()

    def _raise_after_comm(node, robot_type):
        node.communicator = comm     # half-initialized
        raise RuntimeError('boom after comm')

    node = _Node(_raise_after_comm)
    _run(node)   # must NOT raise
    assert comm.cleaned is True       # torn down
    assert node.communicator is None  # unambiguous degraded state
    assert any('[FEHLER]' in e for e in node._logger.errors)


def test_boot_blank_config_tears_down_to_degraded(monkeypatch):
    """Audit fix 4 — a wired-but-DEAD pipeline (blank camera/joint config) must
    not stay wired: it would advertise READY + full capabilities over nothing.
    The blank-config branch now mirrors the exception path (communicator=None)."""
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)
    comm = _FakeComm()

    def _empty_params(node, robot_type):
        node.params = {'camera_topic_list': [], 'joint_list': []}
        node.communicator = comm

    node = _Node(_empty_params)
    _run(node)
    assert any('unvollständig' in e for e in node._logger.errors)
    assert comm.cleaned is True       # torn down, degraded is unambiguous
    assert node.communicator is None
    assert node._reinit_timer is not None   # retry armed


def test_boot_blank_config_for_declared_default_tears_down(monkeypatch):
    """The value production ACTUALLY produces on a missing config YAML:
    the params are declared with default_value=[''] and load_parameters
    returns that — a NON-empty list of one blank string. The naive
    truthiness check silently passed it; the blank-aware check must not —
    and (audit fix 4) the node must degrade to communicator=None."""
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)
    comm = _FakeComm()

    def _default_params(node, robot_type):
        node.params = {'camera_topic_list': [''], 'joint_list': ['']}
        node.communicator = comm

    node = _Node(_default_params)
    _run(node)
    assert any('unvollständig' in e for e in node._logger.errors)
    assert comm.cleaned is True
    assert node.communicator is None


def test_boot_degraded_cancels_armed_stale_notice_timer(monkeypatch):
    """A raise AFTER init_ros_params armed the stale-session poll timer must
    cancel AND destroy it in the teardown (symmetry with the communicator
    teardown — a leaked 5 s timer keeps polling a degraded node forever)."""
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    timer = _FakeTimer(5.0, lambda: None)

    def _raise_after_timer(node, robot_type):
        node._stale_notice_timer = timer
        node.communicator = _FakeComm()
        raise RuntimeError('boom after timer armed')

    node = _Node(_raise_after_timer)
    _run(node)   # must NOT raise
    assert timer.canceled is True
    assert timer in node.destroyed_timers   # audit fix 6: destroy, not just cancel
    assert node._stale_notice_timer is None
    assert node.communicator is None


# --- bounded re-init retry (audit fix 1) ------------------------------------

def test_boot_degraded_arms_reinit_retry_timer(monkeypatch):
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    def _raise(node, robot_type):
        raise RuntimeError('boom')

    node = _Node(_raise)
    _run(node)
    assert node._reinit_timer is not None
    # Lightweight timer on its OWN callback group (never the node default).
    assert isinstance(node._reinit_timer.callback_group, _FakeCallbackGroup)
    # Idempotent: a second schedule must not stack a second timer.
    first = node._reinit_timer
    node._schedule_reinit_retry()
    assert node._reinit_timer is first
    assert len(node.created_timers) == 1


def test_reinit_retry_success_resumes(monkeypatch):
    """Degraded boot, then the init path recovers: the retry re-runs
    _init_robot_profile, the communicator comes up (idle tick resumes
    delivering identity), and the retry timer destroys itself."""
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    def _raise(node, robot_type):
        raise RuntimeError('boom')

    node = _Node(_raise)
    _run(node)
    assert node.communicator is None
    retry_timer = node._reinit_timer
    assert retry_timer is not None

    # The environment heals (e.g. config volume mounted late).
    node._init_impl = _success
    node._reinit_retry_cb()   # simulated timer fire — must not raise

    assert isinstance(node.communicator, _FakeComm)
    assert node._reinit_timer is None
    assert retry_timer.canceled is True
    assert retry_timer in node.destroyed_timers


def test_reinit_retry_exhaustion_stays_degraded_without_crash(monkeypatch):
    """The init path never recovers: after _REINIT_MAX_ATTEMPTS the retry logs
    one final German [FEHLER] telling the user to restart the environment,
    destroys its timer, and the node stays alive degraded — no raise."""
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    def _raise(node, robot_type):
        raise RuntimeError('boom forever')

    node = _Node(_raise)
    _run(node)
    retry_timer = node._reinit_timer
    assert retry_timer is not None

    for _ in range(_REINIT_MAX_ATTEMPTS):
        node._reinit_retry_cb()   # must never raise

    assert node.communicator is None                  # stays degraded
    assert node._reinit_timer is None                 # retry gave up
    assert retry_timer in node.destroyed_timers
    assert node._reinit_attempts == _REINIT_MAX_ATTEMPTS
    assert any(
        '[FEHLER]' in e and 'Umgebung neu starten' in e
        for e in node._logger.errors
    )
    # A stray late fire after exhaustion must still be harmless.
    node._reinit_retry_cb()


def test_reinit_retry_noops_over_live_communicator(monkeypatch):
    """The retry must never re-run init over a LIVE pipeline (that would leak a
    second Communicator's ROS entities): with a healed communicator it only
    destroys its own timer."""
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    def _raise(node, robot_type):
        raise RuntimeError('boom')

    node = _Node(_raise)
    _run(node)
    retry_timer = node._reinit_timer
    assert retry_timer is not None

    # Something else healed the node before the retry fired.
    live = _FakeComm()
    node.communicator = live
    node._reinit_retry_cb()

    assert node.communicator is live          # untouched, NOT re-initialized
    assert live.cleaned is False
    assert node._reinit_timer is None
    assert retry_timer in node.destroyed_timers
    assert getattr(node, '_reinit_attempts', 0) == 0   # no attempt burned
