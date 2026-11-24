# Research 04 — iPad use, cross-platform architecture and the web app's host contract

Date: 2026-11-24. Repo HEAD `dba3ac81` (release 2.41.0). Author: research agent 4 of 4.
Scope: the owner's new requirement (candidates must be tested for use **with iPads** and should make
**full use of the board's Bluetooth and Wi-Fi**), measured against the code and against current
primary sources. Read-only on the repo; scratch probes under the session scratchpad (`agent4/`).

**Tags.** **VERIFIED** = proven by reading code (`file::symbol`), a command I ran, or a primary
source with URL (all fetched 2026-11-24). **REFUTED** = a claim (brief, CLAUDE.md, the dropped plan)
the evidence contradicts. **UNVERIFIED-DEVICE** = needs an iPad / Windows PC / robot; the exact test
is given. **JUDGMENT** = my opinion, kept apart from facts. No iPad, no iOS simulator (only Xcode
Command Line Tools are installed — `xcode-select -p` → `/Library/Developer/CommandLineTools`) and no
robot were available, so every on-device behaviour below is UNVERIFIED-DEVICE.

---

## 0. Executive findings

1. **No iPad browser can talk to the arm.** WebKit's official positions are *oppose* for Web
   Bluetooth (#570), Web Serial (#199), WebUSB (#68) and WebHID (#510) — VERIFIED (WebKit
   standards-positions `summary.json`). No non-WebKit engine has shipped on iOS/iPadOS in the EU or
   Japan as of 2026-11-20 — VERIFIED (Mozilla blog, 2026-11-20). Every iPad that drives the arm
   **directly** therefore needs a **native app** (CoreBluetooth); a thin native bridge exposing BLE to
   a WKWebView has App Store precedent (Bluefy, WebBLE) — VERIFIED (App Store listings).
2. **On an iPad the arm link is BLE, not USB.** USB serial to a non-MFi device is only possible with
   DriverKit, which exists on iPadOS **only for M-series iPads** and needs an Apple-granted
   entitlement tied to the hardware vendor ID — VERIFIED (Apple DriverKit / USBDriverKit docs).
   ExternalAccessory is MFi-only — VERIFIED. Classic Bluetooth SPP is MFi-only on iOS (same doc).
   So: BLE GATT on every iPad; Wi-Fi as an option.
3. **Cameras are the hard wall, not the arm.** Every robot function except cloud-side training needs
   the scene/tower camera (and recording a second one): since 2.41.0 every real Roboter-Studio start
   is refused without the tower calibration (`workflow/autocal/service.py::SceneCalibration.program_start_gate`)
   — VERIFIED. External UVC cameras on iPad: **USB-C iPads only, iPadOS 17+, native AVFoundation**
   (WWDC23 10106) — VERIFIED. **Safari/WKWebView `getUserMedia` very probably cannot see them**: WebKit
   main soft-links and requests only `AVCaptureDeviceTypeExternalUnknown`
   (`AVVideoCaptureSource::cameraCaptureDeviceTypes`, PAL `AVFoundationSoftLink.h`), a constant Apple
   documents for **macOS only**; the iPadOS constant is `AVCaptureDeviceType.external` — VERIFIED at
   source level, device behaviour UNVERIFIED-DEVICE. The still-supported Lightning **iPad 9** (on
   iPadOS 27's support list) can attach no camera at all. Two simultaneous external cameras on one
   USB 2.0 port (iPad A16 is USB 2.0, 480 Mb/s) — UNVERIFIED-DEVICE.
4. **LeRobot cannot run on an iPad, in any form.** `lerobot==0.5.1` requires `torch`, `torchvision`,
   `datasets`, `av`, `opencv-python-headless`…, and `lerobot/datasets/lerobot_dataset.py`,
   `utils.py`, `video_utils.py` import `torch` at module top — VERIFIED (PyPI metadata + wheel
   source, read only). torch, PyAV, pyarrow, opencv-python, pupil-apriltags have **no iOS wheels**
   on PyPI; Pyodide has no torch/PyAV/pupil-apriltags — VERIFIED. An iPad that records must hand
   packaging to Python elsewhere (PC or a cloud packager) or re-implement the v3.0 writer (drift risk
   Rule §5 exists to avoid).
5. **The Python engine cannot move onto an iPad unchanged.** Embedded CPython on iOS has threads but
   **no subprocess / multiprocessing / IPC** ("will either lock up, or crash") — VERIFIED (Python docs).
   Pyodide has **no threads** — VERIFIED; `workflow/` (29 468 lines) is thread-based (run threads, hat
   threads, `ctx.motion_lock` sliced waits, `threading.local` frames). Every Daten/HF/edit helper and
   the code runner are separate processes today. numpy has iOS wheels only via the BeeWare channel —
   VERIFIED; OpenCV, apriltag, PyAV, pyarrow have none.
6. **The student-code sandbox has no iPad equivalent of today's process model.** An iOS app cannot
   spawn processes; the only killable isolation unit on iPad is a WebAssembly Worker (Pyodide for
   Python; CheerpJ for Java — Safari mobile supported, `javac` runs client-side in JavaFiddle,
   commercial licence needed) — VERIFIED (vendor pages); viability for our four Rule-§2 conditions is
   a spike, not a fact.
7. **The React app is already a LAN client on the Orange Pi; it is not one on Windows.** The SPA is
   same-origin by construction (`utils/piMode.js::localRosbridgeUrl`, `videoStreamBase`), works over
   plain HTTP (no `crypto.subtle`/`randomUUID` dependence — `features/hfToken/hfTokenSeal.js`,
   `services/cloudTrainingApi.js`), and has a robot login for LAN clients (`features/piAuth/piAuth.js`)
   — VERIFIED. The Windows stack binds `127.0.0.1:80` and nginx answers 444 to any Host but
   localhost (`nginx.conf` `$edubotics_host_is_local`) — VERIFIED. A **no-admin** Windows app cannot
   accept LAN connections either: for a non-admin user the firewall prompt creates **block** rules
   "no matter what option is selected" — VERIFIED (Microsoft Learn, updated 2026-10-08).
8. **The cheapest iPad candidates exist today and are untested:** (a) iPad Safari as the screen of an
   Orange-Pi station (the Pi line continues per brief D11); (b) iPad Safari for the cloud-only pages
   (`?cloud=1` keeps Startseite + Training, `utils/cloudMode.js::isCloudOnlyMode`). (b) needs a
   small "cloud host" build mode first: on any non-loopback host the SPA fail-closes into Pi mode
   (`utils/piMode.js` `isPi = marker || !isLoopbackPage()`), and the teacher-web build bounces
   students (wrong-role bounce) — VERIFIED by reading.
9. **"Full use of Bluetooth and Wi-Fi" has hard limits on the HAT (A)'s classic ESP32:** BLE **4.2
   only** (no 2M PHY), ~90 KB/s board-to-board peak — VERIFIED (Espressif FAQ); one 2.4 GHz radio
   time-shared with Wi-Fi; **SoftAP + BLE is "supported but unstable"**, STA + BLE stable — VERIFIED
   (ESP-IDF v6.1.1 RF coexistence). iOS BLE: interval ≥ 15 ms (multiple of 15, may be answered
   with 30), **supervision timeout 6–18 s** — VERIFIED (Apple DTS, Jun 2024): link-layer loss
   detection is far too slow for safety, the firmware watchdog must run on application frames.
10. **Touch readiness is partial.** Every keyboard action I traced has an on-screen button (Aufnahme
    `model/recordKeys.js`, Vormachen `teach/teachGates.js::teachKeyOffered`, Daten `Transport.jsx`) —
    VERIFIED. Gaps: 174 `title=` tooltips (62 files also set `disabled=`) carry disabled reasons that
    touch users never see; the Vormachen review handles are 16 px wide with no `touch-action`
    (`teach/ReviewStrip.jsx`); three outside-click handlers listen to `mousedown`; 16 `100vh`/`h-screen`
    uses and no `dvh`/safe-area — VERIFIED by reading; effect on iPad UNVERIFIED-DEVICE.
11. **Recommendation (JUDGMENT):** build the brief's full-function Python engine (Windows first, macOS/
    Linux from the same code), and take **five cheap decisions now** that make every later client
    tier additive: one board protocol over USB-CDC / BLE-GATT / Wi-Fi-TCP; one authenticated engine
    API served on loopback in any native shell; a raw-episode → packager seam that keeps LeRobot
    authoritative; a transport-neutral sandbox contract; touch-readiness as a UI rule. Ship iPad
    tiers 0/1 now, and gate an iPad-native tier behind seven proofs (§9.1).

