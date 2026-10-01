# Edu:1 classroom station: hardware and one-step calibration

**Status:** design proposal, 2026-10-01. Nothing here is built or rig-validated.
Every number is either read from the code, measured on the shipped meshes and
URDF through the repo's own solver, or produced by the Monte-Carlo tool
`tools/edu1_kit_calibration_budget.py`. Every assumption that tool makes is a
named sigma in its `SIGMA` table (Appendix A). The bench gates that turn this
into fact are listed in section 9.

**Scope:** the hardware for an `edu1_studio` Roboter-Studio station (arm,
camera tower, mat, base plate, servos) and the calibration flow that goes with
it. Goal: one short step per lesson, with grasp accuracy that is better than
today's, not merely faster.

---

## 0. The answer on one page

**What the student does at the start of a lesson (about 60 s, no tools):**

1. Roll out the mat.
2. Put the robot station (the arm, factory-bolted to its base plate) on the
   grey outline.
3. Put the camera tower on the violet circle. Plug in one USB cable and the
   12 V supply.
4. Press „Umgebung starten", then „Roboter aktivieren", then open Roboter
   Studio. The station check runs by itself in about 2 s: „Station geprüft –
   Genauigkeit 1,4 mm". No board, no 20 photos, no table tapping.

**Why the station is precise even though students place it by eye:** nothing
precise depends on where the student puts things. The calibration reference is
**printed on the robot's own base plate** (12 AprilTags in two "wings" left
and right of the arm's foot, one of them doubling as the kit-number tag). The camera measures that plate in every
lesson, so the camera-to-robot transform is *measured*, never assumed. The mat
and the tower position only make sure the workspace is framed; ±1 cm there
costs nothing.

**The four lifetimes of calibration:**

| When | Who | What | Time |
|---|---|---|---|
| Once per **camera**, at kit assembly | vendor / teacher | Intrinsics: 40 views of a large board, stored in the kit record | 5 min, never again |
| Once per **arm**, after the students assembled it | student, automatic | „Arm einmessen": the arm shows a wrist tag to the camera in 12 poses. This finds swapped servos and wrong spline teeth, and corrects joint zeros. | ~25 s |
| Every **lesson** | automatic | „Station prüfen": one image of the base plate gives the camera→robot transform | ~2 s |
| **Continuously** | automatic | Plate watchdog: a bumped tower or robot is detected and re-measured | — |

**Accuracy (Monte-Carlo, 60 classroom setups × 199 cube positions, p95 of the
claw-tip miss distance):**

| | Grasp XY p95 | Height p95 | Within grasp tolerance | Student calibration effort per lesson |
|---|---:|---:|---:|---|
| Today's process (with a better camera) | 11.8 mm | 5.2 mm | 68 % | ~10 min, 30+ clicks, hand-guided taps |
| The proposal taken literally | 23.6 mm | 23.2 mm | 27 % | ~1 min |
| **Recommended station** | **2.2 mm** | **3.4 mm** | **100 %** | **0 clicks, ~2 s** |

(Full table, including every intermediate variant, in section 5.)

**What changes in the proposal, and why:**

* **The ChArUco board moves onto the robot.** A board that a student lays on a
  printed mat mark is only as accurate as the student's placement of the board
  *and* of the robot. Both are by eye, so their two yaw errors add up, and a
  0.5° yaw error is 3 mm at full reach. A loose board can also be flipped or
  mirrored, which `calibration_manager._check_extrinsic_orientation` documents
  it **cannot** detect. Printed on the base plate with unique tag IDs, the
  reference cannot be misplaced, cannot be mirrored, and is always there for
  the watchdog.
* **A painted mark on the servo horn cannot set a zero.** The STS3215 output
  is a 25-tooth spline, so the horn can only sit in 14.4° steps, and a link
  bolted to the horn's holes can only sit where the holes are. A mark aligned
  by eye is good to about ±2°. That is ±25 mm at the claw (K1a). One spline
  tooth on joint 2 is 72 mm, which drives the claw into the table. The working
  version of the idea is an **index horn**. The servo is calibrated at the
  bench *after* its horn is fitted, using a fixture that pins the horn's own
  screw holes. One hole carries a dowel, so the link fits on in exactly one
  orientation. The screws, not the eye, then set the angle.
* **The arm has six servos, not five** (joints 1–5 plus the claw, ID 6), and
  IDs 2/3 are a different model (STS3250). The boot probe cannot detect a
  physical swap between two STS3215s. The ID travels with the servo, and so
  does its EEPROM window. Each servo therefore gets a keyed ID collar, and
  „Arm einmessen" checks the assembly by moving each joint.
* **The camera must change.** Today's scene camera runs at 640×480 with a
  ~102° lens. From a 38 cm / 45° tower, it resolves a 24 mm cube tag at
  ≥ 20 px over **0 %** of the Edu:1 workspace. A 1920×1080 camera with a
  ~70° M12 lens covers **98 %** (median tag 34 px).
* **The tower is fixed, not adjustable.** It is 38 cm to the lens centre with
  a 45° machined bracket, as specified. The lens sits 58 cm in front of the
  joint-1 axis and the post at 64 cm. The arm can physically stretch to
  52.8 cm, so the tower stays outside every pose the joint limits allow.

---

## 1. What the code does today, and what that costs

Read from `CalibrationWizard.jsx`, its four step components,
`physical_ai_server.py` (calibration callbacks, `_load_workflow_calibration`)
and `workflow/calibration_manager.py`.

