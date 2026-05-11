# EduBotics camera-pipeline — action doc

Findings from the 2-pass deep audit (host plumbing, ROS subscribers/recording, inference safety envelopes, Roboter Studio calibration/perception, React/web_video_server, Modal OWLv2 worker, Cloud-API proxy, Supabase quota, on-host cloud-vision bridge, React Workshop UI).

**How to use this doc.** Each finding has location, current state, root cause + connections to other files, and a fix sketch — enough to apply without re-reading the audit chain. Status legend: `🔴 CRITICAL` · `🟠 HIGH` · `🟡 MEDIUM` · `⚪ LOW`. The "Verified" line records the verdict from cross-checking against the actual code.

Skip the "False alarms" appendix unless you're curious — those are issues the auditors raised that I confirmed are non-bugs.

---

## Part 1 — Two-model object-detection (Roboter Studio)

EduBotics has two detection models:
- **Model 1**: YOLOX-tiny ONNX (Apache-2.0), local CPU in `physical_ai_server` container, COCO 80 classes filtered to 16 German labels in `coco_classes.py`.
- **Model 2**: OWLv2 (`google/owlv2-base-patch16-ensemble`, Apache-2.0), cloud burst on Modal T4 GPU, open-vocabulary with German CLIP text head.

Dispatch block `edubotics_detect_open_vocab`: an 18-entry German→{mode,class,color} synonym dict in `physical_ai_server.py:2502-2535` resolves the prompt → hits go to Model 1, misses go to Model 2 via `_cloud_vision_burst`.

### F1 🔴 Synonym-dict-to-handler bridge is doubly broken (today + tomorrow)
- **Location**: `robotis_ai_setup/docker/physical_ai_server/overlays/workflow/handlers/perception_blocks.py:185-216`
- **Today's bug (crash)**: line 193 `coco = translate.get(prompt.lower())` returns the **whole entry dict** (`{'mode':'object','class':'cup','color':'rot'}`), not a string. Line 198 `detect_object(ctx, {'class': coco})` forwards a dict where `perception.py:266` does `if coco_class in COCO_CLASSES` → `TypeError: unhashable type: 'dict'`. Workflow error surfaced as misleading `"Cloud-Erkennung fehlgeschlagen: …"`.
- **Tomorrow's bug (silent)**: even after fixing the crash by extracting `coco['class']`, `coco_classes.py:20-37` keys are **German** (`Tasse`, `Banane`, `Apfel`) while the synonym dict uses **English** COCO labels (`cup`, `banana`, `apple`). So `'cup' in COCO_CLASSES` → False → `wanted_id = None` → loop returns every detected object unfiltered ("finde rote Tasse" returns bananas too).
- **Connections**: `perception_blocks.py` → `perception.py:266` → `coco_classes.py`. Synonym dict in `physical_ai_server.py:2502-2535` is the source of the English-label drift.
- **Fix**:
  1. Branch in `detect_open_vocab` on `coco['mode']`:
     - `'object'` → `detect_object(ctx, {'class': coco['class'], 'color': coco.get('color')})`
     - `'color'`  → `detect_color(ctx, {'color': coco['color']})`
  2. Rewrite the synonym dict to use **German** class labels (`'class': 'Tasse'`, `'class': 'Banane'`, …) so `coco_class in COCO_CLASSES` actually matches.
- **Test plan**: unit test `test_detect_open_vocab_dispatch` with each of the 18 prompts → assert no exception + correct delegate called.
- **Verified**: directly read `perception_blocks.py:185-216`, `perception.py:263-275`, `coco_classes.py:20-37`. Trace confirmed end-to-end.

### F2 🔴 YOLOX output decode missing grid+stride
- **Location**: `robotis_ai_setup/docker/physical_ai_server/overlays/workflow/perception.py:217-303`
- **Bug**: lines 230-258 assume the ONNX output is `(N, 85)` with `(cx,cy,w,h)` already at 640-stride. Megvii's `yolox_tiny.onnx` (pinned by SHA `427cc366…` in `robotis_ai_setup/docker/physical_ai_server/Dockerfile`) emits **grid-relative offsets** per feature level with strides `{8, 16, 32}`. Without `demo_postprocess`, bboxes are interpreted at ~0-1 px → NMS keeps nothing → silent empty.
- **Connections**: every `edubotics_detect_object`, `edubotics_count_objects_class`, `edubotics_wait_until_object`, AND `edubotics_detect_open_vocab` (synonym hit path) → `Perception._detect_yolo`. Affects every YOLO block in Roboter Studio.
- **Fix**: re-export ONNX with `--decode_in_inference` OR vendor Megvii's `demo_postprocess` into perception.py and call it on `outputs[0]` before slicing.
- **Test plan**: known image + known object (e.g. cup at known pixel) → bbox center within ±20px.
- **Verified**: read `perception.py:217-303`; comment at line 249 explicitly makes the wrong claim.

### F3 🔴 NMS receives wrong box format
- **Location**: `robotis_ai_setup/docker/physical_ai_server/overlays/workflow/perception.py:305-331`
- **Bug**: `cv2.dnn.NMSBoxes` expects `[x, y, w, h]`; line 321 passes `[x1, y1, x2, y2]` (xyxy). IoU is computed treating `x2`/`y2` as width/height → near-origin boxes look tiny and survive, near-bottom-right boxes look huge and dedupe incorrectly.
- **Connections**: bundle with F2 — same file, same dispatch.
- **Fix**: convert before call: `boxes_xywh = np.stack([x1, y1, x2-x1, y2-y1], axis=1)`.
- **Verified**: read directly.

