# Owner decisions on the three specs (2026-11-25)

This file records every answer to the questions the three spec architects raised, plus the questions the conductor decided under the standing rule "decide residuals yourself, ask only for ask-first items and the release go".
It sits beside `PLAN.md` and `VERIFICATION.md` §7 and, like them, overrides the research reports and CLAUDE.md for everything downstream.
The specs fold these answers in; where a spec and this file disagree, this file wins until the spec is corrected.

Specs: `SPEC-A-phase0-wsl-field-fix.md`, `SPEC-B-proofs-firmware-armlink.md` (+ appendices `protocol`, `proofs`), `SPEC-C-native-app.md` (+ six appendices).
Architects: A and B on Opus 5.5, C on Fable 5.2, all at effort xhigh. Spec C was written now, ahead of PLAN.md §8's "after spec B's Phase 1 results" (owner, 2026-11-25). Every B-dependent decision in C carries an `externalGates` entry.

## Spec A — Phase 0, release 2.41.3 (was 2.41.1, then 2.41.2; see Q1-A, Q1-B)

| # | Question | Answer | By |
|---|---|---|---|
| Q1 | What does 2.41.1 contain besides Phase 0? | *(Superseded by Q1-A.)* Ship both: Phase 0 plus the arm-check stillness fix (`autocal/service.py::_rest_window`, in the tree uncommitted on 2026-11-25). The fix must be on `main` before the `v2.41.1` tag. The arm-check part is proven only on the first kit (RG-10). | owner |
| Q2 | If S0-3 shows a standard user cannot import per user | Ship the safe part (A1, A5, A6, A8, A7) now and park A2–A4. Data loss must not wait. | owner |
| Q3 | How does `release/2.41` ship after the cut? | A maintenance path: CI also on `release/**`, plus a `release.yml` maintenance mode, decided by git ancestry, that skips W1/W2/W2b/W3/trainer for tags not on `main` (spec A WP-A8, on `main` before the `v2.41.1` tag). This pre-approves exactly that CI edit. | owner |
| Q4 | W6 also force-updates every Pi (same source, ~6 GB) | Accept; W6 unchanged. | owner |
| Q1-A | Amends Q1 (2026-11-26): ship the arm-check fix first? | **Yes, alone.** 2.41.1 = the arm-check stillness fix and the version bump only, through the normal `main` pipeline. Phase 0 follows as **2.41.2**; WP-A8 must be on `main` before the `v2.41.2` tag, and `release/2.41` is cut from `v2.41.2`. In `PLAN.md`, `VERIFICATION.md` and specs A–C every „2.41.1" that names Phase 0's release or the cut reads „2.41.2", and every „2.41.0" that names the release before Phase 0 reads „2.41.1". Cost accepted: a second hardware test round and a second ~6 GB pull on every Pi. | owner |
| Q1-B | Amends Q1-A (2026-11-28): a field fix before Phase 0? | **Yes, alone.** The first RG-1 attempt found the „Turm-Kamera“ probe refusing a working camera; 2.41.2 = that fix (the verifier's start-up allowance, the MSMF format read-back with the decoded fallback, the probe's precise reasons) and the version bump only, through the normal `main` pipeline. Phase 0 follows as **2.41.3**: every „2.41.2“ Q1-A gave Phase 0 reads „2.41.3“, and every release Q1-A names before Phase 0 reads „2.41.2“. | owner |

## Spec B — proofs, firmware, arm link

| # | Question | Answer | By |
|---|---|---|---|
| Q-B1 | CI for `native/` | No `ci.yml` edit now. Tests run inside today's `python-tests` job; firmware builds and long fuzz runs stay local or on spike branches. The owner is asked again before the first pilot firmware. | conductor |
| Q-B2 | Leader servos `Max_Torque_Limit = 0`? | Yes: the leader is structurally limp. | owner |
| Q-B3 | Who holds the firmware release-signing key and the provisioning-authority key? | The owner, offline on an encrypted medium; signing and provisioning done locally. No CI secret. | owner |
| Q-B4 | Spend for the OMX train-and-run baseline | **Skip it.** No paid OMX training or Cloud-Inferenz trials. The V1's train-and-run success bar is set by the owner's judgement at the trial gate. The OMX teleop-lag baseline (no spend) stays. This amends PLAN.md §5's "Pass bars" for the train-and-run gate. | owner |

## Spec C — the native app

