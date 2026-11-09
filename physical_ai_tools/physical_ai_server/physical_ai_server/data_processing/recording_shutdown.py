#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""A recording session survives the node's shutdown (launch plan step 8, 2026-11-10).

„Umgebung stoppen“ during a recording used to lose every episode the session had
saved: nothing finalized the dataset on the shutdown path, so LeRobot's data
parquet kept no footer and the buffered episode metadata was never written
(KNOWN-ISSUES M2, confirmed by running).

The signal path, as it runs in the shipped image:
  docker/compose stop → SIGTERM to PID 1 (s6-svscan) → s6 stage 3 brings the
  ``physical_ai_server`` longrun down with its down-signal, SIGINT since this
  round (the thin Dockerfile writes it) → ``ros2 launch`` shuts down in order:
  SIGINT to every child (stdin is /dev/null under s6, so the launch is
  non-interactive), SIGTERM after the node's ``sigterm_timeout``, SIGKILL after
  ``sigkill_timeout`` more → in the node, rclpy's handler shuts the context down
  (``main()`` set SIGINT to SIG_DFL first, so SIGINT raises no KeyboardInterrupt,
  exactly like SIGTERM) → ``executor.spin()`` returns → ``main()``'s ``finally``
  calls ``PhysicalAIServer._finalize_recording_on_shutdown`` FIRST → this module.

stdlib only: the node calls it from ``main()``'s ``finally`` and before the
sensor supervisor's ``os._exit``; ``.github/scripts/record_fsm_smoke.py`` drives
it in a child process through the real rclpy signal path; the deps-free tests
load it by file path.
"""

import sys
import threading
import time

# How long the node waits for the finalize before it goes on tearing down (the
# finalize thread is a daemon: a hang must not keep the process alive). Measured
# with the real LeRobot 0.5.1 writer (two 480x640 cameras, 30 fps, streaming
# h264, a 4-vCPU x86 host): discarding the running take 1.04 s, finalize() 8 ms,
# a save in flight 0.25-0.83 s growing with the video chunk (up to 200 MB). A
# Pi on an SD card remuxes that chunk several times slower, hence the margin.
# Bounded from outside, in this order (robotis_ai_setup/tests/
# test_recording_stop_budget.py holds them together): the launch file's
# sigterm_timeout + sigkill_timeout of the node (SIGKILL 40 s after the launch's
# SIGINT), compose's stop_grace_period of physical_ai_server (50 s), the GUI's
# and the Pi agent's stop timeouts (120 s).
SHUTDOWN_FINALIZE_WAIT_S = 30.0

# The poll of the bounded wait (a join in slices, so a KeyboardInterrupt that
# still reaches the waiting thread cannot cut the finalize short).
_JOIN_SLICE_S = 0.5

VERDICT_TIMEOUT = 'timeout'           # the finalize was still running at the budget
VERDICT_ERROR = 'error'               # it raised (the crash marker stays)
VERDICT_UNSUPPORTED = 'unsupported'   # a DataManager without end_for_shutdown


class _TolerantStream:
    """sys.stdout / sys.stderr for the rest of the process: a write or flush
    that fails (EPIPE once the launch that read the pipe is gone, a closed
    stream) is dropped instead of raised. Without it a fallback path (the s6
    finish script's SIGTERM to the process group after the launch exited) let
    one ``print(..., flush=True)`` on the finalize path raise BrokenPipeError
    and abandon the finalize half way."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        try:
            return self._stream.write(text)
        except (OSError, ValueError):
            return len(text) if isinstance(text, str) else 0

    def flush(self):
        try:
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def make_output_tolerant():
    """Wrap sys.stdout and sys.stderr once (idempotent)."""
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if stream is not None and not isinstance(stream, _TolerantStream):
            setattr(sys, name, _TolerantStream(stream))


def finalize_for_shutdown(data_manager, wait_s=SHUTDOWN_FINALIZE_WAIT_S, *, clock=time.monotonic):
    """Run ``data_manager.end_for_shutdown()`` on its own daemon thread and wait
    for it at most ``wait_s`` seconds.

    Returns ``{'verdict': …, 'seconds': float, 'error': str, 'saved': int}``:
    the verdict is the DataManager's own answer (``finalized``, ``failed``,
    ``already_final``, ``no_dataset``), or VERDICT_TIMEOUT / VERDICT_ERROR /
    VERDICT_UNSUPPORTED. Never raises. The thread is a daemon so a finalize that
    hangs (it waits for the recorder lock) cannot keep the process alive past
    the budget; a KeyboardInterrupt in the waiting thread is absorbed and the
    wait goes on, so it can never cut the finalize in half."""
    make_output_tolerant()
    end = getattr(data_manager, 'end_for_shutdown', None)
    if end is None:
        return {'verdict': VERDICT_UNSUPPORTED, 'seconds': 0.0, 'error': '', 'saved': 0}
    box = {}

    def _run():
        try:
            box['verdict'] = end()
        except BaseException as e:  # noqa: BLE001 — reported, never raised
            box['error'] = f'{type(e).__name__}: {e}'

    worker = threading.Thread(target=_run, name='recording-shutdown-finalize', daemon=True)
    started = clock()
    worker.start()
    deadline = started + max(0.0, float(wait_s))
    while worker.is_alive():
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            worker.join(min(remaining, _JOIN_SLICE_S))
        except KeyboardInterrupt:
            continue
    seconds = round(clock() - started, 3)
    if worker.is_alive():
        verdict = VERDICT_TIMEOUT
    elif 'error' in box:
        verdict = VERDICT_ERROR
    else:
        verdict = box.get('verdict')
    try:
        saved = int(data_manager.saved_episode_count())
    except Exception:  # noqa: BLE001
        saved = 0
    return {'verdict': verdict, 'seconds': seconds, 'error': box.get('error', ''), 'saved': saved}