### F4 🔴 `_cloud_vision_burst` is a `NotImplementedError` stub
- **Location**: `robotis_ai_setup/docker/physical_ai_server/overlays/physical_ai_server.py:2537-2553`
- **State**: Modal app (`vision_app.py`) + Cloud-API proxy (`routes/vision.py`) + Supabase quota (`017_vision_quota.sql`) + React block + `cloud_vision_enabled` field on `StartWorkflow.srv` — all built end-to-end. The 25-line POST from the on-host server to the Railway `/vision/detect` endpoint is the only missing piece. Tracked in `docs/ROBOTER_STUDIO_DEFERRED.md §1.4`.
- **Connections**: blocks Model 2 entirely. Once wired, also triggers F28 (the `enabled` toggle leak) because today nothing fires.
- **Fix sketch**:
  ```python
  def _cloud_vision_burst(self, bgr_frame, prompt: str):
      import cv2, base64, requests
      ok, jpg = cv2.imencode('.jpg', bgr_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
      if not ok: return []
      b64 = base64.b64encode(jpg.tobytes()).decode('ascii')
      # Need JWT propagation — caller must have student's token from
      # last GUI login. Stash on the WorkflowContext or read from
      # env-injected token file written by the GUI's auth flow.
      token = self._get_cached_student_jwt()
      if not token:
          raise WorkflowError('Anmeldung fehlt — bitte erneut einloggen.')
      r = requests.post(
          f'{CLOUD_API_URL}/vision/detect',
          json={'image_b64': b64, 'prompts': [prompt], 'score_threshold': 0.25},
          headers={'Authorization': f'Bearer {token}'},
          timeout=15,
      )
      if r.status_code == 429:
          raise WorkflowError('Cloud-Erkennungs-Kontingent erreicht.')
      if r.status_code == 504:
          raise WorkflowError('Cloud-Erkennung lädt — bitte gleich erneut.')
      r.raise_for_status()
      data = r.json()
      # Convert OWLv2 [x1,y1,x2,y2] bbox to local Detection objects.
      return [self._owlv2_to_detection(d, bgr_frame.shape) for d in data['detections']]
  ```
- **Prereq**: a per-classroom service token mechanism OR student JWT cached on the host (GUI auth flow writes it to `%LOCALAPPDATA%\EduBotics\token` and the GUI mounts it into the container — design pending in §1.4).
- **Verified**: read directly.

### F5 🔴 (downgrade to 🟠) `cv_bridge` `passthrough` encoding + unconditional `BGR2RGB`
- **Location**: `robotis_ai_setup/docker/physical_ai_server/overlays/data_manager.py:587-590` + `data_converter.py:60-80`
- **State**: today's `usb_cam` MJPEG decode IS BGR so the channel-swap is correct. Brittle defence-in-depth: any driver/kernel returning a non-BGR decode silently mis-trains.
- **Fix**: pass `desired_encoding='bgr8'` to `compressed_imgmsg_to_cv2` so cv_bridge raises on mismatch.
- **Severity**: HIGH (was CRITICAL — downgraded after I read the cv_bridge source path).
- **Verified**: read both files.

### F6 🔴 First-frame shape locks the LeRobot dataset
- **Location**: `data_manager.py:783-789` + LeRobot upstream `LeRobotDatasetWrapper` (not in repo, base image).
- **Bug**: `features[f'observation.images.{name}']['shape'] = image.shape` is set from frame 0 and never re-validated. A USB renegotiation that drops 720p→480p mid-record writes torn parquet/mp4.
- **Fix**: in `record()` (line 175 or wrapper), assert per tick:
  ```python
  expected = self._lerobot_dataset.features[f'observation.images.{name}']['shape']
  if image.shape != tuple(expected):
      raise WorkflowError(f'Bildform geaendert: {image.shape} vs {expected}')
  # → set self._last_warning_message + record_early_save()
  ```
- **Connections**: ties to F22 (camera params drift mid-session).
- **Verified**: read data_manager.py:780-790.

### F7 🔴 Healthcheck blind to dead cameras
- **Location**: `robotis_ai_setup/docker/docker-compose.yml:41-49`
- **Bug**: only greps `/joint_states` → a dead `usb_cam` leaves container "healthy" → `physical_ai_server` starts on `service_healthy` → recording proceeds with black frames; failure only surfaces at first record-press via 5s topic-wait.
- **Fix**:
  ```yaml
  test: ["CMD-SHELL", "bash -c 'source /opt/ros/jazzy/setup.bash && source /root/ros2_ws/install/setup.bash && ros2 topic list 2>/dev/null | grep -q /joint_states && ([ -z \"$CAMERA_DEVICE_1\" ] || ros2 topic list | grep -q /${CAMERA_NAME_1:-gripper}/image_raw/compressed) && ([ -z \"$CAMERA_DEVICE_2\" ] || ros2 topic list | grep -q /${CAMERA_NAME_2:-scene}/image_raw/compressed)'"]
  ```
- **Verified**: read docker-compose.yml end-to-end.