---

## 1. What the dropped browser-only plan teaches

`docs/BROWSER_ONLY_DEPLOY_PLAN.md` (2026-07-12, two revisions `1cb0462e`, `b3d2ba67`) proposed: a
Chrome tab as the 100 Hz hardware runtime over Web Serial (OpenRB-150 passthrough), a per-student
**cloud session service** running the non-real-time half of `physical_ai_server`, Modal GPU inference,
static SPA hosting.

**Still valid (re-checked today)**

| Plan claim | Status today |
|---|---|
| `workflow/` is ROS-free behind injected callables | VERIFIED, now **19** constructor parameters (`workflow_manager.py::WorkflowManager.__init__`), not "~14". Module-level `rclpy` imports: 6 files; plus 2 lazy ROS adapters (`workflow/studio_camera.py::_ros_interfaces`, `daten/node_service.py`) — both need rebinding too. |
| Chrome Web Serial + managed auto-grant → Chromebooks viable | VERIFIED: `SerialAllowUsbDevicesForUrls` / `SerialAllowAllPortsForUrls` (chrome_os 94+), `WebUsbAllowDevicesForUrls` (74+) in Chromium's policy definitions. **New nuance:** Web Bluetooth has **no** per-URL auto-grant, only `DefaultWebBluetoothGuardSetting` (ask/block), and `navigator.bluetooth.getDevices()` is still behind a flag (MDN BCD) → a chooser gesture per session. |
| Keep LeRobot packaging in Python ("server-side packager"), reject a JS re-implementation | VERIFIED as the only Rule-§5-compatible route for any non-Python client: LeRobot imports torch (finding 4). |
| Public HTTPS page → LAN device is a landmine | VERIFIED (Chrome LNA; WebKit/W3C mixed content: anything not upgradeable is blockable, WebSocket goes through Fetch). |
| Don't adopt LeRobot's pickle-based async inference | Honoured: Cloud-Inferenz has its own typed wire (`inference/cloud_protocol.py`). |

**Obsolete or refuted**

| Plan claim | Why |
|---|---|
| OpenRB-150 passthrough, Dynamixel Bus Watchdog as the dead-tab safety | OMX retired; Feetech servos have no watchdog (brief §1) → firmware watchdog. |
| Browser Worker as the 100 Hz real-time endpoint | Superseded by the brief's firmware loop (removes tab-lifecycle/jitter risk entirely). |
| Recorder = 15 ms cross-camera pairing | REFUTED for recording: round 5 replaced it with the slot sampler (`communication/capture_timeline.py`); pairing survives only for inference (CLAUDE.md "Every dataset frame is a decided slot"). |
| ChArUco/touch-off calibration server-side | Replaced by the tower kit autocal (2.41.0); `test/test_legacy_calibration_is_gone.py`. |
| "Firefox/Safari have no Web Serial" | Partly REFUTED: MDN BCD lists **Firefox 151** desktop with Web Serial; Safari still none. |

**Why it was dropped.** Documented: owner decision 2026-11-20 "native app + arm firmware … ship
software, no extra box per robot", and the OMX servo contract became unnecessary (brief header).
Inferred (JUDGMENT, not documented): Chrome-only (excluded iPads and Safari), an always-online cloud
session service (cost, ops, latency, continuous classroom video in the cloud), the browser tab as a
hard-real-time endpoint, a new remote arm-command channel, and moving the single-process,
lock-ordered `physical_ai_server` invariants into a multi-tenant service.

**Brief numbers re-measured at `dba3ac81` (REFUTED as stated, minor drift):** `physical_ai_server.py`
is 8 542 lines (brief: 8 908); `feetech_arm_node.py` 2 643 (brief: 2 358); module-level `rclpy`
importers are still 6, but two more modules reach ROS lazily (`workflow/studio_camera.py`,
`daten/node_service.py`). The brief's "wireless/tablet control later" (§4.1) cannot mean the
unchanged React UI in an iPad **browser** driving the arm: Safari has no device API (§3.1).

**Lesson for the iPad (JUDGMENT):** the plan hit the same three walls an iPad hits — (a) the device
link (Safari: none), (b) LeRobot packaging must stay Python, (c) where the engine lives. The brief
solves (a) for Windows with USB+firmware and (c) by putting Python on the PC; for the iPad, (a)–(c)
are open again.

---

## 2. The SPA's host contract (student build), from the code

Size: 398 non-test source files / 83 251 lines; 349 test files / 83 959 lines (VERIFIED, `find … | wc`).
Coupling counts (non-test files): ROS transport 56, cloud API/Supabase 28, Pi-mode/control bridge 26,
Daten HTTP 6 (VERIFIED by grep). Bundle target: Vite 8.0.16 default `baseline-widely-available` =
`safari16.4`/`ios16.4` (no `build.target` override in `vite.config.js`) — VERIFIED.

### 2.1 Transports and endpoints

