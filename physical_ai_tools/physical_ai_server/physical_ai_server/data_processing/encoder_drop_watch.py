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

"""Frame-drop detection through LeRobot's PUBLIC warning (spec §6.3, owner Q2 A).

LeRobot 0.5.1's ``StreamingVideoEncoder.feed_frame`` drops a frame when the
encoder queue is full and logs, on logger ``lerobot.datasets.video_utils``, the
first drop of an episode and then every 10th::

    Encoder queue full for <key>, dropped <N> frame(s). Consider using …

and ``finish_episode`` logs ``Episode finished with <N> dropped frame(s) for
<key>.`` (``N`` counts per episode; ``start_episode`` resets it). One
``logging.Handler`` parses those lines, so the recorder never reads the
encoder's private counter. Detection is exact (the first drop is always
logged); the count is a lower bound until the finish line arrives, which is why
the German sentence names no number. The thin Dockerfile asserts the message
text is still in ``feed_frame`` (the tripwire for a LeRobot bump).

Use: ``watch = install()`` once (idempotent), ``watch.arm()`` at the first frame
of every take, ``watch.dropped() > 0`` before the take is committed.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Dict

LOGGER_NAME = 'lerobot.datasets.video_utils'

_QUEUE_FULL_RE = re.compile(r'Encoder queue full for ([^,\s]+), dropped (\d+) frame')
_FINISHED_RE = re.compile(r'Episode finished with (\d+) dropped frame\(s\) for (\S+?)\.?$')

_INSTALL_LOCK = threading.Lock()


class EncoderDropWatch(logging.Handler):
    """Counts the frames LeRobot reports dropped since the last ``arm()``."""

    # recognised across module reloads (the deps-free loaders load by path)
    edubotics_encoder_drop_watch = True

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._counts: Dict[str, int] = {}

    def emit(self, record: logging.LogRecord) -> None:
        # Handler.handle() holds self.lock around emit().
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record must never break logging
            return
        m = _QUEUE_FULL_RE.search(text)
        if m:
            key, n = m.group(1), int(m.group(2))
        else:
            m = _FINISHED_RE.search(text)
            if not m:
                return
            n, key = int(m.group(1)), m.group(2)
        self._counts[key] = max(self._counts.get(key, 0), n)

    def arm(self) -> None:
        """Forget every count: a new take starts."""
        with self.lock:
            self._counts = {}

    def dropped(self) -> int:
        """Frames reported dropped since ``arm()`` (a lower bound; > 0 is exact)."""
        with self.lock:
            return sum(self._counts.values())

    def dropped_by_key(self) -> Dict[str, int]:
        with self.lock:
            return dict(self._counts)


def install(logger_name: str = LOGGER_NAME) -> EncoderDropWatch:
    """Attach the watch to LeRobot's logger once and return it.

    The logger's effective level is lowered to WARNING when it is above it, so
    the warning is created at all."""
    logger = logging.getLogger(logger_name)
    with _INSTALL_LOCK:
        for handler in logger.handlers:
            if getattr(handler, 'edubotics_encoder_drop_watch', False):
                return handler
        watch = EncoderDropWatch()
        logger.addHandler(watch)
        if logger.getEffectiveLevel() > logging.WARNING:
            logger.setLevel(logging.WARNING)
        return watch
