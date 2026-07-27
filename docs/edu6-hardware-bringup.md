# edu6_studio — Hardware Bring-Up Log & Handoff

**Arm:** EDU6-0001 — 7 × Feetech STS servos on a Waveshare CH343P adapter, **COM5**.
Joints 2 & 3 are STS3250 (model 2825); the other five are STS3215 (model 777) — **mixed by design**.
**Machine:** Windows 11 bench PC, repo at `C:\Users\svend\newaarm\Testre`.
**Last rewritten:** 2026-07-26 (session 7).
**Branch:** `main` — **session 7 is LANDED and pushed, CI green, images rebuilt and
BYTE-VERIFIED.** The desk work is finished; everything remaining is physical (§6). Commit list
in §5.2 — deliberately not repeated here, because a doc cannot name its own commit and a sha in
a header is stale the moment the header changes. `git log --oneline -8` is the source of truth.

> **This file is rewritten each session, never appended to.** Earlier revisions had
> accumulated layered self-corrections in which sections contradicted the ones above them —
> and by the end the section numbers were physically out of order. That is actively dangerous
> for a fresh reader. Superseded *reasoning* is deleted; the *decisions* it produced survive
> here with their reasons. Every number below is either measured on hardware, re-derived from
> code this session, or explicitly flagged as unverified.

---

## 0. STATUS AT A GLANCE

| Area | State |
|---|---|
| Sessions 1–5 code | ✅ pushed, CI green (`9cd43c23`) |
| Session 6 code (dead reroute ladder, roll landmine, place path) | ✅ pushed (`b8f3c71d`, `5427a3be`) |
| **Session 7 code** (guards 3-7, 8 sim defects, 6 vendor-research fixes) | ✅ **PUSHED — `6cce3da0`. CI + docker-publish both green.** Suites: **741** deps-free / **776**+4 server / **230** cloud / **30** jetson |
| **Docker `:latest` ×3** | ✅ **REBUILT from `5edb7979` and BYTE-VERIFIED** — `revision label MATCH`, `edu6_arm_node.py` `31dbc542…`, `feetech_bus.py` `241b1dc6…`. **The pre-powered gate is CLEARED** |
| **Guard stack** | ✅ **7 guards** — fingerprint, boot band 400, torque-on edge refusal 40, post-energise abort 2048, commanded-goal clamp 128, `Torque_Enable` write rail, fingerprint extras (§3). Seam-parking CLOSED |
| **⚠️ A12 — "a goal write can energise a limp arm"** | **OPEN, and it touches the hand-guide safety assumption.** Bench question, first thing at R5 — see §8.4 |
| Cloud (Supabase 035/036, dual-width Contract-B) | ✅ live |
| R1 USB enumeration | ✅ COM5, `1A86:55D3`, serial `5A68010132` |
| R2 register dump + provisioning (all 7) | ✅ done, re-verified live |
| R6 joint direction signs | ✅ **CLEARED — all default (+1). No re-provision.** (§2.5) |
| R9 torque-off collapse | ✅ **CLEARED — worst innocent excursion 107 ticks vs the 400-tick band. No retune.** (§2.4) |
| Encoder / magnet-seam question | ✅ **SETTLED — the firmware wraps; the seam is invisible. Re-clock DROPPED, reassembly NEVER needed on this arm.** (§2.1) |
| Q1 — can J4/J6 reach ±180° by hand? | ✅ **YES, measured** (§2.5) |
| Q1b — what the servo loop does at the ±180° edge | ✅ **MEASURED — it is NOT wrap-aware; it drives the LONG way.** (§2.3) |
| Powered zero-hold | ✅ held |
| **HOME driven + held under power** | ✅ **first powered LOADED pose on this arm** (§2.6) |
| R3 / R4 / R5 / R7 / R8 / R10 | ⏳ remaining (§6) |
| J6 sign + `GRASP_ROLL` jaw check | ⏳ remaining, one observation (§6) |
| Release v2.14.0 | ⏳ remaining (§6) |

---

## 1. THE ARM — hardware & environment reference

- **Serial port** `COM5` — `USB-Enhanced-SERIAL CH343`, VID:PID `1A86:55D3`, serial `5A68010132`.
  Currently "Not shared" in usbipd (Windows owns it for bench work). usbipd-attach to WSL only
  for the container path at R10.
- **Baud** 1,000,000 · **IDs** 1–7 · **4096 ticks/rev** · **firmware 3.10** on all seven.
- **12 V** supply. A silent bus logs the German 12-V-Netzteil hint.
- **GUI:** the installed `.exe` is v2.13.0 (pre-edu6) → **run from source** for edu6 testing:
  `cd robotis_ai_setup/gui && python main.py`. pywebview is broken on Py3.14 here, so it falls
  back to the system browser (fine).
- **Images:** `ghcr.io/svendanilborodun/{open-manipulator,physical-ai-server,physical-ai-manager}:latest`
- **Supabase** `fnnbysrjkfugsqzwcksd` · **Cloud API** `https://scintillating-empathy-production-1068.up.railway.app`
- **`gh` CLI auth is STALE** — re-auth needed for the pre-tag installer proof and CI status
  visibility (`gh auth login`). Not needed for bench work.

### 1.1 Live EEPROM state (read-only probe, matches `tools/edu6_records/EDU6-0001.json` exactly)

```
              model    mode phase lock offset  window        torque maxTq prot
J1 base       STS3215   0   0x00c  1    1736   [1024,3072]     0     800   310
J2 shoulder   STS3250   0   0x00c  1     988   [2048,4095]     0     800   310
J3 elbow      STS3250   0   0x00c  1   -1174   [   0,2048]     0     800   310
J4 f.roll     STS3215   0   0x00c  1    1814   [   0,4095]     0     800   310
J5 wrist      STS3215   0   0x00c  1   -2025   [1024,3300]     0     800   310
J6 w.roll     STS3215   0   0x00c  1    2027   [   0,4095]     0     800   310
gripper       STS3215   0   0x00c  1     679   [2048,3215]     0     150   310
```

`phase = 0x00c` → **bit 4 clear**, multi-turn off as provisioned (bits 2/3 are motor-drive
config, untouched). `Lock = 1` — EEPROM re-protected. `Operating_Mode 0` on all seven.
**This calibration is valid and is NOT superseded by anything in this document.**

HOME is `[0, 0.70, −2.40, 0, 0.70, 0]` rad + gripper 1.75.

---

## 2. MEASURED FACTS — the evidence base

Everything in §3 and §4 rests on these. They were measured on hardware, not reasoned.

### 2.1 The encoder / magnet-seam question — SETTLED, twice over

Each servo reads its shaft as 0–4095 ticks. One physical spot is where that rolls 4095 → 0 —
the **magnet seam**. `Homing_Offset` cannot move it; it only renumbers.

The question that mattered: does the offset subtraction ever report a number **outside**
0–4095? If it did, a joint crossing its seam would publish a full-revolution jump, corrupting
`/joint_states`, IK, the 3D twin, recordings and replay.

**It does not.** Bench datum, recorded verbatim in commit `95848e72`:

> J5 raw 23 under a legacy +85 offset **reads Present 4034**

`23 − 85 = −62`, and `−62 mod 4096 = 4034`. **The firmware computes
`Present_Position = (Actual − Homing_Offset) mod 4096` — it wraps.**

And the seam is physically clean too. Torque-off hand sweeps at **500 Hz** on J5 (190°, 4
crossings) and J6 (**full circle**, 3 crossings): the speed-normalised tracking ratio
`|Δpos| / (reported_speed × Δt)` over moving samples is **~1.0 both at the seam and away from
it**, and the worst single step is *smaller* near the seam than away from it. **There is no
physical dead zone.**

Three consequences, and they are the whole basis of §4:

1. No full-revolution jump can reach our software. `/joint_states` is continuous.
2. The magnet seam is invisible to us. Six of seven joints cross it routinely — a non-event.
3. **The only discontinuity our code sees is our own tick→angle map:** tick 4095 = +179.91°,
   tick 0 = −180°. That edge sits at **exactly ±180° from each joint's designed zero, for
   every joint, independent of `Homing_Offset`.**

Seam positions in the corrected frame (`(0 − offset) mod 4096`): J1 +27.4°, J2 +93.2°,
J3 −76.8°, J4 +20.6°, J5 −2.0°, J6 +1.9°, gripper +120.3° (the only one outside its travel).

### 2.2 The ±180° map edge — the one real residual

Not the magnet seam. Reachable by exactly one route:

A joint **hand-guided past ±180°** (hand-guide is torque-off by design) reports on the wrong
side — a 360° error. A subsequent torque-on then commands it the long way round.

Bounded by two facts:
- **Commanding is safe as of 2026-07-26** — but it was NOT before. `rad_to_tick` clamps into
  [0, 4095] and commanded tick travel is monotonic inside that range, which is why this file
  long said nothing about normal motion needed a reduced limit. **That argument had a hole:**
  six of the twelve arm-joint bounds mapped ONTO tick 0/4095, so the ENDPOINT itself sat on the
  seam (§3.4 item 1). Commanded goals now go through `rad_to_command_tick` (guard 5, §3.5). Jog
  still *refuses* an out-of-limit step rather than clamping; what it now silently trims is the
  outer 11.25° at those six bounds only.
- The exposure is **jog, hand-guide and replay only**. Autonomous grasping cannot reach it
  (the IK sets `q4 ≡ 0` always, and the J6 fold bounds `|q6| ≤ 90°`).

### 2.3 Q1b — the servo loop is NOT wrap-aware (measured 2026-07-26)

**A wrapped position error makes the STS servo drive the LONG way round.** Measured on J6
with a deliberate small cross-seam goal, hands off, torque verified on:

```
joint6 parked  tick   12  (−178.95°), 12 ticks from the map edge
goal seeded    tick 4090  (+179.47°)  → read back 4090, verified
  SHORT way across the seam:   −18 ticks (−1.58°)
  LONG  way round:           +4078 ticks (+358.4°)
observed: ticks 12 → 22 → 33 → 43 …   (INCREASING = the long way)
          onset 0.10 s after torque-on, ~200 steps/s, load 80–96
          AUTO-ABORT at +42 ticks (3.7°), torque cut by the tool
```

Controls that make this unambiguous, all printed by the tool:
- **hands-off baseline before torque:** drift 0 ticks, peak |speed| 0 over 39 samples;
- **`Torque_Enable` read back = 1** — without this a "nothing moved" result is vacuous,
  because a roll joint needs no torque to stay put;
- **onset at sample 2 (~0.10 s) at a constant ~200 steps/s** — exactly the commanded
  `Goal_Speed`; no hand produces a constant machine speed 0.1 s after torque arrives;
- the goal was **verified by read-back** before energising.

**Two earlier runs showing a clean hold at 8 and 12 ticks from the edge tested nothing** —
both had `goal == present`, i.e. `error = 0`, so the loop never had a wrap to resolve. A
zero-error hold cannot answer this question. (A third, earlier run *did* show a 1192-tick
runaway and was initially dismissed as possible hand interference; this test vindicates it.)

**How fast the runaway is** — this matters for the mitigation calculus and the first version
of the fix got it wrong: on the **service** path it creeps at `SEED_SPEED_STEPS` = 200 steps/s
= **0.307 rad/s ≈ 20.5 s per revolution**, because `_torque_cb` clears the trajectory and
`_write_targets` only runs on a non-empty buffer, so nothing replaces the seeded
`Goal_Position` or its `Goal_Speed`. That matches the trace above. Only the **boot** path
overrides it within ~20 ms with the boot-home glide at `GOAL_SPEED_CAP_STEPS`. A 20 s creep is
interruptible. It is **not** the 4.36 rad/s "commanded speed cap" the original comments claimed.

**Which joints are exposed** (the torque-ON transition only — an already-torqued joint tracks a
monotonic tick goal our own write loop set):

| joint | why |
|---|---|
| **J4 / J6** | full-circle windows → the boot plausibility band is a documented no-op |
| **J2 / J3** | their windows TOUCH a register end (J2 `hi = 4095`, J3 `lo = 0`), so a near-edge reading is *in*-window and equally invisible to the band |

J2/J3 are bounded: the shoulder/elbow are structurally interfered at ±180°, so the wrong-way
command stalls against `Max_Torque 800` instead of completing a revolution. And at BOOT a
J2/J3 wrap lands 2043–2048 ticks outside the window where the 400-tick band catches it — the
genuinely exposed J2/J3 path is the **service re-torque only**.

**Interim operational rule:** do not leave J2/J3/J4/J6 near ±180° and then start the
environment or exit hand-guide. Since the §3 guards, the software refuses or aborts in German
instead of running away — but the arm is then LIMP and the remedy is still a hand nudge toward
the middle. The rule that survives every guard: **do not push a joint across ±180° while the
arm is torqued** — no torque-on guard can see that (§8.2).

### 2.4 R9 — torque-off collapse: CLEARED

Read-only probe (`write`/`sync_write` replaced with raisers; refuses to run if any servo
reports `Torque_Enable = 1`). Arm powered, torque off, each joint raised by hand and released
so gravity took it to its true rest. Two runs.

| joint | worst excursion OUTSIDE its designed window | angle |
|---|---|---|
| **J5 wrist** | **107 ticks** | **9.4°** |
| J3 elbow | 70–71 | 6.2° |
| J2 shoulder | 21–29 | 2.5° |
| gripper | 1 | 0.1° |
| J1 / J4 / J6 | 0 | — |

```
worst innocent excursion   107 ticks  ( 9.4°)   measured
band in force              400 ticks  (35.2°)   margin  293 ticks (25.8°)
wrap-detection ceiling     795 ticks  (69.9°)   headroom 395 ticks
```

