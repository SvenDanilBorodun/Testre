"""The rig never uploads into a HuggingFace namespace it does not own.

D3 — this guard was the HEADLINE of the 2026-08-06 handover commit and it had
ZERO tests: replacing the whole refusal with ``if False:`` left every suite in
the repo green. That is the class of defect this file exists to close, so it is
mutation-verified in both directions.

WHY THE GUARD EXISTS. ``DataManager.__init__`` builds ``_save_repo_name`` from
the CLIENT-SUPPLIED ``task_info.user_id``, not from the rig's token, while
``InfoPanel`` auto-selects ``hfUserList[0]``. On one shared Windows account,
student B's recordings therefore uploaded into student A's namespace — and
SUCCEEDED, because the rig token has no idea whose id was typed. The Aufnahme
surface has no auth gate and rosbridge is unauthenticated, so "any client" is
not hypothetical either.

WHY IT MUST FAIL OPEN, and this is the half that is easy to get wrong. Recording
with NO cloud login is a fully supported path (only Training and Inferenz gate
on a session). So "cannot judge" — no token, whoami timed out, HF down, a
malformed reply — must ALLOW: with no token the upload fails on its own and
nothing is published, whereas turning a network blip into a destroyed upload is
a worse outcome than the case the guard exists for. It is a REFUSE-ON-PROOF
gate, the same stance ``hf_token_is_foreign`` takes on an absent stamp.

``data_manager`` imports a large ROS/cv2/HF/lerobot tree at module level, none
of which this logic needs, so those are stubbed in ``sys.modules`` before the
module is loaded by path — the same approach as
``test_data_manager_finalize.py``. ``dataset_paths`` is loaded FOR REAL because
``__init__``'s confine() is one of the things under test.
"""

import importlib.util
import pathlib
import shutil
import sys
import tempfile
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA_MANAGER_PATH = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
    / 'data_processing' / 'data_manager.py'
)


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


class _FakeConverter:
    """Real enough for __init__ — a bare placeholder AttributeErrors here."""

    def set_action_duration_from_fps(self, fps):
        self.fps = fps

    def tensor_array2joint_trajectory(self, *a, **k):
        return None


def _install_stubs():
    _placeholder = type('_Placeholder', (), {})

    _stub('cv2')
    _stub('numpy')
    _stub('requests')
    _stub('geometry_msgs')
    _stub('geometry_msgs.msg', Twist=_placeholder)
    _stub('nav_msgs')
    _stub('nav_msgs.msg', Odometry=_placeholder)
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', JointState=_placeholder)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg', JointTrajectory=_placeholder)
    _stub('huggingface_hub',
          CommitOperationDelete=_placeholder,
          DatasetCard=_placeholder, DatasetCardData=_placeholder,
          HfApi=_placeholder, ModelCard=_placeholder, ModelCardData=_placeholder,
          snapshot_download=lambda *a, **k: None,
          upload_large_folder=lambda *a, **k: None)
    _stub('huggingface_hub.errors',
          LocalTokenNotFoundError=type('LocalTokenNotFoundError', (Exception,), {}),
          RevisionNotFoundError=type('RevisionNotFoundError', (Exception,), {}))
    _stub('lerobot')
    _stub('lerobot.datasets')
    _stub('lerobot.datasets.utils', DEFAULT_FEATURES={})
    _stub('lerobot.datasets.dataset_metadata', CODEBASE_VERSION='v3.0')
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', TaskStatus=_placeholder)
    _stub('physical_ai_server')
    _stub('physical_ai_server.data_processing')

    # Loaded for REAL: __init__'s confine() is under test here, and a stub
    # without it would AttributeError rather than prove anything.
    name = 'physical_ai_server.data_processing.dataset_paths'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, str(DATA_MANAGER_PATH.parent / 'dataset_paths.py'))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    sys.modules['physical_ai_server.data_processing'].dataset_paths = sys.modules[name]

    _stub('physical_ai_server.data_processing.data_converter',
          DataConverter=_FakeConverter)
    _stub('physical_ai_server.data_processing.lerobot_dataset_wrapper',
          LeRobotDatasetWrapper=_placeholder)
    _stub('physical_ai_server.data_processing.progress_tracker',
          HuggingFaceProgressTqdm=_placeholder,
          HuggingFaceLogCapture=_placeholder)
    _stub('physical_ai_server.device_manager')
    _stub('physical_ai_server.device_manager.cpu_checker', CPUChecker=_placeholder)
    _stub('physical_ai_server.device_manager.ram_checker', RAMChecker=_placeholder)
    _stub('physical_ai_server.device_manager.storage_checker',
          StorageChecker=_placeholder)