### F8 🟠 Camera-role fallback `camera1`/`camera2` writes non-matching topic names
- **Location**: `robotis_ai_setup/gui/app/config_generator.py:79-82`
- **Bug**: `CAMERA_NAME_{i}={_quote(cam.role or f"camera{i}")}` — if `cam.role` ever empty (single-camera-no-role-fired edge case), env writes `CAMERA_NAME_1="camera1"` → entrypoint launches usb_cam on topic `/camera1/...` → `omx_f_config.yaml:10-11` hard-codes `gripper`/`scene` → subscriber waits forever.
- **Connections**: `device_manager.CameraDevice.role` default `""` (line 46) + `gui_app._on_cameras_changed` (lines 780-842) is the only setter. The single-camera path (line 794) DOES set role to `"gripper"`, so the fallback only fires on programmatic misuse.
- **Fix**: in `generate_env_file`, refuse to write a camera with `role not in {'gripper','scene'}`:
  ```python
  if cam.role not in ('gripper', 'scene'):
      raise ValueError(f'Kamera ohne Rolle: {cam.path}')
  ```
- **Severity**: HIGH (downgraded from CRITICAL — the wizard flow always sets the role).
- **Verified**: read config_generator.py end-to-end + device_manager.py:42-54 + gui_app.py:780-842.

---

## Part 2 — Inference safety envelopes

### F9 🟠 Single bad frame aborts entire inference run
- **Location**: `physical_ai_server.py:736-744`
- **Bug**: any exception from `convert_msgs_to_raw_datas` (one malformed JPEG, cv_bridge transient) sets `on_inference=False`, clears policy, stops timer.
- **Fix**: skip-tick + N-consecutive-fail counter; only abort after N (e.g. 30 ticks ≈ 1 s):
  ```python
  except Exception as e:
      self._inference_convert_fail_count += 1
      if self._inference_convert_fail_count >= 30:
          # current abort path
      return  # skip this tick
  # success path resets the counter
  self._inference_convert_fail_count = 0
  ```
- **Verified**: read directly.

### F10 🟠 Extra cameras not rejected (only missing)
- **Location**: `inference_manager.py:242-258`
- **Bug**: only checks `expected - provided`; a 1-camera-trained policy with 2 connected cameras passes the check, `_preprocess` (line 309-323) injects an extra `observation.images.scene` tensor → policy crashes on unknown key → caught by broad `except Exception` at line 790 of physical_ai_server → full abort.
- **Fix**: also reject `provided - expected`:
  ```python
  unexpected = provided - set(self._expected_image_keys)
  if unexpected:
      print(f'[FEHLER] Zu viele Kameras: {unexpected}, Modell erwartet nur {self._expected_image_keys}. Tick uebersprungen.', flush=True)
      return None
  ```
- **Connections**: compounds F9 (causes the broad-except abort).
- **Verified**: read inference_manager.py end-to-end.

### F11 🟠 First-tick velocity cap is a no-op
- **Location**: `workflow/safety_envelope.py:109-119` + `inference_manager.py:144` (`reset_policy()` clears `_last_action`)
- **Bug**: `if self._action_max_delta is not None and self._last_action is not None:` — after `reset()`, `_last_action=None` → delta cap skipped for the first action. If policy emits action 1.5 rad away from current pose, JointTrajectoryController interpolates over `time_from_start` ≈ 50 ms → ~30 rad/s ≈ 1700°/s snap. Joint absolute clamp still bounds within `omx_f_config.yaml:42-43` but doesn't bound the *step*.
- **Connections**: `SafetyEnvelope` is shared between InferenceManager and WorkflowManager — fix applies to both.
- **Fix**: seed `_last_action` from current follower joint state on the first tick after `reset()`. Two clean options:
  - Plumb current joint state into `SafetyEnvelope.apply` and seed on first call.
  - Add `SafetyEnvelope.seed_last_action(joints)` and call it from `_inference_timer_callback` before the first `predict()` of each episode.
- **Verified**: read safety_envelope.py end-to-end. Confirmed `_last_action is not None` gate.

### F12 🟠 Action shape mismatch → warn but pass through unclamped
- **Location**: `safety_envelope.py:92-107`
- **Bug**: if `len(action) != len(self._action_min)`, prints German `"Limits werden NICHT erzwungen"` every 30 ticks and **passes action through unchanged**. A 5-joint policy on 6-joint hardware ships raw policy output to the arm.
- **Fix**: return None (= skip tick) on shape mismatch:
  ```python
  else:
      if self._action_shape_warn_counter % 30 == 0:
          print(f'[STOPP] Aktion hat {len(action)} Werte, Limits sind fuer {len(self._action_min)} konfiguriert. Tick verworfen.', flush=True)
      self._action_shape_warn_counter += 1
      return None
  ```
- **Verified**: read directly. The German warning text is verbatim in the file.

### F13 🟠 Image-shape mismatch returns None forever with no escalation
- **Location**: `inference_manager.py:277-288`
- **Bug**: returns None every tick on shape mismatch; no consecutive-skip counter, no TaskStatus.error → student sees frozen arm + log spam, no UI signal.
- **Fix**: counter pattern same as F9 — after ~3 s of consecutive skips publish `TaskStatus.error` (German) and stop timer.

### F14 🟠 PARK/TSAI reject doesn't clear hand-eye buffer
- **Location**: `workflow/calibration_manager.py:387-399`
- **Bug**: when disagreement > 4°/10mm, returns failure but leaves `_handeye_buffers[camera]` populated. Re-clicking "solve" gets same rejection forever.
- **Fix**:
  ```python
  if disagreement_too_large:
      self._handeye_buffers.pop(camera, None)  # force recapture
      return False, 0.0, angle_deg, msg + ' Bitte neu starten und 14 Posen erneut erfassen.'
  ```
- **Verified**: read calibration_manager.py:387-418.

