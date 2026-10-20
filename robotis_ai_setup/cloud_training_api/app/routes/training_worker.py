"""The managed-training worker API (spec §3.2, §4.5, §4.6).

``POST /trainings/worker/{training_id}/{attempt}/{action}`` with
``Authorization: Bearer <worker_token>``: the only door a training host (a
RunPod pod or a Modal container) has into the API. Every frame is hostile: the
body is read by hand (≤ 384 KB, a JSON object), every field is checked by type
and range, and the answer to a failed authorisation never says which condition
failed (401 ``unauthorized``, S10).

Authorisation (every action): the header is ``Bearer <canonical uuid>``; the
row exists, is managed, ``status IN ('queued','running')``,
``current_attempt == attempt``; ``hmac.compare_digest`` of the token; the
attempt's ``state IN ('placing','starting','running')``. The rate limiter keys
these paths per worker token (``/trainings/`` is per-user keyed in main.py).

Error bodies are ``{"detail": <German, training_texts_de>, "code": <code>}``;
the worker reacts to the CODE (§3.2's reaction table). A host's ``detail``
text only ever reaches the server log, sanitized and cut to 500 characters
(E11); nothing here writes host text into the database.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services import artifact_validation as AV
from app.services import training_dispatch as D
from app.services import training_texts_de as T

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trainings/worker", tags=["training-worker"])

MAX_BODY_BYTES = 384 * 1024
_ID_RE = re.compile(r"[1-9][0-9]{0,9}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
ACTIONS = ("start", "upload-url", "ckpt-done", "ckpt-bad", "finish")
DETERMINISTIC_BAD = ("file_list", "steps", "training_step", "train_config")
CKPT_BAD_REASONS = DETERMINISTIC_BAD + ("sha256",)
FINISH_REASONS = {
    "succeeded": (None,),
    "failed": ("lerobot_error", "time_cap"),
    "host_unfit": ("no_cuda", "gpu_capability", "gpu_memory", "matmul", "disk", "download", "resume_crash",
                   "lerobot_crash"),
}
MIN_BUDGET_S = 300


def _resp(status: int, content: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=content, headers={"Cache-Control": "no-store"})


def _err(status: int, detail: str, code: str) -> JSONResponse:
    return _resp(status, {"detail": detail, "code": code})


def _unauthorized() -> JSONResponse:
    return _err(401, T.S10, "unauthorized")


def _unavailable() -> JSONResponse:
    return _err(503, T.WORKER_UNAVAILABLE_DE, "unavailable")


def _bad_request() -> JSONResponse:
    return _err(422, T.WORKER_BAD_REQUEST_DE, "bad_request")


def _expected_lock_sha256() -> str:
    """The generated lock hash (WP-B's training_lock_hash.py), imported lazily
    so ``from app import main`` works without it (TR-11 n13)."""
    from app.services import training_lock_hash

    return training_lock_hash.TRAINING_LOCK_SHA256


def _bearer(request) -> str | None:
    headers = getattr(request, "headers", None) or {}
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    if not raw.startswith("Bearer "):
        return None
    token = raw[len("Bearer "):]
    return token if _UUID_RE.fullmatch(token) else None


def _row_with_token(training_id: int):
    rows = (
        D._sb().table("trainings").select(D.TRAINING_COLUMNS + ", worker_token").eq("id", int(training_id))
        .execute()
    ).data or []
    return rows[0] if rows else None


async def _read_body(request):
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001 - a client that hung up
        return None
    if raw is None or len(raw) > MAX_BODY_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


@router.post("/{training_id}/{attempt}/{action}")
async def worker_action(training_id: str, attempt: str, action: str, request: Request):
    if not (_ID_RE.fullmatch(str(training_id)) and _ID_RE.fullmatch(str(attempt))):
        return _unauthorized()
    token = _bearer(request)
    if token is None:
        return _unauthorized()
    tid, att_no = int(training_id), int(attempt)
    try:
        row, att = await asyncio.to_thread(_authorize, tid, att_no, token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker auth read failed: %s", type(exc).__name__)
        return _unavailable()
    if row is None:
        return _unauthorized()
    if action not in ACTIONS:
        return _err(404, T.WORKER_UNKNOWN_ACTION_DE, "unknown_action")
    body = await _read_body(request)
    if body is None:
        return _bad_request()
    handler = {
        "start": _start, "upload-url": _upload_url, "ckpt-done": _ckpt_done, "ckpt-bad": _ckpt_bad,
        "finish": _finish,
    }[action]
    try:
        return await handler(row, att, body)
    except Exception as exc:  # noqa: BLE001 - a database or R2 fault: the worker retries a 5xx
        logger.warning("worker %s for training %s attempt %s failed: %s", action, tid, att_no, type(exc).__name__)
        return _unavailable()


def _authorize(training_id: int, attempt_no: int, token: str):
    row = _row_with_token(training_id)
    if row is None or row.get("managed") is not True or row.get("status") not in ("queued", "running"):
        return None, None
    if int(row.get("current_attempt") or 0) != attempt_no:
        return None, None
    if not hmac.compare_digest(str(row.get("worker_token") or ""), token):
        return None, None
    attempt = D.read_attempt(training_id, attempt_no)
    if attempt is None or attempt.get("state") not in D.ACTIVE_STATES:
        return None, None
    row = {k: v for k, v in row.items() if k != "worker_token"}
    return row, attempt


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _spec(row) -> dict:
    params = D._params(row)
    return {
        "dataset_name": row["dataset_name"],
        "model_name": row["model_name"],
        "model_type": "act",
        "training_params": params,
        "total_steps": D.total_steps_of(row),
        "save_freq": D.save_freq_of(row),
        "mirror_every": D.mirror_every_of(row),
        "dataset_revision": params.get("dataset_revision"),
        "output_dir": AV.output_dir_for(row["model_name"]),
    }


def _resume_point(ckpts, total: int):
    return D.newest_ready_checkpoint(ckpts, total)


def _resume_files(tid: int, ckpt) -> dict:
    from app.services import r2_client

    manifest = ckpt.get("manifest") or {}
    out = {}
    for rel in AV.CHECKPOINT_RELPATHS:
        meta = manifest.get(rel) or {}
        out[rel] = {
            "url": r2_client.presign_get(r2_client.ckpt_key(tid, ckpt["attempt_no"], ckpt["step"], rel)),
            "size": meta.get("size"),
            "sha256": meta.get("sha256"),
        }
    return out


def _kept_files(parent_id: int, kept) -> dict:
    """_resume_files' shape, but presigned GETs of the kept copy's keep/ keys (owner E36)."""
    from app.services import r2_client

    manifest = kept.get("manifest") or {}
    step = int(kept["step"])
    out = {}
    for rel in AV.CHECKPOINT_RELPATHS:
        meta = manifest.get(rel) or {}
        out[rel] = {
            "url": r2_client.presign_get(r2_client.keep_key(int(parent_id), step, rel)),
            "size": meta.get("size"),
            "sha256": meta.get("sha256"),
        }
    return out


def _kept_usable(parent_id: int, kept) -> bool:
    """A kept copy is usable iff it exists, is `kept`, and all 11 keep/ objects are present
    and sized as its manifest says. An R2 transport fault propagates (→ 503, the worker
    retries `start`). The clock is NOT judged here (presence decides, review m1)."""
    from app.services import r2_client

    if not kept or kept.get("state") != "kept":
        return False
    manifest = kept.get("manifest") if isinstance(kept.get("manifest"), dict) else {}
    step = int(kept["step"])
    for rel in AV.CHECKPOINT_RELPATHS:
        meta = manifest.get(rel) or {}
        try:
            if int(r2_client.head_size(r2_client.keep_key(int(parent_id), step, rel))) != int(meta.get("size")):
                return False
        except r2_client.R2Missing:
            return False
    return True


def _expected_handler_sha256() -> str:
    """The generated hash of training_handler.py (make_handler_hash.py, V2-1), imported
    lazily like the lock hash."""
    from app.services import training_handler_hash

    return training_handler_hash.TRAINING_HANDLER_SHA256


async def _start(row, att, body):
    lock = body.get("lock_sha256")
    build = body.get("build")
    # V2-1: the sha256 of the training_handler.py the worker runs. Absent = an image from
    # before the handler hash, i.e. a stale handler: refused as wrong_image, never 422.
    handler = body.get("handler_sha256")
    if type(lock) is not str or not _HEX64.fullmatch(lock) or type(build) is not str or len(build) > 64:
        return _bad_request()
    if handler is not None and (type(handler) is not str or not _HEX64.fullmatch(handler)):
        return _bad_request()
    tid, n = int(row["id"]), int(att["attempt_no"])
    now = D._now()
    logger.info("worker start: training %s attempt %s build %s", tid, n, D.sanitize_log_text(build, 64))
    try:
        expected = _expected_lock_sha256()
        expected_handler = _expected_handler_sha256()
    except Exception as exc:  # noqa: BLE001 - never admit a lock or handler we cannot compare
        logger.error("training lock or handler hash unavailable: %s", type(exc).__name__)
        return _unavailable()
    stale = []
    if not hmac.compare_digest(lock, expected):
        stale.append(f"lock {lock[:12]}…, expected {expected[:12]}…")
    if handler is None or not hmac.compare_digest(handler, expected_handler):
        stale.append(f"handler {(handler or 'none')[:12]}…, expected {expected_handler[:12]}…")
    if stale:
        logger.error("training %s attempt %s: wrong image (%s); requeued", tid, n, "; ".join(stale))
        await D.requeue_attempt(row, att, "host_unfit:wrong_image", now)
        await D.terminate_attempt(att)
        return _err(409, T.S10, "wrong_image")
    total = D.total_steps_of(row)
    ckpts = await asyncio.to_thread(D.read_checkpoints, [tid], ["ready"])  # A3: before the budget check
    finished = D.final_ready(ckpts, total) is not None
    budget_s, bound = D.budget_for(row, att, now)
    if budget_s < MIN_BUDGET_S:
        result = await D.cap_attempt(row, att, now, ckpts, bound=bound)
        await D.terminate_attempt(att)
        detail = T.S22 if result == "published" else (T.S6 if bound == "wall" else T.s5(D.SETTINGS.cap_s))
        return _err(409, detail, "budget_exhausted")
    D.record_budget_bound(tid, n, bound)
    resume = None if finished else _resume_point(ckpts, total)
    # D: a continuation whose own attempts have no ready checkpoint yet resumes from
    # the parent's kept copy (presence decides, not the clock — review m1).
    if resume is None and not finished and row.get("continued_from"):
        parent_id = int(row["continued_from"])
        kept = await asyncio.to_thread(D.read_kept, parent_id)
        if not await asyncio.to_thread(_kept_usable, parent_id, kept):
            await D.fail_training(row, n, T.S27)
            await D.terminate_attempt(att)
            return _err(409, T.S27, "kept_unusable")
        await asyncio.to_thread(D.write_attempt_meta, tid, n,
                                {"resume_from_step": int(kept["step"]), "resume_from_attempt": None},
                                D.ACTIVE_STATES)
        files = await asyncio.to_thread(_kept_files, parent_id, kept)
        out_resume = {"step": int(kept["step"]), "step_id": AV.step_id(int(kept["step"]), total),
                      "attempt_no": 0, "files": files}
        return _resp(200, {"spec": _spec(row), "budget_s": int(budget_s), "finished": False, "resume": out_resume})
    meta = {"resume_from_step": int(resume["step"]) if resume else 0,
            "resume_from_attempt": int(resume["attempt_no"]) if resume else None}
    await asyncio.to_thread(D.write_attempt_meta, tid, n, meta, D.ACTIVE_STATES)
    out_resume = None
    if resume is not None:
        files = await asyncio.to_thread(_resume_files, tid, resume)
        out_resume = {"step": int(resume["step"]), "step_id": AV.step_id(int(resume["step"]), total),
                      "attempt_no": int(resume["attempt_no"]), "files": files}
    return _resp(200, {"spec": _spec(row), "budget_s": int(budget_s), "finished": finished, "resume": out_resume})


# ---------------------------------------------------------------------------
# upload-url
# ---------------------------------------------------------------------------


def _read_ckpt(tid: int, attempt_no: int, step: int):
    rows = (
        D._sb().table("training_checkpoints").select(D.CHECKPOINT_COLUMNS)
        .eq("training_id", int(tid)).eq("attempt_no", int(attempt_no)).eq("step", int(step)).execute()
    ).data or []
    return rows[0] if rows else None


def _insert_ckpt(tid: int, attempt_no: int, step: int, manifest: dict):
    D._sb().table("training_checkpoints").insert({
        "training_id": int(tid), "attempt_no": int(attempt_no), "step": int(step), "state": "uploading",
        "manifest": manifest,
    }).execute()


def _presign_all(keys_sizes) -> tuple[dict, dict]:
    from app.services import r2_client

    urls, headers = {}, {}
    for rel, key, meta in keys_sizes:
        url, hdr = r2_client.presign_put(key, meta["size"], meta["md5"])
        urls[rel] = url
        headers[rel] = hdr
    return urls, headers


async def _upload_url(row, att, body):
    from app.services import r2_client

    tid, n = int(row["id"]), int(att["attempt_no"])
    kind = body.get("kind")
    if kind == "log":
        try:
            manifest = AV.validate_log_manifest(body.get("files"))
        except AV.ManifestError:
            return _err(422, T.S11, "bad_manifest")
        meta = manifest[AV.LOG_RELPATH]
        urls, headers = await asyncio.to_thread(_presign_all, [(AV.LOG_RELPATH, r2_client.log_key(tid, n), meta)])
        return _resp(200, {"urls": urls, "expires_in": r2_client.PUT_TTL_S, "headers": headers})
    if kind != "ckpt":
        return _bad_request()
    step = body.get("step")
    try:
        manifest = AV.validate_ckpt_manifest(
            step, body.get("files"), total_steps=D.total_steps_of(row), save_freq=D.save_freq_of(row),
            resume_step=int(att.get("resume_from_step") or 0))
    except AV.ManifestError:
        return _err(422, T.S11, "bad_manifest")
    for _ in range(2):
        existing = await asyncio.to_thread(_read_ckpt, tid, n, step)
        if existing is None:
            try:
                await asyncio.to_thread(_insert_ckpt, tid, n, step, manifest)
            except Exception as exc:
                if D.pg_code(exc) == "23505":
                    continue  # a concurrent insert: judge the row it wrote
                raise
            break
        state = existing.get("state")
        if state in ("ready", "deleted"):
            return _err(409, T.S14, "already_ready")
        if state == "refused":
            return _err(409, T.S12, "already_refused")
        if existing.get("manifest") != manifest:
            return _err(422, T.S11, "bad_manifest")
        break
    else:
        return _unavailable()
    entries = [(rel, r2_client.ckpt_key(tid, n, step, rel), meta) for rel, meta in manifest.items()]
    urls, headers = await asyncio.to_thread(_presign_all, entries)
    return _resp(200, {"urls": urls, "expires_in": r2_client.PUT_TTL_S, "headers": headers})


# ---------------------------------------------------------------------------
# ckpt-done
# ---------------------------------------------------------------------------


def _verify_checkpoint(row, ckpt) -> tuple:
    """('ready', None, None) | ('refused', reason, content). R2 transport faults raise."""
    from app.services import r2_client

    tid, n, step = int(row["id"]), int(ckpt["attempt_no"]), int(ckpt["step"])
    manifest = ckpt.get("manifest") or {}
    total = D.total_steps_of(row)
    for rel, meta in manifest.items():
        try:
            size = r2_client.head_size(r2_client.ckpt_key(tid, n, step, rel))
        except r2_client.R2Missing:
            return "refused", f"missing {rel}", False
        if size != meta.get("size"):
            return "refused", f"size {rel}", False
    rels = list(AV.PM_JSON_RELPATHS) + (list(AV.TS_JSON_RELPATHS) if step < total else [])
    docs = {}
    for rel in rels:
        meta = manifest.get(rel) or {}
        try:
            data = r2_client.get_bytes(r2_client.ckpt_key(tid, n, step, rel), AV.SIZE_CAPS[rel])
        except r2_client.R2Missing:
            return "refused", f"missing {rel}", False
        if hashlib.sha256(data).hexdigest() != meta.get("sha256") or len(data) != meta.get("size"):
            return "refused", f"sha256 {rel}", False
        try:
            docs[rel] = AV.parse_json_bytes(data, rel)
        except AV.ArtifactRefused as exc:
            return "refused", exc.reason, True
    try:
        AV.validate_checkpoint_jsons(
            docs, step=step, total_steps=total, dataset_name=row["dataset_name"],
            training_params=D._params(row), output_dir=AV.output_dir_for(row["model_name"]))
    except AV.ArtifactRefused as exc:
        return "refused", exc.reason, exc.content
    return "ready", None, None


def _set_ckpt_state(tid, attempt_no, step, payload, expect):
    return (
        D._sb().table("training_checkpoints").update(payload)
        .eq("training_id", int(tid)).eq("attempt_no", int(attempt_no)).eq("step", int(step))
        .in_("state", list(expect)).execute()
    ).data


async def _ckpt_done(row, att, body):
    from app.services import hf_publish

    step = body.get("step")
    if type(step) is not int or step < 1:
        return _bad_request()
    tid, n = int(row["id"]), int(att["attempt_no"])
    ckpt = await asyncio.to_thread(_read_ckpt, tid, n, step)
    if ckpt is None or ckpt.get("state") == "deleted":
        return _err(409, T.S15, "not_uploading")
    if ckpt.get("state") == "ready":
        return _resp(200, {"state": "ready"})
    if ckpt.get("state") == "refused":
        return _err(422, T.S12, "ckpt_refused")
    verdict, reason, content = await asyncio.to_thread(_verify_checkpoint, row, ckpt)
    now = D._now()
    if verdict == "ready":
        await asyncio.to_thread(_set_ckpt_state, tid, n, step, {"state": "ready", "ready_at": D._iso(now)},
                                ("uploading",))
        try:
            await asyncio.to_thread(D.apply_retention, tid, now)
        except Exception as exc:  # noqa: BLE001 - the janitor catches up
            logger.warning("retention for training %s failed: %s", tid, type(exc).__name__)
        fresh = dict(ckpt, state="ready", ready_at=D._iso(now))
        if hf_publish.mirror_due(row, fresh, now):
            hf_publish.schedule_mirror(tid, n, step)
        logger.info("training %s attempt %s: checkpoint %s ready", tid, n, step)
        return _resp(200, {"state": "ready"})
    await asyncio.to_thread(_set_ckpt_state, tid, n, step,
                            {"state": "refused", "refused_reason": str(reason)[:200], "refused_by": n},
                            ("uploading",))
    logger.warning("training %s attempt %s: checkpoint %s refused (%s%s)", tid, n, step, reason,
                   ", content rule: training failed" if content else "")
    if content:
        # FR-5: a content rule refuses every checkpoint of this training the same way.
        await D.fail_training(row, n, T.S7)
        await D.terminate_attempt(att)
    return _err(422, T.S12, "ckpt_refused")


# ---------------------------------------------------------------------------
# ckpt-bad
# ---------------------------------------------------------------------------


# ckpt-bad sha256 streams the whole handed checkpoint (~620 MB for ACT) through
# the API (V1-6), so a hostile host could make it pull hundreds of GB a minute.
# Therefore: one verdict per asking attempt and checkpoint (training, asking
# attempt, checkpoint attempt, step), remembered for the process's life, so a
# repeat answers without hashing; one hash at a time per key (concurrent
# askers wait for the same one); and the hashing runs in its own 1-thread
# pool, never in the default executor that every asyncio.to_thread Supabase
# call shares.
# The asking attempt is in the key (fix round 2026-10-19): a checkpoint's PUT
# links stay usable for r2_client.PUT_TTL_S after they were minted (an upload
# begun before then may end later), and a same-length body with the same MD5
# (both are signed) replaces an object with its size and ETag unchanged, so no
# verdict may be reused for another attempt: each hashes the checkpoint as it
# then is. Reusing it for the attempt that asked equals that attempt not asking
# again (`kept` changes nothing; its worker then gives the GPU up). The cost
# stays one hash per asking attempt, at most MAX_ATTEMPTS per checkpoint.
_HASH_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ckpt-hash")
HASH_VERDICTS_MAX = 1024
_HASH_VERDICTS: OrderedDict = OrderedDict()   # (training, asker, attempt, step) -> intact (bool)
_HASH_INFLIGHT: dict = {}                     # (training, asker, attempt, step) -> asyncio.Future


def reset_hash_state_for_tests() -> None:
    _HASH_VERDICTS.clear()
    _HASH_INFLIGHT.clear()


async def _checkpoint_intact(tid: int, asker: int, ckpt, key_of=None) -> bool:
    """The remembered or single-flight verdict of _hash_checkpoint (V1-6) for
    the asking attempt `asker`. A transport fault is never remembered: it
    propagates (503) and the next ask hashes again. ``key_of`` hashes a kept copy
    (keep/ keys) instead of the attempt's own ckpt_key (owner E36)."""
    key = (int(tid), int(asker), int(ckpt["attempt_no"]), int(ckpt["step"]))
    if key in _HASH_VERDICTS:
        _HASH_VERDICTS.move_to_end(key)
        return _HASH_VERDICTS[key]
    fut = _HASH_INFLIGHT.get(key)
    if fut is None:
        fut = asyncio.get_running_loop().run_in_executor(_HASH_POOL, _hash_checkpoint, tid, ckpt, key_of)
        _HASH_INFLIGHT[key] = fut

        def _settled(f, key=key):
            if _HASH_INFLIGHT.get(key) is f:
                del _HASH_INFLIGHT[key]
            if f.cancelled() or f.exception() is not None:
                return
            _HASH_VERDICTS[key] = bool(f.result())
            while len(_HASH_VERDICTS) > HASH_VERDICTS_MAX:
                _HASH_VERDICTS.popitem(last=False)

        fut.add_done_callback(_settled)
    # shield: an asker that hangs up never cancels the hash the others wait for.
    return bool(await asyncio.shield(fut))


def _hash_checkpoint(tid: int, ckpt, key_of=None) -> bool:
    """True when every object of the checkpoint matches its manifest (size + sha256).
    ``key_of(rel)`` picks the R2 key (default the attempt's ckpt_key; a kept copy
    passes keep_key)."""
    from app.services import r2_client

    if key_of is None:
        def key_of(rel):
            return r2_client.ckpt_key(tid, ckpt["attempt_no"], ckpt["step"], rel)
    for rel, meta in (ckpt.get("manifest") or {}).items():
        try:
            size, sha = r2_client.stream_sha256(key_of(rel))
        except r2_client.R2Missing:
            return False
        if size != meta.get("size") or sha != meta.get("sha256"):
            return False
    return True


def _set_kept_state(parent_id: int, payload: dict, expect: str):
    return (
        D._sb().table("training_kept_checkpoints").update(payload)
        .eq("training_id", int(parent_id)).eq("state", expect).execute()
    ).data


def _count_kept_crash(tid: int, n: int, parent_id: int, step: int) -> bool:
    """Owner E43/review M3: add this child attempt to the kept row's bad_attempts
    (read-modify-write, filtered on state='kept'); True once RESUME_CRASH_LIMIT distinct
    attempts across all children have complained with a crash / train_config."""
    kept = D.read_kept(parent_id)
    if not kept or kept.get("state") != "kept" or int(kept.get("step") or 0) != int(step):
        return False
    bad = set(str(x) for x in (kept.get("bad_attempts") or [])) | {f"{int(tid)}:{int(n)}"}
    _set_kept_state(parent_id, {"bad_attempts": sorted(bad)}, "kept")
    return len(bad) >= D.RESUME_CRASH_LIMIT


async def _refuse_kept(row, att, parent_id: int, reason: str) -> None:
    """A proven-unusable kept copy (owner E43, review M3): mark it refused, expire the
    parent, fail the child with a refund, stop the pod. Order load-bearing (§6.4 F)."""
    tid, n = int(row["id"]), int(att["attempt_no"])

    def _mark():
        _set_kept_state(parent_id, {"state": "refused", "refused_reason": reason}, "kept")
        (
            D._sb().table("trainings").update({"continuable_until": D._iso(D._now())})
            .eq("id", int(parent_id)).eq("status", "succeeded").execute()
        )

    await asyncio.to_thread(_mark)
    await D.fail_training(row, n, T.S27)
    await D.terminate_attempt(att)


def _is_kept_resume(row, att) -> bool:
    return bool(att.get("resume_from_attempt") is None and int(att.get("resume_from_step") or 0) >= 1
                and row.get("continued_from"))


async def _ckpt_bad(row, att, body):
    step = body.get("step")
    reason = body.get("reason")
    if type(step) is not int or reason not in CKPT_BAD_REASONS:
        return _bad_request()
    tid, n = int(row["id"]), int(att["attempt_no"])
    # A kept-copy resume (owner E36/E43): a complaint about the handed copy is re-hashed
    # for content reasons, counted for train_config, never trusted on one host's word.
    if _is_kept_resume(row, att) and att.get("resume_from_step") == step:
        from app.services import r2_client

        parent = int(row["continued_from"])
        kept = await asyncio.to_thread(D.read_kept, parent)
        if not kept or kept.get("state") != "kept" or int(kept.get("step") or 0) != step:
            return _resp(200, {"state": "ignored"})
        if reason in ("sha256", "file_list", "steps", "training_step"):
            probe = {"attempt_no": 0, "step": step, "manifest": kept.get("manifest") or {}}
            intact = await _checkpoint_intact(tid, n, probe,
                                              key_of=lambda rel: r2_client.keep_key(parent, step, rel))
            if intact:
                logger.info("training %s attempt %s: kept copy %s intact despite ckpt-bad %s", tid, n, step, reason)
                return _resp(200, {"state": "kept"})
        elif not await asyncio.to_thread(_count_kept_crash, tid, n, parent, step):
            return _resp(200, {"state": "kept"})
        await _refuse_kept(row, att, parent, reason)
        logger.warning("training %s attempt %s: kept copy %s refused (%s)", tid, n, step, reason)
        return _resp(200, {"state": "refused"})
    src_attempt = att.get("resume_from_attempt")
    if att.get("resume_from_step") != step or src_attempt is None or step < 1:
        return _resp(200, {"state": "ignored"})
    ckpt = await asyncio.to_thread(_read_ckpt, tid, int(src_attempt), step)
    if ckpt is None:
        return _resp(200, {"state": "ignored"})
    if reason == "sha256":
        intact = await _checkpoint_intact(tid, n, ckpt)
        if intact:
            logger.info("training %s attempt %s: ckpt-bad sha256 not confirmed for step %s", tid, n, step)
            return _resp(200, {"state": "kept"})
    if ckpt.get("state") in ("ready", "uploading"):
        await asyncio.to_thread(_set_ckpt_state, tid, int(src_attempt), step,
                                {"state": "refused", "refused_reason": reason, "refused_by": n},
                                ("ready", "uploading"))
    logger.warning("training %s attempt %s: checkpoint %s (attempt %s) refused by ckpt-bad %s",
                   tid, n, step, src_attempt, reason)
    return _resp(200, {"state": "refused"})


# ---------------------------------------------------------------------------
# finish
# ---------------------------------------------------------------------------


def _count_resume_crash(tid: int, att) -> None:
    """crash_attempts gains this attempt; at RESUME_CRASH_LIMIT distinct attempts
    the handed checkpoint is refused (§4.5, SR-2)."""
    src, step = att.get("resume_from_attempt"), att.get("resume_from_step")
    if src is None or not step:
        return
    ckpt = _read_ckpt(tid, int(src), int(step))
    if ckpt is None or ckpt.get("state") != "ready":
        return
    crashes = sorted(set(int(x) for x in (ckpt.get("crash_attempts") or [])) | {int(att["attempt_no"])})
    payload = {"crash_attempts": crashes}
    if len(crashes) >= D.RESUME_CRASH_LIMIT:
        payload.update({"state": "refused", "refused_reason": "resume_crash", "refused_by": int(att["attempt_no"])})
    _set_ckpt_state(tid, int(src), int(step), payload, ("ready",))


async def _finish(row, att, body):
    from app.services import hf_publish

    outcome = body.get("outcome")
    reason = body.get("reason")
    detail = body.get("detail")
    if outcome not in FINISH_REASONS or reason not in FINISH_REASONS[outcome]:
        return _bad_request()
    if detail is not None and (type(detail) is not str or len(detail) > 500):
        return _bad_request()
    tid, n = int(row["id"]), int(att["attempt_no"])
    now = D._now()
    logger.info("worker finish: training %s attempt %s %s/%s %s", tid, n, outcome, reason,
                D.sanitize_log_text(detail) if detail else "")
    runpod = att.get("backend") in D.RUNPOD_RUNGS
    if outcome == "succeeded":
        ckpts = await asyncio.to_thread(D.read_checkpoints, [tid], ["ready"])
        if D.final_ready(ckpts, D.total_steps_of(row)) is None:
            return _err(409, T.S13, "no_final_checkpoint")
        if not await D.end_attempt_succeeded(row, att, now):
            hf_publish.schedule_publish(tid, n)  # someone else ended it first; the lease decides
        if runpod:
            await D.terminate_attempt(att)
        return _resp(200, {"status": "publishing"})
    if outcome == "failed":
        if reason == "lerobot_error":
            await D.fail_training(row, n, T.S19)
            if runpod:
                await D.terminate_attempt(att)
            hf_publish.schedule_failed_log(dict(row, current_attempt=n))
            return _resp(200, {"status": "failed"})
        # time_cap: publish the newest ready checkpoint (training time or the 12 h wall,
        # owner E33/E44), else S5 / S6 + the failed log.
        ckpts = await asyncio.to_thread(D.read_checkpoints, [tid], ["ready"])
        bound = D.time_cap_bound(row, att, now)
        result = await D.cap_attempt(row, att, now, ckpts, bound=bound)
        if runpod:
            await D.terminate_attempt(att)
        if result == "failed":
            hf_publish.schedule_failed_log(dict(row, current_attempt=n))
            return _resp(200, {"status": "failed"})
        return _resp(200, {"status": "publishing"}) if result == "published" else _resp(200, {"status": "failed"})
    # host_unfit
    if reason == "resume_crash":
        if _is_kept_resume(row, att):
            step = int(att.get("resume_from_step") or 0)
            parent = int(row["continued_from"])
            if await asyncio.to_thread(_count_kept_crash, tid, n, parent, step):
                await _refuse_kept(row, att, parent, "resume_crash")
                return _resp(200, {"status": "failed"})
            # below the limit: requeue as today (fall through)
        else:
            await asyncio.to_thread(_count_resume_crash, tid, att)
    await D.requeue_attempt(row, att, f"host_unfit:{reason}", now, skip_community_dc=True)
    if runpod:
        await D.terminate_attempt(att)
    return _resp(200, {"status": "requeued"})
