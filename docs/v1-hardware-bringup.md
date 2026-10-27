# v1_studio (the V1) — Hardware Bring-Up Sheet

**Arm:** 6 × Feetech STS servos (12 V) on a Waveshare Bus Servo Adapter (A) (CH343, USB
`1A86:55D3`, `cdc_acm`). Joints 2 & 3 are the high-load STS3250 (Model 2825); joints 1, 4,
5 and the claw are STS3215 (Model 777) — **mixed by design**, and the boot probe accepts both.
**Status:** the software chain is complete and green in CI; the arm is hardware-UNTOUCHED, and the
servo spec (`robotis_ai_setup/docker/open_manipulator/feetech_servo_spec.py`) ships UNPINNED: until the
P0 pin commit, a released arm image refuses to boot a V1, the Servo-Station only reads references and
`tools/feetech_provision.py` writes nothing. **No V1 is provisioned before the pin.**

> This sheet is a CHECKLIST, not a log. Every number in it is derived from the CAD export
> (`5dof_assembly_urdf2 (1)` + `Base_part.2.step`) or from the shipped meshes; nothing has
> been measured on a real arm. The gates are **VR1–VR16** and the servo bench gates **P0–P8**,
> also listed, with their consequences, in `docs/KNOWN-ISSUES.md`. Record results there or in a session log,
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

## P0 — the servo reference  *(before any V1 is calibrated)*

At the Servo-Station (`docs/servo-station.md`), mode „Referenz lesen (P0)", with at least **three
fabrikneue servos per model** (STS3215 and STS3250). Per servo: „Referenz lesen" (a read-only dump of
addresses 0–86; a servo that is not vendor-fresh — ID 1, limits 0/4095 — is refused with
`P0_NOT_FRESH`), „Drehrichtung messen" (the servo IN the cradle, horn up, turned by hand along the
cradle's printed counter-clockwise arrow seen from the horn side), „Fahrzeit messen" (OUT of the
cradle; RAM only, the servo stays fresh). Then „P0-Bericht" writes `reference/p0_report.json` and
`reference/p0_pin_snippet.txt`: the factory values compared on the `FACTORY` keys only, the tick
direction per model and the motion window. Any disagreement refuses to propose a value
(`P0_DISAGREE`, `P0_DIRECTION_DISAGREE`, `P0_FALLBACK` after a single-register fallback read).

The snippet goes into the **dev-pin commit** on a branch from the release tag — `TICK_DIRECTION`,
`FACTORY`, `MOTION_TEST_WINDOW_MS`, `PROVISIONAL_NAMES` emptied, the two tripwires in
`test_feetech_servo_spec.py` updated; `MAX_TORQUE` and the protection current keep their values until
VR5 and the overcurrent check on the pilot. That branch is never merged, gets no PR and no tag; the
owner pushes it alone (no workflow fires on a non-`main` push) and runs it on the pilot arm below.
P0 also settles what nothing here could: the 40-byte block read at address 0, the broadcast-ping
answer window, the odd baud rates and Lock persistence across a power cycle.

## Pilot — unreleased arm software on the pilot arm  *(P1–P6, VR3 (= P3), VR5, overcurrent)*

The owner, in PowerShell on the Windows pilot PC; the released step-1 version installed and started
once, so its four images are present. **Nothing is pushed.**