### F15 🟡 `_derive_z_table` biased upward when board is tilted
- **Location**: `calibration_manager.py:420-433`
- **Bug**: medians the board *origin* z; a tilted ChArUco puts origin 2-3 cm above table → projected cubes mis-located by that bias.
- **Fix**: require near-flat capture (reject frames where `R_target2cam[:,2]` deviates >10° from estimated table plane), OR fit a plane through all corners and use plane z. Add UX message: "Tafel flach auf dem Tisch halten."
- **Verified**: math + code path read.

### F16 🟡 (downgrade) `_detect_charuco` accepts ≥4 corners
- **Location**: `calibration_manager.py:207`
- **State**: per-frame floor is 4 corners. With 12 captured frames that's ≥48 observations pooled across `calibrateCameraCharucoExtended`. Usable in practice. Auditor #4 overstated.
- **Fix (optional)**: raise per-frame floor to ~12 with a clearer UX message; not load-bearing.
- **Severity**: MEDIUM (was HIGH).
- **Verified**: read.

---

## Part 3 — Recording side (data_manager / communicator / data_converter)

### F17 🟠 Camera frame rate never compared to `task_info.fps`
- **Location**: `data_manager.py:97-99` + `physical_ai_server.py:596-717` (timer callback)
- **Bug**: timer fires at `task_info.fps` (e.g. 30 Hz). `usb_cam` runs at its config rate (usually 30 Hz, sometimes 15 Hz at high resolutions). At 15 Hz the latest cached `CompressedImage` repeats; dataset gets duplicate frames with linear timestamps from `frame_index/fps`. Trained model learns from strobing data.
- **Fix**: in `communicator._camera_callback` (line 326), track `msg.header.stamp` per camera; at recording start, compute observed Hz from a 1-second window. If observed_hz < fps × 0.8, surface German warning to `TaskStatus.error`.
- **Connections**: F18 (header.stamp ignored generally).
- **Verified**: read communicator.py end-to-end + data_manager.py:97-99.

### F18 🟠 `_camera_callback` ignores `msg.header.stamp`
- **Location**: `communicator.py:326-327`
- **Bug**: callback stores only `msg`, no timestamp tracking. Staleness only detectable via byte-hash. A driver that re-emits the same buffer with incrementing stamps defeats the stale-camera halt.
- **Fix**: store `(msg, time.monotonic())` or extract `msg.header.stamp.sec + nanosec*1e-9` at receive; expose via `get_latest_camera_age(name)`.

### F19 ⚪ `hash()` collision-prone (false alarm)
- **Location**: `data_manager.py:545-572`, `inference_manager.py:182-224`
- **State**: Python's `hash(bytes)` for a 1024-byte tuple is SipHash-1-3 truncated to 64 bits. Birthday-collision probability over a full hour at 30 Hz: ~3×10⁻¹⁰. Practically zero. Auditor's concern is theoretical.
- **Severity**: LOW. Optional fix: switch to `xxhash.xxh64` for slightly faster + deterministic across runs.
- **Verified**: arithmetic.

### F20 🟠 `/dev/videoN` not stable across replug
- **Location**: `gui/app/wsl_bridge.py:104-131` (`list_video_devices`)
- **Bug**: returns raw `/dev/videoN`. Kernel may reassign on hotplug (`/dev/video0` → `/dev/video2`). `.env` is written once at GUI scan time; if student replugs before Start, paths stale.
- **Fix**: resolve to `/dev/v4l/by-id/...` path (stable across replug):
  ```bash
  for d in /dev/video*; do
      stable=$(udevadm info -q symlink -n "$d" 2>/dev/null | tr ' ' '\n' | grep -m1 v4l/by-id || true)
      path=${stable:+/dev/$stable}
      path=${path:-$d}
      ...
  done
  ```
- **Connections**: mirror of the existing `/dev/serial/by-id/` pattern used for the arms (`device_manager.py:36`).
- **Verified**: read wsl_bridge.py.

### F21 🟠 No `wait_for_device` gate for cameras at entrypoint
- **Location**: `robotis_ai_setup/docker/open_manipulator/entrypoint_omx.sh:254-262`
- **Bug**: synchronous `[ -e "$device" ]` once; if usbipd's WSL forwarding lags on cold boot the test fails, `[WARN]` logged, container proceeds **without cameras**.
- **Fix**: add a polling wait analogous to the arm gate at the top of the script:
  ```bash
  wait_for_camera() {
      local dev=$1 name=$2 timeout=${3:-30} t=0
      while [ ! -e "$dev" ] && [ $t -lt $timeout ]; do sleep 1; t=$((t+1)); done
      if [ -e "$dev" ]; then return 0; fi
      echo "[WARN] Camera $name ($dev) not present after ${timeout}s"; return 1
  }
  ```
- **Verified**: read entrypoint Phase 4.

### F22 🟠 `params_1.yaml` applied to both cameras
- **Location**: `open_manipulator/open_manipulator_bringup/launch/camera_usb_cam.launch.py:46-71`
- **Bug**: same upstream `params_1.yaml` for both cameras (resolution/fps/pixel_format identical). Two webcams with different native modes can yield `VIDIOC_S_FMT: Invalid argument` for the second — logs to stderr, healthcheck doesn't notice (F7 compounds).
- **Fix**: declare explicit LaunchArgs and pass per-camera values (or document a single supported mode like YUYV 640×480 @ 30 Hz):
  ```python
  DeclareLaunchArgument('image_width',  default_value='640'),
  DeclareLaunchArgument('image_height', default_value='480'),
  DeclareLaunchArgument('framerate',    default_value='30.0'),
  DeclareLaunchArgument('pixel_format', default_value='yuyv'),
  ```
  Pass these as overrides on the `Node.parameters` list.