| Dependency | Where (symbol) | What exactly | iPad note |
|---|---|---|---|
| rosbridge v2 (JSON over WebSocket) | `utils/rosConnectionManager.js::RosConnectionManager` (singleton, `new ROSLIB.Ros({url})` default JSON transport, 10 s connect timeout, local URL reconnect ≤ 2 s with no cap, `setConnectGate` for the Pi login) | roslib **1.4.1**; features used: `Ros`, `Topic.subscribe/unsubscribe` (23 sites), `Service.callService` (9), `ServiceRequest`; options `throttle_rate`, `queue_length` (one site passes `queue_size`, which roslib ignores for subscribers); no `compression`, no `latch`. **The SPA never advertises or publishes a topic** (grep for `.advertise(`/`.publish(` on ROSLIB topics: none). ≈31 service names (30 interface types + `std_srvs/Trigger`) and ≈20 topic names. 40 `.srv` / 10 `.msg` exist in `physical_ai_interfaces`. | VERIFIED. An emulator needs `subscribe` (with throttle/queue semantics), `unsubscribe`, `call_service` and server `publish`/`service_response` only. |
| Same-origin URL rule | `utils/piMode.js::localRosbridgeUrl` (`ws(s)://${window.location.host}/rosbridge`), `videoStreamBase` (`/video`) | The page must be served by the engine host. | VERIFIED. A native shell must serve the SPA from a loopback server (or the code must learn a "shell" mode). |
| MJPEG camera streams | `components/ImageGridCell.js`, `components/Workshop/CameraFeedOverlay.jsx`, `Home/RobotHero.jsx` | `<img src="/video/stream?…type=ros_compressed…">` (web_video_server `multipart/x-mixed-replace`); CameraFeedOverlay maps tap coordinates through `naturalSize` for "Ziel in der Kamera"; a 1 Hz CompressedImage rosbridge subscription for liveness (base64 JPEG in JSON). | UNVERIFIED-DEVICE: MJPEG in `<img>` on iPadOS Safari (WebKit kept multipart images but has historic iOS reports). Test: open the Pi Aufnahme page on an iPad, watch both tiles for 10 min. |
| Daten sidecar | `features/editDataset/api/datenHttp.js` (`/daten-api/v1/lib|ds/<token>/…`), `hooks/useEpisodePlayer.js` | GET/HEAD JSON, thumbnails, `<video>` MP4 with **Range** (server `daten/http_server.py::parse_range`, `Accept-Ranges: bytes`, 206), `requestVideoFrameCallback` frame clock. Codec h264 via LeRobot (`lerobot_dataset_wrapper.py::EDUBOTICS_VCODEC='h264'`). | Safari needs Range for media — implemented (VERIFIED). rVFC Safari 15.4 (BCD). H.264 decodes on every iPad. |
| Control bridge | `utils/piMode.js::rsControlBase` → `http://localhost:8769` (Windows GUI) or `/api/system` (Pi) | `GET /roboter-studio/status`, mode POST (`LeaderToggle.jsx`, `hooks/useRsBridgeStatus.js`). | On a non-loopback page the SPA assumes a Pi (`robotIsRemote`/fail-closed piMode). |
| Pi agent | `/pi-mode.json`, `/api/system/{status,auth/*,robot-key,update/check,protokoll,cameras/*,environment/*,scan-arms,robot-type,scene-camera-profile,factory-reset,netzwerk-check}` (`utils/piMode.js`, `features/piAuth/piAuth.js`, `pages/SystemPage.js`) | Robot login = cloud ticket → HttpOnly SameSite=Strict cookie; nginx `auth_request` on `/rosbridge`, `/video/stream`, `/daten-api/`. | This is the existing LAN-client contract. |
| Cloud API | `services/cloudConfig.js::CLOUD_API_URL`, `services/apiClient.js::apiRequest` (Bearer) | HTTPS + CORS; the student calls are on `main.py::_ANY_ORIGIN_BEARER_ROUTES` (40 routes; VERIFIED by AST) → callable from any origin, incl. a custom-scheme WKWebView. | Origin-agnostic already. |
| Supabase | `lib/supabaseClient.js` (`createClient`, session in localStorage, `realtime.setAuth` on every auth event) | Auth + PostgREST + Realtime `postgres_changes` over wss. | Works from any HTTPS-capable client. |
| Self-update | `hooks/useVersionCheck.js` (`/version.json`), `features/legal` (`/version` on the cloud) | buildId reload loop. | In an App-Store app the bundle updates only with the app (JUDGMENT: operational change). |
| Storage | localStorage (allowlist `utils/sessionScope.js`), IndexedDB via `idb-keyval` (`Workshop/useAutosave.js`, `code/useCodeAutosave.js`) | Drafts, prefs, Supabase session. | Safari ITP deletes script-writable storage after 7 days of Safari use without interaction (WebKit 2020-03-24) — drafts can vanish after holidays (cloud copies unaffected). Quotas: browser apps up to 60 % disk/origin, other WebKit apps 15 % (WebKit 2023-08-10). |
| WebGL | `components/UrdfTwin.jsx` (three 0.184, urdf-loader 0.12.7, lazy chunk; OrbitControls) | WebGL2, STL meshes from `public/v1-urdf/` (3.6 MB). | Fine on iPad GPUs (JUDGMENT). |
| Lazy chunks | dynamic `import()` (CodeMirror, Blockly plugins, three, noble crypto, `codeInsert`) | Same-origin fetches. | Fine. |
| Clipboard | `MyModels.js` (`writeText` in click), `teach/TeachOverlay.jsx::copyLines` (after `await target.insertProgram`, which may lazy-load) | Secure context only; Safari: "must be called within user gesture event handlers" (MDN BCD note) | UNVERIFIED-DEVICE: the post-`await` write may be refused on Safari → the existing click-first hint shows. Test: Vormachen → "Als Programm einfügen" without cursor on iPad. |
| Drag and drop | `sammlung/SammlungDrawer.jsx`, `code/MethodenPanel.jsx` (`draggable`) → `code/CodeEditor.jsx` drop with custom `code/snippetMime.js::SNIPPET_MIME` | HTML5 DnD. "Einfügen" buttons exist as the non-drag path. | UNVERIFIED-DEVICE: custom MIME types in iPadOS DnD. |
| File import/export | `Workshop/ToolbarButtons.jsx` (Blob + `<a download>`, `<input type=file>` + FileReader) | JSON workflow export/import. | Should route to the Files app (UNVERIFIED-DEVICE, minor). |
| Audio | `hooks/useRosTopicSubscription.js` (AudioContext unlocked on `touchstart/touchend/mousedown/keydown/click`), `Record/recordSounds.js`, `teach/teachSounds.js` | Beeps. | Touch unlock already wired (VERIFIED). |
| Other APIs | `ResizeObserver` (17), `OffscreenCanvas` (with fallback, `AssetCardInflater.js`), `structuredClone`, keepalive `fetch` on `pagehide` (cloud inference end, Pi logout) | — | All in Safari ≥ 16.4 per BCD. |

### 2.2 Secure-context sensitivity (matters for any LAN HTTP client)

Already designed for plain HTTP (the Pi): token encryption uses `@noble/*`, not `crypto.subtle`
(`features/hfToken/hfTokenSeal.js`); UUIDs fall back to `getRandomValues`
(`services/cloudTrainingApi.js`, `sammlung/destinationStore.js`) — VERIFIED. What an HTTP page loses
on iPad: `navigator.clipboard` (known: KNOWN-ISSUES rig gate CS-R3), `getUserMedia`, WebCodecs
(VideoEncoder is secure-context-only per MDN), service workers. None is needed by a pure UI client.

### 2.3 Touch readiness

| Area | Evidence | Status / gap |
|---|---|---|
| Aufnahme keys (Leertaste, ←/→, Strg+Umschalt+X, Esc) | `Record/model/recordKeys.js::keyToAction` accepts `ctrlKey || metaKey`; ActionBar buttons for every action | VERIFIED complete; a Magic Keyboard's ⌘ works for the end chord. |
| Vormachen keys (Space/F/P/Z/Esc/Enter/R/Entf) | `teach/teachGates.js::teachKeyOffered` is the single predicate for keys **and** buttons ("a key without its button is dead") | VERIFIED complete. |
| Strg+S | `ToolbarButtons.jsx` (`ctrlKey || metaKey`), `CodeEditor` `Mod-s`, Save button | VERIFIED. |
| Daten player keys | `Daten/PlayerView.jsx` window keydown; `Transport.jsx` buttons for prev/next episode, ±1 frame, play, speed, mark; scrubber for Home/End/±5 | VERIFIED covered. |
| Disabled reasons as tooltips | 174 `title=` attributes; 62 files combine `title=` and `disabled=`; e.g. `StudentApp.js` `logoutTitle` (`LOGOUT_BLOCK_TITLES_DE`), Studio hint `hidden lg:block` with `title` | **Gap**: on touch, `title` never shows. CLAUDE.md's own rule for the Vormachen chooser (reason as visible text) should become general. |
| Hover-only affordances | `teacher/StudentRow.js` `group-hover:opacity-100` (teacher web), `ImageTopicSelectModal.js` hover highlight, CodeMirror `methodHover`, Blockly tooltips, flyout-card SVG `<title>` | Minor; Methoden column is the touch alternative (hidden below `md`). |
| Outside-click by `mousedown` on `document` | `TaskSelector.js`, `code/NewProgramDialog.jsx`, `teacher/StudentRow.js` | UNVERIFIED-DEVICE: iOS fires compatibility mouse events only for "clickable" targets. `usePopoverDismiss.js` already uses capture-phase `pointerdown` — the pattern to copy. |
| Drag handles | `Daten/daten.css` `.dat-scrub {touch-action:none}`, `SimScene.jsx` `touch-none`, `layout/ResizeHandle.jsx` `touch-none` — good; `teach/ReviewStrip.jsx` handles: `w-4` (16 px), pointer capture, **no `touch-action`** | **Gap** (VERIFIED by reading): likely `pointercancel` on pan; 16 px is far below Apple's 44 pt target. |
| Hit areas | Sammlung kind-icon buttons 24×24, flyout card controls 32×32 (CLAUDE.md) | JUDGMENT: enlarge to ≥ 44 pt on coarse pointers (`@media (pointer: coarse)`). |
| Viewport | `index.html` `width=device-width, initial-scale=1` (no `viewport-fit=cover`); 16 uses of `100vh`/`h-screen` (`StudentApp.js` `h-screen w-screen`), no `dvh`/`safe-area-inset` | UNVERIFIED-DEVICE: bottom action bars under Safari's toolbar. Fix: `100dvh`, safe-area padding. |
| Breakpoints | Tailwind `md` 768 / `lg` 1024; CSS 1150/1180/1200 containers; Studio hides the hint below `lg`, Methoden column and resize handles below `md`; Aufnahme switches to one column at ≤ 1150 px container (all VERIFIED by reading) | iPad widths in points (commonly published values, not re-fetched): mini 744, 9th gen 810, 10th/A16/Air 11" 820, Pro 11" 834, Pro 13" 1024 portrait; 1080–1366 landscape. Landscape gets the full desktop layout; portrait iPad mini falls below `md`; Split View halves the width (phone layout). |
| Blockly 12 | Pointer-event gesture system; `BlocklyWorkspace.jsx` enables wheel + pinch zoom; custom flyout inflaters bind pointer events with `conditionalBind(…, true)` | UNVERIFIED-DEVICE — rig gate **S-R9** (touch screen) is already written and OPEN in KNOWN-ISSUES. |
| CodeMirror 6 + on-screen keyboard | `@codemirror/view` 6.43.12 sets `spellcheck:"false"`, `autocorrect:"off"`, `autocapitalize:"off"`, `writingsuggestions:"false"` by default (`dist/index.js`) — VERIFIED; our Python Enter/Tab/Backspace are block-structure-aware (`codeInsert.newlineIndentAt`) | UNVERIFIED-DEVICE: iOS Smart Punctuation (curly quotes, "…", "—") inside contenteditable; selection handles (CM forum reports); the soft keyboard has no Tab and hides `:()[]"` behind a second page — typing Python/Java on glass is slow (JUDGMENT; the Methoden "Einfügen" path helps). |

