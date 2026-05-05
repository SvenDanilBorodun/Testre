# 19 — Roboter Studio (Block-based authoring + classical CV)

> Hardware-only feature. Hidden when the WebApp is launched with `?cloud=1`.
> Lets students compose multi-step tasks as Blockly programs that drive the
> OMX-F arm via classical perception (HSV / YOLOX-tiny / AprilTag),
> TRAC-IK Cartesian motion, and a 4-step camera + hand-eye calibration wizard.

---

## §1 — Why this layer exists

The other student-facing tabs (Aufnahme, Training, Inferenz) all train on
demonstrations. Roboter Studio is the **first surface where a student
composes the task as logic** rather than recording it. The arm executes
deterministically, no policy in the loop. Two student personas:

- **First-week** student: drag four blocks (`Heimposition`, `wenn rot
  erkannt → aufnehmen → ablegen bei A`), press Start, see the arm sort
  cubes.
- **Project-week** student: chains AprilTag detection, COCO object class
  detection, and conditional logic to build a small "lab assistant".

The tab also ships the **classroom calibration kit** (board PDF, gripper
patch STL, AprilTag sheet) so a teacher can prep a station in <15 minutes.

---

## §2 — Architecture (one-glance)

```
React (physical_ai_manager)
    └─ pages/WorkshopPage.js
        ├─ components/Workshop/CalibrationWizard.jsx (4 steps)
        ├─ components/Workshop/BlocklyWorkspace.jsx  (PR4)
        ├─ components/Workshop/RunControls.jsx       (PR4)
        └─ components/Workshop/CameraFeedOverlay.jsx (PR2 — uses HTTP
             /stream pattern from ImageGridCell.js, NOT roslibjs Topic)
                │
                ▼ rosbridge JSON-RPC
ROS 2 (physical_ai_server)
    └─ physical_ai_server.py                (services + on_calibration / on_workflow flags)
        └─ workflow/
            ├─ calibration_manager.py       (PR1 — ChArUco + dual-solve hand-eye)
            ├─ auto_pose.py                 (PR1 — hemisphere sampler, IK stub until PR3)
            ├─ color_profile.py             (PR1 — Otsu + HSV percentiles)
            ├─ perception.py                (PR2 — HSV / YOLOX / AprilTag)
            ├─ projection.py                (PR2 — pixel → table-plane)
            ├─ ik_solver.py                 (PR3 — TRAC-IK + KDL fallback)
            ├─ trajectory_builder.py        (PR3 — quintic + chunked_publish)
            ├─ workflow_manager.py          (PR4 — runtime daemon)
            ├─ interpreter.py               (PR4 — Blockly JSON walker, allowlist)
            └─ handlers/                    (PR3 + PR4)
        └─ overlays/safety_envelope.py      (PR3 — extracted from inference_manager)
                │
                ▼ JointTrajectory publisher
/leader/joint_trajectory  (publisher, NOT action — see omx_f_follower_ai.launch.py:144)
```

### Persistence

- **Per-machine calibration files:** named docker volume `edubotics_calib`
  mounted at `/root/.cache/edubotics/` inside the `physical_ai_server`
  container. Contents: `gripper_intrinsics.yaml`, `scene_intrinsics.yaml`,
  `gripper_handeye.yaml`, `scene_handeye.yaml` (also stores derived
  `z_table`), `color_profile.yaml`. Files are written via
  `cv2.FileStorage`. Re-running a step overwrites that step's YAML.
- **Per-user workflows:** Supabase `workflows` table (PR5). RLS lets
  students read their own + classroom templates.

### Cloud-mode hide

`StudentApp.js` filters the `navItems` array by `hardwareOnly` when
`isCloudOnlyMode()` (URL `?cloud=1`). Roboter Studio, Aufnahme, and
Inferenz are flagged hardware-only; Training and Daten remain visible
in cloud mode.

---

## §3 — Calibration (PR1)

### Board

ChArUco, **7×5 squares, 30 mm square edge, 22 mm marker edge,
DICT_5X5_250**. Generate via `tools/generate_charuco.py` (PR6). Mount on
a foam-board or thick cardboard — paper warps and silently corrupts
intrinsics.

### Step sequence