### F23 🟠 `_expected_video_paths` no-op on default single-task path
- **Location**: `data_manager.py:283-298` (snapshot) + `303-347` (`_verify_saved_video_files`)
- **Bug**: snapshot reads `dataset.encoders` BEFORE calling `save_episode_without_write_image()` (single-task branch, line 298). Upstream LeRobot populates the encoders dict during/after save in the single-task path, so the snapshot grabs an empty dict → `_expected_video_paths = []` → `_verify_saved_video_files()` returns early at line 316 → the entire mp4-integrity safety net is a no-op for the most common recording mode.
- **Fix**: derive the expected path from canonical LeRobot v2.1 layout when encoders is empty:
  ```python
  if not self._expected_video_paths:
      # LeRobot v2.1 mp4 layout: {root}/videos/chunk-NNN/observation.images.{cam}/episode_{idx:06d}.mp4
      root = Path(self._lerobot_dataset.root)
      idx = self._lerobot_dataset.get_episode_index()
      chunk = idx // 1000
      for cam in self._lerobot_dataset.features:
          if not cam.startswith('observation.images.'): continue
          self._expected_video_paths.append(
              root / 'videos' / f'chunk-{chunk:03d}' / cam / f'episode_{idx:06d}.mp4'
          )
  ```
- **Verified**: read data_manager.py:270-347 + traced upstream wrapper behaviour (`save_episode_without_write_image` defers encoder creation to async writer thread).

---

## Part 4 — Browser side (React + web_video_server)

### F24 🟠 MJPEG stall undetectable
- **Location**: `physical_ai_tools/physical_ai_manager/src/components/ImageGridCell.js:137-139`, `Workshop/CameraFeedOverlay.jsx:52-55`
- **Bug**: `web_video_server` keeps the TCP socket open with `multipart/x-mixed-replace` after `usb_cam` dies; browser sees "loaded" image with frozen content → `img.onerror` never fires. Student records / calibrates against a stale frame.
- **Fix**: side-channel a 1 Hz liveness ping over rosbridge. Subscribe to each camera's `/<name>/image_raw/compressed` (low rate, dropping all but the latest by using `throttle_rate: 1000`); when no message in 2 s, overlay a "Kamera eingefroren" badge identical to the inference-side halt copy.
- **Connections**: F18 (server-side timestamp tracking would also serve this).

### F25 🟠 `naturalSize` captured only on first `onload`
- **Location**: `Workshop/CameraFeedOverlay.jsx:48-51`
- **Bug**: MJPEG fires `onload` only for the first frame; if camera renegotiates resolution mid-session, click-to-mark scales by stale natural size → `/workshop/mark_destination` gets wrong pixel coords → arm misses.
- **Fix**: re-tear-down + recreate the `<img>` on known camera-config change (or query intrinsics from a service every N seconds; intrinsic camera_matrix already exists at `/root/.cache/edubotics/calibration/scene_intrinsics.yaml`).

### F26 🟠 `isCreatingRef` race lets duplicate `<img>` leak
- **Location**: `ImageGridCell.js:90-96, 141-144`
- **Bug**: non-atomic JS flag + 300 ms `await` window → two effect runs can both pass the guard, append two `<img>` tags, only one ref tracked → cleanup leaks one stream (5-8 Mbps per leak).
- **Fix**: use an effect-scoped cancel token:
  ```js
  useEffect(() => {
      let cancelled = false;
      (async () => {
          await new Promise(r => setTimeout(r, staggeredDelay));
          if (cancelled || !containerRef.current) return;
          // ... append <img> ...
      })();
      return () => { cancelled = true; destroyImage(); };
  }, [topic, isActive, rosHost, idx]);
  ```

### F27 🟠 `StudentApp.js:199` blunt-force teardown
- **Location**: `physical_ai_tools/physical_ai_manager/src/StudentApp.js:199`
- **Bug**: `document.querySelectorAll('img[src*="/stream"]')` tears down ANY future component that embeds a `/stream` URL on navigation.
- **Fix**: ref registry — track active stream-component refs in a Redux slice; teardown only iterates the registry.

### F28 🟡 Open-vocab toolbox visible even with toggle OFF
- **Location**: `Workshop/blocks/toolbox.js:72` (unconditional)
- **Bug**: student can drag the block while `cloudVisionEnabled=false`, run it, get a runtime "lokal nicht bekannt + Cloud deaktiviert" error with no UI hint why.
- **Fix**: pass `cloudVisionEnabled` into `buildToolbox(restricted, cloudEnabled)` and conditionally filter out `edubotics_detect_open_vocab` (or grey it out with a tooltip).

### F29 🟡 `cloudVisionEnabled` not persisted across page reload
- **Location**: `features/workshop/workshopSlice.js:81` (initial state `false`)
- **Bug**: no `redux-persist` for the `workshop` slice → page reload resets the toggle.
- **Fix**: in `setCloudVisionEnabled` reducer, also `localStorage.setItem('edubotics_cloud_vision', String(payload))`; hydrate in `initialState` from `localStorage.getItem` (with `=== 'true'` parse).

### F30 🟡 No cost / quota communication to student
- **Location**: `Workshop/RunControls.jsx:315-327` (only label + 1-line title)
- **Bug**: CLAUDE.md §8.3 documents ~$1-2/term/classroom cost and §9.17 introduces a per-user `vision_quota_per_term`. Student sees no live quota readout.
- **Fix**: extend `GET /me` to expose `vision_used_per_term` and `vision_quota_per_term`; render a quota chip next to the toggle: `"Cloud-Erkennung: 42/200 verbleibend"`.

