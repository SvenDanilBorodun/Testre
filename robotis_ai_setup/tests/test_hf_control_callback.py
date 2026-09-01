"""`ControlHfServer` — repository deletion is refused, and cancel unwinds cleanly.

`physical_ai_server.control_hf_server_callback` is reached over the
UNAUTHENTICATED rosbridge. (The SPA's student login gate,
physical_ai_manager/src/utils/authGate.js, now gates the Daten tab in the
BROWSER; that is one client, and the wire has no gate at all.) Two defects lived
in it:

  * **`mode='delete'` deleted any HuggingFace repo the rig's token reached.**
    `repo_id` is read verbatim off the wire and NOTHING validated it on any
    mode — the `local_dir` confine is explicitly scoped to `mode == 'upload'`.
    The chain was callback -> `hf_api_worker` -> `DataManager
    .delete_huggingface_repo` -> `HfApi().delete_repo(repo_id, ...)`. The mode
    has ZERO callers anywhere in the repo (the only `controlHfServer` call
    sites are `DatasetHuggingfaceSection` upload/download/cancel, `MyModels`
    download and `PolicyDownloadModal` download/cancel), and a wrong delete is
    irreversible, so it is refused outright rather than namespace-checked.

    **NOT the same 'delete' as `edit_worker.MODE_DELETE`**, which is EPISODE
    deletion in the Daten tab and a live student feature. Nothing here touches
    it; `test_dataset_edit_subprocess.py` owns that one.

  * **`return response` sat inside a `finally`** on the `mode == 'cancel'`
    branch. That swallows whatever is unwinding (Python 3.14 emits
    `SyntaxWarning: 'return' in a 'finally' block` for exactly this) and, when
    the cleanup raised, handed the caller an untouched response — `success`
    False by ROS default with an EMPTY message, so the student saw nothing.

The callback is extracted with `ast` and exec'd onto a fake node rather than
imported: `physical_ai_server.py` pulls in rclpy, torch, lerobot and cv2 at
module level, none of which this logic needs. Same technique as
`physical_ai_server/test/test_workshop_manual_callbacks.py`. `dataset_paths` is
loaded FOR REAL (stdlib-only) because the upload branch's confine is one of the
things the not-vacuous half asserts.
"""

import ast
import importlib.util
import pathlib
import shutil
import sys
import tempfile
import textwrap
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG_ROOT = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
)
SERVER_PY = PKG_ROOT / 'physical_ai_server.py'
PATHS_PY = PKG_ROOT / 'data_processing' / 'dataset_paths.py'

# Loaded under a PRIVATE name. Two sibling test modules
# (test_dataset_path_confinement, test_upload_namespace_guard) install
# dataset_paths under its CANONICAL name and manage that entry carefully; this
# file only needs the module OBJECT to hand to the exec namespace, so it must
# not join in that bookkeeping.
_PATHS_MODULE_NAME = '_edubotics_hfctl_dataset_paths'


def _load_dataset_paths():
    if _PATHS_MODULE_NAME in sys.modules:
        return sys.modules[_PATHS_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_PATHS_MODULE_NAME, str(PATHS_PY))
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PATHS_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_PATHS_MODULE_NAME, None)
        raise
    return module


def _extract(names, extra_globals):
    """Exec the named top-level-or-method FunctionDefs out of the server module.

    Raises rather than returning a short dict when a name is missing — a
    renamed callback must fail loudly here, not quietly leave every assertion
    below unexecuted.
    """
    source = SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    ns = dict(extra_globals)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, str(SERVER_PY), 'exec'), ns)  # noqa: S102
            found[node.name] = ns[node.name]
    missing = sorted(set(names) - set(found))
    if missing:
        raise AssertionError(
            f'{missing} not found in {SERVER_PY.name} — either renamed or this '
            f'test is stale, and both mean nothing below is checked')
    return found


_DP = _load_dataset_paths()
_CB = _extract(['control_hf_server_callback'], {'dataset_paths': _DP})
_CONTROL = _CB['control_hf_server_callback']


class _Fatal(BaseException):
    """A BaseException, so `except Exception` cannot catch it.

    `return` in a `finally` discards an in-flight exception of ANY class, and
    the branch's own `except Exception` hides that for ordinary errors. A
    BaseException is what makes the swallow observable.
    """


class _Logger:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.infos = []

    def error(self, msg):
        self.errors.append(msg)

    def warning(self, msg):
        self.warnings.append(msg)

    def info(self, msg):
        self.infos.append(msg)


class _Worker:
    def __init__(self, alive=True, busy=False, accept=True):
        self._alive = alive
        self._busy = busy
        self._accept = accept
        self.requests = []

    def is_alive(self):
        return self._alive

    def is_busy(self):
        return self._busy

    def send_request(self, request_data):
        self.requests.append(request_data)
        return self._accept


class _Req(types.SimpleNamespace):
    """The ControlHfServer request. Every field is client-supplied."""


