"""Cloud-Inferenz control plane, the pure half (migration 048; spec §4.3).

No FastAPI and no Modal import: the route module (`routes/inference.py`) calls
these from worker threads, and `inference_edge.py` is the only Modal caller.

What lives here:
  * the settings (env caps, clamped) — `INFERENCE_SESSION_MAX_S` is held at
    ≤ 3300 s until a 35-minute run on one Modal session is measured (R-18);
  * the CONNECT token (shown once to its owner, stored only as sha256 hex);
  * the EDGE-token envelope (D32): the Modal session's edge token is stored
    only as `e1.<parent kid>.<b64url(nonce + AES-256-GCM)>` under an HKDF-SHA256
    sub-key of EDUBOTICS_HF_TOKEN_KEY (`hf_credentials.subkeys`), AAD = the
    session id, and erased once the session is terminated. `cryptography` is
    imported INSIDE the two functions (G-5) so `from app.routes import
    inference` works where it is not installed;
  * the training visibility ladder (exactly `routes/training.py::get_training`'s),
    the model resolver against the Hugging Face Hub, and thin wrappers around
    the 048 service-role RPCs with their error mapping;
  * `log_ended_sessions`, the one writer of `inference_runs` rows with
    backend='cloud' (D14, R-19).

Neither token, its hash, the envelope nor a credential object is ever logged
or put in an exception text: logs name ids, an HTTP status or an exception
TYPE only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from app.services import hf_credentials

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, lo: int, hi: int, environ=None) -> int:
    """An integer setting, clamped to [lo, hi]; absent or unparsable -> default."""
    env = os.environ if environ is None else environ
    raw = env.get(name)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _settings(environ=None) -> dict:
    reserve_s = _env_int("INFERENCE_RESERVE_S", 300, 30, 900, environ)
    return {
        "SESSION_MAX_S": _env_int("INFERENCE_SESSION_MAX_S", 1800, 60, 3300, environ),  # R-18, §1.1 (13)
        "DAILY_CAP_S": _env_int("INFERENCE_DAILY_CAP_S", 7200, 60, 86400, environ),
        "GLOBAL_CAP": _env_int("INFERENCE_GLOBAL_SESSION_CAP", 60, 1, 10000, environ),  # owner A-Q1
        "RESERVE_S": reserve_s,  # a reservation the robot never uses
        # D34: Modal's idle timeout always outlives the reservation window.
        "EDGE_IDLE_S": max(_env_int("INFERENCE_EDGE_IDLE_S", 600, 120, 7200, environ), reserve_s + 120),
    }


_S = _settings()
SESSION_MAX_S = _S["SESSION_MAX_S"]
DAILY_CAP_S = _S["DAILY_CAP_S"]
GLOBAL_CAP = _S["GLOBAL_CAP"]
RESERVE_S = _S["RESERVE_S"]
EDGE_IDLE_S = _S["EDGE_IDLE_S"]

# fullmatch only, NEVER match + `$` (Python's `$` also matches before a trailing newline).
MODEL_REPO_RE = re.compile(r"EduBotics-Solutions/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")   # == SQL CHECK
SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MODAL_SESSION_ID_RE = re.compile(r"sd-[A-Za-z0-9_-]{1,128}")                 # == cloud_protocol, == SQL CHECK
EDGE_TOKEN_RE = re.compile(r"[\x21-\x7e]{16,4096}")                       # == cloud_protocol.EDGE_TOKEN_RE
CONNECT_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
CAMERA_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")                        # == cloud_protocol camera names

EDGE_INFO = b"edubotics-inference-edge:v1"
EDGE_ENVELOPE = "e1"
NONCE_BYTES = 12
HF_TIMEOUT_S = 10.0
MODEL_CACHE_TTL_S = 60.0
CONFIG_MAX_BYTES = 65_536
HF_BASE_URL = "https://huggingface.co"
MAX_CAMERAS = 4
MAX_DIM = 32
MAX_IMAGE_SIDE = 8192
INFERENCE_RUNS_KEEP = 50  # == routes/jetson.py::_INFERENCE_RUNS_KEEP

# spec §4.3 — keys == the SQL end_reason CHECK (test_end_reason_maps_cover_the_sql_check)
END_REASON_DE = {
    "user_stop": "",
    "robot_stop": "",
    "superseded": "Durch einen neuen Cloud-Lauf ersetzt.",
    "client_gone": "Die Inferenz-Seite war nicht mehr geöffnet.",
    "never_connected": "Die Cloud-GPU war reserviert, aber der Roboter hat sich nicht verbunden.",
    "expired": "Das Zeitlimit für diesen Lauf war erreicht.",
    "robot_error": "Der Roboter hat den Lauf mit einem Fehler beendet.",
    "stall": "Die Verbindung zur Cloud war zu lange unterbrochen.",
    "server_error": "In der Cloud ist ein Fehler aufgetreten.",
    "server_gone": "Die Cloud-GPU hat nicht mehr geantwortet.",
    "edge_failed": "Die Cloud-GPU konnte nicht gestartet werden.",
}
EXIT_REASON = {
    "user_stop": "stopped",
    "robot_stop": "stopped",
    "superseded": "stopped",
    "client_gone": "stopped",
    "never_connected": "stopped",
    "expired": "finished",
    "robot_error": "error",
    "stall": "error",
    "server_error": "error",
    "server_gone": "error",
    "edge_failed": "error",
}


class DailyCapReached(Exception):
    """P0048: less than a minute of Cloud-Zeit left today."""


class CloudBusy(Exception):
    """P0049: the global session cap (reservations included) is reached."""


class NotFound(Exception):
    """P0002: not the caller's session (or no such user)."""