**Verdict: keep `BOOT_POSITION_TOLERANCE_TICKS = 400.`** 3.7× above the worst innocent rest
pose, well below the wrap floor. No `.env` retune, no code change.

Two results worth keeping:

1. **J5's 107 ticks is a MECHANICAL bound, independently cross-validated.** Its window tops out
   at +110° (tick 3300) and a hand sweep separately reached **+119.2°** — 9.2° past the
   software limit, i.e. ~105 ticks. Two independent measurements agree, so this is the joint's
   real hard stop just beyond its software limit on the relieved side, not a noisy rest pose.
2. **J1 measured exactly 0, as predicted from first principles** (it rotates about the vertical
   axis, so gravity applies no torque). **J4/J6 also printed 0, but that number is VACUOUS and
   must never be cited as evidence** — excursion is measured *outside the designed window*,
   their window IS the whole register, so 0 is definitional and would print for any pose. (An
   earlier revision of this file used it to argue the edge guard cannot collide with the band —
   circular.) The load-bearing evidence is the incidental result below.

**Incidental:** the limp arm collapses to roughly all-zeros — every joint within **~7° (~80
ticks) of tick 2048**, i.e. half a turn from either map edge. That is the real reason the edge
guard and the boot band cannot collide. It is **not** a mechanical guarantee for the two ROLL
joints, though: gravity applies no restoring torque about their axes, so they stay where they
are put, and J6 was measured swept by hand to −178.1°/+179.7°. That is exactly the state the
torque-on edge guard exists for.

Structural facts re-derived from the live windows: a wrap lands ≥1023 (J1), 2048 (J2), 2047
(J3), **795 (J5)**, 880 (gripper) ticks outside — so 795 is the ceiling the band must stay
under. J2's window ends at 4095 and J3's starts at 0, so one of each of their two wrap
landings is undetectable at ANY band width (§7).

### 2.5 R6 — joint direction signs: CLEARED

```
EDUBOTICS_EDU6_JOINT_SIGNS  →  leave UNSET (baked default = all +1)
```

**No re-provision. No re-jig.** The existing provisioning stands.

The strongest result of that session — **the limit-impossibility argument**, which needs no
accurate pose and no judgement call. A flipped sign means the true angle is the NEGATIVE of
the reading; where that lands outside the joint's own URDF limits it is physically impossible:

```
J2   read  +78.3° → flipped  −78.3° outside [0,180]     IMPOSSIBLE
J3   read −152.2° → flipped +152.2° outside [−180,0]    IMPOSSIBLE
J5   read +118.9° → flipped −118.9° outside [−90,110]   IMPOSSIBLE
grip read   +0.4° → flipped   −0.4° outside [0,103]     IMPOSSIBLE
```

J1 = +1 from a large clean +87.7° for "swing arm LEFT" (which also validated the left/right
convention used by every later test). J4 = +1 from an absolute tool-direction test. **J6 = +1
assumed, DEFERRED** — free to flip later (symmetric window, §2.5.1) and it shows itself at the
first tag-aligned grasp, which is the `GRASP_ROLL` jaw check we owe anyway.

**Q1 answered: YES.** J6 swept **−178.1° → +179.7°** (full circle) and J4 accumulated **227°**
during the captures. Both roll joints can be hand-turned to ±180°, so the map-edge residual is
genuinely reachable, not theoretical.

#### 2.5.1 Which sign flips are free

`position_limit_window` is sign-aware, but only for joints with an **asymmetric** window:

| joint | window at +1 | window at −1 | flipping later |
|---|---|---|---|
| J1 | (1024, 3072) | (1024, 3072) | **FREE** — env var only |
| J2 | (2048, 4095) | (0, 2048) | re-jig + FULL RE-PROVISION |
| J3 | (0, 2048) | (2048, 4095) | re-jig + FULL RE-PROVISION |
| J4 | (0, 4095) | (0, 4095) | **FREE** |
| J5 | (1024, 3300) | (796, 3072) | re-jig + FULL RE-PROVISION |
| J6 | (0, 4095) | (0, 4095) | **FREE** |
| gripper | (2048, 3215) | (881, 2048) | re-jig + FULL RE-PROVISION |

**Every joint whose flip would cost a re-provision is already proven by limit-impossibility.**
The one open joint (J6) is in the free column.

### 2.6 First powered poses

- **Powered zero-hold** — held.
- **HOME driven + HELD** — `[0, 0.70, −2.40, 0, 0.70, 0]` + gripper 1.75 commanded, reached and
  held. **The first powered LOADED pose on this arm**, i.e. the first time the shoulder and
  elbow were energised against gravity. No errors, no error bits, no sag.
- **The ±180° wrap test** (§2.3) — the first powered act, run with an auto-abort at 3.7°.

**Bringing the stack up IS a powered motion.** `edu6_arm_node.main()` calls `start_boot_home()`
unconditionally: torque-on with the goal seed, then a 3 s quintic glide to HOME. There is no
"connect and look" — the first powered act is already chosen by the code, and HOME is the right
choice (verified arrival, 0.30 rad tolerance, bounded re-send).

---

## 3. THE GUARD STACK — what protects this arm

Four layers, all edu6-specific unless noted. Each states what it does **not** cover.

| # | guard | trips on | rollback |
|---|---|---|---|
| 1 | **provisioning fingerprint** (`probe_bus`) | any servo whose `Min/Max_Position_Limit` ≠ the designed window, or `Operating_Mode ≠ 0`, or `Phase` bit 4 set, or a wrong model | none — it is the identity gate |
| 2 | **boot position plausibility band** | a joint reading > 400 ticks outside its designed window | `EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS`, ≥4096 disables |
| 3 | **torque-ON edge refusal** | a joint within 40 ticks of the ±180° register edge, on an OFF→ON transition | `EDUBOTICS_EDU6_EDGE_MARGIN_TICKS=0` disables |
| 4 | **post-energise wrong-way abort** | after `Torque_Enable`, any joint whose naive `goal − present` exceeds 2048 ticks (or is the longer way round) | `EDUBOTICS_EDU6_TORQUE_ABORT_TICKS=0` disables |
| 5 | **commanded-goal edge clamp** (§3.5) | never *trips* — it silently keeps every COMMANDED tick ≥128 ticks clear of the tick-0/4095 seam | `EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS=0` disables |
| 6 | **`Torque_Enable` write rail** (`feetech_bus.assert_safe_torque_enable`) | any write putting ≠ 0/1 into register 40 — **128 = „re-datum to 2048"** | none — widen `SAFE_TORQUE_ENABLE_VALUES` consciously |
| 7 | **boot fingerprint extras** — `Angular_Resolution` (reg 30) ≠ 1, non-uniform firmware, firmware in the 3.9 denylist | **never refuses**; German `[WARNUNG]` via `probe_bus`'s `logger`, once, on an otherwise-healthy arm | n/a (advisory) |

Plus: **boot-home arrival verification** (0.30 rad, arm joints only, one bounded re-send, soft
German `[WARNUNG]`, yields the command rail via `_traj_gen`), **torque-on goal seeding**
(`Goal_Position = Present_Position` written BEFORE `Torque_Enable`, so a stale RAM goal cannot
launch the arm at up to 4.36 rad/s), **~1 s wall-clock read-failure latch**, and
**torque-off on SIGTERM/atexit** (there is no servo watchdog — a dead host would otherwise
leave the arm energised forever).

### 3.1 Guard 2 — the boot band (400 ticks ≈ 35°)

Sized against the **wrap signature**, not tightly: a wrap lands ≥795 ticks outside every
detectable window, and R9 measured the worst innocent excursion at 107. The probe runs
**before** torque-on, i.e. on an arm flopped under gravity, where resting slightly past a
designed limit is HEALTHY — at the original 114 ticks a limp J5 refused „Umgebung starten" on a
completely healthy arm with only 4.7° of margin. A real, live near-miss.

**Documented no-op on J4/J6** (full-circle windows — nothing can read out of range), and on
J2/J3 one of the two wrap landings is undetectable because their windows touch a register end.
The retry-every-5 s probe loop means nudging a joint back **self-heals** without a restart.

### 3.2 Guard 3 — the torque-ON edge refusal (40 ticks ≈ 3.5°)

Sized for the read→write window, not the sensor dither: the dither needs 1–2 ticks, but a hand
still on a free-spinning roll joint at several rad/s covers ~16 ticks in 5 ms. So 40 is ~20×
the dither and ~2× that hand case. Refused readings are exactly tick ≤ 39 (≤ −176.57°) and
tick ≥ 4056 (≥ +176.48°) — **the window is asymmetric**, because the map is.

Four properties, each of which was got wrong once and corrected:

1. **EVERY joint is judged.** The original `full_circle_joint_indexes()` predicate was
   **deleted**, because its justification was false: it claimed a trimmed window immunises the
   joint since the servo clamps `Goal_Position` into it. **The clamp bounds the GOAL, not the
   PATH.** Trim J4/J6 to ±170° → window (114, 3982); a joint hand-parked at tick 4095 seeds
   goal 4095, the servo clamps to 3982, and if `present` crosses to tick 2 before the loop
   reads it the naive error is **3980 ticks = 350°**. Checking all 7 **cannot over-refuse**: a
   reading within 40 ticks of a register edge on a joint whose window is clear of both ends is
   necessarily far outside that window (J1 ≥ 984, J5 ≥ 756, gripper ≥ 841 ticks out, i.e.
   66°–176° past a designed limit) versus R9's 107-tick worst innocent excursion. What it ADDS
   over the band is the in-window near-edge readings: J4/J6 anywhere, J2 at its 4095 end, J3 at
   its 0 end, and any future trimmed window automatically.
2. **It is evaluated ONLY on an OFF→ON transition** (`self._torque_on and not self._bus_fault`).
   Ungated it broke jogging: `workshop_jog_callback` asserts `_set_follower_torque(True)` on
   EVERY jog, so any jog target beyond ±176.5° on J4/J6 — inside the URDF window `WorkshopJog`
   accepts — refused, and three of those produce „Arm konnte wiederholt nicht verriegelt
   werden … Bitte die Umgebung neu starten": a misdiagnosis (the arm IS locked) whose remedy
   `probe_bus` then refuses at the same tick. `not self._bus_fault` is in the condition because
   `_torque_on` is our own BELIEF, never a reading — a 12 V brown-out drops real torque without
   touching the flag — so the guard re-arms precisely while the read loop cannot see the arm, at
   zero cost to a healthy jog.
3. **It sits at the single torque choke point**, inside `_seed_goal_from_present_locked` and
   BEFORE any register is written, so it covers boot AND `/edu6/set_torque` + the legacy alias —
   i.e. the „Beenden" hand-guide exit, which is the most likely way a joint is left on the edge.
   A guard only in `probe_bus` would have missed that path entirely. **Torque-OFF is never
   guarded.** `probe_bus` carries the same check deliberately, because it self-heals through
   `main()`'s 5 s retry whereas `start_boot_home` runs exactly once.
4. **The env knob's polarity is the INVERSE of the band's** and is now stated at the knob: the
   band is a tolerance where large = permissive and ≥4096 disables; this is a refusal RADIUS
   where **0 disables** and larger refuses MORE. Anything ≥ `TICKS_PER_REV // 2` would refuse
   4096 of 4096 ticks — an arm that can never energise — so it falls back to the default with a
   `[WARN]` naming 0 as the real disable value.

**Measured: this guard cannot refuse a pose autonomous grasping actually reaches.** 16,136
solutions from the real solver over the whole documented annulus — closest approach to a
register edge is **253 ticks measured / 247 analytic** — see §3.5. That is **~2×** the 128-tick
clamp. **Do NOT frame this as a headroom correction** — guard 5's headroom at 128 was 2.0× (256/128) and is 1.98× (253/128); the "6.4×" is 256/40, the EDGE guard's ratio, and becomes 6.3×. Neither DEFAULT was ever mis-justified; only the FENCE was, having been set equal to the wrong measurement.

> ⚠️ **This number has now been wrong three times in a row** — 367 (16,136-solution sweep) →
> 263 (39,804) → 256 (977,808) → **253** (1,183,341 over a fine grid at the razor edge near full
> extension). Every one was a sparse-sweep artifact of the one before. The **analytic** bound is
> what finally settled it: q2/q3/q5 depend only on (ρ, ψ), and `q3 = γ + ALPHA0 − π/2` with
> `ALPHA0 = −68.3317°` gives **q3 ≥ −158.3317° always** ⇒ tick **247**. Quote the analytic
> figure, not a sweep. Only jog, hand-guide and replay reach the outer
3.5°. Cost: the outer 3.5° of J2's travel refuses a torque-ON transition — HOME is 40°, the
worst grasp 130.8°.

### 3.3 Guard 4 — the post-energise wrong-way abort

**Why guard 3 alone is not enough.** The wrapped error only forms if `present` crosses the
4095/0 boundary BETWEEN the goal-seed read and the moment `Torque_Enable` applies. Pure wire
time for that window at 1 Mbit/s 8N1:

```
sync_read request  (Present_Position, 2 B, 7 ids)   15 B
7 × status reply   (2 param bytes each)             56 B
seed sync_write    (accel+goal+time+speed, 7 B ea)  64 B
torque sync_write  (1 B each)                       22 B
                                              TOTAL 157 B  = 1.570 ms
```

Covering 40 ticks (0.0614 rad) in 1.60 ms needs **39.1 rad/s** — unreachable. But those
1.57 ms EXCLUDE per-servo `Return_Delay_Time`, pyserial's byte-at-a-time header hunt, 1 ms
CDC-ACM USB frames through usbipd/`vhci_hcd`, and GIL/container scheduling — all **unbounded**.
At 20 ms the same margin needs only **3.07 rad/s**; at 50 ms, 1.23 rad/s. One WSL2 stall plus a
hand on a free-spinning roll joint defeats it. **The 40-tick margin is a probability argument
over an unbounded window.**