* **Three mandatory steps, one optional:** intrinsic (`IntrinsicCalibStep`,
  20 hand-held views + start + solve = 22 clicks), extrinsic
  (`HandEyeCalibStep`, board on the mat at x = 0.18 m, 3 clicks), table
  touch-off (`TableTouchStep`, arm limp, ≥ 4 hand-guided taps with the claw
  CLOSED, 6 clicks), accuracy verify (`AccuracyVerifyStep`, optional).
* **Every lesson repeats them.** `EDUBOTICS_FORCE_RECALIBRATION` defaults to 1.
  On the server only a successful touch-off clears `_recalibrated_this_boot`,
  but the React flags start false each session. In practice the wizard walks
  students through all three steps again: about 10 minutes of a 45-minute
  lesson.

Seven findings bear on the design. Each was checked against the code or
re-measured.

**F1 — today's scene camera cannot serve the Edu:1 workspace.**
`gui/app/constants.py::CAMERA_WIDTH/HEIGHT` default 640×480, and the
Innomaker U20CAM-720P lens is ~102°. With the repo's own camera model
(`generate_edu1_mat.placement_zone`, `TAG_PX_MIN` 20 px) and a 38 cm / 45°
tower, the share of the reach ring where a 24 mm cube tag is detectable is:

| Camera | lens x = 0.55 m | 0.58 m | 0.60 m |
|---|---:|---:|---:|
| 640×480, 102° (today) | 0 % | 0 % | 0 % |
| 1280×720, 102° | 14 % | — | 6 % |
| 1280×720, 78° | 56 % | — | 41 % |
| 1920×1080, 102° | 54 % | — | 39 % |
| 1920×1080, 90° | 78 % | — | 65 % |
| 1920×1080, 78° | 97 % | 93 % | 89 % |
| **1920×1080, 70°** | **98 %** | **98 %** | **98 %** |

The missing ~2 % is mostly the cells the arm hides at its observe pose. A
narrower lens gives bigger tags (60°: smallest 24 px, median 41 px) but no
framing margin. With the reach fan at tag height *and* the base-plate tags
projected, over 400 tower placements by eye (σ 1 cm, 1°), the worst-case
distance to the image border is: 70° +42 px, 65° −43 px, 60° −158 px. **70° is
the narrowest lens that never cuts the fan or the plate.**

**F2 — a board on foam board biases every grasp, and no setting can fix it.**
`_tag_table_xy` intersects the tag ray at `board_table_z + object_height_m`,
and `_solve_scene_extrinsic` writes `board_table_z = BOARD_TABLE_Z_M`. One
number therefore means both "where the board's printed face lay" and "where
the objects stand". `tools/classroom_kit_README.md` tells teachers to glue the
board onto foam board. Re-measured through the repo's PnP + projection:

| Printed face above the table | Cube-top XY error (5 test points) |
|---|---|
| 3 mm | 2.1 – 5.2 mm |
| 5 mm | 3.6 – 8.7 mm |
| 10 mm | 7.1 – 17.4 mm |

Setting `EDUBOTICS_BOARD_TABLE_Z_M` to the foam thickness corrects the camera
pose but then raises the object plane by the same amount, so the error comes
back with the opposite sign. Only a reference whose face *is* the object
surface, or a code change that separates the two heights, removes it.

**F3 — 20 student views give a usable centre and a dangerous rim.** A
synthetic replay of the intrinsic step (`calibrate_intrinsics`: 20 views of the
7×5 / 30 mm board, clustered where students hold it, 0.2 px noise, the repo's
`CALIB_RATIONAL_MODEL`) gives a focal error of median 0.14 % / p95 0.45 %. The
8-term rational model, however, extrapolates freely where no view reached. The
ray error at the image corners has p95 = **142 px**. The far corners of the
Edu:1 fan are exactly there. A kit-assembly calibration (40 views of a
12×9 / 40 mm board covering the corners) gives 0.04 % / 3.9 px.

**F4 — the extrinsic gate cannot catch a rotated or mirrored board,** and says
so (`SCENE_EXTRINSIC_REGION_*` comment block). It relies on the optional
ground-truth step to find a 90°/180°/mirror placement.

**F5 — the touch-off is the noisiest number in the chain.** The code's own
measurement (`TABLE_TOUCH_POINTS_REQUIRED` comment) gives a plane-error SD of
5.1 mm, 12 cm from the tap centroid, with 4 taps at 3 mm tap noise. On the
Edu:1 it must also be taught with the claw closed (E8), or the table reads
17 mm low.

**F6 — joints 2 and 3 put the encoder wrap edge inside their range.** Both are
`(0, π)` around `CENTER_TICK` 2048, so `feetech_bus.position_limit_window`
gives [2048, 4095]. The upper limit sits on the 4095/0 edge that the edu6 edge
guards exist for. Grasps use q2 ≤ 2.31 and q3 ≤ 2.56 rad (measured over the
workspace), so nothing breaks today. A limp arm collapsing forward, however,
lands next to the edge, and the torque-on edge refusal then fires.

**F7 — a 1° joint-zero error is 1.4–5.9 mm at the claw** (measured through
`edu1_ik` at r = 0.30 m, grasp height): j1 5.2 mm sideways, j2 1.4 mm in XY +
5.2 mm in Z, j3 3.6 mm + 3.8 mm, j4 2.6 mm, j5 0. Joint-zero accuracy is
therefore the single most important property of a student-assembled arm.

