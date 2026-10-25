"""Entry point of the CPU PROOF image only (Dockerfile.proof), never of the trainer image.

A fake RunPod pod (e2e/fake_runpod.py) runs this instead of entry.py. Two harness
substitutions, nothing else: the GPU host check is a no-op (a CPU container has no
GPU) and LeRobot trains on --policy.device=cpu. Everything else, the save cadence
included (the E2E API sets TRAINING_SAVE_FREQ=20), comes from the API's `start`
spec exactly as in production.
"""

import sys

import training_handler

training_handler._assert_training_host = lambda: None
training_handler.TRAIN_DEVICE = "cpu"

import entry  # noqa: E402

if __name__ == "__main__":
    sys.exit(entry.main())