class SessionStoreError(Exception):
    """Any other database failure; never carries the database's text."""


class ModelResolveError(Exception):
    """code ∈ {'unreachable', 'not_found', 'invalid', 'not_act'}."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ tokens


def new_token() -> tuple[str, str]:
    """A fresh connect token and its sha256 hex (the only form the database sees)."""
    token = secrets.token_urlsafe(32)
    if not CONNECT_TOKEN_RE.fullmatch(token):  # 32 bytes are always 43 urlsafe characters
        raise RuntimeError("connect token has an unexpected shape")
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64u(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _edge_aad(session_id: str) -> bytes:
    return EDGE_INFO + b":" + str(session_id).lower().encode("ascii")


def edge_seal(session_id: str, token: str) -> str:
    """Seal a Modal edge token for the row `session_id` under the CURRENT sub-key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kid, key = hf_credentials.subkeys(EDGE_INFO)[0]
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, token.encode("utf-8"), _edge_aad(session_id))
    return f"{EDGE_ENVELOPE}.{kid}.{_b64u(nonce + ct)}"


def edge_open(session_id, envelope) -> str | None:
    """The edge token of `envelope`, or None for anything that does not open:
    a malformed envelope, a kid this process does not hold (current or previous),
    another session id, a tag failure, no key at all. Never raises."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not isinstance(session_id, str) or not session_id or not isinstance(envelope, str):
            return None
        parts = envelope.split(".")
        if len(parts) != 3 or parts[0] != EDGE_ENVELOPE or not re.fullmatch(r"[0-9a-f]{8}", parts[1]):
            return None
        key = next((k for kid, k in hf_credentials.subkeys(EDGE_INFO) if kid == parts[1]), None)
        if key is None:
            return None
        raw = _unb64u(parts[2])
        if len(raw) < NONCE_BYTES + 17:
            return None
        token = AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], _edge_aad(session_id)).decode("utf-8")
        return token if EDGE_TOKEN_RE.fullmatch(token) else None
    except Exception:  # noqa: BLE001 - InvalidTag, HfCredentialError, decode errors: all "does not open"
        return None


# --------------------------------------------------------------- visibility


def visible_training(profile, training_id) -> dict | None:
    """The trainings row when the caller may run it, else None (the route answers 404).

    Exactly routes/training.py::get_training's ladder: the caller's own row, or
    a row of a workgroup the caller is or was a member of. Students only — the
    route already refused every other role.
    """
    from app.services.supabase_client import get_supabase
    from app.services import workgroups

    rows = get_supabase().table("trainings").select("*").eq("id", training_id).execute().data or []
    if not rows:
        return None
    row = rows[0]
    uid = str(profile["id"])
    if str(row.get("user_id")) == uid:
        return row
    wg = row.get("workgroup_id")
    if wg and wg in workgroups.resolve_visible_workgroup_ids(uid):
        return row
    return None


# ----------------------------------------------------------- model summary


def _int_in(value, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _trained_fps(meta):
    """InferenceManager.read_policy_summary's rule: bool/non-number/≤0 -> None."""
    fps = meta.get("dataset_fps") if isinstance(meta, dict) else None
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        return None
    return fps