---

## 2. The proposal, evaluated

The owner's idea: tower 38 cm / 45°, a mat with marked places for robot, tower and
board, pre-calibrated and ID-marked servos, horn marks at the URDF zero, one
board on the mat, one re-check, start.

**What is right and stays:** a fixed tower geometry; one reference target
captured once; a single automatic check instead of a wizard; servos
provisioned before the students touch them; a mat that tells everyone where
things go.

**What breaks, measured (section 5):**

* **K1a, literal** (kit intrinsics, board and robot on printed marks, horn
  mark aligned by eye ≈ 2°): p95 23.6 mm XY, 23.2 mm Z, 27 % of grasps within
  tolerance. The horn mark is the cause: Z alone is ±23 mm, and the cube
  tolerance is ±12 mm.
* **K1b, the same with an index horn** (0.4°): p95 6.7 mm, 98 % within
  tolerance. The remaining error is the student placing robot and board by
  eye.
* A one-tooth assembly error (14.4°) is not an accuracy problem but a
  collision (72 mm into the table at r = 0.30 m). The flow must therefore
  *detect* it before the first grasp, not tolerate it.

So the idea needs three changes to become reliable: index horns instead of
marks, the reference on the robot instead of on the mat, and an automatic
assembly check.

---

## 3. The recommended station

```
 PLAN VIEW (base frame: x forward, y left; mat 900 x 760 mm)

        y=+0.38 ┌────────────────────────────────────────────────────────┐
                │                                                        │
                │      reach fan r 0.07…0.35 m (±90°), green = grasp zone │
                │ ┌───────┐  left wing: 6 tags (x < 0)                 │
                │ │ ▣  ▣  │                                   ╭────╮      │
                │ │ ▣  ▣  ├─┐                         ●───────┤post│      │
     joint-1 ── │ │      ARM │→ vorne       lens x=0.58         │0.64│      │
     axis (0,0) │ │ ▣  ▣  ├─┘               z=0.38            ╰────╯      │
                │ │ ▣  ▣  │  Sockelplatte 350 x 210 mm        tower foot   │
                │ └───────┘  right wing: 6 tags (x < 0)       Ø 140 mm     │
                │                                                        │
        y=-0.38 └────────────────────────────────────────────────────────┘
               x=-0.14                                              x=0.76

 SIDE VIEW (x-z plane through y = 0)

   z=0.52 ┊ arm at HOME (claw up)
          ┊   ║
          ┊   ║                                   camera (1080p, 70° M12)
   z=0.38 ┊   ║                         ┌──○╲ 45° fixed bracket
          ┊   ║                         │    ╲ optical axis
          ┊   ║                         │      ╲ ──→ hits table at x≈0.20
          ┊ ┌─╨─┐                       │post (2020 extrusion)
   z=0    ┴═╧═══╧═══════════════════════╧═════ mat ═══════════
           Sockelplatte                ▔▔▔ weighted foot
          x=0                  x=0.528 = farthest any link can reach
```

### 3.1 Sockelplatte (base plate): the reference that cannot be misplaced

* **What:** a rigid plate under the arm. Material: 3 mm aluminium, or 4 mm
  aluminium composite (Dibond), UV-printed directly. A printed sticker
  shrinks and lifts; direct print does not. Size 350 mm (y) × 210 mm (x),
  from x = −0.125 m behind the joint-1 axis to x = +0.085 m, just in front of
  the base. Rubber anti-slip feet underneath.
* **Mounting:** the arm base is bolted to the plate at the factory, with two
  Ø 4 mm dowel pins (H7/m6) plus four screws. Students never separate them.
  This one interface carries the whole calibration, so it is the one that gets
  dowels.
* **Printed reference:** 12 tag36h11 markers, 30 mm black square, in two
  wings of 3 × 2. Centres at x = −0.095 / −0.055 / −0.015 m and
  y = ±0.115 / ±0.150 m: left and right of the base and **behind the joint-1
  axis**, so outside the ±90° reach fan.
  * IDs **100–110**. The twelfth slot (outer rear, right wing) carries
    **ID 200 + kit number**: it identifies the kit (section 4.1) and is a
    geometry point like the others, because its slot, not its ID, fixes its
    position.
  * Measured against the box model with the arm at HOME: all 12 are visible
    from the tower, and the wings cover **0.5 %** of the grasp zone.
    Measured the other way round: a strip of tags across the front of the
    base covered 9.5 % at equal accuracy (2.3 vs 2.1 mm p95). Tags behind the
    base itself are hidden by the 48 mm base body.
  * IDs 100–200+ are outside the object catalog (shipped 20/21,
    student-defined objects 30/31). The object paths filter by
    `recipe.tag_ids`; S7 lists the one path that does not.
* **Heights are kit constants, not measurements:** the plate's top face is
  z = 0 (the base bottom = URDF base_link z = 0). The plate sits on the mat
  and the objects stand on the mat, so the object surface is z = −t_plate
  (−3.0 mm), known by construction. This needs the height split from F2
  (section 7, S3). Otherwise flush-mount the plate in a mat cutout.
* **Anti-tip:** the Edu:1 at full reach puts an estimated ~0.07 kg·m moment
  on a 120 mm base (typical STS servo and printed-link masses; weigh the real
  arm). The plate moves the tipping edge out to the plate edge, and a
  laminated 1.5 mm steel sheet (~0.8 kg) under the print holds the arm down
  without clamps.
