"""Managed ACT training: settings, dispatcher, sweep, reaper (spec §3.6, §3.8, §4.2, §4.3).

A managed row (``trainings.managed``) is placed attempt by attempt: RunPod
Community (EEA requested) → RunPod Secure (EEA, GDPR, verified) → Modal L4,
attempt ``MAX_ATTEMPTS`` always on Modal. Every state change of such a row is
one of migration 049's compare-and-set functions (P0049 = another instance or
the sweep was first; never an error). What lives in memory here (caches,
skip lists, the "two rounds" counters, the delete retry set) only ever costs a
round when the process restarts.

Loops (started by ``managed_supervisor`` whenever ``TRAINING_DISPATCH=managed``
or a managed row is non-terminal, §3.8 / FR-3; off with
``TRAINING_DISPATCH_DISABLED=1``):

* dispatcher (``DISPATCH_INTERVAL_S`` and on ``kick()``): §4.2;
* sweep (``SWEEP_INTERVAL_S``): losses, caps, the placement guard, publish
  recovery, mirrors, the janitor, failed-log uploads (§4.3, TR-2/TR-3);
* reaper (``REAPER_INTERVAL_S``): every pod we created that is not the attached
  current attempt of an active row is deleted (§4.3, SR-8);
* the HF write check and the RunPod boot probe (every 10 min until good).

Blocking I/O never runs on the event loop: supabase-py in ``asyncio.to_thread``
(two batched reads per round, FR-21), RunPod through one ``httpx.AsyncClient``
(``runpod_client``), Modal through ``modal_client``'s threads, publishing and
mirroring in ``hf_publish``'s executor (§4.13).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services import runpod_client
from app.services import training_texts_de as T

logger = logging.getLogger(__name__)

# One id per process start (FR-4): uvicorn is PID 1 in every Railway container,
# so a process id would make two instances one lease owner.
BOOT_ID = uuid.uuid4().hex

# ---------------------------------------------------------------------------
# Constants (spec §3.9). Worker copies live in modal_training/training_handler.py.
# ---------------------------------------------------------------------------
SAVE_FREQ = 2500
MIRROR_EVERY = 10000
ACT_BATCH = 8
MAX_ATTEMPTS = 4
COMMUNITY_LOSS_LIMIT = 2
MANAGED_ACT_CAP_S = 14400
WALL_CAP_S = 43200
HEARTBEAT_INTERVAL_S = 15
HEARTBEAT_LOST_S = 300
STALL_S = 1500
POD_GONE_CONFIRM_S = 30
PLACING_STALE_S = 600
PLACEMENT_GIVEUP_S = 1800
RUNPOD_GIVEUP_S = 120
DC_SKIP_S = 1800
NO_BALANCE_BACKOFF_S = 60
CATALOG_CACHE_S = 60
DISPATCH_INTERVAL_S = 10
SWEEP_INTERVAL_S = 30
REAPER_INTERVAL_S = 60
REAPER_GRACE_S = 1800
PUBLISH_LEASE_S = 900
PUBLISH_RENEW_S = 60
PUBLISH_RETRY_S = 21600
RESUME_CRASH_LIMIT = 2
START_LOOP_MAX = 5
MAX_CREATES_PER_TICK = 6
CREATE_400_CACHE_S = 300
DC_TABLE_CACHE_S = 600
REAPER_SURVIVOR_ROUNDS = 3
MIRROR_LEASE_S = 900
HF_CHECK_TIMEOUT_S = 10
FINAL_UPLOAD_WAIT_S = 1080
WATCHDOG_GRACE_S = 1200
PUT_TTL_S = 900
GET_TTL_S = 3600
MIN_FREE_DISK_B = 30 * 1024**3
NUM_WORKERS_MAX = 12
OUTPUT_DIR = "/tmp/training_output"

MAX_ROWS_PER_TICK = 5              # claims per dispatcher tick (TR-4)
RECHECK_S = 600                    # HF check, RunPod probe, supervisor (§3.8)
START_TIMEOUT_GRACE_S = 300        # the dispatcher's own cap margin (§4.2 step 2)
JANITOR_WINDOW_S = 7 * 86400       # terminal rows the janitor revisits (R2 lifecycle is the backstop)
# train_managed's Modal timeout is fixed at 7 h in code (§4.9, TR-8 n11).
MODAL_TIMEOUT_S = 7 * 3600
MANAGED_CAP_CEILING_S = MODAL_TIMEOUT_S - FINAL_UPLOAD_WAIT_S - 600  # 23 520 s

HF_ORG = "EduBotics-Solutions"
TRAINER_IMAGE_PREFIX = "ghcr.io/svendanilborodun/edubotics-trainer"
TRAINER_IMAGE_RE = re.compile(
    r"ghcr\.io/svendanilborodun/edubotics-trainer(:[A-Za-z0-9._-]{1,128})?@sha256:[0-9a-f]{64}"
)
POD_PREFIX_RE = re.compile(r"[a-z0-9-]{3,40}")
RUNG_COMMUNITY = "runpod_community"
RUNG_SECURE = "runpod_secure"
RUNG_MODAL = "modal"
RUNGS = (RUNG_COMMUNITY, RUNG_SECURE, RUNG_MODAL)
RUNPOD_RUNGS = (RUNG_COMMUNITY, RUNG_SECURE)
RUNG_CLOUD = {RUNG_COMMUNITY: "COMMUNITY", RUNG_SECURE: "SECURE"}

DEFAULT_LADDER = [
    {"rung": RUNG_COMMUNITY, "gpus": [
        {"id": "NVIDIA RTX A5000", "min_vcpu": 8}, {"id": "NVIDIA GeForce RTX 3080", "min_vcpu": 8},
        {"id": "NVIDIA RTX A4000", "min_vcpu": 6}, {"id": "NVIDIA GeForce RTX 3080 Ti", "min_vcpu": 8},
        {"id": "NVIDIA GeForce RTX 4070 Ti", "min_vcpu": 8}, {"id": "NVIDIA RTX A4500", "min_vcpu": 8},
        {"id": "NVIDIA RTX 4000 Ada Generation", "min_vcpu": 6}, {"id": "NVIDIA GeForce RTX 3090", "min_vcpu": 8},
        {"id": "NVIDIA GeForce RTX 3090 Ti", "min_vcpu": 8}, {"id": "NVIDIA GeForce RTX 4080", "min_vcpu": 8},
        {"id": "NVIDIA GeForce RTX 4080 SUPER", "min_vcpu": 8}, {"id": "NVIDIA RTX A6000", "min_vcpu": 8},
        {"id": "NVIDIA GeForce RTX 4090", "min_vcpu": 8}]},
    {"rung": RUNG_SECURE, "gpus": [
        {"id": "NVIDIA RTX A5000", "min_vcpu": 8}, {"id": "NVIDIA RTX A4500", "min_vcpu": 8},
        {"id": "NVIDIA RTX A4000", "min_vcpu": 6}, {"id": "NVIDIA RTX 4000 Ada Generation", "min_vcpu": 6},
        {"id": "NVIDIA GeForce RTX 3090", "min_vcpu": 8}, {"id": "NVIDIA RTX A6000", "min_vcpu": 8},
        {"id": "NVIDIA GeForce RTX 4090", "min_vcpu": 8}]},
    {"rung": RUNG_MODAL},
]

ACTIVE_STATES = ("placing", "starting", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "canceled")

TRAINING_COLUMNS = (
    "id, user_id, status, managed, current_attempt, train_seconds_used, status_note, step_advanced_at, "
    "dataset_fps, unplaced_since, requested_at, last_progress_at, total_steps, training_params, "
    "model_name, dataset_name, terminated_at, log_url, cancel_attempts, current_step"
)
ATTEMPT_COLUMNS = (
    "training_id, attempt_no, backend, cloud_ref, gpu_type, data_center, cuda_version, cost_per_hr, state, "
    "created_at, started_at, first_heartbeat_at, ended_at, end_reason, resume_from_step, resume_from_attempt, "
    "allowed_dcs, publish_claimed_at, publish_owner, publish_first_at, publish_tries"
)
CHECKPOINT_COLUMNS = (
    "training_id, attempt_no, step, state, manifest, created_at, ready_at, mirrored_at, mirror_tries, "
    "refused_reason, refused_by, crash_attempts, mirror_claimed_at, mirror_owner"
)


# ---------------------------------------------------------------------------
# Settings (§3.8)
# ---------------------------------------------------------------------------


class SettingsError(RuntimeError):
    """A malformed managed-training setting. The message names variables, never values."""


@dataclass(frozen=True)
class Settings:
    dispatch: str = "legacy"
    worker_api_url: str = ""
    worker_supabase_url: str = ""
    anon_key: str = ""
    hf_token: str = ""
    runpod_api_key: str = ""
    trainer_image: str = ""
    ladder: tuple = ()
    pod_prefix: str = "edubotics-train"
    max_runpod_pods: int = 30
    start_deadline_s: int = 1200
    cap_s: int = MANAGED_ACT_CAP_S
    save_freq: int = SAVE_FREQ
    mirror_every: int = MIRROR_EVERY
    e2e: bool = False
    r2_configured: bool = False
    missing: tuple = ()          # required-but-missing variable NAMES (§3.8)

    @property
    def managed(self) -> bool:
        return self.dispatch == "managed"

    @property
    def has_runpod_rung(self) -> bool:
        return any(r["rung"] in RUNPOD_RUNGS for r in self.ladder)

    def rung_gpus(self, rung: str) -> list:
        for r in self.ladder:
            if r["rung"] == rung:
                return list(r.get("gpus") or [])
        return []


def parse_ladder(raw) -> tuple:
    """RUNPOD_LADDER (JSON) or the default, validated (§3.6): a list; each rung
    once; modal present and last; gpus non-empty lists of {id, min_vcpu 1–32}."""
    if raw is None or raw == "":
        data = json.loads(json.dumps(DEFAULT_LADDER))
    else:
        try:
            data = json.loads(raw)
        except ValueError:
            raise SettingsError("RUNPOD_LADDER is not JSON") from None
    if type(data) is not list or not data:
        raise SettingsError("RUNPOD_LADDER must be a non-empty list")
    seen = []
    out = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("rung") not in RUNGS:
            raise SettingsError("RUNPOD_LADDER: unknown rung")
        rung = entry["rung"]
        if rung in seen:
            raise SettingsError("RUNPOD_LADDER: a rung appears twice")
        seen.append(rung)
        if rung == RUNG_MODAL:
            if set(entry) != {"rung"}:
                raise SettingsError("RUNPOD_LADDER: the modal rung takes no other key")
            out.append({"rung": RUNG_MODAL})
            continue
        if set(entry) != {"rung", "gpus"}:
            raise SettingsError("RUNPOD_LADDER: a RunPod rung has exactly rung and gpus")
        gpus = entry["gpus"]
        if type(gpus) is not list or not gpus:
            raise SettingsError("RUNPOD_LADDER: gpus must be a non-empty list")
        clean = []
        for g in gpus:
            if (not isinstance(g, dict) or set(g) != {"id", "min_vcpu"} or type(g["id"]) is not str
                    or not g["id"].strip() or type(g["min_vcpu"]) is not int or not 1 <= g["min_vcpu"] <= 32):
                raise SettingsError("RUNPOD_LADDER: a gpu entry is not {id, min_vcpu 1-32}")
            clean.append({"id": g["id"], "min_vcpu": g["min_vcpu"]})
        out.append({"rung": rung, "gpus": clean})
    if RUNG_MODAL not in seen or seen[-1] != RUNG_MODAL:
        raise SettingsError("RUNPOD_LADDER: modal must be present and last")
    return tuple(out)


def _int_env(env, name, default, lo=1, hi=None):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} is not an integer") from None
    if value < lo or (hi is not None and value > hi):
        raise SettingsError(f"{name} is out of range")
    return value


def load_settings(env=None) -> Settings:
    """Parse and validate every managed-training variable. Raises SettingsError
    for a MALFORMED value whatever TRAINING_DISPATCH says; required-but-missing
    variables are listed in ``Settings.missing`` (the boot decides)."""
    env = os.environ if env is None else env
    dispatch = (env.get("TRAINING_DISPATCH") or "legacy").strip()
    if dispatch not in ("legacy", "managed"):
        raise SettingsError("TRAINING_DISPATCH must be legacy or managed")
    ladder = parse_ladder(env.get("RUNPOD_LADDER"))
    e2e = env.get("TRAINING_E2E") == "1"
    save_freq = _int_env(env, "TRAINING_SAVE_FREQ", SAVE_FREQ)
    mirror_every = _int_env(env, "TRAINING_MIRROR_EVERY", MIRROR_EVERY)
    if not e2e:
        if save_freq != SAVE_FREQ or mirror_every != MIRROR_EVERY:
            raise SettingsError(
                "TRAINING_SAVE_FREQ / TRAINING_MIRROR_EVERY may differ from 2500 / 10000 only with TRAINING_E2E=1"
            )
    elif mirror_every % save_freq != 0:
        raise SettingsError("TRAINING_MIRROR_EVERY must be a positive multiple of TRAINING_SAVE_FREQ")
    cap_s = _int_env(env, "MANAGED_ACT_CAP_S", MANAGED_ACT_CAP_S)
    if any(r["rung"] == RUNG_MODAL for r in ladder) and cap_s > MANAGED_CAP_CEILING_S:
        raise SettingsError("MANAGED_ACT_CAP_S exceeds the Modal ceiling (7 h minus the final-upload wait and 600 s)")
    prefix = (env.get("RUNPOD_POD_PREFIX") or "edubotics-train").strip()
    if not POD_PREFIX_RE.fullmatch(prefix):
        raise SettingsError("RUNPOD_POD_PREFIX must match ^[a-z0-9-]{3,40}$")
    image = (env.get("RUNPOD_TRAINER_IMAGE") or "").strip()
    if image and not TRAINER_IMAGE_RE.fullmatch(image):
        raise SettingsError("RUNPOD_TRAINER_IMAGE must be the digest-pinned edubotics-trainer image")
    worker_url = (env.get("TRAINING_WORKER_API_URL") or "").strip().rstrip("/")
    if worker_url and not worker_url.startswith("https://"):
        raise SettingsError("TRAINING_WORKER_API_URL must be an https URL")
    settings = Settings(
        dispatch=dispatch,
        worker_api_url=worker_url,
        worker_supabase_url=(env.get("TRAINING_WORKER_SUPABASE_URL") or env.get("SUPABASE_URL") or "").strip(),
        anon_key=(env.get("SUPABASE_ANON_KEY") or "").strip(),
        hf_token=(env.get("HF_TOKEN") or "").strip(),
        runpod_api_key=(env.get("RUNPOD_API_KEY") or "").strip(),
        trainer_image=image,
        ladder=ladder,
        pod_prefix=prefix,
        max_runpod_pods=_int_env(env, "MAX_RUNPOD_PODS", 30),
        start_deadline_s=_int_env(env, "TRAINING_START_DEADLINE_S", 1200),
        cap_s=cap_s,
        save_freq=save_freq,
        mirror_every=mirror_every,
        e2e=e2e,
        r2_configured=all(env.get(k) for k in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                                                "R2_BUCKET")),
    )
    missing = [k for k in ("SUPABASE_ANON_KEY", "TRAINING_WORKER_API_URL", "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID",
                           "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "HF_TOKEN") if not (env.get(k) or "").strip()]
    if settings.has_runpod_rung:
        missing += [k for k in ("RUNPOD_API_KEY", "RUNPOD_TRAINER_IMAGE") if not (env.get(k) or "").strip()]
    return Settings(**{**settings.__dict__, "missing": tuple(missing)})


SETTINGS: Settings = Settings(ladder=tuple(json.loads(json.dumps(DEFAULT_LADDER))))


def validate_boot(env=None) -> Settings:
    """main.py's import-time check (§3.8). Malformed → RuntimeError; required
    variables missing → RuntimeError under TRAINING_DISPATCH=managed, ERROR log
    otherwise (the managed loops may still have to drive leftover rows)."""
    global SETTINGS
    try:
        settings = load_settings(env)
    except SettingsError as exc:
        raise RuntimeError(f"Cloud API cannot start — managed training: {exc}.") from None
    if settings.e2e:
        logger.warning(
            "TRAINING_E2E=1: managed checkpoints every %d steps, mirrors every %d (an end-to-end stack only, "
            "never production)", settings.save_freq, settings.mirror_every,
        )
    if settings.missing:
        if settings.managed:
            raise RuntimeError(
                "Cloud API cannot start — TRAINING_DISPATCH=managed but required Railway service variables "
                f"are missing or empty: {', '.join(settings.missing)}."
            )
        logger.info("managed training: not configured (%s missing)", ", ".join(settings.missing))
    SETTINGS = settings
    return settings


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def ts(value) -> float | None:
    """Epoch seconds of a PostgREST timestamp (or None)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _sb():
    from app.services.supabase_client import get_supabase

    return get_supabase()


