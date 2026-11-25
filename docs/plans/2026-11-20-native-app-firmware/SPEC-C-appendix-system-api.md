# SPEC C — Appendix 4: processes, trust zones, the station's HTTP surface, the `/api/system` contract and every named React change

Belongs to `SPEC-C-native-app.md`. Used by C4-06, C4-07, C4-08a, C4-08b, C4-12, C5-02, C7-02, C7-03, C7-04.
Everything here is DESIGN (mine) unless tagged CONFIRMED (read in the code on 2026-11-25).

---

**Revision 2 (2026-11-25).** The data root moved out of the WSL product's folder (B6); the import
routes are gone and the upload-first notice's key and route are in (B4, owner Q-C3); maintenance on a
lan session is decided (Q-C7); the firmware route and the `embedded` flag follow spec B's pilot
artefact (SPEC-B §8.1).

## B1. Process tree (what "the engine" of PLAN §1 is made of)

```
EduBotics.exe (launcher) → python -m edubotics_host                      HOST: window, supervisor of the station,
 │                                                                              updater, close guard. Nothing else.
 └─ python -m edubotics_station                                          STATION (always up while the app runs):
     │   HTTP front on 127.0.0.1:<stable per-user port> [+ LAN listener, IT policy only]
     │   static React build · session checks · /api/system · proxies to the robot tier and the sidecar
     ├─ python -m edubotics_robot      ROBOT TIER (only between „Umgebung starten" and „Stoppen"):
     │    │   today's node on the compatibility runtime · rosbridge subset · MJPEG source ·
     │    │   arm bridge (client of the arm link) · capture ingest · code RPC server
     │    ├─ python -m physical_ai_server.daten.http_server   (sidecar, as today)
     │    └─ HF worker (spawn) · download worker · edit worker  (as today)
     ├─ <spec B's arm-link process>    started before, stopped after the robot tier
     ├─ python -m edubotics_capture    started before, stopped after the robot tier (VA-07's order)
     └─ python -m edubotics_sandbox.broker   (Phase 6; student runs are its children)
```

Why the engine is two processes (station + robot tier), decided from the code:

1. **Respawn without a page reload.** Today a node crash is healed by the launch's `respawn` (2 s) while
   nginx keeps serving the page; React shows „Getrennt" and reconnects (`rosConnectionManager`). A single
   engine process would take the page's own origin server down with every node exit.
2. **A fresh process per „Umgebung starten".** Many invariants are "per boot of the node": the
   activation gate is back at `idle`, the HF token slot is empty, `mat_check_last` is "the last `ok`
   result of this boot", the ephemeral token key is new, `SimArm` is rebuilt. Today a compose `up`
   gives a fresh container. Starting the robot tier as a child per start keeps every one of them with
   no code change.
3. **The System page must work with the robot tier down** (it is the setup wizard), exactly like the
   Pi, where the agent and the manager outlive the robot tier.

The station is what the Pi agent plus nginx are today; the robot tier is what the two robot containers
are. Nothing else changes role.

## B2. Trust zones and sessions

