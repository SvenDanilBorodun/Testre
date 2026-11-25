# SPEC B — Proofs, firmware and the arm link (PLAN Phases 1–3)

Date 2026-11-25. Architect B. Base `main` @ `66382d27`. Proposed build branch `feat/native-b-firmware-armlink`
(from `66382d27`); Windows proof runs on `spike/*` branches (§3.4). Read with its two appendices:

- `SPEC-B-appendix-protocol.md` — the board protocol, the kernel state machine, the executor, every guard
  with its vector family, the codes and their German sentences.
- `SPEC-B-appendix-proofs.md` — every bench script (B1–B13, RG-H1, the VR gates, LEADER-LIMP, the OMX teleop-lag
  baseline) and every Windows spike (S1–S5, S7–S13, S16, S-J): what it measures, the harness, the record, the proposed
  pass bar, who runs it, and the decided fix for every failure mode.

Decisions file: `PLAN.md` + `VERIFICATION.md` §7 + `SPEC-DECISIONS.md`. Where this spec disagrees with `FINDINGS.md`,
`BRIEF.md`, a research report or `CLAUDE.md`, the plan and the code win, and the disagreement is listed in §11. The
owner's answers of 2026-11-25 to Q-B1 … Q-B4 are folded in (§1.1, §13); so is Q-C3's consequence for this spec (the
S14 WSL-export spike is dropped: there is no data import).

Claim labels: **CONFIRMED** = observed by a probe listed in §12; **PLAUSIBLE** = argued, not observed;
**ASSUMPTION** = taken as given, must be proven by the named gate.

---

## 0. What this spec delivers, in one paragraph

A frozen, generated board protocol (one schema → C structs + Python codec + shared vectors); a firmware kernel
for the Waveshare Bus Servo Driver HAT (A) that boots read-only, adopts a held arm, runs a 100 Hz loop with a
time-stamped waypoint executor and a stop generator, and carries **every guard of today's V1 driver**, proven
equal to the Python driver by full-domain vectors plus mutants; a signed update path with rollback and no eFuses;
a gated provisioning passthrough; a Python bus simulator and a soft board (the C kernel through `ctypes`) so most
of it is proven in CI; the **arm-link process** (both COM ports, the read-only leader reader, the ~100 Hz teleop
relay, the sync-glide hook, the engine-liveness gate) with a typed IPC; the new firmware guards (runaway,
tracking, gravity-compensated collision stop, bench-only damping); and all Phase-1 proofs — the bench scripts with
records and proposed bars, the OMX teleop-lag baseline, and the 14 Windows spikes with a harness that runs as a
standard user on GitHub and on the owner's locked-down PC. There is no OMX train-and-run baseline: the V1's
train-and-run success bar is the owner's judgement at the trial gate (Q-B4). Spec C consumes the **seam contract** in §8.

---

## 1. Decisions taken by this architect (not owner forks; each with its reason)

| # | Decision | Why |
|---|---|---|
| D-B1 | All new code under one new top-level `native/` (`protocol/`, `firmware/`, `armlink/`, `devboard/`, `bench/`, `spikes/`). | Additive, no existing path filter of W2/W3/W4 matches it (§12 P17), and Phase 7 can tell the native product from the WSL product at a glance. Spec C adds its own siblings (`native/engine/`, …). |
| D-B2 | Every test of `native/` lives in `robotis_ai_setup/tests/test_native_*.py` (sys.path insertion, like `test_feetech_bus.py`). C is compiled by those tests with `cc`. | `ci.yml::python-tests` already discovers that folder, so CI proves `native/` **without editing `ci.yml`** (an ask-first item, §9). Settled by Q-B1 (2026-11-25): no `ci.yml` edit now; firmware container builds and long fuzz runs stay local or on `spike/*` branches; the owner is asked again at gate `OWNER-FW-CI` (D-B18). |
| D-B3 | ESP-IDF **v5.5.6**, image `espressif/idf@sha256:bf72fe7a15c22849a9a94147301165a545fb8905d97c176cf748e3e4614cac84` (index; amd64 `sha256:851f136a…cfb3`, arm64 `sha256:26776964…c112`). | Probed here: builds for `esp32`, signed-apps-without-secure-boot (ECDSA scheme) + rollback resolve, `CONFIG_ESP32_REV_MIN=0`, the image is bit-identical across two build paths, no radio code linked (§12 P14). v6.x moves to mbedTLS 4/PSA and Picolibc (§12 P15) — unprobed; migrate before v5.5's end of support (PLAUSIBLE ≈ 2028-01 by Espressif's 30-month rule). |
| D-B4 | Host link 2 000 000 baud 8N1 on ESP32 UART0 (CH343P "C2"); servo bus 1 000 000 baud on UART1, GPIO19 TX / GPIO18 RX. | 80 MHz APB / 2 Mbaud is an exact divisor; STATE (≈ 92 B framed × 100 Hz) uses < 5 % of it; S13c measures latency and may lower it. |
| D-B5 | Loop 100 Hz (driver: 50 Hz). Follower read every cycle = SYNC_READ(56, 6 B) of ids 1–6; health round-robin one servo per cycle (READ 62..70 + READ 40); write the 7-byte block at 41 only while executing/teleop/setpoint/relaxing. | Re-derived bus cost 1.58–2.06 ms (+ the health reads: 23 B + 15 B on the wire ≈ 0.46–0.62 ms with t_turn 20–100 µs) of a 10 ms cycle (§12 P6). The 50 Hz cap was a usbipd/WSL artefact (`feetech_arm_node.py` docstring). |
| D-B6 | The board's TRAJ default is **today's replace semantics** (`JUMP`: the whole queue replaced, the first point applied at once, clock restarted at receipt), with `FROM_CURRENT` splice and `APPEND` as opt-in flags. | Phase 4 (engine step 1) must reproduce today's motion; the chunk-boundary-bump fix is a measured opt-in (VR12), not a silent behaviour change. |
| D-B7 | The command clamp `[m, 4095−m]` is applied **per cycle to the interpolated goal**, and the arm link **subdivides** a segment at the radian-limit crossing before converting. | Clamping only the waypoints differs from today's per-sample clamp by up to **157 ticks** on band-crossing segments; in-band the integer interpolation differs by ≤ **1 tick** (§12 P10, CONFIRMED). |
| D-B8 | No per-cycle step-cap refusal of TRAJ chunks on the board (research 02 proposed one). `Goal_Speed` 2840 + `Acceleration` 50 stay the hardware second limit, written with every goal, exactly as today. | Parity: today's driver refuses no fast trajectory. A new refusal is a new Rule §2 guard nobody asked for. Trigger to revisit: VR12/B5 shows the registers insufficient. |
| D-B9 | No per-rig joint-sign override in the native stack. The V1 design's signs (all +1 until VR3 says otherwise) are compiled into `native/protocol/arm_spec.py`; a different direction convention is a new arm spec plus re-provisioning. | Today's env knob already "requires re-provisioning" (`feetech_arm_node._parse_signs` comment); there is no `.env` in the native product. |
| D-B10 | No wire switch disables a guard. Today's one-variable rollbacks (`EDUBOTICS_FEETECH_*` = 0) become build-variant constants; the shipped `prod` variant has every guard on. `CONFIG` is tighten-only. | A wire-disableable guard is a guard a bug can disable. The app never downgrades firmware (owner Q25), so a guard that false-refuses is fixed by a roll-forward release. |
| D-B11 | Three build variants: `prod` (pilot/school), `bench` (diagnostics, register-40 value 2 for B8, never shipped), `diag` (Phase-1 read-only diagnostics). HELLO_ACK reports the variant; the arm link refuses to operate `bench`/`diag` firmware outside `--bench` mode (`bench_firmware`). | Bench capabilities must never reach a classroom. |
| D-B12 | Board signature scheme = ESP-IDF "signed apps without secure boot", ECDSA P-256 (the V1 scheme, any chip revision), verified on update **and** at boot; app rollback on; **no** anti-rollback/secure-boot/flash-encryption option (they burn eFuses). Never-downgrade is a soft NVS counter. The verification key is compiled into the **bootloader and the app** (§12 P30), so every image that boots on a HAT — `diag` and `bench` included — carries the owner's signature, and the release key cannot be rotated over the air (a rotation is a USB re-flash, §10). | Owner Q4 (no eFuses); scheme resolves for ESP32 rev ≥ 0 (§12 P14). |
| D-B13 | Provisioning passthrough is unlocked by an ECDSA P-256 signature over a board challenge, verified against a compiled **provisioning-authority public key**; the student app has no code path to it (AST fence). The private key is the owner's, offline (Q-B3). | Owner Q6 ("only an EduBotics tool"), enforceable without trusting the app. |
| D-B14 | The arm link never synthesises a board HEARTBEAT; it relays one per engine heartbeat (20 Hz, sent by the engine only while its executor dispatches and its sensor loop is fresh, §8.5), and **only HEARTBEATs feed the board's link watchdog** (appendix protocol §2, §5.5). When the engine is silent ≥ 1.0 s the arm link stops the teleop relay, drops engine setpoints and reports `engine_silent`; it sends no STOP of its own — the board's heartbeat watchdog latches, once and deterministically. | Otherwise a dead or blind engine behind a live arm link — or an executor that still streams chunks or queued actions with a dead sensor loop — would keep the board's watchdog fed (§12 P34). |
| D-B15 | Shared vectors cover each guard's **full input domain** and every legal threshold boundary, and each guard WP compiles listed mutants that must fail. | A 1/16-grid vector set **missed** a `>=`-for-`>` mutant of the wrong-way rule; the full-domain set catches it (§12 P8–P9). |
| D-B16 | The German sentences for every board and arm-link code live in `native/protocol/edubotics_boardproto/texts_de.py` (spec B owns them); sentences whose meaning is unchanged are byte-equal to today's driver strings (lockstep test). | PLAN: German stays on the PC; one table next to the codes. |
| D-B17 | The leader's read-only bus class has no write method at all (PING/READ/SYNC_READ only), AST-fenced. | Owner: "The PC never writes to the leader bus." Structural, not a convention. |
| D-B18 | The CI question of Q-B1 is a **script refusal**, not a note: `sign_release.sh --channel pilot` (the one way a `prod` image becomes a pilot firmware, the artefact spec C embeds) exits 3 until `native/bench/signoffs/OWNER-FW-CI-<n>.json` records the owner's answer (any answer), and unless the image's `BUILD-MANIFEST.json` shows the two checks CI does not run: two identical builds and a passed radio map check. | Q-B1: "before the first pilot firmware the owner is asked again"; a gate that the release step itself enforces cannot be skipped by forgetting it, and the same step is the one place the unfenced release checks (§6.2) are enforced. |
| D-B19 | Signing never moves a secret: `sign_release.sh` signs with the **host's** `openssl dgst -sha256 -sign <key on the medium>` (the owner's passphrase prompt), converts DER → raw r‖s with a stdlib script, and only the public key and the raw signature enter the pinned container, where `espsecure.py sign_data --version 1 --pub-key … --signature …` assembles and `verify_signature` checks the result. The provisioning tool signs the passthrough challenge the same way (host `openssl`). | Q-B3 (owner-held offline, signed locally, no CI secret). `espsecure`'s own `--keyfile` refuses a passphrase-protected PEM, while its pre-calculated-signature path accepts an external signature that then verifies (§12 P29); so the key file may stay passphrase-protected inside the encrypted medium and never enters Docker's file sharing. |
| D-B20 | The leader is **structurally limp**: provisioned `Max_Torque_Limit = LEADER_MAX_TORQUE = 0` on ids 11–16 (claw included), a compile-time literal like the follower's `ARM_MAX_TORQUE`, never a command-line value; the arm link checks registers 16 = 0, 48 (RAM `Torque_Limit`) = 0 and 40 = 0 read-only before every PREPARE. | Q-B2 = (a). Register 16 is copied into RAM register 48 at power-on (`feetech_bus.py` comment, marked "R2-verify": ASSUMPTION until the LEADER-LIMP bench, appendix proofs §2). |

### 1.1 Owner answers folded in (2026-11-25, `SPEC-DECISIONS.md`)

| # | Answer | Folded into |
|---|---|---|
| Q-B1 (conductor) | No `ci.yml` edit now; `native/` tests run in today's `python-tests` through `robotis_ai_setup/tests`; firmware builds and long fuzz runs stay local / spike-only; the owner is asked again before the first pilot firmware | D-B2, D-B18, release gate `OWNER-FW-CI` (asked when B18 merges; B10's script refusal enforces it), §5, §6.2, §7, §8.1, §8.4, §9 |
| Q-B2 = (a) (owner) | Leader servos provisioned with `Max_Torque_Limit = 0`: structurally limp | D-B20, B07 acceptance 3, B11, B13 acceptance 6, §6.1, §6.2, §8.4, §9, appendix protocol §4, §6.2, §6.5, §9, appendix proofs LEADER-LIMP and §4.1 |
| Q-B3 = (a) (owner) | The owner holds the firmware release-signing key and the provisioning-authority key offline on an encrypted medium and signs and provisions locally; no CI secret. The app's update-manifest key (spec C, C7-02, Q-C4) is held the same way | D-B12, D-B13, D-B19, gate `OWNER-KEYS` (B03), B03/B10/B11 (`native/KEYS.md`, `sign_release.sh`, `--authority-key`), §5, §6.1, §7, §8.1, §8.4, §9, §10, appendix protocol §3.5, §8, appendix proofs HIL, P7 |
| Q-B4 = skip (owner) | No OMX train-and-run baseline (no paid training, no Cloud-Inferenz trials); the V1's train-and-run success bar is the owner's judgement at the trial gate. The OMX teleop-lag baseline stays | §0, B02, B17, §5, §7, §9, appendix proofs §2 |
| Q-C3 (owner, spec C) | No data import from WSL | the S14 spike is dropped from B04 and appendix proofs §3.3 |

---

## 2. Architecture (what B builds, and where)

```
native/
  protocol/                      ONE source of the wire
    bp_schema.py                 message tables (board family 0x01–0x7F, IPC family 0x80–0xBF), codes, versions
    arm_spec.py                  V1 follower + interim leader: ids, names, limits, signs, windows, models,
                                 safety-EEPROM values, guard constants (lockstep with the driver's literals)
    render.py                    renders every generated artifact below; `--check` fails on drift
    reference_paths.py           the ONE place naming today's driver/tool/ClockMap files read by B's tests (§8.8)
    edubotics_boardproto/        Python package (stdlib only; shipped in the app)
      frame.py                   COBS + CRC-32 framing, bounded stream decoder (hand-written)
      _generated.py              codec (generated)          codes.py (generated)     arm_spec_gen.py (generated)
      texts_de.py                German sentence per code (hand-written, linted)
      convert.py                 rad<->tick, the radian clamp with subdivision (lockstep with the driver)
      reference/                 Python reference of executor, stop generator, watchdogs, collision (vectors' oracle)
      fwcheck.py                 verify a signed pilot image against its SIGNED-MANIFEST.json (§8.11; C7-01 calls it)
    vectors/                     codec_v0.json, guards/*.json, executor/*.json, collision/*.json (generated)
    fuzz/RUNS.md                 libFuzzer run log (command, execs, result)
  firmware/
    core/                        portable C11 kernel, NO ESP-IDF include (fenced)
      generated/                 bp_msgs.{h,c}, bp_codes.h, bp_arm_spec.h (generated)
      bp_frame.{h,c} fbus.{h,c} guards.{h,c} executor.{h,c} kernel.{h,c} watchdog.{h,c} health.{h,c}
      collision.{h,c} gravity.{h,c} dfu.{h,c} passthrough.{h,c} bp_hal.h
    host/                        host build: vector runner, unit tests, trace replay, libbpkernel (for ctypes),
                                 fuzz targets + run_fuzz_container.sh
    hat_a/                       ESP-IDF v5.5.6 project: main/, hal_esp32.c, partitions.csv, sdkconfig.defaults.<variant>,
                                 build.sh (digest-pinned container, --check-repro, --map-check, --check-key,
                                 writes BUILD-MANIFEST.json), sign_release.sh (owner-run on the host, D-B19),
                                 sig_der_to_raw.py (stdlib)
    keys/                        release_pub.pem, provision_authority_pub.pem (PUBLIC only, handed over by the owner)
  devboard/edubotics_devboard/   dev/CI only, never shipped: feetech_sim.py, softboard.py (ctypes), fakes
  armlink/edubotics_armlink/     shipped in the app: transport, discovery, board_client, rail (§8.6), leader_bus,
                                 leader, mapping, teleop, ipc, client (engine-side API), dfu, __main__
  bench/                         bench-only: lib/ (bench_arm, bench_bus, record), scripts B1–B13, RG-H1, VR, OMX-LAG,
                                 leader_limp_check, schema/, records/, signoffs/ (incl. OWNER-FW-CI), README.md (runbook)
  spikes/                        windows/ harness + spike scripts, github/windows-spikes.yml (NOT a workflow on main),
                                 schoolpc/, results/, DECISIONS.md
  KEYS.md                        key custody runbook: the three owner-held keys (firmware release, provisioning
                                 authority, spec C's app update manifest), generation, storage, use, rotation, loss
  README.md
```

Process view (Windows product; spec C owns the host/engine boxes):

```
engine (spec C) ──IPC v1 (multiprocessing.connection, AF_PIPE/AF_UNIX, authkey)──► arm-link process (spec B)
                                                                                     ├─ follower COM: board protocol ─► HAT (A) firmware ─► ids 1–6
                                                                                     └─ leader COM: Feetech, READ-ONLY ─► plain adapter ─► ids 11–16
```

---

## 3. Work packages

Notation: paths are repo-relative. "Full regression" = `cd robotis_ai_setup && python3 -m unittest discover -s tests`
whose result must have **no failure or error beyond the environment baseline** of the machine (§10: here 10 errors,
all `No module named 'yaml'` (9) or a gitignored foreign scratch directory (1)); CI's `python-tests` is the real gate.
Every new `.py/.c/.h/.sh/.ps1` carries the EduBotics proprietary header (`CLAUDE.md` licensing paragraph).
No WP edits `CLAUDE.md` except B19. **`files.mustNot` common to every WP** (`COMMON_MUSTNOT`):
`robotis_ai_setup/docker/**`, `physical_ai_tools/**`, `open_manipulator/**`, `.github/**`, `robotis_ai_setup/gui/**`,
`robotis_ai_setup/installer/**`, `robotis_ai_setup/wsl_rootfs/**`, `robotis_ai_setup/pi_agent/**`,
`robotis_ai_setup/cloud_training_api/**`, `robotis_ai_setup/supabase/**`, `robotis_ai_setup/modal_*/**`,
`robotis_ai_setup/runpod_training/**`, `classroom_kit/**`, `VERSION`, `CLAUDE.md`, `docs/**` (B19 excepted).
`tools/**` is in `COMMON_MUSTNOT` except for B11.

### Phase 1 — proofs

#### B01 — Board protocol: one schema, generated codecs, framing, vectors, fuzz harness
- **summary.** Create `native/` (README, `.gitignore` entries for `native/**/build/`, `native/firmware/hat_a/sdkconfig*`
  (except `sdkconfig.defaults*`), `managed_components/`, `*.pem` except `native/firmware/keys/*_pub.pem`, host
  binaries). Write `native/protocol/bp_schema.py` (the tables of `SPEC-B-appendix-protocol.md` §2–§4 as data: message
  id, family, direction, version, fields with C type and Python type, fixed vs extensible length, codes with name and
  sentence key) and `render.py`, which writes `native/firmware/core/generated/{bp_msgs.h,bp_msgs.c,bp_codes.h}`,
  `native/protocol/edubotics_boardproto/{_generated.py,codes.py}` and `native/protocol/vectors/codec_v0.json`, each
  with a "GENERATED — do not edit" notice; `render.py --check` exits non-zero on any drift. Hand-write
  `edubotics_boardproto/frame.py` and `native/firmware/core/bp_frame.{h,c}` (COBS, CRC-32/ISO-HDLC table-driven,
  bounded stream decoder per appendix §1). Build helper `native/firmware/host/Makefile` (targets `vectors`, `unit`,
  `fuzz`) and `native/firmware/host/fuzz/fuzz_frame.c` + `run_fuzz_container.sh` (Ubuntu 24.04 container, apt
  `clang`, `-fsanitize=fuzzer,address,undefined`). Extend `robotis_ai_setup/tests/test_licence_headers.py` so `ROOTS`
  includes `native` and `EXTENSIONS` includes `.c` (coverage extension only). Create
  `native/protocol/reference_paths.py` (§8.8): the one place that names today's reference files (`DRIVER_NODE`,
  `DRIVER_BUS`, `DRIVER_GEOMETRY`, `PROVISION_TOOL`, `CAPTURE_TIMELINE`, repo-relative); every later B test, generator
  and bench tool imports it.
- **dependsOn** []. **externalGates** [].
- **layers** native-protocol (new), native-firmware/core (new), tests, repo-config (.gitignore).
- **files.may** `native/README.md`, `native/protocol/**`, `native/firmware/core/bp_frame.*`,
  `native/firmware/core/generated/**`, `native/firmware/host/**`, `.gitignore`,
  `robotis_ai_setup/tests/test_native_protocol_*.py`, `robotis_ai_setup/tests/test_native_reference_paths.py`,
  `robotis_ai_setup/tests/test_licence_headers.py`.
  **files.mustNot** `COMMON_MUSTNOT`, `tools/**`.
- **testCommands**
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_protocol_*.py' -v`
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_licence_headers.py' -v`
  - `python3 native/protocol/render.py --check`
  - `native/firmware/host/fuzz/run_fuzz_container.sh frame 10000000` (local, Docker; not CI)
  - full regression.
- **acceptance**
  1. `render.py --check` exits 0; `test_native_protocol_generated.py::GeneratedFilesAreCurrent` byte-compares every
     rendered artifact with the checked-in file.
  2. `test_native_protocol_codec.py::CodecVectors::test_c_and_python_agree`: the Python codec round-trips every case
     of `codec_v0.json` (encode(fields) == hex, decode(hex) == fields) and the C runner built by the test with
     `cc -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined` prints `0 mismatches` for the same file.
     When `GITHUB_ACTIONS` is set and `cc` is missing, the test FAILS (never skips); locally a missing `cc` skips
     with a printed reason.
  3. `test_native_protocol_frame.py::ProbeFrame::test_probe_is_not_a_feetech_packet`: `frame.encode_wire(PROBE)`
     equals exactly `00 03 01 01 01 02 04 09 45 44 42 50 7d a4 df f2 00`, contains no `0xFF`, and
     `feetech_bus.FeetechBus` fed these bytes through a fake serial raises `FeetechBusError` from `_read_status`.
  4. `crc32(b"123456789") == 0xCBF43926` in both codecs; CRC/COBS vectors include empty, 1-byte, 254/255/256-byte runs
     of non-zero bytes and all-zero payloads.
  5. Robustness: 10⁶ seeded random and mutated streams through the Python decoder and through the sanitised C decoder
     raise nothing but the codec's own reject path, and every accepted frame re-encodes to the identical bytes.
  6. A libFuzzer run of ≥ 10⁷ executions on `fuzz_frame` (frame + every message decoder) finished without a finding,
     recorded in `native/protocol/fuzz/RUNS.md` (command, clang version, execs, corpus size, result).
  7. `test_licence_headers.py` scans `native/` and `.c` (today no tracked `.c` exists under its roots, so the extension
     breaks nothing — CONFIRMED by `git ls-files`); because that test only judges files that carry a header, a new
     `test_native_protocol_headers.py::EveryNativeSourceHasTheProprietaryHeader` requires the two-line proprietary
     header on every `.py/.c/.h/.sh/.ps1` under `native/` (generated files included — `render.py` writes it; `build/`
     directories skipped like `SKIP_DIRS`).
  8. `test_native_reference_paths.py`: every path in `reference_paths.py` exists; no other module under `native/` or
     `robotis_ai_setup/tests/test_native_*.py` contains the string `feetech_arm_node.py`, `feetech_bus.py`,
     `feetech_geometry.py`, `feetech_provision.py` or `capture_timeline.py` outside an import of `reference_paths` (AST
     string-constant scan), so spec C's later move of the driver (C7-06) is one edit.
