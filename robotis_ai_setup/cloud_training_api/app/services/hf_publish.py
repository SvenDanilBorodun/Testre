"""Publish, mirror and failed-log upload of managed ACT trainings (spec §4.5, §4.6, R4).

Everything here runs in ONE dedicated ``ThreadPoolExecutor`` (3 workers,
``training-publish``), never the default executor and never awaited by a
request handler (§4.13). Two ``BoundedSemaphore``s are taken BEFORE a task is
submitted (TR-8 n12): at most 2 publishes (failed-log uploads count as
publishes) and 1 mirror, so three mirrors can never occupy all three workers.

Publish (one per training, lease-guarded by ``claim_training_publish`` with this
process's ``BOOT_ID``, renewed every ``PUBLISH_RENEW_S``):
  1. claim → 2. stream the final checkpoint's 7 files to a temp dir, size +
  sha256 against the manifest (R4) → 3. validate (§3.5 JSON rules,
  ``act_config_summary``, config.json == train_config.policy, processor steps,
  state files; a refusal fails the row with S7 at once) → 4. our one own file,
  ``edubotics_model_meta.json`` (the legacy worker's byte format) → 5.
  ``create_repo(exist_ok)`` + ONE ``create_commit`` (8 root files, the 7 files
  under ``checkpoints/<step_id>/pretrained_model/``, the training log) → 6.
  read back every path (LFS sha256 + size; non-LFS git blob sha1) → 7.
  ``finish_training(succeeded)`` → 8. the janitor.
An HF transport error or a read-back mismatch keeps the row publishing:
``publish_tries += 1``, the lease lapses, the sweep retries with back-off
(1, 2, 5, 10, then every 15 min) until ``PUBLISH_RETRY_S`` (6 h), then S8.
A renewal that answers ``false`` stops the task at once, before any further
R2, HF or database write (TR-8 n9).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from app.services import artifact_validation as AV
from app.services import training_dispatch as D
from app.services import training_texts_de as T

logger = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="training-publish")
_PUBLISH_SEM = threading.BoundedSemaphore(2)
_MIRROR_SEM = threading.BoundedSemaphore(1)
_RUNNING: set = set()
_RUNNING_LOCK = threading.Lock()
_LOG_TRIES: dict = {}
MAX_LOG_TRIES = 3
PUBLISH_BACKOFF_S = (0, 60, 120, 300, 600)   # by publish_tries; then every 900 s
PUBLISH_BACKOFF_TAIL_S = 900
MODEL_META_NAME = "edubotics_model_meta.json"


class LeaseLost(Exception):
    """A lease renewal answered false: stop, write nothing more."""


class Transient(Exception):
    """An HF / R2 transport fault or a read-back mismatch: retry later."""


class Refused(Exception):
    """The model fails a content rule (S7)."""


def _submit(fn, *args) -> None:
    """The one place a task enters the executor (tests replace it)."""
    _EXECUTOR.submit(fn, *args)


def _start(key, sem, fn, *args) -> bool:
    with _RUNNING_LOCK:
        if key in _RUNNING:
            return False
        if not sem.acquire(blocking=False):
            return False
        _RUNNING.add(key)

    def _run():
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - logged, never propagated into the pool
            logger.error("%s for training %s failed: %s", key[0], key[1], type(exc).__name__)
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)
            sem.release()

    try:
        _submit(_run)
    except Exception:
        with _RUNNING_LOCK:
            _RUNNING.discard(key)
        sem.release()
        raise
    return True


def running(key) -> bool:
    with _RUNNING_LOCK:
        return key in _RUNNING


def reset_for_tests() -> None:
    with _RUNNING_LOCK:
        _RUNNING.clear()
    _LOG_TRIES.clear()


# ---------------------------------------------------------------------------
# The lease
# ---------------------------------------------------------------------------


class _Lease:
    """Claim once, renew every PUBLISH_RENEW_S on a thread; ``check()`` raises
    LeaseLost the moment a renewal answered false."""

    def __init__(self, claim):
        self._claim = claim
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def acquire(self) -> bool:
        return bool(self._claim())

    def start(self) -> None:
        def _loop():
            while not self._stop.wait(D.PUBLISH_RENEW_S):
                try:
                    if not self._claim():
                        self._lost.set()
                        return
                except Exception as exc:  # noqa: BLE001 - a transport blip is not a lost lease
                    logger.warning("lease renewal failed: %s", type(exc).__name__)

        self._thread = threading.Thread(target=_loop, name="training-lease", daemon=True)
        self._thread.start()

    def check(self) -> None:
        if self._lost.is_set():
            raise LeaseLost()

    def stop(self) -> None:
        self._stop.set()


def _publish_claim(tid: int, attempt_no: int):
    return lambda: D.rpc("claim_training_publish", {
        "p_training_id": int(tid), "p_attempt": int(attempt_no), "p_lease_s": D.PUBLISH_LEASE_S,
        "p_owner": D.BOOT_ID}) is True


def _mirror_claim(tid: int, attempt_no: int, step: int):
    return lambda: D.rpc("claim_checkpoint_mirror", {
        "p_training_id": int(tid), "p_attempt": int(attempt_no), "p_step": int(step),
        "p_lease_s": D.MIRROR_LEASE_S, "p_owner": D.BOOT_ID}) is True


# ---------------------------------------------------------------------------
# Hugging Face (huggingface_hub imported lazily; tests patch _hf())
# ---------------------------------------------------------------------------


def _hf():
    from huggingface_hub import CommitOperationAdd, HfApi

    return HfApi(token=D.SETTINGS.hf_token or None), CommitOperationAdd


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_model(tid: int, ckpt: dict, tmp: str, lease: _Lease | None) -> dict:
    """The 7 pretrained_model/ files of a checkpoint into tmp; size + sha256
    against the manifest. Returns {relpath: local path}. A mismatch → Refused
    (the bytes are not what ckpt-done checked); an R2 fault → Transient."""
    from app.services import r2_client

    manifest = ckpt.get("manifest") or {}
    out = {}
    for rel in AV.FINAL_RELPATHS:
        meta = manifest.get(rel)
        if not isinstance(meta, dict):
            raise Refused(f"manifest lacks {rel}")
        path = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if lease is not None:
            lease.check()
        try:
            size, sha = r2_client.stream_to_file(
                r2_client.ckpt_key(tid, ckpt["attempt_no"], ckpt["step"], rel), path,
                should_continue=(lambda: not lease._lost.is_set()) if lease is not None else None)
        except r2_client.R2Missing:
            raise Refused(f"object missing {rel}") from None
        except RuntimeError:
            if lease is not None:
                lease.check()
            raise Transient("r2 stream") from None
        except Exception as exc:  # noqa: BLE001 - transport
            raise Transient(f"r2 {type(exc).__name__}") from None
        if size != meta.get("size") or sha != meta.get("sha256"):
            raise Refused(f"size/sha256 mismatch {rel}")
        out[rel] = path
    return out


def _validate_model(paths: dict, row: dict) -> dict:
    docs = {}
    for rel in AV.PM_JSON_RELPATHS:
        with open(paths[rel], "rb") as fh:
            docs[rel] = AV.parse_json_bytes(fh.read(), rel)
    try:
        return AV.validate_model_jsons(
            docs, dataset_name=row["dataset_name"], training_params=D._params(row),
            output_dir=AV.output_dir_for(row["model_name"]))
    except AV.ArtifactRefused as exc:
        raise Refused(exc.reason) from None


def _dataset_fps(row) -> object:
    fps = row.get("dataset_fps")
    if isinstance(fps, (int, float)) and not isinstance(fps, bool) and fps > 0:
        return int(fps) if float(fps).is_integer() else fps
    # null → re-read meta/info.json at the training's revision (60 s).
    revision = D._params(row).get("dataset_revision") or "v3.0"
    result: dict = {}

    def _read():
        try:
            from huggingface_hub import hf_hub_download

            path = hf_hub_download(repo_id=row["dataset_name"], filename="meta/info.json", repo_type="dataset",
                                   revision=revision, token=False)
            with open(path) as fh:
                result["fps"] = json.load(fh).get("fps")
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(60)
    if t.is_alive() or "error" in result:
        raise Transient("dataset fps")
    return result.get("fps")


def model_meta_bytes(row) -> bytes:
    """edubotics_model_meta.json in the legacy worker's byte format."""
    meta = {"dataset_repo_id": row["dataset_name"], "dataset_fps": _dataset_fps(row), "policy_type": "act"}
    return json.dumps(meta, indent=2).encode("utf-8")