def summary_from_config(config, meta) -> dict:
    """The `ready.policy` dict (spec §2.2) of a model's config.json.

    The config half of chunk_engine.validate_model_dir's rules (§2.7): refuses
    `not_act` for a type other than act, temporal ensembling, n_obs_steps ≠ 1 or
    PEFT; `invalid` for a visual feature that is not [3,H,W], no state or action
    feature, no camera or more than four, or a non-integer chunk size.
    """
    if not isinstance(config, dict):
        raise ModelResolveError("invalid")
    if config.get("type") != "act":
        raise ModelResolveError("not_act")
    if config.get("temporal_ensemble_coeff") is not None:
        raise ModelResolveError("not_act")
    if config.get("n_obs_steps", 1) != 1 or isinstance(config.get("n_obs_steps", 1), bool):
        raise ModelResolveError("not_act")
    if config.get("use_peft") is True:
        raise ModelResolveError("not_act")
    inputs = config.get("input_features")
    outputs = config.get("output_features")
    if not isinstance(inputs, dict) or not isinstance(outputs, dict):
        raise ModelResolveError("invalid")
    cameras: list[str] = []
    shapes: dict[str, list[int]] = {}
    for key, feature in inputs.items():
        if not isinstance(key, str) or not key.startswith("observation.images."):
            continue
        name = key[len("observation.images."):]
        shape = feature.get("shape") if isinstance(feature, dict) else None
        if (not CAMERA_NAME_RE.fullmatch(name) or not isinstance(shape, list) or len(shape) != 3
                or shape[0] != 3 or not all(_int_in(s, 1, MAX_IMAGE_SIDE) for s in shape[1:])):
            raise ModelResolveError("invalid")
        cameras.append(name)
        shapes[name] = [3, shape[1], shape[2]]
    if not 1 <= len(cameras) <= MAX_CAMERAS:
        raise ModelResolveError("invalid")
    state = inputs.get("observation.state")
    action = outputs.get("action")
    dims = []
    for feature in (state, action):
        shape = feature.get("shape") if isinstance(feature, dict) else None
        if not isinstance(shape, list) or len(shape) != 1 or not _int_in(shape[0], 1, MAX_DIM):
            raise ModelResolveError("invalid")
        dims.append(shape[0])
    chunk_size = config.get("chunk_size")
    n_action_steps = config.get("n_action_steps")
    if not _int_in(chunk_size, 1, 10_000) or not _int_in(n_action_steps, 1, 10_000):
        raise ModelResolveError("invalid")
    return {
        "type": "act",
        "expected_cameras": cameras,
        "image_shapes": shapes,
        "trained_fps": _trained_fps(meta),
        "chunk_size": chunk_size,
        "n_action_steps": n_action_steps,
        "state_dim": dims[0],
        "action_dim": dims[1],
        "requires_task": False,  # ACT is not language-conditioned
    }


_TOO_BIG = object()
_model_cache: dict[str, tuple[float, dict]] = {}
_model_lock = threading.Lock()


def _reset_model_cache_for_tests() -> None:
    with _model_lock:
        _model_cache.clear()


def _get_capped(client, url: str, headers: dict):
    """(status, body | None | _TOO_BIG): a body read only on 200, never past CONFIG_MAX_BYTES + 1."""
    with client.stream("GET", url, headers=headers) as response:
        if response.status_code != 200:
            return response.status_code, None
        declared = (response.headers or {}).get("content-length")
        try:
            if declared is not None and int(declared) > CONFIG_MAX_BYTES:
                return 200, _TOO_BIG
        except (TypeError, ValueError):
            pass
        buf = bytearray()
        for chunk in response.iter_bytes():
            buf += chunk
            if len(buf) > CONFIG_MAX_BYTES:
                return 200, _TOO_BIG
        return 200, bytes(buf)