- **failing test** `robotis_ai_setup/tests/test_native_protocol_frame.py::ProbeFrame::test_probe_is_not_a_feetech_packet`
  — input: the `PROBE` message of the schema; asserts the golden bytes above and the Feetech parser's refusal. Fails
  today: `native/protocol/edubotics_boardproto` does not exist. The golden bytes were computed for the pinned layout
  and checked to contain no `0xFF` (§12 P12).
- **risk** high (a new wire contract). **lenses** correctness, delivery, regression.

#### B02 — Bench toolkit, the Phase-1 benches on today's V1, and the OMX teleop-lag baseline
- **summary.** Build `native/bench/` (bench-only; never imported by `edubotics_armlink`/`edubotics_boardproto`).
  `lib/driver_ast.py` loads today's driver functions and constants from
  `robotis_ai_setup/docker/open_manipulator/feetech_arm_node.py` exactly the way `test_feetech_bus.py::_load_node_functions`
  does (read-only; env knobs popped; the driver file is never edited). `lib/bench_arm.py` composes them into a
  bench driver over `feetech_bus.FeetechBus`: `probe()` = the driver's `probe_bus`; `energise()` = the same
  read → edge guard → seed → `Torque_Enable 1` → post-energise verify sequence as `FeetechArmNode.set_torque(True)`
  (built by binding the extracted methods onto a stub, the `_TorqueStubNode` pattern, never re-typed); `glide_home()`
  = `build_boot_home` + `interpolate_trajectory` + `rad_to_command_tick` at 50 Hz, preceded by
  `boot_home_floor_decision`; `hold()`; `health()` (READ 62..70 + READ 40 per servo); `torque_off()`.
  `lib/bench_bus.py` subclasses `FeetechBus` and allows register-40 value 2 only when constructed with
  `allow_damping=True` and only through `write_damping(sid, torque_limit)`; value 128 stays refused. `lib/record.py`
  writes and validates the record schema `native/bench/schema/bench-record-1.json` (appendix proofs §1). Scripts per
  appendix proofs §2: `rg_h1_hold_home.py`, `b2_power.py`, `b7_goal_write_autotorque.py`, `b9_register_dump.py`,
  `vr_checks.py`, `teleop_lag.py` (the analyzer shared with B17), `omx/record_teleop_pair.py` (rclpy, run inside the
  WSL product's `physical_ai_server` container). Owner runbook `native/bench/README.md` (German-free; English bench
  instructions with safety notes: hand near the 12 V switch, never under the arm). **No OMX train-and-run baseline is
  built** (owner Q-B4 = skip, 2026-11-25: no paid training, no Cloud-Inferenz trials): there is no script, record or
  gate for it in this spec. The V1's train-and-run success bar is **set by the owner's judgement at the trial gate**
  (spec C's `TRIAL-PASSED`), not derived from a measured OMX rate; this amends PLAN §5's "Pass bars" for that gate
  only. The OMX teleop-lag baseline (no spend) stays.
- **dependsOn** []. **externalGates** owner bench sessions with the pilot V1 on its plain adapter: RG-H1, B2a–d, B7
  (single loose servo), B9 (incl. esptool chip info), VR1/VR3/VR4/VR9/VR11/VR16 records, VR5–VR8/VR10/VR12–VR15 under
  today's WSL product; the OMX teleop-lag baseline (OMX-LAG); owner approval of every proposed bar (appendix proofs §2).
- **layers** native-bench (new, bench only), tests.
- **files.may** `native/bench/**`, `robotis_ai_setup/tests/test_native_bench_*.py`, `native/README.md`.
  **files.mustNot** `COMMON_MUSTNOT`, `tools/**`, `native/armlink/**`, `native/protocol/edubotics_boardproto/**`.
- **testCommands**
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_bench_*.py' -v`
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_feetech_*.py'` (driver untouched: today
    `Ran 324 … OK`, §12 P2)
  - full regression.
- **acceptance**
  1. `test_native_bench_arm.py::EnergiseMatchesDriver::test_same_bytes_as_driver_set_torque`: for 12 simulated arms
     (fake serial replies: all in-window; joint 3 at tick 4060 (edge); joint 2 at 2047 outside window; a servo silent
     on the post-energise read; a wrong-way present), the packet list `bench_arm.energise()` writes equals byte for
     byte the list the driver's extracted `set_torque(True)` writes on the same replies, and the refusal kind equals.
  2. `test_native_bench_bus.py`: value 2 refused without `allow_damping`; 128 refused always; `write_damping` exists
     only on `BenchBus`.
  3. `test_native_bench_fences.py::ProductNeverImportsBench`: no module under `native/armlink`, `native/protocol`,
     `native/firmware` imports `native.bench`/`bench` (AST).
  4. `teleop_lag.py` recovers a synthetic 37 ms lag within ±1 ms on 5 joints with noise (seeded) and reports per-joint
     lag, median, p95.
  5. Every script validates its output against the schema before writing; a record with a missing raw-file hash is
     refused.
  6. External: the records listed in externalGates exist under `native/bench/records/phase1/` with
     `verdict_by_script` and, for each bar, an owner sign-off file in `native/bench/signoffs/` (appendix proofs §1.3).
- **failing test** `test_native_bench_arm.py::EnergiseMatchesDriver::test_same_bytes_as_driver_set_torque` (input
  above; fails today: module absent).
- **risk** high (bench code energises and moves a real arm). **lenses** correctness, delivery, regression.