### F31 🟡 Confidence not shown on detection overlay
- **Location**: `Workshop/CameraFeedOverlay.jsx:161-171` (`DetectionOverlay`)
- **Bug**: renders only `d.label`. With OWLv2's default 0.10 threshold (F45), low-confidence boxes are indistinguishable from high-confidence locks.
- **Fix**: render `${d.label} ${Math.round((d.confidence||0)*100)}%`.

### F32 🟡 Open-vocab block has no visual differentiator
- **Location**: `Workshop/blocks/perception.js:120` (`colour: PERCEPTION_COLOR`)
- **Fix**: distinct hue + cloud emoji in `message0`: e.g. `'☁ finde Objekt mit Beschreibung %1'`. Visually signals "uses the internet".

### F33 🟡 Free-text prompt has no client validation
- **Location**: `Workshop/blocks/perception.js:118`
- **Fix**: register a `edubotics_validate_open_vocab_prompt` extension: trim, reject empty, cap at e.g. 80 chars (matches `vision.py:40` `MAX_PROMPT_CHARS=200` but tighter for student UX).

### F34 🟡 Cloud-burst errors not toasted, only in `WorkflowStatus`
- **Location**: `useRosTopicSubscription.js:759` writes `error` into Redux; `RunControls.jsx:343-350` displays it inline; no `react-hot-toast.error()` call.
- **Fix**: in the topic subscription, on `phase === 'error'`, fire `toast.error(msg.error)`.

### F35 🟡 Stream quality drift (50 vs 70)
- **Location**: `ImageGridCell.js:131` vs `Workshop/CameraFeedOverlay.jsx:45`
- **Fix**: single `STREAM_QUALITY` constant in `constants/`; ideally a Redux setting so the teacher can tune for school Wi-Fi.

### F36 ⚪ `web_video_server` launched with zero parameters
- **Location**: `physical_ai_tools/physical_ai_server/launch/physical_ai_server_bringup.launch.py:54-60`
- **Risk**: defaults bind `0.0.0.0:8080`; compose loopback-bind (`docker-compose.yml:74`) saves us today. Defence-in-depth fix:
  ```python
  web_video_server_node = Node(
      package='web_video_server', executable='web_video_server',
      parameters=[{'address': '127.0.0.1', 'port': 8080}],
  )
  ```

---

## Part 5 — Modal worker (OWLv2)

### F37 🟠 Modal worker `error` field silently dropped by proxy
- **Location**: `robotis_ai_setup/modal_training/vision_app.py:247-252` (returns `{error: ...}`) + `cloud_training_api/app/routes/vision.py:264-268` (reads only `detections`, `cold_start`).
- **Bug**: inference exception in the worker → React sees "no detections" with no diagnostic.
- **Fix**: in proxy, after `raw = response["result"] or {}`:
  ```python
  worker_error = raw.get('error') if isinstance(raw, dict) else None
  if worker_error:
      logger.warning('OWLv2 worker error: %s', worker_error)
  ```
  Optionally add a `worker_error: Optional[str]` field on `DetectResponse` (forward to React).
- **Verified**: read both files.

### F38 🟠 Proxy timeout 10 s < worker timeout 120 s
- **Location**: `cloud_training_api/app/routes/vision.py:50` (`MODAL_INVOKE_TIMEOUT_S=10`) vs `vision_app.py:122` (`timeout=120`)
- **Bug**: cold-start path that the worker would complete in 30-60 s always 504s to React → refund fires (good) but UX is bad.
- **Fix**: bump proxy default to 30 s. Optionally also set `min_containers=1` during teacher hours via env var to skip cold start entirely.

### F39 🟠 EXIF transpose is a no-op
- **Location**: `vision_app.py:204` + `217-220`
- **Bug**: `Image.open(...).convert("RGB")` (line 204) discards EXIF metadata; subsequent `ImageOps.exif_transpose` (line 218) has no orientation tag to read.
- **Fix**: swap order:
  ```python
  img = Image.open(io.BytesIO(image_bytes))
  try: img = ImageOps.exif_transpose(img)
  except Exception: pass
  img = img.convert('RGB')
  ```
- **Verified**: read.

### F40 🟠 `from_pretrained` may invisibly bind to CUDA
- **Location**: `vision_app.py:150-151`
- **State**: Modal snapshot builders are CPU-only, so today safe. If a future `transformers` upgrade pulls `accelerate` transitively, `device_map="auto"` defaults could change.
- **Fix (defence in depth)**:
  ```python
  self.model = Owlv2ForObjectDetection.from_pretrained(
      MODEL_NAME, torch_dtype=torch.float32, device_map=None,
  )
  self.model.to('cpu')
  ```

### F41 🟡 No FP16 on T4
- **Location**: `vision_app.py:155-166` (`bind_device`)
- **Opportunity**: T4 has solid FP16 support. `.half()` halves memory + ~1.6× speedup at no accuracy cost for OWLv2.
- **Fix**:
  ```python
  if self._torch.cuda.is_available():
      self.model = self.model.to('cuda').half()
      self.dtype = torch.float16
  ```
  Then `inputs.to(self.device, dtype=self.dtype)` in `detect`.

### F42 🟡 Volume `edubotics-vision-cache` grows unbounded
- **Location**: `vision_app.py:88-91`
- **Risk**: if HF rotates the model SHA, old weights stay. Currently the model card hasn't rotated; pin to a `revision=` hash:
  ```python
  self.model = Owlv2ForObjectDetection.from_pretrained(MODEL_NAME, revision='<sha>')
  ```