_MODULE_NAME = '_edubotics_dm_namespace_test'


def _load():
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME, str(DATA_MANAGER_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


class _TaskInfo:
    def __init__(self, user_id='alice', task_name='pick', fps=30):
        self.user_id = user_id
        self.task_name = task_name
        self.task_instruction = ['do the thing']
        self.fps = fps
        self.record_rosbag2 = False
        self.tags = []
        self.private_mode = True
        self.push_to_hub = True
        self.num_episodes = 3
        self.episode_time_s = 10


class _Rig(unittest.TestCase):
    """A DataManager wired so _upload_dataset can be driven in isolation."""

    def setUp(self):
        self.dm_mod = _load()
        self.DataManager = self.dm_mod.DataManager
        # The namespace set is cached at CLASS level (it is a property of the
        # RIG's token, not of a recording). Every test starts from cold, or one
        # test's answer silently decides the next one's.
        self.DataManager.invalidate_hf_namespace_cache()
        self.addCleanup(self.DataManager.invalidate_hf_namespace_cache)

        self.root = pathlib.Path(tempfile.mkdtemp(prefix='dsroot_')).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        self.whoami_calls = []
        self.uploads = []

    def _make(self, user_id='alice'):
        dm = self.DataManager(
            self.root, 'omx_f', _TaskInfo(user_id=user_id),
            upload_callback=lambda *a: self.uploads.append(a))
        dm._lerobot_dataset = None
        return dm

    def _whoami(self, result):
        """Patch the ONE network call the guard depends on.

        Patched at `get_huggingface_user_id`, not at `_rig_hf_namespaces` —
        patching the resolver itself would skip the cache and the fail-open
        classification, i.e. exactly the code under test.
        """
        def fake():
            self.whoami_calls.append(1)
            if isinstance(result, BaseException):
                raise result
            return result
        self.DataManager.get_huggingface_user_id = staticmethod(fake)
        self.addCleanup(
            lambda: setattr(
                self.DataManager, 'get_huggingface_user_id',
                self.dm_mod.DataManager.__dict__.get('get_huggingface_user_id')))


class TheGuardRefusesAForeignNamespace(_Rig):

    def test_the_rigs_OWN_namespace_uploads(self):
        # Not vacuous: the refusal below has to be distinguishable from a guard
        # that refuses everything.
        self._whoami(['alice'])
        dm = self._make(user_id='alice')
        dm._upload_dataset(tags=[], private=True)
        self.assertEqual(len(self.uploads), 1, 'a legitimate upload was refused')
        self.assertEqual(self.uploads[0][0], 'alice/omx_f_pick')

    def test_ANOTHER_students_namespace_is_refused(self):
        """THE defect: B's recordings uploaded into A's namespace and SUCCEEDED."""
        self._whoami(['alice'])
        dm = self._make(user_id='bob')
        dm._upload_dataset(tags=[], private=True)
        self.assertEqual(
            self.uploads, [],
            "the upload reached the HF worker with another student's namespace")

    def test_the_refusal_is_reported_to_the_student_in_German(self):
        self._whoami(['alice'])
        dm = self._make(user_id='bob')
        dm._upload_dataset(tags=[], private=True)
        msg = dm._last_warning_message
        self.assertTrue(msg, 'the refusal is silent — the student is told nothing')
        self.assertIn('bob', msg)
        # Rule §1: literal umlauts, never ae/oe/ue transliterations.
        self.assertIn('prüfen', msg)
        self.assertNotIn('pruefen', msg)
        self.assertNotIn('abgelehnt: Der Roboter darf nicht in das HuggingFace-Konto "', msg)

    def test_an_ORG_the_token_belongs_to_is_accepted(self):
        # whoami returns the account name plus every org; a school org account
        # is the whole point of the org-namespace plan.
        self._whoami(['alice', 'schule-musterstadt'])
        dm = self._make(user_id='schule-musterstadt')
        dm._upload_dataset(tags=[], private=True)
        self.assertEqual(len(self.uploads), 1)

    def test_a_refusal_does_not_fall_through_to_the_direct_push(self):
        """The fallback path (no callback wired) must be gated too.

        `_upload_dataset` has TWO exits — the HfApiWorker callback and a direct
        `push_to_hub`. A refusal placed after the callback branch would leave
        the standalone path wide open.
        """
        self._whoami(['alice'])
        dm = self._make(user_id='bob')
        dm._upload_callback = None
        pushed = []

        class _DS:
            def push_to_hub(self, **kw):
                pushed.append(kw)
        dm._lerobot_dataset = _DS()
        dm._upload_dataset(tags=[], private=True)
        self.assertEqual(pushed, [], 'the direct push_to_hub bypassed the guard')

    def test_a_refusal_cannot_spin_the_state_machine(self):
        """`_upload_enqueued` is latched BEFORE the guard runs.

        The state machine can reach `_upload_dataset` on every tick; a refusal
        that left the flag clear would re-run whoami forever.
        """
        self._whoami(['alice'])
        dm = self._make(user_id='bob')
        for _ in range(5):
            dm._upload_dataset(tags=[], private=True)
        self.assertTrue(dm._upload_enqueued)
        self.assertEqual(self.uploads, [])
        self.assertEqual(
            len(self.whoami_calls), 1,
            'a refused upload re-queried whoami on every tick')


class ItFailsOPENWhenItCannotJudge(_Rig):
    """Recording with no cloud login is FULLY SUPPORTED — do not break it."""

    def _assert_allowed(self, whoami_result, why):
        self._whoami(whoami_result)
        dm = self._make(user_id='bob')
        dm._upload_dataset(tags=[], private=True)
        self.assertEqual(
            len(self.uploads), 1,
            f'the upload was REFUSED although the rig could not judge ({why})')
        self.assertFalse(dm._last_warning_message)

    def test_no_token_registered_allows(self):
        self._assert_allowed(
            Exception('No registered HuggingFace token found'), 'no token')

    def test_an_arbitrary_raise_allows(self):
        self._assert_allowed(RuntimeError('boom'), 'unexpected exception')

    def test_a_network_error_allows(self):
        self._assert_allowed(OSError('Network is unreachable'), 'network down')

    def test_a_timeout_allows(self):
        self._assert_allowed(TimeoutError('whoami timed out'), 'whoami timeout')

    def test_a_malformed_whoami_allows(self):
        # get_huggingface_user_id returning nothing usable is "cannot judge",
        # never "owns nothing" — the latter would refuse every upload.
        for empty in ([], None, ''):
            with self.subTest(empty=empty):
                self.uploads.clear()
                self._assert_allowed(empty, f'whoami returned {empty!r}')

    def test_a_failure_is_NOT_negatively_cached(self):
        """A blip must not disable the guard for the life of the process.

        The cache exists because whoami is an 8 s bounded network call on the
        end-of-recording save path. Caching the FAILURE would silently turn the
        guard off until the container restarts.
        """
        self._whoami(OSError('down'))
        dm = self._make(user_id='bob')
        dm._upload_dataset(tags=[], private=True)
        self.assertEqual(len(self.uploads), 1)  # allowed, as designed

        self._whoami(['alice'])                 # network comes back
        dm2 = self._make(user_id='bob')
        dm2._upload_dataset(tags=[], private=True)
        self.assertEqual(
            len(self.uploads), 1,
            'the earlier failure was cached, so the guard stayed off')

    def test_a_SUCCESS_is_cached(self):
        """The other half — one whoami per rig, not one per recording."""
        self._whoami(['alice'])
        for _ in range(3):
            self._make(user_id='alice')._upload_dataset(tags=[], private=True)
        self.assertEqual(len(self.uploads), 3)
        self.assertEqual(
            len(self.whoami_calls), 1,
            'whoami ran once per recording on the end-of-recording save path')


class TheSavePathIsConfinedToTheDatasetRoot(_Rig):
    """`_save_path` reaches a `shutil.rmtree` in `_check_dataset_exists`.

    `user_id` is client-supplied, and `root / '<absolute>'` DISCARDS the root,
    so this turned a per-frame recording check into an arbitrary tree delete.
    Sanitiser first, then a confine() that PROVES the result stayed inside.
    """

    def test_a_plain_user_id_lands_under_the_root(self):
        dm = self._make(user_id='alice')
        self.assertEqual(dm._save_path, self.root / 'alice' / 'omx_f_pick')

    def test_an_absolute_user_id_cannot_escape(self):
        dm = self._make(user_id='/etc/passwd')
        self.assertIn(self.root, dm._save_path.parents)

    def test_a_dotdot_user_id_cannot_escape(self):
        for bad in ('..', '../..', 'a/../../..'):
            with self.subTest(bad=bad):
                dm = self._make(user_id=bad)
                self.assertIn(self.root, dm._save_path.parents)

    def test_a_dot_only_user_id_becomes_a_placeholder(self):
        dm = self._make(user_id='..')
        self.assertTrue(str(dm._save_path).endswith('unknown-user/omx_f_pick'))

    def test_an_empty_user_id_becomes_a_placeholder(self):
        dm = self._make(user_id='')
        self.assertTrue(str(dm._save_path).endswith('unknown-user/omx_f_pick'))

    def test_the_repo_name_and_the_path_agree(self):
        # The guard reads the namespace off _save_repo_name while the rmtree
        # reads _save_path; if the sanitiser ever applied to only one of them,
        # the guard would judge a string the filesystem never sees.
        dm = self._make(user_id='Bob Smith!')
        self.assertTrue(str(dm._save_path).endswith(dm._save_repo_name))

    def test_the_confine_is_LOAD_BEARING_and_not_belt_over_a_complete_sanitiser(self):
        """`robot_type` is the one component the sanitiser never touches.

        `_save_repo_name` is `f'{safe_user_id}/{robot_type}_{safe_task_name}'`
        — user id and task name are both `re.sub`'d, robot_type is not. It is
        not client-supplied today (`robot_profiles.resolve` returns a fixed
        literal), which is exactly why nothing would notice if that ever
        changed: with the confine() deleted, every OTHER test in this class
        still passes, because the sanitiser alone already blocks the user_id
        escapes. Measured — removing the confine leaves this file green without
        this test.

        `'../..'` is NOT an escape (it collapses to a literal `.._pick`
        directory inside the root), so the case has to be deep enough to leave
        the root; `'/etc'` is not one either, because it is only ever a MIDDLE
        segment and pathlib's absolute-segment rule needs a leading one.
        """
        with self.assertRaises(self.dm_mod.dataset_paths.DatasetPathError):
            self.DataManager(
                self.root, '../../../etc', _TaskInfo(user_id='alice'))
        # Not vacuous: a normal robot_type still constructs.
        self.DataManager(self.root, 'omx_f', _TaskInfo(user_id='alice'))


if __name__ == '__main__':
    unittest.main()
