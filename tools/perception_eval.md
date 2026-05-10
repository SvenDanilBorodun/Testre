# Roboter Studio perception evaluation

> **Current state (2026-05).** The helper Python scripts mentioned
> below (``tools/eval_perception.py`` etc.) are NOT yet committed to
> this repo. The evaluation methodology is documented for the
> implementer's reference; runnable scripts will land alongside the
> D-FINE-N integration. Until then, follow the methodology with
> off-the-shelf tools (Ultralytics' ``yolo val`` or the upstream
> D-FINE/YOLOX repo evaluation scripts) and record results under
> ``tools/eval_results/``.

A reproducible recipe for evaluating the on-device closed-vocabulary
detector (currently YOLOX-tiny, Apache-2.0; an optional D-FINE-N drop-in
for the future) and the cloud open-vocabulary path (OWLv2 on Modal).

## What we measure

For a 50-image hold-out set captured in a real classroom:

| Metric | Tool | Threshold |
|---|---|---|
| Precision per class | `cv2.dnn.NMSBoxes`-style match | 80% (Lego cubes), 70% (cluttered scenes) |
| Recall per class | manual labels | 60% (acceptable) |
| Latency per frame, CPU | Python `time.perf_counter()` | ≤200 ms |
| False-positive rate | `# detections / # frames` | <0.1 per frame |
| German-prompt success rate (OWLv2) | manual review | ≥75% on classroom vocabulary |

## Capture set

Take 50 photos with the gripper or scene camera at 1280×720, mixing:
- 10 photos of a single colored Lego cube on the table.
- 10 photos with two cubes of different colors.
- 10 photos with classroom debris (pens, paper) plus one target object.
- 10 photos at oblique angles.
- 10 photos under different lighting (shaded vs. direct light).

Save as `tools/eval_data/<class>/<idx>.jpg` with sibling `.json` label files
(`{label, bbox: [x,y,w,h]}` per detection).

## Closed-vocab evaluation

```bash
# From the repo root:
python tools/eval_perception.py \
    --model yolox-tiny \
    --weights /opt/edubotics/yolox_tiny.onnx \
    --images tools/eval_data \
    --out tools/eval_results/yolox_baseline.json
```

Output bundle:
- Confusion matrix per class.
- Per-class precision / recall.
- Mean latency (median, p95) on a single CPU thread.

When we add D-FINE-N alongside YOLOX-tiny:

```bash
python tools/eval_perception.py \
    --model dfine-n \
    --weights /opt/edubotics/dfine_n.onnx \
    --images tools/eval_data \
    --out tools/eval_results/dfine_baseline.json
```

The script emits a side-by-side comparison table. We accept the swap
only if D-FINE-N matches or beats YOLOX-tiny on **every** class with
≤30% latency increase.

## Open-vocab evaluation

For OWLv2 on Modal:

```bash
modal run -m vision_app::smoke_test
python tools/eval_open_vocab.py \
    --prompts "rote Tasse" "gelbe Banane" "grüner Würfel" "Schraube" \
    --images tools/eval_data \
    --out tools/eval_results/owlv2.json
```

Pass criterion: ≥3 of 4 prompts produce a true positive on at least
70% of frames where the object is visible. Latency budget per call:
median ≤500 ms warm, ≤3 s cold start.

## Running on a fresh classroom

The eval set is intentionally small so a teacher can capture it in
~10 minutes between class periods. The full sweep + report should
complete in under 5 minutes on a teacher's laptop with the model
ONNX cached locally.
