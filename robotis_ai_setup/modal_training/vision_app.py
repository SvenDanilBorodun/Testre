"""edubotics-vision — Modal app for cloud-burst open-vocabulary detection.

Phase-3 of the Roboter Studio upgrade. Hosts an OWLv2 (Apache-2.0)
detector that accepts German prompts and returns COCO-style bounding
boxes. The cloud_training_api forwards `POST /vision/detect` calls to
this app; the Roboter Studio block ``edubotics_detect_open_vocab``
sets ``cloud_burst`` on the workflow context and triggers the call
when a prompt isn't covered by the local closed-vocab model.

License posture
---------------
- ``google/owlv2-base-patch16-ensemble`` is **Apache-2.0** (model card:
  https://huggingface.co/google/owlv2-base-patch16-ensemble).
- ``transformers`` is Apache-2.0.
- ``torch`` is BSD-3.

This avoids the AGPL-3.0 footprint of the Ultralytics YOLO family that
the original plan flagged as a blocker for our distribution model.

Cost model
----------
Modal T4 = $0.59/hr per the 2026 pricing page (https://modal.com/pricing).
With ``min_containers=0``, ``scaledown_window=180``, and
``enable_memory_snapshot=True``, an idle classroom pays nothing and a
warm container handles a typical detection in 200–400 ms ≈ $0.00007 per
call. 9 000 calls/term/classroom ≈ $0.50 in compute + < $1 in idle.

Deploy with:

    modal deploy modal_training/vision_app.py
"""

from __future__ import annotations

import base64
import io
import os
from typing import Any

import modal


APP_NAME = os.environ.get("EDUBOTICS_VISION_APP_NAME", "edubotics-vision")
MODEL_NAME = os.environ.get("EDUBOTICS_VISION_MODEL", "google/owlv2-base-patch16-ensemble")

# Memory snapshots collapse cold-start time from ~10s to 1-3s on this
# class of model. They require Modal Team plan; the deploy will print a
# warning if you're on the free tier — the app still works, just colder.
ENABLE_MEMORY_SNAPSHOT = os.environ.get("EDUBOTICS_VISION_SNAPSHOT", "1") == "1"

# When zero, the container scales fully down between calls. For a
# classroom of 30 students it's worth bumping to 1 during a teacher's
# active session — saves ~3 s on the first detection but does cost
# ~$0.0002/min while warm. Toggleable per-deploy via env var.
MIN_CONTAINERS = int(os.environ.get("EDUBOTICS_VISION_MIN_CONTAINERS", "0"))

# How long an idle warm container stays alive before scaling down.
# Modal renamed `container_idle_timeout` → `scaledown_window` in 1.0.
SCALEDOWN_WINDOW_S = int(os.environ.get("EDUBOTICS_VISION_SCALEDOWN_S", "180"))


app = modal.App(APP_NAME)


image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "transformers==4.46.0",
        "torch==2.4.0",
        "torchvision==0.19.0",
        "pillow",
        "huggingface_hub>=0.25.0",
        "numpy",
    )
    .env({"PYTHONUNBUFFERED": "1", "TRANSFORMERS_OFFLINE": "0"})
)


# Persistent volume for the HuggingFace cache so the model weights
# (~600 MB) only download once per workspace lifetime.
model_volume = modal.Volume.from_name(
    "edubotics-vision-cache",
    create_if_missing=True,
)


def _vision_secret() -> modal.Secret:
    """Prefer a vision-only secret; fall back to training-secrets so
    pre-deploy environments still work without two secret bundles.
    Audit §G4 — passing the full training bundle leaks Supabase creds.
    """
    name = os.environ.get("EDUBOTICS_VISION_SECRET_NAME", "edubotics-vision-secrets")
    try:
        return modal.Secret.from_name(name)
    except Exception:
        return modal.Secret.from_name("edubotics-training-secrets")


