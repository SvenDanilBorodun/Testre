#!/usr/bin/env python3
#
# Copyright 2025 ROBOTIS CO., LTD.
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
#
# Author: Dongyun Kim
#
# EduBotics overlay: fixes the two correctness bugs in check_task_status
# that surfaced when the recording auto-push started routing through
# this worker:
#
#   1. Original lines 158-162 had unreachable code after `return result`
#      and reported "HF API worker process died" even when the worker
#      was simply idle. Replaced with a clean control flow that
#      distinguishes idle from died.
#
#   2. If the worker process was killed mid-upload (OOM during a 5 GB
#      multipart upload on a 6 GB-limit container is the realistic
#      scenario), is_alive() went False but is_processing stayed True
#      (the worker never got to put a result on output_queue). The
#      original code set status='Failed' at the top of check_task_status
#      then OVERWROTE it to 'Uploading' at line 213, so the React UI
#      saw "Uploading" forever and the recording auto-upload pipeline
#      stalled silently. Now: detect the (dead worker + still
#      processing) state, emit ONE Failed event with a German message,
#      reset internal state so subsequent ticks report Idle.
#
#   3. Aufnahme 2.0 round 5 (F7, spec-r5-final §7.4): an upload that stopped
#      moving. huggingface_hub's upload_large_folder retries a failing LFS
#      pre-upload or commit FOREVER, and its report line does not move while
#      one large file is on the wire, so a dead network left the page at
#      „Hochladen … 30 %" for good. The child forwards the library's public
#      ERROR lines (progress_tracker.install_upload_error_forwarder); the
#      parent drains EVERY queue item and ends an upload with no progress for
#      UPLOAD_STALL_S while it logs errors, or with no progress at all for
#      UPLOAD_HARD_STALL_S: the child is terminated, ONE Failed carries
#      UPLOAD_STALL_DE. The local dataset is untouched (the worker only reads
#      it), and the node starts a new worker on the next request. Byte or
#      socket counters cannot replace this: /proc/<pid>/io does not count
#      send() (4 MB sent, 96 bytes counted), the container's NIC counters
#      carry video and DDS, and hf_xet uploads outside httpx.
#
#   4. 2026-10-04 (review of the per-student token, item g): the other modes
#      had no bound at all, and is_busy() is what /register_hf_user asks
#      before it changes the token, so a hung download or list fetch blocked
#      every token change for ever. A download now ends after
#      DOWNLOAD_STALL_S without any progress (its progress is per FILE, so a
#      single large file legitimately shows none for a while; the bound is the
#      upload's hard cap), a list fetch or a delete after HUB_QUERY_TIMEOUT_S in
#      total. Same ending as an upload stall: the child is killed, ONE Failed
#      with a German sentence, a fresh worker on the next request.

#   5. Daten 2.0 (2026-10-05, spec §C4/§E2): an upload request may carry
#      `expected_hub_sha`, passed to DataManager.upload_huggingface_repo ONLY
#      when the key is present; the guarded upload's status extras (repo_type,
#      info_json, the unconfirmed-commit sentence) reach the parent on the
#      progress queue. The ('success'|'error', message) tuples are unchanged.

import importlib.util
import logging
import multiprocessing
import os
import queue
import time
from typing import List, Optional

from physical_ai_server.data_processing.data_manager import DataManager

try:
    from physical_ai_server.data_processing.record_texts_de import (
        DOWNLOAD_STALL_DE,
        HUB_QUERY_STALL_DE,
        UPLOAD_STALL_DE,
    )
except ImportError:  # loaded by path (deps-free tests): read the sibling directly
    _texts_spec = importlib.util.spec_from_file_location(
        '_edubotics_record_texts_de',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'record_texts_de.py'))
    _texts = importlib.util.module_from_spec(_texts_spec)
    _texts_spec.loader.exec_module(_texts)
    UPLOAD_STALL_DE = _texts.UPLOAD_STALL_DE
    DOWNLOAD_STALL_DE = _texts.DOWNLOAD_STALL_DE
    HUB_QUERY_STALL_DE = _texts.HUB_QUERY_STALL_DE

