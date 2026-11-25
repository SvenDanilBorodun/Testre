# SPEC B — Appendix: board protocol, kernel, guards, codes

Part of `SPEC-B-proofs-firmware-armlink.md`. Everything here is the content of `native/protocol/bp_schema.py`,
`native/protocol/arm_spec.py` and the kernel; the coder renders it, the tests hold the code to it. Little-endian
throughout. "Tick" = the servo's 0..4095 position unit. Board time = `esp_timer_get_time()` µs (int64, monotonic).

---

## 1. Framing (frozen at B01)

- Stream: frames separated by `0x00`. Each frame on the wire = `COBS(body) ‖ 0x00`. A sender writes one extra `0x00`
  before its first frame after opening the port (flushes garbage such as the ESP32 ROM boot log at 115 200 baud,
  which cannot be silenced without an eFuse or a strapping pin — PLAUSIBLE, research 02 H6).
- `body = header ‖ payload ‖ crc32`; header (6 B): `fver u8` (framing version, **always 1**), `type u8`, `seq u16`
  (per-direction counter, diagnostic only), `len u16` (payload length). `crc32` = CRC-32/ISO-HDLC (reflected poly
  0xEDB88320, init 0xFFFFFFFF, xorout 0xFFFFFFFF; = Python `zlib.crc32`) over `header ‖ payload`, little-endian.
- Limits: `BP_MAX_PAYLOAD = 1024`; decoder buffer `BP_MAX_ENCODED = 1100`. A stream decoder that exceeds the buffer
  discards bytes until the next `0x00` and counts `rx_overflow`.
- A decoder drops (never acts on) a frame with: COBS error; decoded length < 10; `len ≠ decoded − 10`; CRC mismatch;
  `fver ≠ 1`; unknown `type` (counted); a payload whose length is not the type's fixed size (strict types) or is
  below the type's minimum (extensible types; trailing bytes ignored); a variable-length type whose inner length field
  disagrees with `len`. Every drop increments a counter reported in HEALTH (`rx_bad_frames`).
- Golden PROBE frame (fver 1, type 0x01, seq 0, len 4, payload `"EDBP"`): wire bytes
  `00 03 01 01 01 02 04 09 45 44 42 50 7d a4 df f2 00` (leading `0x00` = the flush). No `0xFF` byte, so no Feetech
  servo can parse it (a Feetech instruction starts with `FF FF`). Pinned by B01.

## 2. Sessions, versions, command ids

- Discovery: PROBE → PROBE_ACK carries `proto_min`, `proto_max`, variant, build number, MAC. No session yet.
- HELLO carries the chosen protocol `proto` (the host picks the highest version both support), `host_kind`
  (1 arm link, 2 provisioning tool, 3 bench) and a random non-zero `session_nonce`. The board answers HELLO_ACK with
  `proto_accepted` (0 = refused, with `refuse_code` `app_too_old` / `firmware_too_old`). A new HELLO (new nonce) starts
  a new session: command-id memory cleared, **latches and physical state kept**.
- Version rule: the firmware of release N implements protocol `P_N` and `P_{N−1}`; the bootstrap types (PROBE,
  PROBE_ACK, HELLO, HELLO_ACK, HEARTBEAT, ERROR, DFU_*, REBOOT, ACK) never change layout. After the freeze (B18)
  every change is append-only: new types, or new trailing fields on extensible types; a layout break needs a new
  protocol version.
- Every command carries `cmd_id u32 ≠ 0`, unique per session. The board keeps the last 16 `(cmd_id, result)`; a
  duplicate is answered with the stored result and **never re-applied**.
- **Only HEARTBEAT frames of the current session feed the link watchdog** (§5.5; settled with spec C as M18,
  2026-11-25): a TRAJ, SETPOINT or any other command proves only that something sends, while a HEARTBEAT is relayed by
  the arm link once per engine heartbeat, which the engine sends only while its executor dispatches and its sensor
  loop is fresh (main spec §8.5). Frames before HELLO other than PROBE/HELLO are answered `ERROR session`.

## 3. Message layouts

Columns: id · name · direction (H→B host to board, B→H) · fields in order · size (F fixed, E extensible minimum,
V variable).

### 3.1 Bootstrap (frozen forever)
| id | name | dir | fields | size |
|---|---|---|---|---|
| 0x01 | PROBE | H→B | `magic char[4] = "EDBP"` | F 4 |
| 0x41 | PROBE_ACK | B→H | `magic char[4] = "EDBA"`, `board_kind u8` (1 = HAT_A), `proto_min u8`, `proto_max u8`, `variant u8` (1 prod, 2 bench, 3 diag), `build_number u32`, `mac u8[6]` | F 18 |
| 0x02 | HELLO | H→B | `proto u8`, `host_kind u8`, `session_nonce u32` | F 6 |
| 0x42 | HELLO_ACK | B→H | `proto_accepted u8`, `refuse_code u16`, `session_nonce u32`, `variant u8`, `fw_version char[16]` (NUL-padded ASCII), `git_sha u8[20]`, `build_number u32`, `chip_rev u16` (major·100+minor), `mac u8[6]`, `reset_reason u8` (`esp_reset_reason_t`), `boot_count u32` (RTC_NOINIT), `uptime_ms u32`, `ota_state u8` (0 valid, 1 pending_verify, 2 rolled_back), `kernel_state u8`, `torque_mask u8`, `probe_code u16`, `seen_ids u32` (bit n−1 = id n answered, ids 1–16 pinged), `n_servos u8` (6), then 6 × {`id u8`, `present u8`, `model u16`, `fw_major u8`, `fw_minor u8`, `angular_resolution u8`, `fp_code u16`, `fp_register u8`, `fp_read u16`, `fp_expected u16`} | E 159 |
| 0x03 | HEARTBEAT | H→B | `session_nonce u32` | F 4 |
| 0x45 | ACK | B→H | `cmd_id u32`, `result u16` (0 ok, else a code of §4), `detail u16` | F 8 |
| 0x4F | ERROR | B→H | `code u16`, `related_type u8`, `reserved u8`, `related_cmd_id u32` | F 8 |

### 3.2 Torque and motion
| id | name | dir | fields | size |
|---|---|---|---|---|
| 0x04 | ENERGISE | H→B | `cmd_id u32` | F 4 |
| 0x05 | TORQUE_OFF | H→B | `cmd_id u32`, `mask u8` (bit i = servo id i+1), `reserved u8` | F 6 |
| 0x07 | TRAJ | H→B | `cmd_id u32`, `flags u8` (bit0 FROM_CURRENT, bit1 APPEND; 0 = JUMP/replace), `purpose u8` (0 program, 1 sync_glide, 2 recovery, 3 jog, 4 replay, 5 home, 6 rest; diagnostic only), `start_at_us u64` (0 = the cycle that applies it), `n u16` (1..60), n × {`t_us u32` (offset from start, strictly increasing), `tick i16[6]`} | V 16+16n |
| 0x08 | SETPOINT | H→B | `seq u32`, `tick i16[6]` (no ACK; refusals as EVENT) | F 16 |
| 0x09 | MODE | H→B | `cmd_id u32`, `mode u8` (0 HOLD, 1 TELEOP, 2 SETPOINT, 3 TELEOP_ONBOARD reserved for O8), `reserved u8` — entry and exit change no goal (§5.4) | F 6 |
| 0x0A | STOP | H→B | `cmd_id u32`, `kind u8` (0 PATH_DECEL, 1 HOLD_COMMANDED, 2 HOLD_MEASURED), `reserved u8` | F 6 |
| 0x0B | CLEAR_LATCH | H→B | `cmd_id u32`, `mask u8` (latch bits of §5.5), `reserved u8` | F 6 |
| 0x0C | CONFIG | H→B | `cmd_id u32`, `key u8` (1 COLLISION_SCOPE_ADD, 2 LINK_TIMEOUT_MS, 3 TELEOP_TIMEOUT_MS), `reserved u8`, `value i32` — tighten only; reset at boot | F 10 |