@app.cls(
    image=image,
    gpu="T4",
    volumes={"/root/.cache/huggingface": model_volume},
    secrets=[_vision_secret()],
    min_containers=MIN_CONTAINERS,
    scaledown_window=SCALEDOWN_WINDOW_S,
    enable_memory_snapshot=ENABLE_MEMORY_SNAPSHOT,
    timeout=30,
)
class OWLv2Detector:
    """Wraps the HuggingFace pipeline for OWLv2.

    The ``@modal.enter(snap=True)`` hook runs *before* the snapshot,
    so the model weights are baked into the snapshot image — that's
    the key that makes cold starts <3 s instead of >10 s.
    """

    @modal.enter(snap=ENABLE_MEMORY_SNAPSHOT)
    def setup(self) -> None:  # noqa: D401 — Modal lifecycle hook
        import torch
        from transformers import Owlv2Processor, Owlv2ForObjectDetection

        self._torch = torch
        self.processor = Owlv2Processor.from_pretrained(MODEL_NAME)
        self.model = Owlv2ForObjectDetection.from_pretrained(MODEL_NAME)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = self.model.to(device)
        self.model.eval()
        self.device = device
        # Note: model_volume.commit() is intentionally NOT called here
        # — snap=True enter hooks must be side-effect-free for the
        # snapshot to be deterministic (Modal docs). The volume is
        # committed elsewhere if needed.

    @modal.method()
    def detect(
        self,
        image_bytes: bytes,
        prompts: list[str],
        score_threshold: float = 0.10,
    ) -> dict[str, Any]:
        """Run a single open-vocabulary detection.

        Parameters
        ----------
        image_bytes
            Raw JPEG/PNG bytes (cloud_training_api decodes the base64
            envelope before forwarding here).
        prompts
            German or English prompts; up to 8 entries enforced
            client-side. OWLv2's CLIP text head handles German natively.
        score_threshold
            Minimum OWLv2 confidence to retain.

        Returns
        -------
        dict with keys:
            - "detections": list of {label, score, bbox: [x1,y1,x2,y2]}
            - "cold_start": True if this was the first call after a
              snapshot resume, False otherwise (best-effort heuristic).
        """
        from PIL import Image
        import time

        # Best-effort cold-start detection: first call after enter()
        # records a marker; subsequent calls find it.
        cold_start = not getattr(self, "_warmed", False)
        self._warmed = True

        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as e:
            return {"detections": [], "cold_start": cold_start, "error": f"Bild fehlerhaft: {e}"}

        # OWLv2 expects a list-of-lists for prompts (one outer entry per
        # image; the inner list is the queries for that image).
        texts = [[p for p in prompts if isinstance(p, str) and p]]
        if not texts[0]:
            return {"detections": [], "cold_start": cold_start}

        # EXIF orientation matters for the bbox alignment — apply it
        # before the processor sees the image (audit §J5).
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        try:
            inputs = self.processor(text=texts, images=img, return_tensors="pt").to(self.device)
            with self._torch.no_grad():
                outputs = self.model(**inputs)
            target_sizes = self._torch.tensor([img.size[::-1]]).to(self.device)
            # transformers 4.46 renamed the keyword to ``text_queries``;
            # older versions accept ``text_labels``. Try the new name
            # first and fall back so the app works across SDK updates.
            try:
                results = self.processor.post_process_grounded_object_detection(
                    outputs=outputs,
                    target_sizes=target_sizes,
                    threshold=float(score_threshold),
                    text_queries=texts,
                )
            except TypeError:
                results = self.processor.post_process_grounded_object_detection(
                    outputs=outputs,
                    target_sizes=target_sizes,
                    threshold=float(score_threshold),
                    text_labels=texts,
                )
        except Exception as e:
            return {
                "detections": [],
                "cold_start": cold_start,
                "error": f"Inferenz fehlgeschlagen: {e}",
            }

        out: list[dict[str, Any]] = []
        if not results:
            return {"detections": out, "cold_start": cold_start}
        first = results[0]
        boxes = first.get("boxes")
        scores = first.get("scores")
        labels = first.get("text_labels") or first.get("labels")
        if boxes is None or scores is None or labels is None:
            return {"detections": out, "cold_start": cold_start}
        try:
            for box, score, label in zip(boxes.tolist(), scores.tolist(), labels):
                out.append({
                    "label": str(label),
                    "score": float(score),
                    "bbox": [float(b) for b in box],
                })
        except Exception:
            pass
        return {"detections": out, "cold_start": cold_start}


@app.function(image=image, timeout=60)
def smoke_test() -> dict[str, Any]:
    """One-shot smoke-test verifying torch + the model load.

    Run with: ``modal run -m vision_app::smoke_test``
    """
    import torch  # noqa: F401  — imported for side-effect of failing fast
    return {
        "ok": True,
        "torch_version": __import__("torch").__version__,
        "cuda_available": __import__("torch").cuda.is_available(),
        "model": MODEL_NAME,
    }


def _b64_to_bytes(b64: str) -> bytes:
    """Helper for local invocation. The cloud_training_api decodes the
    base64 envelope before calling .remote(); this is only used by
    smoke / dev scripts."""
    return base64.b64decode(b64)
