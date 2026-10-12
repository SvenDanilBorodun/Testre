"""The cloud decodes a camera JPEG to the same pixels the robot's local path does (spec §9.2).

Image only: needs ``cv_bridge`` (ROS) next to the pip OpenCV that ``chunk_engine`` uses.
The local path is ``DataConverter.compressed_image2cvmat(msg, 'rgb8')`` =
``CvBridge.compressed_imgmsg_to_cv2(msg, 'rgb8')``; the cloud path is
``chunk_engine.decode_jpeg_rgb``. Equal by sha256 for deterministic q80, q95 and
progressive JPEGs (the shapes a robot camera, the bridge and a browser produce).
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

pytest.importorskip('cv_bridge')
pytest.importorskip('sensor_msgs')
cv2 = pytest.importorskip('cv2')

from cv_bridge import CvBridge  # noqa: E402
from sensor_msgs.msg import CompressedImage  # noqa: E402

try:   # the shipped package; an image older than the cloud files gets them on PYTHONPATH (spec §9.2)
    from physical_ai_server.inference import chunk_engine as CE
except ImportError:  # pragma: no cover - only against a pre-Cloud-Inferenz image
    import chunk_engine as CE


def _scene(w=640, h=480, seed=5):
    """A deterministic picture with edges, gradients and noise (what a JPEG actually has to code)."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    img = np.stack([(x * 255 // max(w - 1, 1)), (y * 255 // max(h - 1, 1)), ((x ^ y) & 0xFF)], -1)
    img = img.astype(np.int16) + rng.integers(-20, 21, (h, w, 3))
    img[h // 4:h // 2, w // 3:w // 2] = (30, 200, 90)
    return np.clip(img, 0, 255).astype(np.uint8)


def _jpegs():
    img = _scene()
    return {
        'q80': cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes(),
        'q95': cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes(),
        'progressive': cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80,
                                                  cv2.IMWRITE_JPEG_PROGRESSIVE, 1])[1].tobytes(),
        'q80_444': cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80, cv2.IMWRITE_JPEG_SAMPLING_FACTOR,
                                              cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444])[1].tobytes(),
    }


def _sha(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


@pytest.mark.parametrize('kind', ['q80', 'q95', 'progressive', 'q80_444'])
def test_decode_equals_cv_bridge_rgb8(kind):
    data = _jpegs()[kind]
    msg = CompressedImage()
    msg.format = 'jpeg'
    msg.data = data
    local = CvBridge().compressed_imgmsg_to_cv2(msg, 'rgb8')
    cloud = CE.decode_jpeg_rgb(data, 640, 480)
    assert cloud is not None and local.shape == cloud.shape == (480, 640, 3)
    assert local.dtype == cloud.dtype == np.uint8
    assert _sha(cloud) == _sha(local)
