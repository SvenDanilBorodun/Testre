"""make_dataset.py <root> [vcodec]   (vcodec default libsvtav1, LeRobot's default)

Build a tiny deterministic LeRobot v3.0 dataset shaped like an OMX recording
(spec §B.5): 3 episodes x 40 frames at 30 fps, 6 joints, two 48x64 cameras.
120 frames at batch 8 is exactly 15 steps per epoch, which is what lets the
resume proof put one resume mid-epoch (step 10) and one on an epoch boundary (15).
"""
import sys

import numpy as np
from lerobot.datasets.lerobot_dataset import LeRobotDataset

REPO_ID = "edubotics-e2e/synthetic"
NAMES = ["joint1", "joint2", "joint3", "joint4", "joint5", "gripper_joint_1"]
H, W = 48, 64
EPISODES, FRAMES = 3, 40


def main(root: str, vcodec: str = "libsvtav1") -> None:
    features = {
        "observation.state": {"dtype": "float32", "shape": (6,), "names": NAMES},
        "action": {"dtype": "float32", "shape": (6,), "names": NAMES},
        "observation.images.gripper": {"dtype": "video", "shape": (H, W, 3),
                                       "names": ["height", "width", "channels"]},
        "observation.images.scene": {"dtype": "video", "shape": (H, W, 3),
                                     "names": ["height", "width", "channels"]},
    }
    ds = LeRobotDataset.create(REPO_ID, fps=30, features=features, root=root,
                               robot_type="omx_f", use_videos=True, vcodec=vcodec)
    rng = np.random.default_rng(1234)
    for ep in range(EPISODES):
        for t in range(FRAMES):
            q = np.sin(np.arange(6) * 0.3 + t * 0.05 + ep).astype(np.float32)
            img1 = rng.integers(0, 255, size=(H, W, 3), dtype=np.uint8)
            img2 = rng.integers(0, 255, size=(H, W, 3), dtype=np.uint8)
            ds.add_frame({"observation.state": q, "action": q + 0.01,
                          "observation.images.gripper": img1, "observation.images.scene": img2,
                          "task": "pick the cube"})
        ds.save_episode()
    ds.finalize()
    print("frames", ds.meta.total_frames, "episodes", ds.meta.total_episodes)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "libsvtav1")