class _Resp:
    """A ROS srv response as the wire actually hands it over: bool default
    False, string default ''. The empty message is what makes the
    return-in-finally regression visible."""

    def __init__(self):
        self.success = False
        self.message = ''


class _Node:
    def __init__(self, worker=None, cancel_in_progress=False, cleanup_raises=None):
        self.hf_cancel_on_progress = cancel_in_progress
        self.hf_api_worker = worker
        self._cleanup_raises = cleanup_raises
        self.cleanup_calls = 0
        self.init_calls = 0
        self._logger = _Logger()

    def get_logger(self):
        return self._logger

    def _cleanup_hf_api_worker_with_threading(self):
        self.cleanup_calls += 1
        if self._cleanup_raises is not None:
            raise self._cleanup_raises

    def _init_hf_api_worker(self):
        self.init_calls += 1
        self.hf_api_worker = _Worker()


def _request(mode, repo_id='someone/dataset', local_dir='', repo_type='dataset',
             author=''):
    return _Req(mode=mode, repo_id=repo_id, local_dir=local_dir,
                repo_type=repo_type, author=author)


def _call(node, request):
    response = _Resp()
    return _CONTROL(node, request, response)


class DeletionIsRefusedAtTheWire(unittest.TestCase):
    """`mode='delete'` never reaches the worker, in any node state."""

    def test_delete_is_refused(self):
        worker = _Worker()
        node = _Node(worker=worker)
        resp = _call(node, _request('delete', repo_id='opfer/datensatz'))
        self.assertFalse(resp.success)
        self.assertEqual(
            worker.requests, [],
            'a repository deletion was dispatched to the HF worker')

    def test_the_refusal_is_german_with_literal_umlauts(self):
        node = _Node(worker=_Worker())
        msg = _call(node, _request('delete')).message
        self.assertTrue(msg, 'the refusal is silent — the student is told nothing')
        # Rule §1: literal umlauts, never ae/oe/ue/ss transliterations.
        self.assertIn('löschen', msg)
        self.assertNotIn('loeschen', msg)
        self.assertIn('verfügbar', msg)
        self.assertNotIn('verfuegbar', msg)

    def test_the_refusal_does_not_echo_the_repo_id_back(self):
        # Same rule the path-confinement refusals follow: a refusal rendered in
        # the browser must not reflect caller-supplied text.
        node = _Node(worker=_Worker())
        resp = _call(node, _request('delete', repo_id='geheim-org/interner-satz'))
        self.assertNotIn('geheim-org', resp.message)
        self.assertNotIn('interner-satz', resp.message)

    def test_delete_is_refused_while_a_cancel_is_in_progress(self):
        """The gate is UNCONDITIONAL — placed above the canceling check.

        Below it, a delete arriving mid-cancel would take the English
        'currently canceling' branch instead. Same outcome for the worker, but
        the student gets a different sentence for the same request, and the
        refusal would then depend on worker state rather than on the mode.
        """
        worker = _Worker()
        node = _Node(worker=worker, cancel_in_progress=True)
        resp = _call(node, _request('delete'))
        self.assertFalse(resp.success)
        self.assertEqual(worker.requests, [])
        self.assertIn('löschen', resp.message)

    def test_delete_never_restarts_the_worker(self):
        """The gate precedes the worker restart, so a delete cannot spawn one.

        `_init_hf_api_worker` forks a 'spawn' multiprocessing child; a refused
        mode must not pay for one.
        """
        node = _Node(worker=None)
        _call(node, _request('delete'))
        self.assertEqual(node.init_calls, 0)

    def test_delete_never_reaches_a_dead_or_busy_worker_branch(self):
        node = _Node(worker=_Worker(alive=True, busy=True))
        resp = _call(node, _request('delete'))
        self.assertFalse(resp.success)
        self.assertIn('löschen', resp.message)
        self.assertNotIn('busy', resp.message)


