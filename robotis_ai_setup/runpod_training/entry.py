"""Entry point of the RunPod trainer image (spec §4.8). Never prints a token or a URL query.

Reads the pod's environment (set by the cloud API's create body, plus RunPod's own
RUNPOD_POD_ID / RUNPOD_CPU_COUNT / RUNPOD_API_KEY), runs ONE managed attempt through
training_handler.run_managed, and then makes sure the pod stops costing money:

  * a watchdog at budget_s + WATCHDOG_GRACE_S (before `start` answers: the default
    training-time cap) does a best-effort self-stop and then os._exit(3);
  * after run_managed returns: sleep POST_RUN_SLEEP_S, best-effort self-stop
    (POST {RUNPOD_API_BASE}/v2/pods/{RUNPOD_POD_ID}/action {"action": "stop"}, 3 tries);
    then EXIT_AFTER_FAILED_SELF_STOP decides: False sleeps forever (an exiting main
    process might make RunPod restart the container, P1b measures it), True exits 0.

Either way the cloud API's reaper deletes the pod within a round.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

# Set to True before a release if P1b shows an exiting main process leaves the pod
# EXITED without a restart (spec §8 item 8, FR-20).
EXIT_AFTER_FAILED_SELF_STOP = False

WATCHDOG_GRACE_S = 1200
MANAGED_ACT_CAP_S_DEFAULT = 14400
POST_RUN_SLEEP_S = 120
SELF_STOP_TRIES = 3
SELF_STOP_TIMEOUT_S = 20
DEFAULT_RUNPOD_API_BASE = "https://api.runpod.io"
WATCHDOG_EXIT_CODE = 3

REQUIRED_ENV = (
    "EDUBOTICS_TRAINING_ID",
    "EDUBOTICS_ATTEMPT",
    "EDUBOTICS_WORKER_TOKEN",
    "EDUBOTICS_API_URL",
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
)

_sleep = time.sleep
_monotonic = time.monotonic


def log(msg: str) -> None:
    print(f"[edubotics-entry] {msg}", flush=True)


def cpu_budget(env=None) -> int:
    env = os.environ if env is None else env
    try:
        n = int(str(env.get("RUNPOD_CPU_COUNT", "")).strip())
        if n >= 1:
            return n
    except ValueError:
        pass
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def self_stop(env=None) -> bool:
    """Best effort: ask RunPod to stop this pod. True when RunPod answered 2xx."""
    env = os.environ if env is None else env
    pod_id = env.get("RUNPOD_POD_ID")
    key = env.get("RUNPOD_API_KEY")
    if not pod_id or not key:
        log("self-stop skipped: RUNPOD_POD_ID or RUNPOD_API_KEY is not set")
        return False
    base = (env.get("RUNPOD_API_BASE") or DEFAULT_RUNPOD_API_BASE).rstrip("/")
    url = f"{base}/v2/pods/{pod_id}/action"
    body = json.dumps({"action": "stop"}).encode("utf-8")
    for attempt in range(1, SELF_STOP_TRIES + 1):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=SELF_STOP_TIMEOUT_S) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        except (urllib.error.URLError, OSError) as e:
            status = type(e).__name__
        log(f"self-stop try {attempt}: {status}")
        if isinstance(status, int) and 200 <= status < 300:
            return True
        if attempt < SELF_STOP_TRIES:
            _sleep(5 * attempt)
    return False


class Watchdog:
    """Ends the process at its deadline, whatever run_managed is doing (§4.8)."""

    def __init__(self, seconds: float, on_fire=None):
        self._lock = threading.Lock()
        self._deadline = _monotonic() + seconds
        self._on_fire = on_fire or self._fire
        self._thread = threading.Thread(target=self._loop, name="entry-watchdog", daemon=True)

    def start(self) -> "Watchdog":
        self._thread.start()
        return self

    def rearm(self, budget_s: int) -> None:
        with self._lock:
            self._deadline = _monotonic() + int(budget_s) + WATCHDOG_GRACE_S
        log(f"watchdog armed for budget {int(budget_s)} s + {WATCHDOG_GRACE_S} s grace")

    def deadline(self) -> float:
        with self._lock:
            return self._deadline

    def _loop(self) -> None:
        while True:
            left = self.deadline() - _monotonic()
            if left <= 0:
                self._on_fire()
                return
            _sleep(min(left, 30.0))

    @staticmethod
    def _fire() -> None:
        log("watchdog deadline reached: self-stop, then exit")
        try:
            import training_handler
            proc = training_handler._managed_proc
            if proc is not None:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass
        self_stop()
        os._exit(WATCHDOG_EXIT_CODE)


def main(env=None) -> int:
    env = os.environ if env is None else env
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    watchdog = Watchdog(MANAGED_ACT_CAP_S_DEFAULT + WATCHDOG_GRACE_S).start()
    if missing:
        log(f"missing environment: {', '.join(missing)}")
    else:
        import training_handler
        try:
            result = training_handler.run_managed(
                env["EDUBOTICS_API_URL"], env["SUPABASE_URL"], env["SUPABASE_ANON_KEY"],
                int(env["EDUBOTICS_TRAINING_ID"]), int(env["EDUBOTICS_ATTEMPT"]),
                env["EDUBOTICS_WORKER_TOKEN"], cpu_budget=cpu_budget(env),
                on_budget=watchdog.rearm)
            log(f"run_managed returned {json.dumps(result, sort_keys=True)}")
        except Exception as e:  # noqa: BLE001  (SystemExit from SIGTERM passes through)
            log(f"run_managed raised {type(e).__name__}")
    _sleep(POST_RUN_SLEEP_S)
    stopped = self_stop(env)
    if not stopped and EXIT_AFTER_FAILED_SELF_STOP:
        return 0
    while True:        # RunPod's stop (SIGTERM) or the reaper's DELETE ends us
        _sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