# F7: no upload progress for this long while the library logs upload errors.
UPLOAD_STALL_S = 120.0
# F7: no upload progress for this long at all.
UPLOAD_HARD_STALL_S = 1800.0
UPLOAD_ERROR_ITEM_TYPE = 'upload_error'
# Daten 2.0 (§E2 step 9): the child hands the upload's status extras
# (DataManager._last_upload_extras: repo_type, info_json, a success sentence)
# to the parent on the progress queue, BEFORE the result tuple; the parent
# waits at most this long for it once the result is there. The result tuples
# keep their ('success'|'error', message) shape.
UPLOAD_EXTRAS_ITEM_TYPE = 'upload_extras'
UPLOAD_EXTRAS_WAIT_S = 1.0
# Item g: a download with no progress for this long (= the upload's hard cap;
# snapshot_download reports per file, so one big file shows no progress).
DOWNLOAD_STALL_S = UPLOAD_HARD_STALL_S
# Item g: a list fetch or a delete (no progress reports at all) in total.
HUB_QUERY_TIMEOUT_S = 120.0
HUB_QUERY_MODES = frozenset({'get_dataset_list', 'get_model_list', 'delete'})


class UploadStallWatch:
    """Is an upload still moving? Pure; the caller passes the monotonic clock.

    Progress = the (current, total, percentage) triple changed. Stalled = no
    progress for ``UPLOAD_STALL_S`` with at least one upload error in the last
    ``UPLOAD_STALL_S``, or no progress for ``UPLOAD_HARD_STALL_S``."""

    def __init__(self, now: float):
        self.reset(now)

    def reset(self, now: float) -> None:
        self.last_progress_mono = float(now)
        self._last_key = None
        self.error_times: List[float] = []

    def note_progress(self, item: dict, now: float) -> None:
        key = (item.get('current'), item.get('total'), item.get('percentage'))
        if key != self._last_key:
            self._last_key = key
            self.last_progress_mono = float(now)

    def note_error(self, now: float) -> None:
        self.error_times.append(float(now))

    def stalled(self, now: float) -> bool:
        now = float(now)
        self.error_times = [t for t in self.error_times if t >= now - UPLOAD_STALL_S]
        idle = now - self.last_progress_mono
        if idle >= UPLOAD_HARD_STALL_S:
            return True
        return idle >= UPLOAD_STALL_S and bool(self.error_times)


# Use the 'spawn' start method explicitly. Linux's default 'fork' causes
# the child to inherit the parent's rclpy state — including the
# `physical_ai_server` ROS node registration. The duplicate node then
# competes for DDS service routing and `/task/command` calls from the
# React UI time out (symptom: "Befehlsausführung fehlgeschlagen [Stop]:
# Service call failed for /task/command"). 'spawn' boots a clean Python
# interpreter in the child, so no inherited node, publishers, or
# subscriptions follow. The trade-off is a small startup cost (~200 ms
# for the child to re-import its modules) which is invisible at the
# per-recording cadence this worker is used.
_MP_CTX = multiprocessing.get_context('spawn')