def _read_back(api, repo_id: str, expected: dict) -> None:
    """Every published path, by LFS sha256 + size or git blob sha1 (R4)."""
    try:
        infos = api.get_paths_info(repo_id, list(expected), expand=True, repo_type="model")
    except Exception as exc:  # noqa: BLE001
        raise Transient(f"read-back {type(exc).__name__}") from None
    seen = {getattr(i, "path", None): i for i in infos or []}
    for path, (sha256, size, sha1) in expected.items():
        info = seen.get(path)
        if info is None:
            raise Transient(f"read-back: {path} missing")
        lfs = getattr(info, "lfs", None)
        if lfs is not None:
            lfs_sha = getattr(lfs, "sha256", None) if not isinstance(lfs, dict) else lfs.get("sha256")
            lfs_size = getattr(lfs, "size", None) if not isinstance(lfs, dict) else lfs.get("size")
            if lfs_sha != sha256 or lfs_size != size:
                raise Transient(f"read-back: {path} lfs mismatch")
        elif getattr(info, "blob_id", None) != sha1:
            raise Transient(f"read-back: {path} blob mismatch")


def _bump_publish_tries(tid: int, attempt_no: int) -> None:
    att = D.read_attempt(tid, attempt_no)
    if not att:
        return
    (
        D._sb().table("training_attempts").update({"publish_tries": int(att.get("publish_tries") or 0) + 1})
        .eq("training_id", int(tid)).eq("attempt_no", int(attempt_no)).eq("state", "ended").execute()
    )


