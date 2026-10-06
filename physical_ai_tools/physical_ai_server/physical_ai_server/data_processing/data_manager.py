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
# Author: Dongyun Kim, Seongwoo Kim

from contextlib import contextmanager
import functools
import importlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata

import cv2
from geometry_msgs.msg import Twist
from huggingface_hub import (
    HfApi,
    ModelCard,
    ModelCardData,
    snapshot_download,
)
from huggingface_hub.errors import LocalTokenNotFoundError
from lerobot.datasets.utils import DEFAULT_FEATURES
from nav_msgs.msg import Odometry
import numpy as np
from physical_ai_interfaces.msg import TaskStatus
from physical_ai_server.data_processing import dataset_paths
from physical_ai_server.data_processing.data_converter import DataConverter
from physical_ai_server.data_processing.lerobot_dataset_wrapper import (
    LeRobotDatasetWrapper,
)
from physical_ai_server.data_processing.progress_tracker import (
    HuggingFaceProgressTqdm
)
from physical_ai_server.device_manager.cpu_checker import CPUChecker
from physical_ai_server.device_manager.ram_checker import RAMChecker
from physical_ai_server.device_manager.storage_checker import StorageChecker
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory


def _sibling(name):
    """A stdlib-only sibling module of this package (round 5): by package name
    in the image, by file path when a deps-free test loader gave the package
    no __path__ (the reason this file had no sibling imports before)."""
    try:
        return importlib.import_module(f'physical_ai_server.data_processing.{name}')
    except ImportError:
        spec = importlib.util.spec_from_file_location(
            f'_edubotics_dm_{name}', str(Path(__file__).with_name(f'{name}.py')))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


record_texts_de = _sibling('record_texts_de')
hf_errors = _sibling('hf_errors')
dataset_card = _sibling('dataset_card')
# Daten 2.0: the sync record and the ONE decision (stdlib only). hub_sync (the
# guarded upload, the hub view) is reached inside the functions that use it
# (A18: the deps-free loaders stub huggingface_hub with a fixed attribute set).
dataset_sync = _sibling('dataset_sync')


# Student-facing German camera names for record-path sentences (Aufnahme 2.0).
# The config keys stay `gripper`/`scene`; only the words a student reads change.
# One vocabulary: these names are record_texts_de's (round 7).
CAMERA_NAME_DE = record_texts_de.CAMERA_NAME_DE
camera_name_de = record_texts_de.camera_name_de


# Dataset repo names (Aufnahme 2.0, owner decision Q2). A German task name used
# to reach the Hub mangled („Würfel“ -> `W-rfel`), and a double space, an emoji
# or an accented letter left `--`/`..` runs that huggingface_hub's repo-id
# validator refuses. The React page predicts the same name
# (utils/datasetName.js); both sides reproduce the shared fixture
# physical_ai_manager/src/utils/__tests__/datasetName.cases.json byte for byte,
# and THIS module is the reference it is generated from. Stdlib only, and no new
# sibling module: the deps-free test loaders give the package no __path__.
_DE_TRANSLITERATION = (
    ('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'),
    ('Ä', 'Ae'), ('Ö', 'Oe'), ('Ü', 'Ue'),
    ('ß', 'ss'), ('ẞ', 'SS'),
)
_UNSAFE_NAME_CHARS = re.compile(r'[^a-zA-Z0-9._-]')
_DASH_RUN = re.compile(r'-{2,}')
_DOT_RUN = re.compile(r'\.{2,}')


def safe_dataset_task_name(task_name) -> str:
    """NFC -> German pairs -> NFKD accent fold -> sanitise -> collapse runs -> strip `-`.

    The order is part of the contract (the client mirrors it). Combining marks
    are Unicode category M* (the same predicate as the Unicode property M in JS).
    """
    text = unicodedata.normalize('NFC', str(task_name or ''))
    for src, dst in _DE_TRANSLITERATION:
        text = text.replace(src, dst)
    text = ''.join(ch for ch in unicodedata.normalize('NFKD', text)
                   if not unicodedata.category(ch).startswith('M'))
    text = _UNSAFE_NAME_CHARS.sub('-', text)
    text = _DOT_RUN.sub('.', _DASH_RUN.sub('-', text))
    return text.strip('-')


def safe_dataset_user_id(user_id) -> str:
    """HEAD's rule, deliberately unchanged: no transliteration, no collapse.

    The upload namespace guard compares this part to the names whoami returns,
    so it must stay the HF account name as sent. `-` and `.` survive the
    sanitiser, so `..` does too; HF user ids cannot be dot-only, and anything
    that is becomes a safe placeholder rather than a traversal.
    """
    safe = _UNSAFE_NAME_CHARS.sub('-', str(user_id or '')).strip('-')
    if not safe or set(safe) <= {'.'}:
        safe = 'unknown-user'
    return safe


# Aufnahme 2.0 record-FSM constants (module constants, NOT env vars — a new
# EDUBOTICS_* knob would need a compose forward).
# MOVE_TO_NEXT inside the first second of a run answers `too_early`, and FINISH
# drops a run that short (owner decision Q3): it was never a real attempt.
EARLY_SAVE_MIN_S = 1.0
# The server remembers a RERECORD received from the wire for this long; a
# following FINISH drops any run that STARTED after it (owner decision Q4), so
# „Verwerfen und beenden“ keeps nothing however slow the link or short the reset.
RERECORD_FINISH_WINDOW_S = 5.0
# Terminating-tick reason when the finished dataset could not be handed to the
# upload worker (see _upload_blocked_reason_de). The text lives in
# record_texts_de; this name stays for its readers.
UPLOAD_NOT_STARTED_DE = record_texts_de.UPLOAD_NOT_STARTED_DE
# ONE re-record cap per episode number (round 6; reset when that episode is
# saved), shared by both automatic re-records: a take with a source gap >=
# SOURCE_GAP_S on the source's own timeline (O2/C6) and a take the encoder
# dropped frames from (C7). Once reached, a gapped take is SAVED with a warning
# and a lossy take ends the session like „Beenden“ (the take dropped, saved
# episodes kept, finalize + upload).
MAX_REDOS_PER_EPISODE = 2
# D7 (round 6): the logged-in hub existence check (whoami + repo_exists) is
# bounded; a hub that does not answer within this time counts as unreachable.
HUB_CHECK_TIMEOUT_S = 15.0
# Post-save length check (LeRobot's train-time FrameTimestampError condition):
# a saved episode's video span may differ from length / fps by this many frames.
SAVED_LENGTH_TOLERANCE_FRAMES = 0.5
# Daten 2.0, D14 (spec §C4). `_sync_base` is the hub head the Start decision saw
# (a 40-hex sha), None (the hub holds no dataset) or this sentinel: the Start
# could not ask (upload off, an unreachable hub, a download stub that returned
# no revision) — the upload then decides at its own time. A string, never an
# object(): the deps-free loaders execute this module more than once.
SYNC_UNCHECKED = 'unchecked'
# How often a Start that waits for this dataset's upload (or for its sync
# download) looks again (= daten.contract.START_UPLOAD_POLL_S, lockstep-tested).
START_UPLOAD_POLL_S = 0.5
_HEAD_SHA = re.compile(r'^[0-9a-f]{40}$')
# upload_huggingface_repo's „no expectation given“: the guarded upload then
# decides at upload time (§E2 step 3). Never passed on to hub_sync.
_UNSET = object()


def _resume_compatibility_check(dataset, robot_type, fps, features):
    """D4: LeRobot's OWN resume check (lerobot.utils.control_utils.
    sanity_check_dataset_robot_compatibility) — raises ValueError naming the
    first mismatching field: robot_type, fps or features. Imported lazily: it
    is only needed when an existing dataset is opened."""
    from types import SimpleNamespace
    from lerobot.utils.control_utils import sanity_check_dataset_robot_compatibility
    sanity_check_dataset_robot_compatibility(
        dataset, SimpleNamespace(robot_type=robot_type), int(fps), features)


class HubCheckRefused(Exception):
    """The Start check refused the session (``_last_warning_message`` says why)."""


class StartAbandoned(Exception):
    """A FINISH/STOP arrived while the Start waited (an upload, a sync download):
    the session ends without a dataset and without an error text."""