So immediately AFTER `Torque_Enable = 1`, `set_torque` re-reads position once and writes
`Torque_Enable = 0` again **before anything else** — no logging, no message building, no
allocation. This is exactly what the bench tool did when it caught the real runaway and cut
torque after 3.7°, and it caps a runaway **regardless of how long the pre-torque window turns
out to be**:

| added scheduling (each way) | total | travel @ 200 st/s | travel @ 2840 st/s |
|---|---|---|---|
| none (wire + 1.8 µs verdict) | 0.93 ms | 0.19 ticks = **0.02°** | 2.6 ticks = **0.23°** |
| 1 ms CDC-ACM frame | 2.93 ms | 0.05° | 0.73° |
| 5 ms GIL switch interval | 10.9 ms | 0.19° | 2.73° |
| 20 ms WSL2 stall | 40.9 ms | 0.72° | 10.2° |

— against the 360° it prevents.

**The threshold is definitional, not tuned.** Two populations can make the naive error non-zero
after the seed:

- **legitimate — the EEPROM clamp pull-in.** The servo clamps `Goal_Position` into its window,
  so a limp joint resting outside its window is genuinely pulled to the window edge. Enumerated
  over all 4096 readings × the 7 designed windows: worst pull-in among readings guard 3
  PERMITS = **2008 ticks**; with guard 3 DISABLED (margin 0, the documented rollback) the
  supremum is exactly **2048**, at tick 0/4095.
- **wrapped:** `|error| = 4095 −` ticks travelled inside the read→write window, i.e. ≥ **3443**
  even at an absurd 20 rad/s held for 50 ms.

`TORQUE_ABORT_ERROR_TICKS` = `TICKS_PER_REV // 2` = **2048** with a STRICT `>`. It sits *at*
the legitimate supremum (unreachable, guard enabled or not) and 1395 ticks below the wrapped
infimum. And `|error| > TICKS_PER_REV/2` is algebraically identical to
`TICKS_PER_REV − |error| < |error|` — "the naive path is strictly longer than the true short
way", which is the defect itself. Both forms are implemented: they coincide at the default, so
the second *looks* free to delete, but the knob may be lowered for bench sensitivity and below
2048 a bare magnitude test starts flagging ordinary pull-ins.

> ⚠️ **A trap that nearly shipped a false abort:** the legitimate bound is NOT
> `BOOT_POSITION_TOLERANCE_TICKS` (400). That holds only at BOOT. On the **service** re-torque
> path no band runs, so the real software bound is 2008. Sizing against 400 with slack would
> have aborted healthy torque-ons.

**The goal is COMPUTED, never read back.** `_seed_goal_from_present_locked` returns
`{sid: clamp(written_tick, *position_limit_window(...))}` — the same call `probe_bus`
hard-gates the plugged arm against to ±1 tick, so the model is accurate to ±1 tick against a
2048-tick threshold at **zero bus cost**. Reading `Goal_Position` back was rejected: 7 round
trips (~7 ms plus 7 timeout chances), or one 16-byte block read spanning **undocumented
registers 50–54**, on an untested span, at the exact moment the arm might be running away.
**Recorded residual:** a seed `sync_write` lost on the wire is invisible to a computed model,
because SYNC_WRITE takes no reply.

**No transition gate — deliberately.** Guard 3 had to be gated or it broke jogging; this one
judges **measured motion, not a park position**, so a torqued joint holding at tick 4094
reports `goal − present ≈ 0` and passes. That ungated placement is what covers the two
torque-on sub-cases guard 3 misses: an optimistic `_torque_on` after a brown-out shorter than
the read-fail latch, and a redundant torque-on re-seeding the goal on a joint whose dither
straddles the seam.

Cost on a healthy arm: exactly ONE extra `sync_read` = **0.71 ms**, per boot / per „Beenden" /
per touch-off / per `WorkshopJog` service call — never per 50 Hz tick.
`POST_ENERGISE_READ_ATTEMPTS = 2` gives one bounded retry, because de-energising on a single
dropped CDC-ACM reply would collapse a backdriving arm. A persistently silent servo aborts with
kind `'read'` (a bus problem — the boot path keeps retrying); a wrong-way verdict wins over a
partial read. Env `EDUBOTICS_EDU6_TORQUE_ABORT_TICKS` has **both** ends fenced: `0` disables,
below **401** would abort exactly the pull-in the boot probe deliberately ACCEPTS, above
**4095** exceeds the largest error a 12-bit register can express (silently dead — the sibling's
"≥4096 disables" habit landing in the wrong place).

### 3.4 What the guard stack does NOT cover

1. **✅ SEAM-PARKING — CLOSED 2026-07-26 (guard 5, §3.5).** Was the top open hazard.
   Six of the twelve arm-joint limit bounds mapped exactly onto a register end
   (J2 `hi`→4095, J3 `lo`→0, **J4 and J6 both ends**), so a command AT one of them
   pinned `Goal_Position` on the seam, and one encoder sample across the boundary
   committed the joint to a full revolution at `GOAL_SPEED_CAP_STEPS` (4.357 rad/s,
   1.44 s/rev) — with no torque-on event for guards 3 or 4 to hang off (the edge
   refusal is OFF→ON-gated; the abort only runs at torque-on). Reachable with no
   external force at all: hand-guide J6 past +180° → the reading WRAPS to tick ≈0 →
   a „Bewegung aufnehmen" recording stores ≈ −3.14 rad → **replaying it commands
   tick 1**. `WorkshopJog` also accepts exactly `hi` (`if new < lo or new > hi`).
   `_write_targets` now converts through `rad_to_command_tick`, which clamps every
   commanded tick into `[m, 4095 − m]`, m = 128 by default. The minimum forced
   displacement for a COMMANDED goal rises from **1 tick (0.088°)** to
   **m + 1 = 129 ticks (11.34°)** — sensor dither to real external force.
   **System-wide the figure is 41, not 129**, because the goal SEED is exempt and
   guard 3 permits a park at tick 40, so a bare `/edu6/set_torque(True)` (what
   hand-guide's „Beenden" calls) can leave the goal 40 ticks from the seam until a
   trajectory arrives — and the abort cannot see it (goal ≈ present ⇒ err ≈ 0).
   Still 41× better than today's hair trigger. The **external-force** variant on
   top of that is still §8.2's watchdog only.

   > ⚠️ The minimal fix sketched in an earlier revision of this file said "for the
   > full-circle joints". **That was wrong** and would have shipped the hole again:
   > J2's `hi` and J3's `lo` are not full-circle joints and were exactly as pinned.
   > The shipped clamp is BLANKET, which is also a no-op on J1/J5/gripper by
   > arithmetic (≥795 ticks clear) and survives a sign flip for free. Same false
   > premise the deleted `full_circle_joint_indexes()` died of.
2. **A lost seed `sync_write`** (above).
3. **Nothing in the UI says „limp".** Both guard-3 and guard-4 refusals leave a **HEALTHY
   container** — the probe passed, `/joint_states` exists, the healthcheck is green — with a
   **LIMP arm** that sags under gravity while every incoming trajectory is dropped with a
   German `[WARNUNG] … Drehmoment ist aus`. That is the same end state as the pre-existing
   "3 torque-on attempts failed" branch, and it is intentional (never energise an arm we
   believe would run away), but it is invisible in the UI.
4. **`physical_ai_server._set_follower_torque` discards `res.message`** — session 7 fixed this
   (§5.2 F8), so the driver's specific German reason now reaches the toast instead of the
   generic „bitte die Umgebung neu starten". Verify on the rebuilt image.
5. **NEW TRANSITION introduced by guard 4, recorded because it was not:** the server asserts
   torque-on on **every** `WorkshopJog`, so two consecutive full-bus read failures now
   **de-energise an arm that may be holding an object** — and drop it (refusal kind `'read'`).
   Bounded by `POST_ENERGISE_READ_ATTEMPTS = 2` and it sits inside the pre-existing
   `READ_FAIL_STOP_S` envelope, so it is not a new *class* of failure — but
   "de-energise an already-torqued arm because a read failed" is a new transition and belongs in
   R5's observations (that gate measures exactly this bus's reliability through usbipd).

### 3.5 Guard 5 — the commanded-goal edge clamp (128 ticks ≈ 11.25°)

`rad_to_command_tick` clamps every tick written as `Goal_Position` into
`[m, TICKS − 1 − m]`. `_write_targets` is its only caller; **`rad_to_tick` stays a
plain register-range map** so a future non-command caller cannot inherit a safety
clamp, and the `[0, 4095]` clamp keeps its own independent assertion (an
out-of-range tick would make `fb.le16` silently encode a different number).

**The goal SEED is deliberately EXEMPT.** `_seed_goal_from_present_locked` writes
`Goal = Present` from a MEASURED tick to hold a limp arm still; clamping it would
command a real move of up to `m` ticks at the instant torque arrives — a software
guard CREATING motion. Guards 3 and 4 already own that state.