```powershell
# 0. The branch from step 3, checked out with the repository's exact bytes, as CI builds it: Git for Windows'
#    default autocrlf would give every *.py (text=auto) CRLF, and the Dockerfile strips CR only from the scripts it
#    lists, not from the launch overlays. `clone -c` writes the setting into the new clone before its checkout.
#    A fresh window: a per-machine tag or registry override would make $rel / $reg below differ from what the GUI uses.
if ($env:EDUBOTICS_IMAGE_TAG -or $env:EDUBOTICS_REGISTRY) { throw 'use a PowerShell window without EDUBOTICS_IMAGE_TAG / EDUBOTICS_REGISTRY' }
git clone -c core.autocrlf=false --branch <pilot-branch> https://github.com/SvenDanilBorodun/Testre.git C:\EduBotics-Pilot
$sha = git -C C:\EduBotics-Pilot rev-parse --short=12 HEAD
$tag = "pilot-$sha"
$reg = 'ghcr.io/svendanilborodun'
$app = @("${env:ProgramFiles(x86)}\EduBotics", "$env:ProgramFiles\EduBotics") |
       Where-Object { Test-Path "$_\gui\EduBotics.exe" } | Select-Object -First 1
$rel = (Select-String -Path "$app\docker\versions.env" -Pattern '^\s*IMAGE_TAG=(.+)$').Matches[0].Groups[1].Value.Trim()

# 1. Wait until the distro's dockerd answers (the first wsl call boots the distro).
for ($i = 0; $i -lt 60; $i++) { wsl -d EduBotics -- docker info *> $null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 1 }

# 2. Build ONLY the arm image from the clone: the Dockerfile, base and context build-images.sh uses for amd64.
wsl -d EduBotics -- docker build --pull --no-cache --build-arg BASE_IMAGE=robotis/open-manipulator:amd64-4.1.4 -t "$reg/open-manipulator:$tag" -f /mnt/c/EduBotics-Pilot/robotis_ai_setup/docker/open_manipulator/Dockerfile /mnt/c/EduBotics-Pilot/robotis_ai_setup/docker/open_manipulator

# 3. Give the three released images the pilot tag (pulled only if one is missing), then check all four.
foreach ($n in 'physical-ai-server', 'physical-ai-manager', 'code-runner') {
  wsl -d EduBotics -- docker image inspect "$reg/${n}:$rel" *> $null
  if ($LASTEXITCODE -ne 0) { wsl -d EduBotics -- docker pull "$reg/${n}:$rel" }
  wsl -d EduBotics -- docker tag "$reg/${n}:$rel" "$reg/${n}:$tag"
}
foreach ($n in 'open-manipulator', 'physical-ai-server', 'physical-ai-manager', 'code-runner') {
  wsl -d EduBotics -- docker image inspect "$reg/${n}:$tag" *> $null
  if ($LASTEXITCODE -ne 0) { throw "missing pilot image: $n" }
}

# 4. Start the GUI on the pilot tag. Close EduBotics first: a second start only brings the running window forward
#    and exits (gui_app.py::_acquire_single_instance), ignoring these variables.
if (Get-Process EduBotics -ErrorAction SilentlyContinue) { throw 'close EduBotics first' }
$env:EDUBOTICS_IMAGE_TAG = $tag
$env:EDUBOTICS_SKIP_AUTO_PULL = '1'
& "$app\gui\EduBotics.exe"
```

Then „Umgebung starten": the Protokoll shows „Auto-Pull deaktiviert …" and `wsl -d EduBotics -- docker ps --format
"{{.Image}}"` lists four `…:pilot-<sha>` images. The station runs from the same clone: `py -3.12 -m pip install -r
C:\EduBotics-Pilot\tools\servo_station\requirements-station.txt`, then
`C:\EduBotics-Pilot\tools\servo_station\Servo-Station.cmd`.

- **Why process variables, never the `.env`**: the GUI reads both names only from its own environment
  (`constants.py::_resolve_setting` for the tag, `SKIP_AUTO_PULL` at import); the `.env` is compose's env-file, never
  loaded into the GUI, and its managed `IMAGE_TAG` line is re-written from the GUI's resolved value at every
  „Umgebung starten" (`config_generator.generate_env_file`). `$env:` values live only in that PowerShell window and
  the processes it starts. `$reg` is the GUI's default and `versions.env`'s `REGISTRY`
  (`ghcr.io/svendanilborodun`, written by `release-installer.yml`), which only `EDUBOTICS_REGISTRY` could change —
  hence the fresh-window check.
- **Why the skip switch**: without it both pull paths ask the registries for `…:pilot-<sha>`, a tag no registry has —
  the launch check (`check_for_updates`) reads the locally built image (no RepoDigest) as „lokal nicht vorhanden" and
  tries to pull it, the pre-start pull (`_compose_pull`) tries again — each failing after its manifest timeouts with
  error lines in the Protokoll. With it both return early and compose's default pull policy (`missing`) uses the local
  images.
- **Never**: `build-images.sh` (every amd64 run ends in `docker push ${REGISTRY:-nettername}/<image>:latest` — the
  students' Docker Hub fallback — with no switch to skip it), `docker push`, any workflow dispatch (a manual
  docker-publish run overwrites `:latest`), a PR or a tag from the pilot branch.