**What must change for an iPad to be a first-class client (JUDGMENT, ordered by value/cost):**
visible disabled reasons everywhere (replace reason-only `title`s); `touch-action` + ≥ 44 pt hit
areas on all drag handles/icon buttons; `pointerdown` for outside-click; `dvh` + safe areas; an iPad
Safari smoke on the release checklist (via the Pi path, §5.2); a "shell mode" so the SPA can run in a
WKWebView served by an in-app loopback server (ATS: iPadOS 17+ blocks IP-address loads unless
excepted — `NSAllowsLocalNetworking`, VERIFIED Apple doc).

---

## 3. iPadOS capability facts (current, with sources)

### 3.1 Web (Safari = WKWebView engine)

| Capability | iPadOS fact | Tag / source |
|---|---|---|
| Web Serial / WebUSB / WebHID / Web Bluetooth / Web NFC | Not implemented; WebKit **oppose** (#199, #68, #510, #570, #584) | VERIFIED — `raw.githubusercontent.com/WebKit/standards-positions/main/summary.json` |
| Other engines on iPad | BrowserEngineKit entitlements exist for the EU and Japan (Apple's page, seen only as a search excerpt), but **no** non-WebKit browser has launched | VERIFIED (status) — Mozilla blog 2026-11-20 `blog.mozilla.org/en/mozilla/internet-policy/apple-ios-browser-engine-rules/` |
| WKWebView browsers that polyfill Web Bluetooth | Bluefy (id1492822055, iPadOS 12+), WebBLE (id1193531073, iPadOS 14.7+) on the App Store | VERIFIED — App Store listings |
| WebCodecs VideoEncoder/Decoder | Safari 16.4 (iOS mirrors); secure context; Dedicated Workers | VERIFIED — MDN BCD `api/VideoEncoder.json`; MDN page |
| MediaRecorder | Safari 14.1 / iOS 14 | VERIFIED — BCD |
| MediaStreamTrackProcessor / VideoFrame | Safari 18 / 16.4 | VERIFIED — BCD |
| getUserMedia | Safari 11, **secure context required** | VERIFIED — BCD `api/MediaDevices.json` |
| getUserMedia with **external USB cameras** | WebKit main requests only `AVCaptureDeviceTypeExternalUnknown` (`Source/WebCore/platform/mediastream/cocoa/AVVideoCaptureSource.mm::cameraCaptureDeviceTypes`; soft-link `PAL/pal/cocoa/AVFoundationSoftLink.h`), which Apple documents for **macOS 10.15–14.0 only**; iPadOS uses `AVCaptureDevice.DeviceType.external` (iOS/iPadOS 17.0+) | Source: VERIFIED. Behaviour: **UNVERIFIED-DEVICE** — test: USB-C iPad on iPadOS 27, UVC camera on a hub, page calls `getUserMedia({video:true})` then `enumerateDevices()`; expected per source: camera absent. |
| LAN from an HTTPS page | `ws://` / `http://` to a LAN IP is blockable mixed content (not upgradeable; WebSocket goes through Fetch) | VERIFIED (spec chain) — W3C Mixed Content §3.1–3.2 (ED 2023-02-23), WHATWG WebSockets §2 |
| Local Network permission for web content | "Traffic originating from WKWebView, SFSafariViewController, and Safari doesn't require local network access" | VERIFIED — Apple TN3179 |
| mDNS `.local` | Resolved by the OS for any app (Bonjour); for *native* browsing the app needs `NSBonjourServices`, multicast needs `com.apple.developer.networking.multicast` | VERIFIED — TN3179 |
| SharedArrayBuffer / WASM threads | Safari 15.2 with cross-origin isolation; `Atomics.wait` 15.2 | VERIFIED — BCD |
| WASM JSPI (`WebAssembly.Suspending`) | **Safari 27** (iPadOS 27, released 2026-09-14) | VERIFIED — BCD `webassembly/api/Suspending.json`; 9to5mac release date |
| JIT | Third-party apps get no JIT; WKWebView JS/WASM run JIT-compiled in WebKit's process (Lockdown Mode disables JIT) | VERIFIED (Apple forums/Daring Fireball, secondary) |
| Storage | 60 %/80 % of disk (browser apps), 15 %/20 % (other WebKit apps); LRU eviction; `persist()` Safari 15.2; 7-day ITP cap on script-writable storage in Safari | VERIFIED — webkit.org/blog/14403 (2023-08-10), webkit.org/blog/10218 (2020-03-24) |
| Background | Apps are "typically … in a suspended state when … in the background"; web pages have no background modes | VERIFIED — Apple "Configuring background execution modes" |
| Screen Wake Lock | Safari 16.4; iOS 18.4 also in Home-Screen web apps | VERIFIED — BCD |

### 3.2 Native

| Capability | Fact | Tag / source |
|---|---|---|
| CoreBluetooth central | Interval Min ≥ 15 ms, multiple of 15 ms; a 15 ms request may get 30 ms; peripheral latency ≤ 30; **supervision timeout 6–18 s**; throughput advice: write-without-response, large MTU, DLE, 2M PHY if both sides can, L2CAP for bulk | VERIFIED — Apple DTS reply, developer.apple.com/forums/thread/756572 (Jun 2024) |
| MTU | Negotiated by iOS, no app control; writes chunked to `maximumWriteValueLength` | VERIFIED (Apple forums, several threads; secondary) |
| Background BLE | `bluetooth-central` background mode exists; the app is otherwise suspended | VERIFIED — Apple background modes doc |
| Camera in background | "Camera usage is prohibited while in the background" | VERIFIED — `AVCaptureSession.InterruptionReason.videoDeviceNotAvailableInBackground` |
| External UVC cameras | "iPads with USB-C connectors support external cameras"; UVC; MJPEG/H.264 are delivered **decoded** (420f); can join an `AVCaptureMultiCamSession`; hot-plug creates new device objects | VERIFIED — WWDC23 session 10106 |
| Two cameras at once | Gated by `isMultiCamSupported` and `supportedMultiCamDeviceSets` per device | VERIFIED doc; **two external UVC on one hub: UNVERIFIED-DEVICE** |
| USB-C speed | iPad (A16): USB 2.0, up to 480 Mb/s | VERIFIED — apple.com/ipad-11/specs |
| USB serial | DriverKit / USBDriverKit on iPadOS **only for M-series iPads**; drivers ship inside apps; entitlements requested from Apple with "your company's hardware vendor ID" | VERIFIED — Apple DriverKit, USBDriverKit, "Requesting Entitlements for DriverKit Development" |
| ExternalAccessory | MFi accessories only (Lightning/30-pin/Bluetooth Classic) | VERIFIED — Apple ExternalAccessory doc |
| Network.framework / Bonjour | Needs `NSLocalNetworkUsageDescription`, `NSBonjourServices`; ATS blocks IP-address loads by default since iPadOS 17 | VERIFIED — TN3179, `NSAllowsLocalNetworking` doc |
| Embedded CPython | Embedded mode only; all binary modules must be dynamic frameworks in `Frameworks/`; **no subprocess / multiprocessing / IPC** (attempt "will either lock up, or crash"); threads work | VERIFIED — docs.python.org 3.15 "Using Python on iOS", library intro "Mobile platforms" |
| iOS wheels | PyPI latest: numpy 2.5.5, opencv-python 5.1.0.95, pyarrow 26.0.1, av 19.0.2, torch 2.15.0, pupil-apriltags 1.0.4.post11, cryptography 50.0.2: **0 iOS wheels**; aiohttp 3.14.5: iOS wheels. BeeWare channel: numpy up to 2.5.2.post1 (cp315 iOS), pillow; **no** opencv-python, pyarrow, av | VERIFIED — pypi.org/pypi/<pkg>/json; api.anaconda.org/package/beeware/<pkg> |
| Pyodide | No threads or multiprocessing; sockets non-functional; network via browser (CORS). Packages: numpy 2.4.6, opencv-python 4.11.0.86, pyarrow 22.0.0, pandas, scipy, pillow, aiohttp, cryptography; **no** PyAV, torch, apriltag | VERIFIED — pyodide.org/en/stable (docs labelled 314.0.8) |
| Java | No JVM on iOS; CheerpJ (WASM JVM) lists Safari mobile, Java 11, Java 17 preview (4.1); JavaFiddle runs `javac` in the browser; free for personal/education, commercial licence for products | VERIFIED — labs.leaningtech.com (cheerpj, javafiddle, licensing) |
| App Review | 2.5.2: no downloading/executing code that changes features, **except** educational apps that "teach, develop, or allow students to test executable code" with fully viewable/editable source; 4.7.2: "may not extend or expose native platform APIs … to the software without prior permission" (applies to offered mini-apps/plug-ins) | VERIFIED — developer.apple.com/app-store/review/guidelines/ |
| Distribution | Custom Apps: Private distribution to named organisations in Apple School Manager, then MDM or redemption codes (after App Review); TestFlight: builds 90 days, ≤ 10 000 external testers (may need review) | VERIFIED — developer.apple.com/custom-apps/, App Store Connect help |
| Developer account | Organisation: D-U-N-S, legal entity, binding authority, public website; seller = organisation. Individual: seller = **personal legal name**. 99 USD/year | VERIFIED — developer.apple.com/programs/enroll/ |

### 3.3 iPads in German schools

The Lightning iPad 9 stayed on sale to education after its general discontinuation (heise) and is
still supported by **iPadOS 27** (iPad 9th gen, 10th gen, A16; 8th gen dropped) — VERIFIED (heise;
MacRumors 2026-06-08). iPad 10 (A14, 2022) moved the entry model to USB-C — VERIFIED (heise).
Implication (JUDGMENT): a typical school fleet is mixed; any iPad tier that attaches cameras
excludes the Lightning iPads; a Safari-only client tier includes all of them. No reliable share
figures were found (search returned none).

---

## 4. Function-by-function: where can each capability physically run with an iPad?

Legend for columns: **B** = iPad Safari as the screen of a station that has an engine (PC or Pi);
**N** = iPad-native app, no PC at the station.

| Function | Needs | B (iPad as screen) | N (iPad alone) | Hard blockers for N |
|---|---|---|---|---|
| Account, Training start/monitor, Meine Modelle | cloud only | Yes | Yes (even Safari, once a public "cloud host" student build exists; `utils/cloudMode.js::isCloudOnlyMode` keeps HOME+TRAINING, but a non-loopback host currently forces Pi mode in `utils/piMode.js`) | none (a build-mode flag) |
| Startseite (activation, twin, HF token card) | engine topics/services, WebGL, noble | Yes (Pi today) | Yes with an engine | engine placement |
| Aufnahme (teleop recording) | leader+follower states (board), 2 cameras, slot sampler, LeRobot writer, encoder, HF upload | Yes (engine does it) | Only on USB-C iPads with a powered hub + 2 UVC cameras (native AVFoundation, frames arrive decoded), H.264 via VideoToolbox, **packaging in the cloud** (LeRobot needs torch) | cameras (UNVERIFIED-DEVICE), LeRobot |
| Daten 2.0 (lossless edit, player, hub sync) | local datasets, PyAV stream copy, sidecar HTTP | Yes | Not locally (no PyAV on iOS); cloud-side Daten would be a new service | PyAV |
| Roboter Studio — simulator | `workflow/` engine (threads), sim world | Yes | Needs the Python engine on iPad (embedded CPython; numpy from BeeWare suffices for the sim path) or in the cloud | engine packaging |
| Roboter Studio — real arm | engine + **tower camera** (mat check at every real start) + board | Yes | USB-C iPad + tower camera (1920×1080 MJPEG arrives decoded → a new split derivation and `profile_id`); OpenCV (`undistortPoints`, `cornerSubPix`, `projectPoints`, `Rodrigues`, `fisheye`, `imdecode`, `cvtColor`) + apriltag needed on iOS | camera; OpenCV/apriltag builds |
| Vormachen | board torque/hand-guide/record, engine | Yes (no clipboard on HTTP — click-first hint) | Yes with an engine; BLE latency adequate (JUDGMENT) | engine |
| Calibration (arm check, handheld lens) | tower camera, LM solve (numpy), perception | Yes | as real-arm Studio | camera |
| Python / Java programs | sandbox runner + `code_rpc` budgets | Yes (runner beside the engine) | Python: Pyodide in an isolated Worker (killable; no threads; debugger needs a redesign; blocking RPC via JSPI on iPadOS 27 or SAB+Atomics); Java: CheerpJ (licence, Java 17 preview) or remote | sandbox spike |
| Cloud-Inferenz | cameras + board + client (`cloud_client.py` aiohttp, `cloud_schedule.py`, `chunk_engine.py` numpy) | Yes | Client code could run on embedded CPython (aiohttp has iOS wheels; numpy via BeeWare); cameras again | cameras |
| System/setup | Pi agent / GUI | Pi: Yes | native settings | — |

**Where do the cameras connect without a PC?** (VERIFIED facts, JUDGMENT conclusion) Only three
places exist: (1) the iPad's USB-C port through a powered hub — USB-C iPads only, native app only,
two cameras on one USB 2.0 link unproven; (2) Wi-Fi cameras — at 640×480@30 MJPEG ≈ 1–1.5 MB/s per
camera, 15–30 stations × 2 cameras would need 30–90 MB/s of 2.4 GHz airtime in one room, far above
what one 2.4 GHz channel carries (JUDGMENT from arithmetic), and the tower camera's 1080p mode is out
of reach of ESP32-class cameras; (3) the iPad's own camera — it is the student's screen and not the
tower camera the fixture contract was measured for. **Conclusion:** an iPad that replaces the PC must
host the cameras on USB-C.

**Where does student code run on an iPad?** Today's Rule-§2 condition 4 ("different uids, supervisor
not PID 1") is a process boundary; iOS has none for us. The nearest equivalent is a Worker in a
**separate origin** (sandboxed iframe), CSP `connect-src 'none'`, talking only `postMessage` to the
engine, stopped by `worker.terminate()` — strong out-of-band stop, no network, no access to the app's
tokens (JUDGMENT; a spike must prove the four conditions). Server-side budgets and validation stay in
`code_rpc.py` wherever the engine runs.

---

## 5. Architecture alternatives (Windows + iPad, later ChromeOS/macOS/Linux)

Real sizes (VERIFIED by `wc`): server package ≈ 61 k lines (`physical_ai_server.py` 8 542, `workflow/`
29 468 in 45 files, `data_processing/` 10 511, `daten/` 3 829, `inference/` 2 121, `safety/` 1 703,
`communication/` 2 198); its tests: 177 files / 70 894 lines (workflow-area 83 files / 34 157;
recorder/data 26 / 10 727; cloud inference 11 / 3 993). Code runner: 3 291 Python + 1 913 Java.
V1 driver set (`feetech_arm_node.py` 2 643, `feetech_geometry.py` 901, `feetech_bus.py` 476) = the
firmware spec. Windows GUI 15 038 lines. Pi agent 9 748 + 11 613 test lines (the LAN-client
infrastructure: robot login, tickets, firewall). Cloud API 20 367. SPA 83 251 + 83 959 test lines.

| Option | What it is | iPad coverage | Reliability | Cleanliness | Function loss | Effort (JUDGMENT) |
|---|---|---|---|---|---|---|
| **A** Brief: Python engine on the PC, firmware on the arm | as brief | Tier 0 only (cloud pages) | High (local, offline recording; firmware safety) | High (one engine, Python stays authoritative) | none on PC | brief's ~5 months |
| **B** PC/Pi engine + iPad as LAN client | Pi-mode pattern | **All iPads** incl. Lightning (Safari) | Medium: depends on school LAN (VLAN, client isolation off, no TLS inspection — `docs/ORANGE_PI_IT_NETZWERK.md` makes these a *written prerequisite*); plain HTTP exposes the robot cookie and, during login, the access token (same doc) | High on the Pi (exists); on Windows: **inbound blocked for non-admin** (MS Learn) → an IT firewall rule per school | none (engine does everything) | Pi: test only. Windows: port robot login + LAN binding + IT rule, ~3–5 weeks; or WebRTC P2P (outbound-only, encrypted) as a research option |
| **C** One TS/WASM engine in the web app + thin native bridges | browser engine | iPad only inside a native shell (no device APIs in Safari) | Medium-low: LeRobot writer re-implemented in JS (Rule §5 drift), Daten lossless engine (PyAV stream copy) re-implemented, Pyodide has no threads for `workflow/` | Low: two engines or a ~54 k-line Python port plus ~49 k lines of test semantics | high risk of silent dataset drift | very high (≥ 12 months) |
| **D** Shared native core (Rust/Swift/KMP) | rewrite | full in principle | High eventually | Medium: loses the Python ecosystem (LeRobot, PyAV, OpenCV-Python) | none if completed | highest |
| **E** Engine in the cloud (session service) + device runtimes (iPad app, Chrome tab, PC) | the dropped plan, re-based on firmware | USB-C iPads with cameras via a native runtime; Chromebooks via Chrome | Medium: online-only; latency 50–150 ms for program steps (firmware handles real time; Stop must stay local) | High for Python reuse (one engine for every thin client) | offline recording lost; continuous classroom video to the cloud (GDPR/DPA) | high: multi-tenant service, session security, cost model |
| **F** Tiered hybrid: full engine on PC + iPad app for a subset over the same board protocol | brief + iPad tier | USB-C iPads (cameras) or all iPads for camera-less functions | Medium (new platform with BLE, AVFoundation, packaging) | Medium: two engine hosts; Python kept if embedded CPython | subset on iPad | +4–6 months after A, high uncertainty |
| **G** More logic in firmware | trajectory buffer, limits, watchdog, teleop, collision stop (+ optionally closed-form V1 IK and the box-model floor check as a refuse-only floor) | helps every client be thin; does **not** solve cameras/datasets | High for safety | High if it stops at motion primitives | none | inside the brief's Phases 2–3; the interpreter/perception do **not** belong on an ESP32 (no camera, 520 KB SRAM class MCU, perception + contract tables, German texts) |

**Where F's iPad engine should run (JUDGMENT):** embedded CPython is the cleanest if the packaging
gate passes — `workflow/` keeps its threads, numpy exists (BeeWare), the OpenCV surface used is small
(seven functions, see §4) and apriltag is a C library; the Daten/HF/edit helpers become threads (GIL
contention returns — the reason the sidecar is a process today). A Pyodide port of `workflow/` is
**not** sane (no threads). A Swift/TS port is not sane. The cloud engine (E) is the fallback if the
owner accepts online-only. What stays one source of truth in every case: `workflow/robot_api.py`
(already exports `robot_api.json` and `robot_api_docs.json`), the `.srv/.msg` IDL, the fixture
contract (`scene_fixture.py` → `sceneFixture.json`), and the German text modules with their lockstep
tests.

### 5.1 Ranking and what I would build (JUDGMENT)

1. **A, plus the "door-open" decisions of §6** — build now. It is the only option that keeps every
   function, Rule §5's LeRobot posture and offline recording, with the least new risk.
2. **Tier 0 (cloud pages on any iPad)** — add a "cloud host" build mode (no Pi detection, cloud-only
   tabs) and host it publicly (the teacher web's Railway pattern). Cheap; serves "Training/Meine
   Modelle on the iPad" now.
3. **Tier 1 = B on the Pi line (existing)** — run the iPad device-test checklist (§5.2) now; this is
   the only "tested with iPads" candidate that exists today. It needs the Pi IT prerequisites.
4. **F (iPad-native tier)** — only after the seven gates of §9.1; engine = embedded CPython if gate
   G4 passes, else E.
5. **E** — the right answer only if the owner wants iPad-/Chromebook-only schools and accepts
   online-only operation and a new cloud service.
6. **B on Windows** — only for schools whose IT will add one inbound rule; otherwise not reachable
   without admin.
7. **G** — adopt the motion-primitive part (already in the brief); not a client strategy.
8. **C, D** — not recommended.

### 5.2 iPad test checklist for Tier 1 (UNVERIFIED-DEVICE, all of it)

On an iPad 9 (Lightning) and an iPad 10 or A16 (USB-C), iPadOS 27 Safari, against an Orange-Pi
station: login + robot login (ticket); Startseite twin and activation; Aufnahme with both MJPEG tiles
for 10 minutes, Start/Beenden by touch, the collision modal; Roboter Studio: Blockly drag from the
flyout, pinch zoom, long-press context menu, Sammlung cards' play/more, ReviewStrip handles in
Vormachen; code editor: type `print("a")` and `x = "…"` (smart quotes?), Enter/Backspace indentation,
"Einfügen"; Daten player playback and scrubbing; Cloud-Inferenz start/stop; lock the iPad during a
run and during a recording (what continues, what stops); Split View at half width; landscape and
portrait; record every defect with the screen width.

---

## 6. Decisions to take now that keep the iPad path open at low cost (JUDGMENT, each cheap)

1. **One board protocol, three transports.** The brief's framed protocol (COBS + CRC32, versioned
   `HELLO`) carried unchanged over USB-CDC, a **BLE GATT** byte pipe (one write-without-response RX
   characteristic, one notify TX characteristic, chunked to the negotiated MTU−3), and Wi-Fi TCP.
   Use **GATT, not L2CAP CoC**, as the baseline: Chrome's Web Bluetooth speaks GATT only, iOS speaks
   both. Same test vectors for Python, C, Swift/TS.
2. **Safety never depends on the link.** Firmware watchdog on *application* frames per transport
   (iOS supervision timeout 6–18 s is useless for safety); one controlling client per arm (claim
   token); Stop is a local action on every client; a physical stop on the arm is worth considering
   once wireless control exists.
3. **One engine contract, authenticated from day one.** Whether D8 picks rosbridge emulation or a
   typed API, serve it with a per-session token (the Pi's robot login shows the pattern) even on
   loopback, define handlers from the `.srv/.msg` IDL, and bind loopback by default. The same
   contract can later be served on the LAN (iPad as screen), inside an iPad app's loopback server,
   or by a cloud engine. Today's unauthenticated rosbridge surface is a documented hole
   (KNOWN-ISSUES, roster entry) — do not re-create it.
4. **A raw-episode seam.** Make the recorder's *inputs* (frames with `capture_ns`, board `STATE` with
   board µs) a neutral, replayable container, and run the slot sampler + LeRobot writer as a
   *packager* (in-process on the PC; a cloud container for thin clients). `capture_timeline.py` is
   pure stdlib and already decides slots from histories; this keeps Rule §5 for any future client.
5. **A transport-neutral sandbox contract.** Keep `code_rpc` frames independent of AF_UNIX; let the
   runner be an AppContainer process on Windows (D12) and a WASM Worker on iPad/Chromebook later.
6. **Camera abstraction by contract, not by code.** Every source delivers `(cam_id, capture_ns, JPEG
   or pixel buffer)`; the tower split's `profile_id` stays a per-platform derivation (iPad would get
   its own, as Windows and Pi do).
7. **Touch-readiness rule now** (§2.3 list) and a coarse-pointer CSS layer.
8. **Arm identity for classrooms.** Advertise the provisioning serial (sticker `V1-0007`) in the BLE
   name; print a QR code on the arm with that identity (iPads have cameras; Chrome's `requestDevice`
   can filter on the exact name).
9. **Board choice check (for agents 1–3).** If wireless is first-class, compare the HAT (A)'s classic
   ESP32 (BLE 4.2, no 2M PHY, no PSRAM class) with an ESP32-S3 class MCU (BLE 5 with 2M PHY:
   1.35 Mbit/s board-to-board per the Espressif FAQ; native USB CDC per ESP-IDF USB docs). VERIFIED
   facts, decision is the owner's.

---

## 7. Tablet-side Bluetooth and Wi-Fi

### 7.1 BLE in a classroom with 15–30 identical arms (JUDGMENT unless tagged)

- **Pairing UX.** No OS pairing/bonding (it drags students into iPadOS Settings); app-level claim
  instead. Identify by sticker serial in the advertised name + QR scan; offer "move the arm to
  identify" as a second check (the board streams leader/follower q once connected).
- **Reconnection.** CoreBluetooth can reconnect to a known peripheral identifier on the same iPad
  (per-device identifiers). Chrome Web Bluetooth needs a new chooser gesture each session
  (`getDevices` behind a flag — VERIFIED BCD).
- **Lock/background.** On lock the app is suspended; camera capture stops (VERIFIED); heartbeats stop
  → the board must stop within `T_link` (brief: 300 ms moving). Recording ends; the take is
  discarded by the engine as today's source-stop rule would.
- **Throughput budget.** STATE at 100 Hz for 12 servos × (q, dq, load) as int16 ≈ 72 B + header ≈
  9 KB/s; a 50-point TRAJ chunk ≈ 1.4 KB. Well inside BLE 4.2 with DLE (~90 KB/s peak board-to-
  board, VERIFIED Espressif). Latency 15–60 ms per hop at iOS intervals (VERIFIED parameters).
  Cameras never go over BLE.
- **Coexistence.** One radio for Wi-Fi and BLE (VERIFIED ESP-IDF): keep Wi-Fi off unless used;
  pin BT and Wi-Fi tasks per core as Espressif advises — which collides with the brief's "one core
  for the bus, one for USB" plan (flag for the firmware architect).
- **Firmware update.** USB from the PC app (fast); BLE OTA from an iPad (≈ 1.5 MB at 20–60 KB/s ≈
  0.5–1.5 min, JUDGMENT from the throughput figures); Wi-Fi OTA needs credentials and internet.
  Signed images and a rollback partition in all cases.

### 7.2 Wi-Fi

| Mode | Effect for an iPad | Verdict (JUDGMENT) |
|---|---|---|
| SoftAP (iPad joins the arm) | The iPad loses internet → no Supabase login refresh, no cloud API, no training, no Cloud-Inferenz; iPadOS may hop back to a known Wi-Fi; on the ESP32 SoftAP + BLE is "C1 supported but unstable" (VERIFIED ESP-IDF) | Provisioning only |
| STA (arm joins school Wi-Fi) | Credentials (PSK or WPA2-Enterprise: ESP-IDF's `wifi_enterprise` example offers TLS/TTLS/PEAP and lists ESP32 as a target — seen as a search excerpt of the README, plus Espressif advisory AR2024-003 on PEAP phase 2); client isolation must be off between iPads and arms, mDNS crosses no VLAN, headless devices need NAC exceptions — exactly the Pi IT ticket (`docs/ORANGE_PI_IT_NETZWERK.md`) | Optional, IT-dependent |
| Both | SoftAP+STA+BLE on one radio | Avoid |
| HTTPS page → arm | Mixed content and (Chrome) LNA prompts; Safari has no device APIs | Never the design |

**Dependable classroom default for a tablet: BLE** (no IT ticket, no loss of internet, per-arm
identity). Wi-Fi: optional (IT-provisioned STA) for OTA, telemetry or a PC without a cable.

---

## 8. Quick facts: Chromebooks, Android tablets, macOS, Windows WebView2

- **ChromeOS:** Web Serial (Chrome 89+), WebUSB (61+), Web Bluetooth (BCD: Chrome desktop 70+,
  "Linux support is not enabled by default"), WebHID (89+) — VERIFIED BCD (ChromeOS is not tracked
  separately there; the policies below list `chrome_os` explicitly). Managed auto-grant for serial/USB, none for Bluetooth — VERIFIED (Chromium policy YAML).
  ChromeOS ships `uvcvideo`; external webcams must work in Meet on the web — VERIFIED (ChromeOS
  webcam spec v1.3). A Chromebook can be a browser device runtime; the engine-placement question
  (Python) is the same as for iPads (C or E). Crostini on managed school Chromebooks: UNVERIFIED.
- **Android tablets:** Chrome Web Bluetooth (56+), WebUSB yes; Web Serial over Bluetooth RFCOMM from
  138, over USB from **148 (May 2026) only where the OS exposes wired serial ports ("Android 17")** —
  VERIFIED (Chrome 148 notes; blink-dev Intent to Ship, 2026-03-24/25). Android WebView exposes none
  of these (BCD). Native Android has USB host + BLE, and subprocesses are "possible but officially
  unsupported" (Python docs) — a more capable native host than iPad (JUDGMENT).
- **macOS:** Safari has no device APIs (same WebKit positions); Chrome/Edge do. The Python engine runs
  natively (POSIX, subprocess, pyserial, AVFoundation via OpenCV; BLE via bleak 3.1.0, which lists
  macOS, Windows 11, Linux, Android — VERIFIED PyPI) → macOS is the cheapest second engine platform.
- **Windows WebView2:** whether it exposes Web Serial/Bluetooth is UNVERIFIED (one third-party report
  of `requestPort()` succeeding and `open()` failing). Irrelevant if the native engine owns the port;
  the engine can also use BLE on Windows through WinRT without admin (bleak).

---

## 9. Risks, ranked (for the iPad dimension)

| # | Risk | Severity | Evidence | Mitigation |
|---|---|---|---|---|
| 1 | Cameras on iPad: USB-C only; Safari very probably can't see them (WebKit source); two externals on one USB 2.0 port unproven | High | §3 | Gate G1/G2; Tier B needs none |
| 2 | LeRobot impossible on iPad → cloud packager + online dependency | High (tier N) | §0.4 | raw-episode seam (§6.4) |
| 3 | Python engine on iOS: OpenCV/apriltag builds, no subprocess, GIL contention | High (tier N) | §3.2 | Gate G4; else cloud engine |
| 4 | Sandbox on iPad (no processes, no threads in Pyodide, CheerpJ licence/perf) | Med-High | §3.2 | Gate G6 |
| 5 | Apple distribution: legal entity (licensor form unsettled per CLAUDE.md placeholders), review under 2.5.2/4.7.2, UI updates only via app releases | Medium | §3.2 | settle entity first; bundle the SPA |
| 6 | BLE reliability with 30 arms, coexistence, iOS parameters | Medium | §7 | firmware watchdog; gate G3 |
| 7 | LAN-client security (plain HTTP) and school-network prerequisites for B | Medium | IT sheet | VLAN rule; or encrypted transport (WebRTC research) |
| 8 | Touch UX gaps hide reasons and break small drag targets | Medium-Low | §2.3 | UI rule now |
| 9 | Two engine hosts (PC + iPad) double maintenance | Medium | §5 | one contract, one Python codebase |
| 10 | Lightning iPads excluded from any camera tier | Low-Med (business) | §3.3 | Tier B covers them |

### 9.1 Gates before committing to an iPad-native tier (exact tests)

- **G1** Two UVC cameras on iPad 10 and iPad A16 (iPadOS 27) through a powered USB-C hub: tower
  camera 1920×1080 MJPEG @30 + gripper 640×480 @30, native AVFoundation; log `isMultiCamSupported`,
  `supportedMultiCamDeviceSets`, per-frame PTS deltas, drops, thermal state over 30 min.
- **G2** Safari/WKWebView `getUserMedia` with a UVC camera on the same iPads (expected absent).
- **G3** ESP32 (HAT A) BLE GATT with an iPad at 15/30 ms: 100 Hz int16 STATE, TRAJ chunks, Wi-Fi STA
  on and off; 20 boards advertising in one room; reconnect after lock; measured stop latency of the
  board watchdog after the iPad locks.
- **G4** Embedded CPython (3.13/3.14) app running the `workflow/` simulator test subset on an iPad;
  an OpenCV + apriltag path (custom framework build or native shim); app size and launch time.
- **G5** Cloud packager: a raw episode replayed through `capture_timeline.SlotSampler` + the LeRobot
  0.5.1 writer in a container; compare with the local recorder on `record_fsm_smoke.py` scenarios;
  upload size per minute with on-device H.264.
- **G6** Sandbox: Pyodide in a separate-origin Worker (`connect-src 'none'`), blocking robot RPC via
  JSPI (iPadOS 27) or SAB/Atomics, stop by `terminate()`, `code_rpc` budgets unchanged; CheerpJ
  compiles and runs the Java starter on an iPad; measure startup and per-call latency.
- **G7** Apple Developer enrolment as an organisation and one Custom App delivered to a test school
  via Apple School Manager + MDM.

---

## 10. Decision questions for the owner

| # | Question | Options | Recommendation (JUDGMENT) | Evidence |
|---|---|---|---|---|
| Q1 | What does "use with iPads" mean? | (a) iPad is the only device at a station; (b) iPad is the student's screen while a PC/Pi drives the arm; (c) iPad for cloud work (training, models) | (c) now, (b) tested now via the Pi line, (a) only after G1–G7 | §3, §4 |
| Q2 | Which iPads are in scope? | all (incl. Lightning iPad 9) vs USB-C only | (b)/(c) tiers for all; (a) USB-C only | WWDC23 10106; iPadOS 27 list |
| Q3 | May an iPad-native tier require internet for recording (cloud packaging) and possibly for the engine? | yes / no | yes, or drop tier (a) | LeRobot needs torch (§0.4) |
| Q4 | Apple developer identity | organisation (D-U-N-S, legal entity) vs personal name as seller | settle the legal entity first (it also fills the LICENSE placeholders) | Apple enrolment page; CLAUDE.md licensing |
| Q5 | Role of the board's radios | BLE always on for tablets; Wi-Fi STA optional; SoftAP only for provisioning | yes; BLE motion only after an explicit claim; Wi-Fi off by default | ESP-IDF coexistence; Apple DTS |
| Q6 | MCU class if wireless is first-class | classic ESP32 (HAT A) vs ESP32-S3 class | decide after the bench gate on RAM/CPU headroom with BLE + loop | Espressif FAQ |
| Q7 | D8 engine API | rosbridge emulation vs typed API | either, but authenticated, IDL-defined, loopback by default | §2.1, §6.3 |
| Q8 | D12 sandbox | AppContainer now vs one WASM sandbox everywhere | AppContainer on Windows now; keep the contract transport-neutral; WASM for iPad later | §6.5 |
| Q9 | iPad as screen of a **Windows** station | IT adds one inbound rule / WebRTC research / not offered | not offered initially; the Pi line covers (b) | MS Learn firewall rules |
| Q10 | Touch-readiness as a standing UI rule | now / later | now (cheap, also helps Windows touch screens — rig gate S-R9) | §2.3 |

---

## 11. Sources (all accessed 2026-11-24)

- WebKit standards positions: https://raw.githubusercontent.com/WebKit/standards-positions/main/summary.json
- Mozilla, "Why there are still no non-WebKit browsers on iOS" (2026-11-20): https://blog.mozilla.org/en/mozilla/internet-policy/apple-ios-browser-engine-rules/
- Bluefy: https://apps.apple.com/app/id1492822055 · WebBLE: https://apps.apple.com/app/webble/id1193531073
- MDN browser-compat-data (main): https://github.com/mdn/browser-compat-data (api/VideoEncoder, VideoDecoder, MediaRecorder, MediaDevices, Serial, USB, Bluetooth, HID, WakeLock, Clipboard, StorageManager, HTMLVideoElement, OffscreenCanvas, MediaStreamTrackProcessor, VideoFrame, DataTransfer; javascript/builtins/SharedArrayBuffer, Atomics; webassembly/api/Suspending)
- Chrome 148 release notes: https://developer.chrome.com/release-notes/148 · Intent to Ship Web Serial on Android: https://groups.google.com/a/chromium.org/g/blink-dev/c/HBJ-uYFvkpM
- Chromium policy definitions: https://github.com/chromium/chromium/tree/main/components/policy/resources/templates/policy_definitions/ContentSettings
- WebKit source: https://github.com/WebKit/WebKit/blob/main/Source/WebCore/platform/mediastream/cocoa/AVVideoCaptureSource.mm , …/AVCaptureDeviceManager.mm , https://github.com/WebKit/WebKit/blob/main/Source/WebCore/PAL/pal/cocoa/AVFoundationSoftLink.h
- Apple: AVCaptureDevice.DeviceType `external` / `externalUnknown`; WWDC23 10106 https://developer.apple.com/videos/play/wwdc2023/10106/ ; AVCaptureMultiCamSession; InterruptionReason.videoDeviceNotAvailableInBackground; DriverKit https://developer.apple.com/documentation/driverkit ; USBDriverKit; DriverKit entitlements; ExternalAccessory; TN3179 https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy ; NSAllowsLocalNetworking; Configuring background execution modes; App Review Guidelines https://developer.apple.com/app-store/review/guidelines/ ; Custom Apps https://developer.apple.com/custom-apps/ ; Enrolment https://developer.apple.com/programs/enroll/ ; TestFlight overview; DTS BLE parameters https://developer.apple.com/forums/thread/756572 ; iPad (A16) specs https://www.apple.com/ipad-11/specs/
- MacRumors, iPadOS 27 compatibility (2026-06-08): https://www.macrumors.com/2026/06/08/ipados-27-drops-support-for-a-wave-of-ipads/ · 9to5Mac iPadOS 27 date: https://9to5mac.com/2026/09/09/ipados-27-will-be-launched-on-september-14/ · heise iPad 9: https://www.heise.de/en/news/iPad-9-eingestellt-Das-war-s-mit-Lightning-9712093.html · heise iPad 10 USB-C: https://www.heise.de/news/Neues-Low-Cost-iPad-mit-USB-C-7141259.html
- Python on iOS: https://docs.python.org/3/using/ios.html · https://docs.python.org/3/library/intro.html
- PyPI JSON API: https://pypi.org/pypi/{numpy,opencv-python,opencv-contrib-python-headless,pyarrow,av,torch,pupil-apriltags,aiohttp,cryptography,huggingface-hub,lerobot,bleak}/json · lerobot 0.5.1: https://pypi.org/pypi/lerobot/0.5.1/json
- BeeWare channel: https://api.anaconda.org/package/beeware/numpy · BeeWare 2026Q1 roadmap: https://beeware.org/news/buzz/2026q1-roadmap
- Pyodide: https://pyodide.org/en/stable/usage/wasm-constraints.html · https://pyodide.org/en/stable/usage/packages-in-pyodide.html
- CheerpJ: https://labs.leaningtech.com/cheerpj · JavaFiddle: https://labs.leaningtech.com/showcase/javafiddle · licensing: https://labs.leaningtech.com/cheerpj3/licensing
- WebKit blog: https://webkit.org/blog/14403/updates-to-storage-policy/ · https://webkit.org/blog/10218/full-third-party-cookie-blocking-and-more/
- Mixed content: https://w3c.github.io/webappsec-mixed-content/ · WebSockets: https://websockets.spec.whatwg.org/
- Espressif: BLE FAQ https://docs.espressif.com/projects/esp-faq/en/latest/software-framework/bt/ble.html · RF coexistence https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/coexist.html · Wi-Fi Enterprise example https://github.com/espressif/esp-idf/blob/v5.2.1/examples/wifi/wifi_enterprise/README.md
- ChromeOS webcam spec: https://developers.google.com/chromeos/peripherals/cc-webcams-v1
- Microsoft Learn, Windows Firewall rules (updated 2026-10-08): https://learn.microsoft.com/windows/security/operating-system-security/network-security/windows-firewall/rules
- huggingface.js hub: https://huggingface.co/docs/huggingface.js/hub/README · LeRobot.js: https://huggingface.co/blog/NERDDISCO/lerobotjs
- CodeMirror forum (iOS): https://discuss.codemirror.net/t/ios-turn-off-autocorrect/2912 · https://discuss.codemirror.net/t/ios-safari-missing-selection-drag-handles/9679

Commands I ran (all read-only on the repo): `find`/`wc`/`grep` over `physical_ai_tools/` and
`robotis_ai_setup/`; an AST read of `cloud_training_api/app/main.py::_ANY_ORIGIN_BEARER_ROUTES`
(40 entries); `git check-ignore` for this file; downloads of public JSON/source files (BCD, WebKit,
Chromium policies, PyPI metadata, the lerobot 0.5.1 wheel read as a zip, never installed or
executed) into the session scratchpad `agent4/`.