* **Why not tags on the base housing:** a target spread over 330 mm in the
  table plane constrains the camera yaw and tilt far better than a few tags on
  the 120 mm base housing, and it costs the grasp zone nothing.

### 3.2 Camera and tower

* **Camera (purchasing spec):** UVC, 1920×1080 MJPEG at ≥ 25 fps, fixed-focus
  M12 lens with **~70° horizontal FOV** (about 4 mm on a 1/2.9" sensor).
  Global or rolling shutter both fine, since the scene is static while the
  camera looks. Check:
  * its *default* MSMF mode is 1080p (`win_camera.open_capture` deliberately
    does not renegotiate);
  * it exposes a stable USB serial (useful, not required: section 4.1);
  * the lens is glued or locked, because a focus ring that turns changes the
    intrinsics.
* **Tower:** one 20×20 aluminium extrusion post at x = 0.64 m on a weighted
  Ø 140 mm foot (steel disc, rubber pad). A machined or printed head
  cantilevers the camera back to **lens x = 0.58 m, z = 0.38 m, tilt 45°**:
  * The tilt is **fixed**, by a bracket with two dowels and no hinge. A
    tilt-adjustable joint is the thing that drifts.
  * The optical axis hits the table at x ≈ 0.20 m, inside the extrinsic aim
    window `SCENE_EXTRINSIC_REGION_X_MIN/MAX` [0.08, 0.46].
* **Clearance:** within its joint limits and above the table the arm reaches
  0.528 m plan radius at most (`link_points` over a 25×25×21 joint grid,
  q2 = q3 = 2.88). The foot starts at 0.57 m. The lens is 0.648 m from the
  shoulder, so the arm cannot reach it either.
* **Coverage** at this geometry with the spec camera: 98 % of the reach fan
  usable for cube tags, smallest tag 20 px (the far fan corners beside the
  base), median 34 px. `choose_tilt` would pick 47.3°; 45° costs nothing
  measurable (98.3 % vs 98.3 %), so the specified 45° stays.
* **Why 38 cm and not taller:** at 45 cm the margins improve by 1–3 px at the
  far corners and nothing else. 38 cm keeps the tower short enough to store
  upright in the kit box. Note that the arm at HOME (0.52 m) is taller than
  the lens, so the observe pose cannot be "arm out of the picture". The
  coverage figure above already removes the cells it hides.

### 3.3 Mat

* **What:** 900 × 760 mm, ~2 mm, *firm* rubber-backed print (a desk-mat type,
  not soft neoprene foam that lets the plate rock), rolls up.
* **Shows:** the grey Sockelplatte outline with an arrow „vorne"; the violet
  tower circle; the green grasp zone *computed from the camera model*
  (`generate_edu1_mat.placement_zone`, re-run with this tower and camera); the
  red inner keep-out; a 100 mm print-check ruler.
* **Needs no precision.** Its scale, print shrinkage and how straight a
  student puts it down enter nowhere in the accuracy, because the plate and
  camera are measured. ±1 cm is fine. This is what lets it be a cheap,
  rollable textile print instead of a rigid board.

### 3.4 The arm: pre-provisioned servos that students cannot assemble wrong

**Bench station (vendor or teacher, ~1 min per servo, before the kit ships):**

1. Press the horn onto the spline (any tooth) and fix the centre screw with
   thread-locker.
2. Put the servo in the bench fixture: a pocket for the case and a plate with
   **two dowels that drop into two of the horn's own screw holes** at the
   reference angle. The operator turns the horn by hand (torque off) until the
   dowels drop in.
3. The station software is a new single-servo mode of
   `tools/edu6_provision.py`. It reuses every verified step that already
   exists:
   * the `Response_Status_Level` repair;
   * explicit `Lock = 0`;
   * the `Phase` bit-4 clear and `Angular_Resolution` normalisation BEFORE the
     offset;
   * `Homing_Offset` so the pinned horn reads the joint's designed zero tick,
     re-read gated at ±4 ticks;
   * the joint's designed `Min/Max_Position_Limit` window (same
     `position_limit_window`);
   * the safety EEPROM;
   * `Operating_Mode = 0`, re-lock, read-back.

   New in this mode:
   * it **writes the ID** (`REG_ID`);
   * it **refuses the wrong model for the joint**: IDs 2/3 must read model
     2825 (STS3250), the others 777 (STS3215);
   * it appends the servo to the arm's record.