- **Back to the release**: close EduBotics and start it from its shortcut (a process without the two variables): the
  GUI resolves the release tag from `versions.env` again and re-writes the `.env` at the next „Umgebung starten". The
  pilot tags stay until removed (`docker image rm` of the four `…:pilot-<sha>`); a later update may prune them.

On the pilot, in this order: **P1** (gauge repeatability; the absolute output-axis position on both models; the
real case size, horn Ø, horn holes and claw seat; where the connectors leave) before the series gauge is printed,
**P2** (EEPROM persistence over three power cycles; step K's Torque_Limit reading `Max_Torque` after a real power-on
on both models), **P3 = VR3** (a hard gate: each joint moved toward URDF-positive), P4–P6, **VR5** (the claw/arm
`Max_Torque`) and the overcurrent check (the protection current against live `Present_Current`). Their results go
into the pin commit, reviewed onto `main`; a servo calibrated on the dev-pin branch whose image then differs is
refused at boot until it is calibrated again — a safe refusal.

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

## 3. VR4 — station-calibrated servos  *(nothing boots until this is done)*

Every servo is calibrated ONCE at the Servo-Station before assembly (`docs/servo-station.md`): in the cradle,
under the cap of its joint, the station writes the spec's golden EEPROM image, proves a real power-on (step K)
and stores the record; the operator paints one stroke through the cap's slot. The arm is then assembled at
the marks. The whole-arm fallback writes the IDENTICAL image, the arm jigged at q = 0 with the claw closed:

```bash
python3 tools/feetech_provision.py --arm v1 --port /dev/ttyACM0 --serial V1-0001 --dry-run
python3 tools/feetech_provision.py --arm v1 --port /dev/ttyACM0 --serial V1-0001
```

The windows, centred (the middle of each URDF range on tick 2048), identical for every tick-direction
combination, which the driver's boot probe VERIFIES with the rest of the EEPROM image:

| servo | joint | window (ticks) |
|---|---|---|
| 1 | joint1 (base yaw, ±90°) | 1024–3072 |
| 2 | joint2 (shoulder, 0…π) | 1024–3072 |
| 3 | joint3 (elbow, 0…π) | 1024–3072 |
| 4 | joint4 (wrist pitch, ±110°) | 796–3300 |
| 5 | joint5 (roll, ±90°) | 1024–3072 |
| 6 | claw (0 = closed … 1.5708 open) | 1536–2560 |

A servo with any other window refuses to boot with „Servo N (…): Positionsgrenzen [lo, hi] statt [lo, hi] —
dieser Servo ist nicht für diese Stelle des V1 eingestellt. Bitte die Lehrkraft fragen: der Servo muss bei
EduBotics neu eingestellt werden." Provisional until the pilot: `MAX_TORQUE` (arm 800, claw 150; a pinch-force
decision, settled with VR5) and the per-model protection current (settled by the overcurrent check).

## 4. VR3 — joint direction signs, fixed by P0

The signs are a spec constant, `OUTPUT_AXIS_VS_URDF × TICK_DIRECTION[model]`: the output-axis directions are
measured from the CAD, the tick direction per model comes from P0. There is no per-rig flip:
`EDUBOTICS_FEETECH_JOINT_SIGNS` is an assertion that refuses the boot when it differs from the spec. VR3 is
P3 on the dev-pinned pilot arm, a hard gate before the pin merges: move each joint by hand toward its
URDF-positive direction and confirm the joint state rises. A wrong P0 direction calibrates a model mirrored,
and no window can see it.

## 5. VR6 — first boot, activation and HOME

Start the environment and watch the arm container's log: „Alle 6 Servos gefunden", torque on,
nothing moves (the activation gate). Press „Roboter aktivieren" on the Startseite: a 3 s quintic
glide to HOME `(0, 0.64, 1.48, 0.90, 0)` with the claw open 0.90 — the arm standing up over its
own base.

- The glide is REFUSED (arm stays torqued where it is, German `[FEHLER]`) when the straight
  joint-space line would drive a link under the table; about 29 of 1200 sampled limp-collapse
  start poses by the box model, so seeing it once is not a fault. Lift the arm a little and
  press again.

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
and not silently attempted. VR12 (smoothness) is watched along the way.
