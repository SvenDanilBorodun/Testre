import os

import runpod


def get_runpod_endpoint():
    runpod.api_key = os.environ["RUNPOD_API_KEY"]
    endpoint_id = os.environ["RUNPOD_ENDPOINT_ID"]
    return runpod.Endpoint(endpoint_id)


def start_training_job(
    dataset_name: str,
    model_name: str,
    model_type: str,
    training_params: dict,
    training_id: int,
    supabase_url: str,
    supabase_key: str,
    hf_token: str,
) -> str:
    """Dispatch a training job to RunPod Serverless. Returns the job ID."""
    endpoint = get_runpod_endpoint()

    job_input = {
        "dataset_name": dataset_name,
        "model_name": model_name,
        "model_type": model_type,
        "training_params": training_params,
        "training_id": training_id,
        "supabase_url": supabase_url,
        "supabase_key": supabase_key,
        "hf_token": hf_token,
    }

    run_request = endpoint.run(job_input)
    return run_request.job_id


def cancel_training_job(job_id: str) -> bool:
    """Cancel a running RunPod job. Returns True if cancellation was requested."""
    endpoint = get_runpod_endpoint()
    try:
        endpoint.cancel(job_id)
        return True
    except Exception as e:
        print(f"RunPod cancel failed for job {job_id}: {e}")
        raise


def get_job_status(job_id: str) -> str:
    """Get the status of a RunPod job. Returns: QUEUED, IN_PROGRESS, COMPLETED, FAILED, CANCELLED."""
    endpoint = get_runpod_endpoint()
    status = endpoint.status(job_id)
    return status.status
