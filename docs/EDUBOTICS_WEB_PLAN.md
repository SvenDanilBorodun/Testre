# EduBotics Web — Browser-Native Architecture Plan

Status: **committed direction** (decided 2026-06-05). This document records the idea,
the verified findings that support it, and the implementation plan.

## 1. The idea

Students use EduBotics **entirely in the browser**. Nothing is installed on the student
PC — no `.exe`, no WSL2, no Docker, no 15.5 GB images. The hardware connection stays
physically identical: both OMX arms and both cameras plug into the PC over USB exactly
as today. The web app at `https://app.edubotics.de` finds the hardware ("Roboter
verbinden"), and the full student lifecycle — teleop → Aufnahme → Cloud-Training →
Inferenz → Roboter Studio — works as it does now, with the same React UI, the same
German UX, the same LeRobot v3.0 datasets on HF, and the same Modal training.

Decisions locked (2026-06-05):

| Question | Decision |
|---|---|
| Local install endgame | **Kill it entirely** (browser-native, Architecture "B") |
| Where the browser runs | **Same PC** the arms plug into |
| Device fleet | Windows Edge/Chrome + **Chromebooks** + **iPads** (iPads as WebRTC clients — they have no WebSerial and can never hold the USB themselves) |
| Offline | **Online-only is acceptable** |

## 2. Findings

### 2.1 Measured on the rig (2026-06-05) — the go/no-go evidence

Read-only Dynamixel Protocol 2.0 benchmark (PING + SyncRead Present Position + Fast
Sync Read probe; zero writes, zero motion) on the real arms through the **native
Windows CDC driver (`usbser.sys`) — the exact path the Web Serial API uses**. Script:
`%TEMP%\dxl_rtt_bench.py`.

| Metric (300+ reps) | Leader (IDs 1–6) | Follower (IDs 11–16) |
|---|---|---|
| SyncRead RTT, 6 servos | p50 **1.39 ms** / p95 1.43 ms | p50 **1.98 ms** / p95 2.05 ms |
| Flat-out loop (5 s) | **713 Hz** | **503 Hz** |
| Paced 100 Hz (5 s) | **501/501 hits** | **501/501 hits** |
| Failures (~1,100 transactions) | **0** | **0** |
| Fast Sync Read (0x8A) | **supported**, 1.1 ms | **supported**, 1.6 ms |

→ **100 Hz teleop confirmed with 5–7× headroom**, no driver tweaks, no admin rights.
Contrast: the same bus through usbipd/WSL2 produces the historical `-3002
SYNC_READ_FAIL` storms; natively it ran clean.

### 2.2 Hardware identity correction

The arms connect via **ROBOTIS OpenRB-150** boards (VID `2F5D`, PIDs `0103`/`2202` —
`gui/app/constants.py:143`; xacros open `/dev/ttyACM*`), i.e. **USB CDC-ACM**, NOT
U2D2/FTDI. The FTDI 16 ms latency-timer concern from early analysis **does not apply
to this product**; the earlier "~60 Hz stock" caveat is withdrawn. There is no
latency timer anywhere in the Windows path.

### 2.3 Cameras — honest baseline

"30 fps" never existed on the Windows student path: the Innomaker U20CAM caps at
~25 fps through Windows Media Foundation (~14 fps via DirectShow) — which is why the
shipped native bridge is **locked to 24 fps** today. Chrome captures via Media
Foundation → the browser hits the same ceiling → **24 fps parity with the shipped
product** (identical dataset quality). WebCodecs hardware encode is not a limiter
(2× VGA is trivial). True 30 fps exists only on the Jetson/native-Linux path, in both
architectures. Bonus: Chrome's `deviceId` derives from the USB instance path, so the
SN0001 identical-serial collision problem **dissolves** — the visual gripper/scene
role-pick persists per physical port.

### 2.4 Existence proofs

- **lerobot.js** (HF blog: https://huggingface.co/blog/NERDDISCO/lerobotjs) calibrates
  and teleoperates SO-100 arms (1 Mbaud serial servo bus) purely from Chromium ≥ 89
  via Web Serial — no installed software. bambot.org likewise.
- Remaining unmeasured hop: Chrome's Web Serial overhead on top of `usbser.sys`.
  Bounded (Chrome would have to eat >80 % of the 10 ms budget despite 713 Hz raw
  headroom) — closed by a one-click browser bench page on the rig.
- Pending live measurement: Chrome getUserMedia fps on the Innomakers (cameras were
  unplugged on bench day).

### 2.5 Codebase facts that make this cheap(er)

- The student UI **is already a browser app** — React served by nginx on `:80`;
  WebView2 merely embeds it (`gui_app.py:1699`), with a system-browser fallback
  (`gui_app.py:1715`).
- Students **already authenticate with Supabase JWTs** (`apiClient.js:5-7`); a
  hardware-less mode exists (`?cloud=1`, `cloudMode.js`).
- The complete "find and connect to a remote device" pattern is **already shipped**
  for the classroom Jetson: cloud registry + claim lock + 30 s heartbeats + JWT-
  authenticated rosbridge proxy (`:9091`, auth-op first frame,
  `rosConnectionManager.js:133-142`) + camera-over-websocket fallback.
- The rosbridge URL is runtime-swappable (the Jetson swap mechanism) — the SPA is not
  hardwired to localhost.
- Latent issue found: the Jetson connection is plain `ws://<lan-ip>:9091`
  (`useJetsonConnection.js:37`) — it only works because today's page is plain
  `http://localhost`. An HTTPS-hosted SPA breaks it → the Jetson transport must move
  to WebRTC/WSS in the inference phase regardless of everything else.
- All container ports are deliberately loopback-bound (`docker-compose.yml:42,153,154,242`).

### 2.6 Why this architecture fits THIS product specifically

1. **Training is already cloud** (Modal L4) — the local GPU stack (~6 GB CUDA/PyTorch
   in the image) is dead weight on GPU-less student PCs.
2. **Student policies are action-chunked** (ACT ~100 steps ≈ 3.3 s @ 30 fps) →
   inference tolerates 150–400 ms round-trips → cloud/Jetson/WebGPU lanes all work.
3. **The hard-real-time loop lives in servo firmware** (kHz PID inside the Dynamixels);
   the PC only streams goal positions — measured at 100 Hz with 5–7× headroom.
4. The 15.5 GB image is mostly **delivery vehicle** (Ubuntu + ROS + CUDA), not product
   logic. The product logic (teleop mirror, recorder, e-stop, Studio interpreter) is
   small and portable.
5. v2.5.x already moved camera capture **out of the containers** (native Windows
   bridge) because the WSL2 USB path couldn't do the job — browser-native finishes
   that thought.
6. Nearly every major support scar (usbipd races, vhci Hz ceiling, `SYNC_READ_FAIL`
   storms, cv_bridge/numpy ABI brick, dockerd deadlocks, restart storms, WebView2
   cache, identical-serial collisions) lives in the layer this removes.

## 3. Target architecture

```
            https://app.edubotics.de   (new Railway service "student-web")
            React SPA (existing codebase, new build target)
            + TS robot engine (new, pure-logic, ported test vectors)
            │
   ┌────────┼───────────────┬───────────────────┬──────────────────────┐
   ▼        ▼               ▼                   ▼                      ▼
WebSerial  WebSerial    getUserMedia        Railway API           WebRTC (Phase 5)
OpenRB-150 OpenRB-150   2× Innomaker        Supabase JWT (exists) ├─ iPad/Safari UI
leader     follower     24 fps              ├─ Modal training     ├─ teacher live-view
(CDC-ACM, inbox driver, (MF, role-pick      ├─ chunked inference  └─ Jetson transport
 zero install, 100 Hz)   per deviceId)      └─ HF OAuth → huggingface.js upload

Recording: WebCodecs H.264 (iGPU) → mp4-muxer → parquet-wasm → OPFS → HF Hub (v3.0)
Safety:    Dynamixel current limits + Bus Watchdog (firmware) + e-stop port (TS, 1:1)
```

### Component mapping (current → browser)

| Today | Browser-native |
|---|---|
| WSL2/Docker/3 containers/installer/GUI | **deleted** — browser is the runtime; updates = page reload |
| usbipd servo attach | WebSerial chooser (GPO `SerialAllowUsbDevices` for prompt-free fleets) |
| `identify_arm.py` (ping 1–6 vs 11–16) | same algorithm in TS after port grant (~40 lines) |
| ros2_control 100 Hz + `/leader/joint_trajectory` | TS engine: leader SyncRead → follower SyncWrite @ 100 Hz, IO-completion-driven loop in a worker |
| entrypoint quintic boot-sync + Phase-4 verify | `trajectory.ts` quintic + verified arrival (≤ 0.30 rad) |
| SIGTERM torque-disable | **Bus Watchdog** (addr 98, ~500 ms): servo firmware freezes-in-place if the tab dies |
| xacro current limits (350/300 mA, Op Mode 5) | same registers written at connect by the TS robot profile |
| Collision e-stop (v2.6.0) | 1:1 port of `collision_detector.py` + monitor state machine, **test vectors carried over**; existing German two-step React modal rewired to engine events |
| Native camera bridge (`win_camera.py` → TCP 5557 → ingest node) | `getUserMedia` directly (the browser does internally what the bridge hand-built) |
| usb_cam / web_video_server / rosbridge | deleted — preview is a `<video>` tag; no bridge needed (one program) |
| LeRobot recorder + streaming encoder | WebCodecs hardware H.264 → mp4-muxer; parquet-wasm v3.0 writer; OPFS staging; stats in JS; explicit finalize |
| HF token in GUI `.env` + Benutzer-ID list | **"Sign in with Hugging Face" OAuth** per browser profile (the shared-PC token machinery dissolves) |
| Cloud training (Railway → Modal) | unchanged (Supabase JWT already sent) |
| Local PyTorch inference | Lane 1 (default): **cloud-chunked** (obs up ~130 KB, 100 actions down); Lane 2: classroom Jetson via WebRTC; Lane 3: onnxruntime-web WebGPU ACT (~160 MB, cached) |
| Roboter Studio (server-side interpreter) | Blockly already client-side; TS interpreter port; OpenCV.js + AprilTag WASM + same SHA-pinned YOLOX via onnxruntime-web; 4-DOF analytic IK |
| `ROS_DOMAIN_ID` per machine | moot — no DDS |
| iPads | WebRTC "Roboter teilen": host tab (holds USB) ↔ viewer/driver clients, signaling via Supabase Realtime, single-driver lock à la Jetson claim |

### Guardrails

- **`dataset-writer-parity` CI job**: identical synthetic frames through the TS writer
  (Node) and python LeRobot 0.5.1 → compare schema/rows/meta → run the real Modal
  preflight on the TS output. (Designed against the v2.5.0 footer-less-parquet lesson.)
  This becomes a 4th, machine-checked site of the Rule §5 format contract.
- Golden test vectors ported from `test_collision_detector.py` /
  `test_collision_monitor_contract.py` keep TS and Python semantics identical.
- Rule §2 rewrite required (safety placement: "xacro + entrypoint" → "servo firmware
  registers written by TS profile + Bus Watchdog + sanctioned e-stop port") — needs
  explicit maintainer sign-off before implementation.
- Rule §1 unchanged: all new student-facing strings German; engine code English.
- New endpoints (inference proxy, share codes) get Rule §4 ownership assertions; new
  deploy workflow clones the teacher-web health-gate pattern (Rule §6).

## 4. Phases

| Phase | Weeks | Content | Exit criterion |
|---|---|---|---|
| **0 — spikes** | 1–2 | Serial: **driver-side DONE 2026-06-05** (table above); remaining: one-click Web Serial page in Edge on the rig + Chrome camera-fps test | browser-in-the-loop numbers match the driver bench; Chrome delivers 24 fps |
| **1 — trunk** | 2–6 | `student-web` Railway service + deploy workflow; student login; "Roboter verbinden" (port grant → ping-identify → camera role-pick); German wizard; HF OAuth | connect screen works on a clean PC with zero installs |
| **2 — teleop** | 6–10 | TS engine (`dxl/`, `collisionDetector.ts`, `teleopSupervisor.ts`, `trajectory.ts`); boot-sync; Bus Watchdog; e-stop modal rewire | teleop + collision trip/recover on the rig from the hosted HTTPS page |
| **3 — recording** | 10–14 | recorder + v3.0 writer + OPFS + upload + parity CI | record → upload → real Modal training → checkpoint on HF, fully browser-side |
| **4 — inference** | 14–17 | cloud-chunked endpoint (+ rate limits / credit decision); Jetson WebRTC transport; optional WebGPU lane | full lifecycle with zero local install |
| **5 — Studio + share** | 17–22 | Studio in-browser; WebRTC "Roboter teilen" for iPads + teacher live-view | feature parity incl. iPad clients |
| **6 — decommission** | rolling | `.exe`/WSL2 → maintenance mode; retire installer/rootfs/student-image CI surfaces (Jetson images remain); IT-Leitfaden (GPO allowlists) | classes migrated; support load shifts |

First classroom-usable teleop+record beta: **~8 weeks**. Full parity: **~5 months**.
Student onboarding: 30–90 min + admin + ~20 GB → **< 5 minutes in a browser tab**.
Coexistence: ships as an opt-in beta per classroom; the `.exe` path keeps working;
datasets are identical on HF; rollback = "use the exe".

## 5. Residual risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Chrome WebSerial overhead atop usbser.sys | Low (5–7× headroom + lerobot.js proof) | one-click page measures it (Phase 0 remainder) |
| TS dataset writer drifts from LeRobot | Med | parity CI + Modal preflight (two independent nets) |
| School policy blocks WebSerial | Med | IT-Leitfaden + GPO allowlist; Edge is preinstalled |
| Tab discard / sleep mid-session | Low | IO-driven loop (no timers), wakeLock, Chrome keeps Serial tabs; **Bus Watchdog catches the rest in firmware** |
| TS (student) vs ROS (Jetson) divergence | Med | shared golden vectors; both feed the same Modal preflight |
| Innomaker quirks under Chrome MF | Low | Phase-0 camera test; 24 fps target equals shipped baseline |

## 6. Evidence ledger

| Claim | Class |
|---|---|
| 100 Hz read loop, native Windows, our servos | **Measured on the rig 2026-06-05** (0 failures) |
| Fast Sync Read available | **Measured** — firmware responded |
| No FTDI latency timer (OpenRB-150 CDC) | **Verified** in repo + live enumeration |
| Browser teleop of 1 Mbaud servo arms | **Existence proof** (lerobot.js, Chromium ≥ 89) |
| Chrome WebSerial overhead fits 10 ms budget | **Bounded, not yet measured** → one-click page |
| 24 fps camera parity (30 fps never existed on Windows path) | **Prior on-rig measurements** + Chrome-uses-MF |
