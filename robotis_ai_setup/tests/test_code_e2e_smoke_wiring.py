"""The two-container text-program smoke is wired as the student compose wires
the runner (owner decision Q7; deps-free).

``.github/scripts/code_e2e_smoke.py`` runs in the server image beside the
runner image in ``docker-publish.yml::smoke-test``. What it proves is only
worth something if the runner it talks to is the SANDBOXED runner a student
gets: every compose sandbox key appears on the step's ``docker run`` with the
compose's own value, the two share one volume at the compose's mount point,
the step runs on both legs that ship a runner, and the image names travel
through ``env:`` (the zizmor baseline). The script covers the three parts of
the spec (m13) and imports the rig-emulation fixture from next to itself.
"""

import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / '.github' / 'workflows' / 'docker-publish.yml'
_SMOKE = _REPO_ROOT / '.github' / 'scripts' / 'code_e2e_smoke.py'
_COMPOSE = _REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'docker-compose.yml'
_FIXTURE = (_REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'test'
            / 'rig_emulation.py')


def _step(text, name_start):
    start = text.index(f'      - name: {name_start}')
    end = text.index('\n      - name: ', start + 1)
    return text[start:end]


def _compose_runner():
    text = _COMPOSE.read_text(encoding='utf-8')
    start = text.index('\n  code_runner:\n')
    end = text.index('\nvolumes:\n', start)
    return text[start:end]


class SmokeStep(unittest.TestCase):
    def setUp(self):
        self.step = _step(_WORKFLOW.read_text(encoding='utf-8'),
                          'Text-program smoke with the runner and the server side by side')

    def test_it_runs_on_every_leg_that_ships_a_runner(self):
        self.assertIn("if: matrix.runner_repo != ''", self.step)

    def test_the_runner_carries_every_compose_sandbox_key(self):
        compose = _compose_runner()
        run = self.step[self.step.index('docker run -d --name code-e2e-runner'):]
        run = run[:run.index('"$RUNNER_IMAGE"')]
        self.assertIn('init: true', compose)
        self.assertIn('--init', run)
        self.assertIn('user: "0:0"', compose)
        self.assertIn('--user 0:0', run)
        self.assertIn('read_only: true', compose)
        self.assertIn('--read-only', run)
        self.assertIn('network_mode: "none"', compose)
        self.assertIn('--network none', run)
        self.assertIn('--cap-drop ALL', run)
        for cap in re.findall(r'^\s+- (SETUID|SETGID|KILL)\b', compose, re.M):
            self.assertIn(f'--cap-add {cap}', run)
        self.assertEqual(run.count('--cap-add'), 3)
        self.assertIn('no-new-privileges:true', compose)
        self.assertIn('--security-opt no-new-privileges:true', run)
        for tmpfs in re.findall(r'^\s+- (/\w+:size=\w+,mode=\d+)', compose, re.M):
            self.assertIn(f'--tmpfs {tmpfs}', run)
        for key, flag in (('mem_limit', '--memory'), ('pids_limit', '--pids-limit'),
                          ('cpus', '--cpus')):
            value = re.search(rf'^\s+{key}: (\S+)', compose, re.M).group(1)
            self.assertIn(f'{flag} {value}', run)
        mount = re.search(r'- code_runner_ipc:(\S+)', compose).group(1)
        self.assertIn(f'-v code-e2e-ipc:{mount}', run)
        self.assertIn(f'-v code-e2e-ipc:{mount}', self.step[self.step.index('rc=0'):])

    def test_the_images_travel_through_env_and_the_fixture_from_the_checkout(self):
        run = self.step[self.step.index('run: |'):]
        self.assertNotIn('${{', run)
        self.assertIn('SERVER_IMAGE: ghcr.io/svendanilborodun/${{ matrix.server_repo }}:latest',
                      self.step)
        self.assertIn('RUNNER_IMAGE: ghcr.io/svendanilborodun/${{ matrix.runner_repo }}:latest',
                      self.step)
        self.assertIn('physical_ai_tools/physical_ai_server/test/rig_emulation.py:'
                      '/tmp/smoke/rig_emulation.py:ro', run)
        self.assertIn('/tmp/smoke/code_e2e_smoke.py', run)
        self.assertTrue(_FIXTURE.is_file())


class SmokeScript(unittest.TestCase):
    def setUp(self):
        self.src = _SMOKE.read_text(encoding='utf-8')

    def test_the_three_parts_of_the_spec(self):
        for part in ('def run_matrix(', 'def run_examples(', 'def run_putback('):
            self.assertIn(part, self.src)
        # The matrix carries the non-static main (m14) and both languages.
        self.assertIn("'java_nonstatic_main'", self.src)
        self.assertIn("'py_input'", self.src)
        # The examples come from the image's own robot_api (the column's lines).
        self.assertIn('robot_api.export_docs_json()', self.src)
        self.assertIn('import rig_emulation as rig_mod', self.src)


if __name__ == '__main__':
    unittest.main()