4. Press a dowel pin into the horn hole that sat at the reference („Index-
   Loch"). Clip a **keyed ID collar** onto the case: colour + number, and a tab
   whose position differs per ID. The matching link pocket has the matching
   slot, so servo 1 physically does not fit in joint 5's pocket.

**Why calibrate after the horn is on:** the 25T spline quantises the horn to
14.4°. Calibrating the bare shaft and pressing the horn afterwards leaves a
random 0–14.4° error that no mark can show. Calibrating against the horn's
holes makes the holes the reference. The link is bolted to those holes, so it
inherits the zero, with clearance error only (σ ≈ 0.3°, against ±2° by eye).

**What students do:** assemble the links. Each servo fits only its own
pocket, and each link fits its horn only with the index dowel in its socket.
They never align anything by eye, and they never need to turn a servo to a
mark. The keyed horn makes the zero independent of where the shaft happened to
be during assembly.

**Optional, recommended while every servo is being provisioned anyway:**
centre each joint's range in the encoder (F6). Joints 2 and 3 get designed
zero tick 1024 instead of 2048, so their window becomes [1024, 3072]. The wrap
edge then lies 90° beyond both mechanical stops, and the edge refusal can
never fire on a collapsed arm. This is a per-joint zero tick in
`position_limit_window`, `designed_zero_tick` and the driver's tick↔rad map,
which is a provisioning change (section 8).

### 3.5 Wrist tag

One 40 mm tag36h11, **ID 120**, on a small fin clipped to the claw-servo
housing (link 5), 40 mm off the tool axis, facing up-and-out at 45°. Two pins
make it repeatable to ±0.1 mm. In every calibration pose, joint 5 turns it
toward the camera. At HOME it is above the lens height and out of view, so it
does not disturb anything. It sits ~100 mm above the fingertip at grasp
height, clear of 30 mm cubes. If it stays permanently mounted, it must be
added to `EDU1_LINK_BOXES[5]` in both copies (cross-pinned by
`tests/test_edu6_geometry.py`).

---

## 4. The calibration flow

### 4.1 Once per camera: kit intrinsics (factory)

* 40 views of a large (12×9, 40 mm) ChArUco board, or a monitor pattern,
  covering the image corners, `CALIB_RATIONAL_MODEL`. Reprojection target
  < 0.3 px.
* Stored as the **kit record** (kit number, camera K + dist + resolution, arm
  serial, plate layout version, `t_plate`):
  * in Supabase, as a new service-role table beside `edu6_arm_records`;
    the next free migration is 042, since 037 is reserved;
  * cached in the `edubotics_calib` volume.

  The station finds its record by reading the plate's kit tag (ID 200 + n), so
  no typing, USB serial or network is needed once the cache holds it.
* **Wrong-camera protection:** the station check reprojects the plate
  with these intrinsics. A tower from another kit shows up as a residual far
  above the gate, and the check says, in German, that the camera does not
  belong to this kit. The existing 20-view intrinsic step stays as the
  fallback for that case.

### 4.2 Once per arm: „Arm einmessen" (~25 s, after assembly)

The student presses one button with the mat cleared. The tower and plate are
already in place.

1. **Plate image:** gives the camera→base transform (4.3).
2. **Joint check,** one joint at a time, 10°: the wrist tag must move the way
   that joint moves it. Joint 1 turns the tag about the vertical; joint 5
   turns it about the tool axis; and so on. A swapped pair, a reversed sign
   or a wrong tooth shows up here, and the check names it:
   „Servo 1 und Servo 5 sind vertauscht – bitte tauschen."
3. **12 poses** (3 radii × 4 bearings × 2 heights, wrist vertical and pitched
   ±20°, every link ≥ 40 mm above the table, `calibration_poses()`). Each pose
   is a floor-checked glide from the existing motion stack, and records the
   wrist-tag corners and `Present_Position`.
4. **MAP fit** of the five joint-zero corrections:
   * the camera is fixed by the plate, so joint 1 is observable too;
   * prior σ = 0.4° (the index-horn assembly class);
   * accepted only with a reprojection RMS < 1 px and every correction
     < 3°. Larger means "mis-assembled", not "calibrate harder".
5. **Persist on the arm:** refine each servo's `Homing_Offset` by the
   correction, through the same verified read-compare-write path as the bench
   tool, and update the arm record. The arm then carries its own calibration
   to any PC and any camera. The `Min/Max` windows are unchanged in the
   corrected frame, so the boot fingerprint still holds. Nothing in this
   step moves without the student's press.

### 4.3 Every lesson: „Station prüfen" (~2 s, automatic)

* Runs when the student opens Roboter Studio, after activation. The arm is at
  HOME and nothing moves.
* Detects the plate tags (8-frame burst, like `EXTRINSIC_BURST_FRAMES`) and
  runs SQPNP + LM over all visible corners against the known plate layout.
* Gates:
  * at least 6 of the 12 tags, with both wings represented;
  * reprojection RMS ≤ 0.8 px;
  * plausible against the tower geometry: lens 0.38 ± 0.03 m high, tilt
    45 ± 4°.
* Writes `scene_handeye.yaml`:
  * `transform`;
  * `method: KIT_PLATE`;
  * `board_table_z = 0`;
  * the new object-surface key = −t_plate;
  * `z_table` = object surface from the kit geometry.
* Clears the boot latch (section 7, S4).
* **Unambiguous by construction:** every tag ID has one known position, so a
  rotated, mirrored or swapped reference cannot be solved into a wrong pose.
  F4 disappears.
* German result line: „Station geprüft – Kamera 38,2 cm hoch, Genauigkeit
  1,4 mm." The number shown is the predicted grasp-error p95 from the residual.

### 4.4 Continuously: plate watchdog

* While no program runs, re-detect the plate every 2 s from the frames already
  flowing.
* If the solved camera pose moves more than 1.5 mm at the workspace centre,
  or 0.3°:
  * idle: re-solve silently;
  * during a run: show the banner „Kamera oder Roboter wurde verschoben –
    bitte Programm stoppen" and refuse the next perception call until the
    re-solve.

  A bumped tower is then never a silent error.

### 4.5 What the student no longer does

* No intrinsic photos.
* No board.
* No table tapping, so E8's closed-claw instruction and F5's 5 mm plane noise
  go away.
* No „Genauigkeit prüfen".

The touch-off stays available as a teacher tool („Prüf-Tipp": one tap that
must agree with the kit height within 3 mm).

---

## 5. Error budget

`tools/edu1_kit_calibration_budget.py --trials 60 --pool 60 --seed 11`
(60 classroom setups per concept × 199 cube positions spread over the grasp
zone, 1920×1080 / 70° camera, tower 38 cm / 45° / x 0.58).

* **Tolerance:** the claw tip within 8 mm of the cube centre in XY. The cube is
  30 mm and the blade 48.5 mm wide, so ±9 mm with 1 mm margin. Height within
  ±12 mm of the grasp height: below that the tips hit the table, and above
  +16 mm the claw closes past the cube (KNOWN-ISSUES E5 cliff).
* **The physical arm** is the CAD chain plus 0.3 mm per-axis link tolerances,
  0.3° gravity sag at reach and settle noise. Calibration never sees these
  directly.

| Key | Concept | XY p50 | XY p95 | XY p99 | Z p95 | In tolerance |
|---|---|---:|---:|---:|---:|---:|
| K0 | Today (with the new camera): student intrinsics, board on 5 mm foam at mat mark, robot on mat outline, 4 taps | 6.6 | 11.8 | 13.9 | 5.2 | 67.8 % |
| K1a | The proposal literally: kit intrinsics, board + robot on mat marks, horn mark by eye | 10.6 | 23.6 | 28.5 | 23.2 | 26.6 % |
| K1b | K1a with index horns | 3.1 | 6.7 | 8.7 | 5.5 | 98.1 % |
| K2 | Board docked to the robot base with 2 dowels, index horns | 2.6 | 5.5 | 7.8 | 6.3 | 99.2 % |
| K2j | K2 with the whole arm provisioned on a factory jig | 1.4 | 2.9 | 3.6 | 4.1 | 100 % |
| K3 | No board: wrist tag in 12 poses, camera pose + joint zeros estimated | 1.3 | 3.2 | 4.5 | 3.4 | 100 % |
| K4 | Docked board (camera) + wrist tag (joint zeros) | 1.1 | 2.3 | 3.0 | 3.7 | 100 % |
| K4m | K4, assembled by eye (2°) | 1.1 | 2.4 | 3.1 | 3.5 | 100 % |
| K5p | Sockelplatte only, index horns, no arm motion ever | 2.5 | 4.8 | 6.6 | 5.9 | 100 % |
| K5j | Sockelplatte, factory-jig arm, no arm motion ever | 1.5 | 3.1 | 4.1 | 4.3 | 100 % |
| **K5** | **Sockelplatte + one-time „Arm einmessen" (recommended)** | **1.0** | **2.2** | **2.8** | **3.4** | **100 %** |
| K5m | K5, assembled by eye (2°) | 1.1 | 2.3 | 2.8 | 3.5 | 100 % |

**Sensitivity** (scratch runs, 30–40 setups, coarser test grid; the plate rows
marked * used an earlier front-strip tag layout, which the wing layout matched
or beat in a direct comparison: K5 2.1 vs 2.3 mm, K5p 4.7 vs 5.4 mm, K5 with
doubled sigmas 3.8 vs 3.9 mm):

| Variation | XY p95 | Z p95 | In tolerance |
|---|---:|---:|---:|
| K4/K5-type fit with 8, 12, 16, 24, 29 poses | 2.3 / 2.4 / 2.2 / 2.2 / 2.4 | 3.3–3.6 | 100 % |
| K3 (no plate) with 8 … 29 poses | 3.0 – 4.3 | 3.6 – 5.2 | 100 % |
| K5 (wings), every arm/vision/plate sigma doubled (links 0.6 mm, sag 0.6°, tag noise 0.3 px, tag mount 0.6 mm/0.6°, settle 0.1°, plate print 0.1 mm/0.1 %) | 3.8 | 6.6 | 100 % |
| K5p (plate, no einmessen), sigmas doubled * | 5.4 | 7.9 | 99.8 % |
| K2 (docked board, no einmessen), sigmas doubled | 6.7 | 8.0 | 98.6 % |
| K3 (no plate), sigmas doubled | 6.3 | 9.3 | 97.1 % |
| K5 with student intrinsics instead of kit intrinsics * | 2.4 | 3.6 | 100 % |
| First K3 attempt: 10 vertical-only poses, no prior | 15.9 | 11.9 | 57 % |

**What the table says:**

1. **Placement by eye is the ceiling of any mat-referenced design**
   (K1b 6.7 mm). Fixing the reference to the robot brings it to 4.8–5.5 mm
   (K5p, K2).
2. **Joint-zero accuracy dominates once placement is fixed.** The same plate
   gives 4.8 mm with student-assembled index horns and 3.1 mm with a
   factory-jigged arm (K5p vs K5j). „Arm einmessen" brings the student-built
   arm to 2.2 mm, *better* than the factory jig. It also makes the arm
   insensitive to assembly quality (K5m 2.3 mm ≈ K5).
3. **The plate beats the arm-only variant (K3) under pessimistic
   assumptions** (3.8 vs 6.3 mm doubled). It also needs no arm motion per
   lesson and drives the watchdog. That is why it is worth one extra part.
4. **Pose design matters more than pose count.** Ten vertical-only poses
   without a prior failed (15.9 mm). Eight to twelve well-spread poses
   (two heights, pitched wrist, roll facing the camera) with a 0.4° prior are
   as good as 30. The 12-pose program is the recommendation.
5. Kit intrinsics matter less once the arm fit absorbs the residue, but they
   remove the 142 px corner tail of F3. They also delete the longest student
   step, which is the real reason to make them a factory step.

---

## 6. Time budget per lesson

| Step | Today | Station |
|---|---|---|
| Set up hardware | robot + camera on a clamp/tripod, aim by preview, board, foam | roll mat, place station + tower (~60 s) |
| Intrinsics | 20 photos (~5 min) | none (kit record) |
| Extrinsic | place board, 3 clicks (~1 min) | automatic plate check (~2 s) |
| Table height | limp arm, 4 taps, claw closed (~2–3 min) | none (kit geometry) |
| Accuracy check | optional, typing coordinates (~5 min) | automatic (residual shown) |
| **Total calibration** | **~8–10 min, every lesson** | **~2 s per lesson + 25 s once per assembled arm** |

---

## 7. Software changes, by layer

Most items are small and local. The large ones are S3 (two heights) and S6
(„Arm einmessen").

* **S1 — camera resolution per profile.** Scene capture at 1920×1080 for
  `edu1_studio` on Windows (`camera_bridge`, `constants.CAMERA_WIDTH/HEIGHT`,
  today one global pair) and on the Pi (`usb_cam` `image_width/height` in the
  overlay launch). `pupil_apriltags` already runs `quad_decimate=1.0`, which a
  20 px tag needs.
* **S2 — kit intrinsics.** A loader path for a *measured, camera-bound*
  intrinsic (`source: kit_measured`, kit number, resolution checked against
  the live stream, a field the loader ignores today), plus the kit-record
  table/cache. The `factory_default` skip in
  `_load_persisted_intrinsics` stays exactly as it is.
* **S3 — split the two heights (F2).** A new scene-YAML key
  `object_surface_z` beside `board_table_z`, used by `_tag_table_xy`,
  `tag_pose.tag_yaw_base`, `mark_destination_callback` and the verify
  projection. When absent it falls back to `board_table_z`, so an old YAML is
  byte-identical in behaviour.
* **S4 — station check step.** `CalibrationManager.solve_station()`:
  * tag detection on the scene frame;
  * a plate layout table (one source, shared with the mat/plate generator);
  * SQPNP + LM over all corners;
  * the gates in 4.3;
  * writes the YAML in the same truncating manner as
    `_solve_scene_extrinsic`.

  The node:
  * a `station` step on `/calibration/start|solve` (no new srv);
  * `_recalibrated_this_boot` set by a passing station check for a profile
    that declares `calibration: kit_station` in its manifest;
  * React: `CalibrationWizard` collapses to one card for that capability and
    keeps the old steps behind „Erweitert" for a station that fails (F3
    fallback).
* **S5 — watchdog.** A low-rate re-solve on the node's own timer group (never
  the MutuallyExclusive default; see the callback-group rule), a German
  `/task/status` notice and a perception refusal while stale.
