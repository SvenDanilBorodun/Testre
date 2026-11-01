#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The station session state machine of plan v6.13 §5.2, as a pure class.

Driven by events (``enter``, ``leave``, ``keepalive``, ``status``, ``tick``) and
a monotonic clock passed in. No rclpy, no locks: the node holds
``_mode_lock`` → ``_station_lock`` around every call (each is O(1)).

States: OFF (not a station rig), IDLE, ENTERING, ACTIVE. The session record is
``{session_id, owner_client_id, generation, camera_ok, robot_ok, source_ok,
last_keepalive_mono}``; ``revocations`` is the camera-check epoch.
"""

import collections
import uuid

from physical_ai_server.station import texts_de as T

KEEPALIVE_LOST_S = 10.0       # owner keepalive missing → session ends
TAKEOVER_AFTER_S = 4.0        # another tab may take over a silent owner
NATIVE_TIMEOUT_S = 30.0       # no healthy 1080p stream after enter
FRAME_GAP_S = 2.0             # station frame age > 2 s → source and camera revoked
ENDED_MEMORY = 16             # end reasons kept per ended session_id
HEALTH_MIN_FRAMES = 10
HEALTH_MIN_SPAN_S = 1.0
HEALTH_MIN_RATE = 10.0


def stream_healthy_since(arrivals, since, now, size=T.STATION_IMAGE_SIZE):
    """§5.2 "stream healthy": ≥ 10 station frames of exactly 1920×1080 arrived
    after ``since``, spanning ≥ 1 s, with an arrival rate over the last 1 s of
    ≥ 10 fps. ``arrivals`` is an iterable of ``(arrival_mono, (w, h) | None)``."""
    good = [t for t, wh in arrivals if t > since and wh == tuple(size)]
    if len(good) < HEALTH_MIN_FRAMES:
        return False
    if good[-1] - good[0] < HEALTH_MIN_SPAN_S:
        return False
    recent = [t for t in good if t >= now - 1.0]
    return len(recent) >= HEALTH_MIN_RATE


Answer = collections.namedtuple('Answer', 'accepted reason_code message end_reason')


class SessionMachine:
    """One claim, observed by the server; nothing is recovered from disk."""

    def __init__(self, station_rig, new_session_id=None):
        self.station_rig = bool(station_rig)
        self.state = 'IDLE' if self.station_rig else 'OFF'
        self._new_id = new_session_id or (lambda: uuid.uuid4().hex)
        self.session_id = ''
        self.owner = ''
        self.generation = 0
        self.camera_ok = False
        self.robot_ok = False
        self.source_ok = False
        self.last_keepalive = 0.0
        self.entered_at = 0.0
        self.gap_since = None
        self.revocations = 0
        self._ended = collections.OrderedDict()

    # ── helpers ───────────────────────────────────────────────────────────────
    @property
    def holds_claim(self):
        return self.state in ('ENTERING', 'ACTIVE')

    def _revoke_camera(self):
        self.camera_ok = False
        self.revocations += 1

    def _revoke_verification(self):
        self.camera_ok = False
        self.robot_ok = False
        self.revocations += 1

    def _end(self, reason):
        if self.session_id:
            self._ended[self.session_id] = reason
            while len(self._ended) > ENDED_MEMORY:
                self._ended.popitem(last=False)
        self.state = 'IDLE'
        self.session_id = ''
        self.owner = ''
        self.source_ok = False
        self.gap_since = None
        self._revoke_verification()

    def is_owner(self, session_id, client_id):
        return (self.holds_claim and bool(session_id) and session_id == self.session_id
                and bool(client_id) and client_id == self.owner)

    def end_reason_for(self, session_id):
        return self._ended.get(session_id, '')

    def _ended_answer(self, session_id, client_id):
        """SESSION_ENDED (with the end reason kept for that session id), unless
        a session with ANOTHER owner now exists: then OWNER_MISMATCH."""
        if self.holds_claim and client_id != self.owner:
            return Answer(False, 'OWNER_MISMATCH', T.OWNER_MISMATCH_DE, '')
        reason = self.end_reason_for(session_id)
        message = T.END_REASON_DE.get(reason, T.SESSION_ENDED_DE)
        return Answer(False, 'SESSION_ENDED', message, reason)

    # ── commands ──────────────────────────────────────────────────────────────
    def enter(self, client_id, now, new_session_refusal=None):
        """§5.2 / §5.3. ``new_session_refusal`` is called ONLY when this enter
        would open a new session (IDLE → ENTERING) and returns None or
        ``(reason_code, message)``; resume and takeover never call it.
        Returns (Answer, event) with event in {'new', 'resumed', 'takeover', None}."""
        if not self.station_rig:
            return Answer(False, 'NOT_STATION_RIG', T.NOT_STATION_RIG_DE, ''), None
        if not client_id:
            return Answer(False, 'OWNER_MISMATCH', T.OWNER_MISMATCH_DE, ''), None
        if self.holds_claim:
            if client_id == self.owner:
                self.last_keepalive = now
                self._revoke_camera()
                return Answer(True, 'RESUMED', T.RESUMED_DE, ''), 'resumed'
            if now - self.last_keepalive > TAKEOVER_AFTER_S:
                self.owner = client_id
                self.session_id = self._new_id()
                self.generation += 1
                self.last_keepalive = now
                self._revoke_verification()
                if self.state == 'ENTERING':
                    self.entered_at = now
                return Answer(True, '', T.ENTERED_DE, ''), 'takeover'
            return Answer(False, 'BUSY_OTHER_TAB', T.BUSY_OTHER_TAB_DE, ''), None
        refusal = new_session_refusal() if new_session_refusal is not None else None
        if refusal:
            code, message = refusal
            return Answer(False, code, message, ''), None
        self.state = 'ENTERING'
        self.generation += 1
        self.session_id = self._new_id()
        self.owner = client_id
        self.camera_ok = self.robot_ok = self.source_ok = False
        self.last_keepalive = now
        self.entered_at = now
        self.gap_since = None
        return Answer(True, '', T.ENTERED_DE, ''), 'new'

    def keepalive(self, session_id, client_id, now):
        if self.is_owner(session_id, client_id):
            self.last_keepalive = now
            return Answer(True, '', T.KEEPALIVE_OK_DE, '')
        if self.holds_claim and session_id == self.session_id:
            return Answer(False, 'OWNER_MISMATCH', T.OWNER_MISMATCH_DE, '')
        return self._ended_answer(session_id, client_id)

    def leave(self, session_id, client_id, workflow_active):
        """Returns (Answer, ended: bool)."""
        if self.is_owner(session_id, client_id):
            if workflow_active:
                return Answer(False, 'WORKFLOW_ACTIVE', T.WORKFLOW_ACTIVE_DE, ''), False
            self._end('LEFT')
            return Answer(True, '', T.LEFT_DE, ''), True
        if self.holds_claim and session_id == self.session_id:
            return Answer(False, 'OWNER_MISMATCH', T.OWNER_MISMATCH_DE, ''), False
        return self._ended_answer(session_id, client_id), False

    def visible_session_id(self, client_id):
        """`status` returns the session_id only to the owner."""
        if self.holds_claim and client_id and client_id == self.owner:
            return self.session_id
        return ''

    # ── the 5 Hz timer ────────────────────────────────────────────────────────
    def tick(self, now, arrivals, frame_age, leader_active):
        """Returns the end reason when the session ended in this tick, else ''."""
        if not self.holds_claim:
            return ''
        if leader_active:
            self._end('LEADER_ACTIVE')
            return 'LEADER_ACTIVE'
        if now - self.last_keepalive > KEEPALIVE_LOST_S:
            self._end('KEEPALIVE_LOST')
            return 'KEEPALIVE_LOST'
        if self.state == 'ENTERING':
            if stream_healthy_since(arrivals, self.entered_at, now):
                self.state = 'ACTIVE'
                self.source_ok = True
                self.gap_since = None
            elif now - self.entered_at > NATIVE_TIMEOUT_S:
                self._end('NATIVE_TIMEOUT')
                return 'NATIVE_TIMEOUT'
            return ''
        # ACTIVE
        if self.source_ok:
            if frame_age is None or frame_age > FRAME_GAP_S:
                self.source_ok = False
                self.gap_since = now
                self._revoke_camera()
        elif stream_healthy_since(arrivals, self.gap_since or 0.0, now):
            self.source_ok = True
            self.gap_since = None
        return ''

    def digest_changed(self):
        """Any station YAML digest change revokes verification."""
        self._revoke_verification()

    # ── checks: applied only if nothing invalidated them meanwhile ─────────────
    def check_token(self, digest):
        return (self.generation, self.revocations, digest)

    def apply_camera(self, token, digest, ok, extra_revocations=0):
        """Set camera_ok only when generation, revocation counter (allowing
        ``extra_revocations`` of the caller's own) and digest are unchanged."""
        generation, revocations, started_digest = token
        if (self.state != 'ACTIVE' or self.generation != generation
                or self.revocations != revocations + extra_revocations
                or digest != started_digest):
            return False
        self.camera_ok = bool(ok) and self.source_ok
        return True

    def apply_robot(self, token, digest, ok):
        generation, revocations, started_digest = token
        if (self.state != 'ACTIVE' or self.generation != generation
                or self.revocations != revocations or digest != started_digest):
            return False
        self.robot_ok = bool(ok)
        return True

    def verified(self, configured):
        return bool(configured and self.state == 'ACTIVE' and self.source_ok
                    and self.camera_ok and self.robot_ok)
