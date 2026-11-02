# Station bench toolkit (dev only)

Measurement tools for the OMX camera station's bench gates (plan v6.13 §12.2 / §12.3:
B1–B5, B7, S1–S3, S5, G8, and the entry-plus-checks timing). They are for the owner at
the bench, on a real Windows student-class PC with the real InnoMaker U20CAM-1080P.

**Never shipped.** Nothing under `tools/` reaches an image or the installer: every image's
build context is a subfolder that does not contain `tools/` (the one repo-root context, the
RunPod trainer, copies four named files), and the installer and the GUI's PyInstaller spec
name no `tools/` path. `robotis_ai_setup/tests/test_station_bench_toolkit.py` fences that.

**Read-only towards the robot.** No tool calls `/station/session` `enter` or `leave`,
commands motion or switches torque. The only service calls are `status` queries. The only
thing any tool switches is the GUI's Studio stream, in `run_logger.py --toggle`, and only
while no station session exists.

**Output.** Every tool needs `--out <folder>` and writes `<tool>_<YYYYmmdd-HHMMSS>…csv`
(rows, flushed as they happen, so Ctrl+C keeps them) and `…_summary.json` (the numbers, the
gate verdicts and the honest limits) there. Container tools write inside the container and
`container_run.py` copies their folder back into yours.

## Setup on the bench PC

1. A checkout of this branch at the commit the bench image was built from, e.g.
   `C:\src\Testre`. Run every command from `tools\station_bench`.
2. A Python 3.11 or newer with the GUI's camera libraries:
   `py -m pip install opencv-python numpy`. Use the same `opencv-python` major/minor as the
   shipped GUI if you can (the release build log names it): MSMF behaviour can differ
   between OpenCV versions.
3. The EduBotics WSL distro and its containers, as installed by the product (the container
   tools reach `physical_ai_server` through `wsl -d EduBotics -- docker exec`, exactly like
   the GUI; no Docker Desktop, no shared folder).
4. The station must be enabled in the GUI („Kamerastation (OMX)"). In a release
   `STATION_OPTION_OFFERED` is `False` until a layout is released, so the bench runs a dev
   build of the GUI with the measured, released layout (plan §3.1, gate C1).

## B5 / B1 — how the camera opens (environment STOPPED, GUI CLOSED)

`camera_open.py` imports the GUI's own `app.win_camera.open_station_capture` from this
checkout (never a copy) and opens the station camera exactly as the GUI does.

```
py camera_open.py --list --out C:\bench\b5
py camera_open.py --index 1 --out C:\bench\b5 --variants all --duration 15 --repeat 3 --probe
```

* `--variants gui` opens with `constants.STATION_OPEN_METHOD` only; `--variants all` opens
  each method in `STATION_OPEN_METHODS` (`fourcc_only` = MJPG only, `fourcc_size_fps` =
  MJPG plus 1920×1080 and 30 fps), each on a fresh capture, `--pause` seconds apart.
* Per open: the read-back (fourcc, width, height, fps as the driver reports them), the open
  time, the time to the first frame, every frame's size, the measured rate of 1920×1080
  frames and the frame-interval p50/p95/max. `--probe` also runs the GUI's enrollment
  probe (`probe_station_camera`: 60 frames of exactly 1920×1080 at ≥ 29 fps).
* It refuses while the GUI's bridge on `127.0.0.1:8769` answers (a second MSMF open of the
  same camera returns a frameless handle). That is best effort: an `omx_follower` GUI with
  the station off runs no bridge, so close the GUI yourself.
* **Pass/fail (spec):** B5 decides the GUI constant: (a) `fourcc_only` when MSMF's default
  mode is already 1920×1080 at ≥ 29 fps, else (b) `fourcc_size_fps`. Record the open time.
* **Limits:** on MSMF the fourcc read-back is meaningless (the GUI says so in
  `open_capture`), so MJPG cannot be proven from it; frame size and measured rate are what
  count. This measures the NATIVE rate with the GUI closed; B1's „crop ≥ 29 fps" is the
  running bridge's number, measured by `run_logger.py`.

## B1 — the running station (environment RUNNING)

`run_logger.py` writes a row every `--interval` seconds (default 1 s, 10 min):

* the bridge's `GET http://127.0.0.1:8769/station/status`: `scene_fps` (the station crop,
  i.e. the recording stream, over the bridge's own 2 s window), `station_fps` (the Studio
  stream sent under id 200), `stream`, `error_de`;
