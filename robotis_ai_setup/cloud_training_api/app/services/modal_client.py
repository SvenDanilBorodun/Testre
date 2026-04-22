"""Modal dispatch client — replaces the old RunPod client.

Keeps the same three function names (start_training_job, cancel_training_job,
get_job_status) so routes/training.py only needs its imports updated.

Credentials for the Modal worker (SUPABASE_URL, SUPABASE_ANON_KEY, HF_TOKEN)
live in the Modal Secret `edubotics-training-secrets` and are injected into
the worker's env at invocation — we do NOT pass them through the payload.
"""

import logging
import os

import modal

logger = logging.getLogger(__name__)


def _get_train_function():
    """Resolve the deployed Modal training function by name.

    MODAL_TOKEN_ID + MODAL_TOKEN_SECRET must be set in the Railway env; the
    Modal SDK picks them up automatically. App + function names are env-driven
    so the same code can point at a staging deployment without a code change.
    """
    app_name = os.environ.get("MODAL_TRAINING_APP_NAME", "edubotics-training")
    fn_name = os.environ.get("MODAL_TRAINING_FUNCTION_NAME", "train")
    return modal.Function.from_name(app_name, fn_name)


def start_training_job(
    dataset_name: str,
    model_name: str,
    model_type: str,
    training_params: dict,
    training_id: int,
    worker_token: str,
) -> str:
    """Dispatch a training job to Modal. Returns the FunctionCall object id."""
    fn = _get_train_function()
    call = fn.spawn(
        dataset_name=dataset_name,
        model_name=model_name,
        model_type=model_type,
        training_params=training_params,
        training_id=training_id,
        worker_token=worker_token,
    )
    return call.object_id


def cancel_training_job(job_id: str) -> bool:
    """Cancel a running Modal job. Returns True on success, raises on failure."""
    try:
        modal.FunctionCall.from_id(job_id).cancel(terminate_containers=True)
        return True
    except Exception as e:
        logger.warning("Modal cancel failed for call %s: %s", job_id, e)
        raise


def get_job_status(job_id: str) -> str:
    """Return a status string compatible with the existing MODAL_TO_DB_STATUS
    mapping in routes/training.py.

    Modal does not expose a QUEUED-vs-RUNNING distinction via FunctionCall.get,
    so "IN_PROGRESS" covers both. The Railway-side stall detector uses Supabase
    `last_progress_at` for liveness, which is independent of this signal.

    One of: QUEUED, IN_PROGRESS, COMPLETED, FAILED, CANCELLED, TIMED_OUT.
    """
    call = modal.FunctionCall.from_id(job_id)
    try:
        call.get(timeout=0)
        return "COMPLETED"
    except modal.exception.TimeoutError:
        # Client-side poll expired — job is still queued or running.
        return "IN_PROGRESS"
    except modal.exception.FunctionTimeoutError:
        # Function itself exceeded its server-side `timeout=` limit.
        return "TIMED_OUT"
    except modal.exception.InputCancellation:
        return "CANCELLED"
    except (modal.exception.RemoteError, modal.exception.ExecutionError):
        return "FAILED"
    except Exception as e:
        # User code (run_training) raised inside the container. The function
        # body catches its own exceptions and returns {"status": "failed"}
        # cleanly, so reaching here implies a serialization/infra issue —
        # treat as FAILED for reconciliation.
        logger.warning("Modal get(timeout=0) raised non-Modal exception: %r", e)
        return "FAILED"