### 3.3 Board → host stream
| id | name | fields | size |
|---|---|---|---|
| 0x43 | STATE (every cycle, 100 Hz) | `board_us u64` (read completion), `cycle u32`, `kernel_state u8`, `mode u8`, `torque_mask u8`, `latch_mask u8`, `flags u8` (bit0 bus_fault, bit1 executing, bit2 detector_armed, bit3 link_ok, bit4 probe_ok), `missed_mask u8`, `last_cmd_id u32`, `queue_free u16`, `horizon_us u32`, `pos i16[6]`, `vel i16[6]` (steps/s, sign-magnitude bit 15 decoded), `load i16[6]` (±1000, sign bit 10 decoded), `goal i16[6]` (the goal written this cycle, or the held goal), `status u8[6]`; O8 extension (only with `BP_FEATURE_ONBOARD_TELEOP`): `leader_ok_mask u8`, `leader_pos i16[6]`, `action_tick i16[6]` | E 82 |
| 0x44 | HEALTH (10 Hz) | `board_us u64`, 6 × {`voltage_dv u8`, `temp_c u8`, `current_raw u16`, `status u8`, `torque_enable u8`, `age_cycles u16`}, `rx_bad_frames u16`, `bus_corrupt u16`, `bus_missed u16`, `cycle_overruns u16`, `max_cycle_us u16` (counters since the previous HEALTH) | E 66 |
| 0x46 | EVENT | `board_us u64`, `code u16`, `cmd_id u32`, `joint_mask u8`, `reserved u8`, `data i32[4]` | E 32 |
| 0x47 | LOG | `level u8`, `len u8`, `text char[len ≤ 200]` (English) | V |

### 3.4 Acknowledgement rules
- Every command with a `cmd_id` gets exactly one ACK, sent in the bus cycle that applies or refuses it (≤ one cycle
  after receipt plus the link task's latency). TRAJ is acknowledged when validated and queued; its completion is the
  EVENT `traj_done`. ENERGISE is acknowledged after step (8) of §5.3 or its refusal.
- SETPOINT and HEARTBEAT are never acknowledged; a refused SETPOINT raises EVENT `setpoint_refused` at most once per
  second.
- The arm link waits 100 ms for an ACK and re-sends the **same** `cmd_id` at most twice (safe by the duplicate rule);
  then it reports `busy` to the engine and drops the link to `connecting`.
- ERROR is reserved for protocol-level faults (`session`, `bad_length`, `unknown_type`, `not_in_variant`); it never
  replaces an ACK for a well-formed command.

### 3.5 Provisioning passthrough (`prod` and `bench`; never used by the arm link)
| id | name | dir | fields |
|---|---|---|---|
| 0x10 | PROV_CHALLENGE | H→B | `cmd_id u32` |
| 0x50 | PROV_CHALLENGE_ACK | B→H | `cmd_id u32`, `nonce u8[32]` (from `esp_fill_random`), `mac u8[6]` |
| 0x11 | PROV_UNLOCK | H→B | `cmd_id u32`, `sig_len u8`, `sig u8[sig_len ≤ 72]` = DER ECDSA-P256-SHA256 over `nonce ‖ mac ‖ "EDUBOTICS-PROVISION-V1"` |
| 0x12 | PASSTHROUGH | H→B | `cmd_id u32`, `timeout_us u16` (≤ 50 000), `expected_replies u8` (0..1), `pkt_len u8`, `pkt u8[pkt_len ≤ 250]` = one Feetech packet |
| 0x52 | PASSTHROUGH_REPLY | B→H | `cmd_id u32`, `n_replies u8`, `reserved u8`, `total_len u16`, `bytes u8[total_len ≤ 600]` |
| 0x13 | PASSTHROUGH_END | H→B | `cmd_id u32` → ACK, then reboot |

The UNLOCK signature is made on the owner's machine by `tools/feetech_provision.py --transport board
--authority-key <path on the owner's encrypted medium>` through the host `openssl dgst -sha256 -sign` (DER, as sent);
the private key never leaves that medium and no CI run provisions (owner Q-B3, main spec D-B19).
Rules: UNLOCK only from LIMP with every servo reading `Torque_Enable 0` (board reads them); a wrong signature →
`bad_signature` (and a 2 s lock-out); PASSTHROUGH only while unlocked; the board parses `pkt`: allowed instructions
PING, READ, WRITE; refused: SYNC_WRITE, SYNC_READ, BROADCAST id, a WRITE whose span reaches register 40 with a value
≠ 0, any WRITE to register 5 (ID); refusal `passthrough_forbidden`. The 100 Hz loop is stopped while unlocked. 10 min
without a frame → reboot. These rules admit exactly the ordered sequence of `tools/feetech_provision.provision`
(PING, READ, the un-awaited Lock/Response_Status_Level repair, `Torque_Enable 0`, Lock, Phase, Angular_Resolution,
Homing_Offset, limits, safety EEPROM, re-lock, read-backs). A replacement follower servo gets its id 1–6 on a plain
adapter (`feetech_provision.py assign-id`) before it is mounted; the board never writes register 5.

### 3.6 Update (DFU)
| id | name | dir | fields |
|---|---|---|---|
| 0x20 | DFU_BEGIN | H→B | `cmd_id u32`, `size u32`, `sha256 u8[32]`, `build_number u32` |
| 0x21 | DFU_DATA | H→B | `cmd_id u32`, `offset u32`, `len u16`, `data u8[len ≤ 960]` |
| 0x22 | DFU_END | H→B | `cmd_id u32` |
| 0x23 | REBOOT | H→B | `cmd_id u32` |

Rules: BEGIN only in LIMP or idle HOLD (no executor, no teleop/setpoint, no latch other than `link_lost`), never in
PASSTHROUGH; `build_number ≤ max_valid_build` → `downgrade`; size ≤ slot size. DATA must arrive at `offset =
bytes_written` (else ACK `dfu_offset` with `detail` = expected offset / 1024; the host resumes there). The first
DATA chunk containing the image's `esp_app_desc_t` is checked: its build number (stored in `project_name`'s
companion field `BP_BUILD_NUMBER` embedded by the build) must equal BEGIN's. END: sha256 of the written bytes must
equal BEGIN's (`dfu_sha_mismatch`), then `esp_ota_end` verifies the ECDSA signature (`dfu_signature`), then
`esp_ota_set_boot_partition`; ACK ok. REBOOT → `esp_restart`. The new image boots PENDING_VERIFY; it calls
`esp_ota_mark_app_valid_cancel_rollback()` and writes `max_valid_build` to NVS only after its self-test
(kernel initialised, bus task ran 100 cycles, frame codec self-check) **and** the first HELLO; without a HELLO it
stays pending, and the next reset rolls back. Flash writes stall both cores (ESP-IDF flash concurrency): the bus
monitor (§5.2) is relaxed during UPDATE.

