"""Regression tests for app.services.modal_client.

Locks in the fix for "cancel just continues on Modal": cancel_training_job and
get_job_status must drive Modal's BLOCKING API off the event loop (via
asyncio.to_thread), NOT the .aio() async variant on a from_id handle, which was
raising at runtime under uvicorn and leaving the GPU billing to the timeout cap.

A single complete `modal` stub is installed in sys.modules and modal_client is
reloaded against it, so the stub's exception CLASSES are the exact ones
modal_client catches (no cross-stub identity mismatch). Each test swaps the
FunctionCall.from_id behaviour via the module-level _NEXT_CALL hook.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest


# --- One complete modal stub, installed before (re)importing modal_client ---
_REC: dict = {}
_NEXT_CALL = {"factory": None}  # test sets factory(job_id) -> object with cancel()/get()

_modal = types.ModuleType("modal")
_modal.exception = types.SimpleNamespace(
    TimeoutError=type("TimeoutError", (Exception,), {}),
    FunctionTimeoutError=type("FunctionTimeoutError", (Exception,), {}),
    InputCancellation=type("InputCancellation", (Exception,), {}),
    RemoteError=type("RemoteError", (Exception,), {}),
    ExecutionError=type("ExecutionError", (Exception,), {}),
)


class _FunctionCall:
    @staticmethod
    def from_id(job_id):
        _REC["from_id"] = job_id
        return _NEXT_CALL["factory"](job_id)


_modal.FunctionCall = _FunctionCall
sys.modules["modal"] = _modal

# Reload modal_client so its module-global `modal` is bound to OUR stub.
import app.services.modal_client as modal_client  # noqa: E402
modal_client = importlib.reload(modal_client)

EXC = _modal.exception


class TestCancelTrainingJob(unittest.TestCase):
    def setUp(self):
        _REC.clear()

    def test_cancel_uses_sync_api_with_terminate_containers(self):
        class _Call:
            # plain sync method, NO .aio — if the code reverted to
            # `call.cancel.aio(...)` it would AttributeError and fail here.
            def cancel(self, terminate_containers=False):
                _REC["cancel"] = terminate_containers

        _NEXT_CALL["factory"] = lambda jid: _Call()
        self.assertTrue(asyncio.run(modal_client.cancel_training_job("fc-ABC")))
        self.assertEqual(_REC["from_id"], "fc-ABC")
        self.assertIs(_REC["cancel"], True)  # migration-023 cost-bomb fix

    def test_cancel_propagates_failure(self):
        class _Call:
            def cancel(self, terminate_containers=False):
                raise RuntimeError("modal exploded")

        _NEXT_CALL["factory"] = lambda jid: _Call()
        with self.assertRaises(RuntimeError):
            asyncio.run(modal_client.cancel_training_job("fc-X"))


class TestGetJobStatus(unittest.TestCase):
    def _status_when_get_raises(self, exc):
        class _Call:
            def get(self, timeout=None):
                if exc is not None:
                    raise exc
                return None

        _NEXT_CALL["factory"] = lambda jid: _Call()
        return asyncio.run(modal_client.get_job_status("fc-1"))

    def test_completed(self):
        self.assertEqual(self._status_when_get_raises(None), "COMPLETED")

    def test_in_progress_on_timeout(self):
        self.assertEqual(self._status_when_get_raises(EXC.TimeoutError()), "IN_PROGRESS")

    def test_in_progress_on_builtin_timeout(self):
        self.assertEqual(self._status_when_get_raises(TimeoutError()), "IN_PROGRESS")

    def test_timed_out(self):
        self.assertEqual(
            self._status_when_get_raises(EXC.FunctionTimeoutError()), "TIMED_OUT"
        )

    def test_cancelled(self):
        self.assertEqual(
            self._status_when_get_raises(EXC.InputCancellation()), "CANCELLED"
        )

    def test_failed(self):
        self.assertEqual(self._status_when_get_raises(EXC.RemoteError()), "FAILED")

    def test_unknown_failopen(self):
        # Unrecognised exception must fail open to UNKNOWN, never mismark a live
        # row failed.
        self.assertEqual(
            self._status_when_get_raises(ValueError("weird")), modal_client.UNKNOWN_STATUS
        )


if __name__ == "__main__":
    unittest.main()