| Zone | Listener | Who | Proof of identity | What it may do |
|---|---|---|---|---|
| **local** | `127.0.0.1:<P>`; `P` = `stable_port(user SID)`: first free port of `PORT_BASE + (sha256(SID)[:2] mod PORT_SPAN)`, probed upwards, then stored in `settings.json` and reused (the SPA's localStorage and IndexedDB live under the origin, so `P` must not change between launches; research/03 §8.3) | the app's own WebView2 window | cookie `edubotics_host_session` (256 random bits, `HttpOnly; SameSite=Strict; Path=/`), set by `GET /__session?k=<one-time key>`; the host hands `k` to the station on its stdin BEFORE the station reports ready; `k` is single-use (a second use is 403) | everything; it is the Pi agent's "loopback peer" (`AgentApp.peer_is_trusted`): maintenance routes included |
| **lan** | `0.0.0.0:<LAN_PORT>` — bound ONLY while `HKLM\SOFTWARE\Policies\EduBotics\LanMode = 1` (written by the per-machine installer; a standard user cannot write it) | an iPad browser (or any browser on the school network) | the Pi's robot login, without the claim step (owner, PLAN §2.3): one-time cloud ticket → cookie `edubotics_pi_session` (same name, TTL rule, caps and LRU rule as `pi_agent/pi_auth.py`) | what a robot session may do on a Pi; maintenance routes (`/factory-reset`, `/robot-type`, `/scene-camera-profile`, `/firmware/update`) need a teacher's or an admin's session (SA-12 unchanged; conductor's decision Q-C7, 2026-11-25) |
| **internal** | robot tier `127.0.0.1:0` (rosbridge subset + MJPEG), sidecar `127.0.0.1:0` | the station only | header `X-EduBotics-Internal: <256-bit secret>` minted per robot-tier start, handed to the child on stdin, compared constant-time; the sidecar keeps its own link tokens (no change) | — |

Rules (each has a test in C4-07):
- The zone comes from the ACCEPTING SOCKET, never from a header.
- local zone: `Host` must be exactly `127.0.0.1:<P>` or `localhost:<P>` (else the connection is closed,
  nginx's 444 rule); every path except `/__session` needs the host cookie (401 otherwise, no body that
  names a route); a WebSocket upgrade additionally needs `Origin` equal to the page's own origin (an
  ABSENT Origin is refused — stricter than today's nginx, which admits it for the GUI's health check;
  the host's health check sends the cookie over plain HTTP instead).
- lan zone: the Pi template's rules (`nginx.opi.conf.template`): `Host` must be an IP literal, a
  single-label name or `*.local` (421 otherwise); `/rosbridge`, `/video/stream` and `/daten-api/` need
  the robot session; `/api/system/*` enforces itself; the three proxied locations never forward the
  cookie; a POST with an empty Origin is refused.
- Both zones: the four security headers of `nginx.conf` on every response, `Cache-Control: no-store` on
  `/index.html` and `/version.json`, `public, immutable` under `/static/`, `/daten-api/` GET/HEAD only
  with `Referrer-Policy: no-referrer`, and **no request line that contains `/daten-api/` is ever
  logged** (the path holds a link token).
- Plain HTTP on the LAN is accepted (owner, PLAN §2.3) and stated in the IT sheet (C7-03).

`PORT_BASE`, `PORT_SPAN` and `LAN_PORT` are constants of `edubotics_station/ports.py`, chosen in C4-07
from the IANA dynamic range and recorded in the IT sheet; **no value is fixed by this spec** except
that `LAN_PORT` is one machine-wide literal (owner: "fixed machine-wide port") and never 80, 5557,
8444, 8769 or 9090 (the old product may hold them on the same PC).

## B3. Paths the station serves