def _json_or_none(body):
    if body is None or body is _TOO_BIG:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _fetch_model(repo: str) -> dict:
    import httpx

    headers = {}
    platform_token = os.environ.get("HF_TOKEN")
    if platform_token:
        headers["Authorization"] = f"Bearer {platform_token}"
    with httpx.Client(timeout=HF_TIMEOUT_S, follow_redirects=True) as client:
        status, body = _get_capped(client, f"{HF_BASE_URL}/api/models/{repo}/revision/main", headers)
        if status in (401, 403, 404):
            raise ModelResolveError("not_found")
        if status != 200:
            logger.warning("inference model info: status=%s repo=%s", status, repo)
            raise ModelResolveError("unreachable")
        info = _json_or_none(body)
        sha = info.get("sha") if isinstance(info, dict) else None
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise ModelResolveError("invalid")
        status, body = _get_capped(client, f"{HF_BASE_URL}/{repo}/resolve/{sha}/config.json", headers)
        if status in (401, 403, 404):
            raise ModelResolveError("invalid")
        if status != 200:
            logger.warning("inference model config: status=%s repo=%s", status, repo)
            raise ModelResolveError("unreachable")
        config = _json_or_none(body)
        if not isinstance(config, dict):
            raise ModelResolveError("invalid")
        status, body = _get_capped(client, f"{HF_BASE_URL}/{repo}/resolve/{sha}/edubotics_model_meta.json", headers)
        if status == 404:
            meta = None
        elif status != 200:
            logger.warning("inference model meta: status=%s repo=%s", status, repo)
            raise ModelResolveError("unreachable")
        else:
            meta = _json_or_none(body)
    summary = summary_from_config(config, meta)
    return {**summary, "model_repo": repo, "model_revision": sha}


def resolve_model(repo) -> dict:
    """The model's summary + {model_repo, model_revision} at main's head.

    Cached per repo for MODEL_CACHE_TTL_S (one uvicorn worker); a failure is not
    cached. Raises ModelResolveError('unreachable' | 'not_found' | 'invalid' |
    'not_act'). The repo must match MODEL_REPO_RE before anything is fetched.
    """
    if not isinstance(repo, str) or not MODEL_REPO_RE.fullmatch(repo):
        raise ModelResolveError("not_found")
    now = time.monotonic()
    with _model_lock:
        hit = _model_cache.get(repo)
        if hit is not None and now - hit[0] < MODEL_CACHE_TTL_S:
            return dict(hit[1])
    try:
        result = _fetch_model(repo)
    except ModelResolveError:
        raise
    except Exception as exc:  # noqa: BLE001 - transport errors; never log str(exc)
        logger.warning("inference model resolve failed: %s repo=%s", type(exc).__name__, repo)
        raise ModelResolveError("unreachable") from None
    with _model_lock:
        _model_cache[repo] = (now, result)
    return dict(result)


# ------------------------------------------------------------ RPC wrappers


def _pg_code(exc: Exception) -> str | None:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    m = re.search(r"\b(P\d{4}|22023)\b", str(exc))
    return m.group(1) if m else None


def _rpc(sb, name: str, args: dict, *, not_found: bool = False):
    try:
        data = sb.rpc(name, args).execute().data
    except Exception as exc:  # noqa: BLE001 - classified, never echoed
        code = _pg_code(exc)
        if not_found and code == "P0002":
            raise NotFound() from None
        logger.warning("inference rpc %s failed: %s code=%s", name, type(exc).__name__, code)
        raise SessionStoreError(name) from None
    if not isinstance(data, dict):
        logger.warning("inference rpc %s: unexpected answer %s", name, type(data).__name__)
        raise SessionStoreError(name)
    return data


def create_reservation(sb, profile, training_row, summary, token_hash) -> dict:
    """create_inference_session_safe -> {session_id, session_s, remaining_today_s, reserve_until}.

    P0048 -> DailyCapReached, P0049 -> CloudBusy, P0002 -> NotFound, else SessionStoreError.
    Only the token's HASH crosses into the database.
    """
    args = {
        "p_user_id": str(profile["id"]),
        "p_training_id": training_row["id"],
        "p_model_repo": summary["model_repo"],
        "p_model_revision": summary["model_revision"],
        "p_policy_type": "act",
        "p_token_hash": token_hash,
        "p_max_session_s": SESSION_MAX_S,
        "p_daily_cap_s": DAILY_CAP_S,
        "p_global_cap": GLOBAL_CAP,
        "p_reserve_s": RESERVE_S,
    }
    try:
        data = sb.rpc("create_inference_session_safe", args).execute().data
    except Exception as exc:  # noqa: BLE001
        code = _pg_code(exc)
        if code == "P0048":
            raise DailyCapReached() from None
        if code == "P0049":
            raise CloudBusy() from None
        if code == "P0002":
            raise NotFound() from None
        logger.warning("inference reserve failed: %s code=%s user=%s", type(exc).__name__, code, profile.get("id"))
        raise SessionStoreError("create") from None
    if not isinstance(data, dict) or not isinstance(data.get("session_id"), str):
        raise SessionStoreError("create")
    return data