* **S6 — „Arm einmessen".**
  * A pose program built from `calibration_poses()` and the existing
    floor-checked glide (`home_planner`/box model), on an explicit press only.
  * The MAP fit (the same `least_squares` formulation as the budget tool).
  * Driver-side: an EEPROM `Homing_Offset` refinement service that reuses the
    bench tool's read-compare-write + verify. Today only the bench tool writes
    EEPROM.
* **S7 — tag ID reservation.** IDs 100–110, 120, 200+ declared reserved next
  to the catalog. `_verify_add_point`'s "exactly one tag in view" filters by
  catalog IDs, or it would break on the always-visible plate.
* **S8 — generators.**
  * `generate_edu1_mat.py` with the station tower defaults (0.58 / 0.38 /
    45°, 70°, 1920×1080) and the plate outline;
  * a new `generate_edu1_plate.py` (print file + layout table, the
    single source);
  * the single-servo mode of `edu6_provision.py` (3.4).

---

## 8. Rules this touches: owner decisions required

These are deliberate rules in `CLAUDE.md`. The design argues for each change,
but none should be made without the owner's explicit go-ahead.

1. **"Factory-default seeding is deleted (never re-add)."** What it forbids
   is a *guessed* K shared by all cameras of a model. A ~2× focal error was
   the reason. S2 is a K *measured per physical camera*, bound to the kit and
   re-verified every lesson by the plate residual. Different in kind, but it
   is still a K not measured on this PC today. Decision: allow `kit_measured`.
