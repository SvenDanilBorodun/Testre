# edu1_studio („Edu:1") — Hardware Bring-Up Sheet

**Arm:** 6 × Feetech STS servos on a Waveshare CH343P adapter (assumed `1A86:55D3`).
Joints 2 & 3 are the high-load STS3250 (model 2825); the other four are STS3215
(model 777) — **mixed by design**, exactly like the edu6, and the probe accepts both.
**Status:** the software chain is complete, green in CI and hardware-UNTOUCHED.
**Sibling doc:** `docs/edu6-hardware-bringup.md` — the 6-axis arm's session log. Most of
its guard-stack reasoning (§3) applies verbatim here, because the two arms share one driver.

> This sheet is a CHECKLIST, not a log. Every number in it is derived from the CAD export
> (`5dof_assembly_urdf2`) or from the shipped meshes; nothing has been measured on a real
> arm. The gates are E1-E8 and they are also listed, with their consequences, in
> `docs/KNOWN-ISSUES.md`. Record results there or in a session log, not by editing the
> assumptions here into "measured" without evidence.

---

## 0. Before power

- Repo checked out, images built for the release under test (`open-manipulator` carries the
  driver; a stale image will run the OLD spec table and refuse the arm at the boot probe).
- 12 V supply present. **USB alone enumerates the port and does NOT power the servos** —
  that is the single most likely first failure, and the scan names it in German.
- The arm on the provisioning jig: every joint at its designed zero, **claw fully CLOSED**.

## 1. E1 — servo bus layout  *(do this first)*

Assumed: ids **1..6 in joint order**, the claw = **id 6**.

```bash
# Inside the scanner container (or any host with feetech_bus.py importable):
python3 - <<'PY'
import feetech_bus as fb
bus = fb.FeetechBus('/dev/ttyACM0')
for sid in range(1, 9):
    print(sid, bus.ping(sid),
          bus.read_u16(sid, fb.REG_MODEL_NUMBER) if bus.ping(sid) else '')
PY
```

Expect ids 1-6 to answer and **id 7 to stay silent** — that silence is what tells an Edu:1
from an edu6, and the prober asserts it. Then confirm the ORDER by hand: move joint 1 and
check that servo 1's `Present_Position` is what changes. **A wrong order is invisible to
every guard in the stack**; a wrong id set fails loudly at the boot probe.

## 2. E2 — the USB bridge

```bash
ls -l /dev/serial/by-id/          # Linux / the Pi
# Windows: Get-PnpDevice -PresentOnly | ? InstanceId -like '*VID_1A86*'
```

If the VID:PID is **not** `1A86:55D3`, add the real pair to `ARM_USB_IDS['edu1']` in BOTH
thin descriptors, and add the real by-id substring to `_FEETECH_BYID_MARKERS` in BOTH scan
twins. If it IS `1A86:55D3`, nothing to change: the two Feetech arms are then
USB-indistinguishable by design and the servo COUNT is the discriminator.

## 3. E4 — provision the EEPROM  *(nothing boots until this is done)*

```bash
python3 tools/edu6_provision.py --arm edu1 --port /dev/ttyACM0 --serial EDU1-0001 --dry-run
python3 tools/edu6_provision.py --arm edu1 --port /dev/ttyACM0 --serial EDU1-0001
```

The driver's boot probe VERIFIES the exact `Min/Max_Position_Limit` window this writes, so
an arm provisioned with the wrong `--arm` refuses to boot with „nicht provisioniert" and no
further hint.

Check the emitted record before you file it: `edu1_records/EDU1-0001.json` must carry
`"profile_id": "edu1_studio"`. That field is the ONLY machine-readable family tag in the
whole artifact — the local json and the shared `edu6_arm_records` Supabase row are the same
blob — so a record tagged `edu6_studio` is an Edu:1 you will not be able to tell apart from
a 6-axis arm later. (`--dry-run` prints the record without writing EEPROM, so this is
checkable before the first real run.) The identity gate also names the arm on the bench:
a foreign servo on an Edu:1 says „dieser Arm ist kein Edu:1", never „kein EduBotics 6-Achs". Unmeasured and inherited from the edu6: `ARM_MAX_TORQUE` 800 /
`GRIPPER_MAX_TORQUE` 150. Those are a PINCH-FORCE decision — settle them with E5.

## 4. E3 — joint direction signs

Default is all `+1` (which is what the edu6's own R6 later confirmed on real hardware).
Read-only check first: move each joint by hand toward its URDF-positive direction and
confirm the tick count RISES. A flip is one env var — the driver's joint-signs knob, six
comma-separated values — but **flipping a sign requires re-provisioning**, because the
EEPROM window is written sign-aware.

## 5. E6 — first boot and HOME

Start the environment and watch the arm container's log. Expected sequence: „Alle 6 Servos
gefunden", torque on, then a 3 s quintic glide to HOME `(0, 0.64, 1.48, 0.90, 0)` with the
claw open — the arm standing up over its own base, fingertip ≈0.52 m tall.

The glide is REFUSED (arm stays torqued where it is, German `[FEHLER]`) when the straight
joint-space line would drive a link under the table; that is ~1 in 1200 limp-collapse start
poses by measurement, so seeing it once is not a fault. Lift the arm a little and restart.

## 6. E5 — the grasp numbers

With calibration done (see E8 first), grasp the shipped 30 mm cube and read back the
achieved claw angle. Simulated expectation: the jaws BLOCK at ≈0.25 rad with the
end-effector origin 0.090-0.115 m above the table. The catalog commands **0.10** with a
**0.10** held-margin, so HELD reads 0.25 against a 0.20 threshold and a MISS reads 0.10.
If the real block angle differs, adjust `_FIXED_CATALOG_EDU1.gripper_close_rad` (and, if the
squeeze is uncomfortable, `grasp_held_margin_rad`) and rebuild the image.

## 7. E8 — touch-off, claw CLOSED

The TCP is the **closed** fingertip, 86.25 mm below the tool frame; at 0.9 rad open the tip
is only 68.8 mm below it. Teaching „Tisch vermessen" with the claw open measures the table
≈17 mm too low and every later grasp inherits the error. The wizard now shows a „Greifer
ganz schließen" step for this arm — confirm it appears, and confirm the 17 mm.

## 8. E7 — the jaw convention

`EDUBOTICS_GRASP_ROLL_DEG` defaults to 90° and has **no runtime guard** on any arm. It shows
itself at the first tag-aligned grasp: place a cube with its tag square-on and check the jaws
close across the axis you expect. Trim the degrees if not; the mapping is `q5 = wrap(−roll)`,
so ∂q5/∂GRASP_ROLL = −1.

## 9. End to end

Scan → „Umgebung starten" → calibration wizard (intrinsics, scene extrinsic, touch-off) →
Blockly „Greife Würfel" → „Bewegung" record + replay → cloud save. A saved recording is
tagged `edu1_studio` and is 7 floats per point — the SAME width an OMX recording uses, so
confirm that replaying an OMX recording here is refused with a German toast and not silently
attempted.