| # | Question | Answer | By |
|---|---|---|---|
| Q-C1 | Does `main` stop building and tagging the WSL product after the cut? | Yes. WSL fixes ship only from `release/2.41` through A8's maintenance pipeline. The cloud API and the teacher web keep deploying from `main`. | owner |
| Q-C2 | Installer technology | Inno Setup for both channels from one script; MSI only if a pilot school's IT asks. | conductor |
| Q-C3 | What does "data import from WSL" bring over? | **No copy, upload first.** There is no import: schools upload their datasets to Hugging Face before switching. The S14 spike and every tar-export piece are dropped. | owner |
| Q-C4 | Update-manifest signature | Ed25519, key created and held by the owner offline, signed by hand. No CI secret. | owner |
| Q-C6 | Carry `EDUBOTICS_REQUIRE_ACTIVATION=0` (arm homes at start) into the native app? | Not carried. Nothing moves until a student activates the robot. | owner |
| Q-C7 | Maintenance (factory reset, scene-camera profile) from an iPad session | Teacher or admin only, as on the Pi (SA-12). | conductor |
| Q-C8 | CI for the native product on `main` | Same as Q-B1: spike-branch proof runs now; the owner is asked again before C4-12. | conductor |
| Q-C9 | Where the cloud-host build for iPad Safari lives | A new Railway service the owner creates. | owner |
| Q-C10 | When the GPL licence review (x264 in PyAV, the JDK) happens | Before any non-pilot distribution. | owner |
| Q-C12 | Delete `pi_agent/` from `main` with the WSL stack | Deferred to the removal gate (OWNER-GO-REMOVAL on C7-06). | conductor |
| Q-C13 | Between "main stops tagging" and the first native release nothing can update the RunPod trainer image | **A cloud-only release mode on `main`** (owner chose it after seeing the pros and cons): a tag runs W1, the trainer image and its Railway variable, W2 and W3, and skips W4, W5 and W5b. It is replaced by the native mode in C7-01. This pre-approves exactly that CI edit. | owner |
| Q-C1 scope | What Q-C1 approves | Exactly C4-00's workflow edits: `docker-publish.yml` loses its push trigger, and `release.yml` gains the cloud-only mode (Q-C13), composed with A8 without editing A8's lines. | conductor |
| M18 follow-up | A `setpoint_lost` latch (or `armlink_gone`) during a Cloud-Inferenz run | Ends the cloud run through the one idempotent `_end_inference` with a German sentence. It never continues silently and never clears the latch on its own; the student must activate again. This is a new Cloud-Inferenz end rule (Rule §2): **confirmed by the owner 2026-11-25**, which closes spec C's K20. | owner |
| C4-00 details | How the cloud-only mode is built | A committed marker `.github/release-mode` (`cloud-only`, later `native`), read after A8's `mode` step; anything missing or unknown fails closed. `VERSION` on `main` becomes `3.0.0`; cloud-only tags are `v3.0.N` and never collide with `v2.41.x`. A cloud-only tag creates no GitHub Release, so W6 refuses it. | conductor (spec C) |

## Seam points settled between B and C (2026-11-25, spec B §8.5–§8.8)

- **M4, heartbeat:** a 20 Hz timer in its own MutuallyExclusive group on the main executor. It is sent only while the executor dispatches it AND the sensor loop's stamp is under 0.25 s old. Watchdog 1.0 s = 20 periods.
- **M2, `traj`:** follows the driver's `_trajectory_cb` acceptance rule (t0 > 0 and non-increasing times accepted; non-finite values, unknown names and empty messages dropped with the running trajectory kept). Spec C delivers `native/tests/fixtures/rail_messages_v1.json`.
- **M9, clocks:** `ClockMap` stays unchanged and is fed the arm link's arrival stamp plus `board_us`. Lifting `MAX_LAG_S` alone is not enough beyond `WINDOW_S`.
- **M14, rail fixture:** a non-empty JSON array; each entry has exactly five keys. It must contain at least one message over 60 points, one single-point message and one torque-off message.
- **M18, setpoint gaps:** SETPOINT has no stream watchdog. It holds its last goal through any gap while heartbeats arrive and latches 1.0 s after heartbeats stop. TELEOP keeps its own watchdog on the relay's setpoints.
- **M19, non-blocking client:** `heartbeat()` and `setpoint()` never block and never raise (latest-wins slot; code 69 `armlink_gone`). Request methods wait at most 2.0 s.
- **M20, pilot firmware:** the repo holds only `native/firmware/releases/<n>/SIGNED-MANIFEST.json`. The owner uploads the binary to the public release channel. `firmware.pin` is `none` or the build number, and the build verifies with B's `fwcheck`.
- **M6, moved driver:** `native/protocol/reference_paths.py` is B's single path source. C7-06 carries the provisioning tool's `sys.path` and the driver tests' `_DOCKER` paths.

## Facts found while specifying (not decisions)

- A concurrent session's uncommitted fix (2026-11-25) shows that 2.41.0's arm check can never prove rest on a real clock, so no kit can be calibrated on 2.41.0. Hence Q1.
- The executed PowerShell tests run locally in the existing `review-pwsh-own:local` image (57 tests, 0 skipped); CI stays authoritative.
- CLAUDE.md's "V1 boot probe pings id 7" is wrong: only the scan prober does (spec B C1).