2. **"There is no `z_table=0.0` fallback anywhere"** and the touch-off plane
   being the grasp descent height (Rule §2 land, per the
   `TABLE_TOUCH_POINTS_REQUIRED` comment). S4 derives `z_table` from the kit
   geometry, which is not a silent default. The plate is measured each lesson,
   and the robot-to-object-surface height is fixed by construction. But it
   replaces a measured plane with a constructed one. Decision: allow
   `kit_station` profiles to derive `z_table` from the kit, with the
   touch-off kept as a teacher check.
3. **EEPROM writes outside the bench tool** (S6). Today the classroom stack
   never writes servo EEPROM. Decision: allow a single verified
   `Homing_Offset` refinement on „Arm einmessen". The alternative is a per-arm
   software offset table in the driver: no EEPROM writes, but every joint
   consumer would then depend on it.
4. **Designed zero tick per joint** (3.4 optional). A provisioning change: all
   four declaration sites of the Edu:1 limits move together, and every arm is
   re-provisioned. That happens anyway with the new servo bench.
5. **Arm motion for calibration.** It is not a pipeline safety guard and adds
   none. It is a press-started glide program using the existing floor checks,
   like the home glide. It is listed here because it is new arm motion.

Not touched: the collision e-stop (off on Feetech profiles), the activation
gate (everything here runs after activation), the motion lock, the manual-mode
arbiter, `LINK_RADIUS_M + ZONE_MARGIN_M`.