**Sizing — NOT against dither, and this is why it is wider than guard 3.** Guard 3
judges an arm AT REST whose goal is about to equal its own reading, so dither
(1–2 ticks) is the whole threat and 40 is ~20×. This clamp judges a goal a MOVING
joint is arriving at, so it must also cover OVERSHOOT: an overshoot larger than the
margin carries `Present_Position` across the seam and defeats the clamp on the very
path it protects. Overshoot is UNMEASURED here — the position P/I/D gains have never
been read (an R5 item), the gear train backdrives, and while the normal case is a
quintic with zero terminal velocity advancing ≤56.8 ticks per 20 ms, a trajectory
TRUNCATED mid-flight („Stoppen", a latched bus fault, a chunk that never arrives)
pins the goal ~57 ticks ahead of a joint at the cap whose planned decel ramp alone is
v²/2a ≈ 807 ticks. **128 is provisional pending R5** — add "overshoot past a pinned
goal at the cap" to that gate. Under ~20 ticks measured, 40–80 would do; over ~100,
revisit 246 and/or lower `GOAL_SPEED_CAP_STEPS` (Q6) — the ceiling itself cannot simply be
raised, it is pinned one tick under the solver's analytic floor.

**And no, the EEPROM window does not help.** `Min/Max_Position_Limit` clamps the GOAL
register, never the physical PATH — the same lesson `full_circle_joint_indexes()`
died of. Even at face value there is no headroom exactly where it is needed: all four
joints carrying a seam bound have windows TOUCHING a register end — J2 (2048, 4095),
J3 (0, 2048), J4 and J6 (0, 4095).

| m | = deg | forced motion still needed | commandable J4/J6 |
|---|---|---|---|
| 40 | 3.52° | 3.60° | ±176.40° |
| **128 (default)** | **11.25°** | **11.34°** | **±168.66°** |
| 246 (ceiling) | 21.62° | 21.71° | ±158.29° |

> The `±` in that last column is a **one-tick rounding**, quoted from the positive side, exactly as
> the edge guard's own band is asymmetric (−176.57° / +176.48°) — because the map is. Executed
> against the shipped `rad_to_command_tick` at m = 128 the true range is ticks **128…3967** =
> **−168.750° … +168.662°**. Immaterial physically (0.088°), recorded because every other number in
> this file is exact and a reader re-deriving the negative bound would not reproduce `−168.66`.

Ceiling **246** = joint3's ANALYTIC floor (247) − 1, so every commandable solver tick is
STRICTLY interior. Derived from `q3 = γ + ALPHA0 − π/2` with `γ ≥ 0` (elbow-down is never
in-limit — 0 of 8.0 M cells) and `ALPHA0 = −68.33167°`. **No sweep can invalidate an analytic
bound**, which is the point: 367 / 263 / 256 were all grid artifacts, because ρ(γ) is
square-root-singular at full extension so a grid uniform in ρ or xyz resolves γ only as
O(√Δρ). SCOPE: this is a claim about the OPERATING ENVELOPE (z ≥ 0). joint2's `hi` = +π maps
EXACTLY onto tick 4095 and `solve()` will return it — but only for targets ≥ 16.4 cm BELOW the
base plane (it enters the 246-band at 7.96 cm below), which the workspace floor refuses long
before IK is consulted.

**Cost.** Only the outer `m` ticks at those six bounds. Nothing autonomous is near:
HOME's closest joint is **483 ticks** (J3 at −2.40 rad → tick 483) and the solver's true floor
is **247 ticks analytic / 253 measured** (J3, approached as the arm straightens toward full
extension), with `q4 ≡ 0` and J6 folded into ±90° (ticks 1024…3072). The ceiling is **246**,
derived from the ANALYTIC floor so it cannot become a fourth sweep artifact — no legal override
can shift a real solution. Real headroom at the shipped 128 is **~2×** (253/128) — unchanged by this correction; the
6.4× was 256/40, the EDGE guard's ratio, now 6.3×. Hand-guide keeps the full
physical range (torque is off), and a REPLAY holding J6 at 179° can only have come
from hand-guiding past the wrap — the corrupted artifact this clamp neutralises.

**Known accepted cost of being wider than guard 3.** A joint can legally be TORQUED
inside the clamp band (guard 3 permits a park at tick 40; the exempt seed holds it
there), so the first command jerks it up to **88 ticks (7.73°)** of unrequested TRAVEL — but note
that 88 holds only while `EDUBOTICS_EDU6_EDGE_MARGIN_TICKS` is at its default 40. Set it to
`0` (the documented rollback for guard 3) and the jerk becomes **128**; the same applies after
an external displacement of an already-torqued joint, which has no torque-on event. And
"88 more than requested" conflates travel with deviation: the joint can end **128 ticks
(11.25°) from the requested target, in the opposite direction**. `WorkshopJog`'s reply
reports the requested target rather than the achieved one — and for J2/J3 its reported world
position is wrong by the trim (for J4/J6 the world XYZ stays correct, since the fingertip TCP
lies on the J6 axis). Accepted: the jerk is always directed AWAY from the seam toward
`CENTER_TICK`, so it can never itself produce a wrapped error; it is bounded by the
margin; and it is only reachable after a hand-park within 11.25° of ±180°. Setting
`EDUBOTICS_EDU6_EDGE_MARGIN_TICKS=128` makes the bands coincide again, trading the
jerk for a wider torque-on refusal band — an `.env` decision, not a code change.

**Knob** `EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS`, compose-forwarded. SAME polarity as
guard 3's (0 DISABLES — the one-variable rollback; larger clamps HARDER), i.e. the
INVERSE of `EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS`. Ceiling **246 INCLUSIVE** — one tick under
joint3's 247-tick ANALYTIC floor, so every commandable solver tick is STRICTLY interior — above it
the clamp starts SHIFTING real solver solutions instead of only the jog/replay
extremes (248 is the first margin that moves the 247-tick floor solution — 247 itself does not, because the band boundary is inclusive); values above fall back to the default with
a `[WARN]` naming 0 as the real disable value. No lower fence beyond 0: a small margin
only weakens the clamp, it can never invent motion. **Residuals:** an overshoot larger
than `m`; the exempt seed can still leave a goal as close as tick 40 to the seam
between torque-on and the first command (arm at rest ⇒ dither only); and the
external-force variant, which remains §8.2 territory.

---

## 4. BINDING DECISIONS

### 4.1 Six-horn mechanical re-clock — **DROPPED**. Do not re-assemble this arm.

Its only purpose was to move the **magnet seam** out of each joint's travel. Per §2.1 the seam
is invisible numerically AND clean physically, and re-clocking **cannot** move the ±180° map
edge — that edge is defined by where zero is, not where the horn sits. The operation would cost
six horns, a re-jig, a full re-provision and a fresh chance to mis-seat a tooth, for nothing.

### 4.2 Limit trim on J2/J3/J4/J6 — **DROPPED** (Sven)

Proposal: shrink J2 to 0…170°, J3 to −170…0°, J4/J6 to ±170°. Measured cost was genuinely
zero (82,584 targets swept: **57,720 solvable before and after**, identical reach annulus, at
both 5° and 10°). Peak joint usage across every solved grasp: J1 90.0° (at its limit — never
trim J1), J2 130.8°, J3 145.5°, J4 0.0°, J5 108.9° (of 110° — never trim J5), J6 90.0°.

Dropped anyway, on four grounds:
- It **forces a full re-jig + re-provision** — the EEPROM windows *are* the boot fingerprint,
  and the tool re-derives `Homing_Offset` from the jig pose, so it cannot be re-run un-jigged.
- Three linked sites (`edu6_ik._EDU6_JOINT_LIMITS_RAD`, `edu6_arm_node.JOINT_LIMITS_RAD`,
  `tools/edu6_provision.JOINT_LIMITS_RAD`) plus **two no-drift tests** that assert exact URDF
  equality.
- **It detects, it does not prevent.** Hand-guide is torque-off — a hand pushes past any
  software limit.
- **And it does NOT immunise the joint** (discovered session 7): the servo's clamp bounds the
  GOAL, not the PATH — see §3.2 property 1. A ±170° trim turns a 360° runaway into a 350° one.
  This kills the trim's last claim to being a *complete* fix.

Full range ±180° is therefore **kept**. It stays on the table purely as defence-in-depth for
the next time the arm is jigged, where it costs only two registers on servos 4/6
(`Homing_Offset` is unaffected — the trim is symmetric about the same zero, and ±170° maps to
(114, 3982) for **both** signs, so it is sign-free).

**Scheduling note:** if any joint sign is ever flipped, a re-jig is forced regardless — that is
the cheapest moment to add a trim if it is ever wanted.

### 4.3 J6 jaw-symmetry fold into ±90° — **KEPT** (Sven)

`solve()` folds every `q6` into [−90°, +90°] via its jaw-identical half-turn.

**This is not a limit.** J6 keeps ±180° in EEPROM, in jog and in replay. The gripper is a
parallel jaw pair symmetric about the roll axis (CAD meshes map onto each other under a 180°
roll to 0.01 mm; the OMX's do **not** — a fold there shifts the grasp 3.3 mm), so `q6` and
`q6∓π` are the **same physical grasp**. Verified: 0 targets lost, max |q6| exactly 90.0000°,
TCP shift 4e-12 mm, jaw azimuth preserved mod π.

Two reasons it earns its place:

1. **It halves the worst wrist swing between grasps: 358° → 178°.**
   `trajectory_builder.build_segment` computes a straight joint-space delta with **no
   shortest-path unwrapping**, so without the fold two cubes 2° apart in yaw (q6 = +179° and
   −179°) make the wrist drive nearly a full turn — and the velocity floor *stretches* that
   into ~3.6 s of deliberate spinning.
2. **Autonomous grasping never parks J6 at ±180°**, the one position where the next boot can
   read it 360° wrong. This is why the fold and the no-trim decision fit together: without the
   trim we cannot **detect** a bad J6 reading, so the next best thing is guaranteeing our own
   software never creates one.

> ⚠️ The rationale originally in the code comment and tests was **WRONG** (it claimed the
> reading "flickers 4095↔0 so the servo sees a ~4000-tick error and slams the motor", and that
> `Homing_Offset` displaces the seam by ±7.2°). §2.1 disproves both. The fold survives on new
> evidence, not the old.

**The seed-relative upgrade was TRIED and REJECTED.** Picking whichever twin is nearest the
CURRENT wrist angle does the opposite of what was claimed:

```
                                  max |q6|    worst swing   mean swing
fold, nearest-ZERO (shipped)         90.0°        178.9°        60.2°
fold, nearest-SEED (rejected)       180.0°        178.9°        48.5°
no fold at all                      180.0°        358°         ~120°
```

The seed is the PREVIOUS grasp's own answer, so the rule feeds its output back in and drifts —
grasp 1 from rest → −90°, grasp 2 → **−180.0°, exactly the map edge**. That voids reason 2, and
the promised benefit never existed: the worst swing is identical either way; only the MEAN
improved. Not worth trading a bounded joint for 11° of average wrist travel. `solve()`'s
documented "`seed` does not change the solution" contract holds again. Guards:
`test_j6_fold_ignores_any_seed_and_never_leaves_the_pm90_window` (seeds ±140°, ±179°, ±180°)
and `test_j6_never_drifts_toward_the_map_edge_over_chained_grasps` (400 chained solves).

### 4.4 `roll` and the roll JOINT are DIFFERENT NUMBERS on edu6

They coincide on the OMX (`theta5 = roll`), which is why the shortcut survived review. On edu6
`q6 = fold(wrap(π − roll))`, so a "move the tool, keep the current wrist" caller that passes
`joints[roll_idx]` into `roll=` gets `−q6` back and **mirrors the wrist** — up to 178°,
rotating a held object with it.

Both solvers expose **`roll_from_joints(joints)`** (OMX: identity; edu6: `wrap(π − q6)`) and all
three call sites use it: the Cartesian jog (`physical_ai_server.py`), the lift (`motion.py`,
whose `roll=` feeds `plan_safe_route`'s via-point re-solves), and `plan_safe_route`'s own
`roll=None` fallback. The jog additionally PINS `arm_q[roll_idx] = arm[roll_idx]` after solving,
because outside the fold window the round-trip returns the jaw-identical twin (same grasp, 180°
joint swing) — free, since the edu6 fingertip TCP lies exactly ON the joint6 axis.

### 4.5 Other standing decisions

- **Overcurrent floor:** keep factory ~2 A (`PROTECTION_CURRENT = 310`), not the plan's 975 mA —
  the STS3250 shoulder/elbow draw more than that holding the arm's weight and a lower trip
  nuisance-fires. Real force safety is `Max_Torque` (800 arm / 150 gripper) plus the untouched
  factory overload protection. **Finalize from live `Present_Current` at R4.**
- **Mixed STS3215 + STS3250 is by design** — every identity check accepts
  `feetech_bus.STS_ACCEPTED_MODELS` and records the actual per-servo model. **Never hardcode 777.**
- **Hand-guide = torque-off limp.** The STS gear train backdrives, so the arm falls when
  released and the student must hold it. The compliant-mode alternative is documented but not
  shipped.
- **Safe-home = mirror the OMX, not lift-then-fold.** No OMX homing path does collision path
  planning; OMX is safe via gentle quintic + hardware current-limit backstop + verified
  delivery. edu6 already had the first two; the gap was arrival verification, now closed.
  Lift-then-fold is shelved unless R7 shows edu6 collides where OMX would not.
- **Keep the custom driver — do NOT port edu6 to ros2_control.** Gains are near-empty for a
  follower-only profile; costs are real (no Feetech hardware interface in the image,
  `open_manipulator` currently compiles nothing, and recovery from a read fault would need
  `controller_manager_msgs`, the package banned from the server image for an ABI crash). 50 Hz
  is right: 8.2 % bus duty, and the command stream is generated at 30 Hz so 50 Hz already
  oversamples 1.67×.
- **Do NOT drive the arm to URDF zero** — see §9.2.

### 4.6 build-at-2048 — the procedure for a NEW or rebuilt arm

Decide the **direction signs** (`EDUBOTICS_EDU6_JOINT_SIGNS` — the limit windows are
sign-aware, so a later flip forces a full re-provision) **before** starting.

1. Per servo, **one at a time on the bus** (they all ship as ID 1, and `edu6_provision.py` sets
   neither ID nor baud): ID 1–7 in joint order → baud 1 Mbps → `Response_Status_Level = 1`
   (some ship 0 and then never ack a write) → clear `Phase` bit 4 → `Homing_Offset = 0` →
   `Operating_Mode = 0`. EEPROM writes need `Lock = 0` first — **the register is inverted
   (0 = writable)** — then re-lock.
2. **Find 2048 and mark it.** Torque on, command `Goal_Position` 2048, read back to confirm.
   Nothing attached; it may spin a long way. Mark the output spline against the case: that mark
   means *"the magnet seam is 180° from here."*
3. **Assemble and verify with a number.** Fit horn + link so the joint sits at **its own zero**
   while the servo reads 2048. 25 teeth = 14.4°/tooth — pick the closest. **Acceptance per
   joint: jigged at zero with offset still 0, `Present_Position` reads 2048 ± 82 ticks.** That
   single number is the whole build-quality gate.
4. Run `tools/edu6_provision.py` on the fully-assembled arm jigged at all-zeros.

⚠ **Trap:** `Torque_Enable = 128` does NOT do this. The vendor note is „任意当前位置较正为2048" —
it **re-datums whatever position the joint is standing in to tick 2048**, i.e. it RENUMBERS; only
mechanical alignment moves the seam. (An earlier revision of this file said it "writes a
`Homing_Offset`" — that mechanism is an inference the vendor sources do not support.) Whether it
survives a power-cycle is undocumented: register 40 is SRAM, the position correction at 31 is
EEPROM, and no vendor text says whether `Lock` gates it — and it does not matter, because a
session-long re-datum is equally wrong. **Since 2026-07-26 the write is REFUSED outright** by
`feetech_bus.assert_safe_torque_enable` — guard 6. It is a SILENT-corruption hazard rather than a
mere footgun: a re-datum leaves `probe_bus`'s whole fingerprint (windows, mode, Phase, resolution)
intact, so the arm boots green in a wrong frame.

⚠ **Scope.** Building at 2048 buys `Homing_Offset ≈ 0` and a magnet seam at ±180°. It does
**not** move the ±180° `Present` map edge — that edge is at joint ±180° for every offset, since
the joint angle is derived from `Present` and 2048 ≡ 0°. **A new arm built this way is exposed
to §2.3's non-wrap-aware loop exactly like this one** and relies on the same guard stack. For a
full-circle joint (J4/J6 today) **the only complete hardware fix is a MECHANICAL stop limiting
travel below 360°** — worth designing into a future arm revision.

**On the zero pose:** URDF zero is *not* HOME and is a **folded** pose — the forearm doubles
back past the shoulder (**base→wrist 141.5 mm**, vs 312.5 mm at HOME). Zero is a JIG pose,
reached by hand while limp. You do not hold the whole arm folded during assembly: a joint's
zero is the relative angle between its own two links, so set each as you build up the chain.
All-zeros is needed once, for provisioning.

---

## 5. CODE STATE

### 5.1 Pushed to `main`

```
651cda3a  pre-power-on audit rails (fingerprint boot gate, driver guards, bench-tool hardening)
bf1f9090  accept the mixed STS3215+STS3250 servo set (bench R2 discovery)
95848e72  provisioning bench hardening (offset wrap, EEPROM write latency, overcurrent floor)
9cd43c23  pre-bench audit — J6 fold drift, jog wrist mirror, boot band, torque-on seed
b8f3c71d  the no-go-zone reroute ladder was structurally dead on edu6
5427a3be  the place path — gripper over-close, and a baked release clearance
```

`.gitignore` excludes `tools/edu6_records/` (Supabase `edu6_arm_records` is the fleet store).

### 5.2 Session-7 work — LANDED on `main`

Six commits, 29 files, +4,900 lines. All pushed; working tree clean.

```
6cce3da0  fix(tools): verify_image_bytes compared against a HARDCODED stale revision
5edb7979  tools(edu6): bench tools for the powered gates
86ad92f8  docs(edu6): session-7 invariants, the dated story, and a rewritten handoff
33f7dd42  fix(edu6): workflow layer — a false grasp success, dead reroutes, a mirrored wrist
e8fa214c  fix(edu6): driver guard stack — seam hazards, write rails, fingerprint extras
5427a3be  fix(edu6): the place path — gripper over-close, and a baked release clearance
```

(Plus the doc commit that necessarily follows this list — `git log` is authoritative.)

**CI: `CI` and `docker-publish` both `completed / success` on `5edb7979`.**

**Independently verified before landing — verdict: nothing ships broken.** Four adversarial
passes ran over the round; between them they killed **~100 mutations**, and what they found was
one under-described pre-existing hazard (seam-parking, now guard 5) plus a long list of **wrong
numbers in comments**, all corrected. Two implementer agents also refused instructions of mine
that were wrong — see §11.

What shipped, by work package:

| pkg | contents |
|---|---|
| **A** | guard 3 scope: judge EVERY joint (`full_circle_joint_indexes` deleted — its premise was false), OFF→ON transition gate (the unconditional version broke jogging) |
| **B** | guard 4, the post-energise wrong-way abort |
| **C** | eight sim defects F1–F8 — headline: a grasp with no approach room was reported as SUCCESS |
| **D** | guard 5, the commanded-goal edge clamp (seam-parking) |
| **E** | six vendor-research fixes: the error-byte fallback, the `Torque_Enable` write rail (guard 6), `Angular_Resolution` + firmware fingerprinting (guard 7), and two docstring corrections |

Two items in package E were deliberately **NOT** done, and the reasoning matters more than the
code: **C1** (read `Torque_Enable` back) was SKIPPED because guard 4 already covers the only
harmful case *by measurement*, and in the benign case a read-back would have refused the
torque-on and dropped a held object; **B2** was DOWNGRADED from a refusal to a hedged warning
because the „FW 3.9 corrupts SYNC_READ" claim traced to a single confounded, unconfirmed report.

### 5.2.1 The two real test gaps — fixed and mutation-verified

`_MIN_APPROACH_CLEARANCE_M → 0.0` and `<= → <` survived mutation because every F1 test landed
on radii the bisect reports as unusable and the band test compared against the **symbol**
(`got > motion._MIN_APPROACH_CLEARANCE_M` stays true when the constant becomes 0.0). Fixed by
pinning the **literal** 0.002 and adding
`test_a_real_but_sub_floor_rise_is_refused_not_just_an_exactly_zero_one`.

Measuring that case corrected the round's own framing: at grasp z = 0.015, **no** edu6 radius
bisects to exactly zero — all 23 sub-floor radii rise **exactly 1.875 mm**, one bisect step
(`0.06 / 32`). "Zero clearance" is the failure *mode*, not universally the measured value, and
1.875 mm is the interesting case a naive "refuse only exact zero" check would accept. Verified:
the mutation now fails **both** tests, with a byte-identical restore. `<= → <` remains a
provably *equivalent* mutant (the bisect can only report multiples of 1.875 mm, so no reachable
rise equals exactly 0.002) — documented in the test rather than papered over.

```
robotis_ai_setup/docker/open_manipulator/edu6_arm_node.py    +619   plain COPY → image-relevant
robotis_ai_setup/docker/docker-compose.yml                    +19   EDUBOTICS_EDU6_TORQUE_ABORT_TICKS
robotis_ai_setup/tests/test_feetech_bus.py                  +1173   149 test methods
physical_ai_tools/.../workflow/handlers/motion.py            +340   COPY-wholesale
physical_ai_tools/.../workflow/handlers/perception_blocks.py  +128
physical_ai_tools/.../workflow/path_guard.py                 +105
physical_ai_tools/.../physical_ai_server.py                  +103
physical_ai_tools/.../robot_profiles.py                       +17
physical_ai_tools/.../test/*.py  (5 files, 1 new)            +745
CLAUDE.md · docs/CLAUDE-CHANGELOG.md · this file
tools/edu6_bench/  (7 files, untracked — §10, a decision for Sven)
```

**(A) Edge-guard scope corrections** — `edge_parked_joints` now judges EVERY joint
(`full_circle_joint_indexes()` deleted, §3.2 property 1); the transition gate
`offenders = [] if self._torque_on and not self._bus_fault else edge_parked_joints(ticks)`
(§3.2 property 2); `_EDGE_MARGIN_MAX_TICKS = TICKS_PER_REV // 2` (§3.2 property 4). Reported
22/23 mutations killed on the first pass, the survivor then killed.

**(B) Post-energise wrong-way abort** — §3.3. Reported 23/23 mutations killed. Writing one of
them (`M9`, unguarded read+decode) **found a real bug in the implementation itself**: the
position decode sat *outside* the `try`, so a garbled reply would have escaped to
`set_torque`'s bare `except` and left the arm **energised with no diagnosis** — precisely what
the method exists to prevent. Fixed, with two tests pinning both halves.

**(C) Eight sim-sweep defects (F1–F8)** — found by driving the real edu6 solver through the
Roboter-Studio grasp/place paths in sim. Reported 27/27 mutations killed, with OMX equivalence
proven by **differential execution** (the literal old algorithms reimplemented and diffed over
dense sweeps: 956 + 239 + 685 poses, **0 differing joint outputs, 0 newly accepted**).

| # | defect | fix |
|---|---|---|
| **F1** | **A grasp with no room to approach from above was reported as SUCCESS.** The hover bisect reached 0 mm at the innermost/outermost rings, so `above_q == grasp_q` AND `lift_q == closed_q` — no descend, no lift-out; the arm slid SIDEWAYS into the object at grasp height, `check_grasp_held` read HELD (the jaws really were blocked, by an object being shoved) and „Würfel gegriffen." printed | refuse below `_MIN_APPROACH_CLEARANCE_M` = 2 mm = the bisect's own resolution (below it, "some clearance" and "none" are indistinguishable). Raised as **`GraspSkip`**, not `WorkflowError`, so a „Solange sichtbar" loop skips that cube; scoped OFF for `drop_at` |
| **F2** | „hebe an" after „Greife" gained **0 mm at every radius** on edu6 and emitted „Ziel liegt am Rand des Greifbereichs", which is flatly false mid-band | a ≤2 mm rise publishes nothing and logs the true cause; can never raise F1's refusal (a maxed-out lift is legitimate) |
| **F3** | the zone-inflation question | **NOT changed — escalated to Sven, §8.1** |
| **F4** | all four „Kein Greifziel" sites raised the same message for four different causes, telling a student who had done exactly those three things to do them; the old out-of-reach text told a **too-near** cube to move **nearer** | `find_object` records WHY; `_out_of_reach_reason` probes the real solver along the target's own bearing to distinguish too-near / too-far / wrong-bearing |
| **F5** | `SimPerception` caps each type at `len(recipe.tag_ids)` (2) and assigns ids by PLACEMENT ORDER — the caller's `tag_id` is ignored entirely. Its overflow warning went to `_logger` only, so a 3rd cube in plain view was told „Kein „Würfel" SICHTBAR" | one German `[WARNUNG]` through the same sink `ctx.log` uses, naming the placed count and the real capacity |
| **F6** | the clamped-approach warning fired on **100 %** of edu6 grasps (685/685) — the 60 mm hover is unreachable at every radius (max 50.6 mm at r = 0.120) | `_APPROACH_WARN_FRAC = 0.25`, **derived**: `0.25 × 0.060 = 0.015 = object_height − grasp_depth` on both shipped catalogs, i.e. "the hover no longer clears the object". edu6 685→66, OMX 55→7 |
| **F7** | `sim_held_floor_rad = 0.05` is inert on edu6 | kept + test-pinned (dropping the override would inherit the OMX −0.1: equally inert but a negative number on a gripper that never goes negative) |
| **F8** | `_set_follower_torque` discarded `res.message`, so the driver's specific German reason never reached the toast | stashed and appended at all five German-composing sites; OMX byte-identity proven against the literal pre-change string for `''`/whitespace/`None`/non-string |

### 5.2.2 Branch / stash audit — done 2026-07-26, nothing lost

Asked directly (“maybe there is a branch that was worked on in past sessions”) and checked end to end.

| checked | result |
|---|---|
| local branches | **only `main`.** The `roboter-studio-modernization` + “~10 local branches” that `CLAUDE.md` used to list for pruning are GONE — that item is closed |
| stashes | **none** |
| worktrees | one, clean |
| unpushed on `main` | **1 commit** — `5427a3be` (the place path). It goes out with this push |
| remote branches with commits “not in main” | 20+, and **all are the standard squash-merge false positive** — a squash-merged PR's commits are not ancestors of `main`, so `rev-list main..branch` counts them even though the CONTENT landed |

**Every unmerged remote branch was scanned** for files matching
`edu6|feetech|ik_solver|path_guard|handlers/motion|robot_profiles|trajectory_builder|sim_arm`.
**Exactly TWO hit**; the other 18 touch none of them (offline bundle, GHCR, installer,
hf-identity, cloud-training, self-update, accessibility, browser deploy, a v2.9.0 release tag).

**Hit 2 — `origin/feat/lerobot-v0.5.1-dataset-v3`, 3 commits.** 717 files, of which **702 are
vendored upstream LeRobot** — a tree main deliberately does NOT carry (Rule §5: LeRobot is
pip-installed from PyPI, never vendored or overlaid). The 15 files outside `lerobot/` are the
LeRobot 0.5.1 / dataset-v3.0 upgrade — **which SHIPPED**: main is pinned at 0.5.1 across all
four Dockerfiles + `modal_app.py`. And it edits
`docker/physical_ai_server/overlays/training_manager.py`, i.e. it predates main replacing the
overlay model with COPY-wholesale. **Definitively stale — nothing to salvage.** (It does hold a
local copy of LeRobot's `feetech/tables.py`, the file carrying the wrong baud table — but the
2026-07-26 research pass verified that four ways from upstream directly, so it is not needed.)

**Hit 1 — checked in detail** because its name overlaps this session's work:
`origin/claude/6dof-joint6-180-degrees-623iwv`, 4 commits, a PAST session attacking the same
problem — the encoder-seam limits, the OMX roll convention, the joint→roll conversion at the
“keep the current wrist” call sites, and a dispatcher test. We re-derived all of it from scratch
this session without knowing it existed. Verdict:

* its `_EDU6_JOINT_LIMITS_RAD` is **byte-identical to main's full ±180°** — despite commit
  `4d8dc2e3` claiming *“pull joint limits off the single-turn encoder seam (±178°)”*, the branch's
  final state carries the full range. So there is **no divergence** on the one thing that would
  have mattered, and our independent measurement (a trim converts 360° → 350°, because the clamp
  bounds the GOAL not the PATH) stands as the reason not to want one;
* `roll_from_joints` exists in main for **both** solvers — their fix is present, under a plural
  name (theirs was singular);
* main is a functional **SUPERSET**: the branch lacks 2,602 lines main has, including
  `tools/generate_edu6_mat.py` and `tools/classroom_kit_README.md`.

**Nothing to salvage. Do not merge it.** Convergent-derivation note worth keeping: two
independent sessions reached the same three fixes, which is mild evidence the diagnosis is right.
Pruning the remote branches is a `push --delete` — ask first.

### 5.3 Image rebuild — ✅ DONE and BYTE-VERIFIED (2026-07-26)

`docker-publish` rebuilt all three `:latest` images from `5edb7979` and the bytes were verified
straight from GHCR:

```
=== open-manipulator:latest ===
  revision label: 5edb79790cd9  MATCH
  OK   usr/local/bin/edu6_arm_node.py   31dbc542540428d0645bc6e5b97f7531…  119510 B
  OK   usr/local/bin/feetech_bus.py     241b1dc6bd3c79c86a3c2ad6ba25eb20…   24068 B
```

**The pre-powered gate is CLEARED.** The container now runs exactly the driver in git.

Re-run it after ANY future push that touches the driver — one command, anonymous GHCR token,
no multi-GB pull:

```bash
python tools/edu6_bench/verify_image_bytes.py            # compares against origin/main
python tools/edu6_bench/verify_image_bytes.py <git-ref>  # or an older image
```

> ⚠️ **A trap this tool itself fell into, fixed in `6cce3da0`.** It carried its baseline
> revision as a HARDCODED sha, so the first run after a push reported **MISMATCH on bytes that
> were correct** — the image genuinely carried the pushed hashes and the tool still failed,
> because its baseline named the previous commit. A stale baseline inside a verification tool is
> worse than no tool: it trains you to ignore the one thing that gates powered work. It now
> resolves `origin/main` at run time.

> ⚠️ **CRLF TRAP, still live for other files.** `.gitattributes` pins the two driver files
> `eol=lf`, so THEIR worktree hashes match the image — but `edu6_ik.py` is plain `text=auto`, so a
> Windows checkout materializes CRLF while the Linux CI build carries LF. **Take any expected hash
> from `git show HEAD:<path> | sha256sum`, never from `sha256sum <path>` on Windows.**

---

## 6. REMAINING WORK — end to end

**Everything from here is PHYSICAL.** The desk work is finished: code landed, CI green, images
rebuilt and byte-verified (§5.3). Start the next session at step 1 — nothing needs to be
re-derived first.

### Step 0 — sanity, 2 minutes
```bash
git -C C:/Users/svend/newaarm/Testre log -1 --oneline
python tools/edu6_bench/verify_image_bytes.py             # read the VERDICT line
```
**Go / no-go is the tool's own `VERDICT:` line, and its exit code.** GREEN → proceed. RED → stop:
at least one image is not provably built from the current source, so the guards below are
unverified.

> ⚠️ **Do NOT gate on the words "revision MATCH" as this file used to instruct.** That was itself
> the trap it warned about one level up. Images are rebuilt only when image-relevant paths change,
> so *every* docs-only or tools-only push leaves a perfectly current image advertising an older
> revision — and on 2026-07-27 that instruction produced a RED on a completely healthy stack, which
> is exactly how a safety gate gets trained out of usefulness. The tool now **judges** the label
> (`git diff <label>..<HEAD> -- <the paths that image is built from>`) instead of comparing shas,
> and it also covers `physical-ai-server` + `physical-ai-manager` at revision level — the workflow
> layer that R7/R8/R10 exercise, which the byte check never touched (it only ever covered the
> driver's 2 files, while session 7 changed ~8 more that ship in the server image). Those two are
> revision-only for principled reasons: the server image is flattened into ONE ~5 GB layer, and the
> manager ships a built JS bundle with no source bytes to compare against.

### Step 1 — A12: can a goal write energise a limp arm? — **SKIPPED (Sven, 2026-07-27)**

> **DECISION: not run, and the "A12 FIRST" framing below is WRONG — kept only for the reasoning.**
> This file said A12 gated hand-guide because *"hand-guide is torque-off by design and the student
> is holding the arm."* That was never checked against the code. It does not survive checking:
> `_trajectory_cb` REFUSES every trajectory while `not self._torque_on`
> („Trajektorie verworfen: Drehmoment ist aus"), the write loop calls `_write_targets` only under
> `self._torque_on`, and the WHOLE driver contains just four bus writes — of which only two are
> goal writes. **So nothing writes a goal to a limp arm during hand-guide at all**; the firmware
> has nothing to react to. Hand-guide is protected by that gate, not by A12's answer.
>
> The only goal write to a limp arm is the seed, issued ~1 ms before a DELIBERATE torque-on with
> `goal = present` at the gentle `SEED_SPEED_STEPS`. If A12 is true the arm energises a moment
> early, to the pose it is already in. The one case that mattered was `set_torque`'s bare
> `except`: seed lands → the `Torque_Enable` write RAISES → we return False and every caller
> reports the arm limp while it may be live. **That is now closed UNCONDITIONALLY** by a
> best-effort explicit de-energise on that path (+ German „12-V-Netzteil ausschalten" if the drop
> itself fails), so the hazard is gone whether or not A12 is ever measured. 11 regression tests.
>
> The tool below is kept and committed. Running it is now pure curiosity, not a gate.

**Do this before trusting hand-guide, and before R4/R7.** LeRobot #3585 reports the firmware
setting `Torque_Enable = 1` **by itself** on the next PID tick after an out-of-window goal write.
`_seed_goal_from_present_locked` writes the measured tick clamped only into the REGISTER range,
not the EEPROM window — so on a joint resting outside its window (R9 measured up to **107 ticks**
on joint5) that IS an out-of-window goal write, issued while torque is off.

Why it is first: **hand-guide is torque-off by design and the student is holding the arm.** If a
goal write can energise it, that assumption is wrong. The pull-in is already budgeted and
`SEED_SPEED_STEPS` rides the same packet, so it looks bounded — but "looks bounded" is not a
safety argument.

**Test:** torque off, hand a joint to rest outside its designed window (joint5 does this on its
own — R9), then issue the seed write alone and read `Torque_Enable` back. Watch for motion.

**Tool (2026-07-27): `tools/edu6_bench/edu6_a12_seed_selfenable.py`.** Written, adversarially
verified, six defects fixed. Bench order:

```bash
python tools/edu6_bench/edu6_a12_seed_selfenable.py --list            # read-only
python tools/edu6_bench/edu6_a12_seed_selfenable.py --joint 5 --park  # coach J5 out of its window
python tools/edu6_bench/edu6_a12_seed_selfenable.py --joint 5         # the test
python tools/edu6_bench/edu6_a12_seed_selfenable.py --joint 3
python tools/edu6_bench/edu6_a12_seed_selfenable.py --all-servos       # the driver's exact 7-servo packet
```

Exit **0** NOT REPRODUCED · **3** CONFIRMED · **2** INCONCLUSIVE · **1** bus error. **Anything
other than 0 or 3 means repeat — never record an INCONCLUSIVE as a pass.** It writes exactly two
things: the driver's byte-identical seed block at reg 41 (goal clamped ONLY into `[0,4095]` —
sanitising it would delete the experiment) and `Torque_Enable = 0` on every exit path. It never
writes 1 to register 40.

**J4 and J6 are refused by design** — their designed window IS the whole register, so the excursion
gate can never pass. That is the same definitional-zero trap R9 hit on exactly those two joints
(§2.4 result 2); a tool that "ran" on them would be measuring nothing.

> ⚠️ **The one residual, and it is the operator's job.** A slow sag of ~**1.4 ticks/s** passes the
> baseline gate and clears the 4-tick motion threshold inside a 3 s watch → a **false CONFIRMED**;
> so does a 25-tick knock on the target alone. `Present_Speed`/`Present_Load` are the right
> discriminators and are captured in the CSV, but they deliberately do NOT feed the verdict —
> the auto-abort can end the watch before a speed sample lands, and a guard that can suppress a
> true positive is worse than none. **So: a CONFIRMED with no register-40 hit must be checked
> against the CSV speed/load columns before you believe it** — a real self-enable pulls at
> ~200 steps/s (`SEED_SPEED_STEPS`) under load; a sag does neither. Run `--baseline-s 5` on any
> joint that creeps.

**Two free measurements — record both.** (1) The `[NOTE]` line naming which read path answered:
nothing in this tree has ever `sync_read` register 40 (the driver only writes it), so the tool
probes it once and falls back to individual reads. (2) The **register-42 readback value** — guard
4's entire clamp model rests on reg 42 returning the RAW written goal (§8.4 A9), never measured on
this arm. The tool accepts raw or clamped so it cannot false-alarm; the number itself is the datum.

### Step 2 — D1: does a position command clear the overload protection? — **SKIPPED (Sven, 2026-07-27)**

> **DECISION: not run. The framing below overstates it, and the correction is the reason.**
> „The servo current limits are the physical backstop" conflates TWO mechanisms and only one is at
> stake. **`Max_Torque` (150 gripper / 800 arm) is a continuous CAP on output — not a trip.** It
> cannot latch, nothing can clear it, and it is what actually bounds the force the arm applies.
> `Overload_Torque` is the SECONDARY „stop cooking yourself" trip, and D1 tests only that. So a
> FALSE verdict would not mean the arm pushes harder; it would mean the servo never gives up —
> a longevity risk on a sustained unattended stall, not a force-safety one.
>
> R4 and R7 also give DIRECT physical evidence (what force, damage or not), which decides those
> questions better than a register-level test. `Present_Load` already ships on
> `/joint_states.effort` if anyone wants to build a stall detector later.
>
> **The tool is fixed and committed anyway** — it had four defects that manufactured the headline
> `BACKSTOP FALSE` from a single dropped serial reply, plus a 16-tick (1.4°) mechanical slip read
> as a defeated protection. Committing it broken would have laid a trap for whoever picks up R7.
> Verified exhaustively after the fix: 752 states, FALSE reachable in 72, **zero** of them on
> unknown evidence. Reachability note below still stands if it is ever revived.

**Three decisions rest on the answer**, so it also comes before R4/R7: §8.1's zone-inflation
verdict, R4's pinch-force floor, and R7's stall-vs-damage question all assume *"the servo current
limits are the physical backstop"*. Our write loop sends a position command every **20 ms** — if
that clears the protection flag, it can never latch and the premise fails.

**Tool (2026-07-27): `tools/edu6_bench/edu6_d1_overload_latch.py`.** Adversarial verdict:
*"physically safe to run as-is, NOT safe to believe"* — the protection layer held under 14/14
mutations and torque reaches 0 on every exit path, but four evidence defects could manufacture the
headline **BACKSTOP FALSE** from a single dropped serial reply, and a **16-tick (1.4°) mechanical
slip** of the obstruction read as a defeated protection. Being fixed; do not run it before that
lands. Run `--dry-run` (opens no port) and then `--watch` (read-only) first.

**Asymmetry worth keeping in mind:** all 14 guards in that tool protect against manufacturing
`HOLDS`. There is no equivalent guard against manufacturing `FALSE` — which is exactly where every
critical defect lived. Treat a FALSE verdict with more suspicion than a HOLDS.

#### Target: the GRIPPER first (decision, Sven, 2026-07-27)

The gripper as provisioned **cannot reach overload** — and that is arithmetic, not opinion:

| register | gripper | consequence |
|---|---|---|
| `Max_Torque_Limit` (16) | **150** → output capped at **15 % PWM** | |
| `Overload_Torque` (36) | **80 %** | 15 % can never exceed 80 % → overload unreachable |
| `Protection_Current` (28) | 310 → **2015 mA** | 15 % duty into a locked rotor ≈ 0.4 A → unreachable |
| `Max_Temperature` (13) | 70 | a few watts for 3 s → unreachable |

**…IF** `Overload_Torque` is measured against FULL SCALE. If it is measured against
`Torque_Limit`, the gripper reaches 100 % of its own limit and **does** trip. The two candidate
mechanisms make OPPOSITE predictions here, which is exactly why the gripper run is not wasted: it
discriminates them at ~5 W with no arm mass, and shakes the tool down on real hardware before
anything heavier. **An INCONCLUSIVE here is itself a measurement** (⇒ full-scale-relative).

Escalation, only with that evidence in hand:
- **an arm joint** (`Max_Torque 800`) — overcurrent becomes genuinely reachable (~2.0–2.4 A vs the
  2015 mA trip); overload sits EXACTLY marginal (800/10 = 80 vs threshold 80). Carries the real
  thermal exposure: measured **30 s continuously at 2200 mA** with verdict `HOLDS` and no abort,
  because phases 3+4 both run once the bit latches.
- **temporarily raising the gripper's `Torque_Limit` (reg 48)** — RAM, volatile, restored from
  reg 16 at power-on, so provisioning stays byte-identical and a power cycle is the undo. Blocked
  today by the tool's own write allowlist (`{40, 41–47}`), and widening it to raise a torque limit
  in order to force a stall is a **Rule §2 decision — ask first.**

### Step 3 — the guard stack on real hardware

Three checks, all of which should now behave (the image carries them):

1. Park joint6 within a few ticks of ±180° by hand → „Umgebung starten": expect the German
   „am Rand des Positionsbereichs" refusal, **self-healing after a nudge** (the 5 s retry), and
   **no** refusal mid-range.
2. Same for joint2 near +180° / joint3 near −180° (the coverage the 2026-07-26 correction added).
3. **Jog PAST ±176.5° on joint4/joint6 and keep jogging** — a second jog from that pose must NOT
   refuse. That regression produced „Arm konnte wiederholt nicht verriegelt werden".

Then hand-guide → „Beenden" from an edge park: expect the refusal. **Guard 4 (the post-energise
abort) and guard 5 (the clamp) have NEVER fired on a real arm** — only the bench tool's
equivalent has.

Expected, not a fault: commanded J4/J6 now stop at **−168.75° / +168.66°** (guard 5, ticks
128…3967 — asymmetric by one tick, see §3.5), and a replay holding a
recorded value beyond that is trimmed by up to 11.3°.

### Step 4 — R5: loop rate, gains, overshoot

50 Hz through usbipd; optionally 100 Hz. **Read the position P/I/D gains — never read on this
arm**, not even present in the register map. Vendor default is P=32/D=32/I=0; LeRobot ships P=16
and measured the cost: P=16 → 63 ticks from target, P=32 → 26.7 ticks. So lowering P to cut
overshoot roughly **doubles** the static-friction dead-band.

**Also measure OVERSHOOT past a pinned goal at `GOAL_SPEED_CAP_STEPS`** (A10). That single number
turns guard 5's provisional **128** into a derived one: under ~20 ticks, 40–80 would do and the
replay trim shrinks to 3.6°; over ~100, revisit the ceiling and/or lower the speed cap (Q6).

### Step 5 — R4, R3, R7

- **R4** pinch force at `Max_Torque 150` → the final overcurrent floor. Readable live:
  `ros2 topic echo /joint_states --field effort`. **Depends on step 2.**
- **R3** cube seating → confirm the 0.1724 m fingertip tool length.
- **R7** self-collision: harmless stall or damage? **Decides three things** — the deferred jog
  swept-refusal guard (13/160 random in-limit configs self-collide), §8.1's zone inflation (does a
  ~34 mm gripper-housing overhang do damage?), and base-swing via poses, whose near radius (0.10)
  sits 10 mm outside `reach_inner_m` with nothing enforcing it server-side.

### Step 6 — J6 sign + `GRASP_ROLL`, then R8, R10

One observation settles both (§2.5): a wrong J6 sign rotates the jaws the wrong way against a
rotated cube. Free to flip — symmetric window, `.env` only, no re-provision.

**R8** 3D-twin browser smoke (π-yaw, finger animation, „Bahn" trail). **R10** end-to-end: scan →
Umgebung starten → calibrate (20-frame intrinsics → extrinsic → „Tisch vermessen") → Blockly
„finde Würfel → greife → lege ab" → „Bewegung aufnehmen" → replay → cloud save (auto-tagged
`edu6_studio`; cross-check an OMX rig refuses it). Sanity: only Roboter Studio + Inferenz tabs,
lone camera = Szenen-Kamera, jog shows Gelenk 1–6, second launch fast-rehydrates.

**Also reproduce A11 at R10:** hand-guide joint6 *through* ±180°, record, replay — expect a
deliberate ~337° wrist rotation over ~3.4 s. Pre-existing, shared with the OMX, fix is unwrapping
in `resegment_trajectory`. See it before touching it.

### Step 7 — physicals (Sven)

Print the mat via `tools/generate_edu6_mat.py` — **43.6 × 21.8 cm, does NOT fit A3** (42.0 cm long
edge) and the sheet forbids scaling: **A2 or two sheets.** It draws both rims: greifen to 20.8 cm,
ablegen only to 17.4 cm. The **AprilTag sheet needs no reprint** — ids (20, 21) at 24 mm are
identical to `omx_full`, so the same physical tags work on both stations.

### Step 8 — release 2.14.0

`gh auth login` first (stale — it blocks the installer proof). Bump **4 sites in one commit**:
`VERSION`, `installer/robotis_ai_setup.iss` AppVersion, `gui/app/constants.py`,
`pi_agent/constants.py` — `release.yml::version-preflight` hard-fails a tag that disagrees with
any of them. Prove the installer builds via `release-installer.yml` workflow_dispatch with the
**PREVIOUS** tag (the only `.iss` Pascal compile check in CI). Then `git tag v2.14.0 && git push
--tags` → W1–W6. Modal untouched; migrations 035/036 already applied.

### Step 9 — cleanup

Delete this file. Graduate anything durable left in it into `CLAUDE.md` first, and put the dated
story in `docs/CLAUDE-CHANGELOG.md`.

## 7. KNOWN-ACCEPTED (documented, no action)

- **J2/J3 detect only one of their two wrap landings** — their windows touch a register end, so
  one side is invisible to the boot band at *any* width. Covered by the torque-on edge guard
  (§3.2) which asks distance-to-REGISTER-edge instead. Whether they can be hand-parked at ±180°
  at all is still unmeasured — keep it in R6's sweeps to hard stops, as characterisation.
- **J4/J6 have no wrap DETECTION** (full-circle windows) — they have PREVENTION instead
  (§3.2/§3.3) plus behavioural mitigation (`q4 ≡ 0`, the J6 fold).
- **Jog, hand-guide and replay can still reach ±180°** on J4/J6. Replay is the one
  *block-programming* path that can, because it replays hand-recorded joints verbatim by design.
- **On edu6 a no-go zone protects the arm's LINKS but the gripper HOUSING can clip an obstacle
  by up to ~34 mm** — the inflation is 50 mm and the housing spreads 83.6 mm off the sampled
  centreline. Physical backstop is the servo current limits. **R7 decides; see §8.1.**
- **edu6 zone reroutes need drawn zones ≲ 9 mm tall** (`drawn_top + 50 mm inflation + 5 mm eps
  ≤ reachable cruise ≈ 63.8 mm`). Taller zones refuse. Lift-and-travel is dead for anything
  above ~1 cm and that is **physics, not a bug** — this arm genuinely cannot lift over a 3 cm
  cube. Base-swing (going AROUND) is the rung that matters and it works (64/144 vias).
- **`drop_at` reaches 34 mm less far than a grasp** (pick 208 mm, place 174 mm — 29 % of the
  band is pick-yes/place-no; only ~7 mm on the OMX). `_ik_precheck` solves the destination, not
  `destination + DROP_HEIGHT_M`, so there is no start-time warning. The mat now draws both rims.
- **The 60 mm hover over a 30 mm cube tops out ~50.6 mm** at the annulus sweet spot (the +110°
  relief minus the 12 mm-longer tool). `_solve_grasp_and_approach` bisects gracefully; a ~40 mm
  riser is the 1:1 physical lever if ever wanted.
- **`sim_arm._GRASP_CAPTURE_RADIUS_M` (0.06) spans edu6's entire radial band**, so sim reports
  HELD for a close up to 60 mm off target. Sim fidelity only — it carries no object identity, so
  it cannot grasp the WRONG object.
- **Blockly `WORKSPACE_BOUNDS_M` (±0.40)** is merely permissive for edu6; IK refuses anyway.
- **`edu6_arm_records` has no fetch-by-serial route yet** — the student-connect verify uses the
  servo EEPROM itself; the route lands with the first RMA need.
- **A split-path `move_above` clearance refusal inside „Solange sichtbar" is swallowed but the
  tag is not skipped** (`_skip_tag` lives in `perception_blocks`; `motion` importing it would be
  circular), so the loop ends via the existing 3-pass stall guard rather than cleanly.
  `grasp_object` — the Beginner-mode loop body — *does* skip.
- **Hand-guide is torque-off limp: the arm falls when released.** The student must hold it.
- **Windows/WSL2 only this release** — no `-opi`, no Jetson.

---

## 8. OPEN DECISIONS FOR SVEN

### 8.0 — ✅ RESOLVED 2026-07-26. Implemented as guard 5 (§3.5), default **128 ticks** after Sven challenged the 3.5° sizing. Kept below for the reasoning; see §3.4 item 1.

**The highest-value open item, and the one that gates the next powered session.** Six of twelve
joint-limit bounds sit exactly on the seam, so a J4/J6 command at ±π pins the goal at tick 4095
with the FAST speed installed, and one encoder sample across the boundary commits the joint to a
full revolution at 1.44 s/rev. Neither shipped guard fires. Reachable with no external force at
all: hand-guide J6 past +180°, record, replay.

**Proposed fix:** clamp commanded ticks to `[margin, TICKS − 1 − margin]` for the full-circle
joints inside `rad_to_tick` — the same class of clamp as the existing `max(0, min(4095, ·))`
that is already there.

| | |
|---|---|
| **Cost** | 3.5° of jog/replay travel at each end of J4 and J6 only. Autonomous grasping is unaffected (`q4 ≡ 0`, J6 folded to ±90°, and the measured closest approach to a register edge over 977,808 real solutions is 256 ticks = 6.4× the margin). |
| **Why it is your call** | It **reduces reachable commanded range**, and it is a software guard on the command path — Rule §2. |
| **If declined** | The operational rule in §2.3 has to carry it: never hand-guide J4/J6 past ±180°, and never replay a recording made after doing so. That is a procedure, not a guard, and the recording path makes it easy to violate weeks later. |

### 8.1 The no-go-zone inflation — both directions are harmful (Rule §2)

`LINK_RADIUS_M + ZONE_MARGIN_M = 0.05 m` per face. It **looks** like an over-conservative
OMX value that a 0.21 m-reach arm should scale down. It is the opposite.

Measured against the edu6 collision meshes (URDF + STLs, 326 configurations: HOME + 175
strict-vertical grasp poses + 150 random in-limit), the max distance from any **moving**-link
mesh vertex to the nearest `link_points` sample — exactly the inflation the Minkowski "fatten
the obstacle, test link centres" argument requires:

```
link6         83.6 mm   ← the 80×117×67 mm wrist/gripper housing, whose body
                          spreads far off the tool-axis centreline that is sampled
link2         64.7 mm      link3  48.7 mm      link4  43.3 mm
link5         38.7 mm      link1  37.9 mm
fingers       44.1 mm at the 1.75 rad command cap (46.6 mm at the URDF limit)
                        ← after the two breaches, the figure CLOSEST to the 50 mm total
End_effector  17.0 mm
```

So the guard is already **33.6 mm UNDER-covered on the gripper housing**. Shrinking it widens a
real gap on the very link that carries the student's object into the obstacle.

**Raising it to 83.6 mm is equally wrong:** a 10 mm drawn box would inflate to 177 mm across a
measured pick band of 176 mm (r ∈ [0.0325, 0.2082]) — every in-band zone would refuse the whole
band, i.e. the feature disappears, and a Sperrzone nobody can draw guards nothing.

**Left unchanged, deliberately, with the consequence recorded** (§7). Both directions are
Rule §2-class changes to a collision margin. **R7 is where this gets a verdict** — does a
~34 mm housing overhang produce damage or a harmless overload stall? The real fix is for
`edu6_ik.link_points` to sample link6's girth instead of only its axis (a solver change).

### 8.2 The continuous wrong-way watchdog (Rule §2) — assessed, not implemented

The only thing that would see **a joint forced across the edge while already torqued** (§3.4
residual 1). Assessment for whoever picks it up:

- **Warranted?** Yes on the merits, and it is also the *fastest* case: a joint torqued and
  holding at tick 4095 (a jog to J6 = +π clamps there) that a hand forces to tick 2 gets a naive
  error of ~4093 and, with the write loop active, runs at 4.36 rad/s = **1.44 s/rev**.
- **Cost:** no extra bus traffic at all (`_read_tick` already reads position; the goal is
  host-side state the write loop sets), ~30 lines, ~8 tests. Needs the last commanded goal
  shared thread-safely between the write and read loops.
- **False positives:** structurally unlikely — the write loop interpolates in radians and
  `rad_to_tick` clamps into [0, 4095], so the instantaneous goal is always within a few ticks of
  `present`; a −170°→+170° move walks tick 114 → 3982 monotonically.
- **Why it is a bigger Rule §2 step:** it can stop motion **MID-TRAJECTORY**, and the response is
  an open design question. Dropping torque collapses a backdriving arm; **re-seeding
  `goal = present`** stops the joint in place and keeps the arm up, which is almost certainly
  better but is a software guard that **MODIFIES commands**. That wants a bench decision, not a
  desk one.
- A watchdog that read `Goal_Position` back (rather than trusting host-side state) would
  additionally close the lost-seed residual (§3.4 residual 2).

### 8.3 Two OMX tickets — parked by explicit direction

Both are on the **shared OMX path**, i.e. arms already in students' hands, so they need their own
decision rather than a drive-by patch. Analysis is ready; neither has been touched. **Parked
because the focus is edu6** (Sven, session 7).

| id | item |
|---|---|
| **Q5b / A2** | staleness gate on `communicator.get_latest_follower_joints` — it returns the last cached message forever, so a workflow can seed motion from a frozen-but-plausible pose |
| **Q8 / A3** | OMX `dxl11` (Operating Mode 4) and `dxl16` (Mode 5) zero a volatile turn counter at power-on, and `entrypoint_omx.sh` has only a soft-fail *post-hoc* verifier — no pre-move plausibility check on the measured start pose. Same failure class as §2.2, on arms already shipped. **Highest-value open item.** |

### 8.4 Smaller open items

| id | item | when |
|---|---|---|
| A4 | fuse the free-running read/write threads into one absolute-deadline loop (dominant motion jitter) | after R5 |
| A6 | `sync_read` per-reply budget instead of one 160 ms deadline shared across 7 | after R5 |
| A7 | jog swept self-collision guard | R7 decides |
| A8 | **`ci.yml::german-strings-lint` does not scan `robotis_ai_setup/docker/open_manipulator/`** — it covers `physical_ai_server/`, `gui/`, `installer/`, `pi_agent/`. So every German string in the edu6 driver is CI-**unguarded**. Clean today (verified by running the same grep over the file by hand), but nothing enforces it. Cheap fix, but it edits CI config → ask first | before release |
| A11 | **A hand-guide sweep THROUGH the wrap records a 360° jump, and replay honours it.** Consecutive samples store +3.14 → −3.14 rad (the encoder wraps mod 4096), and `resegment_trajectory` has NO shortest-path unwrapping, so it stretches that 1.6° hand motion through the velocity floor into a deliberate **~337° wrist rotation over ~3.4 s** — which rotates a held object 337° with it. **Pre-existing recording-fidelity defect, NOT caused by guard 5** (the clamp makes it marginally shorter, never longer). The fix is unwrapping consecutive samples in `resegment_trajectory`; it is a SHARED path, so OMX-affecting. Reproduce it at R10 before fixing | R7/R10 |
| A10 | **R5 must also measure OVERSHOOT past a pinned goal** when a trajectory is truncated at `GOAL_SPEED_CAP_STEPS`, plus the never-read position P/I/D gains — that measurement is what turns guard 5's provisional 128 into a derived number | R5 |
| A9 | **SETTLED 2026-07-26, and the earlier note was BACKWARDS.** LeRobot #3585 (open, single reporter, SO-101) shows register 42 reads back the **RAW** written goal — verbatim: *"The `Goal_Position` register itself is left at the value the host wrote … the substitution is invisible above the bus"*, with a repro writing goal 228 into a `[2045, 3919]` window, reading 42 back as 228, and measuring ~3472. So **computing the EEPROM clamp is the CORRECT model** for guard 4, and reading 42 back would model the error WRONGLY. It survives only as a separate "did my seed `sync_write` land?" check — and NOT at register 67, which the current official table lists as `无定义` and V3.7 omits entirely (66 → 69); the plausible candidate is register **71** (LeRobot's read-only `Goal_Position_2`), also unverified | closed / bench |
| **A12** | ⚠️ **NEW, and it touches the hand-guide safety assumption.** #3585 also reports the firmware setting `Torque_Enable = 1` **BY ITSELF** on the next PID tick after an out-of-window goal write, regardless of host torque-off. `_seed_goal_from_present_locked` writes the measured tick clamped only into the REGISTER range, not the EEPROM window — so on a joint resting outside its window (R9 measured up to 107 ticks) that IS an out-of-window goal write issued while torque is off. The pull-in is already budgeted and `SEED_SPEED_STEPS` rides the same packet, so it looks bounded — but **"a goal write can energise a limp arm" contradicts the torque-off-is-safe assumption hand-guide rests on**, and hand-guide is where the student is holding the arm. Documented, NOT acted on. Rule §2 + bench | **R5, before hand-guide is trusted** |
| **A13** | **`sync_read` corruption is load-dependent and firmware-AGNOSTIC, not a 3.9 bug** (LeRobot PR #3888; its originating report #3131 was Hiwonder hardware). The accepted upstream fix is bounded read retries (`max_read_retry=3`); our `READ_FAIL_STOP_S` latch approximates it at ~1 s. A bounded per-read retry in `_read_tick` is a read-path change ⇒ Rule §2 | R5 |
| Q6 | lower `GOAL_SPEED_CAP_STEPS` (edu6 4.36 vs OMX ~1.2 rad/s, on the arm with `collision_enabled=False`) | after R5/R6, Rule §2 |
| Q4 | servo swap on the assembled chain — see §9.3 | harness design, before first ship |

---

## 9. DEFERRED IDEAS — analysis kept intact

### 9.1 Make URDF zero the HOME pose (Sven's idea, deferred by Sven)

Wanted: the pose the arm holds with every joint at 0 in the URDF should BE the HOME pose (the
boot-home target and the „Grundstellung" block).

**Possible, but it trades away detection quality, and the conflict is geometric rather than a
tuning problem:**

| | current HOME (0, 0.70, −2.40, 0, 0.70, 0) | URDF zero |
|---|---|---|
| fingertip | (0.050, 0, **0.455**) | (0.228, 0, **0.130**) |
| arm x-span | −0.043 … +0.050 | −0.073 … **+0.228** |
| link samples in the grasp band below 200 mm | **0 of 49** | **10 of 49** |
| J2/J3 distance from their EEPROM clamp edge | 456 ticks | **0 — both sit ON it** |

Current HOME is folded **UPRIGHT** (tucked over the base, clear of everything). URDF zero is
folded **FORWARD-FLAT**, lying across the region where cubes go (world x 0.11–0.23, never
rising above 130 mm).

Measured: keeping the zero SHAPE and merely lifting it does not help — J2=+0.2/J3=−0.2 through
J2=+0.7/J3=−0.7 all still leave 10–11 of 49 samples in the band. Clearing the workspace
REQUIRES folding the elbow far more than the shoulder rises (J2=+0.35/J3=−1.20 → 0/49), which
is no longer the zero shape.

Costs to accept if we do it:
- **scene-camera occlusion** — the camera looks steeply down at exactly that region, so
  „Grundstellung → finde Würfel" would detect with the arm in frame;
- **`observe_pose_joints` becomes MANDATORY** for edu6. It is currently absent from ArmProfile,
  so `motion._observe_joints` falls back to HOME — the „solange … sichtbar" loop would retreat
  INTO the camera view instead of out of it, exactly inverting that pose's purpose;
- **J2 and J3 would rest ON their EEPROM clamp edges** (both tick 2048, J2's minimum and J3's
  maximum), so gravity presses one against its clamp for as long as the environment is up.

Not a blocker (checked): the all-zero "unseeded pose" sentinel
(`motion._require_seeded_start_pose`, tolerance 1e-6) does NOT trip, because the gripper stays
OPEN at 1.75 so the `all()` test is False.

Sites to change together: `edu6_arm_node.HOME_JOINTS_RAD`, `robot_profiles._EDU6_HOME_JOINTS_RAD`
(a no-drift test locks them equal), a new explicit `observe_pose_joints`, `safe_home_arm_rad`,
and the kit doc.

**Middle option if the goal is just "tidier than today":** J2=+0.35 / J3=−1.20 → fingertip
386 mm instead of 480 mm, 0/49 in band, 228 ticks off the clamps.

### 9.2 Do NOT drive the arm to URDF zero (measured)

All-zeros is a **jig** pose, not a drivable target:

1. **The tightest non-adjacent link pair is 39.7 mm CENTRELINE** (forearm vs upper arm) vs
   67.4 mm at HOME — and that is centreline, not surface. path_guard models a 30 mm link
   half-girth and the mesh clearance at the inner bound is 27.6 mm, so this may be a few mm of
   real clearance or it may be contact. **That is exactly R7, still untested.**
2. **J2 and J3 sit EXACTLY on their EEPROM window edges** at zero (tick 2048 is J2's min and
   J3's max). No margin: any residual offset error has the servo pulling against its clamp.
3. **Gripper 0 is fully closed against its own hard stop** — a sustained stall at
   `Max_Torque 150` with nothing between the jaws. Exclude the gripper from any such move.
4. **Nothing in the product drives to zero.** The only routes are the incremental per-joint jog
   or a raw `/leader/joint_trajectory` publish.

If zero must ever be reached under power: hand-guide toward it **limp** and watch whether the
meshes touch (that answers R7 for this pose for free), gripper excluded, one joint at a time at
`SEED_SPEED_STEPS`, hand on the 12 V. `tools/edu6_bench/edu6_goto_zero.py` carries a
link-clearance pre-flight for exactly this — note that a **distal-first drive order would put
links 14 mm below the table**; elbow-first gives 67.4 mm.

### 9.3 Q4 — can one servo be swapped on the assembled chain? (analysis; the harness decision is Sven's)

Two independent problems, and the second is the expensive one.

**(a) The ID.** `edu6_provision.py` sets **neither ID nor baud**, so it assumes 1..7 @ 1 Mbps
already. Servos ship as **ID 1**, so a replacement plugged into the assembled chain puts TWO
devices on ID 1: every command to ID 1 hits both and a READ gets two simultaneous replies.
**In-place re-ID is therefore impossible** — not awkward, impossible. Two routes:
- **Preferred: set the ID on the bench BEFORE fitting**, with the replacement ALONE on the
  adapter. Then it is a drop-in, and no harness change is needed.
- If a fitted servo must be re-ID'd, the harness must let you unplug it from BOTH neighbours and
  connect the adapter directly. **Harness requirement: every servo's two bus ports reachable
  without disassembling the joint**, plus a short programming pigtail in the kit.

**(b) The `Homing_Offset` — the real cost.** The offset is derived from the **jig** pose, so a
replacement has no valid offset for its joint and there is no way to compute one without the
jig. The tool has **no single-servo mode**, so a swap today means re-jig the whole arm and
re-run provisioning (idempotent, read-compare-write, so a full re-run is safe).

**Fail-safe already in place:** a replaced-but-unprovisioned servo **cannot** silently ship — its
EEPROM windows will not match `position_limit_window`, so the driver's boot probe refuses in
German and names the tool. A forgotten step is loud, not silent.

**Recommendation:** ship spares **pre-ID'd per joint position** (labelled "J5" etc.), document
"a servo swap = re-jig + re-run provisioning", and treat a limits-only tool mode as a later
convenience (it cannot remove the jig requirement, because the offset is jig-derived).

---

## 10. BENCH TOOLS

Live in **`tools/edu6_bench/`**. **They are COMMITTED** — landed in `5edb7979`, so the earlier
"untracked, a decision for Sven" note is resolved and gone. (`edu6_goto.py`'s own docstring still
says "throwaway, never committed"; that line is stale, harmless, and fixed when next touched.)

The four hardware tools import the repo's `feetech_bus` and **AST-extract every constant from
`edu6_arm_node.py`** rather than duplicating it, so they cannot drift from the driver.

| tool | what it does |
|---|---|
| `edu6_goto.py` | drives the arm to a pose, with a **link-clearance pre-flight** and live `Present_Current` logging. **This is what closes R4.** Its wrap guard refuses a goal within 30 ticks of the map edge. ⚠️ It carries its **OWN local, UNCLAMPED `rad_to_tick`** and writes goals directly, so it does NOT inherit guard 5 — an operator bench run can still pin a goal on the seam. It is the tool that MEASURED the defect, so that is by design, but treat its 30-tick guard as the only protection on that path |
| `edu6_goto_zero.py` | the URDF-zero variant, with the §9.2 clearance checks |
| `edu6_r9_collapse.py` | **READ-ONLY** collapse probe (`write`/`sync_write` replaced with raisers; refuses to run if any servo reports `Torque_Enable = 1`). Produced §2.4 |
| `edu6_wrap_test.py` | the cross-seam test with auto-abort. Produced §2.3. See the methodology trap in §11 |
| `verify_image_bytes.py` | anonymous GHCR token → manifest → small COPY layer; byte-verifies the shipped driver without a multi-GB pull, **judges** each image's revision label against the paths that image is built from, and covers all three images (server/manager at revision level — see §6 Step 0). Ends in a single `VERDICT: GREEN/RED` line; exit code matches |
| `verify_urdf.py` | vendor URDF ↔ baked solver constants ↔ mesh sha256 cross-check |
| `verify_ik_contracts.py` | edu6 + OMX solver contract checks (`roll_from_joints` round-trip, fold bounds, OMX identity) |

The ~60 other scratchpad scripts (`m*.py`, `v*.py`, `w*.py`, `slice*.py`, `mutate*.py`,
`girth*.py`, …) are genuinely throwaway one-off analysis and mutation harnesses. Not copied.

---

## 11. TRAPS & LESSONS — read before the next bench session

**Measurement methodology**

- **Never describe a roll direction as "CCW sighting along the link".** It silently inverts if
  the operator stands at the other end. Two early J4/J6 sign readings were wrong from exactly
  that phrasing. Use descriptions that cannot invert: **"the fingertip moves LEFT/RIGHT"**,
  **"looking down at the table"**, **"which way does the tool point"**.
- **A zero-error hold proves nothing about the wrap.** Two wrap-test runs showed a "clean hold"
  near the edge; both had `goal == present`. A cross-seam test must verify that the naive error
  actually spans the edge — `|naive| > 2048` — before the result means anything.
- **Read the position AFTER the operator prompt, not before.** A stale goal read before the
  "ready?" prompt produced one false alarm (and, ironically, one real finding).
- **`Torque_Enable` must be read BACK.** A roll joint needs no torque to stay put, so
  "nothing moved" is vacuous without proof the arm was energised.
- **Do not cite a definitional zero as evidence.** J4/J6 measured "0 excursion outside their
  window" in R9 — their window is the whole register, so 0 would print for any pose whatsoever.
- **Redirect the bytecode cache when mutation-testing.** A mutation with the same byte length as
  the original (`0.06`→`0.18`) hit a stale `.pyc` and silently poisoned a run.
- **NEVER use `git checkout -- <file>` to undo a mutation.** It restores from the INDEX, i.e.
  HEAD — so on an uncommitted working tree it deletes the whole session's work in that file, not
  just the mutation. This happened in session 7 and wiped 340 lines of `motion.py`; it was
  recoverable only because a `git diff` of that file happened to be lying in the scratchpad
  (`motion.diff`), and the restored file's sha256 matched the pinned pre-mutation one exactly.
  **Restore a mutation by the inverse text substitution, and sha256-compare before and after.**
  Corollary: while a session's work is uncommitted, commit early or keep a `git diff` snapshot —
  the recovery path here was luck, not design.
- **A mutation harness moves mtimes, which reads as third-party tampering.** Session 7 lost time
  investigating a `motion.py` mtime jump that was simply a verifier's own harness restoring the
  file. If two agents work in one tree, expect this and compare **content hashes**, never mtimes.

**Reasoning traps that produced real bugs this session**

- **Verifying a leaf does not verify the module.** A green `segment_blocked` check actively
  created confidence that path_guard was covered; both of session 6's defects were in the code
  that *calls* it (the reroute ladder was 0/144 dead on edu6, and `plan_safe_route` had the roll
  mirror). **Test the call sites, not just the accessors** — accessor-only assertions still pass
  if the consumption sites keep reading the module constants.
- **A stub that returns a constant makes whole branches unreachable.** `_FakeIK6.fk` returned
  `(0.2, 0.0, 0.1)` for every input and had **no `solve()` at all**, so the Cartesian jog path
  could not even RUN against it. The wrist-mirror bug was not overlooked — its branch was
  unreachable by the suite. `_FakeIK6` is deliberately KEPT in exactly one place
  (`test_path_guard_segment_blocked_slices_n_joints`) where asserting the width IS the point.
- **A clamp bounds the GOAL, not the PATH.** This killed both the "trimmed windows are immune"
  premise and the `full_circle_joint_indexes` predicate built on it (§3.2).
- **Check which code path a bound applies to.** The legitimate torque-error bound is 400 ticks at
  BOOT but 2008 on the SERVICE path, because no band runs there. Sizing against the wrong one
  would have shipped a false abort (§3.3).
- **An "over-conservative OMX constant" may be under-conservative on the smaller arm.** The zone
  inflation looked like an obvious per-profile shrink; measurement showed it is already 33.6 mm
  short (§8.1).

---

## 12. Q&A LEDGER

| # | question | status |
|---|---|---|
| Q1 | Can J4/J6 physically be turned to ±180° by hand? | ✅ **YES** — J6 swept −178.1°→+179.7°, J4 accumulated 227° (§2.5) |
| Q1b | What does the servo loop do AT the ±180° `Present` wrap? | ✅ **NOT wrap-aware — it drives the LONG way** (§2.3). Guards 3 + 4 ship |
| Q1c | J6 sign | open — merges into R6's `GRASP_ROLL` jaw check (§2.5) |
| Q2 | J6 fold: fixed ±90° or nearest-twin-to-seed? | ✅ **fixed ±90°.** Nearest-seed was implemented, measured and reverted (§4.3) |
| Q3 | Boot `Present_Position` plausibility check? | ✅ yes, retrying, band 400 (§3.1) |
| Q4 | Servo swap on the assembled chain? | ✅ **analysed** (§9.3); harness decision is Sven's |
| Q5 | Read-loop robustness gap? | ✅ fixed — whole loop body guarded, fault latched |
| Q5b | Staleness gate on `get_latest_follower_joints` | **parked** — shared OMX path (§8.3) |
| Q6 | Lower `GOAL_SPEED_CAP_STEPS`? | after R5/R6, Rule §2 (§8.4) |
| Q7 | Test 100 Hz? | at R5 |
| Q8 | OMX `dxl11`/`dxl16` power-on turn counter | **parked** — shared OMX path (§8.3) |
| — | **Clamp commanded ticks off the ±180° seam?** | ✅ **RESOLVED — shipped as guard 5 at 128 ticks** (§3.5, §8.0) |
| — | Zone inflation direction | **open, needs Sven** (§8.1) — R7 gives the verdict |
| — | Continuous wrong-way watchdog | **open, needs Sven** (§8.2) |
| — | Promote `tools/edu6_bench/`? | ✅ **RESOLVED — committed in `5edb7979`** (§10) |
| — | HOME = URDF zero? | **deferred by Sven** (§9.1) |
