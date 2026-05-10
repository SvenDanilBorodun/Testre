# Fine-tuning D-FINE-N for classroom perception

D-FINE-N (Apache-2.0, https://github.com/Peterande/D-FINE) is the
recommended Phase-3 upgrade for the local closed-vocabulary detector.
It dominates YOLOX-tiny on COCO mAP at the same parameter budget — but
COCO-class accuracy doesn't always translate to classroom-scale Lego
cubes and 3D-printed props. This recipe captures a small set of
classroom photos and fine-tunes a checkpoint that performs well on
the actual objects students use.

> **Current state (2026-05).** The perception overlay reads
> ``EDUBOTICS_DETECTOR`` (default ``yolox-tiny``). Setting
> ``EDUBOTICS_DETECTOR=dfine-n`` + ``EDUBOTICS_DFINE_ONNX=/opt/edubotics/dfine_n.onnx``
> swaps to the D-FINE-N ONNX path. The repo does NOT yet ship a
> D-FINE-N ONNX in the Docker image — operators must follow this
> recipe to export their own, host it, and bake the path/SHA into
> ``robotis_ai_setup/docker/physical_ai_server/Dockerfile``. Until
> then, the default YOLOX-tiny path is active.

The helper scripts mentioned below (``tools/eval_perception.py``,
``tools/capture_eval_set.py``, ``tools/onnx_smoke.py``,
``tools/eval_open_vocab.py``) are part of the same follow-up — they
are documented for the implementer's reference and will land
alongside the D-FINE-N integration. If you need to evaluate now,
clone the upstream D-FINE repo and use its own ``tools/`` scripts.

## Prerequisites

- ~100 labeled classroom photos (per-class), 1280×720 JPEG.
- A laptop with ≥16 GB RAM (no GPU needed for this scale).
- Local clone of `https://github.com/Peterande/D-FINE.git`.

## Step 1 — Capture and label

```bash
python tools/capture_eval_set.py \
    --classes lego_red,lego_blue,lego_yellow,lego_green \
    --frames-per-class 100 \
    --out classroom_dataset
```

Expects two USB cameras on `/dev/video0` (gripper) and `/dev/video2`
(scene); writes `classroom_dataset/<class>/<idx>.jpg`. Annotate with
**Label Studio** or **CVAT** in COCO format, exporting as
`annotations.json`.

## Step 2 — Fine-tune

```bash
git clone https://github.com/Peterande/D-FINE.git /tmp/dfine
cd /tmp/dfine
pip install -e .

python -m dfine.train \
    --config configs/dfine_n.yml \
    --resume model_zoo/dfine_n_coco.pth \
    --train-anno classroom_dataset/annotations.json \
    --train-img classroom_dataset \
    --val-anno classroom_dataset/annotations_val.json \
    --val-img classroom_dataset \
    --output dfine_n_classroom \
    --epochs 30
```

Memory cap: D-FINE-N at batch=4 fits in 8 GB CPU RAM (slow but
feasible — ~6 hours for 30 epochs on a laptop). For overnight runs
on a teacher's machine, batch=2 with `--workers 2` is reliable.

## Step 3 — Export to ONNX

```bash
python -m dfine.export_onnx \
    --weights dfine_n_classroom/best.pth \
    --input-size 640 640 \
    --opset 17 \
    --out dfine_n_classroom.onnx
```

Verify with the smoke script:
```bash
python tools/onnx_smoke.py dfine_n_classroom.onnx tools/eval_data/lego_red/000.jpg
```

## Step 4 — Hash, host, pin

```bash
sha256sum dfine_n_classroom.onnx > dfine_n_classroom.onnx.sha256
```

Upload the ONNX (and the sha256 sidecar) as a release asset on
github.com/SvenDanilBorodun/Testre. Update
`robotis_ai_setup/docker/physical_ai_server/Dockerfile`:
- Replace the `https://...yolox_tiny.onnx` URL with the new release URL.
- Replace the SHA-256 hash literal.
- Update the path to `/opt/edubotics/dfine_n.onnx`.

## Step 5 — Switch the perception loader

Set the environment variable so the overlay's perception loader picks
the D-FINE-N decoder path:

```bash
EDUBOTICS_DETECTOR=dfine-n
EDUBOTICS_DFINE_ONNX=/opt/edubotics/dfine_n.onnx
```

The overlay falls back to YOLOX-tiny if these are unset, so the
rollout is a feature flag, not a one-way migration.

## Reverting

If post-deploy evaluation shows D-FINE-N worse than YOLOX-tiny on a
classroom-specific class, revert by clearing the env var and
restarting the physical_ai_server container:

```bash
EDUBOTICS_DETECTOR= docker compose restart physical_ai_server
```