def log_url_for(model_name: str) -> str:
    return f"https://huggingface.co/{model_name}/blob/main/training_log.txt"


def publish_sync(tid: int, attempt_no: int) -> str:
    """One publish attempt (§4.6). Returns 'succeeded' | 'refused' | 'retry' | 'not_claimed' | 'lost' | 'gone'."""
    from app.services import r2_client

    lease = _Lease(_publish_claim(tid, attempt_no))
    if not lease.acquire():
        return "not_claimed"
    lease.start()
    tmp = tempfile.mkdtemp(prefix=f"publish-t{int(tid)}-")
    try:
        row = D.read_row(tid)
        attempt = D.read_attempt(tid, attempt_no)
        if (not row or row.get("status") != "running" or int(row.get("current_attempt") or 0) != int(attempt_no)
                or not attempt or attempt.get("state") != "ended" or attempt.get("end_reason") != "succeeded"):
            return "gone"
        total = D.total_steps_of(row)
        final = D.final_ready(D.read_checkpoints([tid], ["ready"]), total)
        if final is None:
            raise Transient("no ready final checkpoint")
        try:
            paths = _download_model(tid, final, tmp, lease)
            _validate_model(paths, row)
        except Refused as exc:
            lease.check()
            logger.error("training %s: final model refused (%s)", tid, exc)
            try:
                D.rpc("finish_training", {"p_training_id": int(tid), "p_attempt": int(attempt_no),
                                          "p_status": "failed", "p_message": T.S7, "p_log_url": None})
            except D.RpcConflict:
                return "gone"
            return "refused"
        meta = model_meta_bytes(row)
        meta_path = os.path.join(tmp, MODEL_META_NAME)
        with open(meta_path, "wb") as fh:
            fh.write(meta)
        log_path = None
        lease.check()
        try:
            data = r2_client.get_bytes(r2_client.log_key(tid, attempt_no), AV.LOG_SIZE_CAP)
            if len(data) <= AV.LOG_SIZE_CAP:
                log_path = os.path.join(tmp, AV.LOG_RELPATH)
                with open(log_path, "wb") as fh:
                    fh.write(data)
        except r2_client.R2Missing:
            log_path = None
        except Exception as exc:  # noqa: BLE001
            raise Transient(f"log {type(exc).__name__}") from None
        sid = AV.step_id(int(final["step"]), total)
        files = []  # (path_in_repo, local path)
        for rel, local in paths.items():
            name = rel.split("/", 1)[1]
            files.append((name, local))
            files.append((f"checkpoints/{sid}/pretrained_model/{name}", local))
        files.append((MODEL_META_NAME, meta_path))
        if log_path is not None:
            files.append((AV.LOG_RELPATH, log_path))
        expected = {}
        for repo_path, local in files:
            with open(local, "rb") as fh:
                data = fh.read()
            expected[repo_path] = (hashlib.sha256(data).hexdigest(), len(data), git_blob_sha1(data))
        api, CommitOperationAdd = _hf()
        repo = row["model_name"]
        lease.check()
        try:
            api.create_repo(repo_id=repo, repo_type="model", exist_ok=True)
            lease.check()
            api.create_commit(
                repo_id=repo, repo_type="model",
                operations=[CommitOperationAdd(path_in_repo=p, path_or_fileobj=l) for p, l in files],
                commit_message=f"EduBotics: trained model (training {int(tid)})",
            )
        except LeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001
            raise Transient(f"hf {type(exc).__name__}") from None
        lease.check()
        _read_back(api, repo, expected)
        lease.check()
        try:
            D.rpc("finish_training", {"p_training_id": int(tid), "p_attempt": int(attempt_no),
                                      "p_status": "succeeded", "p_message": None,
                                      "p_log_url": log_url_for(repo) if log_path is not None else None})
        except D.RpcConflict:
            logger.info("training %s: finish(succeeded) lost a race", tid)
            return "gone"
        logger.info("training %s published to %s", tid, repo)
        try:
            janitor_row(tid)
        except Exception as exc:  # noqa: BLE001 - the sweep's janitor finishes it
            logger.warning("janitor after publish of %s: %s", tid, type(exc).__name__)
        return "succeeded"
    except LeaseLost:
        logger.warning("training %s: publish lease lost; stopped", tid)
        return "lost"
    except Transient as exc:
        logger.warning("training %s: publish will be retried (%s)", tid, exc)
        try:
            _bump_publish_tries(tid, attempt_no)
        except Exception as e2:  # noqa: BLE001
            logger.warning("publish_tries write failed: %s", type(e2).__name__)
        return "retry"
    finally:
        lease.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def janitor_row(tid: int) -> None:
    """Delete every R2 object of a training whose row is terminal (§4.6 step 8)."""
    import time

    from app.services import r2_client

    now = time.time()
    for c in D.read_checkpoints([tid], ["uploading", "ready", "refused"]):
        if D.mirror_claim_live(c, now):
            continue
        D.delete_checkpoint_objects(c, c["state"])
    row = D.read_row(tid)
    if row and row.get("log_url"):
        n = int(row.get("current_attempt") or 0)
        r2_client.delete_keys([r2_client.log_key(tid, a) for a in range(1, n + 1)])