#### B03 — HAT (A) firmware skeleton and the diagnostic build; B1 bus timing, B3 port-open reset
- **summary.** ESP-IDF project `native/firmware/hat_a/` on the pinned image (D-B3): `CMakeLists.txt` with
  `idf_build_set_property(MINIMAL_BUILD ON)`, `partitions.csv` (nvs 0x6000, otadata 0x2000, phy_init 0x1000,
  ota_0 0x20000 + 0x1E0000, ota_1 0x1E0000; partition table at 0x10000, 4 MB flash ASSUMPTION until B9 reads the
  flash ID), `sdkconfig.defaults` + `sdkconfig.defaults.{prod,bench,diag}` (appendix protocol §8),
  `build.sh --variant {prod|bench|diag} [--check-repro] [--map-check]` (runs `idf.py build` twice in two different
  container paths and compares sha256 with `--check-repro`; with `--map-check` greps the map file and fails on any
  linked section from `libnet80211|libpp|libcoexist|libbtdm|libwpa|libphy` for `prod`/`diag`). HAL `hal_esp32.c`
  (UART0 host 2 Mbaud, UART1 bus 1 Mbaud pins 19/18, `esp_timer` 100 Hz ISR notify, bus task pinned core 1 at
  `configMAX_PRIORITIES-2`, link task core 0, RTC_NOINIT boot counter, `esp_reset_reason`, chip revision, MAC). The
  bootstrap subset of the protocol (PROBE/PROBE_ACK, HELLO/HELLO_ACK, HEARTBEAT, ERROR, LOG) and the read-only boot
  probe report in HELLO_ACK (pings 1–16, model, fingerprint registers, Torque_Enable). Diag-only commands
  `DIAG_BUS_TIMING` (shapes: SR(56,6)×{6}, SR(56,15), READ(62,9)+READ(40,1), SW(41,1 B) of the value just read back),
  `DIAG_ECHO`. Bench host scripts `native/bench/b1_bus_timing.py`, `b3_port_open_reset.py` (also spike S13b),
  `native/bench/s13c_echo_latency.py`. Flashing runbook in `native/bench/README.md` (owner; host `esptool==4.12.0`,
  the version in the pinned image, §12 P14; every image is signed with `sign_release.sh` before it is flashed; that an
  unsigned one is not booted is PLAUSIBLE from §12 P30 and proven by the HIL checklist).
  **Signing and key custody (Q-B3; every image that boots on a HAT needs the owner's signature, D-B12, §12 P30).**
  `build.sh` compiles `native/firmware/keys/release_pub.pem` as `CONFIG_SECURE_BOOT_VERIFICATION_KEY` (and fails with
  a clear message while that file is absent), `--check-key` finds that PEM's text in both `bootloader.bin` and the app,
  and every build writes `build/<variant>/BUILD-MANIFEST.json` (variant, app/bootloader sha256, `build_number`, commit,
  `repro` = both builds identical or `not_checked`, `map_check` = `pass`/`fail`/`not_checked`, `key` = sha256 of the
  compiled public key, `scratch_key` true/false). Until the owner's public key is committed, proof builds use
  `--scratch-key` (a throwaway public key in a temp directory outside the repo, its private half never written; such
  an image is never flashed and `sign_release.sh` refuses it). `native/firmware/hat_a/sign_release.sh --channel bench
  --image <unsigned.bin> --key <path>` (D-B19; `bench` covers `diag` and `bench` images and the P7 update images):
  run by the owner on the host with the encrypted medium mounted; exits 2 before reading anything when `CI` or
  `GITHUB_ACTIONS` is set, when the key path resolves (symlinks followed) inside the git work tree, or when it is
  missing; signs with `openssl dgst -sha256 -sign "$KEY"` (the PEM may be passphrase-protected: openssl prompts),
  converts with `sig_der_to_raw.py` (stdlib DER parser → 64-byte r‖s), assembles and verifies in the pinned container
  with the committed public key only (`espsecure.py sign_data --version 1 --pub-key keys/release_pub.pem --signature
  sig.raw`, then `verify_signature`), and writes `SIGNED-MANIFEST.json` beside the output (unsigned and signed sha256,
  `build_number`, variant, channel, commit, key fingerprint). It logs "key: (outside the repository)", never the
  path, copies nothing, and its temp files (`umask 077`) hold only the digest and the signature.
  `native/KEYS.md` (the custody runbook, English): the three owner-held keys — the firmware release key and the
  provisioning-authority key (ECDSA P-256, this spec) and the app update-manifest key (Ed25519, spec C C7-02, gate
  `OWNER-UPDATE-KEY`; one section reserved for spec C's signing procedure); generated by the owner on his own machine
  with the network off (`openssl ecparam -name prime256v1 -genkey -noout | openssl ec -aes256 -out
  <medium>/<name>.pem`), kept on an encrypted medium that is mounted only while signing or provisioning (a second
  encrypted copy stored apart is recommended; the owner decides); never in the repository, a CI secret, a cloud drive,
  a container, the build session's machine or a log; only the public halves (`openssl ec -pubout`) are handed over
  and committed under `native/firmware/keys/`.
- **dependsOn** [B01]. **externalGates** `OWNER-KEYS`: the owner has generated the release key and the
  provisioning-authority key per `native/KEYS.md` and handed over the two public PEMs (Q-B3 = (a)); the owner signs
  the `diag` image with `sign_release.sh --channel bench`, flashes it onto HAT (A) #1 with the V1 follower bus
  connected and SW1 in the ESP32 position; runs B1 and B3 (Windows, standard user) and S13c; owner approves B1/B3 bars.
- **layers** native-firmware (new), native-bench, tests.
- **files.may** `native/firmware/hat_a/**`, `native/firmware/core/**` (bootstrap handlers, `bp_hal.h`),
  `native/firmware/host/**`, `native/firmware/keys/release_pub.pem`, `native/firmware/keys/provision_authority_pub.pem`
  (public files from the owner, committed verbatim), `native/KEYS.md`, `native/bench/b1_*.py`, `native/bench/b3_*.py`,
  `native/bench/s13c_*.py`, `native/bench/README.md`, `robotis_ai_setup/tests/test_native_hat_a_*.py`,
  `robotis_ai_setup/tests/test_native_core_*.py`, `robotis_ai_setup/tests/test_native_keys.py`.
  **files.mustNot** `COMMON_MUSTNOT`, `tools/**`, `native/armlink/**`, any private key anywhere.
- **testCommands**
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_hat_a_*.py' -v`
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_core_*.py' -v`
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_keys.py' -v`
  - `native/firmware/hat_a/build.sh --variant diag --check-repro --map-check --check-key` (local Docker; `--scratch-key`
    until `OWNER-KEYS`)
  - `native/firmware/hat_a/build.sh --variant prod --check-repro --map-check --check-key` (local Docker; same)
  - `native/firmware/hat_a/sign_release.sh --self-test` (local, Docker): generates a throwaway passphrase-protected
    key pair in a temp directory outside the repo, runs the same openssl → `sig_der_to_raw.py` → espsecure assemble →
    verify path over a random 4 KiB file, expects "Signature is valid" and, with a second throwaway public key, "Signature
    is not valid", then deletes both (the replay of §12 P29); it never reads `native/firmware/keys/`
- **acceptance**
  1. `test_native_hat_a_config.py::NoEfuseBurningConfig::test_no_option_that_burns_efuses`: none of
     `CONFIG_SECURE_BOOT=y`, `CONFIG_SECURE_FLASH_ENC_ENABLED=y`, `CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK=y`,
     `CONFIG_SECURE_BOOT_V2_ENABLED=y`, `CONFIG_SECURE_UART_ROM_DL_MODE_*=y`, `CONFIG_ESP32_DISABLE_BASIC_ROM_CONSOLE=y`
     appears in any `sdkconfig.defaults*`; `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`,
     `CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT=y`, `CONFIG_SECURE_SIGNED_APPS_ECDSA_SCHEME=y`,
     `CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT=y`, `CONFIG_SECURE_SIGNED_ON_BOOT_NO_SECURE_BOOT=y`,
     `CONFIG_ESP_CONSOLE_NONE=y`, `CONFIG_APP_REPRODUCIBLE_BUILD=y` do.
  2. `test_native_core_diag.py::DiagIsReadOnly::test_diag_never_writes_goal_or_torque`: the host-built core with a
     recording HAL answers PROBE, HELLO and `DIAG_BUS_TIMING` for every shape; the recorded bus writes contain no
     WRITE/SYNC_WRITE whose span reaches registers 40 or 42–47, and the only write at all is the 1-byte SYNC_WRITE at 41.
  3. `test_native_core_includes.py::CoreHasNoIdfIncludes`: no file under `native/firmware/core/` includes an
     `esp_*`, `freertos/*`, `driver/*` or `hal/*` header.
  4. Locally: `build.sh --check-repro` prints two identical sha256 for app and bootloader; `--map-check` prints 0
     linked radio sections (probe evidence: §12 P14); `--check-key` finds the compiled public key's PEM text in both
     `bootloader.bin` and the app (§12 P30); `BUILD-MANIFEST.json` records all three outcomes.
  5. External: B1 record shows the proposed bars met (appendix proofs B1) or a decided fix chosen; B3 record shows
     0 resets in 1 000 open/close cycles with DTR/RTS deasserted before open, and the count with pyserial defaults.
  6. `test_native_hat_a_signing.py::Custody` (runs `sign_release.sh` under `bash` with a stub `openssl` and a stub
     `docker` on `PATH` that record their argv): exit 2 and no stub called when `CI=true` or `GITHUB_ACTIONS=true`;
     exit 2 for a key path inside the work tree, also through a symlink placed outside it; exit 2 for a missing key;
     stdout and stderr never contain the key path; the key path appears in exactly one stub call, the host `openssl
     dgst -sha256 -sign` (never in a `docker` argv, never in a `cp`/`mv`/`cat`); the container sees only
     `native/firmware/keys/release_pub.pem`, the unsigned image and the raw signature; a `scratch_key: true` manifest
     is refused (exit 3).
  7. `test_native_hat_a_signing.py::DerToRaw`: `sig_der_to_raw.py` turns 1 000 DER ECDSA-P256-SHA256 signatures made
     with `cryptography` (a key generated in memory by the test, never written) into 64-byte r‖s that verify with the
     same public key; truncated, over-long and non-INTEGER DER are refused. Under `GITHUB_ACTIONS` a missing
     `cryptography` fails the test (CI's `python-tests` installs it for the cloud API); locally it skips with the reason.
  8. `test_native_keys.py` (created here, extended by B10): no file under `native/` or `tools/` and no
     `robotis_ai_setup/tests/test_native_*.py` contains a PEM private-key header (the needle is assembled at run time,
     so the test never matches itself; `robotis_ai_setup/tests/` as a whole is NOT scanned: today's
     `test_phone_camera.py` carries a dummy RSA PEM marked `gitleaks:allow`, §12 P32); `native/firmware/keys/` holds
     only `*_pub.pem`, each a `PUBLIC KEY` PEM of curve P-256; no file under `.github/` names `sign_release.sh` or
     `--authority-key` (no CI run ever signs or provisions, Q-B3).
  9. Locally: `sign_release.sh --self-test` passes (testCommands).
- **failing test** `test_native_hat_a_config.py::NoEfuseBurningConfig::test_no_option_that_burns_efuses` (input: every
  `sdkconfig.defaults*` file; fails today: files absent), `DiagIsReadOnly` above, and
  `test_native_hat_a_signing.py::Custody::test_refuses_under_ci_before_touching_anything` (input: `CI=true`, an existing
  key path outside the repo; asserts exit 2 and an empty stub-call log; fails today: the script is absent).
- **risk** high (firmware on hardware; the bootstrap layouts become the update path's contract). **lenses**
  correctness, delivery, regression.

#### B04 — Windows spike harness; platform and Python-stack spikes (S7–S13, S16)
- **summary.** `native/spikes/windows/run_as_standard_user.ps1` (UTF-8 BOM, CRLF; creates local standard users
  `ebspike1`/`ebspike2` with random passwords that are never echoed (`::add-mask::` on CI), runs the selected spike
  group as `ebspike1` through a scheduled task (`-LogonType Password -RunLevel Limited`), waits, copies records;
  every spike first asserts the token is not in `BUILTIN\Administrators` (enabled) and has Medium IL, else writes a
  FAILED record `not_standard_user`). `spike_common.py` (record writer per appendix proofs §3.1). Spike scripts
  `s07_stack_imports.py`, `s08_install_size.py`, `s09_paths.py`, `s10_daten_concurrency.py`, `s11_timers.py`,
  `s12_session_cookie.py`, `s13_serial.py` (PC-only parts), `s16_msmf.py` (no S14: the WSL-export spike was dropped
  with the data import, owner Q-C3; `s11_timers.py`
  also proves that two processes' `time.monotonic_ns()` are one clock — the premise of the seam's clock mapping, §8.7 —
  by the bracket test of §12 P26, 10 000 round trips); the S7 lock
  `native/spikes/windows/stack/spike-lock-cp313-win.txt` compiled with the pinned uv of `.github/scripts/uv-requirements.lock`
  (`uv pip compile --python-platform x86_64-pc-windows-msvc --python-version 3.13 --generate-hashes`; spike-only, not
  the product lock). The workflow definition `native/spikes/github/windows-spikes.yml` (draft linted clean, §12 P16;
  jobs `platform`, `stack`, `sandbox`, each with `if: startsWith(github.ref_name, 'spike/<group>') ||
  startsWith(github.ref_name, 'spike/all')`). School-PC kit `native/spikes/windows/schoolpc/run_all.ps1` + README.
  Results land in `native/spikes/results/<github|schoolpc>/<spike>.json`; `native/spikes/DECISIONS.md` carries the
  decided fix for every failure (appendix proofs §3.3).
- **dependsOn** [B03] (S13c/S13b need the `diag` firmware). **externalGates** the build session pushes
  `spike/platform-<yyyymmdd>-<n>` and `spike/stack-<yyyymmdd>-<n>` (pre-approved, §3.4); the owner runs the school-PC
  kit as a standard user; results reviewed; decided fixes recorded.
- **layers** native-spikes (new), tests.
- **files.may** `native/spikes/**`, `robotis_ai_setup/tests/test_native_spikes_*.py`. **files.mustNot**
  `COMMON_MUSTNOT`, `tools/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_spikes_*.py' -v`;
  `zizmor --min-severity low native/spikes/github/windows-spikes.yml` (run on a copy placed under a temporary
  `.github/workflows/`); `actionlint` on the same copy.
- **acceptance**
  1. `test_native_spikes_workflow.py`: the YAML's `on` is exactly `{push: {branches: ["spike/**"]}}`; top-level
     `permissions` is exactly `{contents: read}`; every `uses:` is pinned to a 40-hex SHA that equals the repo's pin for
     that action in `.github/workflows/*.yml`; every checkout has `persist-credentials: false`; no `secrets.` anywhere;
     no `${{` inside any `run:`; `timeout-minutes` set on every job; `actions/upload-artifact` has `retention-days ≤ 3`.
  2. `test_native_spikes_ps1.py`: every `native/**/*.ps1` starts with the UTF-8 BOM and is CRLF in the index
     (`git ls-files --eol`).
  3. `test_native_spikes_records.py`: the record schema validates the example records; a record with
     `user_is_admin: true` is invalid.
  4. External: for every spike of this WP, a GitHub record and (where the spike lists a school-PC part) a school-PC
     record exist, and `DECISIONS.md` names the decided fix for every failed check (no "TBD").
- **failing test** `test_native_spikes_workflow.py::OnlySpikeBranches::test_trigger_is_exactly_spike_branches` —
  input `native/spikes/github/windows-spikes.yml`; fails today: file absent.
- **risk** medium (no product code; CI minutes; credentials hygiene). **lenses** correctness, regression, delivery.

#### B05 — Sandbox spikes S1–S5 and S-J
- **summary.** `native/spikes/windows/sandbox/` (ctypes only, no third-party): `appcontainer.py` (profile create/reuse,
  SID derivation, ACLs via `icacls *SID:(OI)(CI)RX|M`, `CreateProcessW` with
  `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` and `HANDLE_LIST`, created suspended, assigned to a job, resumed),
  `pipes.py` (`CreateNamedPipeW` with SDDL `D:(A;;GA;;;<user>)(A;;GRGW;;;<AC SID>)S:(ML;;NW;;;LW)` +
  `FILE_FLAG_FIRST_PIPE_INSTANCE`), `job.py`, child probes `child_s1.py`, `child_s5.py`, the S4 spike-only pipe adapter
  `pipe_socket_adapter.py` (socket-like `recv/sendall/settimeout` over overlapped I/O) used to run a listed subset of
  `physical_ai_tools/physical_ai_server/test/test_code_rpc*.py` and `test_code_program_stop.py` with `AF_UNIX`
  monkeypatched (read-only use of those tests; the subset and its count are recorded), S2/S-J scripts (Temurin 21 JDK
  zip pinned by URL + sha256 from the Adoptium API; `jlink --add-modules java.base,java.compiler,jdk.compiler`;
  SmokeMain from `robotis_ai_setup/docker/code_runner/runner/java`, read-only). Records + `DECISIONS.md` rows.
- **dependsOn** [B04] (harness). **externalGates** `spike/sandbox-<yyyymmdd>-<n>` runs; school-PC run; if any S1–S5
  must-fail check succeeds on both, the owner decides O11 (Q27: "confirm the failure first, then decide").
- **layers** native-spikes, tests. **files.may** `native/spikes/windows/sandbox/**`, `native/spikes/DECISIONS.md`,
  `native/spikes/results/**`, `robotis_ai_setup/tests/test_native_spikes_sandbox.py`. **files.mustNot**
  `COMMON_MUSTNOT`, `tools/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_spikes_sandbox.py' -v`.
- **acceptance**
  1. `test_native_spikes_sandbox.py::EscapeMatrixIsComplete::test_every_s5_attempt_listed`: the S5 attempt table
     equals exactly {`open_broker_process`, `open_engine_process`, `write_outside_run_dir`, `create_global_object`,
     `enumerate_pipes`, `dpapi_unprotect_user_blob`, `shellexecute_runas`, `breakaway_from_job`}; S1's must-fail set
     equals {`connect_loopback_engine_port`, `connect_internet`, `read_user_documents`, `open_engine_process`} and
     must-work {`open_run_pipe`, `import_socket_ctypes_subprocess`, `sys_monitoring_present`}.
  2. External: records for S1–S5, S-J (GitHub + school PC) with every check's expected/observed; failures carry a
     decided fix or the O11 escalation.
- **failing test** the `EscapeMatrixIsComplete` test above (fails today: files absent; guards the matrix against
  silent shrinkage later).
- **risk** medium. **lenses** correctness, regression, delivery.

### Phase 2 — the firmware kernel

#### B06 — Feetech bus simulator (dev/CI only)
- **summary.** `native/devboard/edubotics_devboard/feetech_sim.py`: per-servo model with the register map of
  `feetech_bus.py` (EEPROM vs RAM, factory defaults per model 777/2825 from B9 when available, else the values of
  appendix protocol §9.1 marked ASSUMPTION), `Lock` (0 = EEPROM writes persist), `Max_Torque_Limit` copied to
  `Torque_Limit` at power-on, goal clamp into `Min/Max_Position_Limit` with the RAW value read back at 42 (LeRobot
  #3585 model), a naive wrap-unaware position loop with `Goal_Speed`/`Acceleration` profile (100 steps/s² per LSB),
  Present_Speed/Load (load = configurable gain × `feetech_geometry.limp_gravity_torques` + injected external torque,
  ±1000, sign bit 10), status byte, Return_Delay turnaround, power-on `Torque_Enable 0`, the F3 switch
  (`goal_write_auto_torque`: off|out_of_window|always; default from B7, else `out_of_window`), fault injection (drop or
  corrupt a reply, silent servo, brown-out = RAM reset, overload bit, all-silent bus (SW1), extra devices at 7 and
  11–16, a forced position offset to cross the seam). `SimBus` serves byte-level half-duplex transactions (PING, READ,
  WRITE, SYNC_READ in list order, SYNC_WRITE broadcast) with a write log; `FakeSerial` is pyserial-shaped
  (`read/write/reset_input_buffer/in_waiting/timeout`); deterministic injected time.
- **dependsOn** []. **externalGates** [] (B7/B9 values refine defaults later).
- **layers** native-devboard (new), tests. **files.may** `native/devboard/**`,
  `robotis_ai_setup/tests/test_native_feetech_sim*.py`. **files.mustNot** `COMMON_MUSTNOT`, `tools/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_feetech_sim*.py' -v`; full regression.
- **acceptance**
  1. Cross-validation with today's code (read-only use): the driver's extracted `probe_bus` on a factory sim arm
     returns `(False, …'EEPROM-Positionsgrenzen'…)`; after `tools/feetech_provision.provision(bus, 'V1-SIM', signs,
     spec)` on the sim arm jigged at the URDF zero, `probe_bus` returns `(True, '')` and the record's windows equal
     `[(1024,3072),(2048,4095),(2048,4095),(796,3300),(1024,3072),(2048,3072)]` (§12 P11).
  2. The driver's extracted `set_torque(True)` on a sim arm with joint 3 parked at tick 4060 refuses `edge`; with a
     forced wrapped present after energise refuses `wrongway` and the sim's write log ends with `Torque_Enable 0` to
     all six.
  3. Brown-out injection makes `Torque_Enable` read 0 and RAM registers return to defaults.
  4. A `SYNC_READ` with servo 4 silent returns replies 1–3 only (the tail-break behaviour `FeetechBus.sync_read` relies
     on), and the driver's read-fail latch timeline (1.0 s wall clock) reproduces against the sim.
- **failing test** `test_native_feetech_sim.py::DriverAgainstSim::test_probe_refuses_unprovisioned_sim_arm` (input: a
  factory-default sim arm; asserts the driver's own refusal text). Fails today: module absent.
- **risk** medium (never shipped; but it is the oracle of the kernel tests). **lenses** correctness, regression.

#### B07 — Firmware core I: servo-bus codec and the ported pure guards, proven by vectors from today's driver
- **summary.** `native/firmware/core/fbus.{h,c}` (packet build/parse, checksum, LE16, sign-magnitude,
  `bp_torque_enable_byte_in_span` + `bp_check_safe_torque_enable` returning an error code, applied to WRITE and to
  every per-servo block of SYNC_WRITE before framing; allowlist `{0,1}` in `prod`/`diag`, `{0,1,2}` in `bench`),
  `guards.{h,c}` (`edge_parked`, `wrong_way`, `cmd_clamp`, `seed_effective_goal`, `boot_band_ok`, `fingerprint_check`
  with the extended register set of appendix protocol §6.2, `describe_status_bits` → code list), `arm_spec.py` →
  `generated/bp_arm_spec.h` + `edubotics_boardproto/arm_spec_gen.py`. Vector generator
  `native/protocol/vectors/gen_guard_vectors.py` (loads the driver read-only like `test_feetech_bus.py`; domains per
  appendix protocol §6.3), host runner, and `test_native_guard_mutants.py` (compiles each listed mutant of `guards.c` /
  `fbus.c` with a `-D` switch and asserts ≥ 1 vector fails).
- **dependsOn** [B01]. **externalGates** owner sign-off per ported guard is collected at B09 (vectors here + HIL there).
- **layers** native-firmware/core, native-protocol, tests. **files.may** `native/firmware/core/{fbus,guards}.*`,
  `native/firmware/core/generated/**`, `native/firmware/host/**`, `native/protocol/arm_spec.py`,
  `native/protocol/render.py`, `native/protocol/vectors/**`, `native/protocol/edubotics_boardproto/arm_spec_gen.py`,
  `robotis_ai_setup/tests/test_native_guard_*.py`, `robotis_ai_setup/tests/test_native_arm_spec_*.py`.
  **files.mustNot** `COMMON_MUSTNOT`, `tools/**`, `native/armlink/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_guard_*.py' -v`;
  `… -p 'test_native_arm_spec_*.py' -v`; `python3 native/protocol/render.py --check`; full regression.
- **acceptance**
  1. Every vector family of appendix protocol §6.3 reports 0 mismatches in the sanitised C runner.
  2. Each mutant of appendix protocol §6.4 (≥ 10, incl. `naive >= th`, `d <= m`, `4096 - m`, edge guard ignoring the
     transition gate, span test `index <= len`, fingerprint window tolerance 2) fails ≥ 1 vector.
  3. `test_native_arm_spec_lockstep.py`: `SERVO_IDS`, `JOINT_NAMES`, `JOINT_LIMITS_RAD`, `HOME_JOINTS_RAD`,
     `GRIPPER_OPEN_RAD`, `_DEFAULT_SIGNS`, `WRITE_ACCELERATION`, `GOAL_SPEED_CAP_STEPS`, `SEED_SPEED_STEPS`,
     `_DEFAULT_*` guard values, `_GOAL_EDGE_MARGIN_MAX_TICKS`, `POST_ENERGISE_READ_ATTEMPTS`, `READ_FAIL_STOP_S` equal the
     driver's literals (AST); `ARM_MAX_TORQUE`, `GRIPPER_MAX_TORQUE`, `PROTECTION_CURRENT`, `OVERLOAD_TORQUE`,
     `RETURN_DELAY` equal `tools/feetech_provision.py`; `position_limit_window` equals `feetech_bus.position_limit_window`
     on every limit/sign pair; `LOOP_HZ` is the one deliberate difference (100 vs 50) and is listed in the test;
     `arm_spec.LEADER_MAX_TORQUE` is the literal 0 (owner Q-B2; its lockstep with the tool's own literal lands with B11)
     and the `probe` family includes `Max_Torque_Limit` 0 on every follower id, refused `max_torque` (appendix
     protocol §6.2–§6.3).
- **failing test** `test_native_guard_vectors.py::WrongWay::test_c_matches_driver_over_full_error_domain` — input:
  73 719 cases (error ∈ [−4095, 4095] × thresholds {0,1,401,2047,2048,2049,3000,4094,4095}); and
  `test_native_guard_mutants.py::test_ge_instead_of_gt_is_caught`. The probe (§12 P8–P9) showed a 1/16-grid set passes
  this mutant with 0 mismatches and the full-domain set catches it with 8.
- **risk** high (Feetech torque-on guards and edge ceilings — ask-first, §9). **lenses** correctness, delivery, regression.

#### B08 — Firmware core II: kernel, executor, watchdogs; the soft board
- **summary.** `kernel.{h,c}` (state machine of appendix protocol §5; command handling; the torque choke point as one
  synchronous sequence inside a bus cycle; read-only boot + adopt; read-fail latch; brown-out detection from the
  round-robin READ 40), `executor.{h,c}` (512-waypoint ring, JUMP/FROM_CURRENT/APPEND, integer interpolation with
  `div_round_half_away`, per-cycle command clamp, PATH_DECEL stop generator, HOLD_COMMANDED, HOLD_MEASURED),
  `watchdog.{h,c}` (the heartbeat-fed link watchdog in MOVING/STOPPING/TELEOP/SETPOINT, the TELEOP stream watchdog;
  appendix protocol §5.5), `health.{h,c}`. Python reference
  `edubotics_boardproto/reference/{executor,stop,watchdog}.py` + generated vectors. Host library
  `libbpkernel.{so,dylib}` (plain `-O1 -g`) and `native/devboard/edubotics_devboard/softboard.py` (ctypes; HAL over
  `SimBus` or over a real plain adapter via pyserial; serves the board protocol on an in-process `FakeSerial` pair or
  `socket://127.0.0.1:<port>`). Every soft-board scenario can dump a trace (`*.bptrace`: HAL inputs/outputs); the
  sanitised host runner replays the traces and must produce identical outputs.
- **dependsOn** [B06, B07]. **externalGates** [].
- **layers** native-firmware/core, native-devboard, native-protocol, tests. **files.may** `native/firmware/core/**`,
  `native/firmware/host/**`, `native/devboard/**`, `native/protocol/edubotics_boardproto/reference/**`,
  `native/protocol/vectors/**`, `native/protocol/bp_schema.py` (no layout change of bootstrap messages),
  `native/protocol/render.py`, `robotis_ai_setup/tests/test_native_kernel_*.py`,
  `robotis_ai_setup/tests/test_native_executor_*.py`. **files.mustNot** `COMMON_MUSTNOT`, `tools/**`, `native/armlink/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_kernel_*.py' -v`;
  `… -p 'test_native_executor_*.py' -v`; full regression.
- **acceptance** (each a named test in `test_native_kernel_scenarios.py` against `SimBus` unless noted)
  1. `BootIsReadOnly::test_no_write_before_energise`: all-torqued sim arm and all-limp sim arm, 200 cycles after boot
     + PROBE + HELLO: the sim write log is empty; HELLO_ACK `torque_mask` 0x3F (adopted) / 0x00 (limp).
  2. `TorqueChokePoint::test_energise_bytes_equal_driver`: for the 12 arms of B02 acceptance 1, the bus writes of
     ENERGISE equal the driver's `set_torque(True)` writes and the refusal codes map `edge→edge`, `wrongway→wrongway`,
     `read→read`.
  3. `NoGoalToLimp::test_no_goal_write_to_a_limp_servo`: after TORQUE_OFF(mask 0x04) and any command mix, no SYNC_WRITE
     block for id 3 reaches registers 41–47 (P6).
  4. `LinkLoss::test_stop_within_bound_and_no_motion_after`: executing a 3 s TRAJ, host silent from t=0.5 s → at
     t = 0.5 + 1.0 s the kernel enters PATH_DECEL, reaches HOLD within the stop bound of appendix protocol §5.4, latches
     `link_lost`; 5 s of further silence and of HEARTBEATs produce no new goal (P4).
  5. `ReadFailLatch::test_timeline_matches_driver`: servo 4 silent from t0: latch at t0 + 1.0 s (± one cycle), queue
     cleared, writes silenced; cleared only by the first full read after recovery, `bus_ok` event after STATE.
  6. `test_native_executor_vectors.py`: JUMP equals the driver's `interpolate_trajectory` + `rad_to_command_tick` at
     every 10 ms sample within ≤ 1 tick for 2 000 seeded trajectories with in-band waypoints; PATH_DECEL and the stop
     vectors equal the Python reference exactly.
  7. `TeleopWatchdog::test_latches_after_one_second_without_setpoint`, `Session::test_new_hello_keeps_latches`,
     `Commands::test_duplicate_cmd_id_reacked_not_reapplied`, `Config::test_loosening_refused`.
  8. `TraceReplay::test_sanitised_replay_identical`: every scenario's trace replays in the ASan/UBSan runner with
     identical outputs and no sanitizer report.
  9. The watchdog and mode rules settled for M18 (appendix protocol §2, §5.4, §5.5; §8.10):
     `LinkWatchdog::test_only_heartbeats_feed_it` — a 3 s TRAJ; from t = 0.5 s the host sends a TRAJ APPEND every 0.2 s
     and no HEARTBEAT → PATH_DECEL and `link_lost` at t = 1.5 s (± 1 cycle), the later APPENDs ACKed `latched`;
     `SetpointMode::test_heartbeats_alone_hold_setpoint_for_15_s` — SETPOINT, 2 s of setpoints at 30 Hz, then 15 s of
     HEARTBEATs only at 20 Hz → no latch, mode SETPOINT throughout, every goal written during the gap equals the last
     setpoint's, and a new setpoint is the written goal in the next cycle; `SetpointMode::test_no_heartbeat_latches_
     even_while_setpoints_arrive` — HEARTBEATs stop at t while setpoints continue for 1.67 s → HOLD_COMMANDED, mode
     HOLD, `setpoint_lost` (`data[0]` = 1) at t + 1.0 s (± 1 cycle), later setpoints refused (`setpoint_refused`,
     rate-limited); `ModeSwitch::test_hold_and_reentry_change_no_goal` — SETPOINT → MODE HOLD → 10 s of heartbeats →
     MODE SETPOINT → a setpoint: no latch at any time, MODE SETPOINT is ACKed ok without CLEAR_LATCH, the goal is
     unchanged across both MODE frames, the new setpoint is the goal in its cycle; `TeleopWatchdog::
     test_heartbeats_do_not_feed_teleop` — TELEOP with heartbeats and no setpoint for 1.0 s → `teleop_lost`
     (`data[0]` = 2).
- **failing test** `BootIsReadOnly::test_no_write_before_energise` (input above) and
  `SetpointMode::test_heartbeats_alone_hold_setpoint_for_15_s` (fails today: no kernel; under the appendix's first rule
  it would latch at +1.0 s into the gap, §12 P34).
- **risk** high. **lenses** correctness, delivery, regression.

#### B09 — The kernel on the HAT; the HIL safety benches (B4, B5, B10, VR12/VR14 through the firmware)
- **summary.** Integrate `core/` into `hat_a/`: link task (core 0) decodes/validates and pushes commands into a
  static lock-free SPSC queue; bus task (core 1) runs the cycle and publishes a double-buffered snapshot; TX "drop,
  never block"; no allocation, logging or flash access in bus-task translation units; TWDT 1 s on the bus task plus a
  bus-cycle monitor in the link task (no progress for 200 ms outside UPDATE → `esp_restart` → adopt-and-hold);
  variants `prod` and `bench`. HIL scripts `native/bench/b4_link_loss.py`, `b5_stall_smoothness.py`, `b10_seam.py`,
  `hil_checklist.py` (appendix proofs §2).
- **dependsOn** [B03, B08]. **externalGates** Phase-1 gate closed (RG-H1, B1, B2, B3, B7, B9 recorded and bars
  approved); owner bench: HIL checklist, B4, B5, B10 on HAT #1 with the V1 follower; **owner sign-off per ported guard**
  (edge refusal, seed, post-energise abort, command clamp + ceiling, register-40 rule, read-fail latch, extended EEPROM
  check, boot plausibility band, limp/fault refusals) and of the link-loss behaviour (stop and hold, ≈ 1 s).
- **layers** native-firmware (HAT), native-bench, tests. **files.may** `native/firmware/**`, `native/bench/b4_*`,
  `b5_*`, `b10_*`, `hil_checklist.py`, `robotis_ai_setup/tests/test_native_hat_a_*.py`. **files.mustNot**
  `COMMON_MUSTNOT`, `tools/**`, `native/armlink/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_hat_a_*.py' -v`;
  `native/firmware/hat_a/build.sh --variant prod --check-repro --map-check`; `… --variant bench --check-repro`.
- **acceptance**
  1. `test_native_hat_a_static.py::BusTaskDoesNotAllocate`: the bus-task translation-unit list (declared in
     `hat_a/main/BUS_TASK_UNITS`) contains no `malloc|calloc|realloc|free(|printf|ESP_LOG|nvs_|esp_flash|spi_flash|
     esp_partition_write|vTaskDelay(`.
  2. The same core passes every B08 scenario in the host build (no `#ifdef ESP_PLATFORM` in `core/`).
  3. External (records): HIL checklist all pass; B4 stop latency and distance within the approved bar; B5
     joint-error histogram under injected 500 ms stalls within the approved bar; B10 seam cases (VR6/VR14) behave as
     the driver; reset-during-motion holds (servos keep the last goal, HELLO_ACK reports the reset reason).
- **failing test** `BusTaskDoesNotAllocate` (input: the unit list; fails today: absent).
- **risk** high. **lenses** correctness, delivery, regression.

#### B10 — Signed update with rollback (no eFuses); the gated provisioning passthrough
- **summary.** Firmware: `dfu.{h,c}` (DFU_BEGIN/DATA/END/REBOOT of appendix protocol §3.6; allowed only in LIMP or
  idle HOLD; `build_number` read from the image's app description must exceed the NVS `max_valid_build`; OTA to the
  inactive slot; signature verified by IDF on `esp_ota_end`; PENDING_VERIFY → self-test → marked valid on the first
  HELLO after the self-test; `max_valid_build` written only then), `passthrough.{h,c}` (PROV_CHALLENGE/UNLOCK with
  ECDSA P-256 over `nonce‖mac‖"EDUBOTICS-PROVISION-V1"` against `keys/provision_authority_pub.pem`; only from LIMP with
  every servo reading `Torque_Enable 0`; PASSTHROUGH forwards one Feetech packet and returns the replies; refuses any
  write whose span reaches register 40 with a value ≠ 0, any write to register 5 (ID), SYNC_WRITE; 10 min idle → reboot;
  PASSTHROUGH_END → reboot). Host: `native/armlink/edubotics_armlink/dfu.py` (image streaming, offset acks, resume),
  `edubotics_boardproto/passthrough_client.py` (`BoardPassthroughBus` with `FeetechBus`'s method set: `ping`, `read`,
  `read_u16`, `write`, `sync_read` (emulated by per-id READ), `close`; the unlock signature comes from an injected
  `sign_challenge(bytes) -> DER` callable, so the client never sees a key: the provisioning tool passes the host
  `openssl` one (B11), tests an in-memory `cryptography` one).
  **The pilot channel and the custody runbook (Q-B1, Q-B3).** `sign_release.sh` gains `--channel pilot`, the only
  way a `prod` image becomes a pilot firmware (the artefact spec C embeds, `B-FIRMWARE-ARTIFACT`): on top of B03's
  custody refusals it exits 3 **(a)** while no `native/bench/signoffs/OWNER-FW-CI-<n>.json` exists that parses with
  the sign-off schema (appendix proofs §1.3) and carries the owner's verbatim answer to "add a `native-firmware` job
  to `ci.yml` before the first pilot firmware?" (any decision satisfies the gate: it is asked, not presumed; D-B18),
  naming the gate in its message; **(b)** unless the image's `BUILD-MANIFEST.json` says variant `prod`, `repro:
  identical`, `map_check: pass`, `scratch_key: false`, and its `key` equals the fingerprint of the committed
  `release_pub.pem`, and the image's sha256 equals the manifest's; **(c)** unless `build_number` exceeds every
  `build_number` already in `native/firmware/releases/` (never downgrade, before any signing). It then writes
  `native/firmware/releases/<build_number>/SIGNED-MANIFEST.json` with exactly the keys of §8.11 (incl. `release_tag`
  `hat-a-fw-b<n>`, `asset_name` `edubotics-hat_a-prod-b<n>.bin`, `signed_size`, `key_sha256`) — the file spec C's
  `firmware.pin`, fetch check and allowlist are derived from — writes the signed binary as
  `native/firmware/hat_a/build/release/<asset_name>` (never committed) and prints the owner's one `gh release create`
  upload command for the public release channel (§8.11). `native/KEYS.md` gains: signing a release (`--channel pilot` step by step: mount
  the medium, run, unmount), provisioning through the board (`tools/feetech_provision.py --transport board
  --authority-key <medium path>`, B11), rotation, loss and compromise (§10), and what the keys protect: the
  update path of our own tools and the passthrough gate — never a USB attacker, who can re-flash any ESP32 without
  secure boot (owner Q4, accepted).
- **dependsOn** [B09]. **externalGates** `OWNER-KEYS` (met at B03); bench P7 with images the owner signs with
  `--channel bench`: 50 random power cuts during OTA never brick, a failing self-test rolls back; owner sign-off of
  the update path. `OWNER-FW-CI` is **not** a gate of this WP (it is asked at B18, before the first `--channel pilot`
  signing); this WP builds the refusal that enforces it.
- **layers** native-firmware, native-armlink (dfu helper), native-protocol (passthrough client), tests.
  **files.may** `native/firmware/**`, `native/KEYS.md`, `native/armlink/edubotics_armlink/dfu.py`,
  `native/protocol/edubotics_boardproto/passthrough_client.py`, `native/protocol/edubotics_boardproto/fwcheck.py`,
  `native/devboard/**`, `native/bench/p7_ota_power_cut.py`,
  `robotis_ai_setup/tests/test_native_dfu*.py`, `robotis_ai_setup/tests/test_native_passthrough*.py`,
  `robotis_ai_setup/tests/test_native_keys.py`, `robotis_ai_setup/tests/test_native_hat_a_signing.py`,
  `robotis_ai_setup/tests/test_native_fwcheck.py`.
  **files.mustNot** `COMMON_MUSTNOT`, `tools/**`, any private key anywhere, `native/bench/signoffs/**` (the build
  session writes a sign-off only from an explicit owner message, appendix proofs §1.3; this WP only reads them).
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_dfu*.py' -v`;
  `… -p 'test_native_passthrough*.py' -v`; `… -p 'test_native_keys.py' -v`; `… -p 'test_native_hat_a_signing.py' -v`;
  `… -p 'test_native_fwcheck.py' -v`;
  `build.sh --variant prod --check-repro --map-check --check-key`; `sign_release.sh --self-test`.
- **acceptance**
  1. `test_native_dfu.py::NeverDowngrade::test_lower_build_number_refused` (DFU_BEGIN with `build_number =
     current − 1` → ACK `downgrade`); `States::test_refused_while_moving_or_teleop`; `Transfer::test_offset_acks_and_resume`;
     `Transfer::test_sha_mismatch_refused`; `Valid::test_marked_valid_only_after_hello`.
  2. `test_native_passthrough.py::Gate::test_refused_without_valid_signature`, `…_not_limp`,
     `Rules::test_torque_enable_one_refused`, `Rules::test_id_write_refused`, `Rules::test_sync_write_refused`; the
     soft board, built by the test with an authority public key the test generated in memory (never the committed
     one), runs `tools/feetech_provision.provision(BoardPassthroughBus(...), …)` end to end on a sim arm.
  3. `test_native_keys.py` (B03's, extended): additionally `passthrough_client.py` and `dfu.py` contain no key
     loading (`load_pem_private_key`, `-sign`, `PRIVATE`), and no file under `native/firmware/releases/` is a binary.
  4. `test_native_hat_a_signing.py::PilotGate` (stubbed `openssl`/`docker` as in B03, the script run on a temporary
     copy of the tree): `test_pilot_refused_without_owner_fw_ci` — no `OWNER-FW-CI-*.json` → exit 3, the message names
     `OWNER-FW-CI`, no stub called; a well-formed sign-off with `decision` `rejected` → the gate passes (asked, not
     presumed); a malformed one → exit 3. `test_pilot_refused_unless_repro_and_map_passed` — each of `repro:
     not_checked`, `map_check: fail`, `map_check: not_checked`, variant `bench`, `scratch_key: true`, a foreign key
     fingerprint, an image sha256 differing from the manifest → exit 3, no stub called. `test_pilot_refused_on_a_lower_build`
     — `build_number` ≤ one already released → exit 3. `--channel bench` needs none of the three.
     `test_pilot_writes_the_manifest_C_reads` — on a passing input the script writes a manifest with exactly the
     §8.11 key set, `release_tag`/`asset_name` derived from `build_number`, `signed_size` = unsigned + 68, and the
     signed file under `build/release/<asset_name>`; `ReleasesLayout::test_every_committed_manifest` — every
     `native/firmware/releases/*/SIGNED-MANIFEST.json` has exactly that key set, its `build_number` equals its directory
     name, `key_sha256` equals the committed `release_pub.pem`'s, and nothing else lives under `releases/`;
     `test_native_fwcheck.py::Verify` — `edubotics_boardproto/fwcheck.py::verify_signed_image(data, manifest,
     release_pub_pem)` (stdlib + `cryptography`; the one implementation of §8.11's checks, which C7-01's build calls
     instead of writing its own) accepts an image the test signs with an in-memory key in the §8.11 layout and refuses
     each single deviation (one flipped image byte, one flipped signature byte, version word ≠ 0, a foreign key,
     `signed_size`/`signed_sha256`/`unsigned_sha256`/`key_sha256`/`board`/`variant`/`channel`/`protocol` mismatches);
     `sign_release.sh --self-test` additionally runs it on espsecure's real output (the replay of §12 P36).
  5. External: P7 record; the first signed `prod` image's `SIGNED-MANIFEST.json` recorded with the unsigned
     reproducible hash.
- **failing test** `NeverDowngrade::test_lower_build_number_refused` (fails today: no DFU) and
  `test_native_hat_a_signing.py::PilotGate::test_pilot_refused_without_owner_fw_ci` (input: a manifest that passes every
  other check, no sign-off file; asserts exit 3, the message names `OWNER-FW-CI`, the stub-call log is empty; fails
  today: no `--channel pilot`).
- **risk** high (update path, provisioning, secrets). **lenses** correctness, delivery, regression.

#### B11 — Provisioning tool: the board transport and the leader role
- **summary.** Extend `tools/feetech_provision.py` without changing the follower/adapter byte stream:
  `--transport adapter|board` (board = `BoardPassthroughBus`; `--authority-key PATH` per Q-B3 / D-B19: the path must
  resolve, symlinks followed, outside the git work tree, the run is refused when `CI` or `GITHUB_ACTIONS` is set, and
  the path is used only as the argument of the host `openssl dgst -sha256 -sign` that signs the board's challenge
  (`sign_challenge` for `BoardPassthroughBus`; openssl prompts for the passphrase on the terminal) — never logged,
  copied or passed to a container); `--role follower|leader`. **The leader role (Q-B2 = (a), structurally limp):**
  `servo_ids` 11–16, the leader spec of `native/protocol/arm_spec.py` (interim = the follower windows and jig), and
  **the same provisioning sequence as the follower with exactly one value changed**: `Max_Torque_Limit` (register 16)
  = `LEADER_MAX_TORQUE = 0` on all six servos, the claw included, where the follower writes `ARM_MAX_TORQUE` 800 /
  `GRIPPER_MAX_TORQUE` 150. `LEADER_MAX_TORQUE` is a module literal next to those two, under the same rule ("a safety
  floor must not be a casual command-line argument", the tool's own comment): no option sets it. Everything else as
  the follower: `Return_Delay 0`, `Response_Status_Level 1`, `Operating_Mode 0`, Phase bit 4 clear,
  `Angular_Resolution 1`, jig `Homing_Offset`, the designed windows, `Protection_Current` 310, `Overload_Torque` 80,
  `Lock 1`; `Torque_Enable` is only ever written 0, and RAM `Torque_Limit` (register 48) is never written (the servo
  copies register 16 into it at power-on, `feetech_bus.py`'s "R2-verify" comment, ASSUMPTION until LEADER-LIMP). The
  leader run ends with the German line „[OK] Leader-Arm provisioniert. Bitte das 12-V-Netzteil des Leader-Arms einmal
  aus- und wieder einschalten, damit die Drehmomentgrenze 0 auch im Arbeitsspeicher gilt.“ (until that power cycle
  register 48 still holds the factory value, and the arm link's leader fingerprint refuses with `torque_limit_ram`,
  appendix protocol §6.5 — the power cycle is enforced, not just asked for). `--role leader` is refused with
  `--transport board` (a leader is provisioned on its own plain adapter) and when ids 1–6 answer (a follower), before
  any write. Subcommand `assign-id --from N --to M` (pings 1–20; exactly one servo must answer at N, else refuse;
  writes ID with Lock 0 → re-reads at M → Lock 1); a final read-back pass after `Lock = 1` of every provisioned
  register (register 16 included), stored in a new top-level `safety_readback` key, and a new top-level `role` key
  (both outside `record_checksum`, like `profile_id`; the per-servo `max_torque` already is inside it, so a leader's
  record checksum differs from a follower's of the same arm, §12 P28); fix the comment at `PROTECTION_CURRENT`
  (registers 34/35 untouched; 36 `Overload_Torque` IS written 80) and docstring step 7 (§11 C2–C3).
  `native/bench/leader_limp_check.py` (bench only, B02's `BenchBus`): the LEADER-LIMP bench of appendix proofs §2.
- **dependsOn** [B02, B06, B10]. **externalGates** `OWNER-KEYS` (for `--transport board`); the owner provisions the
  interim leader (a second V1 on its own plain adapter) at the bench and power-cycles it; the LEADER-LIMP record
  (registers 16 and 48 read 0 on all six after the power cycle; each joint moves freely by hand with `Torque_Enable 1`)
  and its sign-off. (Q-B2 itself is answered: (a), owner, 2026-11-25.)
- **layers** tools (provisioning), native-protocol, native-bench, tests. **files.may** `tools/feetech_provision.py`,
  `native/protocol/arm_spec.py`, `native/protocol/render.py`, generated files, `native/bench/leader_limp_check.py`,
  `native/bench/README.md` (its LEADER-LIMP section), `robotis_ai_setup/tests/test_native_provision_*.py`.
  **files.mustNot** `COMMON_MUSTNOT` (minus `tools/feetech_provision.py`), `robotis_ai_setup/tests/test_feetech_bus.py`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_feetech_*.py'` (must stay
  `Ran 324+ … OK` with today's provision tests unchanged); `… -p 'test_native_provision_*.py' -v`; full regression.
- **acceptance**
  1. Today's `TestProvisionEndToEnd` and `TestProvisionTool` pass unchanged; the follower/adapter write log of a sim
     provision equals the pre-change tool's write log plus only READs after re-lock.
  2. `test_native_provision_leader.py::LeaderRole::test_leader_is_provisioned_limp_with_ids_11_to_16`: on a
     `feetech_sim` arm with ids 11–16 at the simulator's factory defaults (`Max_Torque_Limit` 1000, appendix protocol
     §9.1 — never a fake whose register 16 starts at 0: today's `_ProvFakeBus` does, so a read-back alone would pass
     with no write, §12 P28), the write log holds exactly one WRITE `(16, 00 00)` per servo; every servo reads 0 at
     register 16 in `safety_readback` after `Lock = 1`, and Return_Delay 0, Response_Status_Level 1, Lock 1, mode 0,
     Phase bit 4 clear; the record says `role: "leader"` and `max_torque` 0 for all six; the write log holds no
     `Torque_Enable` value other than 0 and no write to register 48; a second run on the same arm writes nothing to
     register 16 (read-compare-skip, §12 P28).
  3. `LeaderRole::test_leader_stream_differs_from_the_follower_only_in_ids_and_register_16`: the same jigged sim arm
     provisioned as follower (ids 1–6) and as leader (ids 11–16) gives write logs that are equal after mapping id
     k ↔ k + 10, except the data of the register-16 writes (`00 00` vs `20 03` / `96 00`).
  4. `LeaderRole::test_max_torque_is_a_literal_not_an_option`: AST — `LEADER_MAX_TORQUE` is a module-level integer
     literal 0 in `tools/feetech_provision.py` and equals `native/protocol/arm_spec.py::LEADER_MAX_TORQUE`; no argparse
     option names `torque`.
  5. `RoleRefusals`: `--role leader --transport board` refused; `--role leader` on an adapter where ids 1–6 answer
     refused; both before any WRITE (empty write log).
  6. `AuthorityKey`: with `CI=true`, or a key path inside the work tree (also via a symlink), `--transport board` exits
     before any bus I/O; the key path appears in no output and in exactly one subprocess argv, the host `openssl dgst
     -sha256 -sign` (subprocess stubbed); the challenge DER it returns is what `PROV_UNLOCK` carries.
  7. `AssignId::test_refuses_with_zero_or_two_responders`; `BoardTransport::test_provision_via_softboard_passthrough`.
  8. Delivery note: a merge of this WP to `main` fires `docker-publish.yml` (its `paths` include `tools/**`, §12 P18):
     the images rebuild and push `:latest` with unchanged content; the owner merges knowingly.
- **failing test** `LeaderRole::test_leader_is_provisioned_limp_with_ids_11_to_16` (input: 6 `feetech_sim` servos with
  ids 11–16, jigged, factory `Max_Torque_Limit` 1000; asserts the six `(16, 00 00)` writes, the read-back 0 and no
  `Torque_Enable` value but 0; fails today: no `--role`).
- **risk** high (provisioning safety EEPROM and the authority key; ask-first). **lenses** correctness, delivery, regression.

#### B12 — Bluetooth: transport and security design; radio-on loop jitter (bench variant only)
- **summary.** The design of appendix protocol §10 becomes `native/firmware/BLE-DESIGN.md`; the `bench` variant gains
  NimBLE (BLE 4.2): advertising (identify/status) and one GATT byte pipe carrying the same COBS frames, **no** security
  layer implementation and **no** control over BLE (STATE notify only, commands refused); `native/bench/b11_radio_jitter.py`
  (bus-cycle start-jitter histogram for 1 h with a phone connected and subscribed, plus an advertising-only run).
  `prod` stays radio-free (`--map-check`).
- **dependsOn** [B09]. **externalGates** owner bench B11; compliance consultant before any radio is enabled in a
  shipped build (owner Q7 — not this round).
- **layers** native-firmware (bench variant), native-bench, tests. **files.may** `native/firmware/**`,
  `native/bench/b11_*`, `robotis_ai_setup/tests/test_native_hat_a_*.py`. **files.mustNot** `COMMON_MUSTNOT`, `tools/**`, `native/armlink/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_hat_a_*.py' -v`;
  `build.sh --variant prod --map-check`; `build.sh --variant bench --check-repro`.
- **acceptance** `test_native_hat_a_config.py::ProdHasNoRadio::test_prod_config_has_bt_disabled` (`CONFIG_BT_ENABLED`
  unset in `prod`, set only in `sdkconfig.defaults.bench`); `--map-check` on `prod` reports 0 radio sections; the bench
  firmware refuses every command arriving over BLE (`not_in_variant`); B11 record with p99 cycle-start jitter and
  missed cycles (proposed bar P2: p99 ≤ 0.5 ms, 0 missed in 1 h).
- **failing test** `ProdHasNoRadio::test_prod_config_has_bt_disabled` (fails today: files absent).
- **risk** medium (bench-only radio; `prod` fenced). **lenses** correctness, regression.

### Phase 3 — leader, teleop, new guards

#### B13 — Arm link I: transport, port discovery, board client, the read-only leader bus, codes → German
- **summary.** `native/armlink/edubotics_armlink/`: `transport.py` (pyserial: `Serial()` → `dtr=False`, `rts=False`
  → `port=` → `open()`; exclusive; reader thread; `socket://` URLs for dev; no pyserial import at module top),
  `discovery.py` (candidates = USB serial ports whose VID:PID ∈ `ARMLINK_USB_IDS` (initially `1A86:55D3`, B9/S13 may
  add); per port: send `0x00` + PROBE, wait 150 ms for PROBE_ACK → follower board; else Feetech PING 11..16 → leader
  adapter (all six + accepted model → leader); PING 1..6 on an adapter → `unconverted_follower`; codes of appendix
  protocol §4.2), `board_client.py` (HELLO with a fresh random nonce, version choice N/N−1, `cmd_id` allocation, ACK
  timeouts and duplicate-safe retry, STATE/HEALTH/EVENT decode with `arrival_mono_ns = time.monotonic_ns()` taken
  first in the reader), `edubotics_boardproto/convert.py` (rad↔tick with the arm spec's signs; the radian clamp to
  `JOINT_LIMITS_RAD` with subdivision at limit crossings; chunking into ≤ 60-point TRAJ frames with APPEND),
  `leader_bus.py` (read-only: `ping`, `read`, `read_u16`, `sync_read`; no write path), `leader.py` (leader fingerprint
  per appendix protocol §6.5; sampling), `mapping.py` (interim identity map leader tick → follower radians; O4 table
  slot), `edubotics_boardproto/texts_de.py` (sentences per appendix protocol §4.3), and `rail.py` — the rail-message
  acceptance of §8.6: the driver's `_trajectory_cb` rules (drop on non-finite / no known name / no points, name matching,
  fill from the measured pose at receipt, HOME + claw 0.90 before the first reading) and a port of
  `interpolate_trajectory` (AST-lockstep with the driver) used to resample non-strictly-increasing or negative-time
  messages at 10 ms; strictly increasing ones go straight to JUMP (+ APPEND frames streamed against `queue_free`).
- **dependsOn** [B08]. **externalGates** S13 results (driver binding, open behaviour) before Windows use; **spec C
  delivers `native/tests/fixtures/rail_messages_v1.json`** (real rail messages: activation/home glides, manual home
  glide, `trajectory_builder` workflow chunks, chunked replays, single-point inference and holds; schema in §8.6) —
  acceptance 1 is not met until it is present and passing.
- **layers** native-armlink (new), native-protocol, tests. **files.may** `native/armlink/**` (except `teleop.py`,
  `ipc.py`, `client.py`, `__main__.py`), `native/protocol/edubotics_boardproto/{convert.py,texts_de.py}`,
  `native/protocol/bp_schema.py`, `native/protocol/render.py`, generated files, `native/devboard/**`,
  `robotis_ai_setup/tests/test_native_armlink_*.py`, `robotis_ai_setup/tests/test_native_texts_de.py`.
  **inputs (read-only)** `native/tests/fixtures/rail_messages_v1.json` (spec C's), the files named in
  `native/protocol/reference_paths.py`.
  **files.mustNot** `COMMON_MUSTNOT`, `tools/**`, `native/tests/**` (spec C owns it; B never edits the fixture).
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_armlink_*.py' -v`;
  `… -p 'test_native_texts_de.py' -v`; full regression.
- **acceptance**
  1. `test_native_armlink_traj.py::DriverParity`: for every message of spec C's fixture **and** B's 13 synthetic cases
     (§12 P25: first point > 0, single point at 1.5/fps, t₀ = 0, t₀ < 0, equal times, decreasing times, NaN position,
     NaN time, empty, unknown names only, OMX names, positions shorter than names, a reordered subset) plus a replay
     whose joint 2 exceeds 3.1416 rad by 0.2 rad mid-chunk and whose joint 4 crosses −1.9198: (a) the arm link drops
     exactly the messages the driver's AST-extracted `_trajectory_cb` drops, with the matching code, and leaves the
     running trajectory untouched; (b) for every accepted one, at every board cycle offset τ = k·10 ms up to
     max(tᵢ) + 1 s, the soft board's written goal equals the driver's `rad_to_command_tick(clamp(interpolate_trajectory(
     installed, τ)))` within ≤ 1 tick, the driver's `installed` coming from its own `_trajectory_cb` on the same message
     and the same measured pose. Under `GITHUB_ACTIONS` a missing fixture fails the test; locally it skips with the reason.
     The reader takes exactly the shape of §8.6 (M14): a non-empty top-level array of objects with exactly the five
     keys, integer `sec`/`nanosec`, six finite `measured_q_at_receipt`, boolean `torque_on`; it ignores
     `rail_messages_v1.meta.json`; `DriverParity::test_fixture_shape_is_strict` fails (never skips, never coerces) on
     each deviation of §12 P33, and `test_fixture_covers_the_parity_kinds` fails unless one element has > 60 points,
     one exactly one point and one `torque_on: false`.
  2. `test_native_armlink_discovery.py`: follower board found; leader adapter found; cables swapped →
     `follower_on_leader_port`/`leader_on_follower_bus`; all-silent board bus → `board_bus_silent`; plain adapter with
     ids 1–6 → `unconverted_follower`; two boards → `multiple_followers`; a port opened by another holder →
     `port_busy`; `bench` firmware → `bench_firmware` unless `--bench`.
  3. `test_native_armlink_fences.py::LeaderBusHasNoWritePath`: AST of `leader_bus.py`/`leader.py` references none of
     `INSTR_WRITE`, `INSTR_SYNC_WRITE`, `.write(`, `.sync_write(`, `build_packet(... INSTR_WRITE`; `edubotics_armlink`
     imports neither `edubotics_devboard` nor `bench`; no `PROV_` message is referenced outside `passthrough_client.py`.
  4. `test_native_armlink_versions.py`: app N with firmware N−1 negotiates N−1; firmware N+1 (min N+1) → `app_too_old`;
     firmware N−2 → `firmware_too_old`.
  5. `test_native_texts_de.py`: every code except `empty_trajectory` (the one non-displayed code) has a sentence; the German lint rules of `.github/scripts/german_detail_lint.py`
     (`TRANSLITERATIONS` before the language test; `GERMAN_CHARS`/`GERMAN_WORDS`) pass on every sentence; the ported
     sentences listed in appendix protocol §4.3 equal the driver's functions' output for the same inputs.
  6. `test_native_armlink_leader.py::LeaderFingerprint` (structurally limp leader, D-B20, appendix protocol §6.5): a sim
     leader (ids 11–16) in the state B11 leaves after a power cycle (register 16 = 0, register 48 = 0, `Torque_Enable`
     0) passes; each single deviation is refused before PREPARE — register 16 = 1000 (factory) or 800 (a follower's
     value) → `leader_fingerprint` / `max_torque`; register 48 ≠ 0 (no power cycle since provisioning, or a foreign
     write) → `leader_fingerprint` / `torque_limit_ram`; `Torque_Enable` 1 → `leader_torqued`; mode, Phase bit 4,
     window, Lock, Return_Delay, Response_Status_Level → their fp codes — and in every case the leader's sim write
     log stays empty (the arm link never repairs a leader).
- **failing test** `DriverParity::test_replay_beyond_limits_within_one_tick` (fails today; the probe shows up to 157
  ticks without subdivision/per-cycle clamp, §12 P10) and `DriverParity::test_decreasing_times_follow_the_driver`
  (input points (0.2, 0.3 s), (0.3, 0.1 s), (0.4, 0.5 s): goals at τ = 0.1/0.3/0.4/0.5 s are 0.2/0.2/0.375/0.4 rad as
  the driver computes them, §12 P25 — a strictly-increasing-only client would refuse or reorder this message).
- **risk** high (wire contract; the rad↔tick seam). **lenses** correctness, delivery, regression.

#### B14 — Arm link II: teleop relay, sync-glide hook, engine-liveness gate, IPC server and process
- **summary.** `teleop.py` (relay thread: every 10 ms `leader_bus.sync_read(56, 2, 11..16)` → if all six: map →
  record the unclamped action → clamp → SETPOINT; MMCSS `"Pro Audio"` via `avrt` and process power-throttling opt-out
  on Windows (no-ops elsewhere); teleop states OFF → PREPARED → ON → LOST; `teleop_on` requires a PREPARE within
  10 s, a follower STATE and a leader sample both ≤ 50 ms old, and |follower − mapped leader| ≤ 0.30 rad on all six
  joints (claw included); no automatic resume ever), the engine-liveness gate of §8.5 (every `I_HEARTBEAT` → exactly
  one board HEARTBEAT, never one of its own; `ENGINE_SILENT_S = 1.0` measured on the arm link's clock at IPC receipt →
  stop the relay, **drop every `I_SETPOINT` until a fresh heartbeat**, `teleop_state` LOST with `engine_silent`; it
  sends **no STOP of its own** in any mode — the board's heartbeat watchdog latches `link_lost` / `teleop_lost` /
  `setpoint_lost` by mode, once and deterministically, D-B14),
  `ipc.py` (`multiprocessing.connection.Listener`, family `AF_PIPE` on Windows, `AF_UNIX` in a 0700 temp dir elsewhere;
  authkey = 32 bytes read as 64 hex chars from the first stdin line; one client; IPC messages of appendix protocol §3.8),
  `client.py` (`ArmLinkClient`, the engine-side API of §8.3, incl. `traj(joint_names, points, …)` of §8.6; one writer
  thread owns the send side; `heartbeat()`/`setpoint()` through latest-wins slots, never blocking, never raising,
  `REQUEST_TIMEOUT_S` 2.0 for requests, `armlink_gone` on EOF — the contract of §8.9),
  `__main__.py` (process entry, §8.2). The stamps of §8.7: `arrival_mono_ns` taken in the reader thread right after a
  frame is decoded, `request_mono_ns`/`arrival_mono_ns` around each leader `sync_read`.
- **dependsOn** [B13]. **externalGates** S11 (timer primitive) and S13c (latency) results inform the relay loop
  primitive; the slow-laptop runs happen in B17.
- **layers** native-armlink, tests. **files.may** `native/armlink/**`, `native/protocol/bp_schema.py`, `render.py`,
  generated files, `native/devboard/**`, `robotis_ai_setup/tests/test_native_armlink_*.py`. **files.mustNot**
  `COMMON_MUSTNOT`, `tools/**`, `native/firmware/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_armlink_*.py' -v`; full regression.
- **acceptance** (end-to-end: fake engine `ArmLinkClient` ↔ arm-link process ↔ soft board ↔ sim follower; sim leader
  behind a `FakeSerial`)
  1. `test_native_armlink_teleop.py::NoAutoResume::test_teleop_stays_off_after_link_returns`: relay stalled 1.2 s →
     board latches `teleop_lost`; relay resumes → the sim follower's goal registers do not change and
     `teleop_state == LOST` until PREPARE + a sync TRAJ + TELEOP_ON.
  2. `Proximity::test_refused_beyond_030_rad` (claw 0.35 rad apart → `teleop_not_synced`).
  3. `EngineSilence::test_hold_and_lost_after_one_second` (TELEOP and SETPOINT: heartbeats stop at t₀ → the board
     latches `teleop_lost` / `setpoint_lost` at t₀ + 1.0 s (± 20 ms), the arm link sent no STOP frame, `teleop_state`
     LOST `engine_silent`, the sim follower's goal unchanged afterwards);
     `EngineSilence::test_setpoints_are_dropped_while_engine_silent` (the fake engine keeps sending `I_SETPOINT` at 30 Hz
     with no heartbeat: none reaches the board after 1.0 s); `EngineSilence::test_moving_is_left_to_the_board` (a
     running TRAJ: the arm link sends nothing, the board latches `link_lost` at 1.0 s and decelerates on the path);
     `Heartbeat::test_one_board_heartbeat_per_engine_heartbeat` (20 Hz in → 20 Hz out, 0 without input);
     `Heartbeat::test_gap_of_950ms_never_trips` (19 missed beats); `Heartbeat::test_setpoint_gap_of_15s_with_heartbeats`
     (SETPOINT, the fake engine sends heartbeats and no setpoint for 15 s → no latch, `teleop_state` untouched, the sim
     follower's goal registers unchanged).
  3c. `test_native_armlink_client.py` (the §8.9 contract): `Client::test_heartbeat_never_blocks` (a fake arm-link
     server that accepted the connection and never reads, the pipe filled first: 2 000 `heartbeat()` calls all return
     `True`, p99 ≤ 100 µs, max ≤ 1 ms, coalesced count reported, the calling thread never waits on the pipe);
     `Client::test_setpoint_latest_wins` (writer blocked, 100 setpoints, then the pipe drains: exactly the newest one
     arrives); `Client::test_process_gone` (the arm-link process killed: `heartbeat()`/`setpoint()` return `False`
     within 100 ms and on every later call, no exception reaches the caller, `on_link_state(…, 'armlink_gone')` exactly
     once, a request returns `armlink_gone` without waiting); `Client::test_heartbeat_swallows_internal_errors` (a
     writer whose send raises a foreign exception: `False`, counted, nothing raised).
  3b. `test_native_armlink_stamps.py::BacklogSpacing`: with the engine-side client paused 3.0 s, the follower samples
     it then receives map (unchanged `ClockMap`, `arrival = arrival_mono_ns`, `stamp = board_us`) to spacings within
     10 ± 1 ms — the §8.7 mapping, the real `capture_timeline.ClockMap` loaded via `reference_paths`.
  4. `RecordedAction::test_action_is_mapped_leader_before_clamp` (leader joint 2 at +3.30 rad equivalent: sample's
     `action_rad[1] == 3.30…`, written goal clamped).
  5. `LeaderNeverWritten::test_leader_sim_write_log_empty` after 60 s of teleop and every command.
  6. `Ipc::test_wrong_authkey_refused`, `Ipc::test_second_client_refused`.
- **failing test** `NoAutoResume::test_teleop_stays_off_after_link_returns`. Fails today: no arm link.
- **risk** high. **lenses** correctness, delivery, regression.

#### B15 — New continuous firmware guards: wrong-way runaway, tracking stop; bench-only idle damping release
- **summary.** Kernel additions per appendix protocol §7.1–§7.3: runaway (every cycle, every torqued joint, the
  `wrong_way` predicate against the effective goal → `Torque_Enable 0` to THAT joint from a prebuilt per-joint payload,
  latch, event `runaway_torque_off`); tracking stop (|e| > E_j and not progressing for > T_track while executing,
  teleop or setpoint → HOLD_MEASURED, latch `tracking_stop`); idle damping release compiled only in `bench`
  (`BP_FEATURE_IDLE_DAMPING`, register-40 value 2 + `Torque_Limit`), default off. Thresholds are constants in
  `arm_spec.py` with a `source` record id (`test_native_arm_spec_sources.py`). B8 bench script `b8_damping_mode.py`.
- **dependsOn** [B09]. **externalGates** B5/VR12/VR14 records → E_j, T_track proposal; B8 bench; **owner sign-off**
  of the runaway guard and the tracking stop; damping in `prod` stays off (owner: bench only) until a separate owner
  decision after B8.
- **layers** native-firmware/core, native-protocol, native-bench, tests. **files.may** `native/firmware/**`,
  `native/protocol/**` (no bootstrap layout change), `native/bench/b8_*`, `robotis_ai_setup/tests/test_native_kernel_*.py`,
  `robotis_ai_setup/tests/test_native_arm_spec_*.py`. **files.mustNot** `COMMON_MUSTNOT`, `tools/**`, `native/armlink/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_kernel_*.py' -v`; `… -p 'test_native_arm_spec_*.py' -v`.
- **acceptance** `test_native_kernel_guards.py::Runaway::test_forced_seam_crossing_torques_off_only_that_joint`
  (torqued holding arm; the sim forces joint 3 across 4095→0 by 200 ticks; within 2 cycles the write log shows
  `Torque_Enable 0` to id 3 only; STATE `torque_mask == 0x3B`; no other joint's goal changed);
  `Runaway::test_no_false_positive_on_vectors` (the B07 wrong-way domain with legal goals and moving joints never
  trips); `Tracking::test_far_goal_executed_does_not_trip` (teleop setpoint 1.0 rad away while the joint moves toward it
  at speed: no trip; Q10); `Tracking::test_blocked_joint_stops_and_holds_measured`; `Damping::test_prod_never_writes_2`
  (prod build, every scenario: no register-40 byte 2 on the bus).
- **failing test** `Runaway::test_forced_seam_crossing_torques_off_only_that_joint`.
- **risk** high (new Rule §2 guards). **lenses** correctness, delivery, regression.

#### B16 — Gravity-compensated collision stop on the follower board (scope configurable, O10 open)
- **summary.** `gravity.{h,c}` (float32 port of `feetech_geometry.limp_gravity_torques` with the chain constants
  `_V1_J*` and `V1_LINK_MASSES`, generated into `bp_arm_spec.h`; torques τ2..τ4), `collision.{h,c}` per appendix
  protocol §7.4 (predicted load = a_j·τ_g,j + b_j·sign(ω)·[|ω| > ω0] + c_j·ω + d_j; residual threshold θ_j; debounce 15
  cycles; low-speed gate 0.05 rad/s; overload bit 0x20 backstop; settle 0.5 s after torque-on, mode entry and
  CLEAR_LATCH; joints 1–5, claw excluded); compiled scope default `{TELEOP}`; CONFIG may add `SETPOINT` and/or
  `PROGRAM`, never remove; trip → freeze the source, relax in place (goal := measured, re-captured at +0.35 s and
  +0.70 s), COLLISION_HOLD latch, event with joints and residuals. Python reference + vectors. Bench tools
  `native/bench/b6_collision.py` (load maps over poses × speeds, σ at rest and moving, contact force with a spring
  gauge, 30-min free-teleop soak) and `b6_fit.py` (least-squares a,b,c,d per joint; θ_j = max(k·σ_j, floor) with k
  and floor proposed in the record).
- **dependsOn** [B14]. **externalGates** B6 bench (with the wrist camera mounted if O5 has delivered one, else
  re-run when it is mounted); **owner sign-off of the collision stop and the O10 scope decision**.
- **layers** native-firmware/core, native-protocol, native-bench, tests. **files.may** `native/firmware/**`,
  `native/protocol/**` (no bootstrap layout change), `native/bench/b6_*`, `robotis_ai_setup/tests/test_native_collision*.py`.
  **files.mustNot** `COMMON_MUSTNOT`, `tools/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_collision*.py' -v`; full regression.
- **acceptance** `test_native_collision.py::GravityModel::test_c_matches_driver_limp_gravity_torques` (5 000 seeded
  poses + HOME + the observation pose: |τ_C − τ_py| ≤ 1·10⁻⁴ N·m on j2..j4; HOME gives (0.934, −2.211, −0.413) N·m,
  §12 P7); `Detector::test_contact_trips_within_150ms_in_teleop_only_by_default` (TELEOP: trip at cycle 15 ± 1;
  SETPOINT: no trip unless CONFIG adds it); `Detector::test_config_cannot_remove_teleop`; `Relax::test_recaptures_three_times`;
  `Detector::test_settle_window_after_energise`.
- **failing test** `GravityModel::test_c_matches_driver_limp_gravity_torques`.
- **risk** high (collision gating — ask-first). **lenses** correctness, delivery, regression.

#### B17 — V1 teleop lag, Windows soak, the O8 decision; the conditional on-board-teleop branch
- **summary.** `native/bench/b12_teleop_lag_v1.py` (records the arm link's leader samples and follower STATE on the
  slowest available Windows 11 laptop, app minimised; analysed by B02's `teleop_lag.py` → per-joint lag vs the OMX
  teleop-lag baseline OMX-LAG), `b13_windows_soak.py` (2 h: link stalls > 100 ms, COM disappearances, selective-suspend events, board
  `link_lost`/`teleop_lost` counts), the per-mode acceleration table (teleop stays `Acceleration 50` unless the record
  shows the lag bar met only with a higher value — then a separate sign-off), and `native/bench/decisions/O8.json`.
  **Conditional on the O8 trigger** (the V1 lag bar fails on the slow laptop and the owner confirms): firmware
  `BP_FEATURE_ONBOARD_TELEOP` per appendix protocol §5.6 (leader ids 11–16 on the HAT bus; 12-servo cycle; on-board
  mapping from the generated leader table; the write mask structurally excludes 11–16; the STATE extension block
  carries the leader positions and the mapped action with the same board stamp; proximity 0.30 rad checked on board at
  MODE TELEOP_ONBOARD) and the arm-link switch (no leader COM port).
- **dependsOn** [B02, B14]. **externalGates** owner bench runs; the OMX teleop-lag baseline (OMX-LAG, B02) and the approved lag bar; the O8
  trigger confirmation.
- **layers** native-bench, native-firmware (conditional), native-armlink (conditional), tests. **files.may**
  `native/bench/**`, and only if O8 triggers: `native/firmware/**`, `native/armlink/**`, `native/protocol/**`,
  `robotis_ai_setup/tests/test_native_onboard_teleop*.py`. **files.mustNot** `COMMON_MUSTNOT`, `tools/**`.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_bench_*.py' -v`; if
  triggered, `… -p 'test_native_onboard_teleop*.py' -v`.
- **acceptance** unconditional: lag and soak records with the approved bars and the O8 decision file. Conditional:
  `test_native_onboard_teleop.py::NeverWritesLeader::test_no_write_to_ids_11_to_16` (12-servo sim bus, 10 s of on-board
  teleop: no WRITE addressed to 11–16 and no SYNC_WRITE block for them); `Proximity::test_refused_beyond_030_rad`.
- **failing test** `test_native_bench_teleop_lag.py::KnownLag::test_recovers_37ms` (synthetic, unconditional part);
  the conditional failing test above.
- **risk** high. **lenses** correctness, delivery, regression.

#### B18 — Seam freeze: board protocol 1 and arm-link IPC 1
- **summary.** Set `BP_PROTOCOL_VERSION = 1`, `IPC_VERSION = 1`; render `native/protocol/FROZEN-1.json` (per message
  type: family, id, direction, field list with types and offsets, fixed/extensible; the codes table; sha256 of each);
  `test_native_protocol_frozen.py` fails on any change of a v1 layout or code unless the version increments and v1 stays
  decodable (N−1); copy §8 of this spec to `native/SEAM.md` with the resulting numbers (STATE rate measured, bars);
  `codec_v1.json` kept forever.
- **dependsOn** [B10, B11, B12, B15, B16, B17]. **externalGates** Phase-2 and Phase-3 sign-offs recorded (ported guards,
  link loss, runaway, tracking, collision + O10, teleop lag/O8, update path, LEADER-LIMP).
  **releaseGate (not a merge gate) `OWNER-FW-CI`** (Q-B1, D-B18): when B18 merges — the protocol is then frozen and
  the next firmware step is a pilot image — the build session puts the question to the owner: "add a
  `native-firmware` job to `ci.yml` (container build with `--check-repro --map-check --check-key`, 10⁷ fuzz executions
  per run) before the first pilot firmware?" and records the owner's verbatim answer as
  `native/bench/signoffs/OWNER-FW-CI-1.json` (appendix proofs §1.3). A yes becomes its own work package (an ask-first CI
  edit, never folded into this one); a no or a "later" keeps firmware builds local and on `spike/*` branches.
  `sign_release.sh --channel pilot` (B10) refuses until the file exists, so the first pilot firmware — spec C's
  `B-FIRMWARE-ARTIFACT` for a pilot — cannot be made without the question asked, while the seam freeze itself (spec
  C's `B-PROTOCOL-FREEZE`) never waits for it.
- **layers** native-protocol, native-armlink, tests. **files.may** `native/protocol/**`, `native/armlink/**`,
  `native/SEAM.md`, `native/firmware/core/generated/**`, `native/bench/signoffs/OWNER-FW-CI-*.json` (from the owner's
  message only), `robotis_ai_setup/tests/test_native_protocol_frozen.py`.
  **files.mustNot** `COMMON_MUSTNOT`, `tools/**`, `.github/**` (a `native-firmware` job is its own WP after a yes).
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_native_*.py'`; `python3 native/protocol/render.py --check`.
- **acceptance** `FrozenLayouts::test_v1_layouts_unchanged`; `NMinusOne::test_firmware_accepts_v0_sessions_until_v2`
  (documented rule); `native/SEAM.md` equals §8 with measured numbers filled in; the merge report puts the
  `OWNER-FW-CI` question to the owner (its answer, once given, is the only file this WP may add under
  `native/bench/signoffs/`; until then B10's `PilotGate` refusal holds).
- **failing test** `FrozenLayouts::test_v1_layouts_unchanged` (fails today: no FROZEN file).
- **risk** high (wire contract freeze). **lenses** correctness, delivery, regression.

#### B19 — Documentation
- **summary.** `CLAUDE.md`: topology rows for `native/`; a new section "The native arm kit (firmware + arm link)"
  stating the invariants of §6 in present tense with symbols; the corrections of §11 (C1, C3) applied; "Common commands"
  gains the native test commands, the firmware build and the fuzz run; "When to ask the user" gains: flashing a HAT,
  signing firmware and provisioning through the board (the owner does both with his offline keys; no agent ever holds
  or asks for a private key, `native/KEYS.md`), the PASSTHROUGH unlock, enabling damping in `prod`, any ci.yml job for
  `native/` (asked at `OWNER-FW-CI`). The new section also states: the leader is provisioned structurally limp
  (`Max_Torque_Limit 0`) and the arm link refuses one that is not. `docs/CLAUDE-CHANGELOG.md`: dated narrative
  (2026-11-25 → completion, the four owner answers), measurements of §12 and the bench records. `docs/KNOWN-ISSUES.md`:
  bench gate status (B1–B13, RG-H1, VR, LEADER-LIMP), spike results, open O4/O5/O8/O10, the `OWNER-FW-CI` answer,
  residuals of §6.2, the UNFENCED items.
- **dependsOn** [B18]. **externalGates** [].
- **layers** docs. **files.may** `CLAUDE.md`, `docs/CLAUDE-CHANGELOG.md`, `docs/KNOWN-ISSUES.md`, `native/README.md`.
  **files.mustNot** everything else.
- **testCommands** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_supabase_056.py' -v` and
  `… -p 'test_deploy_approval_gate.py' -v` (both read `CLAUDE.md`; the second needs `pyyaml`, as in CI) and full regression.
- **acceptance** every symbol named in the new CLAUDE.md section exists (grep); C1/C3 sentences replaced; no line
  numbers cited.
- **failing test** none (documentation).
- **risk** medium (it restates safety rules). **lenses** correctness, regression.

### 3.4 Spike branches (pre-approved pushes; no merge, tag or deploy)

- Branch names: `spike/platform-<yyyymmdd>-<n>`, `spike/stack-<yyyymmdd>-<n>`, `spike/sandbox-<yyyymmdd>-<n>`,
  `spike/all-<yyyymmdd>-<n>`. Each = the feature-branch head + **one** commit that copies
  `native/spikes/github/windows-spikes.yml` to `.github/workflows/windows-spikes.yml` (message
  `spike: enable the Windows spike workflow (never merge)`).
- No other workflow fires on a `spike/**` push (CONFIRMED by reading every trigger, §12 P17). Never open a PR from a
  spike branch (supabase-migrate.yml's `pull_request` jobs would wake).
- Pushing a workflow file needs a token with the `workflow` scope. Runner `windows-2025` (Windows Server, not Windows 11
  client: results are indicative; the school PC is authoritative). Private-repository minutes are billed (Windows ×2).
- Results are copied by the build session from the artifacts into `native/spikes/results/github/` on the feature branch.

---

## 4. Ordering summary

Dependency graph (every edge points to an earlier id):

```
B01 ─┬─ B03 ─ B04 ─ B05
     └─ B07 ─┐
B06 ─────────┴─ B08 ─┬─ B09 ─┬─ B10 ─ B11 (also B02, B06)
                     │       ├─ B12
                     │       └─ B15
                     └─ B13 ─ B14 ─┬─ B16
B02 ───────────────────────────────┴─ B17 (also B14)
B10, B11, B12, B15, B16, B17 ─ B18 ─ B19
```
B02, B06 and B01 can start at once. The Phase-1 gate (records, approved bars, decided fixes of B02–B05) is an external
gate of B09; nothing from B09 on is merged before it closes. The owner's keys (`OWNER-KEYS`) gate B03's first flash, not
its merge; `OWNER-FW-CI` gates the first pilot signing, not any merge.

---

## 5. What this round does not do (and what would bring it in)

| Not done | Trigger |
|---|---|
| The engine, React changes, the System page, installers, the self-updater, signing of Windows binaries | Spec C |
| A custom board; a stop button | Owner decisions (none) |
| Enabling Bluetooth in a shipped build; the Noise session layer's implementation | B12 proofs + compliance review + owner |
| eFuse burning, secure boot, flash encryption | Never on the HAT (owner Q4) |
| Wi-Fi | Never (owner) |
| A per-cycle step-cap refusal of TRAJ | VR12/B5 shows `Goal_Speed`/`Acceleration` insufficient |
| Damping release in `prod` | B8 shows a controlled settle and the owner signs off |
| On-board teleop | O8 trigger (B17) |
| A ci.yml job for `native/` (container firmware build, 10⁷–10⁸ fuzz) | Not now (Q-B1, conductor, 2026-11-25). The owner is asked again at release gate `OWNER-FW-CI` (when B18 merges) before the first pilot firmware, and `sign_release.sh --channel pilot` refuses until the answer is recorded; a yes is its own WP |
| An OMX train-and-run baseline (a paid managed training, Cloud-Inferenz trials) | Never this round (owner Q-B4 = skip). The V1's train-and-run success bar is the owner's judgement at the trial gate. Trigger: the owner asks for a measured comparison |
| The S14 WSL-export spike | Dropped with the data import (owner Q-C3). Trigger: Q-C3 reversed |
| Rotating the firmware release key over the air | Impossible by design: the key is compiled into the bootloader (§12 P30), which OTA never rewrites; a rotation is a USB re-flash (§10). Trigger: a lost or compromised key |
| Espressif QEMU runs of the real image | HIL turnaround becomes the bottleneck |
| HOME changes | RG-H1 → owner (D49); not in spec B |
| The lighter leader's mechanics (O4), the wrist camera (O5) | Design tasks; software needs listed in appendix proofs §4 |
| Changes to today's driver/WSL product | `release/2.41` (spec A) |
| iPad remote / BLE control | O1 + the Bluetooth track |

---

## 6. Invariants and their fences

### 6.1 Fenced
| Invariant | Fence |
|---|---|
| Boot writes nothing; the board never energises on its own | B08 `BootIsReadOnly` (CI) + HIL P5 (bench) |
| No goal write to a limp servo | B08 `NoGoalToLimp` (CI) + HIL |
| The PC never writes the leader bus | B13 AST fence + B14 `LeaderNeverWritten` (CI) |
| Register 40 receives only {0,1} in `prod` (and never 128 anywhere) | B07 vectors + B15 `test_prod_never_writes_2` |
| Every ported guard equals today's driver | B07 full-domain vectors + mutants; B08 choke-point bytes |
| The PROBE frame is not a Feetech packet | B01 golden bytes |
| No eFuse-burning option | B03 config test |
| Never downgrade via DFU | B10 test |
| No private key in the repo; no CI run signs or provisions | B03/B10 `test_native_keys.py` + `ci.yml::secret-scan` (gitleaks, full history) |
| A signing or provisioning key never leaves the owner's medium: refused under CI, refused inside the work tree, used only by the host `openssl`, never logged or handed to a container | B03 `test_native_hat_a_signing.py::Custody`, B11 `AuthorityKey` |
| No pilot firmware before the owner's CI answer; a pilot image is reproducible, radio-free, built with the committed key and newer than every release | B10 `test_native_hat_a_signing.py::PilotGate` |
| The committed release public key is the one compiled into bootloader and app | `build.sh --check-key` (local; recorded in `BUILD-MANIFEST.json`, checked by `PilotGate`) |
| The leader is provisioned structurally limp (`Max_Torque_Limit` 0 written, read back, never an option) | B11 `LeaderRole` (against the simulator's factory 1000) |
| A leader that is not limp (registers 16, 48, 40) is refused before PREPARE and never repaired | B13 `LeaderFingerprint` |
| A follower provisioned with the leader's value never boots | B07 `probe` vectors (`Max_Torque_Limit` 0 on each id → `max_torque`) |
| The arm link stops teleop on engine silence; teleop never auto-resumes | B14 tests |
| The radian↔tick seam equals the driver within 1 tick; `traj` accepts exactly what the driver's `_trajectory_cb` accepts | B13 `DriverParity` (spec C's real rail fixture + 13 synthetic cases) |
| No board heartbeat without an engine heartbeat; setpoints dropped while the engine is silent | B14 `Heartbeat`, `EngineSilence` |
| Only HEARTBEATs feed the board's link watchdog; SETPOINT survives action gaps of any length while they flow and latches 1.0 s after they stop; TELEOP also needs the relay stream; MODE switches move nothing and set no latch | B08 `LinkWatchdog`, `SetpointMode`, `ModeSwitch`, `TeleopWatchdog` (soft board, CI) |
| `heartbeat()` / `setpoint()` never block or raise; a gone arm link answers `False` at once | B14 `test_native_armlink_client.py` (POSIX, CI); Windows by spike S11 |
| The rail fixture is read strictly in C's shape and covers APPEND, single-point and limp messages | B13 `DriverParity::test_fixture_shape_is_strict`, `test_fixture_covers_the_parity_kinds` |
| A pilot firmware C embeds is byte-identical to its manifest and verifies under the committed release key | B10 `ReleasesLayout`, `test_native_fwcheck.py` (CI); C7-01 calls `fwcheck` at build time |
| A backlog received after an engine stall keeps its spacing under the §8.7 mapping | B14 `BacklogSpacing` |
| Every B reader of a driver/tool file takes its path from `reference_paths.py` | B01 `test_native_reference_paths.py` |
| Generated files are current | `render.py --check` via B01 test |
| Protocol 1 never changes silently | B18 frozen test |
| Today's driver is untouched | `files.mustNot` + `test_feetech_*.py` unchanged |

### 6.2 Unfenced (named on purpose)
- **No radio code linked in `prod`** and **bit-identical rebuilds** are checked by `build.sh --map-check` /
  `--check-repro`, which CI does not run (Q-B1: no container job before `OWNER-FW-CI`). They are fenced on the release
  path instead: `sign_release.sh --channel pilot` refuses an image whose `BUILD-MANIFEST.json` lacks a passed check
  (B10 `PilotGate`). Unfenced in CI; fenced at the one step every pilot image passes.
- **The leader's power-on copy 16 → 48** (`Torque_Limit` starts at `Max_Torque_Limit`) is a vendor behaviour the
  simulator only mirrors; LEADER-LIMP proves it on the real servo once, and B13's fingerprint re-reads register 48 at
  every PREPARE.
- **Bus-task timing** (no missed cycle, jitter) exists only on hardware (B1, B11 bench).
- **SW1 glued, XT60-only supply, inline fuse** are kit assembly facts; firmware only names `board_bus_silent`.
- **DTR/RTS behaviour on real Windows drivers** (S13b/B3) and **F3 goal-write auto-torque** (B7) are bench facts;
  the simulator only mirrors the recorded outcome.
- **The soft board and the simulator never ship**: B13 fences imports; spec C's packaging must exclude
  `edubotics_devboard` (spec C to fence).
- **Spec C's heartbeat is a 20 Hz timer in its own callback group on the main executor, gated on `_sensor_alive_mono`
  being < 0.25 s old, never from the default group or a free-running thread** (§8.5) — fenced in spec C; B fences only
  the arm-link half (B14). That arm-link and engine `time.monotonic_ns()` are one clock on **Windows** is S11's proof.

---

## 7. What this machine cannot prove (and which WPs depend on CI or hardware)

| Item | Why | WPs whose real proof is elsewhere |
|---|---|---|
| Anything on Windows (AppContainer, timers, MSMF, CH343 drivers, file semantics) | macOS host; no pwsh | B04, B05 (GitHub `windows-2025` + school PC), B13/B14 Windows parts (B17) |
| Anything on the robot (bus timing, F3, power, collision maps, damping, link loss, OTA power cuts) | no hardware access | B02, B03, B09, B10, B12, B15, B16, B17 (owner bench) |
| libFuzzer on macOS | Apple clang has no fuzzer runtime (§12 P13) | B01 fuzz runs in the Ubuntu container, locally or on a `spike/*` branch (no CI job, Q-B1) |
| amd64 firmware build reproducibility | arm64 host (the probe built on arm64; amd64 emulation here unprobed) | B03 (`build.sh` on an amd64 Linux host; no CI job before `OWNER-FW-CI`, Q-B1) |
| Signing with the owner's real keys | they never come near this machine (Q-B3) | B03/B10: only the owner's runs; CI proves the refusals and the DER conversion, `--self-test` the pipeline with throwaway keys |
| `python-tests` on Python 3.12 with CI's pins | local python3 is 3.14.5 | every WP (CI) |
| The server suite with xdist | `pytest-xdist` missing in the local env (§12 P4) | not used by spec B |

Per work package — what CI (or the local container) proves, and what only the bench or a Windows run proves:

| WP | Proven in CI / container | Proven only at the bench / on Windows |
|---|---|---|
| B01 | codecs, golden PROBE, robustness, vectors; fuzz in the container | — |
| B02 | bench code equals the driver's bytes on a fake bus; analyzers | every Phase-1 record (RG-H1, B2, B7, B9, VR, OMX-LAG) |
| B03 | config fences, diag read-only on the host core; signing custody refusals, DER conversion, key fence; image build + repro + map + key check (container, local) | the owner's keys (`OWNER-KEYS`), the owner-signed diag flash; B1, B3, S13c on the HAT |
| B04 / B05 | workflow and harness fences | every spike (GitHub `windows-2025` + school PC) |
| B06 | simulator vs today's driver and tool | — (B7/B9 refine its defaults) |
| B07 | every ported guard equals the driver (full-domain vectors + mutants) | the owner's per-guard sign-off waits for B09's HIL |
| B08 | kernel scenarios against the simulator; sanitised trace replay | — |
| B09 | static fences; host build of the same core | HIL checklist, B4, B5, B10 |
| B10 | DFU and passthrough state machines; key fence; the pilot gate (`OWNER-FW-CI`, manifest checks) | P7 power cuts on the HAT with owner-signed images |
| B11 | tool byte stream unchanged; leader role (Max_Torque 0 written and read back) and board transport on the simulator/soft board | provisioning the interim leader; LEADER-LIMP |
| B12 | `prod` radio-free config | B11 radio jitter |
| B13 / B14 | discovery, conversion parity, teleop rules, IPC — end to end on the soft board | Windows COM/driver behaviour (S13, B13 soak in B17) |
| B15 | runaway and tracking scenarios; `prod` never writes 2 | thresholds from B5/VR12; B8 damping |
| B16 | gravity model equals the driver's; detector scenarios | B6 load maps, contact force, 30-min soak |
| B17 | lag analyzer; (conditional) no write to 11–16 | V1 lag on the slow laptop, B13 soak |
| B18 | frozen layouts | — (`OWNER-FW-CI` is a release gate, asked at the merge) |

---

## 8. Seam contract for spec C

This section is the contract. It is copied to `native/SEAM.md` at B18 with measured numbers.

### 8.1 What is frozen when
| Milestone | Frozen | C may assume |
|---|---|---|
| B01 merged | The framing (COBS, CRC-32/ISO-HDLC, header), the bootstrap layouts (PROBE, PROBE_ACK, HELLO, HELLO_ACK, HEARTBEAT, ERROR, DFU_*, REBOOT) | Discovery and update work against any later firmware |
| B14 merged | `ArmLinkClient` method names and semantics of §8.3 (dev IPC version 0.x) | C can build engine step 1 against the soft board; regenerate on each B change |
| B18 merged | Board protocol 1, IPC 1, every code and its German sentence key | Append-only changes, version bumps for breaks; N/N−1 for the board, same-version for IPC |
| First `sign_release.sh --channel pilot` (after B18 and `OWNER-FW-CI`) and the owner's upload | The pilot firmware: a `prod` image, its committed `native/firmware/releases/<build_number>/SIGNED-MANIFEST.json` and the asset `<asset_name>` in release `<release_tag>` of the public release channel (§8.11) | C's `B-FIRMWARE-ARTIFACT`: `firmware.pin` = that build number; fetch, verify and embed per §8.11; generate the sha256 allowlist from the manifest; no pilot image exists before the owner's CI answer (D-B18) |

### 8.2 Process boundary
- Entry: `python -I -m edubotics_armlink --ipc <address> [--follower auto|<port>] [--leader auto|<port>] [--log-dir <dir>] [--bench]`.
  `<address>`: Windows `\\.\pipe\EduBotics-armlink-<32 hex random>`, POSIX a path in a 0700 directory. The authkey is the
  **first stdin line** (64 hex chars = 32 bytes); never argv, never environment. Stdout stays empty; logs are English,
  rotating, under `--log-dir`.
- Exit codes: 0 after `shutdown()`; 2 bad arguments; 3 IPC bind failed; 4 authkey missing/invalid. A crash is any other
  code; the board then latches `link_lost`/`teleop_lost` within 1 s and holds.
- The arm link owns both COM ports exclusively and runs its own threads (two readers, the relay, IPC). It sets its own
  Windows scheduling (MMCSS for the relay thread, power-throttling opt-out for the process).
- One engine client at a time. Restart = new process + new authkey; the board keeps its latches across sessions.

### 8.3 Engine-side API (`edubotics_armlink.client.ArmLinkClient`)
All radians are in follower joint order `joint1..joint5, RL_joint`. Every call returns `Result(ok, code, detail,
sentence_de)` unless noted; `code` is a stable snake_case name from `edubotics_boardproto.codes`; `sentence_de` comes
from `texts_de.sentence(code, **params)`.

| Method | Semantics |
|---|---|
| `connect(timeout_s=5.0)` | IPC handshake (version must equal) |
| `heartbeat(sensor_age_s)` → `bool` | The engine's liveness proof, **§8.5 is the contract**: 20 Hz from a timer in its own callback group, sent only while the sensor loop is fresh. **Never blocks and never raises (§8.9)**: `True` = handed to the client's writer, `False` = the arm link is gone. Each heartbeat that reaches the arm link makes it forward exactly one board HEARTBEAT; silence ≥ 1.0 s ⇒ the board's heartbeat watchdog latches (`link_lost` / `teleop_lost` / `setpoint_lost` by mode) and the arm link stops relaying |
| `energise()` | Board ENERGISE (clear queue → seed → torque → verify), exactly today's `set_torque(True)`. Refusal codes `edge`, `read`, `wrongway`, `read_after_energise`, `provision_fault`, `not_probed`, `bad_mode`. The engine keeps today's boot retry (3 × 1 s) |
| `torque_off(mask=0x3F)` | Always executed. This is also "hand-guide on" |
| `traj(joint_names, points, *, mode, purpose)` with `mode` ∈ {`replace`, `from_current`, `append`} | A rail message exactly as today's `/leader/joint_trajectory`: `joint_names` (any order, any subset), `points = [(t_s, positions), …]` with `t_s = time_from_start.sec + nanosec·1e-9` and `positions` aligned to `joint_names` (may be shorter). **Accepted is exactly what today's V1 driver's `_trajectory_cb` accepts, §8.6**: any finite times (first point > 0, = 0 or < 0, equal or decreasing ones included), missing joints filled from the measured pose at receipt. `replace` reproduces the driver's goal function within 1 tick at every board cycle. Refusals (nothing sent, the running trajectory continues — as in the driver): `non_finite`, `unknown_joints`, `empty_trajectory`; plus `time_range` (span > 4 294 s, a deliberate deviation) and the board's `torque_off`/`bus_fault`/`latched`/`bad_mode`. Returns after the board ACK of the first frame; `traj_done` arrives as an event |
| `setpoint(q_rad)` → `bool` | Only in mode `setpoint` (Cloud-Inferenz). Same non-blocking contract as `heartbeat` (§8.9): a latest-wins slot, never queued behind another setpoint. No reply; a refusal arrives as an event. A far goal is executed as is (Q10). Gaps between setpoints of any length are legal while heartbeats flow (§8.10) |
| `mode(m)`, `m` ∈ {`hold`, `setpoint`} | `teleop` is entered only via `teleop_on()`. Entering and leaving change no goal and set no latch; `setpoint` is refused `latched` after a latch until `clear_latch` (§8.10) |
| `stop(kind)`, `kind` ∈ {`path`, `hold`, `hold_measured`} | `path` decelerates along the remaining trajectory; `hold` keeps the commanded goal; `hold_measured` sets goal := measured; in `setpoint`/teleop any stop also returns the board to HOLD (no latch) |
| `clear_latch({'collision','link_lost','teleop_lost','setpoint_lost','tracking_stop'})` | Clears latched stop states to HOLD; never moves |
| `config_tighten(key, value)` | `collision_scope_add` ({'setpoint','program'}), `link_timeout_ms` (200–1000), `teleop_timeout_ms` (200–1000); loosening → `config_loosen` |
| `teleop_prepare()` → `TeleopPrep(follower_q, leader_action_q, follower_age_s, leader_age_s, leader_ok, code)` | Snapshot for the engine's sync-glide planning (floor ladder + tower guard are the engine's) |
| `teleop_on()` | Requires a PREPARE ≤ 10 s ago and abs(follower − mapped leader) ≤ 0.30 rad on all six joints (claw included); else `teleop_not_synced`. Starts the relay |
| `teleop_off()` | Relay stops; board HOLD |
| `firmware_info()` → `FirmwareInfo` | HELLO_ACK fields (variant, version, build_number, git sha, chip rev, MAC, reset reason, boot count, ota_state, servos + fingerprint) |
| `firmware_update(image, sha256, build_number, progress_cb)` | Preconditions enforced by the board (LIMP or idle HOLD) and the arm link (not teleop). Consent and "arm laid down" are the engine's |
| `shutdown()` | Teleop off, ports closed, no torque change |

Callbacks (on the client's reader thread; must not block — copy and queue): `on_follower(FollowerSample)` 100 Hz
(`board_us`, `arrival_mono_ns`, `cycle`, `kernel_state`, `mode`, `torque_mask`, `latch_mask`, `flags`, `q_rad`,
`dq_rad_s`, `load_frac` (signed, ±1.0 = ±100 % PWM, sign per joint convention as today's `JointState.effort`),
`goal_tick`, `goal_rad`, `status_bits`, `missed_mask`, `last_cmd_id`, `queue_free`); `on_leader(LeaderSample)` ≈ 100 Hz
while teleop is PREPARED or ON (`request_mono_ns`, `arrival_mono_ns`, `seq`, `ok_mask`, `leader_tick`, `action_rad` =
**the mapped leader reading before any clamp — the recorded action**, `status_bits`); `on_health(Health)` 10 Hz;
`on_event(Event)`; `on_link_state(follower, leader, code)`; `on_teleop_state(state, code)` where state ∈
{OFF, PREPARED, ON, LOST} — **the authoritative "teleop is on" for every engine gate**.

### 8.4 What C may assume / must do
- C may assume: every sample is stamped **at its source, outside the engine** — follower samples carry `board_us`
  (board read completion, monotonic, 64-bit, `cycle` counter for gaps; the epoch restarts on a board reboot, announced
  by EVENT `boot` and a new HELLO_ACK `boot_count`) **and** `arrival_mono_ns`, taken by the arm link's reader thread
  when it decodes the frame; leader samples carry the arm link's `request_mono_ns` and `arrival_mono_ns`. Both PC
  stamps are `time.monotonic_ns()` of the arm-link process, the same clock as the engine's `time.monotonic()` (one
  system-wide clock: CONFIRMED on macOS, §12 P26; on Windows QPC, proven by spike S11). So a backlog the engine
  receives late after its own stall keeps its true spacing (§8.7 gives the mapping both specs use). The board writes
  the newest setpoint each cycle and buffers nothing in setpoint/teleop mode; `traj(mode='replace')` reproduces today's
  V1 driver goal stream within 1 tick at every board cycle; every refusal has a code and a German sentence; the arm
  link never writes the leader bus; the board holds (never torques off) on link loss, and only a runaway or a
  post-energise fault torques off on its own.
- **The leader** the arm link accepts is provisioned by the owner with `tools/feetech_provision.py --role leader` on
  its own plain adapter: ids 11–16, the follower's sequence with `Max_Torque_Limit = 0` on all six servos (owner Q-B2:
  structurally limp, D-B20), then power-cycled. Before every PREPARE the arm link reads registers 16, 48 and 40 of
  all six and refuses — never repairs — a leader that is not limp: `leader_fingerprint` with `fp_code` `max_torque`
  or `torque_limit_ram` (the latter also right after provisioning until the power cycle), or `leader_torqued`; each
  has its German sentence in `texts_de.py`. C never provisions, writes or "fixes" the leader; its UI shows the
  sentence (the remedy of `torque_limit_ram` and `leader_torqued` is a power cycle of the leader's 12 V supply).
- **The firmware image** C embeds for a pilot is the one `sign_release.sh --channel pilot` produced, identified by its
  `SIGNED-MANIFEST.json` and fetched and verified as §8.11 says; every image that boots on a HAT is signed by the owner's offline release key (D-B12, Q-B3).
  The key custody of all three owner keys — firmware release, provisioning authority, and C's own update-manifest key
  (C7-02, `OWNER-UPDATE-KEY`) — is one text, `native/KEYS.md` (B03 writes it with a section reserved for C7-02's
  manifest-signing procedure; C fills that section, B owns the rest).
- C must: send `heartbeat()` exactly as §8.5 says; plan every glide (activation, sync, recovery, "Ablegen") through
  its floor ladder and tower guard; own every German composition beyond the code sentences; gate the PLAN §2.4 set on
  `on_teleop_state`; exclude `edubotics_devboard` and `native/bench` from every shipped package; never call
  `firmware_update` with an image not in its allowlist of signed hashes (generated from a `SIGNED-MANIFEST.json`);
  never downgrade; never hold, request or store a private key (signing is the owner's, by hand).
- Error ownership: spec B owns the code table and `texts_de.py`; C may add sentences only for its own codes.
- Health: `on_health` per servo voltage (0.1 V), temperature (°C), current (raw × 6.5 mA), status bits; events
  `health_warn` (rate-limited 1 per 5 s per servo) with proposed thresholds 60 °C, 1.5 A for 10 s, 10.8 V (owner
  approves at RG-H1/B2).
- Versioning: the app embeds exactly one firmware image and its protocol N; firmware speaks N and N−1; HELLO_ACK
  `app_too_old` → C shows „App aktualisieren“; `firmware_too_old` → C offers the update.

### 8.5 The heartbeat (M4, settled with spec C)
**What it proves.** One heartbeat = "the engine's executor dispatched a callback just now **and** the engine's sensor
path ingested within the last 0.25 s" — i.e. the engine can both *see* the arm and *act* (a Stopp, a refusal, the next
chunk). Either one dead ⇒ no heartbeat. The board has one watchdog and cannot tell which half died; it does not need
to: both mean "stop and hold".

**Source (C's proposal, confirmed).** A 20 Hz timer (period 50 ms) in a `MutuallyExclusiveCallbackGroup` of its own on
the main executor, the pattern today's node already uses for its 1 Hz ROS heartbeat (`_heartbeat_cb_group`, created
precisely so "a long-blocking callback on the node's default group … can no longer stall the 1 Hz tick"). The callback
sends `heartbeat(sensor_age_s)` only if `time.monotonic() − _sensor_alive_mono < HB_SENSOR_FRESH_S = 0.25 s`; else it
sends nothing. Never from the default group: it serialises `/task/command`, `/workflow/start`, `/workflow/stop`,
`/calibration/start`, `/huggingface/control`, `/workshop/mark_destination` …, and `/workflow/start` holds it across
the program-start torque assert `_set_follower_torque(True)` in `_launch_workflow` — up to 2.0 s `wait_for_service` +
0.2 s hold settle + 3.0 s call ≈ 5.2 s — and then across `WorkflowManager.start`'s `_start_gate` (the mat check,
`_workflow_start_gate` → `program_start_gate`) (CONFIRMED by reading, §12 P24). Never from a free-running non-executor thread: today's
`_sensor_loop` docstring records exactly that failure class ("thread dead, heartbeat still ticking"). (Correction to
C's premise: the HF whoami wait is not on the default group — `/register_hf_user` and `/get_registered_hf_user` run on
`_hf_cb_group`, a `ReentrantCallbackGroup` — but the conclusion stands.)

**Why 0.25 s for the sensor gate.** `_sensor_loop` stamps `_sensor_alive_mono` before every `spin_once(timeout_sec=0.1)`,
so a healthy loop's age stays ≤ 0.1 s plus one guarded callback; `signal_status.INGEST_ALIVE_S` (1.0 s) would double
the detection bound.

**Numbers.**
| Quantity | Value |
|---|---|
| Heartbeat period | 50 ms (20 Hz) |
| Board heartbeat (link) watchdog `T_LINK`, fed by HEARTBEAT frames only, in MOVING, STOPPING, TELEOP and SETPOINT | 1 000 ms = 20 periods; a gap of 1 000 ms trips, a gap ≤ 950 ms (19 missed beats) never does |
| Arm link `ENGINE_SILENT_S` (measured at IPC receipt on its own clock) | 1.0 s (stops the relay, drops setpoints; sends no STOP) |
| Board stream watchdog `T_STREAM` (TELEOP only, fed by the relay's setpoints) | 1.0 s; SETPOINT has none (§8.10) |
| Worst detection after the sensor loop stops | ≤ 0.25 + 1.0 + transport (≈ 0.003) + one board cycle (0.01) ≈ 1.27 s |
| Worst detection after the executor stops dispatching | ≤ 1.0 + 0.003 + 0.01 ≈ 1.02 s |
| Board HEARTBEAT traffic | 20 frames/s × 14 B ≈ 280 B/s |
The engine must therefore never starve the heartbeat group for ≥ 0.95 s (every executor thread blocked, or one GIL hold
that long); spec C fences its thread budget.

**What happens when only one side is alive.**
| Alive | Dead | Board / arm link reaction |
|---|---|---|
| Executor | Sensor loop | No heartbeat → the board, 1.0 s after the last one, whatever chunks or setpoints the executor still sends: MOVING → PATH_DECEL + `LINK_LOST`; TELEOP → HOLD + `TELEOP_LOST`; SETPOINT → HOLD + `SETPOINT_LOST`. The arm link stops the relay, **stops forwarding `I_SETPOINT`**, `teleop_state` LOST (`engine_silent`), and sends no STOP of its own. Idle HOLD/LIMP: nothing moves, nothing latches |
| Sensor loop | Executor (or a ≥ 1 s GIL hold) | Same as above (no heartbeat) |
| Engine | Arm-link process | Board gets no frames: the same latches by mode after 1.0 s; the engine's `heartbeat()` returns `False` and `on_link_state` reports `armlink_gone` (§8.9) |
| Arm link + engine | Board (reset) | Servos hold their last goal; the board reboots read-only and adopts; EVENT `boot` with the reset reason |
Recovery is never automatic: a latch needs `clear_latch`, teleop needs PREPARE + sync glide + `teleop_on`.

### 8.6 What `traj` accepts (M2, proven against the driver)
`FeetechArmNode._trajectory_cb` (run by AST on 13 messages, §12 P25) accepts and evaluates exactly this, and the arm
link (`edubotics_armlink/rail.py`, B13) reproduces it:
1. **Dropped, the running trajectory continues**: torque believed off (`torque_off`), bus fault latched (`bus_fault`),
   any non-finite time or position in any point (`non_finite`, the whole message), no name in `joint_names` known
   (`unknown_joints`), no points at all (`empty_trajectory` — silently, like the driver; not displayed).
2. **Names**: matched by name; positions beyond `len(positions)` and unnamed joints are filled from the measured pose
   at receipt (the driver's `_last_positions`; HOME + claw 0.90 before the first reading). OMX names (`gripper_joint_1`)
   leave the claw at its measured value.
3. **Times**: taken as given. The goal at τ after receipt is the driver's `interpolate_trajectory`: τ ≤ t₀ → point 0;
   τ ≥ t_last → the last point; else the first segment with tᵢ ≤ τ ≤ tᵢ₊₁ (a non-increasing pair jumps). So a first
   point at t > 0 (every `build_segment`/`build_linear_segment` chunk, the 1.5/fps inference point, the 0.05/0.25 s
   holds) is applied at once and held until its time; t₀ < 0 starts mid-segment (τ = 0 gives 0.25 for points
   (−0.5 s, 0.2), (0.5 s, 0.3)).
4. **Mapping to the board**: strictly increasing times with t₀ ≥ 0 → board TRAJ `JUMP` (t_us = round(t·10⁶), first
   frame JUMP, further frames APPEND, streamed while `queue_free` allows); anything else (t₀ < 0, an equal or
   decreasing pair, a µs rounding collision) → the ported `interpolate_trajectory` sampled at τ = k·10 ms up to
   max(tᵢ) + 10 ms → JUMP. Radian clamp to `JOINT_LIMITS_RAD` with subdivision at the crossing, `rad_to_tick`, the
   board's per-cycle command clamp. Deviation: a span beyond 4 294 s (u32 µs) is refused `time_range` (the driver
   would accept it; no producer comes close — replays are capped at `MAX_TRAJECTORY_SPAN_S` 600 s, rail messages are
   ≤ ~1.2 s chunks).
5. **Time base**: the driver starts its clock in its callback; the board at the applying cycle. Parity is defined at
   equal offsets from each side's own start; the start lag itself (IPC + USB + ≤ 1 cycle) is measured by S13c/B5.
The test input is spec C's fixture `native/tests/fixtures/rail_messages_v1.json` (owned by C, read-only for B) plus
B's 13 synthetic cases of §12 P25.

**The fixture's shape — what B13's `DriverParity` reader takes (M14, confirmed 2026-11-25 against C4-0R; §12 P33).**
Exactly C's shape; C changes nothing:
- the file is UTF-8 JSON whose top level is a **non-empty array**; one array element = one rail message, in the order
  the producers emitted them (the reader keeps that order; formatting and whitespace are free — `json.load`);
- every element is an object with **exactly** the five keys `producer` (non-empty string, any value — B does not
  police C's producer list), `joint_names` (array of strings), `points` (array, possibly empty), `measured_q_at_receipt`
  (exactly six finite numbers, radians, in the driver's `JOINT_NAMES` order `joint1 … joint5, RL_joint`) and
  `torque_on` (a JSON boolean, not 0/1);
- every point is an object with exactly `positions` (array of numbers; finite or not is the parity's business, not
  the reader's) and `time_from_start` = `{"sec": int ≥ 0, "nanosec": int in [0, 10⁹)}` — integers, not floats, as
  `builtin_interfaces/Duration` carries them; the reader rebuilds `t_s = sec + nanosec·1e-9` exactly as the driver does;
- `rail_messages_v1.meta.json` is **not read** by B (provenance is C's).
Any deviation — an object at the top level, a sixth key (e.g. provenance inline), a missing key, float seconds,
`nanosec` ≥ 10⁹, five measured values, `torque_on` as 1 — **fails** the test with the entry index and the reason; it
never skips or coerces (under `GITHUB_ACTIONS` a missing file fails too; locally it skips with the reason). Run per
element: the driver's AST-extracted `_trajectory_cb` with `_last_positions = measured_q_at_receipt` and `_torque_on =
torque_on`, against `rail.py` + the soft board with the sim arm placed at `measured_q_at_receipt`. The reader also
requires that the fixture exercises what the parity must cover — at least one element with more than 60 points (the
APPEND path), one with exactly one point, and one with `torque_on: false` — and fails, naming the missing kind, if not.
C's sample of six real entries (`scratch/architect-C/probes/rail_fixture_sample.json`) passes this reader and the
driver installs every one of them (30/30/25/15/1/150 points); its `torque_on: false` copy is dropped (§12 P33).

### 8.7 Clock mapping of the board sources (M9, settled with spec C)
Today's `ClockMap` (`WINDOW_S 2.0`, `MAX_LAG_S 0.5`) fed with the **engine's receipt time** collapses a backlog: after an
engine stall of 0.8 s the backlog's spacing falls to 0 ms (error up to 0.3 s), after 1.5 s up to 1.0 s. Lifting only
the lag clamp (C5-01 as proposed) keeps the spacing up to 1.5 s but **not beyond `WINDOW_S`**: after a 3.0 s stall the
window, pruned by arrival time, holds only burst samples and the spacing collapses again (0–0.79 ms) — §12 P27, the real
class. Agreed mapping instead:
- **Follower state**: unchanged `ClockMap`, `arrival = arrival_mono_ns / 1e9` (the arm link's decode time), `stamp =
  board_us / 1e6`. Spacing 9.98–10.01 ms after stalls of 0.3–3.0 s, error spread 0.02 ms (§12 P27). Reset that source's
  `ClockMap` when `boot_count` changes.
- **Leader action**: `t = (request_mono_ns + arrival_mono_ns) / 2e9` — already the engine's clock; no mapping, no clamp.
  (Today's back-dating of an unstamped leader inside a delivery burst is not needed: the arm link's stamps are not bursts.)
- The recorded action's time is the leader sample's (PLAN §3 item 2); the engine's receipt time is used for neither
  source. C5-01 may still lift the lag clamp for these two sources (harmless); it is not what keeps the spacing.
- Residual: a stall of the **arm-link** process itself delays its own stamps. The relay thread is small and MMCSS-boosted;
  B17's soak measures arm-link stalls.

### 8.8 Paths spec C will move (M6)
C7-06 later moves `robotis_ai_setup/docker/open_manipulator/feetech_arm_node.py` (and with it, presumably,
`feetech_bus.py`, `feetech_geometry.py`). Every B test, generator and bench tool that reads those reference files by path
(B01, B02, B06, B07, B08, B11, B13, B16) takes the path from **one** constant module, `native/protocol/reference_paths.py`
(`DRIVER_NODE`, `DRIVER_BUS`, `DRIVER_GEOMETRY`, `PROVISION_TOOL`, `CAPTURE_TIMELINE`), so C7-06 changes one file;
`test_native_reference_paths.py` fails if a listed path does not exist, and an AST fence fails if any other B module
spells one of those paths itself. Outside B's reach, C7-06 also has to carry: `tools/feetech_provision.py`, which puts
`robotis_ai_setup/docker/open_manipulator` on `sys.path` itself to import `feetech_bus` (B11 keeps that line), and
today's `robotis_ai_setup/tests/test_feetech_*.py` and `test_activation_agent.py`, which read the driver directory by
path (`_DOCKER` in `test_feetech_bus.py`).

### 8.9 `heartbeat()` and `setpoint()` never block their caller (M19)
Both run on the engine's executor (the heartbeat in its own group, §8.5; the Cloud-Inferenz tick's setpoints on the
node's group), and `multiprocessing.connection.Connection.send_bytes` **blocks** once the peer stops reading: on this
macOS host after 585 heartbeat-sized frames (8 190 bytes) to an `AF_UNIX` peer that never reads (§12 P35). A blocking
heartbeat would stall exactly the group whose silence the board measures. Contract of `ArmLinkClient`:
- **One writer thread owns the connection's send side.** No public method writes to the pipe on its caller's thread.
  `heartbeat()` and `setpoint()` put their frame in a **single-slot, latest-wins mailbox** (one each) and wake the
  writer; the lock around a slot is held for the assignment only, never across I/O. The writer sends, in this order,
  the pending heartbeat, the pending setpoint, then queued requests. A slot overwritten before it was sent counts
  `hb_coalesced` / `sp_coalesced` (`client.stats()`); a setpoint is never queued behind an older one (R-5).
- **Return value, no exceptions.** `heartbeat(sensor_age_s) -> bool` and `setpoint(q) -> bool`: `True` = handed to the
  writer (not "delivered"), `False` = the link is down; any internal error is caught, counted and answered `False` —
  nothing ever propagates into the engine's timer callback (an uncaught exception in a callback ends today's node,
  CLAUDE.md). The call does no allocation beyond its frame and no logging. Measured with the contract in miniature
  (§12 P35): with the writer blocked on a full pipe, 2 000 calls took p50 1.5 µs, p99 1.7 µs, max 57 µs, all `True`
  (1 999 coalesced). Bound the client keeps: p99 ≤ 100 µs, max ≤ 1 ms.
- **Pipe full** (the arm link stopped reading): `heartbeat()` keeps returning `True` at once; only the newest
  heartbeat waits in the slot. Nothing reaches the board meanwhile, so the board's heartbeat watchdog latches after
  1.0 s — correct, because an arm link that does not read is not relaying either. When the pipe drains, the one
  pending heartbeat goes out; the latch stays until `clear_latch`.
- **Process gone** (EOF, `BrokenPipeError`, `OSError` on send or receive): the writer marks the link down; from then
  on `heartbeat()`/`setpoint()` return `False` at once (§12 P35: the first `False` 5.4 ms after the peer was killed,
  199 of 200 calls `False`, the remaining one `True` because it was handed over before the writer learned of the death;
  max call time 18 µs); `on_link_state(follower=absent, leader=absent, code='armlink_gone')` is called once (code 69,
  appendix protocol §4); request methods return `Result(ok=False, code='armlink_gone')` without waiting. The client
  never restarts the process (the station does, R-11) and a new process needs a new `ArmLinkClient` (new authkey).
- **Request methods** (`energise`, `traj`, `mode`, `stop`, …) do wait for their `I_REPLY`, bounded by
  `REQUEST_TIMEOUT_S` = 2.0 s (the arm link's own ACK budget is 3 × 100 ms per board command); on timeout `Result(ok=False,
  code='busy')`. They belong on C's ordinary groups, never on the heartbeat group.
- C's adapter may still count `False` returns; it needs no second drop layer (C's R-14 is met by the client).
- Windows: `AF_PIPE`'s `send_bytes` blocking on a full pipe is PLAUSIBLE (overlapped `WriteFile` + wait); spike S11
  measures `heartbeat()` call time with a peer that does not read, on the school PC and on GitHub's runner.

### 8.10 SETPOINT across Cloud-Inferenz's action gaps (M18)
Today's Cloud-Inferenz tick publishes nothing while its action queue is empty — `_cloud_inference_tick` publishes only
`if r.action is not None` — and the arm holds its last commanded pose; a run ends only after `stall_end_s` (≤ 15 s,
`cloud_schedule.STALL_END_S_MAX`) without an adopted plan (read in the code). The appendix as first written would
latch `setpoint_lost` 1.0 s into every such gap, and C's workaround (leave SETPOINT with `mode('hold')` after 0.5 s,
re-enter on the next action) would have tripped too: nothing restarted the stream clock on re-entry, so the first
cycle after `mode('setpoint')` saw a setpoint 10 s old and latched (reference model, §12 P34). **Settled:**
- **SETPOINT has no stream watchdog; the heartbeat watchdog guards it.** While heartbeats arrive, SETPOINT holds the
  last setpoint's goal for any length of gap, torque on, no latch (P34: 10 s gap → no latch, goal unchanged, the next
  setpoint applied in its cycle). When the heartbeats stop, the board latches `setpoint_lost` (HOLD_COMMANDED, mode
  HOLD) 1.0 s after the last one, **even if setpoints keep arriving** (P34: a dead sensor loop with 1.67 s of queued
  actions latches at +1.0 s; a watchdog fed by any frame would have waited until +2.67 s). TELEOP keeps a stream
  watchdog on the relay's setpoints as well (P34: heartbeats alone → `teleop_lost` 1.0 s after the last setpoint).
- **So C needs no mode dance:** stay in `setpoint` for the whole run and leave with `mode('hold')` when the run ends.
  If C does leave and re-enter (C4-09 as written), that is correct too: `mode('hold')` from SETPOINT is HOLD_COMMANDED —
  the last commanded goal is kept, torque stays on, no latch, nothing new is written; `mode('setpoint')` from HOLD needs
  no `clear_latch` (none is set) and writes exactly the held goal until the first new setpoint, so neither switch moves
  the arm; the first setpoint after it is executed as any setpoint is (a far one as is, owner Q10 — the cloud schedule
  adopts after a hold only a chunk planned from an observation sent after the hold began, so it starts from the pose
  the arm holds). Either way: zero motion caused by the switch (P34: no goal change at either MODE frame).
- **After a latch** (`setpoint_lost` because the engine or the arm link went silent ≥ 1.0 s), `mode('setpoint')` is
  refused `latched` until `clear_latch({'setpoint_lost'})`; clearing never moves. Whether the cloud run then goes on or
  ends is C's decision (it was blind for ≥ 1 s; ending it with a German sentence is the conservative choice).
- Fenced in B08 (`SetpointMode`, `ModeSwitch`, `LinkWatchdog`) on the soft board; the board-level timing is HIL.

### 8.11 The pilot firmware artefact: where it lives, how C fetches it, what `firmware.pin` names (M20)
- **In the repository** (committed, B10): `native/firmware/releases/<build_number>/SIGNED-MANIFEST.json` and nothing
  else under `releases/<build_number>/` — no binary is ever committed. Exact keys (`test_native_hat_a_signing.py::
  ReleasesLayout`): `schema` = `"edubotics-firmware-manifest/1"`, `board` = `"hat_a"`, `variant` = `"prod"`, `channel` =
  `"pilot"`, `build_number` (int, = the directory name), `protocol` (int N, the board protocol it speaks; N−1 too),
  `fw_version` (string), `commit` (40 hex), `unsigned_sha256` (= `BUILD-MANIFEST.json`'s reproducible app hash),
  `signed_sha256`, `signed_size` (= unsigned size + 68), `signature_scheme` = `"esp-ecdsa-p256-v1"`, `key_sha256` (sha256
  of the DER of the committed `native/firmware/keys/release_pub.pem`), `release_tag` = `"hat-a-fw-b<build_number>"`,
  `asset_name` = `"edubotics-hat_a-prod-b<build_number>.bin"`.
- **The binary** is a release asset in the **public release channel** — the repository the `.exe` and the Pi tarball
  already go to, `${{ vars.RELEASES_REPO || github.repository }}`, which `version-preflight` proves public (a private
  repository's assets answer 404 anonymously): release `release_tag`, asset `asset_name`, byte-identical to
  `signed_sha256`. `sign_release.sh --channel pilot` writes the signed file as
  `native/firmware/hat_a/build/release/<asset_name>` (gitignored) next to the manifest it commits and prints the one
  upload command; **the owner uploads** it with his own GitHub login (`gh release create <release_tag> --repo <channel>
  --latest=false --title "HAT (A) firmware b<build_number>" <file>`; no CI secret, no workflow — Q-B3, and writing the
  release repository is an owner action, CLAUDE.md). The tag `hat-a-fw-b…` matches no trigger (`release.yml` fires on
  `v*.*.*` only; nothing reads `releases/latest` — §12 P36), so it starts no release and advertises nothing.
- **How C's build fetches and checks it** (C7-01): URL = `https://github.com/<channel>/releases/download/<release_tag>/
  <asset_name>`, derived only from the channel the build already uses and the manifest's two fields (SA-01's rule; no
  free URL for a tagged build); anonymous GET. Then, before embedding, B's `edubotics_boardproto.fwcheck.
  verify_signed_image` (B10) checks: size = `signed_size`; sha256 = `signed_sha256`;
  sha256 of all but the last 68 bytes = `unsigned_sha256`; the last 68 bytes are a little-endian u32 `0` and a 64-byte
  big-endian r‖s, an ECDSA-P256-SHA256 signature over all preceding bytes that verifies under the committed
  `release_pub.pem`, whose sha256 equals `key_sha256` (§12 P36: espsecure's V1 block is exactly that); `board`,
  `variant`, `channel` as above and `protocol` = the app's N. Any mismatch fails the build. An **untagged** development
  build may take the bytes from a local file instead (C's switch) — checked identically.
- **What `firmware.pin` names:** exactly `none` or the decimal `build_number` (e.g. `7`): it selects
  `native/firmware/releases/7/SIGNED-MANIFEST.json` from the same checkout, and everything else is derived from that
  manifest. A tagged build with `none`, with a number whose manifest is absent, or with an asset that fails a check is
  refused (C's rule, unchanged). C's allowlist of firmware hashes (C7-02) is generated from the same manifest.

---

## 9. Ask-first items touched (CLAUDE.md "When to ask the user") and their status

| Item | Where | Pre-approved? |
|---|---|---|
| Feetech torque-on guards and edge ceilings (porting to firmware, the extended EEPROM check) | B07–B09 | In principle (PLAN §2.4, Phase 2 "every driver guard and the full EEPROM check"); **owner sign-off per guard after vectors + HIL** (B09 gate) |
| New firmware guards: runaway, tracking stop | B15 | In principle (PLAN §2.4); thresholds + **sign-off after bench** |
| Collision gating (a V1 collision stop) and its scope | B16 | In principle; **O10 open — owner decides with B6 data** |
| Damping mode writes | B15 (bench variant) | Bench only (PLAN §2.4); `prod` **not** approved |
| Link-loss behaviour | B08/B09 | Decided (PLAN §2.4); sign-off after B4 |
| V1 HOME | none (RG-H1 measures) | Not touched |
| Flashing HATs; esptool on the bench | B03, B09, B10 | Owner flashes (PLAN §2.1 "EduBotics flashes every HAT"); every flashed image is owner-signed (D-B12) |
| Provisioning safety EEPROM (leader role, `Max_Torque_Limit` 0) | B11 | **Pre-approved: owner Q-B2 = (a), 2026-11-25.** Leader ids/Return_Delay decided (PLAN §2.1); LEADER-LIMP proves it on the real servos |
| Editing CI/CD config | spike workflow on `spike/**` only; no ci.yml change | Spike runs pre-approved; **no ci.yml edit (conductor, Q-B1, 2026-11-25)**; a `native-firmware` job is asked again at `OWNER-FW-CI` (when B18 merges) and would be its own WP |
| Production secrets (firmware release key, provisioning-authority key; spec C's update-manifest key) | B03, B10, B11 | **Answered: owner Q-B3 = (a), 2026-11-25** — the owner generates and holds all three offline on an encrypted medium, signs and provisions locally; no CI secret; no agent creates, receives or holds a private key (gate `OWNER-KEYS` is the owner's action) |
| Paid checks | none | **None: the OMX train-and-run baseline is skipped (owner Q-B4, 2026-11-25)**; no training hours, no Cloud-Inferenz trials, no RunPod spend in spec B |
| `git push` | build session pushes `spike/*` only | Pre-approved for spike branches; no merge/tag/deploy |
| Renaming files other layers reference | none | — |

---

## 10. Rollback

- Repository: every WP except B11 adds files under `native/` and `robotis_ai_setup/tests/test_native_*` only → `git revert`
  of the WP. B11's tool change keeps the follower/adapter byte stream identical (acceptance 1) → `git revert` restores it.
- Firmware: the app never downgrades (owner Q25) — a bad firmware is fixed by **roll-forward** (build N+1 with the
  change reverted). A firmware that fails its self-test reverts automatically on the next reset (IDF rollback). On the
  bench, `esptool` over the ROM bootloader always recovers (no secure download mode).
- A guard that false-refuses in the field: the arm link shows its sentence; the owner ships a roll-forward with the
  constant corrected; there is deliberately no field switch (D-B10).
- **Keys** (`native/KEYS.md`). The release public key sits in the bootloader **and** the app (§12 P30); OTA rewrites
  only the app, so the release key cannot be rotated over the air. *Release key lost* (every copy): fielded boards
  accept no further update through the app; recovery is an `esptool` re-flash over USB of a bootloader and an app
  built with a new key — always possible, since no eFuse is burnt (D-B12) — per board, by EduBotics or a service visit.
  *Release key compromised*: the same re-flash; until then the app's own sha256 allowlist (spec C) still refuses a
  foreign image on the update path, and a person with the USB cable could re-flash any board anyway (owner Q4,
  accepted). *Provisioning-authority key lost or compromised*: a roll-forward firmware with a new authority public
  key, signed with the release key and delivered over the normal update path; provisioning through the board waits
  until then (provisioning on a plain adapter needs no key). A second encrypted copy of each key, stored apart,
  turns "lost" into a non-event (recommended; the owner decides).
- Spike branches: delete the branch; nothing reaches `main`.

---

## 11. Code-truth corrections

| # | Claim (where) | Code | Evidence |
|---|---|---|---|
| C1 | `CLAUDE.md` (v1_studio): "`/joint_states` is created only after the boot probe succeeds: ping 1..6 (id 7 silent) …" | `FeetechArmNode.probe_bus` pings only `SERVO_IDS` 1–6; "id 7 silent" is asserted only by the scan prober `identify_arm.identify_feetech` (`feetech_bus_length` up to `FEETECH_MAX_ID` = 7) | CONFIRMED by reading |
| C2 | `tools/feetech_provision.py` comment at `PROTECTION_CURRENT`: "factory sustained-overload protection (addr 34/35/36, untouched)" | `provision` step 6 writes `REG_OVERLOAD_TORQUE` (36) = 80 | CONFIRMED (VERIFICATION C10 holds) |
| C3 | `tools/feetech_provision.py` docstring step 7 "Re-lock (Lock = 1), read EVERY register back and VERIFY"; `CLAUDE.md` "re-lock → read-back verify" | Each register is read back right after its own write (`_write_verify_u8/_u16`), before the re-lock; after `Lock = 1` only Lock is read back; the record stores `max_torque` but no other safety value | CONFIRMED by reading |
| C4 | Research 02 §6.9/§10, FINDINGS: shared executor vectors "need no tolerance" | True only between in-band waypoints (≤ 1 tick vs today's radian interpolation); today's per-sample clamp vs a waypoint-only clamp differs by up to 157 ticks on band-crossing segments | CONFIRMED (§12 P10) |
| C5 | Research 02 §7.4: "j2 ≥ 50 % in 18 % of samples" | 29.2 % with uniform sampling within the joint limits and the box-model above-table filter (121 330 poses); the 83 % maximum and HOME's 19/45/14 % reproduce | CONFIRMED (§12 P7); the fraction depends on sampling |
| C6 | Research 02 §6.3: "task watchdog on bus_task at 100–200 ms" | `CONFIG_ESP_TASK_WDT_TIMEOUT_S` is whole seconds; a sub-second bound needs the firmware's own cycle monitor (B09) | PLAUSIBLE (Kconfig type), probe set 1 s |
| C7 | Research 02 §6.1 "ESP-IDF pinned v5.x LTS" | Espressif's current policy states 30-month support per release without an LTS label (search result); pinned v5.5.6 (D-B3) | PLAUSIBLE (search) |
| C8 | Research 02 §3.6 B1 pass bar "p99 ≤ 5 ms with 12 servos" | The one-bus 12-servo topology is superseded (two buses); B1 is re-defined for the 6-servo follower bus (appendix proofs) | Decision record |
| C9 | Dispatch test matrix: server suite `-n auto --dist loadfile` | `pytest: error: unrecognized arguments: -n` in the local `lerobot` env (`pytest-xdist` not installed) | CONFIRMED (§12 P4) |
| C10 | `identify_arm.py` constants `LEADER_IDS = 1..6`, `FOLLOWER_IDS = 11..16` | These are the OMX's Dynamixel ids — the reverse of the V1 plan (follower 1–6, leader 11–16). Never reuse them for the V1 | CONFIRMED by reading |
| C12 | Spec C's C5-01: lifting `ClockMap.MAX_LAG_S` for the two board sources keeps a stalled engine's backlog spacing | It does up to a stall of `WINDOW_S` (2.0 s) only: the deque is pruned by **arrival** time, so after a 3.0 s stall the window holds only burst samples and the spacing collapses to 0–0.79 ms. Feeding the unchanged `ClockMap` the arm link's `arrival_mono_ns` keeps 9.98–10.01 ms for every stall | CONFIRMED with the real class (§12 P27) |
| C13 | Spec C's premise "the HF whoami wait blocks the default group" | `/register_hf_user` and `/get_registered_hf_user` run on `_hf_cb_group` (Reentrant), isolated for exactly that reason (comment in `_init_ros_service`); the default group still blocks ≈ 5.2 s in `/workflow/start` (`_set_follower_torque(True)`) and across the mat-check start gate | CONFIRMED by reading (§12 P24) |
| C14 | This spec's first §8.3: "`traj` points have t from 0, strictly increasing" | Wrong against the driver: every `build_segment`/`build_linear_segment` chunk starts at t = dt > 0, inference points sit at 1.5/fps, holds at 0.05/0.25 s, and `_trajectory_cb` also installs t₀ ≤ 0 and equal/decreasing times; §8.6 now states the driver's rule | CONFIRMED (§12 P25) |
| C11 | Local baseline of `robotis_ai_setup` suite vs CLAUDE.md "Ran 3390" | Here: `Ran 3218 … FAILED (errors=10, skipped=92)`; 9 errors `No module named 'yaml'`, 1 `IsADirectoryError` because `test_supabase_058.OneMarker` walks gitignored `docs/plans/**` and another plan's scratch holds a directory named `ver_one_robotis_ai_setup_gui_app_constants.py` | CONFIRMED (§12 P5) — a test walking gitignored scratch is fragile; spec B forbids `*.py` directory names in its scratch and `native/` |
| C15 | This spec's earlier B10: "owner creates and keeps the release-signing key" as a gate of the update WP, and `sign_release.sh` with `espsecure.py sign_data --keyfile` | The verification key is compiled into the **bootloader** as well as the app, so every image that boots on a HAT — B03's first `diag` flash included — needs the owner's signature (gate `OWNER-KEYS` moves to B03), the release key cannot be rotated over the air, and `espsecure`'s `--keyfile` refuses a passphrase-protected PEM, so signing goes through the host `openssl` and espsecure's pre-calculated-signature path (D-B19) | CONFIRMED (§12 P29, P30) |
| C16 | An implied premise of the earlier B11 acceptance ("ends with `Max_Torque_Limit` per Q-B2") | Today's `_ProvFakeBus` (`test_feetech_bus.py`) starts every register at 0, so register 16 already reads the leader's value 0 before any write; a read-back test passes with the write deleted. B11 now asserts the write itself against the simulator's factory 1000 | CONFIRMED (§12 P28) |
| C17 | PLAN §5 "Pass bars" (train-and-run success rate measured on today's OMX); PLAN Phase 1 "OMX baselines (teleop lag, success rate)" and "spikes S1–S5, S7–S14"; spec C's `TRIAL-PASSED` ("success rate against the OMX baseline") | Amended by the owner (Q-B4, Q-C3): there is no OMX success-rate baseline — the V1's train-and-run bar is the owner's judgement at the trial gate — and no S14. Spec C's `TRIAL-PASSED` wording needs the same change | Decision record (`SPEC-DECISIONS.md`) |
| C18 | This spec's appendix §2/§5.5 as first written: the link watchdog fed by "the last valid frame of the session", and §8.5's table "executor alive, sensor loop dead → MOVING: `LINK_LOST` after 1.0 s" | The two disagreed: a TRAJ chunk or a queued setpoint the executor still sends would have fed the watchdog, so a dead sensor loop was caught only 1.0 s after the arm link stopped forwarding (+2.67 s in the reference model instead of +1.0 s). Now only HEARTBEATs feed it, in MOVING, STOPPING, TELEOP and SETPOINT | CONFIRMED with the reference model (§12 P34) |
| C19 | This spec's appendix §5.5 as first written: SETPOINT's stream watchdog fed by setpoints, and "re-entering needs CLEAR_LATCH then MODE"; nothing said when the stream clock starts on MODE entry | Against today's Cloud-Inferenz (`_cloud_inference_tick` publishes only when there is an action; holds up to `stall_end_s` 15 s are legitimate) every gap > 1.0 s latched `setpoint_lost`, and spec C's leave-and-re-enter workaround latched at the re-entry cycle. SETPOINT now has no stream watchdog (§8.10) | CONFIRMED by reading the tick + the reference model (§12 P34) |

---

## 12. Probes run (command → observed)

P1. `git rev-parse HEAD; git status --porcelain; git branch --show-current` → `66382d27…`, clean, `main`.
P2. `cd robotis_ai_setup && python3 -m unittest discover -s tests -p 'test_feetech_*.py'` → `Ran 324 tests in 62.4s OK`
(python3 3.14.5). `-p 'test_activation_agent.py'` → `Ran 60 … OK`; `-p 'test_collision_detector.py'` → `Ran 27 … OK`.
P3. Driver/provision/scan/geometry read in full where load-bearing (`probe_bus`, `_seed_goal_from_present_locked`,
`_verify_after_energise_locked`, `set_torque`, `_trajectory_cb`, `_read_tick`, `_write_targets`, `provision`,
`identify_feetech`, `limp_gravity_torques`, `collision_detector`, `ClockMap`).
P4. `/opt/miniconda3/envs/lerobot/bin/python -m pytest test/test_feetech_goal_edge_ceiling.py test/test_v1_ik.py
test/test_arm_geometry_v1.py -q -n 8` → `unrecognized arguments: -n 8`; without `-n` → `54 passed, 33 warnings in 9.00s`.
P5. Full `python3 -m unittest discover -s tests` (robotis_ai_setup) → `Ran 3218 tests in 252.9s FAILED (errors=10,
skipped=92)`, causes as C11, 0 failures.
P6. `scratch/architect-B/bus_timing.py` (framing from `feetech_bus.build_packet`) → follower 6, L=6 + 7 B write:
1.58/1.76/2.06/2.36/4.46 ms at t_turn 20/50/100/150/500 µs; L=15: 2.12/2.30/2.60/2.90/5.00; leader read-only L=2:
0.76/0.94/1.24/1.54/3.64; L=6: 1.00/1.18/1.48/1.78/3.88; duty at 100 Hz 21.2–26.0 %; today's sync-read deadline 140 ms.
P7. `scratch/architect-B/gravity_duty.py` → HOME τ2..4 = (0.934, −2.211, −0.413) N·m = 19/45/14 % of stall (4.90/4.90/2.94);
observation pose (−2.350, 0.560, −0.348) = 48/11/12 %; 121 330 above-table random poses: max 83/45/14 %, j2 ≥ 50 % in 29.2 %.
P8. `scratch/architect-B/vector_probe.py` → vectors generated from the AST-loaded driver in 0.06 s: edge 16 384, clamp
16 384, wrong-way 209 088 (1/16 grid), CRC+COBS 2 000; a C11 port under ASan+UBSan: `243856 cases, 0 mismatches`.
Mutants: `naive>=th` → **0 mismatches (missed)**; `d<=m` → 6; `4096-m` → 259.
P9. Full error domain wrong-way (73 719 cases, thresholds {0,1,401,2047,2048,2049,3000,4094,4095}) → C port 0 mismatches;
`naive>=th` mutant → 8 mismatches (caught).
P10. Interpolation parity: 760 000 samples, in-band waypoints → max |driver − kernel| = 1 tick; waypoints allowed to
cross the clamp band → 157 ticks.
P11. `feetech_bus.position_limit_window` for the V1 limits → sign +1 `[(1024,3072),(2048,4095),(2048,4095),(796,3300),
(1024,3072),(2048,3072)]`, sign −1 `[(1024,3072),(0,2048),(0,2048),(796,3300),(1024,3072),(1024,2048)]`.
P12. PROBE frame for the pinned layout → `00 03 01 01 01 02 04 09 45 44 42 50 7d a4 df f2 00`, no `0xFF`.
P13. `cc -fsanitize=fuzzer,address` on macOS → `libclang_rt.fuzzer_osx.a not found`; in `espressif/idf:v5.5.6` (Ubuntu
24.04) after `apt-get install clang` (18.1.3): a COBS/frame fuzzer → `Done 2000000 runs in 5 second(s)`, no finding.
P14. `docker pull espressif/idf:v5.5.6` → index `sha256:bf72fe7a…cac84` (amd64 `851f136a…cfb3`, arm64 `26776964…c112`);
`scratch/architect-B/idf_probe` (UART0 2 Mbaud, UART1 1 Mbaud pins 19/18, esp_timer ISR, ECDSA verify via mbedTLS) built
twice at different paths → app `9a0130ea…8495` ×2, bootloader `3e66aa39…f64f` ×2 (bit-identical); app 262 132 B (87 % of
a 0x1E0000 slot free), bootloader 23 472 B; resolved `CONFIG_ESP32_REV_MIN=0`, rollback y, anti-rollback unset,
flash encryption unset, BT unset, `CONFIG_SECURE_SIGNED_APPS_ECDSA_SCHEME=y`; map file: 0 linked sections from
libnet80211/libpp/libcoexist/libbtdm/libwpa (387 for the UART driver lib); no `/work/` path in the binary;
esptool/espsecure 4.12.0. (The throwaway signing key was deleted after the probe.)
P15. Docker Hub tags `espressif/idf`: v5.5.6 (2026-10-16), v6.0.3, v6.1.1 (2026-11-13); web search: v6.0 brings mbedTLS 4/PSA
and Picolibc; Espressif's 2026 flyer: 30-month support per release.
P16. Draft `windows-spikes.yml` (scratch) → `zizmor --min-severity low`: "No findings"; `actionlint`: exit 0. (zizmor's
online audits were not run.)
P17. Triggers of all nine workflows read: only `push` to `main` (ci, docker-publish, W2, W3), tags (release), `pull_request`
(supabase-migrate), dispatch/call (W6, trainer, W5) — nothing fires on `spike/**`.
P18. `docker-publish.yml` `on.push.paths` includes `tools/**` (and `robotis_ai_setup/docker/**`, `physical_ai_tools/**`).
P19. `test_licence_headers.py`: `ROOTS = [physical_ai_tools, open_manipulator, robotis_ai_setup, tools]`, `.c` not in
`EXTENSIONS`; `ci.yml::powershell-encoding` scans only `robotis_ai_setup` and `physical_ai_tools`.
P20. `.gitignore` has no rule for `native/`; `git check-ignore` of `native/...` → not ignored.
P21. `capture_timeline.ClockMap`: `WINDOW_S = 2.0`, `MAX_LAG_S = 0.5`.
P22. `git status --porcelain` at the end → see the returned `treeCheck`.
P23. Same probe project with `CONFIG_SECURE_BOOT_BUILD_SIGNED_BINARIES=n` + `CONFIG_SECURE_BOOT_VERIFICATION_KEY` (a
throwaway public key) → `build_exit=0`, app 262 064 B; `espsecure.py sign_data --version 1` → "Signed 262064 bytes";
`espsecure.py verify_signature --version 1` → "Signature is valid"; signing twice → identical sha256 (`fa96c0a7…`).
The throwaway key was deleted.
P24. Read `physical_ai_server.py`: `_init_ros_service` service table (default group: `/task/command`, `/workflow/start`,
`/workflow/stop`, `/calibration/start`, `/huggingface/control`, `/workshop/mark_destination`, …; HF services on
`_hf_cb_group` = Reentrant), `_set_follower_torque` (`wait_for_service(timeout_sec=2.0)`, `_TORQUE_ON_HOLD_SETTLE_S` 0.2,
3.0 s call deadline), `_launch_workflow` (one `_set_follower_torque(True)` then `manager.start` → `_start_gate` =
`_workflow_start_gate` → `program_start_gate`), `_heartbeat_cb_group` (1 Hz heartbeat on its own group "so a
long-blocking callback on the node's default group … can no longer stall" it), `_sensor_loop` (stamps
`_sensor_alive_mono` before every `spin_once(timeout_sec=0.1)`; docstring: "thread dead, heartbeat still ticking"),
`main()` `MultiThreadedExecutor(num_threads=8)`, `signal_status.INGEST_ALIVE_S = 1.0`.
P25. `scratch/architect-B/rail_acceptance_probe.py` (the driver's `_trajectory_cb` + `interpolate_trajectory` extracted by
AST, 13 messages): installed — first point t > 0, single point 0.05 s, t₀ = 0, t₀ = −0.5 s (goal at receipt 0.25),
equal times, decreasing times, OMX names (claw kept at measured 0.9), positions shorter than names (rest from measured),
reordered subset; dropped with the running trajectory kept — NaN position, NaN time (`nicht-endliche Werte`), unknown
names (`ohne bekannte Gelenknamen`), empty points (silently), torque off (`Drehmoment ist aus`). Decreasing-times
evaluator for (0.2, 0.3 s), (0.3, 0.1 s), (0.4, 0.5 s): τ 0.0/0.1/0.2/0.3 → 0.2, 0.4 → 0.375, 0.5 → 0.4. Read the
producers: `build_segment`/`build_linear_segment` first sample at t = dt; `chunked_publish` re-bases each chunk;
`DataConverter` single point at `1.5·10⁹/fps` ns; `collision_monitor` relax hold 0.25 s.
P26. `scratch/architect-B/xproc_monotonic_probe.py`: 2 000 round trips, a child's `time.monotonic_ns()` lay outside the
parent's [send, receive] bracket 0 times (rtt p50 12.0 µs, p99 16.1 µs; `mach_absolute_time`, macOS only).
P27. `scratch/architect-B/clockmap_backlog_probe.py` with the real `capture_timeline.ClockMap`, 100 Hz board stamps,
engine stall S then a 5 ms burst: today's input (engine receipt) — S 0.3 s spacing kept; 0.8 s → 0.00–10.00 ms, error
spread 301.6 ms; 1.5 s → 1 001.6 ms; 3.0 s → 0.00–0.79 ms, 2 989 ms. `MAX_LAG_S = inf` — kept up to 1.5 s, collapsed at
3.0 s (0.00–0.79 ms). Arm-link arrival used directly — 9.01–10.94 ms. Unchanged `ClockMap` fed the arm-link arrival —
9.98–10.01 ms, spread 0.02 ms at every S.
P28. `scratch/architect-B/leader_max_torque_probe.py` (today's `tools/feetech_provision.py` unmodified, the suite's own
`_ProvFakeBus`, only `ARM_MAX_TORQUE`/`GRIPPER_MAX_TORQUE` patched to 0 and an in-memory spec with ids 11–16): the fake's
register 16 reads **0 before any write**; seeded with the factory 1000, the leader run writes `(16, 00 00)` once per
servo, reads back 0 on all six, writes `Torque_Enable` only as 0, records `max_torque` 0 ×6; the record checksum
differs from the same entries with 800/150 (`max_torque` is inside `record_checksum`); a second run writes nothing to
register 16 (read-compare-skip). The record has no `role` key today.
P29. In `espressif/idf@sha256:bf72fe7a…` (espsecure 4.12.0), throwaway keys only, deleted after: `sign_data --version 1
--keyfile <plain PEM>` → "Signed 4096 bytes"; `--keyfile <aes256 passphrase-protected PEM>` → "A fatal error occurred:
Incorrect ECDSA private key specified"; host-style `openssl dgst -sha256 -sign <passphrase PEM>` → DER → raw 64-byte
r‖s, then `sign_data --version 1 --pub-key pub.pem --signature sig.raw` → "Signed 4096 bytes"; `verify_signature
--version 1` with the matching public key → "Signature is valid", with another key → "Signature is not valid"; a DER
signature passed directly is refused ("expected 64 bytes long, provided string is 71"); signed file = data + 68 bytes. On
this macOS host, outside any container: `/opt/miniconda3/bin/openssl` (OpenSSL 3.5.6) and `/usr/bin/openssl`
(LibreSSL 3.3.6) both sign with a passphrase-protected EC PEM (`dgst -sha256 -sign -passin`), and both signatures,
converted to r‖s, assemble and verify "Signature is valid" in the pinned container (only the public PEM, the image
and the signature were mounted). All throwaway keys were deleted.
P30. The `idf_probe` project rebuilt (in the session scratchpad) with `CONFIG_SECURE_BOOT_BUILD_SIGNED_BINARIES=n` and
`CONFIG_SECURE_BOOT_VERIFICATION_KEY` = a throwaway public PEM → `build_exit=0`; `pub.pem.S`
(`signature_verification_key_bin`) is generated in **both** the app build and `build/bootloader/`; the PEM text is
found in `bootloader.bin` (offset 1 360) and in the app `bp_probe.bin` (offset 34 348); the raw X‖Y and the DER are
in neither (the key is embedded as PEM text — `--check-key` greps the PEM).
P31. After the edits of 2026-11-25: `grep -n -i -E "train.and.run|train_and_run|OMX-T&R|S14|s14_" SPEC-B-*.md` →
only lines that say they are dropped or skipped: the header, §0, §1.1, B02's summary, B04's summary, §5, §9, §11 C17,
this probe, §13, and the appendix proofs' introduction and §3.3 note; no `acceptance`, `externalGates`, `failing test`,
script name, record-schema gate or bench-table row names them (the `gate` enum lists `OMX-LAG` and `LEADER-LIMP`, no
train-and-run gate).
P32. `git grep -l "PRIVATE KEY"` → `robotis_ai_setup/tests/test_phone_camera.py` (two dummy RSA PEM lines with
`# gitleaks:allow`), `robotis_ai_setup/pi_agent/phone_camera.py` and its test, `gui/app/phone_cert.py`,
`gui/app/scripts/new_phone_cert.ps1`; nothing under `tools/`. So the key fence scans `native/`, `tools/` and B's own
test files only, and builds its needle at run time.
P33. `scratch/architect-B/rail_fixture_reader_probe.py <repo> scratch/architect-C/probes/rail_fixture_sample.json` (C's
file read only): top level `list` of 6; the strict reader of §8.6 accepts it; the driver's AST-extracted
`_trajectory_cb` with `_last_positions = measured_q_at_receipt` installs 30/30/25/15/1/150 points (`workflow_chunk` ×2,
`replay_chunk` ×2, `inference_point`, `activation_home`; `joint_names` equal the driver's `JOINT_NAMES`
`['joint1' … 'joint5', 'RL_joint']`); a `torque_on: false` copy is dropped; refused: an object at the top level, a sixth
key, a missing `torque_on`, float `time_from_start`, `nanosec` = 10⁹, five measured values, `torque_on` = 1.
P34. `scratch/architect-B/setpoint_mode_model_probe.py`: a cycle-level reference model of appendix §5 under three rules
— OLD (SETPOINT stream watchdog fed by setpoints, no clock restart on MODE entry), ANY (fed by any frame), HB (the
settled rule: heartbeats only; TELEOP keeps its stream rule; MODE entry restarts the stream clock and keeps the goal).
After 2 s of 30 Hz setpoints: 10 s gap with heartbeats — OLD latches `SETPOINT_LOST` at 3.0 s, ANY/HB none, goal held,
the next setpoint applied at 12.0 s; C's dance (MODE HOLD at +0.5 s, MODE SETPOINT 30 ms before the next action) — OLD
latches at the re-entry cycle 11.97 s, ANY/HB none and no goal change at either MODE frame; the same with a late MODE
HOLD (+1.1 s) — OLD latches at 3.0 s and refuses the re-entry `latched`, ANY/HB none; engine dead — all latch at
3.0 s; sensor loop dead at 2.0 s while 1.67 s of queued setpoints play — OLD and ANY latch at 4.67 s, HB at 3.0 s and
refuses the late setpoints; TELEOP with heartbeats only — `TELEOP_LOST` 1.0 s after the last setpoint. Read in the code:
`_cloud_inference_tick` calls `publish_action` only `if r.action is not None`; `cloud_schedule.STALL_END_S_MAX = 15.0`.
P35. `scratch/architect-B/heartbeat_nonblocking_probe.py` (macOS, `AF_UNIX`, Python 3.13): raw `send_bytes` to a peer
that never reads stopped returning after 585 heartbeat-sized frames (8 190 B) — it blocks; the §8.9 mailbox client on
that full pipe: 2 000 `heartbeat()` calls p50 1.5 µs, p99 1.7 µs, max 57.4 µs, all `True`, 1 999 coalesced; peer killed
(SIGKILL): first `False` after 5.4 ms, 199/200 calls `False`, max call 18 µs, no exception reached the caller. (Under
`spawn` the authkey must be passed to the child, not re-derived — the arm link's stdin authkey of §8.2 does exactly that.)
P36. In `espressif/idf@sha256:bf72fe7a…`: a 262 064-byte image signed with `espsecure.py sign_data --version 1` (throwaway
key, deleted) → +68 bytes, the unsigned bytes an unchanged prefix, the block = little-endian u32 `0` + 64-byte r‖s
big-endian that verifies over the prefix (`ecdsa`, SHA-256; read as two little-endian halves it fails). Consistent with
P14/P23: the IDF-signed app was 262 132 B, the unsigned 262 064 B. `release.yml` triggers on tags `v*.*.*` only; `git grep
"releases/latest"` and `make_latest|--latest` in `.github/` find nothing.

---

## 13. Owner questions — answered 2026-11-25 (`SPEC-DECISIONS.md`)

- **Q-B1 — CI for `native/`** → *(conductor)* no `ci.yml` edit now; `native/` is proven through `robotis_ai_setup/tests`
  inside today's `python-tests` job; firmware container builds and long fuzz runs stay local or on `spike/*` branches.
  **The owner is asked again before the first pilot firmware**: release gate `OWNER-FW-CI` (asked when B18 merges), enforced by
  `sign_release.sh --channel pilot`, which refuses until the answer is recorded (D-B18, B10 `PilotGate`).
- **Q-B2 — Leader servos `Max_Torque_Limit = 0`** → *(owner)* (a), the leader is structurally limp: B11 provisions 0
  on ids 11–16 and B13 refuses a leader that is not limp (D-B20); LEADER-LIMP proves it on the real servos.
- **Q-B3 — Key custody** → *(owner)* (a): the owner holds the firmware release-signing key and the
  provisioning-authority key offline on an encrypted medium and signs and provisions locally; no CI secret. Spec C's
  app update-manifest key (C7-02, Q-C4) is held the same way. One custody text, `native/KEYS.md` (B03, B10); signing
  through the host `openssl` (D-B19); gate `OWNER-KEYS` at B03, because every image that boots on a HAT is signed (D-B12).
- **Q-B4 — OMX train-and-run baseline** → *(owner)* skip: no paid OMX training and no Cloud-Inferenz trials; the V1's
  train-and-run success bar is the owner's judgement at the trial gate; the OMX teleop-lag baseline (no spend) stays.

No genuine fork of spec B is open. The next owner question is `OWNER-FW-CI`, asked at its gate, not now; the second
encrypted key copy (§10) is a recommendation the owner decides when he creates the keys.