_PG_CODE_RE = re.compile(r"\b(P0\d{3}|22023|42501|23505|PGRST\d{3})\b")


def pg_code(exc) -> str | None:
    """The SQLSTATE / PostgREST code of a supabase-py error, or None."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    m = _PG_CODE_RE.search(str(exc))
    return m.group(1) if m else None


class RpcConflict(Exception):
    """P0049: a lost compare-and-set (INFO, no further action)."""


def rpc(name: str, args: dict):
    """A 049 RPC as the service role; P0049 → RpcConflict, anything else propagates."""
    try:
        return _sb().rpc(name, args).execute().data
    except Exception as exc:
        if pg_code(exc) == "P0049":
            raise RpcConflict(name) from None
        raise


async def arpc(name: str, args: dict):
    return await asyncio.to_thread(rpc, name, args)


def _params(row) -> dict:
    p = row.get("training_params")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = None
    return p if isinstance(p, dict) else {}


def total_steps_of(row) -> int:
    """The API's single source for a managed row's step count (FR-1)."""
    steps = _params(row).get("steps")
    return int(steps) if isinstance(steps, int) and steps > 0 else int(row.get("total_steps") or 0)


def save_freq_of(row) -> int:
    v = _params(row).get("save_freq")
    return int(v) if isinstance(v, int) and v > 0 else SAVE_FREQ


def mirror_every_of(row) -> int:
    v = _params(row).get("mirror_every")
    return int(v) if isinstance(v, int) and v > 0 else MIRROR_EVERY


def attempt_seconds(attempt, now: float) -> int:
    """now − first_heartbeat_at of the attempt (0 when unset)."""
    first = ts((attempt or {}).get("first_heartbeat_at"))
    if first is None:
        return 0
    return max(0, int(now - first))


def resume_step_from(checkpoints, total_steps: int) -> int:
    """Newest ready checkpoint step below the total (refused/deleted excluded), or 0."""
    steps = [c["step"] for c in checkpoints if c.get("state") == "ready" and c.get("step", 0) < total_steps]
    return max(steps) if steps else 0


def final_ready(checkpoints, total_steps: int):
    for c in checkpoints:
        if c.get("state") == "ready" and c.get("step") == total_steps:
            return c
    return None


def sanitize_log_text(text, limit: int = 500) -> str:
    if not isinstance(text, str):
        return ""
    return "".join(ch if ch.isprintable() else " " for ch in text)[:limit]


# ---------------------------------------------------------------------------
# In-memory state (per process)
# ---------------------------------------------------------------------------


@dataclass
class _State:
    hf_check: object = None          # True | False | None | 'rejected' | 'unreachable' | 'bad_reply' | 'no_token'
    runpod_probe: object = None      # None (unknown) | True | False (401/403)
    runpod_402_until: float = 0.0
    dc_skip: dict = field(default_factory=dict)
    create_400: dict = field(default_factory=dict)
    catalog: dict = field(default_factory=dict)
    gdpr: tuple | None = None
    dc_table: tuple | None = None
    all_403_logged: bool = False
    pod_count: int = 0
    last_failed_round_at: float = field(default_factory=time.time)
    gone_first: dict = field(default_factory=dict)
    heartbeat_miss: dict = field(default_factory=dict)
    guard_final: set = field(default_factory=set)
    guard_warned: set = field(default_factory=set)
    delete_retry: dict = field(default_factory=dict)       # pod id -> rounds still listed
    modal_cancel_retry: set = field(default_factory=set)
    budget_bound: dict = field(default_factory=dict)       # (tid, attempt) -> 'wall' | 'cap'
    janitor_done: set = field(default_factory=set)
    loops_started: bool = False


STATE = _State()
_STATE_LOCK = threading.Lock()


def reset_state_for_tests() -> None:
    global STATE
    STATE = _State()


def placement_blocked(settings: Settings | None = None) -> bool:
    """§3.8: placement waits while the HF write check is False, `rejected` or has
    no token; and while a variable every attempt needs is missing."""
    s = settings or SETTINGS
    if STATE.hf_check in (False, "rejected", "no_token"):
        return True
    if not (s.worker_api_url and s.anon_key and s.r2_configured):
        return True
    return False


def runpod_configured(settings: Settings | None = None) -> bool:
    s = settings or SETTINGS
    return bool(s.runpod_api_key and s.trainer_image)


def skip_dc(dc, now: float | None = None) -> None:
    if dc:
        STATE.dc_skip[dc] = (now or _now()) + DC_SKIP_S


def _dc_skipped(dc, now: float) -> bool:
    until = STATE.dc_skip.get(dc)
    return until is not None and until > now


def record_budget_bound(training_id: int, attempt: int, bound: str) -> None:
    STATE.budget_bound[(int(training_id), int(attempt))] = bound


def budget_for(row, attempt, now: float, settings: Settings | None = None) -> tuple[int, str]:
    """``(budget_s, bound)`` of §3.2 `start` (FR-18): the smaller of the training-time
    cap left (minus the current attempt's own time) and the 12 h wall cap left."""
    s = settings or SETTINGS
    cap_left = s.cap_s - int(row.get("train_seconds_used") or 0) - attempt_seconds(attempt, now)
    requested = ts(row.get("requested_at")) or now
    wall_left = WALL_CAP_S - int(now - requested)
    if wall_left < cap_left:
        return int(wall_left), "wall"
    return int(cap_left), "cap"


def time_cap_sentence(row, attempt, now: float, settings: Settings | None = None) -> str:
    """`finish {failed, time_cap}` → S6 when the wall term bound at `start`, else S5 (TR-2 n3)."""
    s = settings or SETTINGS
    bound = STATE.budget_bound.get((int(row["id"]), int(attempt["attempt_no"])))
    if bound is None:
        _, bound = budget_for(row, attempt, now, s)
    return T.S6 if bound == "wall" else T.s5(s.cap_s)


# ---------------------------------------------------------------------------
# DB reads (sync; callers use asyncio.to_thread)
# ---------------------------------------------------------------------------


def read_rows(statuses) -> list:
    return (
        _sb().table("trainings").select(TRAINING_COLUMNS)
        .eq("managed", True).in_("status", list(statuses))
        .order("requested_at", desc=False).execute()
    ).data or []


def read_attempts(ids) -> list:
    if not ids:
        return []
    return (_sb().table("training_attempts").select(ATTEMPT_COLUMNS).in_("training_id", list(ids)).execute()).data or []


def read_checkpoints(ids, states=None) -> list:
    if not ids:
        return []
    q = _sb().table("training_checkpoints").select(CHECKPOINT_COLUMNS).in_("training_id", list(ids))
    if states:
        q = q.in_("state", list(states))
    return q.execute().data or []


def read_row(training_id: int):
    rows = (_sb().table("trainings").select(TRAINING_COLUMNS).eq("id", int(training_id)).execute()).data or []
    return rows[0] if rows else None


def read_attempt(training_id: int, attempt_no: int):
    rows = (
        _sb().table("training_attempts").select(ATTEMPT_COLUMNS)
        .eq("training_id", int(training_id)).eq("attempt_no", int(attempt_no)).execute()
    ).data or []
    return rows[0] if rows else None


def _group(rows, key="training_id") -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def write_attempt_meta(training_id: int, attempt_no: int, payload: dict, states=("starting", "running")) -> None:
    """A direct metadata write (§3.1 rule of state), filtered on the attempt's state."""
    (
        _sb().table("training_attempts").update(payload)
        .eq("training_id", int(training_id)).eq("attempt_no", int(attempt_no))
        .in_("state", list(states)).execute()
    )


def mark_unplaced(row, now: float) -> None:
    """The ONE direct write of trainings.unplaced_since (FR-3): NULL → now, filtered."""
    if row.get("unplaced_since"):
        return
    (
        _sb().table("trainings").update({"unplaced_since": _iso(now)})
        .eq("id", int(row["id"])).eq("managed", True).eq("status", "queued")
        .eq("current_attempt", int(row.get("current_attempt") or 0))
        .is_("unplaced_since", "null").execute()
    )


# ---------------------------------------------------------------------------
# Terminating an attempt
# ---------------------------------------------------------------------------


async def delete_pod(pod_id: str, *, locked: bool = False) -> bool:
    """DELETE with §3.6 call 7's unlock rule (FR-15, TR-8 n7). True when gone.

    PATCH ``{"locked": false}`` only when the listed pod is locked or the DELETE
    answered 400/409 — never after 401, 403 or 429. A pod not gone stays in the
    retry set; the reaper tries again every round."""
    if not pod_id:
        return True
    try:
        if locked:
            await runpod_client.patch_pod(pod_id, {"locked": False})
        resp = await runpod_client.delete_pod(pod_id)
        if resp.status in (400, 409):
            await runpod_client.patch_pod(pod_id, {"locked": False})
            resp = await runpod_client.delete_pod(pod_id)
    except runpod_client.RunpodNetworkError:
        STATE.delete_retry.setdefault(pod_id, 0)
        return False
    if resp.status in (200, 204, 404):
        STATE.delete_retry.pop(pod_id, None)
        return True
    if resp.status in (401, 403):
        logger.error("RunPod DELETE of pod %s answered %s (key without pods:write?)", pod_id, resp.status)
    elif resp.status != 429:
        logger.warning("RunPod DELETE of pod %s answered %s", pod_id, resp.status)
    STATE.delete_retry.setdefault(pod_id, 0)
    return False


async def cancel_modal(call_id: str) -> bool:
    from app.services.modal_client import cancel_training_job

    try:
        await cancel_training_job(call_id)
        STATE.modal_cancel_retry.discard(call_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Modal cancel of %s failed: %s", call_id, type(exc).__name__)
        STATE.modal_cancel_retry.add(call_id)
        return False


async def terminate_attempt(attempt) -> bool:
    """Stop whatever runs an attempt (failures go to a retry set)."""
    if not attempt:
        return True
    ref = attempt.get("cloud_ref")
    backend = attempt.get("backend")
    if not ref:
        return True
    if backend in RUNPOD_RUNGS:
        return await delete_pod(ref)
    if backend == RUNG_MODAL:
        return await cancel_modal(ref)
    return True


async def stop_for_cancel(backend, attempt_state, cloud_ref) -> bool:
    """Cancel's „stop the attempt" (§4.4, SR-19): a `starting`/`running` attempt
    with a cloud_ref is stopped (DELETE the pod / cancel the Modal call); a
    `placing` or `lost` attempt, or none, has nothing running. True = stopped."""
    if attempt_state in ("starting", "running") and cloud_ref:
        return await terminate_attempt({"backend": backend, "cloud_ref": cloud_ref})
    return True


# ---------------------------------------------------------------------------
# Losses, requeues, the end of an attempt
# ---------------------------------------------------------------------------


async def requeue_attempt(row, attempt, reason: str, now: float, checkpoints=None, *, skip_community_dc=False) -> bool:
    """requeue_training with §4.2's arguments (resume step, S1/S2, seconds). False on P0049."""
    tid = int(row["id"])
    total = total_steps_of(row)
    if checkpoints is None:
        checkpoints = await asyncio.to_thread(read_checkpoints, [tid], ["ready"])
    resume = resume_step_from(checkpoints, total)
    try:
        await arpc("requeue_training", {
            "p_training_id": tid, "p_attempt": int(attempt["attempt_no"]), "p_reason": reason,
            "p_note": T.loss_note(resume), "p_resume_step": resume,
            "p_train_seconds": attempt_seconds(attempt, now),
        })
    except RpcConflict:
        logger.info("requeue of training %s attempt %s lost a race", tid, attempt["attempt_no"])
        return False
    logger.info("training %s attempt %s requeued (%s, resume step %s)", tid, attempt["attempt_no"], reason, resume)
    if skip_community_dc and attempt.get("backend") == RUNG_COMMUNITY:
        skip_dc(attempt.get("data_center"), now)
    kick()
    return True


async def end_attempt_succeeded(row, attempt, now: float) -> bool:
    """end_training_attempt(succeeded, S3) and schedule the publish (§4.6)."""
    tid = int(row["id"])
    try:
        await arpc("end_training_attempt", {
            "p_training_id": tid, "p_attempt": int(attempt["attempt_no"]), "p_reason": "succeeded",
            "p_note": T.S3, "p_train_seconds": attempt_seconds(attempt, now),
        })
    except RpcConflict:
        logger.info("end of training %s attempt %s lost a race", tid, attempt["attempt_no"])
        return False
    from app.services import hf_publish

    hf_publish.schedule_publish(tid, int(attempt["attempt_no"]))
    return True


async def fail_training(row, attempt_no: int, message: str) -> bool:
    try:
        await arpc("finish_training", {
            "p_training_id": int(row["id"]), "p_attempt": int(attempt_no), "p_status": "failed",
            "p_message": message, "p_log_url": None,
        })
    except RpcConflict:
        return False
    logger.info("training %s failed (attempt %s)", row["id"], attempt_no)
    return True


async def on_loss(row, attempt, reason: str, now: float, checkpoints, *, skip_community_dc=False) -> None:
    """§4.3 „On a loss": a ready final → publish; else requeue; then terminate."""
    total = total_steps_of(row)
    final = final_ready(checkpoints, total)
    if final is not None:
        await end_attempt_succeeded(row, attempt, now)
    else:
        await requeue_attempt(row, attempt, reason, now, checkpoints, skip_community_dc=skip_community_dc)
    await terminate_attempt(attempt)


# ---------------------------------------------------------------------------
# RunPod catalog caches
# ---------------------------------------------------------------------------


async def _catalog(cloud: str, now: float):
    hit = STATE.catalog.get(cloud)
    if hit and now - hit[0] < CATALOG_CACHE_S:
        return hit[1]
    try:
        resp = await runpod_client.catalog_gpus(cloud)
    except runpod_client.RunpodNetworkError:
        return None
    if resp.status != 200 or not isinstance(resp.body, dict) or not isinstance(resp.body.get("gpus"), list):
        logger.warning("RunPod catalog (%s) answered %s", cloud, resp.status)
        return None
    table = {g.get("id"): g for g in resp.body["gpus"] if isinstance(g, dict)}
    STATE.catalog[cloud] = (now, table)
    return table


async def _gdpr_dcs(now: float):
    if STATE.gdpr and now - STATE.gdpr[0] < CATALOG_CACHE_S:
        return STATE.gdpr[1]
    try:
        resp = await runpod_client.datacenters_gdpr()
    except runpod_client.RunpodNetworkError:
        return None
    if resp.status != 200 or not isinstance(resp.body, dict) or not isinstance(resp.body.get("dataCenters"), list):
        return None
    ids = frozenset(d.get("id") for d in resp.body["dataCenters"] if isinstance(d, dict) and d.get("id"))
    STATE.gdpr = (now, ids)
    return ids


async def dc_regions(now: float):
    """{data centre id: region} from the unfiltered catalog (the known-outside
    table, E18, FR-8), cached DC_TABLE_CACHE_S; None when it cannot be read."""
    if STATE.dc_table and now - STATE.dc_table[0] < DC_TABLE_CACHE_S:
        return STATE.dc_table[1]
    try:
        resp = await runpod_client.datacenters_all()
    except runpod_client.RunpodNetworkError:
        return None
    if resp.status != 200 or not isinstance(resp.body, dict) or not isinstance(resp.body.get("dataCenters"), list):
        return None
    table = {d["id"]: d.get("region") for d in resp.body["dataCenters"] if isinstance(d, dict) and d.get("id")}
    STATE.dc_table = (now, table)
    return table


def known_outside(dc: str, table) -> bool:
    region = (table or {}).get(dc)
    return region is not None and region not in ("EUROPE", "UNKNOWN")


# ---------------------------------------------------------------------------
# Placement guard (§4.2, E18, FR-8, TR-8 n8)
# ---------------------------------------------------------------------------


def _parse_cuda(value):
    m = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.\d+)?\s*", value) if isinstance(value, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def judge_guard(attempt, pod, table) -> tuple[str, bool]:
    """``(verdict, final)``: verdict 'refuse' or 'ok'; ``final`` once both the data
    centre and the CUDA version were judged for good (else the sweep re-judges)."""
    key = (attempt["training_id"], attempt["attempt_no"])
    backend = attempt.get("backend")
    allowed = list(attempt.get("allowed_dcs") or [])
    dc = pod.get("dataCenterId")
    dc_final = False
    if backend == RUNG_SECURE:
        if dc is None:
            if pod.get("status") == "RUNNING":
                return "refuse", True
        elif dc not in allowed:
            return "refuse", True
        else:
            dc_final = True
    else:
        if dc is None:
            if (key, "dc-null") not in STATE.guard_warned:
                STATE.guard_warned.add((key, "dc-null"))
                logger.warning("training %s attempt %s: Community pod reports no data centre", *key)
        elif table is None:
            logger.info("training %s attempt %s: data-centre table unreadable, no verdict on %s", key[0], key[1], dc)
        elif known_outside(dc, table):
            return "refuse", True
        else:
            if dc not in allowed and (key, "dc-unrequested") not in STATE.guard_warned:
                STATE.guard_warned.add((key, "dc-unrequested"))
                logger.warning("training %s attempt %s: Community pod in data centre %s (not requested)",
                               key[0], key[1], dc)
            dc_final = True
    cuda = pod.get("cudaVersion")
    cuda_final = False
    if cuda is not None:
        parsed = _parse_cuda(cuda)
        if parsed is None:
            if (key, "cuda") not in STATE.guard_warned:
                STATE.guard_warned.add((key, "cuda"))
                logger.warning("training %s attempt %s: unparseable cudaVersion %r", key[0], key[1], str(cuda)[:32])
        elif parsed < (12, 6):
            return "refuse", True
        else:
            cuda_final = True
    return "ok", dc_final and cuda_final


async def apply_guard(row, attempt, pod, now: float, checkpoints) -> bool:
    """Judge one RunPod attempt; True when it was refused (deleted + requeued)."""
    key = (int(attempt["training_id"]), int(attempt["attempt_no"]))
    if key in STATE.guard_final:
        return False
    table = None
    if attempt.get("backend") == RUNG_COMMUNITY and pod.get("dataCenterId") is not None:
        table = await dc_regions(now)
    meta = {}
    if pod.get("dataCenterId") and pod.get("dataCenterId") != attempt.get("data_center"):
        meta["data_center"] = pod.get("dataCenterId")
    if pod.get("cudaVersion") and pod.get("cudaVersion") != attempt.get("cuda_version"):
        meta["cuda_version"] = str(pod.get("cudaVersion"))[:32]
    if meta:
        try:
            await asyncio.to_thread(write_attempt_meta, key[0], key[1], meta)
        except Exception as exc:  # noqa: BLE001 - metadata only
            logger.warning("attempt metadata write failed: %s", type(exc).__name__)
    verdict, final = judge_guard(attempt, pod, table)
    if verdict == "refuse":
        logger.warning("training %s attempt %s refused by the placement guard (dc=%s cuda=%s)",
                       key[0], key[1], pod.get("dataCenterId"), pod.get("cudaVersion"))
        await delete_pod(pod.get("id"))
        att = dict(attempt, data_center=pod.get("dataCenterId") or attempt.get("data_center"))
        await requeue_attempt(row, att, "placement_guard", now, checkpoints, skip_community_dc=True)
        STATE.guard_final.add(key)
        return True
    if final:
        STATE.guard_final.add(key)
    return False


# ---------------------------------------------------------------------------
# Dispatcher (§4.2)
# ---------------------------------------------------------------------------


@dataclass
class _Tick:
    creates_left: int = MAX_CREATES_PER_TICK
    runpod_off: bool = False
    statuses: list = field(default_factory=list)
    last_400_detail: str = ""


def _runpod_usable(now: float, settings: Settings) -> bool:
    if not runpod_configured(settings):
        return False
    if STATE.runpod_probe is False:
        return False
    if STATE.runpod_402_until > now:
        return False
    if STATE.pod_count >= settings.max_runpod_pods:
        return False
    return True


def rungs_for(row, attempts_of_row, now: float, tick: _Tick, settings: Settings | None = None) -> list:
    """§4.2 step 3: the rungs attempt N+1 may use, in ladder order."""
    s = settings or SETTINGS
    n_next = int(row.get("current_attempt") or 0) + 1
    if n_next == MAX_ATTEMPTS:
        return [RUNG_MODAL]
    rungs = [r["rung"] for r in s.ladder]
    lost_community = sum(1 for a in attempts_of_row
                         if a.get("backend") == RUNG_COMMUNITY and a.get("state") == "lost")
    if lost_community >= COMMUNITY_LOSS_LIMIT:
        rungs = [r for r in rungs if r != RUNG_COMMUNITY]
    if tick.runpod_off or not _runpod_usable(now, s):
        rungs = [r for r in rungs if r not in RUNPOD_RUNGS]
    if any(r in RUNPOD_RUNGS for r in rungs):
        unplaced = ts(row.get("unplaced_since"))
        if unplaced is None or now - unplaced < RUNPOD_GIVEUP_S:
            rungs = [r for r in rungs if r != RUNG_MODAL]
    return rungs


def pod_body(row, attempt_no: int, token: str, cloud: str, gpu: dict, dcs: list, settings: Settings) -> dict:
    """POST /v2/pods, exactly §3.6 call 3."""
    return {
        "name": f"{settings.pod_prefix}-{int(row['id'])}-a{int(attempt_no)}",
        "image": settings.trainer_image,
        "cloud": cloud,
        "gpu": {"id": gpu["id"], "count": 1, "minCudaVersion": "12.6",
                "minVcpuCountPerGpu": int(gpu["min_vcpu"]), "minRamPerGpu": 24},
        "dataCenterIds": list(dcs),
        "disk": 50,
        "ports": [],
        "env": {
            "EDUBOTICS_TRAINING_ID": str(int(row["id"])),
            "EDUBOTICS_ATTEMPT": str(int(attempt_no)),
            "EDUBOTICS_WORKER_TOKEN": str(token),
            "EDUBOTICS_API_URL": settings.worker_api_url,
            "SUPABASE_URL": settings.worker_supabase_url,
            "SUPABASE_ANON_KEY": settings.anon_key,
        },
        "startSsh": False,
        "startJupyter": False,
        "globalNetworking": False,
    }


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _create_one(body: dict, tick: _Tick):
    """One candidate's POST with §3.6's retry rules. Returns (status, response or None)."""
    tries_429 = 0
    tries_5xx = 0
    while True:
        if tick.creates_left <= 0:
            return "budget", None
        tick.creates_left -= 1
        try:
            resp = await runpod_client.create_pod(body)
        except runpod_client.RunpodNetworkError:
            resp = None
        if resp is None or resp.status >= 500:
            if tries_5xx >= 2:
                return "next", resp
            await _sleep(2 if tries_5xx == 0 else 4)
            tries_5xx += 1
            continue
        if resp.status == 429:
            if tries_429 >= 3:
                return "next", resp
            tries_429 += 1
            await _sleep(min(resp.retry_after or 1.0, 30.0))
            continue
        return "answer", resp


async def _try_runpod(row, attempt_no: int, token: str, rung: str, now: float, tick: _Tick, s: Settings):
    """'placed' | 'stop' (do not try another rung) | 'none' (nothing placed here)."""
    cloud = RUNG_CLOUD[rung]
    catalog = await _catalog(cloud, now)
    if catalog is None:
        return "none"
    gdpr = None
    if rung == RUNG_SECURE:
        gdpr = await _gdpr_dcs(now)
        if gdpr is None:
            return "none"
    tid = int(row["id"])
    for gpu in s.rung_gpus(rung):
        if tick.creates_left <= 0:
            return "none"
        entry = catalog.get(gpu["id"])
        if not isinstance(entry, dict) or entry.get("availability") in (None, "NONE"):
            continue
        dcs = [d.get("id") for d in entry.get("dataCenters") or []
               if isinstance(d, dict) and d.get("id") and d.get("availability") not in (None, "NONE")]
        if rung == RUNG_COMMUNITY:
            dcs = [d for d in dcs if not _dc_skipped(d, now)]
        dcs = [d for d in dcs if STATE.create_400.get((cloud, gpu["id"], d), 0) <= now]
        if gdpr is not None:
            dcs = [d for d in dcs if d in gdpr]
        if not dcs:
            continue
        try:
            await asyncio.to_thread(write_attempt_meta, tid, attempt_no, {"allowed_dcs": dcs}, ("placing",))
        except Exception as exc:  # noqa: BLE001 - FR-8: no create without the persisted set
            logger.warning("allowed_dcs write failed for training %s: %s", tid, type(exc).__name__)
            continue
        body = pod_body(row, attempt_no, token, cloud, gpu, dcs, s)
        kind, resp = await _create_one(body, tick)
        if kind == "budget":
            return "none"
        if kind == "next":
            tick.statuses.append(resp.status if resp is not None else 0)
            continue
        status = resp.status
        tick.statuses.append(status)
        if status in (200, 201) and isinstance(resp.body, dict) and resp.body.get("id"):
            return await _attach_runpod(row, attempt_no, rung, gpu, resp.body, now)
        if status == 400:
            for d in dcs:
                STATE.create_400[(cloud, gpu["id"], d)] = now + CREATE_400_CACHE_S
            tick.last_400_detail = resp.detail
            continue
        if status == 403:
            continue
        if status == 402:
            STATE.runpod_402_until = now + NO_BALANCE_BACKOFF_S
            tick.runpod_off = True
            logger.warning("RunPod answered 402 (balance): no RunPod placement for %d s", NO_BALANCE_BACKOFF_S)
            return "none"
        # 422, 404, 413 and every other 4xx: a bug on our side (TR-8 n7).
        logger.error("RunPod create answered %s: %s", status, resp.detail)
        tick.runpod_off = True
        return "none"
    return "none"


async def _attach_runpod(row, attempt_no: int, rung: str, gpu: dict, pod: dict, now: float) -> str:
    tid = int(row["id"])
    pod_id = str(pod["id"])
    try:
        await arpc("attach_training_attempt", {
            "p_training_id": tid, "p_attempt": int(attempt_no), "p_backend": rung, "p_cloud_ref": pod_id,
        })
    except Exception as exc:  # P0049 or anything else: never leave a pod running unattached
        logger.warning("attach of pod %s to training %s failed (%s): deleting it", pod_id, tid,
                       "conflict" if isinstance(exc, RpcConflict) else type(exc).__name__)
        await delete_pod(pod_id)
        return "stop"
    STATE.pod_count += 1
    meta = {"gpu_type": (pod.get("gpu") or {}).get("id") if isinstance(pod.get("gpu"), dict) else None}
    meta["gpu_type"] = meta["gpu_type"] or gpu["id"]
    if pod.get("dataCenterId"):
        meta["data_center"] = pod.get("dataCenterId")
    if pod.get("cudaVersion"):
        meta["cuda_version"] = str(pod.get("cudaVersion"))[:32]
    if isinstance(pod.get("cost"), (int, float)) and not isinstance(pod.get("cost"), bool):
        meta["cost_per_hr"] = float(pod["cost"])
    try:
        await asyncio.to_thread(write_attempt_meta, tid, attempt_no, meta)
    except Exception as exc:  # noqa: BLE001 - metadata only
        logger.warning("attempt metadata write failed: %s", type(exc).__name__)
    logger.info("training %s attempt %s placed on %s (%s, pod %s)", tid, attempt_no, rung, meta["gpu_type"], pod_id)
    if pod.get("dataCenterId"):
        attempt = await asyncio.to_thread(read_attempt, tid, attempt_no)
        if attempt:
            await apply_guard(row, attempt, pod, now, None)
    return "placed"


async def _try_modal(row, attempt_no: int, token: str, s: Settings) -> str:
    from app.services import modal_client

    tid = int(row["id"])
    try:
        call_id = await modal_client.start_managed_training_job(
            training_id=tid, attempt=int(attempt_no), worker_token=str(token), api_url=s.worker_api_url,
            supabase_url=s.worker_supabase_url, supabase_anon_key=s.anon_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Modal spawn for training %s failed: %s", tid, type(exc).__name__)
        return "none"
    try:
        await arpc("attach_training_attempt", {
            "p_training_id": tid, "p_attempt": int(attempt_no), "p_backend": RUNG_MODAL, "p_cloud_ref": call_id,
        })
    except Exception as exc:
        logger.warning("attach of Modal call to training %s failed (%s): cancelling it", tid,
                       "conflict" if isinstance(exc, RpcConflict) else type(exc).__name__)
        await cancel_modal(call_id)
        return "stop"
    logger.info("training %s attempt %s placed on Modal", tid, attempt_no)
    return "placed"


async def _budget_finish(row, now: float, s: Settings) -> bool:
    """§4.2 step 2. True when the row was finished (or someone else changed it)."""
    n = int(row.get("current_attempt") or 0)
    msg = None
    if n >= MAX_ATTEMPTS:
        msg = T.S4
    elif int(row.get("train_seconds_used") or 0) >= s.cap_s - START_TIMEOUT_GRACE_S:
        msg = T.s5(s.cap_s)
    elif now - (ts(row.get("requested_at")) or now) >= WALL_CAP_S - START_TIMEOUT_GRACE_S:
        msg = T.S6
    else:
        unplaced = ts(row.get("unplaced_since"))
        if unplaced is not None and now - unplaced >= PLACEMENT_GIVEUP_S:
            msg = T.S18
    if msg is None:
        return False
    await fail_training(row, n, msg)
    return True


async def dispatch_tick(settings: Settings | None = None) -> dict:
    """One dispatcher round. Returns counters (tests)."""
    s = settings or SETTINGS
    now = _now()
    out = {"placed": 0, "released": 0, "finished": 0, "marked": 0, "claimed": 0}
    try:
        rows = await asyncio.to_thread(read_rows, ["queued"])
        ids = [r["id"] for r in rows]
        attempts = await asyncio.to_thread(read_attempts, ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dispatcher: database read failed: %s", type(exc).__name__)
        return out
    by_row = _group(attempts)
    current = {(a["training_id"], a["attempt_no"]): a for a in attempts}
    needing = []
    for row in rows:
        n = int(row.get("current_attempt") or 0)
        cur = current.get((row["id"], n))
        if n == 0 or (cur is not None and cur.get("state") == "lost"):
            needing.append(row)
    if not needing:
        return out
    try:
        ready = _group(await asyncio.to_thread(read_checkpoints, [r["id"] for r in needing], ["ready"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("dispatcher: checkpoint read failed: %s", type(exc).__name__)
        return out
    blocked = placement_blocked(s)
    tick = _Tick()
    claimed_rows = 0
    for row in needing:
        tid = row["id"]
        n = int(row.get("current_attempt") or 0)
        # 1. Finished?
        final = final_ready(ready.get(tid, []), total_steps_of(row))
        if final is not None and current.get((tid, n)) is not None:
            if await end_attempt_succeeded(row, current[(tid, n)], now):
                out["finished"] += 1
            continue
        # 2. Budgets (every tick, also while placement is blocked: FR-3, TR-4).
        if await _budget_finish(row, now, s):
            out["finished"] += 1
            continue
        if blocked:
            await asyncio.to_thread(mark_unplaced, row, now)
            out["marked"] += 1
            continue
        if claimed_rows >= MAX_ROWS_PER_TICK or tick.creates_left <= 0:
            continue  # TR-8 n6: neither claimed nor marked this tick
        rungs = rungs_for(row, by_row.get(tid, []), now, tick, s)
        if not rungs:
            await asyncio.to_thread(mark_unplaced, row, now)
            out["marked"] += 1
            continue
        # 4. Claim.
        try:
            data = await arpc("claim_training_attempt", {"p_training_id": tid, "p_expected_attempt": n})
        except RpcConflict:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("dispatcher: claim failed for training %s: %s", tid, type(exc).__name__)
            continue
        claimed_rows += 1
        out["claimed"] += 1
        claim = (data or [{}])[0] if isinstance(data, list) else (data or {})
        attempt_no = int(claim.get("o_attempt_no") or n + 1)
        token = claim.get("o_worker_token")
        result = "none"
        for rung in rungs:
            if rung in RUNPOD_RUNGS:
                if tick.runpod_off:
                    continue
                result = await _try_runpod(row, attempt_no, token, rung, now, tick, s)
            else:
                result = await _try_modal(row, attempt_no, token, s)
            if result in ("placed", "stop"):
                break
        if result == "placed":
            out["placed"] += 1
            continue
        if result == "stop":
            continue
        # 7. Nothing placed: release (costs no attempt, starts the 30-min clock).
        try:
            await arpc("release_training_attempt", {"p_training_id": tid, "p_attempt": attempt_no, "p_note": None})
            out["released"] += 1
        except RpcConflict:
            pass
        except Exception as exc:  # noqa: BLE001 - the sweep's placing_stale rule releases it later
            logger.warning("dispatcher: release failed for training %s: %s", tid, type(exc).__name__)
    if tick.statuses and all(st == 400 for st in tick.statuses):
        logger.error("RunPod answered 400 to every create this tick (the request is wrong): %s",
                     tick.last_400_detail)
    if tick.statuses and all(st == 403 for st in tick.statuses) and not STATE.all_403_logged:
        STATE.all_403_logged = True
        logger.error("RunPod answered 403 to every create this tick: the key probably lacks pods:write")
    return out


# ---------------------------------------------------------------------------
# Sweep (§4.3)
# ---------------------------------------------------------------------------


async def _list_pods_or_none():
    if not SETTINGS.runpod_api_key:
        return None
    try:
        pods = await runpod_client.list_pods()
    except (runpod_client.RunpodNetworkError, runpod_client.RunpodListError) as exc:
        logger.warning("sweep: RunPod pod list failed: %s", type(exc).__name__)
        return None
    prefix_re = re.compile(rf"{re.escape(SETTINGS.pod_prefix)}-(\d+)-a(\d+)")
    STATE.pod_count = sum(1 for p in pods if isinstance(p.get("name"), str) and prefix_re.fullmatch(p["name"])
                          and p.get("status") != "TERMINATED")
    return pods


async def _check_attempt(row, attempt, now: float, pods_by_id, pods_ok: bool, checkpoints, s: Settings) -> None:
    tid = int(row["id"])
    key = (tid, int(attempt["attempt_no"]))
    state = attempt.get("state")
    backend = attempt.get("backend")
    total = total_steps_of(row)
    t_fail = STATE.last_failed_round_at
    # Ready-final rule first (TR-2).
    if final_ready(checkpoints, total) is not None and state in ("starting", "running"):
        if await end_attempt_succeeded(row, attempt, now):
            await terminate_attempt(attempt)
        return
    # Caps (with WATCHDOG_GRACE_S, TR-2).
    if state in ("starting", "running"):
        used = int(row.get("train_seconds_used") or 0) + attempt_seconds(attempt, now)
        msg = None
        if used > s.cap_s + WATCHDOG_GRACE_S:
            msg = T.s5(s.cap_s)
        elif now - (ts(row.get("requested_at")) or now) > WALL_CAP_S + WATCHDOG_GRACE_S:
            msg = T.S6
        if msg is not None:
            await terminate_attempt(attempt)
            await fail_training(row, key[1], msg)
            return
    if state == "placing":
        created = ts(attempt.get("created_at")) or now
        if now - max(created, t_fail) > PLACING_STALE_S:
            try:
                await arpc("release_training_attempt", {"p_training_id": tid, "p_attempt": key[1], "p_note": None})
                logger.info("training %s attempt %s: placing attempt released (stale)", *key)
            except RpcConflict:
                pass
        return
    # RunPod: pod gone (two rounds ≥ POD_GONE_CONFIRM_S apart) and the guard.
    if backend in RUNPOD_RUNGS and pods_ok and attempt.get("cloud_ref"):
        pod = pods_by_id.get(attempt["cloud_ref"])
        if pod is None or pod.get("status") in runpod_client.GONE_STATUSES:
            first = STATE.gone_first.get(key)
            if first is None:
                STATE.gone_first[key] = now
            elif now - first >= POD_GONE_CONFIRM_S:
                STATE.gone_first.pop(key, None)
                await on_loss(row, attempt, "pod_gone", now, checkpoints)
                return
        else:
            STATE.gone_first.pop(key, None)
            if await apply_guard(row, attempt, pod, now, checkpoints):
                return
    # Modal: the call ended.
    if backend == RUNG_MODAL and attempt.get("cloud_ref") and state in ("starting", "running"):
        from app.services import modal_client

        status = await modal_client.get_job_status(attempt["cloud_ref"])
        if status in ("FAILED", "CANCELLED", "TIMED_OUT", "COMPLETED"):
            await on_loss(row, attempt, "modal_ended", now, checkpoints)
            return
    if state == "starting":
        started = ts(attempt.get("started_at")) or now
        if now - max(started, t_fail) > s.start_deadline_s:
            await on_loss(row, attempt, "start_timeout", now, checkpoints, skip_community_dc=True)
        return
    if state == "running":
        t0 = max(ts(row.get("last_progress_at")) or 0.0, t_fail)
        if now - t0 > HEARTBEAT_LOST_S:
            STATE.heartbeat_miss[key] = STATE.heartbeat_miss.get(key, 0) + 1
            if STATE.heartbeat_miss[key] >= 2:
                STATE.heartbeat_miss.pop(key, None)
                await on_loss(row, attempt, "heartbeat_lost", now, checkpoints)
                return
        else:
            STATE.heartbeat_miss.pop(key, None)
        advanced = ts(row.get("step_advanced_at")) or (ts(attempt.get("first_heartbeat_at")) or now)
        if now - max(advanced, t_fail) > STALL_S:
            await on_loss(row, attempt, "stalled", now, checkpoints)


async def sweep_tick(settings: Settings | None = None) -> dict:
    s = settings or SETTINGS
    now = _now()
    out = {"rows": 0}
    try:
        rows = await asyncio.to_thread(read_rows, ["queued", "running"])
        ids = [r["id"] for r in rows]
        attempts = await asyncio.to_thread(read_attempts, ids)
        ckpts = await asyncio.to_thread(read_checkpoints, ids, ["ready"])
    except Exception as exc:  # noqa: BLE001 - SR-4: skip the round, remember when
        STATE.last_failed_round_at = now
        logger.warning("sweep: database read failed, round skipped: %s", type(exc).__name__)
        return out
    pods = await _list_pods_or_none()
    pods_ok = pods is not None
    pods_by_id = {p.get("id"): p for p in pods or []}
    current = {(a["training_id"], a["attempt_no"]): a for a in attempts}
    ready = _group(ckpts)
    from app.services import hf_publish

    for row in rows:
        tid = row["id"]
        out["rows"] += 1
        n = int(row.get("current_attempt") or 0)
        attempt = current.get((tid, n))
        if attempt is None:
            continue
        try:
            if row.get("status") == "running" and attempt.get("state") == "ended" \
                    and attempt.get("end_reason") == "succeeded":
                await hf_publish.recover_publish(row, attempt, now)
                continue
            if attempt.get("state") in ACTIVE_STATES:
                await _check_attempt(row, attempt, now, pods_by_id, pods_ok, ready.get(tid, []), s)
        except Exception as exc:  # noqa: BLE001 - one row never stops the round
            logger.warning("sweep: training %s failed: %s", tid, type(exc).__name__)
    for call_id in list(STATE.modal_cancel_retry):
        await cancel_modal(call_id)
    try:
        await hf_publish.schedule_due_mirrors(rows, attempts, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sweep: mirror scheduling failed: %s", type(exc).__name__)
    try:
        await janitor_tick(now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sweep: janitor failed: %s", type(exc).__name__)
    return out


# ---------------------------------------------------------------------------
# Janitor + retention (§4.3, §4.5, TR-3)
# ---------------------------------------------------------------------------


def _checkpoint_keys(c) -> list:
    from app.services import r2_client

    manifest = c.get("manifest") if isinstance(c.get("manifest"), dict) else {}
    return [r2_client.ckpt_key(c["training_id"], c["attempt_no"], c["step"], rel) for rel in manifest]


def mirror_claim_live(c, now: float) -> bool:
    claimed = ts(c.get("mirror_claimed_at"))
    return claimed is not None and now - claimed < MIRROR_LEASE_S


def delete_checkpoint_objects(c, expect_state: str) -> bool:
    """Delete a checkpoint's objects, then mark it `deleted` (filtered on its state)."""
    from app.services import r2_client

    r2_client.delete_keys(_checkpoint_keys(c))
    (
        _sb().table("training_checkpoints").update({"state": "deleted"})
        .eq("training_id", c["training_id"]).eq("attempt_no", c["attempt_no"]).eq("step", c["step"])
        .eq("state", expect_state).execute()
    )
    return True


def apply_retention(training_id: int, now: float | None = None) -> int:
    """After every `ready` (§4.5): keep the two highest ready steps plus every
    ready checkpoint whose mirror is pending or claimed; delete the rest."""
    now = now or _now()
    row = read_row(training_id)
    if row is None:
        return 0
    total = total_steps_of(row)
    every = mirror_every_of(row)
    ready = read_checkpoints([training_id], ["ready"])
    keep = {(c["attempt_no"], c["step"]) for c in sorted(ready, key=lambda c: (c["step"], c["attempt_no"]),
                                                          reverse=True)[:2]}
    deleted = 0
    for c in ready:
        if (c["attempt_no"], c["step"]) in keep:
            continue
        pending = (c["step"] % every == 0 and c["step"] < total and not c.get("mirrored_at")
                   and int(c.get("mirror_tries") or 0) < 3)
        if pending or mirror_claim_live(c, now):
            continue
        try:
            delete_checkpoint_objects(c, "ready")
            deleted += 1
        except Exception as exc:  # noqa: BLE001 - the janitor finishes it later
            logger.warning("retention delete failed for training %s step %s: %s", training_id, c["step"],
                           type(exc).__name__)
    return deleted


async def janitor_tick(now: float) -> None:
    """Terminal managed rows: delete every checkpoint not `deleted` (refused ones
    included) and the log keys; a live mirror claim waits; a failed row's log
    is kept while it is still being uploaded (TR-3)."""
    from app.services import hf_publish, r2_client

    since = _iso(now - JANITOR_WINDOW_S)

    def _terminal():
        return (
            _sb().table("trainings").select("id, status, log_url, terminated_at, current_attempt, model_name")
            .eq("managed", True).in_("status", list(TERMINAL_STATUSES)).gte("terminated_at", since)
            .execute()
        ).data or []

    rows = [r for r in await asyncio.to_thread(_terminal) if r["id"] not in STATE.janitor_done]
    if not rows:
        return
    ckpts = _group(await asyncio.to_thread(read_checkpoints, [r["id"] for r in rows],
                                           ["uploading", "ready", "refused"]))
    for row in rows:
        tid = row["id"]
        done = True
        for c in ckpts.get(tid, []):
            if mirror_claim_live(c, now):
                done = False
                continue
            try:
                await asyncio.to_thread(delete_checkpoint_objects, c, c["state"])
            except Exception as exc:  # noqa: BLE001
                done = False
                logger.warning("janitor: training %s step %s: %s", tid, c["step"], type(exc).__name__)
        n = int(row.get("current_attempt") or 0)
        log_url = row.get("log_url")
        terminated = ts(row.get("terminated_at")) or now
        if not log_url and now - terminated < PUBLISH_RETRY_S:
            # TR-3: a failed (or canceled) row's log goes to its model repo first.
            if n > 0 and await asyncio.to_thread(r2_client.exists, r2_client.log_key(tid, n)):
                hf_publish.schedule_failed_log(row)
                done = False
        if done:
            keys = [r2_client.log_key(tid, a) for a in range(1, n + 1)]
            try:
                if keys:
                    await asyncio.to_thread(r2_client.delete_keys, keys)
                STATE.janitor_done.add(tid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("janitor: log delete for training %s: %s", tid, type(exc).__name__)


# ---------------------------------------------------------------------------
# Reaper (§4.3, SR-8, FR-15)
# ---------------------------------------------------------------------------


def _pod_env(pod) -> dict:
    env = pod.get("env")
    if isinstance(env, dict):
        return env
    if isinstance(env, list):
        out = {}
        for item in env:
            if isinstance(item, dict) and "key" in item:
                out[item["key"]] = item.get("value")
        return out
    return {}


async def reaper_tick(settings: Settings | None = None) -> dict:
    s = settings or SETTINGS
    now = _now()
    out = {"deleted": 0, "kept": 0}
    if not s.runpod_api_key:
        return out
    try:
        pods = await runpod_client.list_pods()
    except (runpod_client.RunpodNetworkError, runpod_client.RunpodListError) as exc:
        logger.warning("reaper: pod list failed: %s", type(exc).__name__)
        return out

    def _db():
        refs = (
            _sb().table("training_attempts").select("training_id, attempt_no, backend, cloud_ref, state")
            .in_("backend", list(RUNPOD_RUNGS)).not_.is_("cloud_ref", "null").execute()
        ).data or []
        active = (
            _sb().table("trainings").select("id, status, current_attempt")
            .eq("managed", True).in_("status", ["queued", "running"]).execute()
        ).data or []
        ids = [r["id"] for r in active]
        placing = read_attempts(ids)
        return refs, active, placing

    try:
        refs, active, attempts = await asyncio.to_thread(_db)
    except Exception as exc:  # noqa: BLE001 - never judge pods without the database
        logger.warning("reaper: database read failed, round skipped: %s", type(exc).__name__)
        return out
    by_ref = {}
    for a in refs:
        by_ref.setdefault(a["cloud_ref"], []).append(a)
    active_by_id = {r["id"]: r for r in active}
    attempt_by_key = {(a["training_id"], a["attempt_no"]): a for a in attempts}
    name_re = re.compile(rf"{re.escape(s.pod_prefix)}-(\d+)-a(\d+)")
    max_age = s.cap_s + s.start_deadline_s + REAPER_GRACE_S
    listed = set()
    for pod in pods:
        pod_id = pod.get("id")
        if not pod_id:
            continue
        listed.add(pod_id)
        name = pod.get("name") if isinstance(pod.get("name"), str) else ""
        m = name_re.fullmatch(name)
        image = pod.get("image") if isinstance(pod.get("image"), str) else ""
        by_image = (image.startswith(TRAINER_IMAGE_PREFIX) and s.worker_api_url
                    and _pod_env(pod).get("EDUBOTICS_API_URL") == s.worker_api_url)
        if not (m or pod_id in by_ref or by_image or pod_id in STATE.delete_retry):
            continue  # another environment's pod on the same account: never touched
        created = ts(pod.get("createdAt"))
        age = now - created if created is not None else 0.0
        keep = False
        for a in by_ref.get(pod_id, []):
            row = active_by_id.get(a["training_id"])
            cur = attempt_by_key.get((a["training_id"], a["attempt_no"]))
            if (row and int(row.get("current_attempt") or 0) == a["attempt_no"] and cur
                    and cur.get("state") in ("starting", "running") and cur.get("cloud_ref") == pod_id
                    and age < max_age):
                keep = True
        if not keep and m:
            tid, att = int(m.group(1)), int(m.group(2))
            row = active_by_id.get(tid)
            cur = attempt_by_key.get((tid, att))
            if (row and int(row.get("current_attempt") or 0) == att and cur and cur.get("state") == "placing"
                    and age < PLACING_STALE_S):
                keep = True
        if keep:
            out["kept"] += 1
            continue
        if pod.get("status") == "TERMINATED":
            STATE.delete_retry.pop(pod_id, None)
            continue
        if pod_id in STATE.delete_retry:
            STATE.delete_retry[pod_id] += 1
            if STATE.delete_retry[pod_id] > REAPER_SURVIVOR_ROUNDS:
                logger.error("reaper: pod %s survives deletion (%d rounds); the RunPod spend limit is the backstop",
                             pod_id, STATE.delete_retry[pod_id])
        if await delete_pod(pod_id, locked=bool(pod.get("locked"))):
            out["deleted"] += 1
            logger.info("reaper: deleted pod %s (%s)", pod_id, name or image)
    for pod_id in list(STATE.delete_retry):
        if pod_id not in listed:
            STATE.delete_retry.pop(pod_id, None)
    return out


# ---------------------------------------------------------------------------
# HF write check (§3.8, SR-7, FR-3) and the RunPod boot probe
# ---------------------------------------------------------------------------


def _hf_whoami_body(token: str) -> dict:
    """GET /api/whoami-v2, bounded exactly like hf_credentials.validate_with_hub
    (the httpx client's timeout plus a HF_CHECK_TIMEOUT_S join). Returns the
    parsed body; raises HfCredentialError rejected | unreachable | bad_reply."""
    from app.services import hf_credentials

    result: dict = {}

    def _ask():
        try:
            import httpx

            with httpx.Client(timeout=httpx.Timeout(HF_CHECK_TIMEOUT_S), follow_redirects=False) as client:
                response = client.get(hf_credentials._hub_endpoint() + hf_credentials.WHOAMI_PATH,
                                      headers={"Authorization": f"Bearer {token}"})
            result["status"] = response.status_code
            if response.status_code == 200:
                try:
                    result["info"] = response.json()
                except ValueError:
                    result["info"] = None
        except Exception as exc:  # noqa: BLE001 - classified below
            result["error"] = exc

    worker = threading.Thread(target=_ask, name="hf-whoami-org", daemon=True)
    worker.start()
    worker.join(HF_CHECK_TIMEOUT_S)
    if worker.is_alive():
        raise hf_credentials.HfCredentialError("unreachable")
    if "error" in result:
        raise hf_credentials.HfCredentialError("unreachable")
    status = result.get("status")
    if status != 200:
        raise hf_credentials.HfCredentialError("rejected" if status in (401, 403) else "unreachable")
    info = result.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("name"), str) or not info.get("name"):
        raise hf_credentials.HfCredentialError("bad_reply")
    return info


def org_repo_write(info, org: str = HF_ORG):
    """Can this token write repositories of ``org``? True / False / None (§3.8)."""
    from app.services import hf_credentials

    if not isinstance(info, dict):
        return None
    auth = info.get("auth")
    access = auth.get("accessToken") if isinstance(auth, dict) else None
    if not isinstance(access, dict):
        return None
    role = access.get("role")
    org_l = org.lower()
    if role in ("write", "god"):
        orgs = info.get("orgs")
        if not isinstance(orgs, list):
            return None
        for o in orgs:
            if isinstance(o, dict) and isinstance(o.get("name"), str) and o["name"].lower() == org_l:
                return o.get("roleInOrg") in ("admin", "write", "contributor")
        return False
    if role == "read":
        return False
    if role == hf_credentials.FINE_GRAINED_ROLE:
        block = access.get("fineGrained")
        if not isinstance(block, dict) or not isinstance(block.get("scoped"), list):
            return None
        for entry in block["scoped"]:
            if not isinstance(entry, dict):
                return None
            entity = entry.get("entity")
            permissions = entry.get("permissions")
            if not isinstance(entity, dict) or not isinstance(permissions, list):
                return None
            if entity.get("type") != "org":
                continue
            name = entity.get("name")
            if isinstance(name, str) and name.lower() == org_l:
                if any(hf_credentials.is_repo_write_permission(p) for p in permissions):
                    return True
        return False
    return None


async def hf_check_tick(settings: Settings | None = None):
    from app.services import hf_credentials

    s = settings or SETTINGS
    if not s.hf_token:
        STATE.hf_check = "no_token"
        logger.error("managed training: HF_TOKEN is not set; placement is blocked")
        return STATE.hf_check
    try:
        info = await asyncio.to_thread(_hf_whoami_body, s.hf_token)
    except hf_credentials.HfCredentialError as exc:
        STATE.hf_check = exc.code
        if exc.code == "rejected":
            logger.error("managed training: the Hub rejected HF_TOKEN; placement is blocked")
        else:
            logger.warning("managed training: HF write check %s; placement proceeds", exc.code)
        return STATE.hf_check
    verdict = org_repo_write(info, HF_ORG)
    STATE.hf_check = verdict
    if verdict is False:
        logger.error("managed training: HF_TOKEN cannot write to %s; placement is blocked", HF_ORG)
    elif verdict is None:
        logger.warning("managed training: HF_TOKEN's write access to %s is unknown; placement proceeds", HF_ORG)
    return verdict


async def runpod_probe_tick(settings: Settings | None = None):
    s = settings or SETTINGS
    if not (s.runpod_api_key and s.has_runpod_rung):
        return None
    try:
        resp = await runpod_client.request("GET", "/v2/pods", params={"limit": 1})
    except runpod_client.RunpodNetworkError:
        return STATE.runpod_probe
    if resp.status == 200:
        STATE.runpod_probe = True
    elif resp.status in (401, 403):
        STATE.runpod_probe = False
        logger.error("RunPod boot probe answered %s: no RunPod placement until a later probe succeeds", resp.status)
    return STATE.runpod_probe


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

_KICK: asyncio.Event | None = None
_LOOP: asyncio.AbstractEventLoop | None = None
_TASKS: set = set()


def kick() -> None:
    """Wake the dispatcher now (safe from any thread)."""
    loop, event = _LOOP, _KICK
    if loop is None or event is None:
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:  # the loop is closed (shutdown)
        pass


async def _forever(name: str, fn, interval: float, *, kick_event: asyncio.Event | None = None,
                   initial_delay: float = 0.0) -> None:
    if initial_delay:
        await asyncio.sleep(initial_delay)
    while True:
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001 - a loop never dies
            logger.error("%s: round failed: %s", name, type(exc).__name__)
        if kick_event is not None:
            try:
                await asyncio.wait_for(kick_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            kick_event.clear()
        else:
            await asyncio.sleep(interval)


async def _recheck_until_good(name: str, fn, good) -> None:
    while True:
        try:
            result = await fn()
            if good(result):
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s failed: %s", name, type(exc).__name__)
        await asyncio.sleep(RECHECK_S)


def _keep(task) -> None:
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def start_loops() -> bool:
    """Start the managed loops once per process."""
    global _KICK, _LOOP
    if STATE.loops_started:
        return False
    STATE.loops_started = True
    if SETTINGS.missing:
        # §3.8: the loops run (a legacy deploy still drives the managed rows it left
        # behind), but whatever needs these variables cannot work.
        logger.error("managed training loops start with required variables missing: %s",
                     ", ".join(SETTINGS.missing))
    _LOOP = asyncio.get_running_loop()
    _KICK = asyncio.Event()
    _keep(asyncio.create_task(_recheck_until_good("HF write check", hf_check_tick, lambda r: r is True)))
    _keep(asyncio.create_task(_recheck_until_good("RunPod probe", runpod_probe_tick,
                                                  lambda r: r is True or r is None and not SETTINGS.has_runpod_rung)))
    _keep(asyncio.create_task(_forever("dispatcher", dispatch_tick, DISPATCH_INTERVAL_S, kick_event=_KICK,
                                       initial_delay=5)))
    _keep(asyncio.create_task(_forever("managed sweep", sweep_tick, SWEEP_INTERVAL_S, initial_delay=15)))
    _keep(asyncio.create_task(_forever("reaper", reaper_tick, REAPER_INTERVAL_S, initial_delay=20)))
    logger.info("managed training loops started (boot %s, dispatch=%s)", BOOT_ID[:8], SETTINGS.dispatch)
    return True


def _has_nonterminal_managed() -> bool:
    rows = (
        _sb().table("trainings").select("id").eq("managed", True)
        .in_("status", ["queued", "running", "cancel_requested"]).limit(1).execute()
    ).data or []
    return bool(rows)


async def managed_supervisor() -> None:
    """§3.8 „When the managed loops run": under TRAINING_DISPATCH=managed, or while
    any managed row is non-terminal (checked again every 10 min)."""
    while not STATE.loops_started:
        try:
            if SETTINGS.managed or await asyncio.to_thread(_has_nonterminal_managed):
                start_loops()
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("managed supervisor: database check failed: %s", type(exc).__name__)
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(RECHECK_S)