def schedule_publish(tid: int, attempt_no: int) -> bool:
    return _start(("publish", int(tid)), _PUBLISH_SEM, publish_sync, int(tid), int(attempt_no))


def publish_backoff_s(tries: int) -> int:
    return PUBLISH_BACKOFF_S[tries] if 0 <= tries < len(PUBLISH_BACKOFF_S) else PUBLISH_BACKOFF_TAIL_S


async def recover_publish(row, attempt, now: float) -> str:
    """The sweep's publish recovery (§4.3): give up after 6 h (S8), else retry with back-off."""
    tid = int(row["id"])
    n = int(attempt["attempt_no"])
    claimed = D.ts(attempt.get("publish_claimed_at"))
    ours = attempt.get("publish_owner") == D.BOOT_ID
    lease_live = claimed is not None and now - claimed < D.PUBLISH_LEASE_S
    first = D.ts(attempt.get("publish_first_at"))
    if first is not None and now - first > D.PUBLISH_RETRY_S:
        if running(("publish", tid)) or (lease_live and not ours):
            return "busy"
        await D.fail_training(row, n, T.S8)
        return "gave_up"
    if lease_live and not ours:
        return "other_instance"
    if claimed is not None and now - claimed < publish_backoff_s(int(attempt.get("publish_tries") or 0)):
        return "backoff"
    return "scheduled" if schedule_publish(tid, n) else "busy"


# ---------------------------------------------------------------------------
# Mirror (§4.5, FR-4)
# ---------------------------------------------------------------------------


