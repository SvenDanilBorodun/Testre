"""Contract test: LeRobotDatasetWrapper.compute_episode_stats_buffer must
stay numerically equivalent to upstream LeRobot's compute_episode_stats.

WHY THIS EXISTS (P3). The EduBotics recording path does NOT use upstream
``compute_episode_stats``. ROBOTIS's ``LeRobotDatasetWrapper`` ships a
re-implementation, ``compute_episode_stats_buffer`` (plus copies of
``estimate_num_samples`` / ``sample_indices`` / ``auto_downsample_*``),
because the recording buffer holds in-RAM frames rather than the on-disk
image paths upstream expects. Those per-episode stats are written into
``meta/episodes_stats`` and become the **input-normalization constants the
policy is trained against on Modal**. If the re-implementation ever drifts
from upstream semantics (a LeRobot bump changes the sampling heuristic, the
reduction axes, or the /255 image normalization), training silently learns
the wrong normalization and the failure is nearly undebuggable from
symptoms. This test turns that silent drift into a red build.

It also covers the P1 JPEG-in-RAM path: image stats computed from a list of
``JpegFrame`` objects must equal the stats computed from the same frames
already decoded to RGB ndarrays (isolating the "_sample_images decodes
JpegFrame" logic from JPEG's own lossiness).

RUNNABILITY. The wrapper module imports ``physical_ai_server`` and
``lerobot`` (+ numpy/cv2/Pillow), which are present only inside the
``physical-ai-server`` image (or a full dev env with those installed). When
any dependency is missing the whole case skips cleanly — matching the rest
of robotis_ai_setup/tests, which never pulls heavy ML deps. Run it inside
the image, e.g.:

    docker run --rm -v "$PWD":/w -w /w nettername/physical-ai-server \
        python -m pytest robotis_ai_setup/tests/test_lerobot_stats_contract.py
"""

import tempfile
import unittest
from pathlib import Path


def _load_deps():
    """Import everything the contract needs. Returns a namespace dict on
    success, or None when any dependency is unavailable (→ skip)."""
    try:
        import numpy as np  # noqa: F401
        import cv2  # noqa: F401
        from PIL import Image  # noqa: F401
        from lerobot.datasets.compute_stats import compute_episode_stats
        # The OVERLAID wrapper (installed as part of physical_ai_server in
        # the image). Importing the in-tree physical_ai_tools copy instead
        # would test the pre-overlay code, so we deliberately import the
        # package path, not a file path.
        from physical_ai_server.data_processing.lerobot_dataset_wrapper import (
            JpegFrame,
            LeRobotDatasetWrapper,
        )
    except Exception:
        return None
    return {
        'np': np,
        'cv2': cv2,
        'Image': Image,
        'compute_episode_stats': compute_episode_stats,
        'JpegFrame': JpegFrame,
        'LeRobotDatasetWrapper': LeRobotDatasetWrapper,
    }


_DEPS = _load_deps()