| # | Step | Captured | Solved by |
|---|---|---|---|
| 1 | Greifer-Kamera intrinsisch | 12 board views from different angles, hand-held | `cv2.aruco.calibrateCameraCharucoExtended` |
| 2 | Szenen-Kamera intrinsisch | 12 board views, board hand-held in front of fixed scene cam | same as 1 |
| 3 | Greifer-Kamera Hand-Auge (eye-in-hand) | 14 (gripper-pose, board-pose) pairs sampled by `auto_pose.py` | `cv2.calibrateHandEye` × {PARK, TSAI} |
| 4 | Szenen-Kamera Hand-Auge (eye-to-base) | 14 pairs with ChArUco mounted on the gripper | same as 3, gripper poses inverted |
| 5 | Farbprofil | 1 frame per canonical colour (rot/grün/blau/gelb), Otsu (auto-polarity) + LAB cluster mean/std | `color_profile.py` (via `/calibration/capture_color`, finalised by `/calibration/solve` with step `color_profile`) |

### Dual-solve disagreement check

PARK and TSAI minimise different cost functions. If their results diverge
by **>2°** rotation or **>5 mm** translation, `_solve_handeye` **refuses
to persist the YAML** and returns a German "Hand-Auge-Solve abgewiesen"
message — the v1 ship only warned and saved anyway. Re-capturing from
the existing 14 poses is free. See `calibration_manager._solve_handeye`.

### `z_table` derivation

For the scene camera's eye-to-base solve, the manager records the median
z-coordinate of the board origin across all captured poses. This gets
written to `scene_handeye.yaml` and is the plane that pixel-click
destinations are projected onto in PR2.

---

## §4 — Block library (PR4)

| Category | Hex | Blocks |
|---|---|---|
| Bewegung | `#3b82f6` | `Heimposition`, `bewege zu (Pos)`, `aufnehmen (Obj)`, `ablegen bei (Ziel)`, `Greifer öffnen`, `Greifer schließen`, `warte X Sekunden` |
| Wahrnehmung | `#22c55e` | `erkenne Farbe %`, `warte bis Farbe % erkannt`, `Anzahl Farbe %`, `erkenne Marker %`, `warte bis Marker % erkannt`, `erkenne Objekt %`, `warte bis Objekt % erkannt`, `Anzahl Objekt %` |
| Ziele | `#f59e0b` | `setze Ziel = Pin (Klick)`, `setze Ziel = aktuelle Position` |
| Logik | `#eab308` | `wenn`, `wenn-sonst`, `wiederhole für immer`, `wiederhole X mal`, `für jedes obj in liste`, vergleichs- und logische Operatoren |
| Variablen | (Blockly default) | dynamische Variablen pro Run-Scope |
| Ausgabe | `#a855f7` | `melde X`, `Ton spielen` |

**Object-class dropdown (16 curated COCO classes):** Flasche, Tasse, Gabel,
Löffel, Schüssel, Banane, Apfel, Orange, Karotte, Brokkoli, Maus,
Fernbedienung, Handy, Buch, Schere, Teddybär. Single source of truth in
server `coco_classes.py`; frontend mirror in
`components/Workshop/blocks/messages_de.js`. A Jest test diffs the two and
fails the build on drift.

### Allowlist

`interpreter.py` rejects any block type not in `ALLOWED_BLOCK_TYPES` with
the German error `Unbekannter Block-Typ: …`. The allowlist is the **only**
defence against malicious workflow JSON; the table is reviewed when adding
a new block.

---

## §5 — Stop semantics

- **Command-side latency target:** < 100 ms. The next trajectory chunk's
  `publish()` is suppressed within 100 ms of `_should_stop = True`.
  Pinned by `test_chunked_publish_stop_latency.py`.
- **Physical overshoot:** up to one chunk (≤ 1 s of motion). The
  controller still finishes the in-flight `JointTrajectory`.
- **Recovery sequence (implemented in `WorkflowManager._run_recovery`):**
  publish hold-current-q for 1 s → open gripper over 0.5 s → return
  to home pose over 3 s. Triggered on `'stopped'` and `'error'`
  terminal phases, not on a clean `'finished'` run.

This is documented because the brief originally over-promised "<100 ms
physical halt"; the realistic guarantee is command-side suppression with a
bounded physical overshoot, followed by an auto-home that always
completes.

---

## §6 — Safety envelope

Both `inference_manager` (existing) and `workflow_manager` (PR4)
instantiate their own `SafetyEnvelope` (PR3, extracted from
`inference_manager.py:305-367`). State (action_min/max/max_delta_per_tick)
is configured per-manager from the same source. Today the source is
hardcoded constants in `physical_ai_server.py:829-849`; the
`safety_envelope:` section in `omx_f_config.yaml` is reserved for a future
config-driven loader.