def attach_edge(sb, session_id, modal_session_id, envelope) -> dict:
    return _rpc(sb, "attach_inference_edge", {
        "p_session_id": session_id, "p_modal_session_id": modal_session_id, "p_edge_token_enc": envelope,
    })


def arm(sb, user_id, session_id, token_hash) -> dict:
    return _rpc(sb, "arm_inference_session", {
        "p_session_id": session_id, "p_user_id": str(user_id), "p_token_hash": token_hash,
    }, not_found=True)


def end_session(sb, user_id, session_id, reason="user_stop") -> dict:
    return _rpc(sb, "end_inference_session", {
        "p_session_id": session_id, "p_user_id": str(user_id), "p_reason": reason,
    }, not_found=True)


def client_heartbeat(sb, user_id, session_id) -> dict:
    return _rpc(sb, "heartbeat_inference_session_client", {
        "p_session_id": session_id, "p_user_id": str(user_id),
    }, not_found=True)


def usage(sb, user_id) -> dict:
    return _rpc(sb, "inference_usage", {"p_user_id": str(user_id), "p_daily_cap_s": DAILY_CAP_S})


# ------------------------------------------------------- teacher run rows


def log_ended_sessions(sb, limit=100) -> int:
    """Write one `inference_runs` row (backend 'cloud') per ended session the robot
    connected to; returns how many. Mark-then-insert: a lost insert drops one
    teacher row, it never duplicates one. A reservation that never connected is
    marked and logs nothing (R-19)."""
    rows = (sb.table("inference_sessions").select("id").eq("status", "ended")
            .is_("run_logged_at", "null").limit(limit).execute().data or [])
    ids = [r["id"] for r in rows if r.get("id")]
    if not ids:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    marked = (sb.table("inference_sessions").update({"run_logged_at": now_iso})
              .in_("id", ids).is_("run_logged_at", "null").execute().data or [])
    runs = []
    for r in marked:
        if not r.get("connected_at"):
            continue
        reason = r.get("end_reason") or ""
        runs.append({
            "student_user_id": r["user_id"],
            "classroom_id": r.get("classroom_id"),
            "jetson_id": None,
            "policy_repo": r.get("model_repo") or "",
            "started_at": r.get("connected_at"),
            "duration_s": r.get("active_seconds"),
            "exit_reason": EXIT_REASON.get(reason, "error"),
            "error_message_de": END_REASON_DE.get(reason, ""),
            "backend": "cloud",
        })
    if not runs:
        return 0
    try:
        sb.table("inference_runs").insert(runs).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("inference run rows not written (%d): %s", len(runs), type(exc).__name__)
        return 0
    for student in sorted({r["student_user_id"] for r in runs}):
        _retain_newest(sb, student)
    return len(runs)


def _retain_newest(sb, student_id) -> None:
    """Keep the newest INFERENCE_RUNS_KEEP rows of one student (routes/jetson.py's rule)."""
    try:
        keep = (sb.table("inference_runs").select("id").eq("student_user_id", student_id)
                .order("created_at", desc=True).limit(INFERENCE_RUNS_KEEP).execute().data or [])
        if len(keep) >= INFERENCE_RUNS_KEEP:
            keep_ids = [r["id"] for r in keep]
            (sb.table("inference_runs").delete().eq("student_user_id", student_id)
             .not_.in_("id", keep_ids).execute())
    except Exception as exc:  # noqa: BLE001 - retention is best-effort
        logger.warning("inference_runs retention failed: %s", type(exc).__name__)


__all__ = [
    "SESSION_MAX_S", "DAILY_CAP_S", "GLOBAL_CAP", "RESERVE_S", "EDGE_IDLE_S",
    "MODEL_REPO_RE", "SESSION_ID_RE", "MODAL_SESSION_ID_RE", "EDGE_TOKEN_RE",
    "END_REASON_DE", "EXIT_REASON", "new_token", "edge_seal", "edge_open", "visible_training",
    "resolve_model", "summary_from_config", "create_reservation", "attach_edge", "arm", "end_session",
    "client_heartbeat", "usage", "log_ended_sessions",
]
