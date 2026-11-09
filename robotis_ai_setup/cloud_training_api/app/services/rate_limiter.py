"""In-process sliding-window rate limiter for the Cloud API.

Single-tenant by design: the Cloud API runs ``uvicorn --workers 1``
(Railway default), so process-local state is correct. Scaling out would
require Redis-backed state or a Postgres advisory lock so only one worker
counts a given key.

Memory safety (audit fix): bucket keys are derived from attacker-
controlled inputs BEFORE authentication — a hashed bearer token for the
per-user buckets, the X-Forwarded-For client IP for the rest — both of
which are forgeable/spoofable. An unbounded key map would let a flood of
distinct forged tokens / spoofed IPs grow ``_buckets`` without limit and
OOM the single worker. Each bucket is therefore an LRU capped at
``_MAX_KEYS_PER_BUCKET`` distinct keys; evicting the least-recently-used
key can at worst reset an idle key's window — it never weakens limiting
for the actively-hot keys an attacker would be hammering.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from threading import Lock


class RateLimiter:
    # Per-bucket cap on distinct keys. There is one bucket per rate rule
    # (~12), so worst-case memory is ~12 × 8192 short deques — a few MB,
    # bounded regardless of forged-token / spoofed-IP volume.
    _MAX_KEYS_PER_BUCKET = 8192

    def __init__(self) -> None:
        # { bucket_name: OrderedDict{ key: deque[timestamps] } }
        # The inner OrderedDict gives O(1) LRU eviction via
        # popitem(last=False). The outer dict is bounded by the static
        # rule list, so it needs no cap.
        self._buckets: dict[str, "OrderedDict[str, deque]"] = {}
        self._lock = Lock()

    def check(self, bucket: str, key: str, limit: int, window_s: float) -> bool:
        now = time.monotonic()
        with self._lock:
            keys = self._buckets.get(bucket)
            if keys is None:
                keys = OrderedDict()
                self._buckets[bucket] = keys
            hist = keys.get(key)
            if hist is None:
                hist = deque()
                keys[key] = hist  # new key inserted as most-recently-used (tail)
            else:
                keys.move_to_end(key)  # touch → most-recently-used
            while hist and now - hist[0] > window_s:
                hist.popleft()
            allowed = len(hist) < limit
            if allowed:
                hist.append(now)
            # Hard LRU backstop: evict least-recently-used keys so the
            # map can't grow past the cap under a forged-key flood.
            while len(keys) > self._MAX_KEYS_PER_BUCKET:
                keys.popitem(last=False)
            return allowed


class FailureCounter:
    """Failed authentications per client address in a sliding window (the API's
    failed-authentication cap, app/edge.py::AuthFailureLimitMiddleware).

    ``record`` adds one failure and answers the count in the window; ``count``
    only reads. LRU-capped like ``RateLimiter`` (the keys are client addresses,
    which a flood can rotate), and each key keeps at most ``_MAX_PER_KEY``
    timestamps: once an address is over its cap its requests are refused
    before they can fail again, so the cap is never far exceeded.
    """

    _MAX_KEYS = 8192
    _MAX_PER_KEY = 1024

    def __init__(self) -> None:
        self._keys: "OrderedDict[str, deque]" = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def _prune(hist: deque, now: float, window_s: float) -> None:
        while hist and now - hist[0] > window_s:
            hist.popleft()

    def record(self, key: str, window_s: float) -> int:
        now = time.monotonic()
        with self._lock:
            hist = self._keys.get(key)
            if hist is None:
                hist = deque(maxlen=self._MAX_PER_KEY)
                self._keys[key] = hist
            else:
                self._keys.move_to_end(key)
            self._prune(hist, now, window_s)
            hist.append(now)
            while len(self._keys) > self._MAX_KEYS:
                self._keys.popitem(last=False)
            return len(hist)

    def count(self, key: str, window_s: float) -> int:
        now = time.monotonic()
        with self._lock:
            hist = self._keys.get(key)
            if hist is None:
                return 0
            self._prune(hist, now, window_s)
            if not hist:
                del self._keys[key]
                return 0
            return len(hist)