* the GUI process CPU (ctypes `GetProcessTimes` on the GUI's PID: psutil is not a GUI
  requirement, `wmic` is gone from Windows 11 24H2, and `typeperf` cannot tell the GUI from
  its WebView2 child, which is the same `EduBotics.exe`). The PID is found with one
  PowerShell `Get-CimInstance` query (the `EduBotics.exe` without `--webview`), or pass
  `--gui-pid`. Both „% of one core" and „% of all cores" are logged;
* the gripper and scene camera rates the SERVER measures (`/edubotics/signal_status`), read
  by an in-container watcher (`container/watch_stream.py`) that also polls
  `/station/session status` once a second and reports, per second, the largest arrival gap
  of `/scene/image_raw/compressed` on the container's clock. It subscribes `/scene` with
  the recorder's own QoS (BEST_EFFORT, KEEP_LAST 32, `communicator.init_subscribers`), so
  a gap it reports is one the recorder sees.

The four B1 runs, same PC, same cameras:

```
:: 1. baseline: the station option OFF in the GUI, „Umgebung starten", 10 min
py run_logger.py --out C:\bench\b1 --label baseline
:: read stream_off.gripper_hz.mean from its summary, e.g. 25.1
:: 2. station ON, no session (the recording case, Studio stream off), 10 min
py run_logger.py --out C:\bench\b1 --label stream-off --gripper-baseline-hz 25.1
:: 3. station ON, Roboter Studio open, „Station starten" pressed (the page holds the stream), 10 min
py run_logger.py --out C:\bench\b1 --label stream-on --gripper-baseline-hz 25.1
:: 4. „Station verlassen", then 200 switches
py run_logger.py --out C:\bench\b1 --label toggle --toggle 200 --on-s 2 --off-s 2
```

* **Pass/fail (spec):** crop ≥ 29 fps with the Studio stream off and on; ≥ 14 fps sent under
  id 200 with it on; the gripper within 1 fps of its no-station baseline in both cases; GUI
  CPU recorded for both; 200 switches without a camera reopen or a recording-stream gap.
* The summary reports the crop's mean AND every 2 s window below 29: the spec gives no
  window, so both are there to judge.
* `--toggle N` first checks, read-only, that `/station/session` is `IDLE` and the bridge
  reports the station enabled with the stream off; it re-checks before every cycle and
  stops if a session appears. Each `on` phase is `--on-s` (≤ 8 s, inside the 10 s lease),
  sent with the page's `Origin: http://localhost`; Ctrl+C always sends `off`.
