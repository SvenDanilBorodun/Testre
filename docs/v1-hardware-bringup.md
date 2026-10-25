# v1_studio (the V1) — Hardware Bring-Up Sheet

**Arm:** 6 × Feetech STS servos (12 V) on a Waveshare Bus Servo Adapter (A) (CH343, USB
`1A86:55D3`, `cdc_acm`). Joints 2 & 3 are the high-load STS3250 (Model 2825); joints 1, 4,
5 and the claw are STS3215 (Model 777) — **mixed by design**, and the boot probe accepts both.
**Status:** the software chain is complete and green in CI; the arm is hardware-UNTOUCHED.

> This sheet is a CHECKLIST, not a log. Every number in it is derived from the CAD export
> (`5dof_assembly_urdf2 (1)` + `Base_part.2.step`) or from the shipped meshes; nothing has
> been measured on a real arm. The gates are **VR1–VR16** and they are also listed, with
> their consequences, in `docs/KNOWN-ISSUES.md`. Record results there or in a session log,
> not by editing the assumptions here into "measured" without evidence.

---

## 0. Before power

- Repo checked out, images built for the release under test (`open-manipulator` carries the
  driver `feetech_arm_node.py`; a stale image runs the OLD driver and refuses the arm at the
  boot probe, and its prober answers with a token the host reads as „Das Roboter-Abbild ist
  älter als die Software …").
- 12 V supply present. **USB alone enumerates the port and does NOT power the servos** — that
  is the single most likely first failure, and the scan names it in German.
- The printed base is `Base_part.2` (VR9): its bottom is the table, joint 1 sits 50.2 mm above
  it. The shipped base frame is re-origined to that bottom; a different base puts every
  table-relative number off by the difference.

## 1. VR1 — servo bus layout  *(do this first)*

Expected: ids **1..6 in joint order**, the claw = **id 6**; Model 777 on 1/4/5/6, Model 2825
on 2/3.

```bash
# Inside the arm container (or any host with feetech_bus.py importable):
python3 - <<'PY'
import feetech_bus as fb
bus = fb.FeetechBus('/dev/ttyACM0')
for sid in range(1, 9):
    up = bus.ping(sid)
    print(sid, up, bus.read_u16(sid, fb.REG_MODEL_NUMBER) if up else '')
PY
```

Expect ids 1–6 to answer and **id 7 to stay silent**; the prober asserts exactly that
(`--servos=6`). A seventh answering device is `bus_too_long:7` („mindestens …"), fewer than
six is `partial:N` (connector / power). Then confirm the ORDER by hand: move joint 1 and check
that servo 1's `Present_Position` is what changes, and so on. **A wrong order is invisible to
every guard in the stack**; a wrong id set fails loudly at the boot probe.

## 2. VR2 — the USB adapter

```bash
ls -l /dev/serial/by-id/          # Linux / the Pi
# Windows: Get-PnpDevice -PresentOnly | ? InstanceId -like '*VID_1A86*'
```

If the VID:PID is **not** `1A86:55D3`, add the real pair to `ARM_USB_IDS['v1']` in BOTH thin
descriptors (`gui/app/constants.py`, `pi_agent/constants.py`). If the by-id string matches none
of `_FEETECH_BYID_MARKERS` (an admitted guess), add the real substring in BOTH scan twins.

## 3. VR4 — provision the EEPROM  *(nothing boots until this is done)*

On the jig: every joint at its URDF zero — the arm **folded flat**, joint 2 and joint 3 at the
ends of their windows — and the **claw fully CLOSED**.

```bash
python3 tools/feetech_provision.py --arm v1 --port /dev/ttyACM0 --serial V1-0001 --dry-run
python3 tools/feetech_provision.py --arm v1 --port /dev/ttyACM0 --serial V1-0001
```

The windows this writes (sign +1), which the driver's boot probe then VERIFIES as a
provisioning fingerprint:

| servo | joint | window (ticks) |
|---|---|---|
| 1 | joint1 (base yaw, ±90°) | 1024–3072 |
| 2 | joint2 (shoulder, 0…π) | 2048–4095 |
| 3 | joint3 (elbow, 0…π) | 2048–4095 |
| 4 | joint4 (wrist pitch, ±110°) | 796–3300 |
| 5 | joint5 (roll, ±90°) | 1024–3072 |
| 6 | claw (0 = closed … 1.5708 open) | 2048–3072 |

An arm provisioned with any other window refuses to boot with „nicht provisioniert" and no
further hint. Check the emitted record before you file it: `tools/v1_records/V1-0001.json` must
carry `"profile_id": "v1_studio"` — the only machine-readable family tag in the artifact (the
local json and the shared Supabase row are the same blob). Unmeasured and inherited: arm
`Max_Torque` 800, claw 150. Those are a PINCH-FORCE decision — settle them with VR5.

## 4. VR3 — joint direction signs

Default is all `+1`. Read-only check first: move each joint by hand toward its URDF-positive
direction and confirm the tick count RISES. A flip is one env var,
`EDUBOTICS_FEETECH_JOINT_SIGNS` (six comma-separated values) — but **flipping a sign requires
re-provisioning**, because the EEPROM window is written sign-aware.

## 5. VR6 — first boot, activation and HOME

Start the environment and watch the arm container's log: „Alle 6 Servos gefunden", torque on,
nothing moves (the activation gate). Press „Roboter aktivieren" on the Startseite: a 3 s quintic
glide to HOME `(0, 0.64, 1.48, 0.90, 0)` with the claw open 0.90 — the arm standing up over its
own base.

- The glide is REFUSED (arm stays torqued where it is, German `[FEHLER]`) when the straight
  joint-space line would drive a link under the table; about 29 of 1200 sampled limp-collapse
  start poses by the box model, so seeing it once is not a fault. Lift the arm a little and
  press again.
- **The edge refusal.** Joint 2 and joint 3 have windows that end AT tick 4095 (q = +π). A limp
  arm lying on that stop (tick ≥ 4056, i.e. q ≥ 3.0802 rad) is refused the torque-on:
  „[FEHLER] Gelenk N (Tick T) steht genau am Rand des Positionsbereichs (±180°). Das Drehmoment
  wird NICHT eingeschaltet … Bitte das Gelenk von Hand ein Stück Richtung Mitte zurückdrehen
  (etwa 10° genügen)." At boot the arm container adds `BOOT_PHYSICAL_REMEDY_DE['edge']`
  („Grundstellungs-Fahrt entfällt: ein Gelenk steht am Rand des Positionsbereichs …") and
  retries every 5 s. Provoke it once on purpose (lay the arm on a +π stop, start the
  environment), confirm both German lines, turn the joint back ~10° by hand and confirm the
  environment continues by itself.

## 6. VR11 — joint 4's ±110°

CAD puts link 4 4.2 mm from link 3 at +110°. Jog joint 4 to both extremes at the VR4 torque
floors and confirm neither collides; never widen past ±110°.

## 7. VR5 — the grasp numbers

With calibration done (VR8 first), grasp the shipped 30 mm cube and read back the achieved claw
angle. CAD expectation: the jaws BLOCK at 0.195 / 0.206 rad (gear ratio 1 / 18/19) at the
catalog's 15 mm grasp depth; over 10–20 mm a held reading stays ≥ 0.03 rad above the threshold.
The catalog commands **0.05** with a **0.10** held margin, so HELD reads ≈0.20 against a 0.15
threshold and an empty close reads ≈0.05. If the real block angle differs, adjust `_FIXED_CATALOG_V1.gripper_close_rad` (and, if
needed, `grasp_held_margin_rad`) and rebuild the image. Jaw friction is unmeasured; a rubber pad
on the blades removes the question.

## 8. VR8 — touch-off, claw CLOSED

The TCP is the **closed** fingertip, 108.6 mm below the tool frame; at the 0.90 open command the
tip is only 82.6 mm below it. Teaching „Tisch vermessen" with the claw open measures the table
≈26 mm too low and every later grasp inherits the error. The wizard shows a „Greifer ganz
schließen" step for this arm — confirm it appears, and confirm the 26 mm.

## 9. VR7 — the jaw convention

`EDUBOTICS_GRASP_ROLL_DEG` defaults to 90° and has **no runtime guard**. It shows itself at the
first tag-aligned grasp: place a cube with its tag square-on and check the jaws close across the
axis you expect. Trim the degrees if not; the mapping is `q5 = wrap(−roll)`, so
∂q5/∂GRASP_ROLL = −1.

## 10. VR16 — the claw gear ratio

The two blades are geared 18:19 in CAD (the URDF's `LF_joint` mimics `RL_joint` with −0.947368).
Open the claw to a known `RL_joint` angle and measure both blades' angles; the tip-rise table
and the box model assume this ratio.

## 11. VR10 — the mat, VR13/VR15 — the open claw

Print `tools/generate_v1_mat.py`'s mat at 100 % (check the 100 mm ruler), stand the arm on the
grey outline, and confirm the ChArUco origin lands at (0.18, 0.075). Then, with the claw open at
0.90: confirm the blades clear the base tower at the drawn 10 cm inner ring (CAD: ≈7 mm; contact
at 9 cm — nothing in the software refuses a target inside the ring), and set a no-go zone next to
a target to see how close the open claw passes it (CAD: the blades reach 22.9 mm, the housing
19.2 mm beyond the 50 mm inflation).

## 12. End to end

Scan → „Umgebung starten" → „Roboter aktivieren" → calibration wizard (intrinsics, scene
extrinsic, touch-off) → Blockly „Greife Würfel" → „Bewegung" record + replay → cloud save. A
saved recording is tagged `v1_studio` and is 7 floats per point — the SAME width an OMX
recording uses, so confirm that replaying an OMX recording here is refused with a German toast
and not silently attempted. VR12 (smoothness) and VR14 (the goal-edge clamp at full extension,
joint 3 131 ticks from the edge) are watched along the way.