class HfApiWorker:

    def __init__(self):
        self.input_queue = _MP_CTX.Queue()
        self.output_queue = _MP_CTX.Queue()
        self.progress_queue = _MP_CTX.Queue()
        self.process = None
        self.logger = logging.getLogger('HfApiWorker')

        # Task state management
        self.is_processing = False
        self.current_task = None
        self.start_time = None

        # Progress tracking
        self.current_progress = {
            'current': 0,
            'total': 0,
            'percentage': 0.0,
            'is_downloading': False,
            'repo_id': '',
            'repo_type': ''
        }
        self.last_logged_current_progress = -1  # Track last logged current value
        # Daten 2.0: the running upload's status extras (repo_type, info_json)
        self.last_upload_extras = None
        # F7: the upload stall watchdog (round 5); item g: the other modes
        self.stall_watch = UploadStallWatch(time.monotonic())
        self.task_started_mono = time.monotonic()

        # Basic config for the main process logger
        logging.basicConfig(
            level=logging.INFO,
            format='%(name)s - %(levelname)s - %(message)s')

    def start(self):
        if self.process and self.process.is_alive():
            self.logger.warning('HF API worker process is already running.')
            return False

        try:
            self.logger.info('Starting HF API worker process...')

            self.process = _MP_CTX.Process(
                target=self._worker_process_loop,
                args=(
                    self.input_queue,
                    self.output_queue,
                    self.progress_queue
                )
            )

            self.process.start()
            self.logger.info(f'HF API worker process started with PID: {self.process.pid}')
            return True

        except Exception as e:
            self.logger.error(f'Failed to start HF API worker: {str(e)}')
            return False

    def stop(self, timeout=3.0):
        if not self.is_alive():
            self.logger.info('HF API worker process is not running or already stopped.')
            return

        try:
            self.logger.info('Sending shutdown signal to HF API worker...')
            # Send graceful shutdown signal first
            try:
                self.input_queue.put_nowait(None)
            except Exception:
                # If queue is full/unavailable, proceed to force terminate
                pass

            # Give a very short grace period, then force terminate if still alive
            grace_timeout = min(max(timeout, 0.0), 1.0)
            if grace_timeout > 0:
                self.process.join(grace_timeout)

            if self.process.is_alive():
                self.logger.warning(
                    'HF API worker did not terminate gracefully. Forcing termination now.')
                self.process.kill()
                # Ensure the process is reaped promptly
                self.process.join(1.0)
        except Exception as e:
            self.logger.error(f'Error stopping HF API worker process: {e}')
        finally:
            self.process = None
            # Reset state
            self.is_processing = False
            self.current_task = None
            self.start_time = None

    def is_alive(self):
        return self.process and self.process.is_alive()

    def send_request(self, request_data):
        if self.is_alive():
            self.input_queue.put(request_data)
            self.last_upload_extras = None
            self.is_processing = True
            self.current_task = request_data
            self.start_time = time.time()
            self.task_started_mono = time.monotonic()
            self.stall_watch.reset(self.task_started_mono)
            return True
        else:
            self.logger.error('Cannot send request, HF API worker process is not running.')
            return False

    def get_result(self, block=False, timeout=0.1):
        try:
            return self.output_queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def check_task_status(self) -> dict:
        """Check the current task status and return appropriate message."""
        result = {
            'operation': '',
            'status': 'Idle',
            'repo_id': '',
            'local_path': '',
            'message': '',
            'progress': {
                'current': 0,
                'total': 0,
                'percentage': 0.0,
            }
        }

        # Carry the in-flight task identifiers into the result early so
        # the dead-worker branch below has a repo_id to surface.
        mode = None
        if self.current_task:
            mode = self.current_task.get('mode', 'Processing')
            result['operation'] = mode
            result['repo_id'] = self.current_task.get('repo_id', '')
            result['local_path'] = self.current_task.get('local_path', '')
            result['repo_type'] = self.current_task.get('repo_type', '') or ''

        # Dead-worker recovery. If the worker process was killed (OOM,
        # SIGSEGV, external kill) while a task was in flight, the
        # output_queue never got a result; without this branch
        # is_processing stays True forever, and the downstream UI shows
        # "Uploading" indefinitely because the per-mode branches below
        # overwrite the Failed status. Emit ONE Failed event with a
        # German message, then reset state so subsequent polls report
        # Idle (worker can be restarted by the node).
        if not self.is_alive() and self.is_processing:
            self.logger.error(
                'HF API worker process died while processing — emitting Failed.')
            result['status'] = 'Failed'
            if mode == 'upload':
                result['message'] = (
                    'Upload abgebrochen: Worker-Prozess unerwartet beendet. '
                    'Bitte erneut versuchen.'
                )
            elif mode == 'download':
                result['message'] = (
                    'Download abgebrochen: Worker-Prozess unerwartet beendet. '
                    'Bitte erneut versuchen.'
                )
            else:
                result['message'] = (
                    'HF-Anfrage abgebrochen: Worker-Prozess unerwartet beendet.'
                )
            self.is_processing = False
            self.current_task = None
            return result

        # Worker is up but idle — no task to report on.
        if not self.is_processing:
            return result

        try:
            # Check for download / upload progress updates.
            if mode == 'download' or mode == 'upload':
                self.current_progress = self.get_progress_from_progress_queue()
                current = self.current_progress.get('current', 0)
                total = self.current_progress.get('total', 0)
                percentage = self.current_progress.get('percentage', 0.0)
                result['progress']['current'] = current
                result['progress']['total'] = total
                result['progress']['percentage'] = percentage

                # Only log when current value changes
                if current != self.last_logged_current_progress:
                    self.last_logged_current_progress = current

            # Check for task result
            task_result = self.get_result(block=False, timeout=0.1)
            if task_result:
                status, message = task_result
                if mode == 'upload':
                    extras = self._await_upload_extras()
                    if extras.get('repo_type'):
                        result['repo_type'] = extras['repo_type']
                    if status == 'success' and extras.get('info_json'):
                        result['info_json'] = extras['info_json']
                    if extras.get('code'):
                        result['upload_code'] = extras['code']      # the Daten keep_both job reads it
                # The client toasts `message` verbatim (useRosTopicSubscription
                # /huggingface/status), so it gets the worker's own German
                # sentence; the English wrapper goes to the log only (Rule §1).
                if status == 'success':
                    self.logger.info(f'HF API task completed successfully:\n{message}')
                    self.is_processing = False
                    self.current_task = None

                    result['operation'] = mode
                    result['status'] = 'Success'
                    result['message'] = message
                    return result
                elif status == 'error':
                    self.logger.error(f'HF API task failed:\n{message}')
                    self.is_processing = False
                    self.current_task = None

                    result['operation'] = mode
                    result['status'] = 'Failed'
                    result['message'] = message
                    return result

            # F7: an upload that stopped moving ends here, once.
            now = time.monotonic()
            if mode == 'upload' and self.stall_watch.stalled(now):
                return self._fail_stalled_upload(result)
            # Item g: a download that stopped moving, or a list fetch / delete
            # that never answers, ends the same way (is_busy() must not stay
            # true for ever: it blocks every token change on the robot).
            if mode == 'download' and now - self.stall_watch.last_progress_mono >= DOWNLOAD_STALL_S:
                return self._fail_stalled_task(result, mode, DOWNLOAD_STALL_DE)
            if mode in HUB_QUERY_MODES and now - self.task_started_mono >= HUB_QUERY_TIMEOUT_S:
                return self._fail_stalled_task(result, mode, HUB_QUERY_STALL_DE)

            # Still processing - return appropriate status message
            if mode:
                if mode == 'upload':
                    result['operation'] = mode
                    result['status'] = 'Uploading'
                    return result
                elif mode == 'download':
                    result['operation'] = mode
                    result['status'] = 'Downloading'
                    return result
                elif mode == 'delete':
                    result['operation'] = mode
                    result['status'] = 'Deleting'
                    return result
                elif mode in ['get_dataset_list', 'get_model_list']:
                    result['operation'] = mode
                    result['status'] = 'Fetching'
                    return result
                else:
                    result['operation'] = 'Unknown'
                    result['status'] = 'Processing'
                    return result

            result['status'] = 'Processing'
            return result

        except Exception as e:
            self.logger.error(f'Error checking HF API task status: {str(e)}')
            result['operation'] = mode if mode else 'Unknown'
            result['status'] = 'Failed'
            result['message'] = (
                'Der Status der Hugging Face-Aufgabe konnte nicht gelesen werden: '
                f'{str(e)}'
            )
            return result

    def is_busy(self):
        """Check if the worker is currently processing a task."""
        return self.is_processing

    def get_progress_from_progress_queue(self):
        """Drain EVERY item of the progress queue (F7): progress items feed the
        stall watch and the latest one is returned; an upload-error item (from
        the child's forwarder) is time-stamped, never mistaken for progress."""
        latest_progress = None
        now = time.monotonic()
        try:
            while True:
                try:
                    item = self.progress_queue.get(block=False, timeout=0.01)
                except queue.Empty:
                    break
                if not isinstance(item, dict):
                    continue
                if item.get('type') == UPLOAD_ERROR_ITEM_TYPE:
                    self.stall_watch.note_error(now)
                    continue
                if item.get('type') == UPLOAD_EXTRAS_ITEM_TYPE:
                    self.last_upload_extras = item.get('extras') or {}
                    continue
                latest_progress = item
                self.stall_watch.note_progress(item, now)
        except Exception as e:
            self.logger.error(f'Error updating progress from worker: {e}')

        # Return the latest progress or current progress if no new data
        return latest_progress if latest_progress else self.current_progress

    def _await_upload_extras(self) -> dict:
        """The finished upload's extras: already drained, or still in flight on
        the progress queue (a feeder thread per queue: the result tuple can
        overtake it). Bounded by UPLOAD_EXTRAS_WAIT_S; never raises."""
        extras = getattr(self, 'last_upload_extras', None)
        progress_queue = getattr(self, 'progress_queue', None)
        deadline = time.monotonic() + UPLOAD_EXTRAS_WAIT_S
        while extras is None and progress_queue is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = progress_queue.get(block=True, timeout=remaining)
            except queue.Empty:
                break
            except Exception:  # noqa: BLE001
                break
            if isinstance(item, dict) and item.get('type') == UPLOAD_EXTRAS_ITEM_TYPE:
                extras = item.get('extras') or {}
        self.last_upload_extras = None
        return extras or {}

    def _fail_stalled_upload(self, result: dict) -> dict:
        """Terminate the child and report ONE Failed with the German sentence."""
        idle = time.monotonic() - self.stall_watch.last_progress_mono
        self.logger.error(
            f'HF upload made no progress for {idle:.0f} s '
            f'({len(self.stall_watch.error_times)} upload error(s) in the last '
            f'{UPLOAD_STALL_S:.0f} s): terminating the worker.')
        self._terminate_worker()
        result['operation'] = 'upload'
        result['status'] = 'Failed'
        result['message'] = UPLOAD_STALL_DE
        return result

    def _fail_stalled_task(self, result: dict, mode: str, message_de: str) -> dict:
        """Item g: a download / list fetch / delete that does not end. Same ending
        as an upload stall (kill the child, ONE Failed, a fresh worker next time)."""
        elapsed = time.monotonic() - self.task_started_mono
        self.logger.error(
            f'HF {mode} did not finish ({elapsed:.0f} s since it started): '
            f'terminating the worker.')
        self._terminate_worker()
        result['operation'] = mode
        result['status'] = 'Failed'
        result['message'] = message_de
        return result

    def _terminate_worker(self) -> None:
        """Kill the child at once (it is stuck inside upload_large_folder and
        reads no shutdown signal) and reset the task state. The queues a killed
        process may have been writing to are replaced."""
        process: Optional[multiprocessing.Process] = self.process
        try:
            if process is not None and process.is_alive():
                process.kill()
                process.join(1.0)
        except Exception as e:
            self.logger.error(f'Error terminating HF API worker process: {e}')
        finally:
            self.process = None
            self.is_processing = False
            self.current_task = None
            self.start_time = None
            for name in ('input_queue', 'output_queue', 'progress_queue'):
                try:
                    setattr(self, name, _MP_CTX.Queue())
                except Exception as e:  # noqa: BLE001
                    self.logger.error(f'Could not renew {name}: {e}')

    @staticmethod
    def _worker_process_loop(input_queue, output_queue, progress_queue):
        # Set up logging for the worker process
        logging.basicConfig(
            level=logging.INFO,
            format='[HF_API_WORKER] %(levelname)s: %(message)s')
        logger = logging.getLogger('hf_api_worker')

        try:
            logger.info(f'HF API worker process started with PID: {os.getpid()}')
            logger.info('Worker is ready and waiting for requests')

            # Set progress queue for DataManager
            DataManager.set_progress_queue(progress_queue)

            # 042: this spawned child never imports the node module, so the
            # node's token scrubber is NOT active here. Attach it to the named
            # HF/HTTP loggers (and the root handlers basicConfig just made).
            try:
                from physical_ai_server.data_processing import hf_token_store
                hf_token_store.install_log_scrubber()
            except Exception as e:  # noqa: BLE001
                logger.error(f'Token log scrubber not installed: {type(e).__name__}')

            # F7: forward upload_large_folder's ERROR lines to the parent's
            # stall watchdog. Never fatal: without it only the hard cap applies.
            try:
                from physical_ai_server.data_processing.progress_tracker import (
                    install_upload_error_forwarder,
                )
                install_upload_error_forwarder(progress_queue)
            except Exception as e:  # noqa: BLE001
                logger.error(f'Upload error forwarder not installed: {e}')

            request_count = 0
            last_log_time = time.time()

            while True:
                try:
                    # Log periodic status
                    current_time = time.time()
                    if current_time - last_log_time > 30.0:  # Log every 30 seconds
                        msg = f'Worker still alive, processed {request_count} requests so far'
                        logger.info(msg)
                        logger.info(f'Input queue size: {input_queue.qsize()}')
                        last_log_time = current_time

                    # Check for new requests
                    try:
                        data = input_queue.get(timeout=1.0)

                        if data is None:  # Shutdown signal
                            logger.info('Received shutdown signal')
                            break

                        request_count += 1
                        logger.info(f'*** Received HF API request #{request_count} ***')

                        mode = data.get('mode')
                        repo_id = data.get('repo_id')
                        repo_type = data.get('repo_type')
                        local_dir = data.get('local_dir')
                        author = data.get('author')
                        # Defaults True so an upload request without the
                        # key fails safe to a private repo.
                        private = bool(data.get('private', True))

                        logger.info(f'Processing {mode} request for repo: {repo_id}')

                        # Process the request based on mode
                        # The ('error', message) tuples below land in
                        # TaskStatus.error and surface verbatim as a student
                        # toast — German per Rule §1. When the DataManager
                        # call classified the failure (e.g. invalid HF token
                        # -> the "ersetze dein Token auf der Startseite"
                        # hint), surface that precise
                        # reason instead of the generic line. The reason
                        # side-channel is same-process: this worker loop is
                        # single-threaded and reads it right after the call.
                        if mode == 'upload':
                            logger.info(f'Starting upload for repo: {repo_id}')
                            kwargs = {'repo_id': repo_id, 'repo_type': repo_type,
                                      'local_dir': local_dir, 'private': private}
                            # Daten 2.0 (§C4): passed ONLY when the request names
                            # it — an absent key reaches the function's own
                            # default (decide at upload time), never None.
                            if 'expected_hub_sha' in data:
                                kwargs['expected_hub_sha'] = data['expected_hub_sha']
                            result = DataManager.upload_huggingface_repo(**kwargs)
                            extras = getattr(DataManager, '_last_upload_extras', None) or {}
                            try:
                                progress_queue.put({'type': UPLOAD_EXTRAS_ITEM_TYPE, 'extras': extras})
                            except Exception as e:  # noqa: BLE001 — the status works without them
                                logger.error(f'Upload extras not forwarded: {type(e).__name__}')
                            if result:
                                message = (extras.get('message_de')
                                           or f'Hugging Face-Upload abgeschlossen: {repo_id}')
                                logger.info(f'Upload completed: {repo_id}')
                                output_queue.put(('success', message))
                            else:
                                reason = DataManager._last_hf_failure_reason_de
                                message = reason or (
                                    f'Upload zu Hugging Face fehlgeschlagen:'
                                    f'\n{repo_id}'
                                    f'\nBitte Internetverbindung und Repo-Namen '
                                    f'prüfen und erneut versuchen.'
                                )
                                logger.error(f'Upload failed: {repo_id}')
                                output_queue.put(('error', message))

                        elif mode == 'download':
                            logger.info(f'Starting download for repo: {repo_id}')
                            result = DataManager.download_huggingface_repo(
                                repo_id=repo_id,
                                repo_type=repo_type
                            )
                            if result:
                                message = f'Hugging Face-Download abgeschlossen: {repo_id}'
                                logger.info(f'Download completed: {repo_id}')
                                output_queue.put(('success', message))
                            else:
                                reason = DataManager._last_hf_failure_reason_de
                                message = reason or (
                                    f'Download von Hugging Face fehlgeschlagen:'
                                    f'\n{repo_id}'
                                    f'\nBitte Internetverbindung und Repo-Namen '
                                    f'prüfen und erneut versuchen.'
                                )
                                logger.error(f'Download failed: {repo_id}')
                                output_queue.put(('error', message))

                        elif mode == 'delete':
                            logger.info(f'Starting delete for repo: {repo_id}')
                            DataManager.delete_huggingface_repo(
                                repo_id=repo_id,
                                repo_type=repo_type
                            )
                            message = f'Hugging Face-Repo gelöscht: {repo_id}'
                            logger.info(f'Delete completed: {repo_id}')
                            output_queue.put(('success', message))

                        elif mode == 'get_dataset_list':
                            logger.info(f'Starting dataset list fetch for author: {author}')
                            DataManager.get_huggingface_repo_list(
                                author=author,
                                data_type='dataset'
                            )
                            message = f'Datensatzliste von {author} geladen.'
                            logger.info(f'Dataset list fetch completed: {author}')
                            output_queue.put(('success', message))

                        elif mode == 'get_model_list':
                            logger.info(f'Starting model list fetch for author: {author}')
                            DataManager.get_huggingface_repo_list(
                                author=author,
                                data_type='model'
                            )
                            message = f'Modellliste von {author} geladen.'
                            logger.info(f'Model list fetch completed: {author}')
                            output_queue.put(('success', message))

                        else:
                            logger.error(f'Unknown mode: {mode}')
                            output_queue.put(('error', f'Unbekannte Hugging Face-Aktion: {mode}'))

                    except queue.Empty:
                        continue

                except Exception as e:
                    logger.error(f'HF API operation error: {str(e)}')
                    import traceback
                    logger.error(f'Traceback: {traceback.format_exc()}')
                    output_queue.put(('error', f'Die Hugging Face-Aktion ist fehlgeschlagen: {str(e)}'))

        except Exception as e:
            logger.error(f'HF API worker initialization error: {str(e)}')
            import traceback
            logger.error(f'Traceback: {traceback.format_exc()}')
            output_queue.put(('error', f'Der Hugging Face-Dienst konnte nicht starten: {str(e)}'))

        logger.info('HF API worker process shutting down')