* **Limits (reopen / gap):** the bridge has no reopen counter. Its only evidence is
  `error_de` („Stationskamera liefert keine Bilder — wird neu geöffnet …", visible for
  the ~3 s a reopen takes; polled at 4 Hz during toggling) and a dip of `scene_fps`. The
  recording-stream gap is measured where the recorder sees it: the container's largest
  arrival gap of `/scene` per cycle (`scene_max_gap_ms`). Judge it against run 2's
  `scene_max_gap_ms_1s` column (the camera's own jitter), not against zero.

## B2 / B3 — frames at the server, their age and latency

The frame stamp is the Windows capture time (`capture_ns`, `time.time_ns()` in the GUI);
the container stamps arrival with the WSL kernel clock. Measure the offset first:

```
py clock_offset.py --out C:\bench\b2 --samples 300
```

It runs one long-lived `date +%s%N` loop inside `physical_ai_server` (`--target distro`
for the distro itself; same kernel clock) and keeps the fastest of the round trips: the
offset is true within ± half that round trip (printed; measured, never assumed — the
container summaries carry it and call a result inside it `UNDECIDED`). WSL2 re-syncs its clock only now and then (after sleep it can be seconds off), so
measure right before and after a run; `--repeat 5 --every 60` shows the drift.

Then, with a station session active („Station starten" in Roboter Studio; the page turns
the Studio stream on):

```
py container_run.py arrival_rate --out C:\bench\b2 --offset-json C:\bench\b2\clock_offset_<stamp>.json -- --duration 120
```

* Subscribes `/station_scene/image_raw/compressed` with the SERVER's own QoS for it
  (BEST_EFFORT, KEEP_LAST 2, `communicator.enable_station_store`; any other `--topic` with
  the recorder's BEST_EFFORT, KEEP_LAST 32) and logs per frame `recv_ns`, `stamp_ns`, raw
  and corrected age. A RELIABLE subscriber would also get the retransmissions the server's
  BEST_EFFORT subscription loses and so overstate B2; `-- --reliable` subscribes with the
  publisher's RELIABLE, KEEP_LAST 5 for a comparison run only (the summary's `qos` block
  says which was used).
* **Pass/fail (spec):** B2 ≥ 14 fps and arrival-age p95 ≤ 0.3 s (also: the preview shows
  in the browser — look at it). A p95 within the clock uncertainty of 0.3 s is reported
  `UNDECIDED`, never PASS.
* **Limits:** the stamp is when MSMF handed the frame to the GUI, so B2's age leaves out the
  camera's own exposure and pipeline. That is what B3 adds.

**B3 (capture-to-arrival against a time-changing target).** Put a screen in the station
camera's view (on the mat, flat or tilted toward the camera) and run the target on the
same Windows PC, so it shows the same clock the offset was measured against:

```
py time_target.py --period-ms 33 --size 700
py container_run.py arrival_rate --out C:\bench\b3 --offset-json C:\bench\b2\clock_offset_<stamp>.json -- --duration 60 --qr
```

The target shows a QR code of `EBT1:<time.time_ns()>` taken when it is drawn (plus the
time as text). The container decodes it and reports `latency = recv − (shown + offset)`.
**Pass/fail (spec):** p95 ≤ 0.35 s. **Limits:** the latency includes the display's own
latency (one or two refreshes) and up to one `--period-ms` (the code stays on screen that
long), so it OVERestimates: a PASS is safe; for a near miss, rerun with `--period-ms 17` on
a 60 Hz screen. Frames where two codes blend do not decode and are skipped; the summary
says how many decoded. The column `shown_to_stamp_ms` is the camera-side part on its own.

## B4 — lens solve time and perception load

**Lens solve on 30 views ≤ 3 s.** It runs inside the commission service call, so it cannot
be timed from outside without becoming the commission's owner. Time it from the wizard:
in „Station einrichten (Lehrkraft)", capture 30 views, then

```
py pick_tally.py stopwatch --out C:\bench\b4 --gate lens --label "30 views"
```

Enter as you click „Linse berechnen", Enter when the result appears (on the „Matte
ausrichten" step, or as a refusal). Reaction time (about ±0.2 s) is in every lap. The page
gives the call 8 s before it reports no answer.

**Hat polls during a grasp.** With a station session active (frames flowing) and NO program
running, a cube with tag 20 on the mat:

```
py container_run.py perception_load --out C:\bench\b4 -- --trials 10 --tag-id 20
```

It loads the server's real `workflow.perception.Perception` (pupil_apriltags tag36h11
behind its one thread lock — the instance the node shares between a program's threads) and
decodes live 1920×1080 station frames the way the node does, subscribed like the node's
station store (BEST_EFFORT, KEEP_LAST 2). One thread polls like the
„wenn … erkannt" hat (look, then 0.2 s sleep); per trial, the composite „Greife"'s yaw
sampling makes 14 detects 0.03 s apart (both numbers read from `perception_blocks` in the
image). **Pass/fail (spec):** the hat keeps ≥ 2 polls/s during the grasp, and the 14
detects end within 2 s, in every trial (also reported: the single-thread 1080p detect p95,
v6.3's informational ≤ 200 ms). **Limits:** it runs in its own process beside the node:
the CPU contention is real, the node's own detector lock is not shared (the hat-versus-
grasp contention is modelled inside the one instance, as in the node). Motion, claims and
the yaw maths are not run (microseconds next to a 1080p detect). A failure here is the
owner's go / no-go; the prepared remedy is detecting on the accepted zone's bounding box.

## B7 — the real unit's focal length

The lens step now names it. A refusal reads, e.g., „Die Linsenkalibrierung ist nicht gut
genug (Brennweite fx 1312 px, fy 1310 px, erlaubt 935–1265 px) – bitte neu erfassen."; an
accepted solve shows „Fehler 0,42 px (Kontrolle 0,51 px, höchstens 1,20 px), Brennweite fx
1102 px, fy 1099 px" above „Matte ausrichten". Calibrate the first unit under §6.1 and
note fx / fy: they become f₀ (`commission.F0_PX`), and the ±15 % band is re-centred on it.
If the unit's fx lies outside today's provisional band [935, 1265] px, the solve refuses
and the refusal is where you read it; re-capturing does not help then — f₀ must move.

## S1 / S2 / S3 / S5 — the session checks

Run the check log for the whole check session (it changes nothing):

```
py container_run.py check_log --out C:\bench\s1 -- --interval 0.5
```

It polls `/station/check` and `/station/session` with `{command: status}` (the same
read-only calls as `ros2 service call … "{command: status}"`, through an rclpy client so the
answer needs no text parsing) and appends every NEW check result with its kind (`camera`,
`robot` or `resolve`), result, rms / max / centroid / rotation, dot id / dxy / dz and
per-reference residuals, every session change, and every change of the owner's last re-solve
job (`…_jobs.csv`: `job_id`, `job_state`, message). The console shows a running S1 tally.

* **S1:** 50 static camera checks („Kamera prüfen") all `OK` with every metric at most half
  its threshold (rms ≤ 1.5 mm, max ≤ 3.0 mm, centroid ≤ 1.5 mm, rotation ≤ 0.25°), and 30
  static robot checks („Roboter prüfen", then „Prüfen") all `OK`. For the robot the
  half-threshold count (dxy ≤ 2.0 mm and dz ≤ 1.5 mm) is reported, never a gate (spec
  §12.3 says „all OK" for the robot).
* **S2:** after a deliberate ≈ 1° camera rotation, 10/10 `MOVED`; after a 5 mm base shift,
  10/10 `ROBOT_MOVED` (count them in the `result` column).
* **S3:** a re-solve („Kamera neu ausrichten (Lehrkraft)") stores its OWN result
  (`service.py::_finish_work_locked`) as a row of kind `resolve`: `OK` when it passes,
  `RESOLVE_REFUSED` when the new pose's own check is not `OK` or the pose moved beyond the
  re-solve bounds (more than 3° or 25 mm from commissioning). The row carries that check's
  rms / max / centroid / rotation (all 0 when the check found no refs) and no ref ids and
  no dot. After a 1–2° bump expect `resolve` `OK`, then picks still succeed (and the next
  „Kamera prüfen" is an ordinary `camera` row); after > 3° expect `resolve`
  `RESOLVE_REFUSED` (the page says „Kamera zu stark verstellt – vollständige Einrichtung
  nötig."). A re-solve that fails before its check (too few board frames, the board solve,
  the write) stores no result: it shows only in the jobs CSV (`job_state` `failed`, the
  German message) and on the page.
* **S5:** a page reload in ACTIVE keeps the session's `generation` and `robot_ok`, and a new
  camera check follows by itself; the session CSV shows both.
* **Limits:** the server keeps ONE last check per session generation with no counter: a
  check replaced before the next poll is missed (poll at 0.5 s; one press every few
  seconds is safe), and two checks with exactly equal numbers count once. `NO_FRAME`,
  `WRONG_SIZE`, `BUSY` and `NOT_OWNER` are never stored and never appear.

## G8 and „entry plus both checks"

```
py pick_tally.py tally --out C:\bench\g8 --label "zone A, 50 picks"
py pick_tally.py stopwatch --out C:\bench\entry --gate entry
```

* Tally: `s` success at the first attempt, `f` fail, `u` unsafe contact, `n` a note on the
  last pick, `z` undo, `q` quit; each pick is time-stamped. **Pass/fail (spec):** ≥ 50
  supervised picks over the accepted zone, ≥ 95 % first-attempt, no unsafe contact.
* Stopwatch: Enter when you click „Station starten", Enter when the page shows „Geprüft –
  der Roboter darf sich bewegen". **Pass/fail (spec):** typical ≤ 2 min, p50 / p95 reported.
  `check_log.py` also measures it automatically from the first poll that sees a new session
  generation to the first `verified` (`entry_to_verified_s`); that leaves out the click and
  the page's own start. A session already running when `check_log` starts is not timed
  (start the log first, then „Station starten").

## Unit tests

The pure parts (parsers, statistics, CSV writing, the clock-offset estimate, the toggle
guard, the check tally, the pick tally) and the never-shipped fence:

```
cd robotis_ai_setup
python -m unittest discover -s tests -p "test_station_bench_toolkit.py"
```