The envelope rejects NaN/Inf actions, clamps per-joint, caps per-tick
delta. Logs prefix `[STOPP]` or `[WARNUNG]` in German for the student;
diagnostic info follows in English for the maintainer.

---

## §7 — File map

### Server

```
physical_ai_tools/physical_ai_interfaces/
├─ srv/StartCalibration.srv
├─ srv/CalibrationCaptureFrame.srv
├─ srv/CalibrationSolve.srv
├─ srv/AutoPoseSuggest.srv
├─ srv/ExecuteCalibrationPose.srv
├─ srv/MarkDestination.srv
├─ srv/StartWorkflow.srv
├─ srv/StopWorkflow.srv
└─ msg/WorkflowStatus.msg

physical_ai_tools/physical_ai_server/physical_ai_server/workflow/
├─ __init__.py
├─ calibration_manager.py
├─ auto_pose.py
├─ color_profile.py
├─ perception.py            (PR2)
├─ coco_classes.py          (PR2)
├─ projection.py            (PR2)
├─ ik_solver.py             (PR3)
├─ trajectory_builder.py    (PR3)
├─ workflow_manager.py      (PR4)
├─ interpreter.py           (PR4)
└─ handlers/                (PR3 + PR4)

robotis_ai_setup/docker/
├─ docker-compose.yml                                 (named volume edubotics_calib)
├─ physical_ai_server/Dockerfile                      (OpenCV-contrib, pupil-apriltags, onnxruntime CPU,
│                                                      manual-license-audit, YOLOX-tiny ONNX bake-in
│                                                      via curl-with-sha256-pin to /opt/edubotics/)
└─ physical_ai_server/overlays/safety_envelope.py     (PR3)
```

### Frontend

```
physical_ai_tools/physical_ai_manager/src/
├─ utils/cloudMode.js                                 (single source of truth for ?cloud=1)
├─ pages/WorkshopPage.js
├─ pages/teacher/WorkflowTemplatesPage.js             (PR6)
├─ features/workshop/workshopSlice.js
├─ services/workflowApi.js                            (PR5)
├─ hooks/useSupabaseWorkflows.js                      (PR5)
└─ components/Workshop/
    ├─ CalibrationWizard.jsx
    ├─ IntrinsicCalibStep.jsx
    ├─ HandEyeCalibStep.jsx
    ├─ ColorProfileStep.jsx
    ├─ BlocklyWorkspace.jsx                           (PR4)
    ├─ RunControls.jsx                                (PR4)
    ├─ CameraFeedOverlay.jsx                          (PR2)
    ├─ TemplatePicker.jsx                             (PR5)
    └─ blocks/
        ├─ motion.js
        ├─ perception.js
        ├─ destinations.js
        ├─ logic.js
        ├─ output.js
        ├─ toolbox.js
        └─ messages_de.js
```

### Cloud API

```
robotis_ai_setup/cloud_training_api/app/
├─ main.py                                            (extend _RATE_LIMIT_RULES, register router)
└─ routes/
    ├─ workflows.py                                   (PR5 — _assert_workflow_owned helper)
    └─ teacher.py                                     (PR5 — add /classrooms/{cid}/workflow-templates)
```

### Supabase

```
robotis_ai_setup/supabase/
├─ 008_workflows.sql                                  (PR5 — table, RLS mirroring 004, idempotent realtime DO)
└─ rollback/008_workflows_rollback.sql
```

---

## §8 — Verification gates per PR

PR-by-PR gates live in the implementation plan
(`.claude/plans/dive-deep-this-is-logical-sundae.md` — original) plus the
verification report (`.claude/plans/dive-very-deep-rustling-quokka.md` —
corrections). Cross-cutting:

- License audit: `pip-licenses --fail-on='AGPL-3.0;AGPL-3.0-only;AGPL-3.0-or-later'`
  passes; no `ultralytics` substring anywhere.
- Image-size delta < 100 MB (CPU-only ONNX runtime, not GPU).
- All overlay changes verified per `WORKFLOW-overlay-change.md`.
- All Supabase migrations verified per `WORKFLOW-supabase-migration.md`.
- German for student-facing strings; English for maintainer log lines.

---

**Last verified:** 2026-05-04