class TheOtherModesStillDispatch(unittest.TestCase):
    """Not vacuous: the refusal must be distinguishable from a dead service."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix='hfctl_')).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_download_dispatches(self):
        worker = _Worker()
        node = _Node(worker=worker)
        resp = _call(node, _request('download', repo_id='alice/satz'))
        self.assertTrue(resp.success)
        self.assertEqual(len(worker.requests), 1)
        self.assertEqual(worker.requests[0]['mode'], 'download')

    def test_upload_dispatches_with_a_confined_local_dir(self):
        worker = _Worker()
        node = _Node(worker=worker)
        inside = self.root / 'alice' / 'omx_f_pick'
        original = _DP.browsable_roots
        _DP.browsable_roots = lambda: [self.root]
        self.addCleanup(setattr, _DP, 'browsable_roots', original)
        resp = _call(node, _request('upload', repo_id='alice/omx_f_pick',
                                    local_dir=str(inside)))
        self.assertTrue(resp.success, resp.message)
        self.assertEqual(len(worker.requests), 1)
        self.assertEqual(worker.requests[0]['local_dir'], str(inside))

    def test_an_escaping_upload_local_dir_is_still_refused(self):
        worker = _Worker()
        node = _Node(worker=worker)
        original = _DP.browsable_roots
        _DP.browsable_roots = lambda: [self.root]
        self.addCleanup(setattr, _DP, 'browsable_roots', original)
        resp = _call(node, _request('upload', local_dir='/root'))
        self.assertFalse(resp.success)
        self.assertEqual(worker.requests, [])

    def test_cancel_still_works(self):
        node = _Node(worker=_Worker())
        resp = _call(node, _request('cancel'))
        self.assertTrue(resp.success)
        self.assertEqual(node.cleanup_calls, 1)
        self.assertFalse(node.hf_cancel_on_progress)


class TheCancelBranchUnwindsCleanly(unittest.TestCase):
    """`return` no longer sits in the `finally`."""

    def test_a_cancel_that_raises_reports_a_german_failure(self):
        """With the return in the `finally`, the response was NEVER touched.

        The except handler only logged, so the caller got `success=False` with
        an EMPTY message — a silent failure in the UI.
        """
        node = _Node(worker=_Worker(), cleanup_raises=RuntimeError('boom'))
        resp = _call(node, _request('cancel'))
        self.assertFalse(resp.success)
        self.assertTrue(
            resp.message, 'a failed cancel told the student nothing at all')
        self.assertIn('Abbrechen', resp.message)
        self.assertNotIn('fehlgeschlagenn', resp.message)

    def test_a_failed_cancel_still_clears_the_flag(self):
        """The half the `finally` legitimately exists for.

        Left set, `hf_cancel_on_progress` refuses every later HF request with
        'currently canceling' for the life of the node.
        """
        node = _Node(worker=_Worker(), cleanup_raises=RuntimeError('boom'))
        _call(node, _request('cancel'))
        self.assertFalse(node.hf_cancel_on_progress)

    def test_a_base_exception_is_no_longer_swallowed(self):
        """THE defect a `return` in a `finally` actually is.

        `except Exception` cannot catch a BaseException, so with the return in
        the finally the exception was DISCARDED and the callback returned a
        success-less, message-less response as if nothing had happened. It must
        propagate — which is what every other mode in this callback already
        does (the outer handler also catches only `Exception`).
        """
        node = _Node(worker=_Worker(), cleanup_raises=_Fatal('interrupted'))
        with self.assertRaises(_Fatal):
            _call(node, _request('cancel'))
        # ...and the flag is still cleared on the way out, because that is
        # what the `finally` is now for.
        self.assertFalse(node.hf_cancel_on_progress)


def _returns_in_finally(path):
    """Every `return` that sits directly in a `finally` body of `path`.

    Nested function/class bodies are skipped: a `return` inside a closure
    DEFINED in a finally block belongs to that closure and exits nothing.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'))
    hits = []
    _scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    _tries = (ast.Try, getattr(ast, 'TryStar', ast.Try))

    def walk(node, in_finally):
        if isinstance(node, _scopes):
            # A new scope: whatever finally encloses the DEFINITION does not
            # enclose the body.
            in_finally = False
        elif isinstance(node, ast.Return) and in_finally:
            hits.append(node.lineno)
        if isinstance(node, _tries):
            for section in (node.body, node.handlers, node.orelse):
                for stmt in section:
                    walk(stmt, in_finally)
            for stmt in node.finalbody:
                walk(stmt, True)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, in_finally)

    walk(tree, False)
    return sorted(set(hits))


class NoReturnSitsInAFinally(unittest.TestCase):
    """Structural fence, version-independent.

    Python 3.14 (PEP 765) emits `SyntaxWarning: 'return' in a 'finally' block`
    for this, but CI pins 3.11 and never does — so a warning-based check would
    be vacuously green on the machine that gates merges. This one is not.
    """

    def test_the_server_module_has_none(self):
        self.assertTrue(SERVER_PY.is_file(), SERVER_PY)
        self.assertEqual(
            _returns_in_finally(SERVER_PY), [],
            'a `return` in a `finally` swallows whatever is unwinding')

    def test_the_detector_is_not_vacuous(self):
        # A detector that found nothing anywhere would make the assertion above
        # meaningless, so prove it on a known-positive sample.
        sample = pathlib.Path(tempfile.mkdtemp(prefix='finally_')) / 'x.py'
        self.addCleanup(shutil.rmtree, sample.parent, ignore_errors=True)
        sample.write_text(
            'def f():\n'
            '    try:\n'
            '        pass\n'
            '    finally:\n'
            '        return 1\n',
            encoding='utf-8')
        self.assertEqual(_returns_in_finally(sample), [5])
        sample.write_text(
            'def f():\n'
            '    try:\n'
            '        pass\n'
            '    finally:\n'
            '        def g():\n'
            '            return 1\n'
            '        g()\n',
            encoding='utf-8')
        self.assertEqual(
            _returns_in_finally(sample), [],
            'a return inside a closure defined in a finally is not a finally-return')


if __name__ == '__main__':
    unittest.main()
