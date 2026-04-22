"""Modal dispatch client.

Exposes three async helpers (start_training_job, cancel_training_job,
get_job_status) used by routes/training.py. All three use Modal's `.aio()`
methods and must be awaited — FastAPI handlers stay non-blocking.

Credentials for the Modal worker (SUPABASE_URL, SUPABASE_ANON_KEY, HF_TOKEN)
live in the Modal Secret `edubotics-training-secrets` and are injected into
the worker's env at invocation — we do NOT pass them through the payload.
"""

import logging
import os

import modal

logger = logging.getLogger(__name__)

# Sentinel returned by get_job_status() when Modal raised an exception we
# don't recognize (unknown SDK error class, transient network issue,
# deserialization glitch, etc.). Not present in MODAL_TO_DB_STATUS in
# training.py, which means _sync_modal_status falls back to the row's
# current status — no accidental state transitions on unrecognized errors.
UNKNOWN_STATUS = "UNKNOWN"


def _get_train_function():
    """Resolve the deployed Modal training function by name.

    MODAL_TOKEN_ID + MODAL_TOKEN_SECRET must be set in the Railway env; the
    Modal SDK picks them up automatically. App + function names are env-driven
    so the same code can point at a staging deployment without a code change.
    """
    app_name = os.environ.get("MODAL_TRAINING_APP_NAME", "edubotics-training")
    fn_name = os.environ.get("MODAL_TRAINING_FUNCTION_NAME", "train")
    return modal.Function.from_name(app_name, fn_name)


async def start_training_job(
    dataset_name: str,
    model_name: str,
    model_type: str,
    training_params: dict,
    training_id: int,
    worker_token: str,
) -> str:
    """Dispatch a training job to Modal. Returns the FunctionCall object id."""
    fn = _get_train_function()
    call = await fn.spawn.aio(
        dataset_name=dataset_name,
        model_name=model_name,
        model_type=model_type,
        training_params=training_params,
        training_id=training_id,
        worker_token=worker_token,
    )
    return call.object_id


async def cancel_training_job(job_id: str) -> bool:
    """Cancel a running Modal job. Returns True on success, raises on failure."""
    try:
        call = modal.FunctionCall.from_id(job_id)
        await call.cancel.aio(terminate_containers=True)
        return True
    except Exception as e:
        logger.warning("Modal cancel failed for call %s: %s", job_id, e)
        raise


async def get_job_status(job_id: str) -> str:
    """Return a status string compatible with the MODAL_TO_DB_STATUS mapping
    in routes/training.py.

    Modal does not expose a QUEUED-vs-RUNNING distinction via FunctionCall.get,
    so "IN_PROGRESS" covers both. The Railway-side stall detector uses Supabase
    `last_progress_at` for liveness, which is independent of this signal.

    On an exception we don't recognize, returns UNKNOWN_STATUS rather than
    "FAILED" — so a running job doesn't get mismarked failed if Modal's SDK
    evolves or raises an unexpected class. MODAL_TO_DB_STATUS does not list
    UNKNOWN, so the reconciler leaves the row's existing status alone.

    Normal return values: QUEUED, IN_PROGRESS, COMPLETED, FAILED, CANCELLED,
    TIMED_OUT, UNKNOWN.
    """
    try:
        call = modal.FunctionCall.from_id(job_id)
        await call.get.aio(timeout=0)
        return "COMPLETED"
    except (modal.exception.TimeoutError, TimeoutError):
        # Client-side poll expired — job is still queued or running.
        # Modal's SDK raises the BUILT-IN TimeoutError for a still-in-flight
        # call (not the Modal-namespaced one), so we catch both to be safe
        # across SDK versions. Mismatching this fires every time the UI
        # polls /trainings/list and flips live rows to failed.
        return "IN_PROGRESS"
    except modal.exception.FunctionTimeoutError:
        # Function itself exceeded its server-side `timeout=` limit.
        return "TIMED_OUT"
    except modal.exception.InputCancellation:
        return "CANCELLED"
    except (modal.exception.RemoteError, modal.exception.ExecutionError):
        return "FAILED"
    except Exception as e:
        # Unknown SDK error class, deserialization issue, transient network
        # blip. Fail open — don't touch the row's status, let the next poll
        # try again. See UNKNOWN_STATUS comment at top of file.
        logger.warning("Modal get(timeout=0) raised unrecognized exception: %r", e)
        return UNKNOWN_STATUS