---

## 9. Bench gates before anything ships (K-R1 …)

* **K-R1 Camera:**
  * the 1080p/70° module's default MSMF mode is 1080p MJPEG at ≥ 25 fps
    on a school PC;
  * a 24 mm tag is detected at the far fan corner (x ≈ 0, y ≈ ±0.30 m)
    over 100 frames;
  * the measured HFOV is fed to `generate_edu1_mat.py --hfov`.
* **K-R2 Plate:**
  * the print position error over the 12 tags is ≤ 0.1 mm (measure with a
    calibrated scanner or a flatbed);
  * flatness ≤ 0.3 mm;
  * `t_plate` is measured.
* **K-R3 Station check repeatability:** remove and replace the tower 20 times.
  The solved transform must reproduce the *measured* ground truth within
  1 mm; the camera-pose scatter does not have to be small.
* **K-R4 Bench fixture:** a horn pinned → removed → re-pinned 10 times reads
  the same tick ±1. One servo provisioned, horn link bolted 10 times: angle
  scatter σ ≤ 0.3° (the `zero_keyed_deg` assumption is 0.4°).
* **K-R5 Arm einmessen:**
  * on a deliberately mis-assembled arm (one tooth off on j2; j1/j5
    swapped), the check names the fault;
  * on a good arm, the corrections reproduce within 0.1° over 5 runs;
  * the EEPROM write survives a power cycle.
* **K-R6 End to end:** 50 cube grasps across the zone, after a fresh setup by a
  student who has never seen the kit. Expect ≥ 98 % first-try success
  (the budget predicts 100 % within tolerance). Record the actual miss
  distances against the wrist tag; they are the numbers that replace this
  table.
* Plus the open Edu:1 gates in `docs/KNOWN-ISSUES.md` (E1–E11), in particular
  E1 bus order, E3 signs and E9 joint 4's ±115°. The servo bench makes E1/E3
  *design* decisions, so they get settled once, on paper, with the fixture
  drawings.

---

## 10. Non-goals (this round)

* **Stereo, depth cameras or a camera on the gripper.** Trigger: a catalog
  object without a flat top for a tag.
* **Learning the gravity sag.** It is in the budget as an unmodelled 0.3° and
  costs ~1 mm. Trigger: K-R6 misses that correlate with reach.
* **A permanently rigid tower-to-plate link** (calibrate once, never per
  lesson). The 2 s check makes it unnecessary and the plate watchdog covers
  bumps. Trigger: classrooms where the station is never moved.
* **The OMX and edu6 stations.** The same plate idea applies to both. The edu6
  joint ranges and reach differ, so they need their own geometry.

---

## Appendix A — assumptions (the tool's `SIGMA`)

| Source | Value | Basis |
|---|---|---|
| Robot on printed outline (by eye) | 1.0 mm, 0.5° | 150 mm edge aligned to a line, ±1 mm per end |
| Board on printed mark (by eye) | 1.0 mm, 0.4° | 210 mm edge |
| Mat print scale | 0.2 % | large-format textile print |
| Board docked to the base (K2/K4) | 0.15 mm, 0.05° | H7 holes, 100 mm dowel spacing |
| Robot on the Sockelplatte (factory dowels) | 0.1 mm, 0.03° | fitted once, never separated |
| Sockelplatte print | 0.05 mm per tag, 0.05 % scale | UV flatbed on aluminium (gate K-R2) |
| Joint zero, factory jig | 0.15° | jig tolerance |
| Joint zero, index horn + student assembly | 0.4° | fixture 0.15° ⊕ horn holes 0.3° ⊕ case pocket 0.25° ⊕ part 0.1° (gate K-R4) |
| Joint zero, mark by eye | 2.0° | 1 mm at the horn's ~25 mm radius |
| Link geometry vs CAD | 0.3 mm per axis per joint | printed parts |
| Gravity sag at reach (execution only) | 0.3° on j2 (−0.6×/0.3× on j3/j4) | servo steady-state error under load, unmeasured |
| Present_Position read / settle | 0.03° / 0.05° | 1 tick = 0.088° |
| ChArUco / plate corner | 0.15 px / √8 | 8-frame burst |
| Wrist-tag corner | 0.15 px | 40 mm tag, 3-frame average |
| Cube tag centre | 0.125 px | 4 corners at 0.25 px |
| Wrist-tag mount | 0.3 mm, 0.3° | clip with 2 pins |
| Cube height, table flatness | 0.3 mm, 0.5 mm | — |
| Touch-off tap | 1.5 mm | contact compliance by hand |
| Camera unit-to-unit | focal 3 %, principal point 25 px | why a guessed default fails |
| Student intrinsics | 20 views, 7×5/30 mm, 0.2 px, centre-clustered | today's step |
| Kit intrinsics | 40 views, 12×9/40 mm, 0.1 px, corners covered | proposed factory step |

Reproduce: `pip install numpy scipy opencv-contrib-python pillow`, then
`python3 tools/edu1_kit_calibration_budget.py`. It prints the section 5 table.
`--only K5,K5p` re-runs a subset. Edit `SIGMA` to test your own numbers.