@unittest.skipUnless(
    _DEPS is not None,
    'numpy/cv2/Pillow/lerobot/physical_ai_server not importable — '
    'run inside the physical-ai-server image',
)
class TestStatsContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        d = _DEPS
        cls.np = d['np']
        cls.cv2 = d['cv2']
        cls.Image = d['Image']
        cls.compute_episode_stats = staticmethod(d['compute_episode_stats'])
        cls.JpegFrame = d['JpegFrame']
        # compute_episode_stats_buffer / _sample_images / _sample_indices /
        # _estimate_num_samples / _auto_downsample_height_width use no
        # instance state, so a bare instance (no .create(), no torch /
        # datasets) is sufficient to exercise them.
        cls.wrapper = d['LeRobotDatasetWrapper'].__new__(d['LeRobotDatasetWrapper'])

    def _assert_stats_close(self, a, b, atol=0.0, rtol=1e-6):
        np = self.np
        self.assertEqual(set(a), set(b))
        for key in a:
            self.assertEqual(set(a[key]), set(b[key]), msg=f'stat keys for {key}')
            for stat in a[key]:
                self.assertTrue(
                    np.allclose(a[key][stat], b[key][stat], atol=atol, rtol=rtol),
                    msg=(
                        f"feature '{key}' stat '{stat}' diverged:\n"
                        f"  wrapper:  {a[key][stat]}\n"
                        f"  upstream: {b[key][stat]}"
                    ),
                )

    def test_vector_feature_stats_match_upstream_exactly(self):
        """observation.state / action go through the identical non-image
        branch in both implementations — they must match to float tol."""
        np = self.np
        rng = np.random.default_rng(0)
        data = rng.standard_normal((137, 6)).astype(np.float32)
        features = {'observation.state': {'dtype': 'float32', 'shape': (6,)}}

        ours = self.wrapper.compute_episode_stats_buffer(
            {'observation.state': data}, features)
        upstream = self.compute_episode_stats(
            {'observation.state': data}, features)

        self._assert_stats_close(ours, upstream)

    def test_image_feature_stats_match_upstream(self):
        """Image stats from in-RAM ndarrays (wrapper) must equal upstream
        stats from the same pixels written to PNG and loaded by path."""
        np = self.np
        rng = np.random.default_rng(1)
        h, w, n = 64, 48, 90
        frames = [rng.integers(0, 256, (h, w, 3), dtype=np.uint8) for _ in range(n)]
        features = {
            'observation.images.cam': {'dtype': 'video', 'shape': (h, w, 3)}
        }

        ours = self.wrapper.compute_episode_stats_buffer(
            {'observation.images.cam': list(frames)}, features)

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, fr in enumerate(frames):
                p = str(Path(tmp) / f'{i:06d}.png')
                # Pillow stores/loads RGB faithfully, matching how
                # upstream load_image_as_numpy reads it back.
                self.Image.fromarray(fr, mode='RGB').save(p)
                paths.append(p)
            upstream = self.compute_episode_stats(
                {'observation.images.cam': paths}, features)

        # PNG is lossless, so equality should be tight. Allow a hair of
        # tolerance for the float /255 normalization path only.
        self._assert_stats_close(ours, upstream, atol=1e-6, rtol=1e-4)

    def test_jpegframe_stats_equal_decoded_array_stats(self):
        """P1: stats over a JpegFrame list must equal stats over the same
        frames pre-decoded to ndarrays — proving _sample_images decodes
        JpegFrame correctly (independent of JPEG's own lossiness)."""
        np = self.np
        rng = np.random.default_rng(2)
        h, w, n = 80, 60, 75
        frames = [rng.integers(0, 256, (h, w, 3), dtype=np.uint8) for _ in range(n)]
        features = {
            'observation.images.cam': {'dtype': 'video', 'shape': (h, w, 3)}
        }

        jpeg_frames = []
        for fr in frames:
            bgr = self.cv2.cvtColor(fr, self.cv2.COLOR_RGB2BGR)
            ok, enc = self.cv2.imencode('.jpg', bgr)
            self.assertTrue(ok, 'cv2.imencode failed')
            jpeg_frames.append(self.JpegFrame(enc.flatten(), (h, w, 3)))

        decoded = [jf.decode() for jf in jpeg_frames]

        stats_jpeg = self.wrapper.compute_episode_stats_buffer(
            {'observation.images.cam': list(jpeg_frames)}, features)
        stats_decoded = self.wrapper.compute_episode_stats_buffer(
            {'observation.images.cam': list(decoded)}, features)

        # Same bytes decoded the same way → exact match.
        self._assert_stats_close(stats_jpeg, stats_decoded, atol=0.0, rtol=0.0)

    def test_jpegframe_decode_roundtrips_to_rgb(self):
        """JpegFrame.decode() returns an (h, w, c) RGB uint8 array whose
        shape matches the carried .shape."""
        np = self.np
        h, w = 48, 64
        rgb = np.random.default_rng(3).integers(0, 256, (h, w, 3), dtype=np.uint8)
        bgr = self.cv2.cvtColor(rgb, self.cv2.COLOR_RGB2BGR)
        ok, enc = self.cv2.imencode('.jpg', bgr)
        self.assertTrue(ok)
        jf = self.JpegFrame(enc.flatten(), (h, w, 3))

        self.assertEqual(jf.shape, (h, w, 3))
        out = jf.decode()
        self.assertEqual(out.shape, (h, w, 3))
        self.assertEqual(out.dtype, np.uint8)
        # nbytes accounting reflects the compressed size, not the decode.
        self.assertEqual(jf.nbytes, int(enc.flatten().nbytes))
        self.assertLess(jf.nbytes, h * w * 3)


if __name__ == '__main__':
    unittest.main()