### 3.7 Diagnostics (`diag`, `bench` only; `prod` answers `not_in_variant`)
| id | name | dir | fields |
|---|---|---|---|
| 0x30 | DIAG_BUS_TIMING | H→B | `cmd_id u32`, `shape u8` (1 SR(56,6) ids 1–6; 2 SR(56,15); 3 READ(62,9)+READ(40,1) one servo; 4 shape 1 + SW(41, 1 B per servo, the value read back first); 5 shape 2 + shape 4), `reserved u8`, `cycles u16`, `period_us u32` |
| 0x53 | DIAG_RESULT | B→H | `cmd_id u32`, `shape u8`, `reserved u8`, `n u16`, `hist u16[32]` (cycle µs in 50 µs bins), `p50_us u32`, `p99_us u32`, `max_us u32`, `missed u32`, `corrupt u32`, `t_first_byte_median_us u16[6]` |
| 0x31 | DIAG_ECHO | H→B | `cmd_id u32`, `len u8`, `data u8[len ≤ 64]` |
| 0x54 | DIAG_ECHO_REPLY | B→H | same as DIAG_ECHO |
| 0x32 | DIAG_DAMP | H→B (`bench` only) | `cmd_id u32`, `mask u8`, `reserved u8`, `torque_limit u16` → writes `Torque_Limit (48)` then `Torque_Enable 2` for the mask |
| 0x33 | DIAG_STATE_RING | H→B (`bench`, `diag`) | `cmd_id u32`, `from_index u16`, `count u16` → answered by up to `count` frames 0x55 |
| 0x55 | DIAG_STATE_RING_ITEM | B→H | `cmd_id u32`, `index u16`, `board_us u64`, `kernel_state u8`, `mode u8`, `latch_mask u8`, `torque_mask u8`, `pos i16[6]`, `goal i16[6]` (the `bench`/`diag` firmware keeps the last 1 000 such 36-byte snapshots = 10 s, 36 KB, in a static RAM ring written by the bus task — no allocation; used by B4 to see what happened while the host was gone) |

### 3.8 IPC family (engine ⇄ arm link; never on the serial link)
Transport: `multiprocessing.connection` (`send_bytes`), body = the same `header ‖ payload ‖ crc32` (no COBS). Types
0x80–0xBF; `IPC_VERSION` must be equal on both sides. Radians are `f64`, joint order `joint1..joint5, RL_joint`.

