"""The Hugging Face status a student is toasted is German and carries no glyph.

``hf_api_worker.HfApiWorker.check_task_status`` builds the ``message`` of the
``/huggingface/status`` topic, and the React client toasts it verbatim for every
``Success`` and ``Failed`` (``hooks/useRosTopicSubscription.js``). It used to
wrap the worker's (German) sentence in English plus an emoji —
``'✅ HF API task completed successfully:\\n<sentence>'`` — so a student saw
English and a glyph after every upload. The English sentence now goes to the
logger only, the client gets the worker's own sentence, and every sentence the
worker process itself produces is German (Rule §1), because each can reach that
toast.

``hf_api_worker`` imports ``DataManager`` (a large ROS/cv2/HF tree), so the
module is loaded by path with that one import stubbed — the approach of
``test_upload_namespace_guard.py``.
"""

import ast
import importlib.util
import pathlib
import queue
import sys
import types
import unittest

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKER_PATH = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
    / 'data_processing' / 'hf_api_worker.py'
)

# The icon ranges the no-icon-glyph fences ban (tests/test_no_icon_glyphs.py).
_BANNED = [
    (0x1F000, 0x1FAFF), (0x2300, 0x23FF), (0x25A0, 0x25FF), (0x2600, 0x27BF),
    (0x2195, 0x21FF), (0x2900, 0x297F), (0x2B00, 0x2BFF), (0x2139, 0x2139),
    (0x22EF, 0x22EF), (0xFE0F, 0xFE0F), (0x20E3, 0x20E3),
]


def _has_glyph(text):
    return any(any(lo <= ord(c) <= hi for lo, hi in _BANNED) for c in text)


class _FakeDataManager:
    _last_hf_failure_reason_de = None
    upload_ok = True

    @staticmethod
    def set_progress_queue(_q):
        return None

    @classmethod
    def upload_huggingface_repo(cls, **_kw):
        return cls.upload_ok

    @classmethod
    def download_huggingface_repo(cls, **_kw):
        return True

    @staticmethod
    def delete_huggingface_repo(**_kw):
        return None

    @staticmethod
    def get_huggingface_repo_list(**_kw):
        return []


def _load_worker_module():
    for name in ('physical_ai_server', 'physical_ai_server.data_processing'):
        sys.modules.setdefault(name, types.ModuleType(name))
    dm = types.ModuleType('physical_ai_server.data_processing.data_manager')
    dm.DataManager = _FakeDataManager
    sys.modules['physical_ai_server.data_processing.data_manager'] = dm
    spec = importlib.util.spec_from_file_location('hf_api_worker_under_test', WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_worker_module()


def _worker_with_result(task_result, mode='upload'):
    worker = MODULE.HfApiWorker.__new__(MODULE.HfApiWorker)
    worker.logger = MODULE.logging.getLogger('HfApiWorkerUnderTest')
    worker.logger.disabled = True
    worker.is_processing = True
    worker.current_task = {'mode': mode, 'repo_id': 'schueler/daten', 'local_path': ''}
    worker.current_progress = {'current': 0, 'total': 0, 'percentage': 0.0}
    worker.last_logged_current_progress = -1
    worker.is_alive = lambda: True
    worker.get_progress_from_progress_queue = lambda: {'current': 1, 'total': 1, 'percentage': 100.0}
    worker.get_result = lambda block=False, timeout=0.1: task_result
    return worker


def _run_loop(requests):
    """Run the worker process loop in THIS process over plain queues."""
    inq, outq, progq = queue.Queue(), queue.Queue(), queue.Queue()
    for r in requests:
        inq.put(r)
    inq.put(None)
    MODULE.HfApiWorker._worker_process_loop(inq, outq, progq)
    out = []
    while not outq.empty():
        out.append(outq.get_nowait())
    return out


class TheClientGetsTheWorkersOwnGermanSentence(BoundedTestCase):

    def test_a_success_is_the_sentence_itself_no_english_prefix_no_glyph(self):
        sentence = 'Hugging Face-Upload abgeschlossen: schueler/daten'
        result = _worker_with_result(('success', sentence)).check_task_status()
        self.assertEqual(result['status'], 'Success')
        self.assertEqual(result['operation'], 'upload')
        self.assertEqual(result['message'], sentence)
        self.assertFalse(_has_glyph(result['message']))
        self.assertNotIn('HF API task', result['message'])

    def test_a_failure_is_the_sentence_itself(self):
        sentence = 'Upload zu Hugging Face fehlgeschlagen:\nschueler/daten'
        result = _worker_with_result(('error', sentence)).check_task_status()
        self.assertEqual(result['status'], 'Failed')
        self.assertEqual(result['message'], sentence)
        self.assertFalse(_has_glyph(result['message']))

    def test_an_error_while_checking_is_german_too(self):
        worker = _worker_with_result(None)

        def boom(block=False, timeout=0.1):
            raise RuntimeError('queue kaputt')
        worker.get_result = boom
        result = worker.check_task_status()
        self.assertEqual(result['status'], 'Failed')
        self.assertTrue(result['message'].startswith('Der Status der Hugging Face-Aufgabe'),
                        result['message'])
        self.assertFalse(_has_glyph(result['message']))


class EverySentenceTheWorkerProcessSendsIsGerman(BoundedTestCase):

    def test_upload_download_delete_and_both_lists(self):
        out = _run_loop([
            {'mode': 'upload', 'repo_id': 'schueler/daten', 'repo_type': 'dataset', 'local_dir': '/x'},
            {'mode': 'download', 'repo_id': 'schueler/daten', 'repo_type': 'dataset'},
            {'mode': 'delete', 'repo_id': 'schueler/daten', 'repo_type': 'dataset'},
            {'mode': 'get_dataset_list', 'author': 'schueler'},
            {'mode': 'get_model_list', 'author': 'schueler'},
            {'mode': 'bogus'},
        ])
        self.assertEqual([s for s, _ in out], ['success'] * 5 + ['error'])
        messages = [m for _, m in out]
        self.assertEqual(messages, [
            'Hugging Face-Upload abgeschlossen: schueler/daten',
            'Hugging Face-Download abgeschlossen: schueler/daten',
            'Hugging Face-Repo gelöscht: schueler/daten',
            'Datensatzliste von schueler geladen.',
            'Modellliste von schueler geladen.',
            'Unbekannte Hugging Face-Aktion: bogus',
        ])
        for m in messages:
            self.assertFalse(_has_glyph(m), m)


class NoGlyphInAnyLogLine(BoundedTestCase):
    """The worker's, the data manager's and the progress tracker's log and
    print lines are plain text (they only reach a developer's console)."""

    def test_the_three_modules_hold_no_glyph_in_any_string(self):
        base = WORKER_PATH.parent
        for name in ('hf_api_worker.py', 'data_manager.py', 'progress_tracker.py'):
            tree = ast.parse((base / name).read_text(encoding='utf-8'))
            bad = [n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str) and _has_glyph(n.value)]
            self.assertEqual(bad, [], name)


if __name__ == '__main__':
    unittest.main()
