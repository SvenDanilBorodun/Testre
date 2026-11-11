#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""An environment stop gives a running recording the time to finalize (launch
plan step 8, 2026-11-10).

The chain, outermost first, and why each link has its value:

  GUI ``compose down`` / Pi ``compose stop`` subprocess timeout (120 s)
    > the stop graces in compose's stop order (manager → physical_ai_server →
      open_manipulator, each its stop_grace_period, docker's 10 s when unset)
  physical_ai_server stop_grace_period (50 s)
    > the launch's SIGKILL bound for the node (sigterm_timeout + sigkill_timeout,
      40 s) + s6 stage 3 after the launch exits (finish script, logger,
      S6_KILL_GRACETIME 3 s)
  the launch's SIGKILL bound (40 s)
    > the node's own finalize budget (recording_shutdown.SHUTDOWN_FINALIZE_WAIT_S,
      30 s) + its teardown after it

and the launch only does its orderly shutdown (SIGINT to every child, then the
two timeouts) because the thin Dockerfile makes SIGINT the s6 down-signal of
the service: on SIGTERM, Jazzy's launch exits at once and orphans its children.

Stdlib only (no PyYAML): the compose files are read with the line-shape
parser test_code_runner_sandbox.py uses, the Python files with ``ast``.
"""

import ast
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_STUDENT = _REPO / 'robotis_ai_setup/docker/docker-compose.yml'
_GPU = _REPO / 'robotis_ai_setup/docker/docker-compose.gpu.yml'
_OPI = _REPO / 'robotis_ai_setup/docker/docker-compose.opi.yml'
_TWIN = _REPO / 'robotis_ai_setup/pi_agent/docker/docker-compose.opi.yml'
_DOCKERFILE = _REPO / 'robotis_ai_setup/docker/physical_ai_server/Dockerfile'
_LAUNCH = _REPO / 'physical_ai_tools/physical_ai_server/launch/physical_ai_server.launch.py'
_HELPER = (_REPO / 'physical_ai_tools/physical_ai_server/physical_ai_server'
           / 'data_processing/recording_shutdown.py')
_NODE = (_REPO / 'physical_ai_tools/physical_ai_server/physical_ai_server'
         / 'physical_ai_server.py')
_GUI_DM = _REPO / 'robotis_ai_setup/gui/app/docker_manager.py'
_PI_DM = _REPO / 'robotis_ai_setup/pi_agent/docker_manager.py'

DOCKER_DEFAULT_GRACE_S = 10          # docker stop's default when compose sets none
S6_STAGE3_AFTER_LAUNCH_S = 5         # finish script + logger + S6_KILL_GRACETIME (3 s)
NODE_TEARDOWN_AFTER_FINALIZE_S = 5   # cloud client, HF worker, sensor executor, node


def _service_block(path, service):
    lines = path.read_text(encoding='utf-8').splitlines()
    out, inside = [], False
    for line in lines:
        if re.match(r'^  %s:\s*$' % re.escape(service), line):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith('   ') and not line.startswith('  #'):
                break
            out.append(line)
    return out


def _grace_s(path, service):
    """The service's stop_grace_period in seconds (docker's default when unset)."""
    for line in _service_block(path, service):
        m = re.match(r'^    stop_grace_period:\s*(\d+)s\s*(?:#.*)?$', line)
        if m:
            return int(m.group(1))
        assert not re.match(r'^    stop_grace_period:', line), f'unparsed grace in {path}: {line}'
    return None


def _module_constant(path, name):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == name):
            return ast.literal_eval(stmt.value)
    raise AssertionError(f'{name} not found in {path}')


def _launch_node_kwargs():
    tree = ast.parse(_LAUNCH.read_text(encoding='utf-8'))
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == 'Node'):
            continue
        kwargs = {k.arg: ast.literal_eval(k.value) for k in call.keywords
                  if isinstance(k.value, ast.Constant)}
        if kwargs.get('executable') == 'physical_ai_server':
            return kwargs
    raise AssertionError('the physical_ai_server Node is not in the launch file')


def _function(path, name):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


class TheS6DownSignal(unittest.TestCase):
    def test_the_thin_dockerfile_makes_sigint_the_down_signal(self):
        text = _DOCKERFILE.read_text(encoding='utf-8')
        m = re.search(r'^RUN set -e && \\\n((?:.*\\\n)*.*\[STOP\].*)$', text, re.M)
        self.assertIsNotNone(m, 'the down-signal RUN block is missing')
        block = m.group(1)
        self.assertIn('svc=/etc/s6-overlay/s6-rc.d/physical_ai_server', block)
        self.assertIn('test -f "$svc/run"', block)
        self.assertIn("grep -qx 'longrun' \"$svc/type\"", block)
        self.assertIn("printf 'SIGINT\\n' > \"$svc/down-signal\"", block)
        self.assertIn("grep -qx 'SIGINT' \"$svc/down-signal\"", block)

    def test_it_runs_after_the_crlf_fix(self):
        # The CRLF fix rewrites the s6 `type` file the block greps.
        text = _DOCKERFILE.read_text(encoding='utf-8')
        self.assertLess(text.index('Fixed CRLF in s6 service files'),
                        text.index("printf 'SIGINT\\n'"))

    def test_the_upstream_s6_tree_does_not_disagree(self):
        upstream = _REPO / 'physical_ai_tools/docker/s6-services/physical_ai_server'
        self.assertTrue((upstream / 'run').is_file())
        self.assertFalse((upstream / 'down-signal').exists(),
                         'the thin Dockerfile is the one place the down-signal is set')


class TheBudgetChain(unittest.TestCase):
    def setUp(self):
        kwargs = _launch_node_kwargs()
        self.sigterm_s = float(kwargs['sigterm_timeout'])
        self.sigkill_s = float(kwargs['sigkill_timeout'])
        self.launch_kill_bound_s = self.sigterm_s + self.sigkill_s
        self.finalize_wait_s = float(_module_constant(_HELPER, 'SHUTDOWN_FINALIZE_WAIT_S'))

    def test_the_documented_values(self):
        self.assertEqual((self.sigterm_s, self.sigkill_s), (10.0, 30.0))
        self.assertEqual(self.finalize_wait_s, 30.0)
        for path in (_STUDENT, _OPI, _TWIN):
            with self.subTest(compose=path.name):
                self.assertEqual(_grace_s(path, 'physical_ai_server'), 50)

    def test_the_launch_keeps_respawn_and_takes_strings(self):
        kwargs = _launch_node_kwargs()
        self.assertIs(kwargs['respawn'], True)
        # launch substitutes these: a plain string, like the sidecar's '3'
        self.assertIsInstance(kwargs['sigterm_timeout'], str)
        self.assertIsInstance(kwargs['sigkill_timeout'], str)

    def test_the_launch_never_kills_a_finalize_inside_its_budget(self):
        self.assertGreaterEqual(
            self.launch_kill_bound_s, self.finalize_wait_s + NODE_TEARDOWN_AFTER_FINALIZE_S)

    def test_docker_never_kills_the_launch_shutdown(self):
        for path in (_STUDENT, _OPI, _TWIN):
            with self.subTest(compose=path.name):
                grace = _grace_s(path, 'physical_ai_server')
                self.assertIsNotNone(grace, f'{path.name}: physical_ai_server has no stop_grace_period')
                self.assertGreaterEqual(grace, self.launch_kill_bound_s + S6_STAGE3_AFTER_LAUNCH_S)

    def test_the_gpu_overlay_keeps_the_grace(self):
        self.assertIsNone(_grace_s(_GPU, 'physical_ai_server'),
                          'the gpu overlay would replace the student grace')

    def _grace_or_default(self, path, service):
        grace = _grace_s(path, service)
        return DOCKER_DEFAULT_GRACE_S if grace is None else grace

    def test_the_gui_waits_for_compose_down(self):
        timeout = _module_constant(_GUI_DM, 'STACK_STOP_TIMEOUT_S')
        # compose down stops one after another: manager, server, then the arm
        chain = sum(self._grace_or_default(_STUDENT, s)
                    for s in ('physical_ai_manager', 'physical_ai_server', 'open_manipulator'))
        self.assertGreater(timeout, chain)
        src = ast.unparse(_function(_GUI_DM, 'stop_containers'))
        self.assertIn('timeout=STACK_STOP_TIMEOUT_S', src)
        self.assertIn("'down'", src)

    def test_the_pi_waits_for_compose_stop_before_rm(self):
        timeout = _module_constant(_PI_DM, 'ROBOT_TIER_STOP_TIMEOUT_S')
        # the robot tier: the server, then the arm (the runner stops beside them)
        chain = sum(self._grace_or_default(_TWIN, s)
                    for s in ('physical_ai_server', 'open_manipulator'))
        self.assertGreater(timeout, chain)
        src = ast.unparse(_function(_PI_DM, 'stop_robot_tier'))
        stop = src.index("(['stop', *_ROBOT_TIER], ROBOT_TIER_STOP_TIMEOUT_S)")
        self.assertLess(stop, src.index("['rm', '-f', *_ROBOT_TIER]"),
                        'rm -f SIGKILLs: it must come after the graceful stop')
        self.assertIn('timeout=timeout', src)


class TheSmokeScenarioMirrorsTheNode(unittest.TestCase):
    """The image-only smoke case (.github/scripts/record_fsm_smoke.py) is worth
    only as much as it resembles the node: the same signal setup before
    rclpy.init, the same finalize helper in the finally, both signals run."""

    _SMOKE = _REPO / '.github/scripts/record_fsm_smoke.py'

    def setUp(self):
        self.src = self._SMOKE.read_text(encoding='utf-8')
        self.tree = ast.parse(self.src)

    def _function(self, name):
        return next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == name)

    def test_the_child_sets_sigint_like_the_node_before_rclpy_init(self):
        child = ast.get_source_segment(self.src, self._function('shutdown_child'))
        self.assertIn('signal.signal(signal.SIGINT, signal.SIG_DFL)', child)
        self.assertLess(child.index('signal.signal(signal.SIGINT, signal.SIG_DFL)'),
                        child.index('rclpy.init()'))
        node_main = ast.unparse(_function(_NODE, 'main'))
        self.assertIn('signal.signal(signal.SIGINT, signal.SIG_DFL)', node_main)

    def test_the_child_finalizes_through_the_nodes_helper_in_its_finally(self):
        child = ast.get_source_segment(self.src, self._function('shutdown_child'))
        finally_src = child[child.index('finally:'):]
        self.assertIn('recording_shutdown.finalize_for_shutdown(', finally_src)
        self.assertIn('recording_shutdown.SHUTDOWN_FINALIZE_WAIT_S', finally_src)
        # no interpreter teardown: LeRobot's __del__ must not be what saves the data
        self.assertIn('os._exit(0)', child)

    def test_both_signals_are_run(self):
        main = ast.get_source_segment(self.src, self._function('main'))
        self.assertIn("_shutdown_scenario('SIGINT')", main)
        self.assertIn("_shutdown_scenario('SIGTERM')", main)


if __name__ == '__main__':
    unittest.main()