| Path | local zone | lan zone | Served by |
|---|---|---|---|
| `/__session` | exchanges `k` for the cookie, 303 to `next` (a same-origin relative path only) | 404 | station |
| `/`, `/index.html`, `/static/*`, `/fonts/*`, `/robots/*`, `/v1-urdf/*`, `/blockly-media/*`, `/tutorials/*`, `/third-party-licenses.txt`, `/version.json` | cookie | public (the login page must load) | station, from `{app}\web\` |
| `/pi-mode.json` | `{"pi": false, "station": "windows"}` | `{"pi": true, "station": "windows"}` | station |
| `/rosbridge` (WebSocket) | cookie + own Origin | robot session + host-relative Origin | relayed to the robot tier's rosbridge subset |
| `/video/stream` (exact match; `topic=`, `type=ros_compressed`, the other query keys ignored) | cookie | robot session | relayed; `multipart/x-mixed-replace` JPEG passthrough from the newest frame of that topic, never re-encoded |
| `/daten-api/*` | cookie | robot session | relayed to the sidecar; 502 JSON `{"error":"sidecar_down"}` while the robot tier is down |
| `/api/system/*` | cookie | per route (B4) | station |
| anything else | SPA fallback `index.html` | SPA fallback | station |

## B4. `/api/system` — the Pi agent's contract, route by route

The contract's SOURCE is `robotis_ai_setup/pi_agent/agent.py` (router `_make_handler`, CONFIRMED route
list) and what `pages/SystemPage.js`, `utils/piMode.js`, `features/piAuth/piAuth.js`,
`components/PiUpdateGate.jsx`, `hooks/useRsBridgeStatus.js` read. It is made checkable by a FIXTURE:
`native/tests/fixtures/system_api_contract.json`, written by `native/tests/tools/record_system_contract.py`,
which drives the real `AgentApp` handlers with the fakes of `pi_agent/tests/test_agent_api.py` and
records, per route and outcome, the status code and every JSON key with its JSON type. The station's
handlers must answer the same key set and types; every difference is one entry of
`native/tests/system_contract_intentional.py` with its reason.

| Route | Pi agent today | Station (native) | Zone rule |
|---|---|---|---|
| `GET /health` | `{ok, agent_ready}` | same | public |
| `GET /auth/state` | `{ok, auth:1, claimed, session, hostname, pi_id[, expires_in, role][, setup_code]}` | lan: `claimed` is ALWAYS `true`, never a `setup_code`; local: 404 (the page is not in Pi mode and never asks) | public |
| `POST /auth/session` `{ticket}` | redeem at the cloud, set the cookie | same code path, ported (`edubotics_station/robot_login.py`); the station's id is `station_id` in `settings.json`, minted like `pi_auth.generate_pi_id` and sent as `pi_id` (the cloud's `PI_ID_RE` `[A-Za-z0-9_-]{16,64}` accepts it unchanged, CONFIRMED in `services/pi_tickets.py`) | lan only (local: 404) |
| `POST /auth/claim`, `POST /auth/setup-code` | claim flow | 404 (no claim step, owner decision) | — |
| `POST /auth/logout` | revoke this session | same | lan |
| `GET /auth/check` | nginx `auth_request` target | absent (no nginx) | — |
| `GET /status` | CONFIRMED keys: `lan_ip, hostname, agent_ready, agent_version, system_files_stale, system_files_version, manager_up, robot_tier_up, container_status, gateway_bound, arms_identified{leader,follower,both}, robot_type, robot_profiles[{id,display_de,scan_requires_leader,camera_roles}], hardware_ready, cameras[{path,role,name}], follower_only, images, scene_camera_profile, scene_camera_hint` | every key kept. Fixed values: `system_files_stale: false`, `system_files_version: null`, `manager_up: true`, `gateway_bound: true`, `images: null`, `robot_type: "v1_studio"`, one profile row. `container_status` keeps the four names the page knows (`physical_ai_manager`, `open_manipulator` = the arm link, `physical_ai_server` = the robot tier, `code_runner` = the broker). `follower_only` = teleop is off. ADDITIVE keys: `host_kind: "windows"`, `lan_mode: {enabled, url}`, `phone_supported: false`, `update_mode: "self" | "managed"`, `firmware: {version, protocol, update_available, embedded}` (`embedded` is false in a development build without the owner-signed pilot image; then no update is ever offered, C7-01), `wrist_camera: bool` | session |
| `POST /scan-arms` | docker prober per port | „Arme verbinden": ask the arm link to discover both ports by protocol (never by COM number) and return the Pi's shape (`leader`, `follower` as port labels; 404 nothing found / cross-family sentence does not exist any more; 409 → "the adapter answers but ids 11–16 do not" / "the HAT answers but is not provisioned", each with the German sentence built from the arm link's reason code) | session; refused in German while the robot tier is up unless the body confirms (it ends the session, as today's scan does) |
| `GET /cameras/scan`, `GET /cameras/preview?device=`, `POST /cameras/preview/stop`, `POST /cameras/roles` | V4L2 | the capture process: enumeration (`win_camera.list_windows_cameras`, USB-topology rules), an MJPEG preview of one device through `crop_to_target_aspect`, roles stored in `settings.json`; the role allowlist is the profile's `camera_roles` (German 400 as today) | session |
| `POST /scene-camera-profile` `{"profile": "tower_v1"|"legacy"}` | store + hint | store; `tower_v1` first runs `probe_scene_camera` (Appendix 2, T3) | maintenance |
| `POST /robot-type` | store | 400 in German for anything but `v1_studio` (one robot type; the selector is hidden by `robot_profiles` having one row) | maintenance |
| `POST /phone/toggle` | phone camera | 404; `/status.phone_supported` is `false` (phone camera removed, owner PLAN §2.5) | — |
| `POST /environment/start` `{cloud_only}` | start the robot tier | start arm link → capture → robot tier, in that order; `cloud_only: true` starts nothing and answers `{ok, cloud_only: true}` as the Pi does (CONFIRMED); refuses with 409 while `hardware_ready` is false | session |
| `POST /environment/stop` `[{force}]` | stop the robot tier | orderly end (C5-04): robot tier finalizes → rest-pose glide → torque off → robot tier exits → arm link → capture. Until C5-04: today's rule (the page asks the tower question first) | session |
| `GET /protokoll` (SSE) | journal tail | the station's ring buffer of the four processes' German Protokoll lines | session |
| `GET /netzwerk-check` | cloud reachability from the Pi | the same checks from the PC (cloud API, Supabase, Hugging Face, the Cloud-Inferenz host), same JSON shape | public |
| `GET /update/check`, `POST /update`, `GET /update/status/<id>` | Pi tarball self-update | the app's update channel (C7-02); until then `update_available: false` | check: public; start: session |
| `POST /factory-reset` | wipe volumes, unclaim | delete the data root's `datasets`, `hf`, `calib` and `settings.json` (both checkboxes and the typed word, unchanged), revoke every lan session; **no `setup_code` in the answer** | maintenance |
| `GET /roboter-studio/status` | `{follower_only, ready, busy, has_leader}` | same four keys: `has_leader: true`; `follower_only` = teleop off; `busy` while a sync glide or a switch runs; `ready` = robot tier up and arm connected | session |
| `POST /roboter-studio/leader-disable` / `leader-enable` | recreate the arm container | C5-02: teleop off at once / the student's action that starts the sync glide and then teleop | session |
| `GET /robot-key` | the node's public token key | same, from `<data root>\run\robot-key\hf_token_key.json` (the node's `write_key_file`, unchanged) | session |
| new `POST /host/close` `[{force}]` | — | the close guard's call (C4-12): the orderly end, then the host exits | local only |
| new `POST /firmware/update` | — | C7-02: streams ONLY the one embedded image whose sha256 is in the allowlist generated from spec B's `SIGNED-MANIFEST.json`; refused while anything runs, without the German consent, or when the board's build number is not lower | maintenance |
| new key `legacy_product` in `GET /status`; new `POST /legacy-product/ack` | — | the upload-first notice (C7-05): `{"state": "present" \| "absent" \| "unknown", "acknowledged": bool}`, from a read-only look at two registry keys and one file; the route stores the acknowledgement. **There is no import route** (owner Q-C3: no data is copied from the WSL product) | session |

## B5. Named React changes (owner rule, PLAN §2.5: "small changes are allowed when a spec names each one")

No file under `physical_ai_tools/physical_ai_manager/src` may change in this spec except the ones
below, in the package named. Each row names its test.

| Id | File(s) | Change | Package | Test |
|---|---|---|---|---|
| RC-1 | `utils/piMode.js` | `fetchPiMarker` returns `{pi, station}` (a 200 body with `station: "windows"` is a station; everything else as today); the provider exposes `stationMode` (= `station && !pi && isLoopbackPage()`) and `systemHost` (= `piMode || stationMode`); the agent-status poll runs when `systemHost`; `rsControlBase(systemHost)`; `getPiModeSync()` keeps meaning "robot login / remote robot" | C4-08b | `utils/__tests__/piMode.station.test.js` (four marker bodies × two page hosts) |
| RC-2 | `StudentApp.js` | the `piOnly` nav filter uses `systemHost`; passes `stationLocal` to the gate; mounts `StationCloseGuard` when `stationMode` | C4-08b, C4-12 | `__tests__/StudentApp.station.test.jsx` |
| RC-3 | `utils/authGate.js` | new optional input `stationLocal`: `if (stationLocal && page === PageType.SYSTEM) return 'app'` placed directly after the `piMode && piLogin === 'pending'` line; nothing else moves | C4-08b | `__tests__/authGate.test.js` gains the truth table rows; its source-text fence is extended, not relaxed |
| RC-4 | `components/StartupGate.js`, `components/HeartbeatStatus.js`, `components/Home/HealthCard.jsx::disconnectedHintDe`, `pages/RecordPage.js` (the offline card), `components/Workshop/teach/TeachOverlay.jsx` (the argument of `teachLeaderStatusNoticeDe`) | every place that today chooses between a Pi sentence and a Windows-GUI sentence („Öffne EduBotics auf dem Desktop und klicke „Umgebung starten"", „… prüfe ob Docker Desktop läuft …", „das EduBotics-Programm auf diesem PC antwortet nicht" — CONFIRMED present) switches on `systemHost` instead of `piMode`, so the station shows the System-tab sentence. ONE new sentence: `utils/piMode.js::STATION_LINK_HINT` („Die Verbindung zum Roboter-Dienst ist unterbrochen. Bitte kurz warten; hilft das nicht, die Umgebung im Tab „System" stoppen und neu starten."), used instead of `PI_PORT_BLOCKED_HINT` when `stationMode` and the robot tier is up but rosbridge is not. `components/PiLoginNotice.jsx` is NOT changed (robot login is Pi mode only) | C4-08b | existing tests + `__tests__/stationWording.test.jsx` (no rendered string names „Docker" or „auf dem Desktop" in station mode) |
| RC-5 | `components/Workshop/LeaderToggle.jsx`, `hooks/useRsBridgeStatus.js` | pass `systemHost` (not `piMode`) into `rsControlBase`; nothing else. (`RobotHero.jsx`, `ImageGridCell.js`, `CameraFeedOverlay.jsx` only call `videoStreamBase`, whose `piMode` parameter is vestigial — CONFIRMED — and stay untouched.) | C4-08b | `LeaderToggle.origin.test.js` (the request URL is same-origin `/api/system/…` in station mode) |
| RC-6 | `pages/SystemPage.js` | host-aware wording and sections from `agentStatus.host_kind`: eyebrow „Windows-PC" vs „Orange Pi"; the `.local` line only for `pi`; the robot-type selector hidden when `robot_profiles.length === 1`; the phone row only when `phone_supported !== false`; „Aktualisierung" text for the app instead of container images; a new card „iPad als Bildschirm" (URL and QR) only when `lan_mode.enabled`; a factory reset without `setup_code` shows the plain success toast; „Arme scannen" reads „Arme verbinden" for `windows`; the arm rows show the arm link's verdict sentence unchanged (C4-09); one card „Frühere EduBotics-Version auf diesem PC" while `legacy_product.state` is not `absent` and not acknowledged (C7-05) | C4-08b, C4-09, C7-02, C7-03, C7-05 | `pages/__tests__/SystemPage.station.test.jsx` (snapshot per `host_kind`; the `pi` snapshot must equal today's) |
| RC-7 | `components/PiUpdateGate.jsx`, `hooks/useAgentUpdate.js` | arms in `stationMode` too; the app wording; in `update_mode: "managed"` it shows the non-blocking IT sentence instead of the forced modal | C7-02 | `__tests__/PiUpdateGate.station.test.jsx` |
| RC-8 | new `components/StationCloseGuard.jsx` | listens for the DOM event `edubotics:close-requested` the host raises, runs the stop flow (the existing `TowerStopDialog` when the station answers `rest_glide_refused`), then calls `POST /api/system/host/close` | C4-12 (C5-04 adds the refusal branch) | `__tests__/StationCloseGuard.test.jsx` |
| RC-9 | `components/icons/robotAssets.js`, `public/robots/rec-warmup-v1_studio.webp`, `rec-reset-v1_studio.webp`, `collision-v1_studio.webp`, the `leader` glyph cell for the V1 in `glyphs.png` | V1 phase pictures and leader glyph, rendered by the existing untracked renderer from the V1 URDF | C5-09 | `icons/__tests__/robotAssets.test.js` (pins the new files by size and sha256) |
| RC-10 | `features/cloudInference/controller.js`, `robotCalls.js`, `robotContract.js`, `gating.js`, `cloudCopy.js`, `components/CloudInference/CloudInferenceView.jsx` | keeps the reserve's `dataset_robot_type` and `dataset_joint_names`; sends them in the start call's two new fields; disables Start with the German reason when both sides are known and differ | C5-07 | `features/cloudInference/__tests__/crossRobot.test.js` |
| RC-11 | `components/MyModels.js` (the „auf den Roboter laden" control only), `hooks/useRosServiceCaller.js` (`sendTrainingCommand`, `getTrainingInfo`, the download call), `hooks/useRosTopicSubscription.js` (the `/training/status` subscription) | removed (owner, VERIFICATION §7 Q20) | C4-02 | `__tests__/wireInventory.test.js` (new: the names React uses equal the fixture the engine serves) |
| RC-12 | `constants/appMode.js`, `utils/cloudMode.js`, `utils/piMode.js`, `utils/robotLocality.js`, `App.js` | third build mode `REACT_APP_MODE=cloud`: `isCloudOnlyMode()` is true, Pi detection is skipped (`piMode` false, `piModeResolved` true at once), no rosbridge seed | C7-04 | `__tests__/cloudHostMode.test.js`; the web build's snapshot tests prove `web` is unchanged |

Not changed, stated so nobody "fixes" it: `utils/pageHost.js::isLoopbackPage` (the local window is
`127.0.0.1:<P>` → loopback, CONFIRMED rule), `utils/robotLocality.js::robotIsRemote` for student and
station builds (a local station page is NOT remote: marker `pi` is false and the page is loopback),
`features/piAuth/piAuth.js` (the lan zone speaks its protocol as it is), `utils/rosConnectionManager.js`,
`TaskInfo.record_rosbag2` (React keeps sending it; the field stays in `TaskInfo.msg`).

## B6. Settings and policy

- Per user: `<data root>\settings.json` (atomic write; schema version 1): `port`, `station_id`,
  `camera_roles`, `scene_camera_profile`, `wrist_camera`, `cloud_only`, `arm_trim`,
  `legacy_notice_ack`. `<data root>` is `%LOCALAPPDATA%\EduBoticsStation` unless the policy names
  another. **It is deliberately NOT `%LOCALAPPDATA%\EduBotics`** (revision 2): that folder belongs to
  the WSL product — its GUI's `.env`, its WebView profile, and since 2.41.1 each account's WSL disk
  `…\EduBotics\wsl\ext4.vhdx` (spec A's D-A3). Sharing it would put the old product's disk inside
  what the native uninstaller and the factory reset may delete. A policy `DataRoot` that is, or lies
  under, `%LOCALAPPDATA%\EduBotics`, `%ProgramData%\EduBotics` or the WSL product's install directory
  is refused at start with a German sentence (`native/tests/test_policy.py`).
- Per machine, read-only for the app: `HKLM\SOFTWARE\Policies\EduBotics`: `LanMode` (DWORD 0/1),
  `UpdateMode` (`self` | `managed`), `DataRoot` (a path). Absent key = `0` / `self` / the default.
  The per-machine installer writes them; the app never does (`native/tests/test_policy.py` AST-fences
  that no code path opens the key for writing).