### F43 🟡 `edubotics-vision-secrets` likely empty
- **Location**: `vision_app.py:113` + helper `_vision_secret` (lines 94-106)
- **State**: nothing in `vision_app.py` reads env vars from the secret bundle. The isolation comment is correct defence-in-depth (don't fall back to training secrets), but the bundle is empty by design. The deploy-time check is the only enforcer.
- **Fix (optional)**: drop the `secrets=[_vision_secret()]` from `@app.cls` if you confirm nothing in transformers' OWLv2 path reads HF_TOKEN for a public model.

### F44 🟡 `huggingface_hub>=0.25.0` unpinned
- **Location**: `vision_app.py:70`
- **Fix**: pin to a concrete version compatible with `transformers==4.46.0`:
  ```python
  'huggingface_hub==0.26.2',
  ```

### F45 🟡 `score_threshold=0.10` default too permissive
- **Location**: `vision_app.py:173`
- **Fix**: bump worker default to `0.25`; proxy's default already gets passed through, so cap at the worker.

---

## Part 6 — Cloud-API proxy + Supabase quota

### F46 🟠 Exception text leaks to student
- **Location**: `vision.py:175`
- **Bug**: `detail=f"Cloud-Erkennung ist fehlgeschlagen: {e}"` exposes Python exception class + Modal internal strings (audit §3.21 fixed this elsewhere).
- **Fix**: log full `exc_info=True` server-side, return fixed German `"Cloud-Erkennung ist fehlgeschlagen. Bitte erneut versuchen."` with a correlation id in `error_id` field if debugging needed.

### F47 🟠 503 mis-classifies any postgrest error as "RPC missing"
- **Location**: `vision.py:213-221`
- **Bug**: bare `except Exception` over `consume_vision_quota` RPC call. Under load, transient pool exhaustion / row-lock timeout → 503 to student.
- **Fix**: inspect error code:
  ```python
  except postgrest.APIError as e:
      if e.code in ('PGRST202', '42883'):  # function not found
          raise HTTPException(503, ...)
      logger.exception('quota RPC error'); raise HTTPException(500, 'Datenbankfehler.')
  ```

### F48 🟠 Unbounded `vision_used_per_term` counter
- **Location**: `robotis_ai_setup/supabase/017_vision_quota.sql:64-71`
- **Bug**: when `vision_quota_per_term IS NULL`, the SQL still increments `vision_used_per_term`. Today every student is NULL-quota → counter grows forever. If admin later flips a student to `quota=200`, they're INSTANTLY locked out (used > 200 already).
- **Fix**: skip the UPDATE in the unbounded path:
  ```sql
  IF v_quota IS NULL THEN
      RETURN QUERY SELECT TRUE, NULL::INTEGER;
      RETURN;
  END IF;
  -- existing bounded path unchanged
  ```
  Plus: ship migration 018 with default `vision_quota_per_term=200` for new users + an admin endpoint (F49).
- **Verified**: I just read 017_vision_quota.sql:64-71 directly.

### F49 🟠 No path to set `vision_quota_per_term`
- **Location**: missing in `cloud_training_api/app/routes/admin.py`, `teacher.py`
- **Fix**: add `PATCH /admin/teachers/{id}/vision-quota` and `PATCH /teacher/students/{id}/vision-quota` (mirrors `adjust_student_credits` pattern). Both call a new `set_vision_quota(p_target_id, p_quota, p_caller_id)` RPC with role-based authorization.

### F50 🟠 Modal-not-deployed vs worker-down conflated
- **Location**: `vision.py:139-148`
- **Bug**: `modal.Function.from_name` failing → 503 "nicht erreichbar". Same error for "deploy never ran" vs "Modal API is down".
- **Fix**: distinguish exception types — `modal.exception.NotFoundError` → "Cloud-Erkennung ist auf dieser Installation noch nicht installiert. Bitte den Lehrer fragen." vs other errors → "Cloud-Erkennung ist vorübergehend nicht erreichbar."

### F51 🟡 `Detection.bbox` no length validation
- **Location**: `vision.py:111-114`
- **Fix**: `bbox: list[float] = Field(..., min_length=4, max_length=4)`.

### F52 🟡 `vision.py:226` fail-open on unknown RPC shape
- **Bug**: `if not isinstance(row, dict): return True, None` — if Supabase changes shape, calls allowed + uncounted. Should be fail-closed.
- **Fix**: `return False, None; logger.error('unexpected RPC shape: %r', row)`.

### F53 🟡 Refund swallows exceptions silently
- **Location**: `vision.py:240`
- **Fix**: bump `logger.info` to `logger.warning`. Add counter metric.

---

## Part 7 — On-host cloud-vision bridge (workflow runtime)

### F54 🟠 `cv.get('enabled')` ignored — toggle is decorative
- **Location**: `perception_blocks.py:202-208`
- **Bug**: only checks `callable(burst)`. Server-side ALWAYS binds `cloud_burst = self._cloud_vision_burst` (physical_ai_server.py:2354) regardless of `cloud_vision_enabled`. Today the burst raises `NotImplementedError` so the bug is invisible; once F4 is wired, student's OFF toggle won't actually disable cloud calls → quota + cost leak.
- **Fix**:
  ```python
  if not cv.get('enabled') or not callable(burst):
      raise WorkflowError(
          f'Begriff "{prompt}" ist lokal nicht bekannt und Cloud-Erkennung '
          'ist deaktiviert. Bitte aktivieren oder einen bekannten Begriff verwenden.'
      )
  ```
- **Verified**: read perception_blocks.py:185-216 + physical_ai_server.py:2340-2360.

### F55 🟠 Synonym dict hardcoded in source
- **Location**: `physical_ai_server.py:2502-2535` (18 entries)
- **Limitation**: adding "Stift", "Lineal", "Maus" needs a Docker rebuild + Hub push + student image re-pull.
- **Fix**: load from a YAML in the `edubotics_calib` volume at `/root/.cache/edubotics/cloud_vision_synonyms.yaml` (volume survives `docker compose down`); fall back to the hardcoded dict if file missing/corrupt. Also fixes the **English-vs-German** label drift surfaced by F1: rewrite the dict to use German `Tasse`/`Banane`/… matching `COCO_CLASSES`.

### F56 🟡 Stub German error message redundancy
- **Location**: `physical_ai_server.py:2551-2553`
- **State**: stub raises `NotImplementedError('Cloud-Erkennung ist auf dieser Installation noch nicht aktiviert.')`. `perception_blocks.py:214-215` catches and re-raises as `WorkflowError(f'Cloud-Erkennung fehlgeschlagen: {e}')` → student sees "Cloud-Erkennung fehlgeschlagen: Cloud-Erkennung ist…".
- **Fix**: have the handler detect `NotImplementedError` specifically and re-raise with the clean original message.

### F57 🟡 No `ctx.should_stop()` between scene-frame fetch and burst
- **Location**: `perception_blocks.py:209-213`
- **Risk (post-F4)**: if `cloud_burst` becomes a 5-15 s HTTPS call, a `/workflow/stop` mid-burst won't cancel.
- **Fix**: check `ctx.should_stop()` before `burst(...)`; pass a per-request timeout the burst implementation must honour.

### F58 🟡 Stale-frame burst
- **Location**: `perception_blocks.py:209` (`ctx.get_scene_frame()`)
- **Bug**: returns latest cached frame with no freshness check. Inference side has a 5 s halt; workflow runtime has nothing equivalent. Could burst on a 30-s-stale frame and waste Modal time.
- **Fix**: in `_get_latest_camera_frame` (physical_ai_server.py:1498-1517), also return age and reject if >2 s.

---

## Part 8 — Other items

### F59 🟡 `perception.py` docstring claims HSV but code uses LAB
- **Location**: `workflow/perception.py:18-20`
- **Fix**: update docstring to say LAB.

### F60 🟡 `cv2.aruco.getBoardObjectAndImagePoints` deprecated in OpenCV 4.10
- **Location**: `workflow/calibration_manager.py:272`
- **Fix**: switch to `board.matchImagePoints(corners, ids)` (OpenCV 4.7+).

### F61 ⚪ Bare `except` swallows YAML corruption
- **Location**: `workflow/calibration_manager.py:121-135` (`_load_persisted_intrinsics`)
- **Fix**: `except Exception as e: logger.warning('intrinsics YAML for %s corrupt: %s', camera, e)`.

---

## False alarms — for reference

| Issue | Why it's not real |
|---|---|
| `_action_duration_ns` never set in inference mode | `init_robot_control_parameters_from_user_task` builds DataManager for inference too; `DataManager.__init__:97-99` calls `set_action_duration_from_fps`. |
| DDS QoS mismatch on subscribers | `MultiSubscriber` defaults `BEST_EFFORT, KEEP_LAST, depth=1` matching `usb_cam`. |
| rclpy executor race on `camera_topic_msgs` | Single-threaded executor serialises callbacks and timer. |
| Mixed-content on Railway HTTPS build | `isCloudOnlyMode()` early-returns before any `:8080` fetch. |
| `/dev:/dev` privileged on `open_manipulator` | Necessary for `/dev/ttyACM*` + `/dev/video*`. `physical_ai_server` is non-privileged. |
| `text_labels` vs `text_queries` fallback wrong | Correct for transformers 4.46.0. |
| `target_sizes` (H,W) order wrong | `img.size[::-1]` reverses (W,H) → (H,W) — exactly what OWLv2 expects. |
| `cold_start` flag broken with snapshots | Works as documented. |
| GPU OOM risk | OWLv2-base + 1.5 MB image fits trivially in T4 16 GB. |
| Rate-limit 429 burns quota | Middleware returns JSONResponse before route runs. |
| `_class_nms` confidence threshold duplication | Cosmetic; harmless. |
| Otsu auto-polarity captures background | `_is_interior` filter explicitly excludes frame-edge contours. |
| `MIN_BLOB_AREA_PX=400` too small | At 1 m / 640×480 a 3 cm cube ≈ 625 px². 400 is fine. |
| Hash collisions on stale-camera | Birthday-bound ~3×10⁻¹⁰ over an hour. Practically zero. |
| `naturalSize` first-onload | True but UI bug; not a security/safety issue. F25 captures real impact. |

---

## Suggested fix order

Apply F1–F4 in one PR (the entire Roboter Studio object-detection feature flips from broken to working). Then F11/F12 (safety envelope first-tick + shape-mismatch — arm-safety). Then F7 + F22/F21 (camera healthcheck + entrypoint resilience). Then F23 (recording integrity). Then the cloud-vision bridge F46–F58 batch. Browser-side F24-F36 last (UX polish).

The first three CRITICALs (F1, F2, F3) are all in `workflow/perception.py` + `workflow/handlers/perception_blocks.py` — a single focused session can unblock the entire object-detection user story.
