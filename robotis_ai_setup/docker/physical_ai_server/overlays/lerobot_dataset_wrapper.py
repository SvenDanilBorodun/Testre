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

import threading

from lerobot.datasets.compute_stats import (
    get_feature_stats
)
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.datasets.utils import (
    DEFAULT_FEATURES,
    validate_episode_buffer,
    validate_feature_dtype_and_shape,
    validate_features_presence,
    validate_frame,
    write_episode,
    write_episode_stats,
    write_info
)
import numpy as np
from physical_ai_server.video_encoder.ffmpeg_encoder import FFmpegEncoder


class JpegFrame:
    """A single camera frame held as its original JPEG bytes plus the
    decoded ``(height, width, channels)`` shape.

    EduBotics P1 (JPEG-in-RAM): the recording buffer used to retain a
    decoded RGB uint8 ndarray per frame (~920 KB at 640×480), so a
    30 Hz × 2-cam episode grew the in-RAM buffer ~1.84 MB/tick and
    OOM-killed the 6 GB container at ~108 s (the EDUBOTICS_MAX_BUFFER_GB
    valve then capped episodes at ~74 s). The frames already arrive from
    the camera as JPEG (~30 KB), so keeping the *compressed* bytes in the
    buffer is ~30× smaller and removes the practical episode-length cap.
    Pixels are materialized lazily, and only where genuinely needed:
    stats sampling (~100–1000 frames) and the video encode (one chunk at
    a time, via :class:`_LazyDecodeBuffer`). The hot 30 Hz append path
    never decodes.

    Gated by ``EDUBOTICS_JPEG_BUFFER`` in data_manager; when that flag is
    off, the legacy RGB-ndarray buffer is used and this class is never
    constructed, so the old path is an exact rollback.
    """

    __slots__ = ('jpeg', 'shape')

    def __init__(self, jpeg: np.ndarray, shape: tuple):
        # jpeg: 1-D uint8 ndarray of the compressed JPEG bytes (a private
        # copy — the source CompressedImage buffer is overwritten in
        # place by the communicator). shape: (h, w, c) of the decode.
        self.jpeg = jpeg
        self.shape = tuple(shape)

    @property
    def nbytes(self) -> int:
        # Read by add_frame_without_write_image for the RAM-valve
        # accounting; ~30 KB here instead of ~920 KB for decoded RGB.
        return int(self.jpeg.nbytes)

    def decode(self) -> np.ndarray:
        """Decode to an (h, w, c) RGB uint8 ndarray. cv2 is imported
        lazily so importing this module stays OpenCV-free (mirrors the
        GUI camera_bridge pattern)."""
        import cv2
        bgr = cv2.imdecode(self.jpeg, cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError(
                'JpegFrame.decode: cv2.imdecode returned None (corrupt '
                'JPEG in recording buffer)'
            )
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


class _LazyDecodeBuffer:
    """A read-only sequence view over a list of :class:`JpegFrame` that
    decodes to RGB ndarrays on access.

    Handed to ``FFmpegEncoder.set_buffer`` in JPEG mode so the encoder —
    which already iterates the buffer in ``chunk_size`` slices — only
    ever materializes one chunk of decoded frames at a time (peak extra
    RAM ≈ chunk_size × ~920 KB), instead of the whole episode. The
    encoder itself is left untouched (no overlay needed): it calls
    ``len(buffer)``, ``buffer[0].shape`` and ``buffer[i:j]`` then
    ``frame.tobytes()`` — all satisfied here. A plain ndarray (legacy
    mode) is passed through unwrapped, so this class is JPEG-mode-only.
    """

    __slots__ = ('_frames',)

    def __init__(self, frames):
        self._frames = frames

    def __len__(self):
        return len(self._frames)

    @staticmethod
    def _decode_one(f):
        return f.decode() if isinstance(f, JpegFrame) else f

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return [self._decode_one(f) for f in self._frames[idx]]
        return self._decode_one(self._frames[idx])


class LeRobotDatasetWrapper(LeRobotDataset):

    # Audit (2026-05-22): cap the in-RAM episode buffer at 4 GB. Decoded
    # frames are RGB uint8 ndarrays (~921 KB/frame at 640×480); at
    # 30 Hz × 2 cams a 5-min episode produces ~16.6 GB and reliably
    # OOMkills the container (`mem_limit: 6g` in docker-compose.yml).
    # Above this cap we set a German warning + a `_buffer_full` flag —
    # the data_manager polls it between ticks and forces a save before
    # the next frame would push us past the docker limit. This is a
    # safety valve, not the long-term fix; v2.4's plan is to keep the
    # buffer as raw JPEG bytes (~30 KB/frame, 50× smaller) and decode
    # only inside FFmpegEncoder. Override via EDUBOTICS_MAX_BUFFER_GB.
    _DEFAULT_MAX_BUFFER_BYTES = int(
        float(__import__('os').environ.get('EDUBOTICS_MAX_BUFFER_GB', '4.0'))
        * (1024 ** 3)
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.encoders = {}
        self.total_frame_buffer = None
        self.episode_ranges = []
        self._append_in_progress = False
        self._robot_type = 'default'  # default
        # Approximate decoded-image bytes accumulated since last save.
        # Updated on each add_frame_without_write_image call. Public so
        # data_manager can read it for the buffer-full check; cleared
        # in save_episode_without_*.
        self._episode_image_bytes = 0
        self._buffer_full = False
        self._buffer_full_warning = ''

    def set_robot_type(self, robot_type: str) -> None:
        self._robot_type = robot_type

    def video_encoding(self) -> None:
        video_paths = {}
        total_buffer_index = self._extract_episode_indices(
            self.total_frame_buffer['episode_index']
        )
        for episode_index, (start, end) in enumerate(self.episode_ranges):
            episode_buffer = self._extract_episode_buffer(start, end, episode_index)
            for key, ep in episode_buffer.items():
                if 'observation.images' in key:
                    video_path = self.root / self.meta.get_video_file_path(
                        total_buffer_index[episode_index], key
                    )
                    video_paths[key] = str(video_path)
                    self._create_video(ep, video_path)

    def _extract_episode_buffer(self, start: int, end: int, episode_index: int) -> dict:
        buffer = {}
        for key, value in self.total_frame_buffer.items():
            if isinstance(value, (list, np.ndarray)):
                buffer[key] = value[start:end + 1]
            else:
                buffer[key] = value

        episode_length = end - start + 1
        buffer['size'] = episode_length
        buffer['index'] = np.arange(start, end + 1)
        buffer['episode_index'] = np.full((episode_length,), episode_index)

        return buffer

    def _extract_episode_indices(self, flat_episode_index_list: list[int]) -> list[int]:
        if not flat_episode_index_list:
            return []

        buffer_idx = []
        for idx in flat_episode_index_list:
            if not buffer_idx or idx != buffer_idx[-1]:
                buffer_idx.append(idx)
        return buffer_idx

    def append_episode_buffer(self, episode_buffer: dict, episode_length) -> None:
        self._append_in_progress = True

        try:
            if not hasattr(self, 'total_frame_buffer') or self.total_frame_buffer is None:
                self.total_frame_buffer = self.create_episode_buffer()
            if not hasattr(self, 'episode_ranges'):
                self.episode_ranges = []

            start_index = self.total_frame_buffer['size']
            num_new_frames = episode_length
            end_index = start_index + num_new_frames - 1

            for key, value in episode_buffer.items():
                if key == 'size':
                    continue
                if key not in self.total_frame_buffer:
                    if isinstance(value, list):
                        self.total_frame_buffer[key] = value.copy()
                    elif hasattr(value, 'tolist'):
                        self.total_frame_buffer[key] = value.tolist()
                    else:
                        self.total_frame_buffer[key] = [value]
                else:
                    if isinstance(self.total_frame_buffer[key], list):
                        if isinstance(value, list):
                            self.total_frame_buffer[key].extend(value)
                        elif hasattr(value, 'tolist'):
                            self.total_frame_buffer[key].extend(value.tolist())
                        else:
                            self.total_frame_buffer[key].append(value)

            if (
                'episode_index' not in self.total_frame_buffer
                or not isinstance(self.total_frame_buffer['episode_index'], list)
            ):
                self.total_frame_buffer['episode_index'] = []

            ep_idx = episode_buffer.get('episode_index')
            if ep_idx is None:
                ep_idx = list(range(episode_length))
            self.total_frame_buffer['episode_index'].extend(ep_idx)

            if 'frame_index' not in episode_buffer:
                self.total_frame_buffer['frame_index'].extend(
                    list(range(start_index, start_index + num_new_frames))
                )

            if 'timestamp' not in episode_buffer:
                self.total_frame_buffer['timestamp'].extend(
                    [(start_index + i) / self.fps for i in range(num_new_frames)]
                )

            self.total_frame_buffer['size'] += num_new_frames

            self.episode_ranges.append((start_index, end_index))
        finally:
            self._append_in_progress = False

    def _validate_frame_allow_jpeg(self, frame: dict) -> None:
        """Frame validation that tolerates JpegFrame image values.

        Mirrors lerobot.datasets.utils.validate_frame exactly (same
        presence check, same per-feature dtype/shape check for non-image
        features) but, for image/video features supplied as a JpegFrame,
        validates the carried ``.shape`` against the declared feature
        shape instead of routing the un-decoded JpegFrame through
        validate_feature_image_or_video (which only accepts ndarray /
        PIL.Image and would reject it). When the frame contains no
        JpegFrame (legacy RGB mode), this is behaviourally identical to
        validate_frame.
        """
        expected_features = set(self.features) - set(DEFAULT_FEATURES)
        actual_features = set(frame)
        error_message = validate_features_presence(actual_features, expected_features)
        for name in (actual_features & expected_features) - {'task'}:
            value = frame[name]
            if isinstance(value, JpegFrame):
                expected_shape = tuple(self.features[name]['shape'])
                c_h_w = (expected_shape[2], expected_shape[0], expected_shape[1])
                if value.shape != expected_shape and value.shape != c_h_w:
                    error_message += (
                        f"The feature '{name}' of shape '{value.shape}' does "
                        f"not have the expected shape '{expected_shape}'.\n"
                    )
            else:
                error_message += validate_feature_dtype_and_shape(
                    name, self.features[name], value)
        if error_message:
            raise ValueError(error_message)

    def add_frame_without_write_image(self, frame: dict, task: str) -> None:
        self._validate_frame_allow_jpeg(frame)

        if self.episode_buffer is None:
            self.episode_buffer = self.create_episode_buffer()

        # Automatically add frame_index and timestamp to episode buffer
        frame_index = self.episode_buffer['size']
        timestamp = frame.pop('timestamp') if 'timestamp' in frame else frame_index / self.fps
        self.episode_buffer['frame_index'].append(frame_index)
        self.episode_buffer['timestamp'].append(timestamp)

        # Add frame features to episode_buffer
        for key in frame:
            if key not in self.episode_buffer:
                self.episode_buffer[key] = [frame[key]]
            else:
                self.episode_buffer[key].append(frame[key])
            # Track decoded-image bytes for the RAM safety valve. Only
            # observation.images.* fields are large enough to matter;
            # joint vectors are < 1 KB and rounding noise.
            if 'observation.images' in key:
                value = frame[key]
                nbytes = getattr(value, 'nbytes', None)
                if nbytes is None and hasattr(value, '__len__'):
                    # Fallback for non-ndarray entries (e.g. lists)
                    nbytes = len(value) * 921_600
                if nbytes is not None:
                    self._episode_image_bytes += int(nbytes)

        self.episode_buffer['task'].append(task)
        self.episode_buffer['size'] += 1

        # RAM safety valve. If the buffer has crossed the cap, raise the
        # flag so data_manager forces a save before the next decode
        # would push us into OOMkill territory. We never raise here —
        # the current frame is already in; the caller drains.
        if (
            not self._buffer_full
            and self._episode_image_bytes > self._DEFAULT_MAX_BUFFER_BYTES
        ):
            self._buffer_full = True
            self._buffer_full_warning = (
                f'Aufnahme-Puffer hat {self._episode_image_bytes // (1024 ** 3)} GB '
                f'erreicht (Limit {self._DEFAULT_MAX_BUFFER_BYTES // (1024 ** 3)} GB). '
                f'Episode wird automatisch gespeichert, damit der Container nicht '
                f'wegen Arbeitsspeicher-Überlauf beendet wird. Nächste Aufnahmen '
                f'bitte kürzer halten oder Auflösung reduzieren.'
            )

    def reset_buffer_accounting(self) -> None:
        """Zero the RAM-valve counters. Called from the save path so the
        next episode starts with a fresh budget."""
        self._episode_image_bytes = 0
        self._buffer_full = False
        self._buffer_full_warning = ''

    def _save_episode_common(self, encode_now: bool):
        """Shared body of the two optimized save paths.

        EduBotics P4 de-dup: ``save_episode_without_video_encoding`` (the
        multi-task path, which defers encoding to ``video_encoding()``)
        and ``save_episode_without_write_image`` (the single-task path,
        which encodes inline) were byte-for-byte identical except for two
        things — whether ``_create_video`` runs in the per-camera loop,
        and whether the caller appends to the multi-episode buffer
        afterwards. Both forks now share this body so a future change to
        the parquet/meta/stats prologue can't drift between them.

        ``encode_now``: when True, kick the async FFmpeg encode for each
        camera immediately (single-task); when False, only record the
        video metadata and leave encoding to a later ``video_encoding()``
        pass (multi-task batched encode).

        Returns ``(episode_buffer, episode_length)`` so the multi-task
        caller can hand them to ``append_episode_buffer``.
        """
        episode_buffer = self.episode_buffer
        validate_episode_buffer(
            episode_buffer,
            self.meta.total_episodes,
            self.features)

        episode_length = episode_buffer.pop('size')
        tasks = episode_buffer.pop('task')
        episode_tasks = list(set(tasks))
        episode_index = episode_buffer['episode_index']

        episode_buffer['index'] = np.arange(
            self.meta.total_frames,
            self.meta.total_frames + episode_length)
        episode_buffer['episode_index'] = np.full((episode_length,), episode_index)

        # Add new tasks to the tasks dictionary
        for task in episode_tasks:
            task_index = self.meta.get_task_index(task)
            if task_index is None:
                self.meta.add_task(task)

        # Given tasks in natural language, find their corresponding task indices
        episode_buffer['task_index'] = np.array([self.meta.get_task_index(task) for task in tasks])

        for key, ft in self.features.items():
            if (key in ['index', 'episode_index', 'task_index'] or
                    ft['dtype'] in ['image', 'video']):
                continue
            episode_buffer[key] = np.stack(episode_buffer[key])

        self._save_episode_table(episode_buffer, episode_index)
        ep_stats = self.compute_episode_stats_buffer(episode_buffer, self.features)

        video_paths = {}
        video_count = 0
        for key, ep in self.episode_buffer.items():
            if 'observation.images' in key:
                video_path = self.root / self.meta.get_video_file_path(episode_index, key)
                video_paths[key] = str(video_path)
                if encode_now:
                    self._create_video(ep, video_path)
                video_count += 1
                video_info = {
                    'video.height': self.features[key]['shape'][0],
                    'video.width': self.features[key]['shape'][1],
                    'video.channels': self.features[key]['shape'][2],
                    'video.codec': 'libx264',
                    'video.pix_fmt': 'yuv420p',
                }
                self.meta.info['features'][key]['info'] = video_info

        self.save_meta_info(
            video_count,
            episode_index,
            episode_length,
            episode_tasks,
            ep_stats
        )
        return episode_buffer, episode_length

    def save_episode_without_video_encoding(self):
        episode_buffer, episode_length = self._save_episode_common(encode_now=False)
        self.append_episode_buffer(episode_buffer, episode_length)

    def get_episode_index(self):
        if self.episode_buffer is None or 'episode_index' not in self.episode_buffer:
            return None
        return self.episode_buffer['episode_index']

    def save_episode_without_write_image(self):
        self._save_episode_common(encode_now=True)

    def save_meta_info(
            self,
            video_count,
            episode_index,
            episode_length,
            episode_tasks,
            episode_stats):
        chunk = self.meta.get_episode_chunk(episode_index)
        if chunk >= self.meta.total_chunks:
            self.meta.info['total_chunks'] += 1
        self.meta.info['total_episodes'] += 1
        self.meta.info['total_frames'] += episode_length
        self.meta.info['total_videos'] += video_count
        self.meta.info['splits'] = {'train': f"0:{self.meta.info['total_episodes']}"}
        self.meta.info['robot_type'] = self._robot_type

        episode_dict = {
            'episode_index': episode_index,
            'tasks': episode_tasks,
            'length': episode_length,
        }

        write_info(self.meta.info, self.meta.root)
        write_episode(episode_dict, self.meta.root)
        write_episode_stats(episode_index, episode_stats, self.meta.root)

    def _create_video(
            self,
            image_buffer: list[np.ndarray],
            save_path: str):
        if not hasattr(self, 'encoders') or self.encoders is None:
            self.encoders = {}

        # Audit F62: bumped to preset='fast'/crf=23 for cleaner ACT training
        # inputs. The previous ultrafast/28 combo introduced visible chrominance
        # noise that the policy then has to memorize. Encoding stays async on a
        # background thread so the 30 Hz recording tick is not affected.
        self.encoders[save_path] = FFmpegEncoder(
                fps=self.fps,
                chunk_size=50,
                preset='fast',
                crf=23,
                pix_fmt='yuv420p',
                vcodec='libx264'
            )
        # JPEG-in-RAM mode (P1): the buffer holds JpegFrame objects. Wrap
        # it so the encoder decodes one chunk at a time (peak extra RAM ≈
        # chunk_size frames) instead of forcing a full-episode decode
        # up-front. Legacy RGB ndarrays pass through untouched.
        encoder_buffer = image_buffer
        if len(image_buffer) > 0 and isinstance(image_buffer[0], JpegFrame):
            encoder_buffer = _LazyDecodeBuffer(image_buffer)
        self.encoders[save_path].set_buffer(encoder_buffer)
        encoding_thread = threading.Thread(
            target=self.encoders[save_path].encode_video,
            args=(save_path,)
        )
        encoding_thread.start()

    def check_video_encoding_completed(self) -> bool:
        if not hasattr(self, 'encoders') or self.encoders is None:
            self.encoders = {}
            return True

        if self.encoders:
            all_completed = True
            completed_encoders = []

            for key, encoder in self.encoders.items():
                if not encoder.encoding_completed:
                    all_completed = False
                else:
                    completed_encoders.append(key)

            for key in completed_encoders:
                encoder = self.encoders[key]
                encoder.clear_buffer()
                del self.encoders[key]
                del encoder

            if all_completed:
                self.encoders = {}
                return True
            else:
                return False

        return True

    def check_append_buffer_completed(self) -> bool:
        return not self._append_in_progress

    def compute_episode_stats_buffer(self, episode_buffer, features):
        ep_stats = {}
        for key, data in episode_buffer.items():
            if features[key]['dtype'] == 'string':
                continue
            elif features[key]['dtype'] in ['image', 'video']:
                ep_ft_array = self._sample_images(data)
                axes_to_reduce = (0, 2, 3)
                keepdims = True
            else:
                ep_ft_array = data
                axes_to_reduce = 0
                keepdims = ep_ft_array.ndim == 1
            ep_stats[key] = get_feature_stats(
                ep_ft_array,
                axis=axes_to_reduce,
                keepdims=keepdims)
            if features[key]['dtype'] in ['image', 'video']:
                ep_stats[key] = {
                    k: v if k == 'count' else np.squeeze(
                        v / 255.0, axis=0) for k, v in ep_stats[key].items()
                }
        return ep_stats

    def _estimate_num_samples(
        self,
        dataset_len: int,
        min_num_samples: int = 100,
        max_num_samples: int = 10_000,
        power: float = 0.75
    ) -> int:
        if dataset_len < min_num_samples:
            min_num_samples = dataset_len
        return max(min_num_samples, min(int(dataset_len**power), max_num_samples))

    def _sample_indices(self, data_len: int) -> list[int]:
        num_samples = self._estimate_num_samples(data_len)
        return np.round(np.linspace(0, data_len - 1, num_samples)).astype(int).tolist()

    def _auto_downsample_height_width(
            self,
            img: np.ndarray,
            target_size: int = 150,
            max_size_threshold: int = 300):
        _, h, w = img.shape

        if max(w, h) < max_size_threshold:
            return img

        downsample_factor = int(w / target_size) if w > h else int(h / target_size)
        return img[:, ::downsample_factor, ::downsample_factor]

    def _sample_images(self, image_array) -> np.ndarray:
        sampled_indices = self._sample_indices(len(image_array))
        images = None
        for i, idx in enumerate(sampled_indices):
            img = image_array[idx]
            # JPEG-in-RAM mode (P1): the buffer holds JpegFrame, not
            # decoded RGB. Decode only the sparse sampled frames here.
            if isinstance(img, JpegFrame):
                img = img.decode()
            img = np.transpose(img, (2, 0, 1))
            img = self._auto_downsample_height_width(img)
            if images is None:
                images = np.empty((len(sampled_indices), *img.shape), dtype=np.uint8)
            images[i] = img

        return images
