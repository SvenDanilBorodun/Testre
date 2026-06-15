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
# Model the REAL modal SDK hierarchy: FunctionTimeoutError subclasses
# modal.exception.TimeoutError. get_job_status must therefore catch
# FunctionTimeoutError BEFORE the generic TimeoutError, or TIMED_OUT is
# unreachable — this stub relationship is what makes test_timed_out actually
# verify that ordering (a flat-Exception stub gave false confidence).
_TimeoutError = type("TimeoutError", (Exception,), {})
_FunctionTimeoutError = type("FunctionTimeoutError", (_TimeoutError,), {})
_modal.exception = types.SimpleNamespace(
    TimeoutError=_TimeoutError,
    FunctionTimeoutError=_FunctionTimeoutError,
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


# Function.from_name(app, fn).spawn.aio(**kwargs) -> object with .object_id.
# Used by the GPU-tier routing tests for start_training_job.
class _SpawnNamespace:
    async def aio(self, **kwargs):
        _REC["spawn_kwargs"] = kwargs
        return types.SimpleNamespace(object_id="fc-NEW")


class _Function:
    spawn = _SpawnNamespace()

    @staticmethod
    def from_name(app_name, fn_name):
        _REC["resolved"] = (app_name, fn_name)
        return _Function()


_modal.Function = _Function
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
                raise exc

        _NEXT_CALL["factory"] = lambda jid: _Call()
        return asyncio.run(modal_client.get_job_status("fc-1"))

    def _status_when_get_returns(self, value):
        class _Call:
            def get(self, timeout=None):
                return value

        _NEXT_CALL["factory"] = lambda jid: _Call()
        return asyncio.run(modal_client.get_job_status("fc-1"))

    # --- Return-value inspection (the C2 fix). run_training NEVER raises on an
    # application-level failure — it catches everything and returns a status
    # dict — so a bare "the Modal call returned" must NOT be read as success.
    # Mapping any return to COMPLETED->succeeded let a dropped worker
    # terminal-write get reconciled to 'succeeded' over a failed run. ---
    def test_returned_succeeded_is_completed(self):
        self.assertEqual(
            self._status_when_get_returns({"status": "succeeded", "model_url": "x"}),
            "COMPLETED",
        )

    def test_returned_failed_is_failed(self):
        self.assertEqual(
            self._status_when_get_returns({"status": "failed", "error": "boom"}),
            "FAILED",
        )

    def test_returned_canceled_is_cancelled(self):
        self.assertEqual(
            self._status_when_get_returns({"status": "canceled"}), "CANCELLED"
        )

    def test_returned_none_or_unexpected_is_failed(self):
        # None / non-dict / missing-status payload -> FAILED, so the reconciler
        # frees the credit instead of reporting a model that doesn't exist.
        self.assertEqual(self._status_when_get_returns(None), "FAILED")
        self.assertEqual(self._status_when_get_returns({"foo": 1}), "FAILED")
        self.assertEqual(self._status_when_get_returns("weird"), "FAILED")

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


class TestStartTrainingJobRouting(unittest.TestCase):
    """gpu_tier selects which deployed Modal function is spawned: L4 `train`
    for ACT-class, A100 `train_vla` for the VLAs. Cancel/get_job_status stay
    tier-agnostic (FunctionCall.from_id), so only dispatch needs the routing."""

    def setUp(self):
        _REC.clear()

    def _spawn(self, **kw):
        base = dict(
            dataset_name="u/d", model_name="u/m", model_type="pi05",
            training_params={}, training_id=1, worker_token="tok",
        )
        base.update(kw)
        return asyncio.run(modal_client.start_training_job(**base))

    def test_a100_routes_to_train_vla(self):
        oid = self._spawn(gpu_tier="a100")
        self.assertEqual(oid, "fc-NEW")
        self.assertEqual(_REC["resolved"], ("edubotics-training", "train_vla"))

    def test_l4_routes_to_train(self):
        self._spawn(gpu_tier="l4", model_type="act")
        self.assertEqual(_REC["resolved"], ("edubotics-training", "train"))

    def test_default_tier_is_l4(self):
        # Omitting gpu_tier must keep the legacy L4 `train` behaviour.
        self._spawn(model_type="act")
        self.assertEqual(_REC["resolved"], ("edubotics-training", "train"))


if __name__ == "__main__":
    unittest.main()