| id | name | dir | fields |
|---|---|---|---|
| 0x80 | I_HELLO | E→A | `ipc_version u16`, `engine_pid u32` |
| 0x81 | I_HELLO_ACK | A→E | `ipc_version u16`, `armlink_version char[16]` |
| 0x82 | I_HEARTBEAT | E→A | `seq u32`, `sensor_age_ms u16` (diagnostic only; the engine sends nothing when its sensor age ≥ 250 ms, main spec §8.5; handed to the client's writer through a latest-wins slot, never written on the caller's thread, main spec §8.9) |
| 0x83 | I_REQUEST | E→A | `req u32`, `op u8` (energise, torque_off, traj, mode, stop, clear_latch, config, teleop_prepare, teleop_on, teleop_off, fw_info, fw_update_begin, fw_update_data, fw_update_end, shutdown), op-specific payload (traj: `flags u8`, `purpose u8`, `n_names u8`, `n_names × name` (u8 length + ASCII ≤ 32), `n_points u16`, n_points × {`t_s f64`, `n_pos u8`, `pos f64[n_pos]` (n_pos ≤ n_names)} — today's rail message shape, main spec §8.6; others as §3.2) |
| 0x84 | I_SETPOINT | E→A | `seq u32`, `q f64[6]` |
| 0x85 | I_REPLY | A→E | `req u32`, `code u16`, `detail u16`, op-specific payload (teleop_prepare: `follower_q f64[6]`, `leader_action_q f64[6]`, `follower_age_s f64`, `leader_age_s f64`, `leader_ok u8`; fw_info: the HELLO_ACK fields) |
| 0x86 | I_FOLLOWER | A→E | `board_us u64`, `arrival_mono_ns u64`, `cycle u32`, `kernel_state u8`, `mode u8`, `torque_mask u8`, `latch_mask u8`, `flags u8`, `missed_mask u8`, `last_cmd_id u32`, `queue_free u16`, `q f64[6]`, `dq f64[6]`, `load_frac f32[6]`, `goal_tick i16[6]`, `goal_q f64[6]`, `status u8[6]` |
| 0x87 | I_LEADER | A→E | `request_mono_ns u64`, `arrival_mono_ns u64`, `seq u32`, `ok_mask u8`, `leader_tick i16[6]`, `action_q f64[6]` (mapped, unclamped), `status u8[6]` |
| 0x88 | I_HEALTH | A→E | HEALTH fields + `arrival_mono_ns u64` |
| 0x89 | I_EVENT | A→E | `board_us u64` (0 for arm-link events), `arrival_mono_ns u64`, `code u16`, `source u8` (1 board, 2 arm link), `joint_mask u8`, `cmd_id u32`, `data i32[4]` |
| 0x8A | I_LINK_STATE | A→E | `follower u8`, `leader u8` (0 absent, 1 connecting, 2 ok, 3 lost), `code u16` |
| 0x8B | I_TELEOP_STATE | A→E | `state u8` (0 OFF, 1 PREPARED, 2 ON, 3 LOST), `code u16` |

---

## 4. Codes and German sentences

### 4.1 Codes (u16; names are the stable contract)
| range | codes |
|---|---|
| 1–49 refusals (ACK result / I_REPLY code) | 1 `torque_off`, 2 `bus_fault`, 3 `bad_mode`, 4 `out_of_window`, 5 `time_order`, 6 `queue_full`, 7 `collision_latched`, 8 `latched`, 9 `edge`, 10 `read`, 11 `wrongway`, 12 `read_after_energise`, 13 `provision_fault`, 14 `not_probed`, 15 `not_limp`, 16 `not_unlocked`, 17 `bad_signature`, 18 `downgrade`, 19 `busy`, 20 `session`, 21 `config_loosen`, 22 `bad_length`, 23 `unknown_type`, 24 `dfu_state`, 25 `dfu_sha_mismatch`, 26 `dfu_signature`, 27 `dfu_offset`, 28 `passthrough_forbidden`, 29 `not_in_variant`, 30 `executing`, 31 `app_too_old`, 32 `firmware_too_old`, 33 `torque_off_failed` |
| 50–99 arm-link refusals | 50 `non_finite`, 51 `teleop_not_synced`, 52 `leader_absent`, 53 `leader_fingerprint`, 54 `leader_torqued`, 55 `engine_silent`, 56 `port_busy`, 57 `no_board`, 58 `multiple_followers`, 59 `follower_on_leader_port`, 60 `leader_on_follower_bus`, 61 `board_bus_silent`, 62 `unconverted_follower`, 63 `bench_firmware`, 64 `ipc_version`, 65 `teleop_prepare_stale`, 66 `unknown_joints`, 67 `empty_trajectory` (not displayed; the driver ignores an empty message silently), 68 `time_range`, 69 `armlink_gone` (client side: the arm-link process ended or its pipe broke, main spec §8.9) |
| 100–199 events | 100 `boot`, 101 `traj_done`, 102 `stop_done`, 103 `link_lost`, 104 `teleop_lost`, 105 `setpoint_lost`, 106 `bus_fault_latched`, 107 `bus_ok`, 108 `servo_torque_lost`, 109 `servo_torque_unexpected`, 110 `collision`, 111 `runaway_torque_off`, 112 `tracking_stop`, 113 `status_bits`, 114 `health_warn`, 115 `probe_result`, 116 `fw_note`, 117 `setpoint_refused`, 118 `leader_lost`, 119 `leader_ok`, 120 `idle_release` (bench) |
| 200–219 probe sub-codes (`fp_code`, `probe_code`) | 200 `ok`, 201 `missing`, 202 `model`, 203 `read_failed`, 204 `mode`, 205 `phase`, 206 `window`, 207 `band`, 208 `edge_at_boot`, 209 `max_torque`, 210 `protection_current`, 211 `overload_torque`, 212 `return_delay`, 213 `response_level`, 214 `lock`, 215 `all_silent`, 216 `leader_ids_present`, 217 `extra_ids_present`, 218 `torque_limit_ram` (leader only: RAM `Torque_Limit` (48) ≠ 0) |

### 4.2 Arm-link discovery (B13)
Per candidate port (VID:PID ∈ `ARMLINK_USB_IDS`), opened with DTR/RTS deasserted before `open()`:
1. write `0x00` + PROBE; PROBE_ACK within 150 ms → **follower board** (then HELLO).
2. else Feetech PING 11..16 (20 ms each); all six answer with an accepted leader model → **leader adapter**; some
   answer → `leader_fingerprint` (partial); PING 1..6 answer → `unconverted_follower` (a follower on a plain adapter;
   or, if a board was already found, `follower_on_leader_port`).
3. else `port silent` (diagnostic only).
Verdicts: no board → `no_board`; board found but its HELLO_ACK `probe_code` = `all_silent` → `board_bus_silent`;
`leader_ids_present` in the board's probe → `leader_on_follower_bus`; two boards → `multiple_followers`; open fails
with access denied → `port_busy`; HELLO_ACK `variant` ≠ prod without `--bench` → `bench_firmware`.

### 4.3 German sentences (`texts_de.py`)
**Ported byte-equal** (lockstep test renders both for the same inputs): `edge` = `edge_refusal_message(o,
self_healing=False)`; `edge_at_boot` = `edge_refusal_message(o, self_healing=True)`; `wrongway` =
`wrong_way_abort_message`; `read_after_energise` = `post_energise_read_failure_message`; `read` = the seed-read
refusal in `_seed_goal_from_present_locked`; `missing`, `model`, `band` = the `probe_bus` strings; `bus_fault_latched`
= the `_read_tick` latch string; `bus_ok` = „Servobus wieder erreichbar — Bewegungen sind wieder möglich.“;
`torque_off` = „[WARNUNG] Trajektorie verworfen: Drehmoment ist aus (Hand-Führung aktiv?).“; `bus_fault` =
„[WARNUNG] Trajektorie verworfen: Servobus antwortet nicht (siehe Fehlermeldung oben).“; `non_finite` = the driver's
NaN refusal; `unknown_joints` = „[WARNUNG] Trajektorie ohne bekannte Gelenknamen ignoriert (erhalten: {names}).“
(`_trajectory_cb`); `empty_trajectory` has no sentence (it is the one non-displayed code; the lint test lists it as the
only exemption); `status_bits` = „[WARNUNG] Servo {sid} meldet: {describe_error_bits(e)} (Status {e:#04x}).“;
`torque_off_failed` = the driver's „FEHLGESCHLAGEN … 12-V-Netzteil … ausschalten“ string; `fw_note` (firmware) =
`firmware_fingerprint_notes`; the two `BOOT_PHYSICAL_REMEDY_DE` entries (keys `remedy_edge`, `remedy_wrongway`).

**Reworded** (the driver's sentence names a bench tool, an env var or a Python exception text): `read_failed`,
`mode`, `phase`, `window`, `fw_note` (angular resolution) → keep the first clause byte-equal, replace the tail with
„Bitte EduBotics kontaktieren: der Arm muss neu provisioniert werden.“ and drop `({e})`.

**New** (drafts; the coder may polish, the lint test binds):
- `board_bus_silent`: „[FEHLER] Die Steuerplatine des Arms antwortet, aber kein Servo meldet sich. Ist das 12-V-Netzteil eingesteckt und eingeschaltet? Steht der kleine Schiebeschalter auf der Platine in der richtigen Stellung?“
- `max_torque`/`protection_current`/`overload_torque`/`return_delay`/`response_level`/`lock`: „[FEHLER] Servo {sid}: Sicherheitswert im EEPROM (Register {reg}) ist {read} statt {expected} — dieser Arm ist nicht korrekt provisioniert. Bitte EduBotics kontaktieren.“
- `link_lost`: „[FEHLER] Die Verbindung zum Roboterarm war länger als eine Sekunde unterbrochen. Der Arm hat angehalten und hält seine Stellung.“
- `teleop_lost`: „[WARNUNG] Die Teleoperation wurde unterbrochen. Der Arm hält seine Stellung. Zum Weitermachen die Teleoperation neu starten.“
- `setpoint_lost`: „[WARNUNG] Die Steuerung hat länger als eine Sekunde keine Befehle geschickt. Der Arm hält seine Stellung.“
- `servo_torque_lost`: „[FEHLER] Servo(s) {ids} haben ihr Drehmoment verloren (Stromversorgung kurz unterbrochen?). Der Arm hält an. Bitte das 12-V-Netzteil prüfen und den Arm neu aktivieren.“
- `collision`: „[STOPP] Kollision erkannt (Gelenk {joints}). Der Arm hat angehalten und gibt nach.“
- `runaway_torque_off`: „[FEHLER] Gelenk {joint} wäre über den Rand des Positionsbereichs (±180°) in die falsche Richtung gelaufen. Sein Drehmoment wurde sofort abgeschaltet — dieses Gelenk ist jetzt weich. Bitte das Gelenk von Hand ein Stück Richtung Mitte drehen und den Arm neu aktivieren.“
- `tracking_stop`: „[STOPP] Gelenk {joint} kommt seiner Zielstellung nicht nach (blockiert oder überlastet). Der Arm hat angehalten und hält seine Stellung.“
- `health_warn` (temperature): „[WARNUNG] Servo {sid} ist heiß ({temp} °C). Bitte den Arm ablegen und abkühlen lassen.“; (current): „[WARNUNG] Servo {sid} zieht seit über zehn Sekunden viel Strom ({amps} A). Bitte den Arm entlasten.“; (voltage): „[WARNUNG] Die Versorgungsspannung ist niedrig ({volts} V). Bitte das 12-V-Netzteil prüfen.“
- `teleop_not_synced`: „Follower- und Leader-Arm stehen noch nicht gleich (Abweichung über 17°). Bitte den Leader-Arm ruhig halten und die Teleoperation erneut starten.“
- `leader_absent`: „Der Leader-Arm wurde nicht gefunden. Bitte sein USB-Kabel und sein 12-V-Netzteil prüfen.“
- `leader_torqued`: „Ein Servo des Leader-Arms ist bestromt, obwohl der Leader-Arm immer weich sein muss. Bitte das 12-V-Netzteil des Leader-Arms kurz aus- und wieder einschalten.“
- `leader_fingerprint`: „Der Leader-Arm ist nicht korrekt eingerichtet (Servo {sid}). Bitte EduBotics kontaktieren.“
  (shown for every leader `fp_code` except `torque_limit_ram`; a leader with `max_torque` — not provisioned limp —
  needs re-provisioning, hence the contact)
- `torque_limit_ram`: „Der Leader-Arm ist nicht weich geschaltet (Servo {sid}: Drehmomentgrenze im Arbeitsspeicher {read} statt 0). Bitte das 12-V-Netzteil des Leader-Arms kurz aus- und wieder einschalten.“
- `port_busy`: „Der Roboterarm wird gerade von einem anderen Programm oder einem anderen Windows-Konto verwendet. Bitte dieses Programm schließen.“
- `no_board`: „Es wurde kein EduBotics-Roboterarm gefunden. Bitte das USB-Kabel des Follower-Arms prüfen.“
- `multiple_followers`: „Es sind zwei Follower-Arme angeschlossen. Bitte nur einen anschließen.“
- `follower_on_leader_port` / `leader_on_follower_bus`: „Leader- und Follower-Arm sind vertauscht angeschlossen. Bitte die Kabel prüfen.“
- `unconverted_follower`: „Dieser Arm hängt an einem einfachen USB-Adapter. Für die App braucht der Follower-Arm die EduBotics-Steuerplatine.“
- `bench_firmware`: „Auf dem Roboterarm läuft eine Werkstatt-Firmware. Bitte EduBotics kontaktieren.“
- `app_too_old`: „Die Firmware des Roboterarms ist neuer als diese App. Bitte die App aktualisieren.“
- `firmware_too_old`: „Die Firmware des Roboterarms muss aktualisiert werden.“
- `engine_silent`: „Die Teleoperation wurde angehalten, weil die App nicht mehr reagiert hat.“
- `downgrade`: „Diese Firmware ist älter als die auf dem Arm. Ältere Firmware wird nicht aufgespielt.“
- `time_range`: „[WARNUNG] Trajektorie verworfen: sie ist länger als 71 Minuten.“
- `armlink_gone`: „Die Verbindung zur Armsteuerung ist unterbrochen. Der Arm hält seine Stellung.“
Every other code gets a sentence in the same style; the lint test fails on a missing one.

---

## 5. Kernel

### 5.1 States (`kernel_state`)
`BOOT_PROBING` → {`BUS_SILENT`, `PROVISION_FAULT`, `LIMP`, `HOLD` (adopted), `MIXED_TORQUE`}; `LIMP`, `HOLD`,
`MOVING`, `STOPPING`, `TELEOP`, `SETPOINT`, `PASSTHROUGH`, `UPDATE`. Latches (orthogonal, `latch_mask`): bit0
`COLLISION`, bit1 `LINK_LOST`, bit2 `TELEOP_LOST`, bit3 `SETPOINT_LOST`, bit4 `TRACKING_STOP`, bit5 `BUS_FAULT`,
bit6 `SERVO_TORQUE_LOST`, bit7 `RUNAWAY`. `BUS_FAULT` clears itself on the first full read (driver semantics);
`RUNAWAY` and `SERVO_TORQUE_LOST` clear on the next successful ENERGISE; the others only by CLEAR_LATCH.

Boot (read-only): ping 1..16 (12 ms each, bounded 200 ms); for 1..6 read model, `Operating_Mode`, `Phase`,
`Min/Max_Position_Limit`, `Present_Position`, firmware 0/1, `Angular_Resolution`, `Max_Torque_Limit`,
`Protection_Current`, `Overload_Torque`, `Return_Delay`, `Response_Status_Level`, `Lock`, `Torque_Enable`. No write of
any kind. All `Torque_Enable 1` → belief on, `HOLD` (adopted; servos keep their goals). All 0 → `LIMP`. Mixed →
`MIXED_TORQUE` (motion refused; ENERGISE and TORQUE_OFF allowed). Fingerprint failure → `PROVISION_FAULT` with the
adopted mask (only TORQUE_OFF allowed). Nothing answers → `BUS_SILENT`. Re-probe every 1.0 s while not OK (the
driver retried every 5 s; faster costs nothing, reads only). Ids 11–16 answering → `probe_code leader_ids_present`
(refusal of ENERGISE unless O8 build); id 7 or other extras → `extra_ids_present` (refusal).

Command acceptance: ENERGISE in LIMP/HOLD/MOVING/MIXED_TORQUE and under a `BUS_FAULT` latch (the seed read decides,
as in the driver), not in PROVISION_FAULT, BUS_SILENT, PASSTHROUGH or UPDATE (`not_probed` / `provision_fault` /
`bad_mode`); TORQUE_OFF always; TRAJ in HOLD/MOVING/STOPPING with every joint torqued and no latch set (a limp joint →
`torque_off`; `BUS_FAULT` → `bus_fault`; `COLLISION` → `collision_latched`; any other latch → `latched`; TELEOP/
SETPOINT → `bad_mode`); MODE TELEOP/SETPOINT in HOLD with full torque and no latch (else `latched` / `torque_off` /
`bad_mode`); MODE HOLD always when torqued (from TELEOP/SETPOINT it is HOLD_COMMANDED, from HOLD a no-op; never a
latch); STOP whenever torqued (in TELEOP/SETPOINT any STOP kind also leaves the mode for HOLD, without a latch);
CLEAR_LATCH always; CONFIG always (tighten only); DFU/PROV per §3.5–§3.6.

### 5.2 The cycle (bus task, core 1, every 10 ms)
1. `SYNC_READ(56, 6, ids 1..6)`; per reply decode pos/vel/load + status byte; per-reply deadline = expected bytes ×
   10 µs + 500 µs; a missing reply ends the transaction (driver tail-break); one immediate per-id READ retry for missing
   ids within the same cycle (LeRobot #4207 lesson); stamp `board_us` at completion.
2. Health round-robin: servo `(cycle mod 6) + 1`: `READ(62, 9)` and `READ(40, 1)`.
3. Read-fail latch, brown-out check (§5.5), watchdogs, guards (§7), executor/teleop goal for this cycle.
4. If any torqued joint needs a goal this cycle: `SYNC_WRITE(41, 7 B)` = `Acceleration` (per-mode table, default 50),
   goal (per-cycle command clamp, §5.4), `Goal_Time 0`, `Goal_Speed 2840`; only for joints in the torque mask (P6).
   In idle HOLD and LIMP nothing is written.
5. Publish the STATE snapshot (double buffer); HEALTH every 10th cycle.
The link task's bus monitor restarts the chip if the cycle counter stalls 200 ms (not in UPDATE); TWDT 1 s on the
bus task. A cycle overrun (> 10 ms) skips to the next slot and counts `cycle_overruns`.

### 5.3 Torque choke point (ENERGISE), one cycle, synchronous
Port of `FeetechArmNode._torque_cb(True)` + `set_torque(True)` (the driver does **not** refuse a torque-on because the
bus-fault latch is up; it lets the seed read decide, and the latch only widens the edge gate): (1) queue cleared;
(2) `SYNC_READ(56, 2)`; any missing → `read` (nothing written); (3) transition = not every joint believed on, **or**
`BUS_FAULT` was latched at the start of this cycle → `edge_parked(present, 40)` non-empty → `edge` (nothing written);
(4) seed: `SYNC_WRITE(41, 7 B)` with `Acceleration 50`, goal =
`clamp(present, 0, 4095)` (exempt from the command clamp), `Goal_Time 0`, `Goal_Speed 200`; (5) effective goals =
`clamp(seed, window_lo, window_hi)`; (6) `SYNC_WRITE(40, 1)` = 1 for all six; (7) up to 2 × `SYNC_READ(56, 2)`;
`wrong_way(effective, present, 2048)` non-empty or any missing → `SYNC_WRITE(40, 0)` to all six from the prebuilt
payload (nothing between the verdict and the write), belief off, `wrongway` / `read_after_energise`; (8) belief on,
`HOLD`. A redundant ENERGISE over a held arm re-seeds `Goal = Present` exactly like today's redundant
`set_torque(True)` (the server asserts torque on every jog). TORQUE_OFF: queue cleared, `SYNC_WRITE(40, 0)` for the
mask, belief off for the mask; never refused.

### 5.4 Executor and stop generator
- Queue: ring of 512 waypoints `(t_us int64 absolute board time, tick i16[6])`.
- JUMP: replace the queue; `base = start_at` (0 → the applying cycle's `board_us`); before `base + t_0` the goal is
  `p_0`. FROM_CURRENT: drop queued points at or after `start_at`, insert the virtual point `(start_at, current_goal)`.
  APPEND: append after the last point (times must exceed it).
- Goal at board time τ between `(t_a, p_a)` and `(t_b, p_b)`: `p_a + div_round_half_away((p_b − p_a)·(τ − t_a),
  t_b − t_a)` (int64); after the last point: `p_last` (hold), event `traj_done` once.
- Per-cycle command clamp (G8): written goal = `clamp(goal, m, 4095 − m)`, `m = 128` (ceiling 130); the seed and
  HOLD_MEASURED of a joint resting inside the band are exempt (a guard must not create motion), exactly as today.
- PATH_DECEL: on STOP(0) or a link-loss stop, `v_j = (g_k − g_{k−1})·10⁶ / dt_us` (truncation toward zero),
  `T_stop = ceil(max_j |v_j| / A_STOP)` with `A_STOP = 5000 ticks/s²` (the `Acceleration 50` profile), capped 2.0 s;
  rate `r` (Q16, 65536 = 1) falls by `ceil(65536·dt/T_stop)` per cycle to 0; τ advances by
  `(dt·(r_old + r_new)) >> 17`; at r = 0 → HOLD at the current goal, event `stop_done`. If the path ends first, it ends
  there. **Stop bound (P4):** link loss detected at last-valid-frame + 1.0 s (± 1 cycle); HOLD reached ≤ T_stop + 1
  cycle later; at the 2840 ticks/s cap T_stop = 0.568 s and the remaining path ≤ 807 ticks (the driver's own figure).
- HOLD_COMMANDED: queue cleared, goal kept. HOLD_MEASURED: goal := last measured position (per joint, torqued only),
  queue cleared.
- TELEOP/SETPOINT: the goal is the newest accepted SETPOINT (per-cycle clamp); no buffering; a far setpoint is
  executed as is, shaped only by `Goal_Speed`/`Acceleration` (owner Q10). Setpoint ticks outside the EEPROM window →
  dropped, event `setpoint_refused` (rate-limited 1/s). **MODE entry** (HOLD → TELEOP/SETPOINT) keeps the held goal: until
  the first SETPOINT of the mode arrives the cycle writes exactly that goal, so entering a mode moves nothing; it also
  starts TELEOP's stream clock at the applying cycle. **MODE HOLD** from TELEOP/SETPOINT = HOLD_COMMANDED: the goal of
  the last cycle is kept with the torque unchanged, no latch, nothing new written (idle HOLD writes nothing, §5.2).
  So leaving and re-entering SETPOINT needs no CLEAR_LATCH and causes no goal change of its own; the first setpoint
  after a re-entry is executed as any setpoint is.

### 5.5 Watchdogs, latches, brown-out
- Link (heartbeat) watchdog: `last_hb` = board time of the last HEARTBEAT of the session (§2: nothing else feeds it).
  `now − last_hb ≥ T_LINK` (1 000 000 µs; CONFIG may lower to ≥ 200 000) → in MOVING/STOPPING: PATH_DECEL, latch
  `LINK_LOST`; in TELEOP: HOLD_COMMANDED, mode HOLD, latch `TELEOP_LOST`; in SETPOINT: HOLD_COMMANDED, mode HOLD, latch
  `SETPOINT_LOST`; each with its event, `data[0]` = 1 (cause: heartbeat). Idle HOLD/LIMP: no action (nothing moves).
- Stream watchdog, **TELEOP only**: `now − max(last_setpoint, mode_entry) ≥ T_STREAM` (1 000 000 µs; CONFIG ≥ 200 000)
  → HOLD_COMMANDED, mode HOLD, latch `TELEOP_LOST`, event `data[0]` = 2 (cause: relay stream). The relay must stream;
  heartbeats alone never keep TELEOP alive.
- **SETPOINT has no stream watchdog** (M18): an action gap of any length while heartbeats arrive keeps SETPOINT and its
  last goal (Cloud-Inferenz legitimately publishes nothing for up to `stall_end_s` 15 s and the arm holds its last
  commanded pose, as today's V1 driver holds its last trajectory point). A dead engine or arm link stops the
  heartbeats and trips the link watchdog 1.0 s after the last one, whatever setpoints still arrive (reference model:
  main spec §12 P34).
- Re-entering TELEOP or SETPOINT after one of these latches requires CLEAR_LATCH then MODE (CLEAR_LATCH never moves;
  for TELEOP the engine adds the student's action and the sync glide).
- Read-fail (G11, port): any servo missing after the retry → `fail_since` (first miss); `now − fail_since ≥ 1 000 000
  µs` → latch `BUS_FAULT`, queue cleared, writes silenced, event `bus_fault_latched`; cleared only by a full read whose
  STATE was published, event `bus_ok`.
- Brown-out: the round-robin `READ(40)` of a joint believed on reads 0 → belief off for it, latch
  `SERVO_TORQUE_LOST`, HOLD_COMMANDED for the rest, event (no automatic re-energise). Believed off but reads 1 → event
  `servo_torque_unexpected` only.

### 5.6 On-board teleop (conditional on O8; B17)
`BP_FEATURE_ONBOARD_TELEOP`: the probe expects ids 11–16; the cycle reads `SYNC_READ(56, 2, 11..16)` after the follower
read; MODE 3 requires |follower − mapped leader| ≤ 0.30 rad (in ticks: 196) on all six; the goal = the mapped leader
ticks (generated per-joint table `offset, sign, ratio_q16`, claw by a 16-point piecewise-linear table); the STATE
extension carries `leader_pos` and `action_tick` with the same `board_us`; the write mask is computed from the
follower torque mask and can never include 11–16 (structural, B17 test).

---

## 6. Ported guards

### 6.1 Catalogue (driver symbol → firmware home → vector family)
| G | Driver rule (symbol, constant) | Firmware | Vectors |
|---|---|---|---|
| G1 | `probe_bus`: ping all, model ∈ {777, 2825} | boot probe | `probe` |
| G2 | fingerprint: mode 0, Phase bit 4 clear, windows = `position_limit_window` ± 1 | `fingerprint_check` | `probe` |
| G3 | boot band `BOOT_POSITION_TOLERANCE_TICKS` 400 | `boot_band_ok` | `band` |
| G4 | edge refusal `EDGE_REFUSE_MARGIN_TICKS` 40, transition-gated (`_torque_on and not _bus_fault`) | `edge_parked` + §5.3(3) | `edge`, `edge_gate` |
| G5 | seed Goal = Present, `SEED_SPEED_STEPS` 200, acc 50, exempt from the clamp; effective = window clamp | §5.3(4–5) | `seed` |
| G6 | post-energise abort `TORQUE_ABORT_ERROR_TICKS` 2048 strict `>` and `wrapped < naive`; 2 reads | `wrong_way` + §5.3(7) | `wrong_way` |
| G7 | command clamp `GOAL_EDGE_MARGIN_TICKS` 128, ceiling 130 | per-cycle clamp | `clamp` |
| G8 | register-40 span rule {0,1} on WRITE and each SYNC_WRITE block | `bp_check_safe_torque_enable` | `reg40_span` |
| G9 | TRAJ refused while limp / bus-faulted / non-finite | acceptance + arm link (`non_finite`) | `traj_accept` |
| G10 | radian clamp to `JOINT_LIMITS_RAD` | arm link `convert.py` with subdivision | `convert` |
| G11 | read-fail latch 1.0 s wall clock, cleared by a full published read | §5.5 | `read_fail` |
| G12 | status bits named, unknown bits by value | codes `status_bits` + `describe_error_bits` (PC) | `status_bits` |
| G13 | `Goal_Speed` 2840 + `Acceleration` 50 on every write | §5.2(4) | `write_block` |
| G14 | angular resolution ≠ 1 and firmware non-uniform / 3.9 → warnings only | HELLO_ACK fields + PC notes | `fw_notes` |
| G15 | torque-off belief rules (`_verify_after_energise_locked` sets off even if the drop failed; `set_torque` except-path keeps belief) | §5.3 | `choke_point` |

### 6.2 Extended EEPROM fingerprint (new refusals, PLAN Phase 2 "full EEPROM check")
Expected (from `tools/feetech_provision.py`, lockstep): `Max_Torque_Limit` 800 (ids 1–5) / 150 (id 6);
`Protection_Current` 310; `Overload_Torque` 80; `Return_Delay` 0; `Response_Status_Level` 1; `Lock` 1. Exact equality.
A provisioned arm already carries every one of these (the tool writes them; `Response_Status_Level` is normalised to
1; `Lock` written 1) — PLAUSIBLE until B9's dump of a provisioned arm confirms. Exact equality also means a follower
servo provisioned with the leader role's `Max_Torque_Limit` 0 (owner Q-B2) is refused `max_torque` at boot, and a
leader provisioned with follower values is refused by §6.5: the two roles cannot be swapped silently. Values left at factory
(`Protective_Torque`, `Protection_Time`, the over-current time, `Max_Temperature`, voltage limits, `Unload_Condition`)
are reported in B9 and the provisioning record, not checked.

### 6.3 Vector families and domains (generated by `gen_guard_vectors.py`, full domains)
- `edge`: tick 0..4095 × margin {0, 1, 39, 40, 41, 2046, 2047} → bool (+ the transition gate truth table).
- `wrong_way`: error −4095..4095 × threshold {0, 1, 401, 2047, 2048, 2049, 3000, 4094, 4095} → bool (73 719 cases).
- `clamp`: tick 0..4095 × margin {0, 1, 127, 128, 129, 130} → tick; plus `rad_to_command_tick` over 10⁵ seeded
  radians in [−4, 4] × signs ±1 (arm-link `convert`).
- `seed`: present −50..4145 (incl. out-of-register) × the six windows → (written tick, effective goal).
- `band`: present 0..4095 × windows × tolerance {0, 400, 4096}.
- `probe`: generated register sets (every single-field deviation from a provisioned servo — `Max_Torque_Limit` 0, the
  leader value, on each id included —, missing replies at each position, wrong model per id) → (ok, first failing
  servo, code) equal to `probe_bus`'s verdict order, extended by §6.2's checks after it.
- `reg40_span`: (addr 0..60, len 1..8, byte at 40 ∈ {0, 1, 2, 127, 128, 255}) → allowed.
- `read_fail`: miss timelines (cycle-level, injected time) → latch time and clear time.
- `choke_point`: the 12 B02 arms → the write sequence and the refusal code.
- `status_bits`: 0..255 → the list of named bits and the unnamed remainder (C side returns codes; text on the PC).
- `convert`: radian trajectories crossing the limits → subdivided tick waypoints; kernel goal stream ≤ 1 tick from the
  driver's.

### 6.4 Mutants (each must fail ≥ 1 vector; compiled with `-DBP_MUTANT=<n>`)
1 `naive >= th`; 2 `wrapped <= naive`; 3 edge `d <= m`; 4 edge without the transition gate; 5 clamp `4096 − m`;
6 clamp lower bound `m − 1`; 7 span `index <= len`; 8 allowlist accepts 2 in `prod`; 9 fingerprint window tolerance 2;
10 band `<` for `<=`; 11 read-fail latch at 0.99 s; 12 seed clamped by the command clamp.

### 6.5 Leader fingerprint (arm link, before PREPARE)
Ids 11–16 answer; models ∈ `LEADER_ACCEPTED_MODELS` (interim {777, 2825}); mode 0; Phase bit 4 clear; windows =
the leader spec (interim = the follower windows) ± 1; `Return_Delay 0`; `Response_Status_Level 1`; `Lock 1`.
**Structurally limp (owner Q-B2 = (a), main spec D-B20):** `Max_Torque_Limit` (16) = `LEADER_MAX_TORQUE` = 0 on all
six (else `leader_fingerprint` / `max_torque`) and RAM `Torque_Limit` (48) = 0 on all six (else `leader_fingerprint`
/ `torque_limit_ram`: the servo copies 16 into 48 at power-on — `feetech_bus.py`'s comment, marked "R2-verify",
ASSUMPTION until LEADER-LIMP — so after provisioning 48 still holds the factory value until the leader is
power-cycled, and a foreign RAM write shows here too); `Torque_Enable` reads 0 on all six (else `leader_torqued`).
Three READs per servo (16 2 B, 48 2 B, 40 1 B), at every PREPARE only, never on the 100 Hz relay path; the arm link
never writes to fix anything. Ids 1–6 answering on the leader port → `follower_on_leader_port`.

---

## 7. New guards (Rule §2; each signed off by the owner after its bench)

### 7.1 Runaway (B15)
Every cycle, every joint in the torque mask: `wrong_way(effective_goal_j, present_j, 2048)` with the effective goal =
the goal the servo holds (window-clamped written goal). True → `SYNC_WRITE(40, 0)` for that joint only (prebuilt
per-joint payloads), belief off for it, latch `RUNAWAY`, HOLD_COMMANDED for the rest, event `runaway_torque_off`.
Cannot fire in normal motion: the per-cycle clamp keeps every goal ≥ 128 ticks from the seam (G7) and the window
clamp cannot cross it (driver comment, "proven exhaustively").

### 7.2 Tracking stop (B15)
While MOVING/STOPPING/TELEOP/SETPOINT, per torqued arm joint (claw excluded): `e' = ((goal_eff − present + 2048) mod
4096) − 2048`; progress = `sign(e')·vel`; trip when `|e'| > E_j` and progress < `P_MIN` for `N_TRACK` consecutive
cycles → HOLD_MEASURED, latch `TRACKING_STOP`, event. Proposed until B5/VR12 records replace them: `E_j = 300` ticks
(0.46 rad), `P_MIN = 50` ticks/s, `N_TRACK = 50` (0.5 s). A far goal executed at speed never trips (progress high).

### 7.3 Idle damping release (bench variant only)
`BP_FEATURE_IDLE_DAMPING` (bench): after `T_IDLE = 120 s` of idle HOLD without a session frame, or on a temperature
`health_warn`, write `Torque_Limit (48) = TL` then `Torque_Enable 2`; event `idle_release`. Register-40 allowlist
{0,1,2} exists only in `bench`. `prod` never writes 2 (B15 test). Enabling in `prod` = a later owner decision after B8.

### 7.4 Gravity-compensated collision residual (B16)
- Gravity: float32 port of `feetech_geometry.limp_gravity_torques` (joint origins `_V1_J*_XYZ/RPY`, axes `(0,0,−1)`,
  masses/CoMs `V1_LINK_MASSES`, g 9.81) → τ2, τ3, τ4; τ1 = τ5 = 0. Reference tolerance 1·10⁻⁴ N·m; HOME =
  (0.934, −2.211, −0.413) N·m (§12 P7 of the main spec).
- Model per joint j = 1..5: `pred_j = a_j·τ_j + b_j·sign(ω_j)·[|ω_j| > ω0] + c_j·ω_j + d_j` in load units (±1000);
  residual `r_j = load_j − pred_j`; trip candidate when `|r_j| ≥ θ_j` and `|ω_j| ≤ 0.05 rad/s` (OMX gate kept until B6
  says otherwise); debounce 15 consecutive cycles (150 ms, the OMX `DEFAULT_DEBOUNCE_MS`); status bit 0x20 (overload)
  = immediate trip. Claw excluded. Disarmed for 0.5 s after ENERGISE, mode entry and CLEAR_LATCH
  (`COLLISION_SETTLE_WINDOW_S`).
- Scope: armed in a mode only if the mode ∈ scope; compiled default {TELEOP}; CONFIG `COLLISION_SCOPE_ADD` may add
  SETPOINT and/or PROGRAM (= MOVING/STOPPING), never remove (O10 decides the compiled default).
- Trip: source frozen (setpoints ignored, queue cleared), goal := measured (torqued joints), re-captured at +0.35 s and
  +0.70 s (the OMX `RELAX_RESEND_EVERY_S`, `RELAX_SENDS` 3, without the DDS delay `RELAX_DELAY_S`, which was a ROS
  artefact), latch `COLLISION`, event `collision` (joint mask, residuals in `data`). Recovery (glide to the V1 HOME,
  resync, the two German steps) is the engine's.
- Parameters `a,b,c,d,θ,ω0` per joint from `b6_fit.py` with a record id in `arm_spec.py`.

---

## 8. Build variants (`sdkconfig.defaults` + `.<variant>`)

Common (probed, main spec §12 P14): `CONFIG_IDF_TARGET="esp32"`, `CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y` (ASSUMPTION until
B9), `CONFIG_PARTITION_TABLE_CUSTOM=y`, `CONFIG_PARTITION_TABLE_OFFSET=0x10000`,
`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, `CONFIG_APP_REPRODUCIBLE_BUILD=y`,
`CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT=y`, `CONFIG_SECURE_SIGNED_APPS_ECDSA_SCHEME=y`,
`CONFIG_SECURE_SIGNED_ON_BOOT_NO_SECURE_BOOT=y`, `CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT=y`,
`CONFIG_SECURE_BOOT_BUILD_SIGNED_BINARIES=n` with `CONFIG_SECURE_BOOT_VERIFICATION_KEY` = the committed public key,
the same in **every** variant (the PEM is compiled into the bootloader and the app, main spec §12 P30, so every image
that boots — `diag` and `bench` too — is signed afterwards by the owner with `sign_release.sh`, on his machine, from
his offline medium, through the host `openssl` and espsecure's pre-calculated-signature path; CI, coders and agents
never hold the private key, owner Q-B3 — CONFIRMED buildable on v5.5.6, `espsecure.py sign_data --version 1` +
`verify_signature` report "Signature is valid", two signings give the identical file: main spec §12 P23; the external
signature path: §12 P29),
`CONFIG_ESP_CONSOLE_NONE=y`, `CONFIG_BOOTLOADER_LOG_LEVEL_NONE=y`, `CONFIG_FREERTOS_HZ=1000`,
`CONFIG_ESP_TASK_WDT_EN=y`, `CONFIG_ESP_TASK_WDT_TIMEOUT_S=1`, `CONFIG_ESP_TASK_WDT_PANIC=y`,
`CONFIG_ESP_TIMER_SUPPORTS_ISR_DISPATCH_METHOD=y`, `CONFIG_COMPILER_OPTIMIZATION_PERF=y`; CMake `MINIMAL_BUILD ON`.
Forbidden everywhere (B03 test): `CONFIG_SECURE_BOOT=y`, `CONFIG_SECURE_BOOT_V2_ENABLED=y`,
`CONFIG_SECURE_FLASH_ENC_ENABLED=y`, `CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK=y`, any `CONFIG_SECURE_UART_ROM_DL_MODE_*=y`.
`prod`: `CONFIG_BT_ENABLED` unset; `BP_VARIANT=1`. `bench`: `CONFIG_BT_ENABLED=y`, `CONFIG_BT_NIMBLE_ENABLED=y` (B12),
`BP_VARIANT=2`, `BP_FEATURE_IDLE_DAMPING`, diag commands. `diag`: BT unset, `BP_VARIANT=3`, bootstrap + diag only.

## 9. Simulator model (B06)

### 9.1 Factory defaults (ASSUMPTION until B9 replaces them per model)
ID 1, `Baud_Rate` 0 (1 Mbaud), `Return_Delay` 250 (500 µs), `Response_Status_Level` 1, `Min/Max_Position_Limit`
0/4095, `Max_Temperature` 70, `Max_Torque_Limit` 1000 (never 0: a fake whose register 16 starts at 0 hides a missing
leader write, main spec C16), `Phase` bit 4 clear, `Unload_Condition` 44,
`Protection_Current` 511 (the vendor tables; `feetech_provision.py` calls factory "~2 A (310)" — conflict F10, B9
settles), `Angular_Resolution` 1, `Homing_Offset` 0, `Operating_Mode` 0, `Protective_Torque` 20,
`Overload_Torque` 80, `Lock` 1, RAM: `Torque_Enable` 0, `Acceleration` 0, goal = present at power-on, `Torque_Limit`
(48) = `Max_Torque_Limit` at power-on (`feetech_bus.py` comment, "R2-verify"; ASSUMPTION until LEADER-LIMP), and a
simulated power cycle re-copies it.

### 9.2 Dynamics
Position loop per sim tick (1 ms): target = goal clamped into the window (register 42 reads the raw written value);
error naive `target − present` (not wrap-aware); velocity follows the trapezoid of `Goal_Speed` and `Acceleration`
(100 steps/s² per LSB; 0 = unlimited) toward the error; present wraps mod 4096 when forced; load = gain·τ_gravity +
injected external torque, saturating at `Torque_Limit`; `Torque_Limit` 0 ⇒ no output at all, so a torqued servo with
`Torque_Limit` 0 moves exactly like a limp one (the structurally limp leader, Q-B2); overload bit after load >
`Overload_Torque` for 2 s.

## 10. Bluetooth design (B12; not shipped)

- Role: follower board only; BLE 4.2 (ESP32 classic), NimBLE host on core 0, controller and host off in `prod`.
- Advertising: name `EduBotics-<short id>` (short id = last 3 bytes of MAC in base32), manufacturer data = status
  (kernel state, latch mask, firmware build) — read-only, no control.
- Data: one GATT service (128-bit UUID generated at B12 and frozen), characteristic RX (write without response) and TX
  (notify); the same COBS frames, chunked to `ATT_MTU − 3` (never assume > 20 B); same protocol, same codes.
- Security (designed, not implemented this round): application-layer Noise `XX_25519_ChaChaPoly_SHA256` (mbedTLS
  X25519 + ChaCha20-Poly1305 + SHA-256) with a per-device static key pair generated at provisioning and stored in NVS
  (no flash encryption: physical access can read it — accepted threat model: classroom mis-association, not cloning);
  the kit's QR label carries the static public-key fingerprint + short id; the client verifies it; nonces = 64-bit
  counters (replay protection); one controlling session; a live USB session makes BLE read-only; "identify" = LED +
  a 1-tick nudge (research 02) only after authentication.
- Loop protection: flash writes forbidden while energised and moving; BT on core 0, bus task on core 1; P2 bar:
  cycle-start jitter p99 ≤ 0.5 ms and 0 missed cycles in 1 h radio-on (B11).
- Shipping gate: B11 pass + the compliance consultant (RED incl. 2022/30 / EN 18031, CRA) + owner. Licences (NimBLE
  Apache-2.0, mbedTLS Apache-2.0) join `THIRD_PARTY_NOTICES` when shipped.