def _daten_texts():
    """``daten/texts_de`` (the Daten tab's [D] sentences): by package name in the
    image, by file path for the deps-free loaders."""
    try:
        return importlib.import_module('physical_ai_server.daten.texts_de')
    except ImportError:
        spec = importlib.util.spec_from_file_location(
            '_edubotics_dm_daten_texts_de',
            str(Path(__file__).resolve().parent.parent / 'daten' / 'texts_de.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _recorder_locked(method):
    """Run a public DataManager mutator under the recorder lock (round 5, O1).

    Re-entrant (an RLock), so the record tick's outer section and the inner
    calls compose. Every release applies a collision discard that arrived
    meanwhile (see DataManager._release_and_drain); such a drain always lands
    BETWEEN DataManager operations, never inside one.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.locked():
            return method(self, *args, **kwargs)
    wrapper.edubotics_recorder_locked = True
    return wrapper


class DataManager:
    RECORDING = False
    RECORD_COMPLETED = True
    SKIP_TIME = 0.1  # Seconds

    # Progress queue for multiprocessing communication
    _progress_queue = None

    def __init__(
            self,
            save_root_path,
            robot_type,
            task_info,
            upload_callback=None):
        self._robot_type = robot_type
        safe_task_name = safe_dataset_task_name(task_info.task_name)
        # `user_id` is CLIENT-SUPPLIED and was the ONE component here that was
        # never sanitised, while `task_name` beside it always was. That matters
        # because `_save_path` reaches a `shutil.rmtree` in
        # `_check_dataset_exists` (an incomplete dataset is deleted and
        # re-created), and `save_root_path / '<abs>/x'` DISCARDS the root —
        # pathlib's absolute-segment rule again. So an absolute or `..`-bearing
        # user_id turned a per-frame recording check into an arbitrary
        # directory delete. Same sanitiser as task_name, then `..`-collapse,
        # then a confine() that PROVES the result stayed under the root.
        safe_user_id = safe_dataset_user_id(task_info.user_id)
        self._save_repo_name = f'{safe_user_id}/{robot_type}_{safe_task_name}'
        self._save_path = save_root_path / self._save_repo_name
        # Belt as well as braces: prove it, rather than trusting the sanitiser.
        # A raise here is correct — refusing to record beats deleting a tree.
        self._save_path = dataset_paths.confine(self._save_path, save_root_path)
        self._save_rosbag_path = '/workspace/rosbag2/' + self._save_repo_name
        self._on_saving = False
        self._single_task = len(task_info.task_instruction) == 1
        self._task_info = task_info
        # Wired by the node to HfApiWorker.send_request so the
        # end-of-recording auto-push runs out-of-process, surfaces errors
        # on /huggingface/status, and lets the React side fire the
        # /datasets/register Cloud-API call on success. None when the
        # DataManager is constructed standalone (tests, fallback path).
        self._upload_callback = upload_callback
        # One-shot idempotency guard. The state machine has two call
        # sites that can both reach _upload_dataset on the same tick
        # (the 'finish' branch and the post-loop cap-reached check); the
        # timer also normally stops on RECORD_COMPLETED, but we belt-
        # and-suspenders against a future refactor that loses that stop,
        # plus the joystick re-entry path. Reset to False is intentional
        # only at construction — a new DataManager is built for every
        # recording (see init_robot_control_parameters_from_user_task).
        self._upload_enqueued = False

        self._lerobot_dataset = None
        self._record_episode_count = 0
        # Session-marker state (leLab-comparison PR-3). DISARMED by
        # default; the node arms it for START_RECORD sessions only.
        # (record_inference_mode is refused at START_INFERENCE — the
        # inference timer has no record() path, so such a session would
        # arm the marker while writing nothing.)
        self._session_marker_enabled = False
        self._session_marker_written = False
        self._session_started_unix = 0
        self._start_time_s = 0
        self._proceed_time = 0
        self._status = 'warmup'
        # Aufnahme 2.0 bookkeeping. The contract tests build DataManager via
        # __new__ with a fixed attribute set, so every reader of these uses
        # getattr(self, name, <default>).
        #   _run_entered_at        perf_counter() of the last entry into 'run'
        #   _wire_rerecord_at      perf_counter() of the last RERECORD off the wire
        #   _finish_count_pending  FINISH met an episode save() already committed
        #   _finish_drops_run      FINISH must discard the run in flight (Q3/Q4)
        #   _stop_count_pending    STOP committed a real episode (count only that)
        #   _disk_stop_requested   the low-disk stop fired (once per session)
        #   _upload_blocked_reason_de  German reason the finished dataset was NOT
        #                          uploaded; the node puts it on the terminating tick
        #   _last_stale_warn_mono  throttle for the recording stale-camera warning
        #   _discard_pending       a discarded take's streaming encoder still has
        #                          to be cancelled (next record tick, see
        #                          _cancel_discarded_take)
        self._run_entered_at = None
        self._wire_rerecord_at = None
        self._finish_count_pending = False
        self._finish_drops_run = False
        self._stop_count_pending = False
        self._disk_stop_requested = False
        self._upload_blocked_reason_de = ''
        self._last_stale_warn_mono = 0.0
        self._discard_pending = False
        # Round 5 (O1). One recorder lock: the record tick holds it for a whole
        # record step, /task/command for one transition (bounded try-acquire,
        # never blocking the default group), the resync for its recording work.
        # A collision trip NEVER waits for it: request_collision_discard() sets
        # _collision_discard_requested and the lock holder applies it at release.
        #   _save_count_pending  the latched 'save' really committed frames (an
        #                        empty save is not an episode, I2j)
        #   run_entered_mono     time.monotonic() of the last run entry, stamped
        #                        wherever _run_entered_at is (the slot sampler's
        #                        take boundary)
        self.lock = threading.RLock()
        self._collision_discard_requested = False
        self._save_count_pending = True
        self.run_entered_mono = None
        # Round 5 session rules.
        #   _source_stop_requested  R5-2: a stopped source ended the session (once)
        #   _take_gap_source        O2/C6: (kind, name) of the source whose gap
        #                           makes the running take a re-record (the
        #                           node's capture integrity; cleared per take)
        #   _redos                  automatic re-records per episode number (the
        #                           shared cap MAX_REDOS_PER_EPISODE)
        #   _end_requested          F1: a FINISH/STOP that arrived while the
        #                           recorder lock was held, applied at release
        #   _upload_off_notice_de   F4: why this session runs without upload
        #   _commit_count           takes committed to disk (the node closes the
        #                           take's integrity on a change)
        #   _frame_added            the last record() call added a frame
        self._source_stop_requested = False
        self._take_gap_source = None
        self._redos = {}
        self._end_requested = None
        self._upload_off_notice_de = ''
        self._commit_count = 0
        self._frame_added = False
        # Daten 2.0 (spec §C4, D14). Every reader uses getattr with these
        # defaults (the contract tests build DataManager via __new__).
        #   _sync_base         the hub head the Start decision saw, None (no
        #                      dataset online) or SYNC_UNCHECKED; handed to the
        #                      end-of-session upload as expected_hub_sha
        #   _start_abandoned   a FINISH/STOP ended the Start's wait: no dataset is
        #                      created and no error is shown
        #   _dataset_lease     injected by the node (DatenService.claim_record_lease):
        #                      root -> the busy kind of that dataset, or None after
        #                      registering this session's `record` lease
        #   _sync_download     injected by the node (DatenService.start_sync_download):
        #                      (repo_id, revision, root, token_fp) -> a handle with
        #                      poll() -> None | result dict and cancel()
        #   _start_notice_de   a notice for the first record tick (OFFLINE_START_DE)
        self._sync_base = SYNC_UNCHECKED
        self._start_abandoned = False
        self._dataset_lease = None
        self._sync_download = None
        self._lease_logged = False
        self._start_notice_de = ''
        self._cpu_checker = CPUChecker()
        self.data_converter = DataConverter()
        # Propagate the task fps into the action-duration setter so
        # published action messages use the right time_from_start at
        # non-30 Hz recordings. Safe no-op if fps is missing/zero.
        self.data_converter.set_action_duration_from_fps(
            getattr(task_info, 'fps', 0) or 0
        )
        self.force_save_for_safety = False
        self._stop_save_completed = False
        self.current_instruction = ''
        self._current_task = 0
        self._init_task_limits()
        self._current_scenario_number = 0
        # Surfaced to TaskStatus.error as a [WARNUNG] prefix so the React UI
        # renders a banner after the truncated episode saves. Cleared on the
        # next episode reset.
        self._last_warning_message: str = ''
        # Stale-camera detection at recording time (mirrors the inference
        # path's overlay/inference_manager.py:_check_stale_cameras). Without
        # this a frozen USB camera silently writes the same frame to every
        # tick of the dataset — the trained model then learns from a static
        # observation. Hashing 4 sparse 256-byte slices is cheap (~µs per
        # frame) and detects any decoded-pixel change.
        self._last_image_hashes: dict[str, int] = {}
        self._last_image_change_time: dict[str, float] = {}
        self._stale_threshold_s = 2.0
        self._stale_halt_threshold_s = 5.0
        # v2.5.0: streaming_encoding=True (LeRobotDatasetWrapper default) feeds
        # camera frames directly to ffmpeg as they arrive — no per-frame PNG
        # temp files, no in-RAM frame accumulation. The v2.4 JpegFrame buffer
        # + its env-var-toggled safety valve are gone (env var removed from
        # docker-compose.yml; ROS node bounds memory architecturally now).

    def get_status(self):
        return self._status

    def get_save_rosbag_path(self):
        episode_index = self._lerobot_dataset.get_episode_index()
        if episode_index is None:
            return None
        return self._save_rosbag_path + f'/{episode_index}'

    def should_record_rosbag2(self):
        return self._task_info.record_rosbag2

    # ── Crash-recoverable session marker (leLab-comparison PR-3) ─────────
    # A tiny SIBLING json next to the dataset dir (never inside it —
    # push_to_hub uploads the folder verbatim) that exists exactly while a
    # recording session is in flight. A crash/power-cut leaves it behind;
    # the node surfaces a one-shot German notice at the next start so the
    # student knows the dataset is incomplete and can delete it in the
    # Daten tab. DETECT + INFORM ONLY: the buffered (<10) episodes died
    # with the process — finalizing a crashed session is impossible, so
    # nothing here ever auto-finalizes or auto-deletes.

    SESSION_MARKER_SUFFIX = '.session.json'

    def _session_marker_path(self):
        # Telemetry only: a DataManager built without the full __init__
        # (tests construct via __new__; standalone helpers) has no
        # _save_path — markers silently disable rather than ever touching
        # the recording/finalize contract.
        save_path = getattr(self, '_save_path', None)
        if not isinstance(save_path, Path):
            return None
        return save_path.parent / (save_path.name + self.SESSION_MARKER_SUFFIX)

    def _write_session_marker(self):
        try:
            marker = self._session_marker_path()
            if marker is None:
                return
            marker.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                'repo_id': getattr(self, '_save_repo_name', ''),
                'episodes_saved': getattr(self, '_record_episode_count', 0),
                'status': getattr(self, '_status', ''),
                'started_unix': getattr(self, '_session_started_unix', 0),
            })
            tmp = marker.with_name(marker.name + '.tmp')
            tmp.write_text(payload, encoding='utf-8')
            os.replace(tmp, marker)
        except Exception as e:  # noqa: BLE001 — telemetry must never block
            print(f'[WARNUNG] Sitzungsmarker konnte nicht geschrieben '
                  f'werden: {e}', file=sys.stderr, flush=True)

    def _clear_session_marker(self):
        try:
            marker = self._session_marker_path()
            if marker is not None:
                marker.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — telemetry must never block
            pass

    @_recorder_locked
    def record(
            self,
            images,
            state,
            action):

        self._frame_added = False
        # A collision discard or an end that arrived since the last step (the
        # trip and FINISH never wait for the lock; normally the holder applies
        # them at release).
        self._drain_collision_request_locked()
        self._drain_end_request_locked()

        # A take discarded since the last tick (Wiederholen, a collision, the
        # frame-drop re-record, a dropped FINISH run): cancel its streaming
        # encoder NOW, in this reset/discard tick, before any frame of the next
        # take — never lazily inside that take's first frame. Before the start
        # stamp, so a reset timer does not count the cancel. (After a collision
        # the monitor has already done it, before releasing the arm.)
        self.cancel_pending_discard()

        if self._start_time_s == 0:
            self._start_time_s = time.perf_counter()
            if (getattr(self, '_session_marker_enabled', False)
                    and not getattr(self, '_session_marker_written', True)):
                self._session_marker_written = True
                self._session_started_unix = time.time()
                self._write_session_marker()

        if self._status == 'warmup':
            self._current_task = 0
            self._current_scenario_number = 0
            if not self._check_time(self._task_info.warmup_time_s, 'run'):
                return self.RECORDING

        elif self._status == 'run':
            # Round 5 (spec §3.3): a take ends by FRAME COUNT, never by wall
            # clock. The slot sampler hands the tick one decided frame per grid
            # slot (and catches up after a late tick), so the take is exactly
            # n_target frames and the dataset's frame_index / fps timestamps are
            # true. A call without images (a tick with no decided slot) adds
            # nothing. streaming_encoding=True (v2.5.0) bounds the in-RAM
            # buffer, so takes of any length are fine.
            if images is None:
                return self.RECORDING
            fps = self._record_fps()
            n_target = max(1, int(round(float(self._task_info.episode_time_s) * fps)))
            if not self._buffer_has_frames():
                # The take's first frame: re-arm the frame-drop watch (LeRobot's
                # public "Encoder queue full" warning), so save() judges this
                # take only.
                arm = getattr(self._lerobot_dataset, 'arm_frame_drop_watch', None)
                if arm is not None:
                    arm()
            frame = self.create_frame(images, state, action)
            if self._task_info.use_optimized_save_mode:
                self._lerobot_dataset.add_frame_without_write_image(
                    frame,
                    self.current_instruction)
            else:
                self._lerobot_dataset.add_frame(
                    frame,
                    self.current_instruction)
            self._frame_added = True
            size = self._buffer_size()
            self._proceed_time = size / fps
            if size >= n_target:
                self._status = 'save'
                self._start_time_s = 0
                self._proceed_time = 0

        elif self._status == 'save':
            if self._on_saving:
                if (
                    self._lerobot_dataset.check_video_encoding_completed()
                    or (
                        not self._single_task
                        and self._lerobot_dataset.check_append_buffer_completed()
                    )
                ):
                    # I2j: count only a save that committed frames. An empty
                    # latched save (an old client's multi-task MOVE_TO_NEXT in
                    # the first warm-up) is not an episode: no verify, no count,
                    # no scenario/task advance.
                    committed = getattr(self, '_save_count_pending', True)
                    if committed:
                        self._verify_saved_video_files()
                    self._episode_reset()
                    if committed:
                        self._record_episode_count += 1
                        self._write_session_marker()
                        self._get_current_scenario_number()
                        self._current_task += 1
                    self._on_saving = False

                    # Check if we've reached the target episode count
                    if (self._record_episode_count <
                            self._task_info.num_episodes):
                        # Not finished yet, go to reset for next episode
                        self._status = 'reset'
                        self._start_time_s = 0
                    else:
                        # Finished! Set status to 'finish' to skip reset
                        self._status = 'finish'
            else:
                # save() returns False when it discarded the episode for
                # re-recording (streaming frame drop) — it has already set
                # _status='reset', so do NOT latch _on_saving.
                committing = self._buffer_has_frames()
                if self.save():
                    self._save_count_pending = committing
                    self._on_saving = True

        elif self._status == 'reset':
            if not self._single_task:
                if not self._check_time(self.SKIP_TIME, 'run'):
                    return self.RECORDING
            else:
                if not self._check_time(self._task_info.reset_time_s, 'run'):
                    return self.RECORDING

        elif self._status == 'skip_task':
            if not self._check_time(self.SKIP_TIME, 'run'):
                return self.RECORDING

        elif self._status == 'stop':
            if not self._stop_save_completed:
                if self._on_saving:
                    # A STOP before the first record tick has no dataset at all.
                    if (self._lerobot_dataset is None
                            or self._lerobot_dataset.check_video_encoding_completed()):
                        self._on_saving = False
                        self._episode_reset()
                        # Count only an episode save() really committed: a STOP
                        # with nothing in flight (warm-up, reset) used to count
                        # one too many. Default True keeps a DataManager built
                        # without __init__ on HEAD's behaviour.
                        if getattr(self, '_stop_count_pending', True):
                            self._record_episode_count += 1
                        self._write_session_marker()
                        self._get_current_scenario_number()
                        self._current_task += 1
                        self._stop_save_completed = True
                        # v3.0: 'Stop' must finalize (and upload) exactly like
                        # 'finish' — otherwise a Stop-ended dataset ships a
                        # footer-less data parquet (and, for <10 episodes, no
                        # meta/episodes/*.parquet at all): silent corruption that
                        # only surfaces at train time / on a later manual push.
                        # Flush LeRobot's writers BEFORE upload (see
                        # _finalize_dataset); skip the upload if finalize failed.
                        finalized = self._finalize_dataset()
                        if (finalized and self._task_info.push_to_hub and
                                self._record_episode_count > 0):
                            self._upload_dataset(
                                self._task_info.tags,
                                self._task_info.private_mode)
                        return self.RECORD_COMPLETED
                else:
                    committing = self._buffer_has_frames()
                    if self.save():
                        self._stop_count_pending = committing
                        self._proceed_time = 0
                        self._on_saving = True
                    else:
                        # save() discarded the take (a frame loss or a source
                        # gap). A STOP is not re-recorded: complete it with the
                        # episodes already saved, like FINISH below.
                        self._finish_after_discard()
                        self._status = 'stop'
                        self._stop_count_pending = False
            return self.RECORDING

        elif self._status == 'finish':
            if self._on_saving:
                # A FINISH before the first record tick has no dataset at all:
                # nothing to wait for, nothing to finalize.
                if (self._lerobot_dataset is None
                        or self._lerobot_dataset.check_video_encoding_completed()):
                    self._on_saving = False
                    if getattr(self, '_finish_count_pending', False):
                        # The episode this FINISH committed (or found committed
                        # by a latched 'save') is counted exactly like the
                        # normal save-completion branch counts it — HEAD saved
                        # it locally and never counted, so it was not uploaded.
                        self._finish_count_pending = False
                        self._verify_saved_video_files()
                        self._record_episode_count += 1
                        self._write_session_marker()
                    self._episode_reset()
                    # v0.5.1: close the data ParquetWriter + flush the episode
                    # metadata buffer to disk BEFORE upload — without this the
                    # dataset is incomplete on disk (see _finalize_dataset).
                    finalized = self._finalize_dataset()
                    if (finalized and self._task_info.push_to_hub and
                            self._record_episode_count > 0):
                        self._upload_dataset(
                            self._task_info.tags,
                            self._task_info.private_mode)
                    return self.RECORD_COMPLETED
            elif getattr(self, '_finish_drops_run', False):
                # Q3/Q4: the run in flight is shorter than EARLY_SAVE_MIN_S or
                # started after a wire RERECORD <= RERECORD_FINISH_WINDOW_S ago.
                # Discard it and finish with what is already saved.
                self._finish_drops_run = False
                self._drop_in_progress_episode()
                self._proceed_time = 0
                self._on_saving = True
            else:
                committing = self._buffer_has_frames()
                if self.save():
                    self._finish_count_pending = committing
                    if not self._single_task:
                        self._lerobot_dataset.video_encoding()
                    self._proceed_time = 0
                    self._on_saving = True
                else:
                    # save() discarded the take (a frame loss or a source gap).
                    # A FINISH is not re-recorded: end the session with the
                    # episodes already saved and say why (HEAD silently
                    # continued the session instead).
                    self._finish_after_discard()

        if self._record_episode_count >= self._task_info.num_episodes:
            if self._lerobot_dataset.check_video_encoding_completed():
                # v0.5.1: flush writers to disk BEFORE upload (see
                # _finalize_dataset) — this is the auto-complete path that fires
                # once the target episode count is reached.
                finalized = self._finalize_dataset()
                if (finalized and self._task_info.push_to_hub and
                        self._record_episode_count > 0):
                    self._upload_dataset(
                        self._task_info.tags,
                        self._task_info.private_mode)
                return self.RECORD_COMPLETED

        return self.RECORDING

    def save(self) -> bool:
        """Commit the in-flight episode to disk.

        Returns True when the episode was committed (or there was nothing to
        commit), False when the episode was DISCARDED for automatic re-recording
        because the streaming encoder dropped frames (see
        _discard_episode_for_redo). Callers must only latch ``_on_saving = True``
        when this returns True — a False return has already routed the state
        machine back to 'reset'.
        """
        # An EMPTY buffer is nothing to commit. LeRobot 0.5.1's writer starts
        # with a size-0 buffer (not None) and save_episode() RAISES on it, which
        # turned a FINISH/MOVE_TO_NEXT in the first warm-up into an error stop.
        if not self._buffer_has_frames():
            return True
        self._last_discard_cause = None
        episode_no = self._record_episode_count + 1
        # Streaming frame-drop guard: with streaming_encoding=True the encoder
        # silently drops camera frames under CPU overload while add_frame still
        # appended a parquet row each tick — so the encoded video would be
        # SHORTER than the data parquet and LeRobot would raise
        # FrameTimestampError at train time. Round 5: detected through
        # LeRobot's PUBLIC warning (the wrapper's frame-drop watch, re-armed at
        # the take's first frame), BEFORE save_episode() commits.
        try:
            dropped = self._lerobot_dataset.streaming_dropped_frame_count()
        except Exception:  # noqa: BLE001 — detection must never block recording
            dropped = 0
        redos = self._redos.get(episode_no, 0) if hasattr(self, '_redos') else 0
        if dropped > 0:
            # Never kept with lost frames: once the shared cap is reached the
            # session ends (C7).
            if redos >= MAX_REDOS_PER_EPISODE:
                self._end_for_frame_loss(episode_no)
                return False
            if hasattr(self, '_redos'):
                self._redos[episode_no] = redos + 1
            self._discard_episode_for_redo(dropped)
            return False
        # O2 + C6: a source was silent >= SOURCE_GAP_S on its own timeline
        # inside this take (the node's capture integrity noted it): re-record
        # while the shared cap allows, then keep it with a warning.
        gap = getattr(self, '_take_gap_source', None)
        if gap is not None:
            if redos < MAX_REDOS_PER_EPISODE:
                if hasattr(self, '_redos'):
                    self._redos[episode_no] = redos + 1
                self._discard_episode_for_gap(gap, episode_no)
                return False
            kind, name = gap
            self._last_warning_message = record_texts_de.source_gap_kept_de(
                kind, name, episode_no)
            print(f'[WARNUNG] {self._last_warning_message}', file=sys.stderr, flush=True)
        # v2.5.0: with streaming_encoding=True + parallel_encoding=False the
        # video files are fully written by the time save_episode() returns, so
        # there is no async encoder snapshot to take here. The mp4s are checked
        # for real, after the save, in _verify_saved_video_files() against the
        # v3.0 on-disk layout — see that method.
        if self._task_info.use_optimized_save_mode:
            if not self._single_task:
                self._lerobot_dataset.save_episode_without_video_encoding()
            else:
                self._lerobot_dataset.save_episode_without_write_image()
        else:
            if self._lerobot_dataset.episode_buffer['size'] > 0:
                self._lerobot_dataset.save_episode()
        self._commit_count = getattr(self, '_commit_count', 0) + 1
        if hasattr(self, '_redos'):
            self._redos.pop(episode_no, None)
        return True

    def _discard_episode_for_gap(self, gap, episode_no) -> None:
        """O2 + C6: discard the take for its source gap and route to
        re-recording the same episode (like the frame-drop re-record)."""
        kind, name = gap
        warning = record_texts_de.source_gap_de(kind, name, episode_no)
        self._last_warning_message = warning
        self._last_discard_cause = ('gap', kind, name, episode_no)
        print(f'[WARNUNG] {warning}', file=sys.stderr, flush=True)
        self._stop_save_completed = False
        self._on_saving = False
        self._episode_reset()
        self._status = 'reset'

    def _end_for_frame_loss(self, episode_no) -> None:
        """C7: a frame loss once the episode's shared re-record cap is spent
        (any mix of gaps and losses before it) ends the session like
        „Beenden“: the take is dropped (official discard in the next record
        step), saved episodes are kept, finalize + upload per the usual guards
        (the finish branch). The sentence counts no kind (round 7)."""
        warning = record_texts_de.frame_loss_end_de(episode_no)
        self._last_warning_message = warning
        self._last_discard_cause = ('frame_loss_end', episode_no)
        print(f'[WARNUNG] {warning}', file=sys.stderr, flush=True)
        self._stop_save_completed = False
        self._finish_count_pending = False
        self._finish_drops_run = False
        self._episode_reset()
        self._status = 'finish'
        self._proceed_time = 0
        self._on_saving = True

    def _finish_after_discard(self) -> None:
        """A FINISH (or STOP) whose take save() discarded is not re-recorded:
        complete with the episodes already saved, and say why."""
        cause = getattr(self, '_last_discard_cause', None) or ('frame_loss',)
        if cause[0] == 'gap':
            _, kind, name, episode_no = cause
            self._last_warning_message = record_texts_de.source_gap_finish_de(
                kind, name, episode_no)
        elif cause[0] != 'frame_loss_end':
            self._last_warning_message = record_texts_de.frame_loss_finish_de(
                self._record_episode_count + 1)
        self._status = 'finish'
        self._finish_count_pending = False
        self._proceed_time = 0
        self._on_saving = True

    def _discard_episode_for_redo(self, dropped_frames: int) -> None:
        """Discard the current in-flight episode and route to re-recording it.

        Called from save() when the streaming encoder dropped frames: the video
        for this episode would be shorter than its parquet rows. We cancel the
        streaming episode (drops the temp mp4), clear the in-RAM buffer, and set
        _status='reset' so the SAME episode index is re-recorded — the episode
        count is NOT incremented, so nothing desynced ever reaches disk. Mirrors
        re_record()'s transition.
        """
        episode_no = self._record_episode_count + 1
        # The count is a lower bound (LeRobot logs the 1st drop, then every
        # 10th), so the sentence names no number.
        warning = record_texts_de.frame_loss_redo_de(episode_no)
        self._last_warning_message = warning
        self._last_discard_cause = ('frame_loss', episode_no)
        print(f'[WARNUNG] {warning} ({dropped_frames}+ dropped)', file=sys.stderr, flush=True)
        # Official discard only (round 5, O6): _episode_reset leaves
        # _discard_pending and the next record step (the reset tick, no frame)
        # cancels the take through the wrapper's discard_episode().
        self._stop_save_completed = False
        self._on_saving = False
        self._episode_reset()
        self._status = 'reset'

    def _finalize_dataset(self) -> bool:
        """Flush LeRobot's writers so the on-disk dataset is actually complete.

        LeRobot v0.5.1 keeps the data ParquetWriter open and buffers per-episode
        metadata (DatasetMetadata._metadata_buffer, default size 10) until
        finalize() runs. Without an explicit finalize() a recording of fewer
        than 10 episodes ships a data parquet with NO footer (unreadable by
        pyarrow/datasets) and NO meta/episodes/*.parquet at all — yet info.json
        and the mp4 files still look valid, so the corruption is silent and only
        surfaces when Modal training (or a local re-read) tries to load the
        dataset. Must run once after the last save_episode() and before upload.
        Idempotent: LeRobotDataset.finalize() guards on its _is_finalized flag.

        Returns True when the dataset is finalized (or was already); False when
        finalize() raised — in which case the caller MUST skip the upload, since
        the on-disk files would be incomplete — or when no dataset exists (then
        there is nothing to upload either).
        """
        ds = self._lerobot_dataset
        if ds is None:
            # No dataset was ever created: the session ended (FINISH, STOP or a
            # forced collision recovery) while the node still waited for sensor
            # data. Nothing to finalize or upload — but the crash marker the
            # first record tick wrote must go, or the next boot reports a
            # crashed session that never recorded anything.
            self._clear_session_marker()
            return False
        try:
            ds.finalize()
            # The session completed cleanly — drop the crash marker so the
            # next boot raises no stale-session notice.
            self._clear_session_marker()
            return True
        except Exception as e:
            warning = record_texts_de.FINALIZE_FAILED_DE
            self._last_warning_message = warning
            self._upload_blocked_reason_de = warning
            print(f'[FEHLER] {warning} ({e})', file=sys.stderr, flush=True)
            return False

    def _verify_saved_video_files(self):
        """After save_episode() returns, verify each camera produced a
        non-empty video file on disk.

        LeRobot v0.5.1 (dataset codebase v3.0) writes one *concatenated* mp4
        per video key at <root>/videos/<video_key>/chunk-NNN/file-NNN.mp4 —
        several episodes share a file, so the exact per-episode filename is not
        predictable from here. (The pre-v2.5.0 v2.1 layout
        videos/chunk-NNN/<key>/episode_NNNNNN.mp4 no longer exists; checking
        it produced false-positive "muss neu aufgenommen werden" errors on
        every single episode.) We therefore verify, per camera, that the key's
        video directory holds at least one non-empty .mp4 — which still catches
        the catastrophic "no video was written at all" case without
        false-positiving on the concatenated layout. The warning is surfaced in
        German on TaskStatus.error; it never blocks the save.
        """
        ds = self._lerobot_dataset
        if ds is None:
            return
        try:
            root = Path(str(getattr(ds, 'root', '') or ''))
            meta = getattr(ds, 'meta', None)
            video_keys = list(getattr(meta, 'video_keys', []) or []) if meta else []
        except Exception:
            return
        if not str(root) or not video_keys:
            return
        missing: list = []
        for key in video_keys:
            key_dir = root / 'videos' / key
            try:
                has_video = key_dir.is_dir() and any(
                    p.is_file() and p.stat().st_size > 0
                    for p in key_dir.rglob('*.mp4')
                )
            except OSError:
                has_video = False
            if not has_video:
                missing.append(key.replace('observation.images.', ''))
        if missing:
            warning = record_texts_de.missing_video_de(
                self._record_episode_count + 1, missing)
            self._last_warning_message = warning
            print(f'[FEHLER] {warning}', file=sys.stderr, flush=True)
            return
        self._verify_saved_lengths(meta, video_keys)

    def _verify_saved_lengths(self, meta, video_keys):
        """Round 5 post-save length check (detect + inform, like the missing-
        video check above): every camera's video span of the episode just
        saved (``meta.latest_episode``: to_timestamp - from_timestamp) must
        equal length / fps within half a frame — exactly LeRobot's train-time
        FrameTimestampError condition. Never raises, never blocks the save."""
        try:
            latest = getattr(meta, 'latest_episode', None)
            if not latest:
                return
            fps = float(getattr(meta, 'fps', 0) or self._record_fps())

            def _first(value):
                return value[0] if isinstance(value, (list, tuple)) else value

            length = float(_first(latest['length']))
            expected = length / fps
            mismatched = []
            for key in video_keys:
                start = latest.get(f'videos/{key}/from_timestamp')
                end = latest.get(f'videos/{key}/to_timestamp')
                if start is None or end is None:
                    continue
                span = float(_first(end)) - float(_first(start))
                if abs(span - expected) > SAVED_LENGTH_TOLERANCE_FRAMES / fps:
                    mismatched.append(key.replace('observation.images.', ''))
        except Exception as e:  # noqa: BLE001 — a check must never break the save
            print(f'saved-length check skipped: {e}', file=sys.stderr, flush=True)
            return
        if mismatched:
            warning = record_texts_de.saved_length_mismatch_de(
                self._record_episode_count + 1, mismatched)
            self._last_warning_message = warning
            print(f'[FEHLER] {warning}', file=sys.stderr, flush=True)

    def create_frame(
            self,
            images: dict,
            state: list,
            action: list) -> dict:

        frame = {}
        for camera_name, image in images.items():
            frame[f'observation.images.{camera_name}'] = image
        frame['observation.state'] = np.array(state, dtype=np.float32)
        frame['action'] = np.array(action, dtype=np.float32)
        self.current_instruction = self._task_info.task_instruction[
            self._current_task % len(self._task_info.task_instruction)
        ]
        return frame

    def _buffer_size(self) -> int:
        ds = getattr(self, '_lerobot_dataset', None)
        buf = getattr(ds, 'episode_buffer', None) if ds is not None else None
        if buf is None:
            return 0
        try:
            return int(buf.get('size', 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return 0

    def _buffer_has_frames(self) -> bool:
        return self._buffer_size() > 0

    def _record_fps(self) -> float:
        try:
            fps = float(getattr(self._task_info, 'fps', 0) or 0)
        except (TypeError, ValueError):
            fps = 0.0
        return fps if fps > 0 else 30.0

    def _run_age_s(self) -> float:
        entered = getattr(self, '_run_entered_at', None)
        if entered is None:
            return float('inf')
        return time.perf_counter() - entered

    def _stamp_run_entry(self) -> None:
        """A run starts NOW: _run_entered_at (perf_counter, the Q3/Q4 and
        'too_early' clock) and run_entered_mono (monotonic, the slot sampler's
        take boundary) are always stamped together."""
        self._run_entered_at = time.perf_counter()
        self.run_entered_mono = time.monotonic()

    def _enter_run(self) -> None:
        self._status = 'run'
        self._start_time_s = 0
        self._proceed_time = 0
        self._stamp_run_entry()

    def _drop_in_progress_episode(self) -> None:
        # Official discard only (round 5, O6): _episode_reset leaves
        # _discard_pending; the next record step's cancel_pending_discard runs
        # the wrapper's discard_episode() (before the finish branch finalizes).
        self._episode_reset()

    # ── The recorder lock (round 5, O1) ───────────────────────────────────────

    def _recorder_lock(self):
        lock = getattr(self, 'lock', None)
        if lock is None:
            # A DataManager built via __new__ (the deps-free contract tests).
            lock = threading.RLock()
            self.lock = lock
        return lock

    @contextmanager
    def locked(self):
        """Hold the recorder lock; on release, apply a collision discard that
        arrived meanwhile (the trip never waits for the lock)."""
        lock = self._recorder_lock()
        lock.acquire()
        try:
            yield self
        finally:
            self._release_and_drain(lock)

    @contextmanager
    def try_locked(self, timeout):
        """The command path's bounded acquire (O6): yields True while the lock
        is held, or False — having changed nothing — when it could not be taken
        within ``timeout`` (a record step in flight, e.g. the official ~1 s
        discard cancel); /task/command then answers „beschäftigt“ instead of
        blocking the default callback group. Drains at release like locked()."""
        lock = self._recorder_lock()
        if not lock.acquire(timeout=max(0.0, float(timeout))):
            yield False
            return
        try:
            yield True
        finally:
            self._release_and_drain(lock)

    def _release_and_drain(self, lock):
        # Drain, release, and re-check: a request set between the holder's last
        # drain and its release (the requester's non-blocking acquire failed)
        # is applied by whoever re-acquires here, never lost. The collision
        # discard goes first: a take a collision interrupted is discarded,
        # never saved by an end queued beside it (Rule §2).
        while True:
            try:
                self._drain_collision_request_locked()
                self._drain_end_request_locked()
            finally:
                lock.release()
            if not (getattr(self, '_collision_discard_requested', False)
                    or getattr(self, '_end_requested', None)):
                break
            if not lock.acquire(blocking=False):
                break

    def request_collision_discard(self) -> bool:
        """The collision trip's discard; NEVER blocks the caller.

        Applied now (True) when the recorder lock is free, else (False) by the
        holder when it releases — at the end of the record step in flight. Same
        semantics as re_record() (F7b, Q7 unchanged)."""
        self._collision_discard_requested = True
        lock = self._recorder_lock()
        if lock.acquire(blocking=False):
            self._release_and_drain(lock)
            return True
        return False

    def _drain_collision_request_locked(self) -> None:
        if getattr(self, '_collision_discard_requested', False):
            self._collision_discard_requested = False
            self.re_record()

    def request_end(self, kind='finish') -> bool:
        """F1 (round 6, owner: the server queues the end): FINISH or STOP
        from /task/command; NEVER waits for the recorder lock and is never
        refused as busy. Applied now (True) when the lock is free, else (False)
        queued and applied the moment the holder releases it — e.g. right after
        the official ~1 s discard that „Wiederholen“ started, so „Verwerfen und
        beenden“ always ends the session (Q3/Q4 judged when it is applied)."""
        self._end_requested = 'stop' if kind == 'stop' else 'finish'
        lock = self._recorder_lock()
        if lock.acquire(blocking=False):
            self._release_and_drain(lock)
            return True
        return False

    def discard_in_flight(self) -> bool:
        """A discarded take still has to be cancelled, or its official cancel
        is running right now (round 7: the only case the command path answers
        a queued FINISH with FINISH_QUEUED_DE). Read without the lock: two
        plain flags, a snapshot is enough for the answer's wording."""
        return bool(getattr(self, '_discard_pending', False)
                    or getattr(self, '_discarding', False))

    def _drain_end_request_locked(self) -> None:
        kind = getattr(self, '_end_requested', None)
        if kind:
            self._end_requested = None
            if kind == 'stop':
                self.record_stop()
            else:
                self.record_finish()

    def saved_episode_count(self) -> int:
        return int(getattr(self, '_record_episode_count', 0))

    @_recorder_locked
    def end_after_error(self):
        """D5 (owner): an error stop ends the session WITHOUT losing what was
        saved. With a dataset: count an episode a latched save already
        committed (the Q7/STOP rule), drop the running take through the
        official discard, finalize — and upload NOTHING (the student uploads
        from the Daten tab). Afterwards the DataManager is inert. Returns True
        when the dataset was finalized (the crash marker is cleared by the
        finalize); False when finalize failed (F3, round 6: the marker STAYS —
        the dataset is incomplete — and the German reason is on
        _upload_blocked_reason_de); None without a dataset (nothing changes)."""
        if getattr(self, '_lerobot_dataset', None) is None:
            return None
        status = self._status
        if getattr(self, '_on_saving', False) and (
                (status == 'save' and getattr(self, '_save_count_pending', True))
                or (status == 'finish' and getattr(self, '_finish_count_pending', False))
                or (status == 'stop' and not getattr(self, '_stop_save_completed', False)
                    and getattr(self, '_stop_count_pending', False))):
            self._verify_saved_video_files()
            self._record_episode_count += 1
            self._write_session_marker()
        self._finish_count_pending = False
        self._stop_count_pending = False
        self._finish_drops_run = False
        self._on_saving = False
        self._episode_reset()
        self.cancel_pending_discard()
        self._status = 'stop'
        self._stop_save_completed = True
        self._upload_enqueued = True          # never upload after an error stop
        return self._finalize_dataset()

    @_recorder_locked
    def record_early_save(self) -> str:
        """MOVE_TO_NEXT (single task) and the joystick right tact.

        Returns what happened, for the node's German answer:
          'run'       a warm-up/reset was skipped, the next run starts now;
          'save'      the running episode (>= EARLY_SAVE_MIN_S) is saved now;
          'too_early' the run is younger than EARLY_SAVE_MIN_S, nothing changed;
          ''          nothing to skip or save (saving, finishing), nothing changed.
        """
        if self._status in ('warmup', 'reset'):
            self._enter_run()
            return 'run'
        if self._status == 'run' and self._buffer_has_frames():
            if self._run_age_s() < EARLY_SAVE_MIN_S:
                return 'too_early'
            self._status = 'save'
            return 'save'
        return ''

    @_recorder_locked
    def rerecord_from_command(self) -> bool:
        """RERECORD from the wire („Wiederholen“, „Verwerfen und beenden“).

        Refused (False, nothing changed) while finishing/stopping and once
        save() has committed the episode — it can no longer be discarded.
        Otherwise re_record() and remember WHEN, for the FINISH window (Q4).
        """
        if self._status in ('finish', 'stop'):
            return False
        if self._status == 'save' and getattr(self, '_on_saving', False):
            return False
        self.re_record()
        self._wire_rerecord_at = time.perf_counter()
        return True

    def _run_started_after_wire_rerecord(self) -> bool:
        rerec = getattr(self, '_wire_rerecord_at', None)
        entered = getattr(self, '_run_entered_at', None)
        if rerec is None or entered is None:
            return False
        return (time.perf_counter() - rerec <= RERECORD_FINISH_WINDOW_S
                and entered >= rerec)

    @_recorder_locked
    def finish_for_low_disk(self, message_de: str) -> bool:
        """End the session because the disk is nearly full (once per session).

        Only while nothing is being saved: warm-up, run or reset. The German
        sentence rides the next status tick as a [WARNUNG].
        """
        if getattr(self, '_disk_stop_requested', False):
            return False
        if self._status not in ('warmup', 'run', 'reset'):
            return False
        self._disk_stop_requested = True
        self._last_warning_message = message_de
        self.record_finish()
        return True

    @_recorder_locked
    def end_for_source_stop(self, message_de: str) -> bool:
        """R5-2: a required source stopped (no message for SOURCE_STOPPED_S):
        end the session like „Verwerfen und beenden“ — measured 2.99–3.03 s
        after the source fell silent (2 s silence + the ~1 s official discard of
        the running take); the owner's bound is ≤ 3.5 s. The take in flight is
        dropped whatever its length, saved episodes are kept, finalize + upload
        by the usual guards. Once per session, only in warm-up/run/reset; the
        German sentence rides the next status tick as a [WARNUNG]."""
        if getattr(self, '_source_stop_requested', False):
            return False
        if self._status not in ('warmup', 'run', 'reset'):
            return False
        self._source_stop_requested = True
        self._last_warning_message = message_de
        in_flight = self._status == 'run'
        self.record_finish()
        if in_flight:
            self._finish_drops_run = True
        return True

    @_recorder_locked
    def note_take_gap(self, gap) -> None:
        """O2 + C6: the node's capture integrity names the source whose gap
        (on its own timeline, >= SOURCE_GAP_S) makes the running take a
        re-record — ``(kind, name)`` or None. Read by save() before the commit;
        cleared with the take (_episode_reset)."""
        if getattr(self, '_on_saving', False):
            return
        if self._status in ('run', 'save', 'finish', 'stop'):
            self._take_gap_source = tuple(gap) if gap is not None else None

    def added_frame(self) -> bool:
        """Did the last record() call add a frame to the take?"""
        return bool(getattr(self, '_frame_added', False))

    def commit_count(self) -> int:
        """Takes committed to disk so far (a change closes the take)."""
        return int(getattr(self, '_commit_count', 0))

    @_recorder_locked
    def end_session_now(self) -> bool:
        """F1: finish the session synchronously (forced collision recovery).

        The record timer is stopped at that point, so the finish/stop branches
        are driven here; they never touch images, hence the None arguments.
        Returns whether the session completed (finalized, upload handed off
        per the usual guards).
        """
        if self._status not in ('finish', 'stop'):
            self.record_finish()
        for _ in range(4):
            if self.record(None, None, None) == self.RECORD_COMPLETED:
                return True
        return False

    @_recorder_locked
    def record_stop(self):
        status = self._status
        if status in ('save', 'finish') and getattr(self, '_on_saving', False):
            # The 'stop' branch finds _on_saving latched and goes straight to
            # completion: count the episode save() already committed (a latched
            # 'save' always did; a latched 'finish' only when it committed one).
            # HEAD counted and uploaded it; without this it stayed on disk,
            # uncounted and never uploaded.
            self._stop_count_pending = (
                (status == 'save' and getattr(self, '_save_count_pending', True))
                or getattr(self, '_finish_count_pending', False))
            self._finish_count_pending = False
        self._status = 'stop'

    @_recorder_locked
    def record_finish(self):
        status = self._status
        if status in ('finish', 'stop'):
            return
        if status == 'save' and getattr(self, '_on_saving', False):
            # Committed by 'save', not yet counted: the finish branch counts it
            # (I2j: only when that save really committed frames).
            self._finish_count_pending = getattr(self, '_save_count_pending', True)
        in_flight = status == 'run' or (
            status == 'save' and not getattr(self, '_on_saving', False))
        self._finish_drops_run = in_flight and (
            (status == 'run' and self._run_age_s() < EARLY_SAVE_MIN_S)
            or self._run_started_after_wire_rerecord())
        self._status = 'finish'

    @_recorder_locked
    def re_record(self):
        # F7b (owner-approved collision-path change): a session that is already
        # finishing/stopping is never reopened — a collision during the finish
        # lets the finish complete normally.
        if self._status in ('finish', 'stop'):
            return
        # Q7 (owner-approved): a collision between save()'s commit and the
        # count (status 'save', _on_saving latched) used to leave the episode on
        # disk but uncounted. Complete the count exactly as the normal
        # save-completion branch does, THEN rewind as before. The collision
        # still halts and resumes the same session; nothing new is discarded.
        # (The buffer reset of that branch is the rewind's own _episode_reset.)
        # When that was the LAST episode, finish as that branch does: a literal
        # rewind to 'reset' records no frame, but it publishes a phantom
        # RESETTING („Episode N+1 von N“) for the whole Zurücksetzen time and a
        # RECORDING tick before the session completes.
        if (self._status == 'save' and getattr(self, '_on_saving', False)
                and getattr(self, '_save_count_pending', True)):
            self._verify_saved_video_files()
            self._record_episode_count += 1
            self._write_session_marker()
            self._get_current_scenario_number()
            self._current_task += 1
            if self._record_episode_count >= self._task_info.num_episodes:
                self._stop_save_completed = False
                self._on_saving = False
                self._episode_reset()
                self._status = 'finish'
                return
        self._stop_save_completed = False
        # Abandon any in-flight save: re_record means "discard the current episode and
        # restart it". If a collision (or a manual Wiederholen) fires while _status=='save'
        # with _on_saving latched True, leaving it set would make the NEXT 'save' tick skip
        # save() and jump straight to the encoding-complete branch — counting an episode whose
        # frames were never written. Clearing it here keeps the re-recorded episode honest.
        self._on_saving = False
        self._episode_reset()
        self._status = 'reset'

    @_recorder_locked
    def record_skip_task(self):
        self._stop_save_completed = False
        self._episode_reset()
        self._status = 'skip_task'
        self._get_current_scenario_number()
        self._current_task += 1

    @_recorder_locked
    def record_next_episode(self):
        self._status = 'save'

    @_recorder_locked
    def get_current_record_status(self):
        current_status = TaskStatus()
        current_status.robot_type = self._robot_type
        current_status.task_info = self._task_info

        if self._status == 'warmup':
            current_status.phase = TaskStatus.WARMING_UP
            current_status.total_time = int(self._task_info.warmup_time_s)
        elif self._status == 'run':
            current_status.phase = TaskStatus.RECORDING
            current_status.total_time = int(self._task_info.episode_time_s)
        elif self._status == 'reset':
            current_status.phase = TaskStatus.RESETTING
            current_status.total_time = int(self._task_info.reset_time_s)
        elif self._status == 'save' or self._status == 'finish':
            is_saving, encoding_progress = self._get_encoding_progress()
            current_status.phase = TaskStatus.SAVING
            current_status.total_time = int(0)
            self._proceed_time = int(0)
            if is_saving:
                current_status.encoding_progress = encoding_progress
            else:
                current_status.encoding_progress = 0.0
        elif self._status == 'stop':
            is_saving, encoding_progress = self._get_encoding_progress()
            current_status.total_time = int(0)
            self._proceed_time = int(0)
            if is_saving:
                current_status.phase = TaskStatus.SAVING
                current_status.encoding_progress = encoding_progress
            else:
                current_status.phase = TaskStatus.STOPPED

        current_status.current_task_instruction = self.current_instruction
        current_status.proceed_time = int(getattr(self, '_proceed_time', 0))
        current_status.current_episode_number = int(self._record_episode_count)

        # Propagate the last non-fatal warning (e.g. RAM truncation) into
        # TaskStatus.error with a [WARNUNG] prefix so the React UI can
        # render it distinctly from hard errors. Without this, truncation
        # was invisible to the student. Clear-on-read so a persistent
        # warning surfaces exactly once per occurrence: if a new
        # truncation/mismatch happens, the warning is re-set by record()
        # and re-surfaced on the next status tick.
        if self._last_warning_message:
            current_status.error = f'[WARNUNG] {self._last_warning_message}'
            self._last_warning_message = ''

        total_storage, used_storage = StorageChecker.get_storage_gb('/')
        current_status.used_storage_size = float(used_storage)
        current_status.total_storage_size = float(total_storage)

        current_status.used_cpu = float(self._cpu_checker.get_cpu_usage())

        ram_total, ram_used = RAMChecker.get_ram_gb()
        current_status.used_ram_size = float(ram_used)
        current_status.total_ram_size = float(ram_total)
        if not self._single_task:
            current_status.current_scenario_number = self._current_scenario_number

        return current_status

    def _get_current_scenario_number(self):
        task_count = len(self._task_info.task_instruction)
        if task_count == 0:
            return
        next_task_index = (self._current_task + 1) % task_count
        if next_task_index == 0:
            self._current_scenario_number += 1

    def _get_encoding_progress(self):
        # v2.5.0: streaming_encoding=True + parallel_encoding=False means
        # save_episode() encodes synchronously and only returns once the
        # episode's video is fully written. There is no async per-camera
        # encoder to poll (the v2.4 self.encoders dict is gone in LeRobot
        # v0.5.1), so the SAVING phase is effectively instantaneous. Report
        # "not saving / 100%" so the React UI never renders a progress bar
        # that can't move.
        return False, 100.0

    def _check_stale_cameras(self, camera_data: dict) -> str | None:
        """Hash sparse byte slices of each decoded camera frame to detect
        a frozen feed. Mirrors overlays/inference_manager.py logic so
        recording and inference treat dead cameras the same way. Returns
        the camera name once it has been frozen >_stale_halt_threshold_s,
        or None when fresh.
        """
        now = time.monotonic()
        halt_on: str | None = None
        for name, img in camera_data.items():
            # v2.5.0: hash the decoded RGB ndarray's bytes. Streaming encoding
            # means the recording buffer never holds compressed JPEG, so the
            # JpegFrame branch is gone.
            buf = img.tobytes() if hasattr(img, 'tobytes') else bytes(img)
            n = len(buf)
            if n <= 1024:
                sample = buf
            else:
                slice_size = 256
                offsets = (0, n // 4, n // 2, (3 * n) // 4)
                sample = b''.join(buf[o:o + slice_size] for o in offsets)
            h = hash(sample)
            prev = self._last_image_hashes.get(name)
            if prev != h:
                self._last_image_hashes[name] = h
                self._last_image_change_time[name] = now
                continue
            last_change = self._last_image_change_time.get(name, now)
            if now - last_change > self._stale_halt_threshold_s and halt_on is None:
                halt_on = name
        return halt_on

    def convert_msgs_to_raw_datas(
            self,
            image_msgs,
            follower_msgs,
            total_joint_order,
            leader_msgs=None,
            leader_joint_order=None) -> tuple:

        camera_data = {}
        follower_data = []
        leader_data = []

        if image_msgs is not None:
            for key, value in image_msgs.items():
                # v2.5.0: always decode to RGB ndarray. streaming_encoding=True
                # bounds in-RAM growth at the encoder boundary, so the v2.4
                # JpegFrame compressed-bytes optimization is no longer needed.
                # cv_bridge handles the BGR→RGB swap in-decoder when we ask
                # for rgb8, saving one full-frame allocation + memcpy per
                # camera per tick.
                camera_data[key] = self.data_converter.compressed_image2cvmat(
                    value, desired_encoding='rgb8')
            stale = self._check_stale_cameras(camera_data)
            if stale is not None:
                # Warn (don't halt) — slow precision demos legitimately
                # produce static scenes for >5 s (insertion, alignment,
                # waiting for a human to place an object). Aborting the
                # episode here was the single most-frequent false
                # positive against real workflows; at recording time
                # the worst case is a degraded frame, not a hardware
                # event. (The inference path currently has NO stale-
                # camera halt of its own — this warning fires there too
                # via convert_msgs_to_raw_datas, but is warn-only.)
                if getattr(self, '_session_marker_enabled', False):
                    # A RECORDING session (the marker is armed for
                    # START_RECORD only): the Aufnahme page shows this in its
                    # problem banner, so say it in its words and at most once
                    # per 5 s instead of on every tick.
                    now_mono = time.monotonic()
                    last = getattr(self, '_last_stale_warn_mono', 0.0)
                    if now_mono - last >= 5.0:
                        self._last_stale_warn_mono = now_mono
                        warning = record_texts_de.stale_camera_recording_de(
                            stale, self._stale_halt_threshold_s)
                        self._last_warning_message = warning
                        print(f'[WARNUNG] {warning}', file=sys.stderr, flush=True)
                else:
                    # Inference keeps HEAD's sentence byte for byte (F3).
                    warning = (
                        f'Kamera "{stale}" liefert seit über '
                        f'{self._stale_halt_threshold_s:.0f}s dasselbe Bild. '
                        f'Aufnahme läuft weiter — bitte prüfen, ob die '
                        f'Szene wirklich statisch ist oder die Kamera hängt.'
                    )
                    self._last_warning_message = warning
                    print(f'[WARNUNG] {warning}', file=sys.stderr, flush=True)
        if follower_msgs is not None:
            for key, value in follower_msgs.items():
                if value is not None:
                    follower_data.extend(self.joint_msgs2tensor_array(
                        value, total_joint_order))
        if leader_msgs is not None:
            for key, value in leader_joint_order.items():
                # remove joint_order. from key
                prefix_key = key.replace('joint_order.', '')
                if prefix_key not in leader_msgs:
                    return camera_data, follower_data, None
                elif leader_msgs[prefix_key] is not None:
                    leader_data.extend(self.joint_msgs2tensor_array(
                        leader_msgs[prefix_key], value))
                else:
                    return camera_data, follower_data, None

        return camera_data, follower_data, leader_data

    def joint_msgs2tensor_array(self, msg_data, joint_order=None):
        if isinstance(msg_data, JointTrajectory):
            return self.data_converter.joint_trajectory2tensor_array(
                msg_data, joint_order)
        elif isinstance(msg_data, JointState):
            return self.data_converter.joint_state2tensor_array(
                msg_data, joint_order)
        elif isinstance(msg_data, Odometry):
            return self.data_converter.odometry2tensor_array(msg_data)
        elif isinstance(msg_data, Twist):
            return self.data_converter.twist2tensor_array(msg_data)
        else:
            raise ValueError(f'Unsupported message type: {type(msg_data)}')

    @_recorder_locked
    def cancel_pending_discard(self) -> bool:
        """Cancel a discarded take's streaming encoder now, if one is pending.

        Called at the top of every record tick, and by the collision monitor in
        _on_resync_complete BEFORE it releases the arm (/collision_flag=False),
        so the ~0.9 s never lands after teleop resumed — the record timer and
        the collision detector share one callback group. Idempotent: returns
        True only when it cancelled; a second call (the next tick) is a no-op.
        """
        if not getattr(self, '_discard_pending', False):
            return False
        self._discard_pending = False
        self._discarding = True
        try:
            self._cancel_discarded_take()
        finally:
            self._discarding = False
        # „Jetzt starten“ may have entered 'run' between the discard and now:
        # the take really starts after the cancel, so its clock starts here —
        # else Q3 (< 1 s is dropped) and 'too_early' see a run ~1 s too old,
        # and the slot sampler's take boundary would include the cancel.
        if self._status == 'run':
            self._stamp_run_entry()
        return True

    def _cancel_discarded_take(self) -> None:
        """Cancel a discarded take's streaming encoder the way LeRobot's own
        record loop does: through the public clear_episode_buffer() (the
        wrapper's discard_episode), which cancels the encoder at once and leaves
        a fresh buffer.

        Why not lazily: DataManager used to only drop the buffer, so LeRobot's
        start_episode() cancelled the stale encoder on the FIRST FRAME of the
        next take. That cancel waits for the encoder threads' 1 s queue timeout
        (measured ~0.9 s), and it landed inside the recording: the take's first
        two frames were ~0.9 s apart in real time but 1/30 s apart in the
        dataset.

        Why in the record tick and not where the discard happens: RERECORD
        arrives on /task/command and a collision trips in the gpio callback,
        and both share the node's default MutuallyExclusiveCallbackGroup with
        the collision detector and its relax-in-place timer — ~0.9 s there
        would delay the relax after a trip. The tick right after the discard is
        a reset (or discard) tick that records no frame. Never raises: at worst
        the next take's start_episode() cancels lazily, as before.
        """
        ds = getattr(self, '_lerobot_dataset', None)
        discard = getattr(ds, 'discard_episode', None) if ds is not None else None
        if discard is None:
            return
        try:
            discard()
        except Exception as e:  # noqa: BLE001 — a failed cancel must not stop the tick
            print(f'[WARNUNG] Die verworfene Episode konnte nicht sofort '
                  f'abgebrochen werden, die nächste holt das nach: {e}',
                  file=sys.stderr, flush=True)

    def _episode_reset(self):
        # An unsaved take is being thrown away: its streaming encoder is
        # cancelled at the top of the next record tick (_cancel_discarded_take).
        # After a save the buffer is fresh and the encoder already finished, so
        # the save path is untouched.
        if self._buffer_has_frames():
            self._discard_pending = True
        # `is not None`, never truthiness: LeRobotDataset.__len__ is the number
        # of SAVED frames, so a fresh dataset is falsy until its first episode
        # is saved — and every discard in that first episode (Wiederholen, a
        # collision, the frame-drop re-record, a dropped FINISH run) used to
        # leave its frames in the writer's buffer, to be saved with the next
        # episode. A fresh buffer also makes the writer restart the streaming
        # encoder (start_episode cancels the stale one), so video and parquet
        # stay in step.
        if (
            self._lerobot_dataset is not None
            and (hasattr(self._lerobot_dataset, 'episode_buffer')
                 or self._current_task == 0)
        ):
            if self._lerobot_dataset.episode_buffer is not None:
                for key, value in self._lerobot_dataset.episode_buffer.items():
                    if isinstance(value, list):
                        value.clear()
                    del value
                self._lerobot_dataset.episode_buffer.clear()
            self._lerobot_dataset.episode_buffer = None
        self._start_time_s = 0
        self._take_gap_source = None
        # Drop the stale-camera hashes so a re-recorded episode starts
        # fresh — otherwise the very first frame of the new episode would
        # always be flagged "same as last frame of previous episode" and
        # immediately advance the stale clock.
        self._last_image_hashes.clear()
        self._last_image_change_time.clear()
        # NOTE: _last_warning_message is deliberately NOT cleared here.
        # _episode_reset() runs inside the same record() tick that set the
        # warning (RAM truncation -> record_early_save -> save -> encoding
        # complete -> _episode_reset), so clearing here would wipe the
        # warning before get_current_record_status() — called from the
        # outer ROS timer — ever surfaces it to the UI. Instead, the
        # warning is cleared in get_current_record_status() after it has
        # been copied onto TaskStatus.error, which guarantees the student
        # sees it at least once.
        # Round 5: no explicit full garbage collection here any more (it ran on
        # every save, reset and discard): collecting the node's heap took
        # 73-105 ms holding the GIL, pausing every thread incl. the collision
        # callbacks. Refcounting frees the buffer; main() freezes the import heap.

    def _check_time(self, limit_time, next_status):
        self._proceed_time = time.perf_counter() - self._start_time_s
        if self._proceed_time >= limit_time:
            self._status = next_status
            self._start_time_s = 0
            self._proceed_time = 0
            if next_status == 'run':
                self._stamp_run_entry()
            return True
        else:
            return False

    def _check_dataset_exists(self, repo_id, root):
        """D14 at recording start (spec §C4). True = open the local dataset,
        False = create one (or, with ``_start_abandoned``, neither); a refusal
        sets ``_last_warning_message`` and raises HubCheckRefused. Runs in the
        record tick's own group, outside the recorder lock."""
        # 0. The dataset's lease: an upload of THIS dataset (the previous
        # session's auto-upload, a Daten upload, „Beide behalten“'s upload
        # stage) is waited for (R-8); an edit, a delete or a download refuses.
        reason = self._lease_reason(root)
        while reason == 'upload':
            if self.get_status() in ('finish', 'stop'):
                self._start_abandoned = True
                return False
            time.sleep(START_UPLOAD_POLL_S)
            reason = self._lease_reason(root)
        if reason in ('edit', 'delete', 'download'):
            print(f'Start refused for {repo_id}: the dataset is busy ({reason})',
                  file=sys.stderr, flush=True)
            self._last_warning_message = record_texts_de.DATASET_BUSY_START_DE
            raise HubCheckRefused(repo_id)

        # 1./2. The local copy.
        if os.path.exists(root):
            missing = [f for f in ('meta', 'videos', 'data') if not os.path.exists(os.path.join(root, f))]
            if not missing:
                if not self._task_info.push_to_hub:
                    return True
                return self._resume_against_hub(repo_id, root)
            print(f'Dataset {repo_id} is incomplete (missing {missing}), re-creating dataset.')
            shutil.rmtree(root)

        # 3. No local copy: D7, a LOGGED-IN existence check. A failure to ask is
        # never „absent“ (round 6, F4): no token / a token the hub refuses at
        # whoami -> the session runs WITHOUT upload; the repo query refused
        # (401/403) -> refused (HUB_CHECK_AUTH_DE). An UNREACHABLE hub (network,
        # 429, 5xx, no answer within HUB_CHECK_TIMEOUT_S) no longer refuses
        # (D14): the session records a new local dataset, the first tick says
        # OFFLINE_START_DE, and the guarded upload decides at the end.
        if self._task_info.push_to_hub:
            verdict, error = self._hub_existence(repo_id)
            if verdict in ('no_token', 'token_refused'):
                self._upload_off(verdict, error, repo_id)
                return False
            if verdict == 'auth_refused':
                print(f'Hub existence check refused for {repo_id}: {error!r}', file=sys.stderr, flush=True)
                self._last_warning_message = record_texts_de.HUB_CHECK_AUTH_DE
                raise HubCheckRefused(repo_id) from error
            if verdict == 'unreachable':
                self._start_offline(repo_id, error)
                return False
            if verdict == 'exists':
                print(f'Dataset {repo_id} exists on Hugging Face, downloading...')
                try:
                    revision = self._download_dataset(repo_id)
                except StartAbandoned:
                    self._start_abandoned = True
                    return False
                except HubCheckRefused:
                    raise
                except Exception as e:  # noqa: BLE001 — never fall back to „new“
                    self._last_warning_message = (
                        hf_errors.hf_error_sentence_de(e) or record_texts_de.SYNC_DOWNLOAD_FAILED_DE)
                    raise HubCheckRefused(repo_id) from e
                if isinstance(revision, str) and _HEAD_SHA.match(revision):
                    self._sync_base = revision
                return True
            self._sync_base = None                        # absent: the hub holds no dataset
        return False

    def _lease_reason(self, root):
        """The busy kind of this dataset from the injected Daten lease, or None.
        A missing lease (no Daten) or ANY exception from it is „no lease“ with
        one log line: recording never depends on Daten (R-18)."""
        lease = getattr(self, '_dataset_lease', None)
        try:
            if lease is None:
                raise LookupError('no Daten lease injected')
            return lease(root)
        except Exception as e:  # noqa: BLE001
            if not getattr(self, '_lease_logged', False):
                self._lease_logged = True
                print(f'Daten lease not consulted ({type(e).__name__}: {e}); recording proceeds',
                      file=sys.stderr, flush=True)
            return None

    def _upload_off(self, verdict, error, repo_id):
        """F4 (round 6): no token / a refused token -> this session records
        WITHOUT upload (nothing on the hub can be overwritten), with a notice."""
        self._task_info.push_to_hub = False
        self._upload_off_notice_de = (
            record_texts_de.UPLOAD_OFF_NO_TOKEN_DE if verdict == 'no_token'
            else record_texts_de.UPLOAD_OFF_TOKEN_INVALID_DE)
        print(f'Hub check for {repo_id}: {verdict} ({error!r}); this session records WITHOUT upload',
              file=sys.stderr, flush=True)

    def _start_offline(self, repo_id, error):
        """D14's offline class: the session records, `_sync_base` stays
        SYNC_UNCHECKED and the end-of-session upload decides by itself."""
        self._start_notice_de = record_texts_de.OFFLINE_START_DE
        print(f'Hub check for {repo_id}: unreachable '
              f'({hf_errors.classify_hf_error(error) if error else "timeout"}: {error!r}); '
              f'recording offline, the upload decides at the end', file=sys.stderr, flush=True)

    def _resume_against_hub(self, repo_id, root):
        """A complete local copy with upload on: the ONE decision against the
        hub (spec §C4), bounded by HUB_CHECK_TIMEOUT_S. True = resume."""
        verdict = self._hub_decision(repo_id, root)
        kind = verdict['kind']
        if kind in ('no_token', 'token_refused'):
            self._upload_off(kind, verdict.get('error'), repo_id)
            return True
        if kind == 'auth_refused':
            print(f'Hub check refused for {repo_id}: {verdict.get("error")!r}', file=sys.stderr, flush=True)
            self._last_warning_message = record_texts_de.HUB_CHECK_AUTH_DE
            raise HubCheckRefused(repo_id)
        if kind == 'unreachable':
            self._start_offline(repo_id, verdict.get('error'))
            return True
        state, view, record = verdict['state'], verdict['view'], verdict['record']
        present = view.get('state') == 'present'
        head = view.get('head') if present else None
        print(f'Hub decision for {repo_id}: {state} (head {head})', flush=True)
        if state == 'local':
            if present or view.get('complete'):
                self._sync_base = None                    # no dataset online (an empty repo, or none)
            return True
        if state == 'unknown':                            # a namespace this token cannot see whole
            return True
        if state == 'current':
            self._sync_base = head
            repair = verdict.get('repair')
            if repair and (record or {}).get('hub_sha'):
                try:
                    dataset_sync.update_record(root, repo_id, **repair)
                except Exception as e:  # noqa: BLE001 — the next decision repairs it again
                    print(f'Record repair for {repo_id} failed: {e}', file=sys.stderr, flush=True)
            return True
        if state == 'changed':
            if present:
                self._sync_base = head
            return True
        if state == 'newer':
            try:
                revision = self._run_sync_download(repo_id, head, root)
            except StartAbandoned:
                self._start_abandoned = True
                return False
            self._sync_base = revision if isinstance(revision, str) and _HEAD_SHA.match(revision) else head
            return True
        self._last_warning_message = (
            record_texts_de.SYNC_CONFLICT_DE if (record or {}).get('hub_sha')
            else record_texts_de.SYNC_UNKNOWN_DE)
        raise HubCheckRefused(repo_id)

    def _hub_decision(self, repo_id, root):
        """Every network step of the resume decision in ONE worker thread joined
        after HUB_CHECK_TIMEOUT_S (a black-holed hub cannot hang the Start):
        whoami; the hub view at main's head (not-found FIRST: a
        RepositoryNotFoundError of any status is „absent“); the record; for a
        record-less dataset the recursive listing and the content decision.
        Returns {'kind': 'decided'|'no_token'|'token_refused'|'auth_refused'|
        'unreachable', 'state', 'view', 'record', 'repair', 'error'}."""
        result = {}

        def _ask():
            try:
                hub_sync = _sibling('hub_sync')
                api = HfApi()       # the rig's stored token
                try:
                    account = api.whoami()['name']
                except Exception as e:  # noqa: BLE001 — classified below
                    result.update(stage='whoami', error=e)
                    return
                complete = repo_id.split('/')[0] == account
                try:
                    view = hub_sync.hub_view(api, repo_id, complete=complete, strict=True)
                except Exception as e:  # noqa: BLE001
                    if not hub_sync.is_not_found(e):
                        result.update(stage='view', error=e)
                        return
                    view = {'state': 'absent', 'complete': complete}
                record = dataset_sync.own_record(root, repo_id)
                try:
                    state, _, repair = dataset_sync.decide(root, record, view)
                except Exception as e:  # noqa: BLE001 — the content decision's listing failed
                    result.update(stage='view', error=e)
                    return
                result.update(stage='done', state=state, view=view, record=record, repair=repair)
            except Exception as e:  # noqa: BLE001 — classified below
                result.update(stage='view', error=e)

        worker = threading.Thread(target=_ask, name='hub-sync-check', daemon=True)
        worker.start()
        worker.join(HUB_CHECK_TIMEOUT_S)
        if worker.is_alive():
            return {'kind': 'unreachable', 'error': None}
        error = result.get('error')
        if result.get('stage') == 'done':
            return dict(result, kind='decided')
        kind = hf_errors.classify_hf_error(error)
        if result.get('stage') == 'whoami':
            if self._is_no_token(error):
                return {'kind': 'no_token', 'error': error}
            return {'kind': 'token_refused' if kind == 'auth' else 'unreachable', 'error': error}
        return {'kind': 'auth_refused' if kind == 'auth' else 'unreachable', 'error': error}

    def _run_sync_download(self, repo_id, revision, root):
        """The sync download (S-2): the Daten download WORKER fetches
        ``revision`` (None = main's head, resolved by the worker) into ``root``
        (replace-or-create, the record written). Waited for in the R-8 loop;
        a FINISH/STOP cancels it (StartAbandoned). Returns the revision it
        downloaded; a failure refuses the Start in German."""
        start = getattr(self, '_sync_download', None)
        if start is None:
            print(f'Sync download of {repo_id} impossible: Daten is not running', file=sys.stderr, flush=True)
            self._last_warning_message = record_texts_de.SYNC_DOWNLOAD_FAILED_DE
            raise HubCheckRefused(repo_id)
        job = start(repo_id, revision, str(root), self._token_fp())
        while True:
            result = job.poll()
            if result is not None:
                break
            if self.get_status() in ('finish', 'stop'):
                job.cancel()
                raise StartAbandoned(repo_id)
            time.sleep(START_UPLOAD_POLL_S)
        if result.get('ok'):
            return result.get('revision')
        code = result.get('code') or ''
        print(f'Sync download of {repo_id} failed: {code}', file=sys.stderr, flush=True)
        if code == 'disk' and result.get('free') is not None and result.get('need') is not None:
            message = record_texts_de.sync_disk_de(result['free'], result['need'])
        elif code in ('stalled', 'timeout'):
            message = record_texts_de.DOWNLOAD_STALL_DE
        elif code in ('auth', 'unreachable', 'not_found'):
            message = record_texts_de.HF_ERROR_SENTENCES_DE.get(
                {'auth': 'auth', 'unreachable': 'network'}.get(code, ''),
                record_texts_de.SYNC_DOWNLOAD_FAILED_DE)
        elif code in ('old_format', 'other_robot', 'unsupported'):
            message = record_texts_de.SYNC_HUB_UNUSABLE_DE
        else:
            message = record_texts_de.SYNC_DOWNLOAD_FAILED_DE
        self._last_warning_message = message
        raise HubCheckRefused(repo_id)

    @staticmethod
    def _token_fp():
        """The fingerprint of the token in the robot's slot ('' when empty)."""
        try:
            store = _sibling('hf_token_store')
            token = store.read()
            return store.fingerprint(token) if token else ''
        except Exception:  # noqa: BLE001
            return ''

    @staticmethod
    def _is_no_token(error) -> bool:
        seen = 0
        while error is not None and seen < 8:
            if type(error).__name__ == 'LocalTokenNotFoundError':
                return True
            error = error.__cause__ or error.__context__
            seen += 1
        return False

    def _hub_existence(self, repo_id):
        """D7: ask the hub, logged in, whether ``repo_id`` exists — bounded by
        HUB_CHECK_TIMEOUT_S (a thread joined with a timeout: a black-holed hub
        must not hang „Start“). Returns (verdict, error), verdict one of
        'exists', 'absent', 'no_token', 'token_refused' (whoami refused the
        token), 'auth_refused' (the repo query was refused) or 'unreachable'
        (network, 429, 5xx, unclassified, or no answer in time)."""
        result = {}

        def _ask():
            try:
                api = HfApi()       # the rig's stored token
                try:
                    api.whoami()
                except Exception as e:  # noqa: BLE001 — classified below
                    result['stage'], result['error'] = 'whoami', e
                    return
                result['exists'] = bool(api.repo_exists(repo_id, repo_type='dataset'))
            except Exception as e:  # noqa: BLE001 — classified below
                result['stage'], result['error'] = 'exists', e

        worker = threading.Thread(target=_ask, name='hub-existence-check', daemon=True)
        worker.start()
        worker.join(HUB_CHECK_TIMEOUT_S)
        if worker.is_alive():
            return 'unreachable', None
        error = result.get('error')
        if error is None:
            return ('exists' if result.get('exists') else 'absent'), None
        kind = hf_errors.classify_hf_error(error)
        if result.get('stage') == 'whoami':
            if self._is_no_token(error):
                return 'no_token', error
            if kind == 'auth':
                return 'token_refused', error
            return 'unreachable', error
        return ('auth_refused' if kind == 'auth' else 'unreachable'), error

    def check_lerobot_dataset(self, images, joint_list):
        """Open or create the session's dataset on the first tick.

        Round 5: the slow I/O (the hub existence check, a download, the
        dataset creation) runs WITHOUT the recorder lock, and the dataset is
        installed under it — a command or a collision discard that arrives
        meanwhile is applied by the next record(). Only the record tick calls
        this, one at a time (its own callback group).
        """
        try:
            dataset = self._lerobot_dataset
            if dataset is None:
                exists = self._check_dataset_exists(self._save_repo_name, self._save_path)
                if getattr(self, '_start_abandoned', False):
                    # A FINISH/STOP ended the Start's wait (G-8): never create a
                    # dataset for a session that is already over, and say nothing.
                    return False
                if exists:
                    dataset = LeRobotDatasetWrapper(
                        self._save_repo_name,
                        self._save_path
                    )
                    # D4 (round 5): resuming an existing dataset is checked by
                    # LeRobot's own compatibility check BEFORE the robot type is
                    # stamped — another fps, another camera/joint set or another
                    # robot is refused in German instead of being appended
                    # (another fps silently stretched/compressed the timeline).
                    if not self._resume_is_compatible(dataset, images, joint_list):
                        return False
                else:
                    dataset = self._create_dataset(
                        self._save_repo_name,
                        images, joint_list)
                    self._write_create_record()

                if not self._task_info.use_optimized_save_mode:
                    dataset.start_image_writer(
                            num_processes=1,
                            num_threads=1
                        )
                dataset.set_robot_type(self._robot_type)
                with self.locked():
                    if self._lerobot_dataset is None:
                        self._lerobot_dataset = dataset
                    notice = (getattr(self, '_upload_off_notice_de', '')
                              or getattr(self, '_start_notice_de', ''))
                    if notice and not self._last_warning_message:
                        # F4: the page learns the session runs without upload
                        # (or, D14, that the hub could not be asked) from the
                        # next status tick's [WARNUNG].
                        self._last_warning_message = notice
                return True
            dataset.set_robot_type(self._robot_type)
            return True
        except Exception as e:
            print(f'Error checking lerobot dataset: {e}')
            return False

    def _write_create_record(self):
        """Daten 2.0 (§C2): a dataset this session CREATED gets a record with
        its display name (the raw task name) and the session's visibility,
        nothing else. Best-effort: a failed write only logs."""
        if not Path(self._save_path).is_dir():
            return
        try:
            dataset_sync.update_record(
                self._save_path, self._save_repo_name,
                display_name=str(getattr(self._task_info, 'task_name', '') or '').strip() or None,
                private=bool(getattr(self._task_info, 'private_mode', True)))
        except Exception as e:  # noqa: BLE001
            print(f'Could not write the sync record of {self._save_repo_name}: {e}',
                  file=sys.stderr, flush=True)

    def _resume_is_compatible(self, dataset, images, joint_list) -> bool:
        features = self._dataset_features(images or {}, joint_list)
        try:
            _resume_compatibility_check(
                dataset, self._robot_type, self._record_fps(), features)
            return True
        except ValueError as e:
            name = str(getattr(self._task_info, 'task_name', '') or '').strip() \
                or self._save_repo_name
            print(f'Resume refused for {self._save_repo_name}: {e}',
                  file=sys.stderr, flush=True)
            self._last_warning_message = self._resume_refusal_de(str(e), name, dataset)
            return False

    @staticmethod
    def _resume_refusal_de(message, name, dataset) -> str:
        """German by the FIRST mismatching field LeRobot names (it lists them
        as ``robot_type: …``, ``fps: …``, ``features: …``)."""
        for line in str(message).splitlines():
            field = line.split(':', 1)[0].strip()
            if field == 'fps':
                # LeRobot writes "fps: expected <present>, got <dataset's>".
                got = re.search(r'got\s+([0-9.]+)', line)
                try:
                    dataset_fps = float(getattr(dataset, 'fps', None) or got.group(1))
                except Exception:  # noqa: BLE001 — the number is a courtesy
                    return record_texts_de.resume_features_de(name)
                return record_texts_de.resume_fps_de(name, round(dataset_fps))
            if field == 'features':
                return record_texts_de.resume_features_de(name)
            if field == 'robot_type':
                return record_texts_de.resume_robot_de(name)
        return record_texts_de.resume_features_de(name)

    def _dataset_features(self, images, joint_list) -> dict:
        """Exactly what _create_dataset builds from the current frames (the D4
        check compares an existing dataset against it)."""
        features = DEFAULT_FEATURES.copy()
        for camera_name, image in images.items():
            features[f'observation.images.{camera_name}'] = {
                'dtype': 'video',
                'names': ['height', 'width', 'channels'],
                'shape': image.shape
            }
        features['observation.state'] = {
            'dtype': 'float32',
            'names': joint_list,
            'shape': (len(joint_list),)
        }
        features['action'] = {
            'dtype': 'float32',
            'names': joint_list,
            'shape': (len(joint_list),)
        }
        return features

    def _create_dataset(
            self,
            repo_id,
            images,
            joint_list) -> LeRobotDatasetWrapper:

        features = self._dataset_features(images, joint_list)
        return LeRobotDatasetWrapper.create(
                repo_id=repo_id,
                fps=self._task_info.fps,
                features=features,
                use_videos=True
            )

    def _upload_dataset(self, tags, private=True):
        """Auto-push the recorded dataset to HuggingFace.

        Prefers the HfApiWorker callback (wired by the node) so the
        upload runs out-of-process: the ROS spin thread stays
        responsive, progress + Success/Failed events flow through
        /huggingface/status (German toasts in the React UI), and a
        successful upload triggers the React side to call
        /datasets/register on the Cloud API — without that registration,
        Modal training cannot discover the dataset.

        Without a wired callback nothing is uploaded and the terminating tick
        says so (UPLOAD_NOT_STARTED_DE). ``private`` is forwarded from the
        student's "Privater Modus" choice in the React UI (TaskInfo
        .private_mode). It defaults to True so a missing/garbled flag
        fails safe to private — classroom recordings can contain
        children's faces / audio. A student may opt a repo public at
        record time; teachers can also flip individual repos later from
        the HF dashboard.

        The signature said ``private=False`` until 2026-08-06, flatly
        contradicting the paragraph above. That default is only ONE of the two
        layers: the operative one is ``TaskInfo.msg``, where ``bool
        private_mode`` carried NO default and ROS 2 booleans default to FALSE —
        so any rosbridge client that simply OMITTED the field got a PUBLIC repo
        of children's faces. Both layers now default to private; keep them
        in lockstep (fenced by test_upload_privacy_fails_safe.py).

        WHAT THE ``.msg`` DEFAULT ACTUALLY COVERS: only a client that OMITS the
        field. React never omits it — ``useRosServiceCaller`` sends
        ``private_mode: Boolean(taskInfo.privateMode)`` EXPLICITLY on every
        recording — so the UI path never reads the ``.msg`` default at all and
        gets ``taskSlice.defaultTaskInfo.privateMode`` instead. That UI default
        is now ``true`` as well. The old wording here ("React always sends
        ``private_mode: true``") described the UI's value, not its behaviour,
        and was false the moment the checkbox default moved; the durable fact
        is that React always sends the field EXPLICITLY.
        """
        private = bool(private)
        if self._upload_enqueued:
            # Already kicked off; subsequent state-machine ticks are no-ops.
            return
        self._upload_enqueued = True

        # Never upload into a namespace the rig's own HF token does not own.
        # _save_repo_name is built from the CLIENT-SUPPLIED task_info.user_id,
        # so without this an unauthenticated rosbridge client could name any
        # namespace at all — and after a student handover the previous
        # student's id was still in play. Marked enqueued ABOVE first, so a
        # refusal cannot spin the state machine re-attempting every tick.
        allowed = self._rig_hf_namespaces()
        namespace = (self._save_repo_name or '').split('/')[0]
        if allowed is not None and namespace not in allowed:
            # The student-facing text names NEITHER the namespace nor the repo
            # id. `namespace` is derived from the CLIENT-SUPPLIED
            # task_info.user_id, and this string is rendered as a German toast
            # in the browser — echoing caller-supplied text into a refusal is
            # the same shape the path-confinement refusals were fixed for. The
            # operator still gets the exact value on stderr below.
            # Quotes are typographic („…“), never straight, per the German
            # string rules.
            self._last_warning_message = record_texts_de.NAMESPACE_REFUSED_DE
            self._upload_blocked_reason_de = self._last_warning_message
            print(
                f'[FEHLER] Upload REFUSED: repo namespace {namespace!r} is not '
                f'owned by this rig\'s HuggingFace token (owns: {sorted(allowed)})',
                file=sys.stderr, flush=True,
            )
            return
        if self._upload_callback is None:
            # Daten 2.0 (§E2): the guarded single commit in the HF worker is the
            # ONLY dataset upload there is. The standalone push_to_hub fallback
            # (an unguarded multi-commit upload) is gone: without a worker the
            # session says so and uploads nothing.
            self._upload_blocked_reason_de = UPLOAD_NOT_STARTED_DE
            print(f'[WARNUNG] No upload worker wired; {self._save_repo_name} stays local',
                  file=sys.stderr, flush=True)
            return
        try:
            self._upload_callback(
                self._save_repo_name,
                str(self._save_path),
                private,
            )
        except Exception as e:
            self._upload_blocked_reason_de = UPLOAD_NOT_STARTED_DE
            print(
                f'[WARNUNG] Upload konnte nicht eingereiht werden: {e}',
                file=sys.stderr, flush=True,
            )

    def _download_dataset(self, repo_id):
        """D7 „exists“: the sync download of main's head (resolved by the Daten
        download worker) into the session's folder; returns the revision it
        downloaded. One-argument shape kept (the record-FSM tests stub it)."""
        return self._run_sync_download(repo_id, None, self._save_path)

    def convert_action_to_joint_trajectory_msg(self, action):
        joint_trajectory_msgs = self.data_converter.tensor_array2joint_trajectory(
            action,
            self.total_joint_order)
        return joint_trajectory_msgs

    def get_task_info(self):
        return self._task_info

    def _init_task_limits(self):
        if not self._single_task:
            self._task_info.num_episodes = 1_000_000
            self._task_info.episode_time_s = 1_000_000

    # Namespaces the rig's own HF token may write to (its account; R-10: no orgs).
    # Cached at CLASS level because it is a property of the RIG's token, not of
    # a recording: whoami is an 8 s-bounded network call and _upload_dataset
    # runs on the end-of-recording save path, which is already busy.
    # Invalidated by the node's /register_hf_user callback after every set or
    # clear of the per-student token slot (hf_token_store, 042).
    _hf_namespace_cache = None

    @classmethod
    def invalidate_hf_namespace_cache(cls):
        cls._hf_namespace_cache = None

    @classmethod
    def _rig_hf_namespaces(cls):
        """Namespaces this rig's token owns, or None when unknowable.

        None means "cannot judge" — no token registered, whoami timed out, the
        school network is down. The caller then ALLOWS the upload, deliberately:

          * with no token the upload fails on its own anyway, so nothing is
            actually published;
          * and turning a transient network blip into a destroyed upload is a
            worse outcome than the case this guard exists for.

        A THIRD reason used to head that list — "recording with no cloud login
        is a fully supported path (only Training and Inferenz gate on a
        session)" — and it is no longer true. The student SPA now requires a
        Supabase login on every page except the Orange Pi's „System" tab
        (physical_ai_manager/src/utils/authGate.js), and the one remaining way
        to record signed out is the „Ohne Anmeldung fortfahren" escape, offered
        only after a login attempt has PROVEN the auth service unreachable. The
        two reasons above are untouched by that, so the fail-open stays exactly
        as correct as it was — and the guard's LOGIC is deliberately unchanged.

        This is a REFUSE-ON-PROOF gate: it only ever refuses a namespace it
        can demonstrably show the rig's token does not own.

        Since 042 the token is the signed-in student's own (the Startseite
        pushes it into the tmpfs slot hf_token_store manages), so this guard is
        defence in depth behind the SPA's start block, not the primary fence.
        """
        if cls._hf_namespace_cache is not None:
            return cls._hf_namespace_cache
        try:
            ids = cls.get_huggingface_user_id()
        except Exception:  # noqa: BLE001 — no token / network / HF outage
            return None
        if not ids:
            return None
        cls._hf_namespace_cache = frozenset(ids)
        return cls._hf_namespace_cache

    @staticmethod
    def get_robot_type_from_info_json(info_json_path):
        with open(info_json_path, 'r', encoding='utf-8') as f:
            info = json.load(f)
        return info.get('robot_type', '')

    @staticmethod
    def get_huggingface_user_id():
        """The token's ACCOUNT, as a one-element list (R-10, Daten 2.0): no
        organisations. The Benutzer-ID list (/get_hf_user), the namespace
        guard (_rig_hf_namespaces) and the guarded upload all know the account
        alone — nothing is recorded into an organisation."""
        def api_call():
            api = HfApi()
            try:
                user_info = api.whoami()
                return [user_info['name']]
            except LocalTokenNotFoundError as e:
                print(f'No registered HuggingFace token found: {e}')
                raise Exception('No registered HuggingFace token found')
            except Exception as e:
                print(f'Token validation failed: {e}')
                raise

        # Use queue to get result from thread
        result_queue = queue.Queue()

        def worker():
            try:
                result = api_call()
                result_queue.put(('success', result))
            except Exception as e:
                result_queue.put(('error', e))

        # Start thread and wait with timeout
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        try:
            # Wait for the whoami result. 8 s (was 1.5 s) so the Benutzer-ID
            # list still loads on a slow/cold school network instead of
            # silently returning an empty list. Safe to block this long: the
            # HF services run in their own ReentrantCallbackGroup
            # (physical_ai_server._init_ros_service), so this wait no longer
            # stalls the heartbeat / task-status timers.
            status, data = result_queue.get(timeout=8.0)
            if status == 'success':
                if data:
                    print(data)
                return data
            else:
                raise data
        except queue.Empty:
            print('HuggingFace whoami timed out after 8 seconds')
            return None

    @staticmethod
    def download_huggingface_repo(
        repo_id,
        repo_type='dataset'
    ):
        # Both roots come from dataset_paths, which is the single source of
        # truth the browse confinement, TrainingManager and React's
        # POLICY_MODEL_PATH all read. They used to be spelled out here, and the
        # model one drifted from every reader of it — see MODEL_ROOT_RELATIVE.
        #
        # (The 'model' value keeps its v2.5.0 meaning: the LeRobot install is
        # pip-managed from PyPI and the vendored
        # ros2_ws/src/physical_ai_tools/lerobot tree is stripped by the image
        # build, so model downloads must NOT target a path under it.)
        download_path = {
            'dataset': dataset_paths.dataset_root(),
            'model': dataset_paths.model_root(),
        }

        save_path = download_path.get(repo_type)

        if save_path is None:
            raise ValueError(f'Invalid repo type: {repo_type}')

        DataManager._last_hf_failure_reason_de = None
        # `repo_id` comes off the wire: `save_path / repo_id` with an absolute
        # right-hand side discarded the root. safe_under proves where it lands.
        try:
            save_dir = dataset_paths.safe_under(save_path, repo_id)
        except dataset_paths.DatasetPathError as e:
            print(f'Download refused (target outside the root): {repo_id!r}: {e}')
            DataManager._last_hf_failure_reason_de = str(e)
            return False
        if repo_type == 'dataset' and save_dir.exists():
            # Daten 2.0 (§E3): the old page's dataset download never mixes files
            # into an existing dataset; a newer version is loaded in the Daten tab.
            print(f'Download refused: {repo_id} exists locally')
            DataManager._last_hf_failure_reason_de = _daten_texts().DOWNLOAD_EXISTS_DE
            return False
        try:
            print(f'Starting download of {repo_id} ({repo_type})...')

            # Create a wrapper class that includes the progress_queue
            class ProgressTqdmWrapper(HuggingFaceProgressTqdm):

                def __init__(self, *args, **kwargs):
                    kwargs['progress_queue'] = DataManager._progress_queue
                    super().__init__(*args, **kwargs)

            result = snapshot_download(
                repo_id=repo_id,
                repo_type=repo_type,
                local_dir=save_dir,
                tqdm_class=ProgressTqdmWrapper
            )

            print(f'Download completed: {repo_id}')
            return result
        except Exception as e:
            print(f'Error downloading HuggingFace repo: {e}')
            # Print more detailed error information
            import traceback
            print(f'Detailed error traceback:\n{traceback.format_exc()}')
            DataManager._last_hf_failure_reason_de = (
                DataManager._classify_hf_failure_de(e)
            )
            return False

    @classmethod
    def set_progress_queue(cls, progress_queue):
        """Set progress queue for multiprocessing communication."""
        cls._progress_queue = progress_queue

    @staticmethod
    def _create_dataset_card(local_dir, readme_path, repo_id, private=True):
        """Write the dataset README: LeRobot's OWN dataset card (O5, round 5),
        built from meta/info.json, naming the repo_id (D6); a PUBLIC dataset's
        card carries an explicit apache-2.0 licence. Our forked template is
        gone (it was never even installed into the image)."""
        info_path = Path(local_dir) / 'meta' / 'info.json'
        dataset_info = None
        if info_path.exists():
            with open(info_path, 'r', encoding='utf-8') as f:
                dataset_info = json.load(f)
        tags = ['robotis']
        robot_type = (dataset_info or {}).get('robot_type') or ''
        if robot_type:
            tags.append(robot_type)
        text = dataset_card.build_dataset_card(
            repo_id, dataset_info, tags, public=not bool(private))
        Path(readme_path).write_text(text, encoding='utf-8')
        print('Dataset README.md written from the LeRobot dataset card')

    @staticmethod
    def _create_model_card(local_dir, readme_path):
        """
        Create ModelCard README for model repository.

        Args:
        ----
        local_dir: Local directory path containing model
        readme_path: Path where README.md will be saved

        """
        # Find train_config.json (check common locations first)
        train_config = None
        common_paths = [
            Path(local_dir) / 'train_config.json',
            Path(local_dir) / 'config' / 'train_config.json',
            Path(local_dir) / 'pretrained_model' / 'train_config.json',
        ]

        # Check common paths first (fast)
        for config_path in common_paths:
            if config_path.exists():
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        train_config = json.load(f)
                    print(f'Found train_config.json at {config_path}')
                    break
                except Exception as e:
                    print(f'Error reading {config_path}: {e}')
                    continue

        # If not found, search recursively (slower fallback)
        if train_config is None:
            for config_path in Path(local_dir).rglob('train_config.json'):
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        train_config = json.load(f)
                    print(f'Found train_config.json at {config_path}')
                    break
                except Exception as e:
                    print(f'Error reading {config_path}: {e}')
                    continue

        if train_config is None:
            print(f'train_config.json not found in {local_dir}')

        dataset_repo = ''
        if train_config:
            dataset_repo = train_config.get(
                'dataset', {}
            ).get('repo_id', '')

        # Prepare tags
        tags = ['robotis', 'robotics']

        # Create ModelCardData with conditional datasets
        card_data_kwargs = {
            'license': 'apache-2.0',
            'tags': tags,
            'pipeline_tag': 'robotics',
        }
        if dataset_repo:
            card_data_kwargs['datasets'] = [dataset_repo]

        card_data = ModelCardData(**card_data_kwargs)

        # Get template path
        template_dir = Path(__file__).parent
        template_path = str(template_dir / 'model_card_template.md')

        # Create card from template
        card = ModelCard.from_template(
            card_data,
            template_path=template_path,
        )
        card.save(str(readme_path))
        print('Model README.md created using HuggingFace Hub')

    @staticmethod
    def _create_readme_if_not_exists(local_dir, repo_type, repo_id=None, private=True):
        """
        Write the repo's README.md before an upload.

        A DATASET's README is rebuilt on EVERY upload (round 5, O5), so a
        resumed dataset's card matches its info.json; other repo types keep an
        existing README.

        """
        readme_path = Path(local_dir) / 'README.md'

        if readme_path.exists() and repo_type != 'dataset':
            print(f'README.md already exists in {local_dir}')
            return

        print(f'Writing README.md in {local_dir}')

        try:
            if repo_type == 'dataset':
                DataManager._create_dataset_card(
                    local_dir, readme_path, repo_id or Path(local_dir).name,
                    private=private)
        except Exception as e:
            print(f'Warning: Failed to create README.md: {e}')
            import traceback
            print(f'Traceback: {traceback.format_exc()}')

    # Student-facing German explanation for an invalid/expired HF token —
    # MUST point at the Startseite, never at `hf auth login`: since 042 the
    # token is the signed-in student's own, pasted once on the Startseite and
    # pushed to the robot's tmpfs slot, so replacing it there applies at once
    # (no environment restart). leLab-comparison PR-1. Byte-identical with
    # record_texts_de.HF_AUTH_ERROR_DE (fenced by test_record_texts_de.py).
    HF_AUTH_ERROR_DE = (
        'Hugging Face-Token ungültig oder abgelaufen. Ersetze dein Token auf der '
        'Startseite der EduBotics-App.'
    )

    # German failure reason of the most recent upload/download attempt.
    # The HfApiWorker calls upload/download IN ITS OWN PROCESS and reads
    # this immediately after a falsy return (single-threaded loop), so the
    # class attribute is a safe side-channel that keeps the long-standing
    # bool/path return contracts intact.
    _last_hf_failure_reason_de = None

    @staticmethod
    def _classify_hf_failure_de(error):
        """The German reason for a Hugging Face failure by its CAUSE (round 5,
        R5-4b: hf_errors walks the exception chain — auth / network / busy /
        server), or None for the worker's generic sentence. An unreachable hub
        is no longer reported as „Token ungültig“."""
        return hf_errors.hf_error_sentence_de(error)

    # Daten 2.0 (§E2 step 9): the status extras of the most recent upload
    # ({'repo_type', 'info_json', 'message_de'}), read by the HF worker in the
    # same process right after the call — the side channel beside
    # _last_hf_failure_reason_de — and forwarded to the node.
    _last_upload_extras = None

    # The guarded upload's refusals (hub_sync.Refused codes), in German.
    _UPLOAD_REFUSALS_DE = {
        'in_session': record_texts_de.UPLOAD_IN_SESSION_DE,
        'local_broken': record_texts_de.UPLOAD_BROKEN_DE,
        'namespace': record_texts_de.NAMESPACE_REFUSED_DE,
        'hub_changed': record_texts_de.HUB_CHANGED_SINCE_CHECK_DE,
        'hub_differs': record_texts_de.UPLOAD_HUB_DIFFERS_DE,
    }

    @staticmethod
    def upload_huggingface_repo(
        repo_id,
        repo_type,
        local_dir,
        private=True,
        expected_hub_sha=_UNSET,
    ):
        """Upload in the HF worker child. A DATASET goes through the ONE guarded
        single commit (``hub_sync.upload``, spec §E2) — every gate is inside it,
        so no caller can skip one; ``expected_hub_sha`` is the head the caller's
        decision saw (a sha), None (the hub must hold no dataset) or absent
        (decide at upload time). A MODEL keeps its upload_large_folder path.
        Returns True/False; the German reason of a failure is
        ``_last_hf_failure_reason_de``, the status extras ``_last_upload_extras``."""
        DataManager._last_hf_failure_reason_de = None
        DataManager._last_upload_extras = {'repo_type': repo_type or ''}
        if repo_type == 'dataset':
            return DataManager._upload_dataset_guarded(repo_id, local_dir, private, expected_hub_sha)
        return DataManager._upload_model(repo_id, repo_type, local_dir, private)

    @staticmethod
    def _upload_dataset_guarded(repo_id, local_dir, private, expected_hub_sha):
        hub_sync = _sibling('hub_sync')
        root = Path(local_dir)
        kwargs = {
            'private': bool(private),
            'progress': DataManager._report_upload_progress,
            'write_card': lambda r, repo, real_private: DataManager._create_readme_if_not_exists(
                r, 'dataset', repo_id=repo, private=real_private),
        }
        if expected_hub_sha is not _UNSET:
            kwargs['expected'] = expected_hub_sha
        print(f'Guarded upload of {local_dir} to {repo_id} (private={bool(private)}, '
              f'expected={kwargs.get("expected", "decide now")})')
        try:
            result = hub_sync.upload(root, repo_id, **kwargs)
        except Exception as e:  # noqa: BLE001 — classified below
            code = getattr(e, 'code', None) if type(e).__name__ == 'Refused' else None
            if code is not None:
                print(f'Upload of {repo_id} refused: {e}')
                DataManager._last_hf_failure_reason_de = DataManager._UPLOAD_REFUSALS_DE.get(code)
                DataManager._last_upload_extras['code'] = code
                return False
            print(f'Error uploading {repo_id}: {e}')
            import traceback
            print(f'Detailed error traceback:\n{traceback.format_exc()}')
            DataManager._last_hf_failure_reason_de = DataManager._classify_hf_failure_de(e)
            kind = hf_errors.classify_hf_error(e)
            DataManager._last_upload_extras['code'] = (
                'auth' if kind == 'auth' else 'unreachable' if kind else 'internal')
            return False
        DataManager._last_upload_extras['info_json'] = DataManager._upload_info_json(
            root, repo_id, result.get('private'))
        if result.get('tag') == 'failed':
            # The data is there, the training pointer is not (§E2 step 7).
            DataManager._last_hf_failure_reason_de = record_texts_de.HUB_TAG_FAILED_DE
            DataManager._last_upload_extras['code'] = 'tag_failed'
            return False
        if result.get('unconfirmed'):
            # G-13: a landed commit is never reported as „nothing uploaded“.
            DataManager._last_upload_extras['message_de'] = record_texts_de.UPLOAD_UNCONFIRMED_DE
        print(f'Upload of {repo_id} committed {result.get("commit")} (tag {result.get("tag")})')
        return True

    @staticmethod
    def _upload_info_json(root, repo_id, private):
        """§E2 step 9: what the page registers the dataset with."""
        try:
            info = json.loads((Path(root) / 'meta' / 'info.json').read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            info = {}
        record = dataset_sync.own_record(root, repo_id)
        return {'fps': info.get('fps'), 'total_episodes': info.get('total_episodes'),
                'total_frames': info.get('total_frames'), 'robot_type': info.get('robot_type'),
                'display_name': record.get('display_name'), 'private': bool(private)}

    @staticmethod
    def _report_upload_progress(done, total):
        """Per pre-uploaded file: the HF worker's progress items (they also feed
        its stall watch)."""
        queue_ = DataManager._progress_queue
        if queue_ is None:
            return
        try:
            queue_.put({'current': int(done), 'total': int(total),
                        'percentage': round(100.0 * done / total, 1) if total else 0.0}, block=False)
        except Exception:  # noqa: BLE001 — progress is a courtesy
            pass

    @staticmethod
    def _upload_model(repo_id, repo_type, local_dir, private):
        """A MODEL's upload (no caller in Daten 2.0): its upload_large_folder
        path, unchanged."""
        try:
            from huggingface_hub import upload_large_folder
            api = HfApi()
            try:
                user_info = api.whoami()
                print(f'Authenticated as: {user_info["name"]}')
            except Exception as auth_e:
                print(f'Authentication failed: {auth_e}')
                DataManager._last_hf_failure_reason_de = (
                    DataManager._classify_hf_failure_de(auth_e)
                )
                return False
            private = bool(private)
            url = api.create_repo(repo_id, repo_type=repo_type, private=private, exist_ok=True)
            print(f'Repository created/verified: {url}')
            DataManager._delete_dot_cache_folder_before_upload(local_dir)
            DataManager._create_readme_if_not_exists(local_dir, repo_type, repo_id=repo_id, private=private)
            print(f'Uploading folder {local_dir} to repository {repo_id}')
            from contextlib import redirect_stdout
            from .progress_tracker import HuggingFaceLogCapture
            log_capture = HuggingFaceLogCapture(progress_queue=DataManager._progress_queue)
            with redirect_stdout(log_capture):
                upload_large_folder(
                    repo_id=repo_id,
                    folder_path=local_dir,
                    repo_type=repo_type,
                    print_report=True,
                    print_report_every=1,
                )
            return True
        except Exception as e:
            print(f'Error Uploading HuggingFace repo: {e}')
            import traceback
            print(f'Detailed error traceback:\n{traceback.format_exc()}')
            DataManager._last_hf_failure_reason_de = (
                DataManager._classify_hf_failure_de(e)
            )
            return False

    @staticmethod
    def _delete_dot_cache_folder_before_upload(local_dir):
        dot_cache_path = Path(local_dir) / '.cache'
        if dot_cache_path.exists():
            shutil.rmtree(dot_cache_path)
            print(f'Deleted {local_dir}/.cache folder before upload')

    @staticmethod
    def delete_huggingface_repo(
        repo_id,
        repo_type='dataset',
    ):
        try:
            result = HfApi().delete_repo(repo_id, repo_type=repo_type)
            return result
        except Exception as e:
            print(f'Error deleting HuggingFace repo: {e}')
            return False

    @staticmethod
    def get_huggingface_repo_list(
        author,
        data_type='dataset'
    ):
        repo_id_list = []
        if data_type == 'dataset':
            dataset_list = HfApi().list_datasets(author=author)
            for dataset in dataset_list:
                repo_id_list.append(dataset.id)

        elif data_type == 'model':
            model_list = HfApi().list_models(author=author)
            for model in model_list:
                repo_id_list.append(model.id)
        reverse = repo_id_list[::-1]
        return reverse

    @staticmethod
    def get_collections_repo_list(
        collection_id
    ):
        collection_list = HfApi().get_collection(collection_id)
        repo_list_in_collection = []
        for item in collection_list.items:
            repo_list_in_collection.append(item.item_id)
        return repo_list_in_collection