def mirror_sync(tid: int, attempt_no: int, step: int) -> str:
    lease = _Lease(_mirror_claim(tid, attempt_no, step))
    if not lease.acquire():
        return "not_claimed"
    lease.start()
    tmp = tempfile.mkdtemp(prefix=f"mirror-t{int(tid)}-")
    try:
        row = D.read_row(tid)
        ckpts = [c for c in D.read_checkpoints([tid], ["ready"])
                 if c["attempt_no"] == int(attempt_no) and c["step"] == int(step)]
        if not row or not ckpts or ckpts[0].get("mirrored_at"):
            return "gone"
        ckpt = ckpts[0]
        try:
            paths = _download_model(tid, ckpt, tmp, lease)
            _validate_model(paths, row)
        except (Refused, Transient) as exc:
            raise Transient(str(exc)) from None
        sid = AV.step_id(int(step), D.total_steps_of(row))
        api, CommitOperationAdd = _hf()
        lease.check()
        try:
            api.create_repo(repo_id=row["model_name"], repo_type="model", exist_ok=True)
            lease.check()
            api.create_commit(
                repo_id=row["model_name"], repo_type="model",
                operations=[CommitOperationAdd(path_in_repo=f"checkpoints/{sid}/pretrained_model/{rel.split('/', 1)[1]}",
                                               path_or_fileobj=local) for rel, local in paths.items()],
                commit_message=f"EduBotics: checkpoint {sid} (training {int(tid)})",
            )
        except LeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001
            raise Transient(f"hf {type(exc).__name__}") from None
        lease.check()
        (
            D._sb().table("training_checkpoints").update({"mirrored_at": D._iso(D._now())})
            .eq("training_id", int(tid)).eq("attempt_no", int(attempt_no)).eq("step", int(step))
            .eq("state", "ready").is_("mirrored_at", "null").execute()
        )
        logger.info("training %s: checkpoint %s mirrored", tid, sid)
        return "mirrored"
    except LeaseLost:
        return "lost"
    except Transient as exc:
        logger.warning("training %s: mirror of step %s failed (%s)", tid, step, exc)
        try:
            (
                D._sb().table("training_checkpoints").update({"mirror_tries": int(ckpt.get("mirror_tries") or 0) + 1})
                .eq("training_id", int(tid)).eq("attempt_no", int(attempt_no)).eq("step", int(step))
                .eq("state", "ready").execute()
            )
        except Exception as e2:  # noqa: BLE001
            logger.warning("mirror_tries write failed: %s", type(e2).__name__)
        return "retry"
    finally:
        lease.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def schedule_mirror(tid: int, attempt_no: int, step: int) -> bool:
    return _start(("mirror", int(tid), int(attempt_no), int(step)), _MIRROR_SEM, mirror_sync,
                  int(tid), int(attempt_no), int(step))


def mirror_due(row, c, now: float) -> bool:
    total = D.total_steps_of(row)
    every = D.mirror_every_of(row)
    if c.get("state") != "ready" or c.get("mirrored_at") or int(c.get("mirror_tries") or 0) >= 3:
        return False
    if c["step"] % every != 0 or c["step"] >= total:
        return False
    if D.mirror_claim_live(c, now) and c.get("mirror_owner") != D.BOOT_ID:
        return False
    return True


async def schedule_due_mirrors(rows, attempts, now: float) -> int:
    ids = [r["id"] for r in rows]
    if not ids:
        return 0
    by_id = {r["id"]: r for r in rows}
    ckpts = await asyncio.to_thread(D.read_checkpoints, ids, ["ready"])
    n = 0
    for c in ckpts:
        row = by_id.get(c["training_id"])
        if row is not None and mirror_due(row, c, now) and schedule_mirror(c["training_id"], c["attempt_no"], c["step"]):
            n += 1
    return n


# ---------------------------------------------------------------------------
# The log of a failed training (§4.6 finish {failed}, TR-3)
# ---------------------------------------------------------------------------


def upload_failed_log_sync(row) -> str:
    from app.services import r2_client

    tid = int(row["id"])
    n = int(row.get("current_attempt") or 0)
    try:
        data = r2_client.get_bytes(r2_client.log_key(tid, n), AV.LOG_SIZE_CAP)
    except r2_client.R2Missing:
        return "no_log"
    if len(data) > AV.LOG_SIZE_CAP:
        return "too_big"
    api, CommitOperationAdd = _hf()
    repo = row["model_name"]
    api.create_repo(repo_id=repo, repo_type="model", exist_ok=True)
    api.create_commit(repo_id=repo, repo_type="model",
                      operations=[CommitOperationAdd(path_in_repo=AV.LOG_RELPATH, path_or_fileobj=data)],
                      commit_message=f"EduBotics: training log (training {tid})")
    (
        D._sb().table("trainings").update({"log_url": log_url_for(repo)})
        .eq("id", tid).eq("managed", True).in_("status", list(D.TERMINAL_STATUSES)).is_("log_url", "null")
        .execute()
    )
    return "uploaded"


def schedule_failed_log(row) -> bool:
    tid = int(row["id"])
    tries = _LOG_TRIES.get(tid, 0)
    if tries >= MAX_LOG_TRIES:
        return False
    started = _start(("log", tid), _PUBLISH_SEM, _failed_log_task, dict(row))
    if started:
        _LOG_TRIES[tid] = tries + 1
    return started


def _failed_log_task(row) -> None:
    result = upload_failed_log_sync(row)
    logger.info("training %s: failed-training log upload: %s", row["id"], result)
