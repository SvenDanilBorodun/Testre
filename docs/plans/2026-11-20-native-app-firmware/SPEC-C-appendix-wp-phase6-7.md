# SPEC C — Appendix 6: work packages of Phase 6 (Roboter Studio and the sandbox) and Phase 7 (product)

Belongs to `SPEC-C-native-app.md` (§4 there is the index and defines the test command and gate ids).
Paths: `SRV` = `physical_ai_tools/physical_ai_server`, `PKG` = `SRV/physical_ai_server`,
`MGR` = `physical_ai_tools/physical_ai_manager`, `RAS` = `robotis_ai_setup`,
`RUN` = `RAS/docker/code_runner/runner`.

---

**Revision 2 (2026-11-25).** The owner's answers (`SPEC-DECISIONS.md`), spec B §8.1–§8.8 as revised and
spec A's WP-A8 are folded in. Rewritten here: C7-01, C7-02, C7-05, C7-06. Touched: C7-03, C7-04,
C7-10, C7-11. The main file's §13 lists every change.

**Revision 3 (2026-11-25, final round).** C7-01: the firmware pin and fetch follow SPEC-B §8.11
exactly (`firmware.pin` is `none` or a build number; the URL is derived; spec B's
`fwcheck.verify_signed_image` is the one check), and it replaces C4-00's CLOUD-ONLY mode by the
native mode (owner Q-C13 = b). C7-02: one sentence on the same check.

# Phase 6 — Roboter Studio and the sandbox

## C6-01 — Roboter Studio and the manual modes on the arm bridge
- **Summary.** Prove, and close the gaps of, every arm-moving Roboter-Studio path against the arm
  bridge: Blockly and code programs, jog, the manual home glide, hand-guide, manual record, replay,
  capture pose, Vormachen in hand and in leader mode. New code is only the CONSEQUENCE of a board
  event: a mixin `PKG/arm_faults.py::ArmFaultMixin.on_arm_fault(code, text_de)` that ends what owns
  the arm in German — a running program through `WorkflowManager.stop()` plus an `error` status with
  the sentence, a manual drive through `_manual_stop_event` plus `_publish_manual_notice`, a cloud run
  through `_end_inference(text, end_reason='robot_error')` — and is called by `arm_bridge/events.py`.
  Today a refused trajectory only logs in the driver and the program "finishes green"; from here it
  fails loud.
- **dependsOn** C5-02 · **externalGates** `B-ARMLINK-API`, `RIG-V1-HAT`
- **layers** 9, native-engine
- **files.may** `PKG/arm_faults.py` (new), `PKG/physical_ai_server.py` (the mixin in the class bases
  only), `native/engine/edubotics_robot/arm_bridge/events.py`, `native/engine/edubotics_robot/texts_de.py`,
  `native/tests/test_studio_on_bridge.py`, `native/tests/test_manual_on_bridge.py`,
  `native/tests/characterisation/t07_*`, `SRV/test/test_arm_faults.py`
- **files.mustNot** `PKG/workflow/**`, the manual arbiter (`_mode_lock`, `_recompute_on_manual_locked`,
  `_manual_exit_gen`), `motion._hold_motion_lock`, every lock order
- **testCommands** T-NATIVE (the two files, t07), T-SERVER (whole suite; it contains `test/test_code_*.py` and the `rig_emulation` cases)
- **acceptance** (1) A Blockly program (`home`, `bewege zu`, `Greife`, `ablegen bei`) on the fake
  link: the link receives trajectories whose waypoints equal the ones `_trajectory_publisher`
  published (count and values), and nothing else. (2) Hand-guide on → exactly one `torque(False)`; no
  goal reaches the link while limp (the fake link fails the test on one); hand-guide off →
  `torque(True)` and NO pre-hold message (`_follower_rail_is_ros2_control()` stays False for the V1).
  (3) A torque refusal's German reason appears in the service answer through `_last_torque_reason()`.
  (4) A board abort in the middle of a program ends the run with phase `error` and the sentence; the
  next start works. (5) Leader-mode Vormachen: `start_leader` with teleop on records the follower at
  25 Hz; teleop turning off discards the take with `leader_lost`. (6) The manual home glide's tower
  refusal and `/edubotics/home_glide_state` behave as in the node's existing tests.
- **Failing test** `native/tests/test_studio_on_bridge.py::test_a_board_abort_fails_the_program_in_german`.
- **risk** high · **lenses** correctness, delivery, regression

## C6-02 — The code RPC over named pipes on Windows
- **Summary.** Add `PKG/workflow/code_transport.py` (stdlib only): `listen(address, security)` and
  `connect(address)` return objects with exactly the socket subset the code uses (`recv`, `sendall`,
  `settimeout`, `close`); AF_UNIX on POSIX (today's behaviour), named pipes on Windows through
  `ctypes` (`CreateNamedPipeW` with an explicit security descriptor and
  `FILE_FLAG_FIRST_PIPE_INSTANCE`, overlapped I/O for the timeouts). `code_rpc.CodeRpcServer` and
  `code_program.CodeProgram._connect_control` use it. `robot_api.py` renders both stubs with the
  second transport (Python: `CODE_RPC_PIPE`; Java: `RandomAccessFile` on the pipe path) and the four
  generated files are re-rendered. `runner_limits.STUDENT_ENV_NAMES` gains the Windows names.
- **dependsOn** C6-01 · **externalGates** `B-S4`, `SPIKE-RUN`
- **layers** 9, runner
- **files.may** `PKG/workflow/code_transport.py`, `code_rpc.py`, `code_program.py`, `robot_api.py`,
  `RUN/lib/robot.py`, `RUN/java/edubotics/Robot.java`, `Greifobjekt.java`, `RpcClient.java`
  (generated), `RUN/runner_limits.py`, `SRV/test/test_code_transport.py`,
  `SRV/test/test_robot_api_generated.py`, `RAS/tests/test_code_runner_limits_lockstep.py`,
  `native/tests/platform_skips.json`
- **files.mustNot** `robot_api.RPC_LIMITS` and every bound, `code_rpc._validate_call`,
  `RunSession.charge_call`, `RUN/java/edubotics/EduJson.java`, `Launcher.java`
- **testCommands** T-SERVER (ubuntu and windows), T-DEPSFREE (`-p "test_code_runner_*.py"`), javac compile of the Java tree (`EDUBOTICS_REQUIRE_JAVAC=1`)
- **acceptance** (1) On POSIX nothing changes: `test_code_rpc*.py`, `test_code_program_stop.py`
  unedited and green. (2) On Windows the same files run through pipes and are removed from the skip
  registry; `CODE_STOP_DEADLINE_S` (derived, not a literal) holds in
  `test_code_program_stop.py`. (3) The generated stubs are byte-equal to the renderer's output. (4) A
  half-written frame followed by silence ends in `ReadAbandoned` within the same timeout as on a
  socket.
- **Default / alternative.** Default: named pipes. If S4 shows pipe timeouts cannot be made to hold
  the deadline: loopback TCP bound to `127.0.0.1:0` with the run token as the first frame AND the
  AppContainer's loopback exemption for that one port — which needs the owner (it weakens "no
  network") and is therefore an O11 question, not a silent fallback.
- **Failing test** `SRV/test/test_code_transport.py::test_settimeout_and_partial_reads_behave_like_a_socket`.
- **risk** high · **lenses** correctness, delivery, regression

## C6-03 — The Windows sandbox: broker, AppContainer, Job Object, trimmed JDK
- **Summary.** Write `native/sandbox/edubotics_sandbox/` — `broker.py` (the supervisor's control protocol,
  reusing `RUN/supervisor.py`'s frames, `Run`, `_LinePump`, `validate_start`, `parse_javac_output` by
  import), `win_spawn.py` (per-user AppContainer profile; a per-run directory the container SID may
  modify; the student process created suspended with the container's security capabilities, an
  explicit handle list and a Job Object: memory, active-process and CPU-rate limits,
  kill-on-job-close, no breakaway; then resumed), `win_limits.py` (the values of `runner_limits`
  mapped; a run-directory size watchdog replaces `RLIMIT_FSIZE`), kill = `TerminateJobObject`. Move
  the POSIX-only imports inside their POSIX functions (CONFIRMED today: `RUN/runner_limits.py` has
  `import resource` at module top, and `supervisor.py` imports `runner_limits`, so neither imports on
  Windows). Add `native/packaging/jlink.ps1` (a runtime with `java.base`, `java.compiler`,
  `jdk.compiler`). Spec B's spike code under `native/spikes/windows/sandbox/` (`appcontainer.py`,
  `pipes.py`, `job.py`) is the starting point; it is COPIED into the product package and reviewed —
  the product never imports `native/spikes`.
- **dependsOn** C6-02 · **externalGates** `B-S1`, `B-S2`, `B-S3`, `B-S5`, `B-SJ`, `O11` (only if a
  spike failed), `OWNER-SIGNOFF-SANDBOX`, `SPIKE-RUN`, `SCHOOL-PC`
- **layers** native-sandbox, runner
- **files.may** `native/sandbox/edubotics_sandbox/**`, `RUN/supervisor.py`, `RUN/runner_limits.py`,
  `RUN/sandbox_exec.py` (import placement and path overrides only), `native/packaging/jlink.ps1`,
  `native/engine/edubotics_station/supervisor.py` (start the broker with the robot tier),
  `native/tests/test_sandbox_*.py`, `RAS/tests/test_code_runner_*.py` (only where a moved import is asserted)
- **files.mustNot** `RUN/student_main.py`, `RUN/lib/edubotics_debug.py`, the Docker sandbox keys and
  `RAS/tests/test_code_runner_sandbox.py` (the container sandbox stays whole until C7-06)
- **testCommands** T-NATIVE (`tests/test_sandbox_*.py`), T-DEPSFREE (`-p "test_code_runner_*.py"`), T-WIN (as a standard user)
- **acceptance** The matrix is exactly spec B's S1 and S5 tables (B05), now against the PRODUCT
  code. From INSIDE a student process on `windows-2025`, running as a non-admin user, each of
  these is refused (one test each, `native/tests/test_sandbox_escape_matrix.py`; plus
  `create_global_object`, `enumerate_pipes`, `dpapi_unprotect_user_blob`, `shellexecute_runas`): TCP connect to the
  station's port; TCP connect to a public host; reading a file in the user's Documents; `OpenProcess`
  on the broker and on the robot tier; writing outside the run directory; creating a process that
  breaks away from the job; opening the broker's control pipe; opening the data pipe of another run.
  And each of these works: the data pipe with the run's token; `import socket, ctypes, subprocess`;
  `sys.monitoring`; `javac` with its real message on a broken `Main.java`; the Java smoke. A program
  that allocates past the memory limit, forks past the process limit, or fills the run directory is
  ended and the run reports the existing German sentence (`memory`, `killed`). The broker dying kills
  every student process.
- **Default / alternative.** Default: the broker is a child of the station inside the app's job, the
  student job nested. If S3 shows nesting fails: the broker is created with breakaway and owns its own
  kill-on-close job whose handle the station holds.
- **Failing test** `native/tests/test_sandbox_escape_matrix.py::test_a_student_process_cannot_reach_loopback`.
- **risk** high · **lenses** correctness, delivery, regression

## C6-04 — Rule §2's four conditions re-proven; the code end-to-end smoke on Windows
- **Summary.** Give `.github/scripts/code_e2e_smoke.py` a native mode (robot tier + broker instead of
  two containers), add the job `native-code-e2e-windows` (standard user) to `native/ci/native-proof.yml`, and write
  `docs/NATIVE-SANDBOX-SIGNOFF.md`: for each of the four conditions the test ids, the spike results of
  spec B it rests on, the residuals against the container (research/03 §6.4) and a signature line.
- **dependsOn** C6-03 · **externalGates** `B-STDUSER-HARNESS`, `OWNER-SIGNOFF-SANDBOX`, `SPIKE-RUN`
- **layers** CI/CD, 9, docs
- **files.may** `.github/scripts/code_e2e_smoke.py`, `native/ci/native-proof.yml` (plus its copy if C4-01b has landed),
  `RAS/tests/test_code_e2e_smoke_wiring.py`, `docs/NATIVE-SANDBOX-SIGNOFF.md`,
  `native/tests/test_four_conditions.py`
- **files.mustNot** the smoke's assertions for the container mode, `PKG/workflow/**`
- **testCommands** T-WIN (`native-code-e2e-windows`), T-NATIVE (`tests/test_four_conditions.py`), T-DEPSFREE (`-p "test_code_e2e_smoke_wiring.py"`)
- **acceptance** Condition 1: a raw-pipe flood from inside the sandbox is answered `rate` beyond
  `RPC_LIMITS`, charged before validation. Condition 2: a student process that ignores every signal
  and never reads is gone within the derived `CODE_STOP_DEADLINE_S` (the test measures and prints it).
  Condition 3: the hostile-frame table of `test_code_rpc*.py` sent from a raw pipe client. Condition 4:
  the student process's token carries the AppContainer SID and Low integrity and is not the broker's.
  The smoke's Java/Python fault matrix, every Methoden example in both languages in the simulator and
  the Python put-back case pass.
- **Failing test** the job (red until C6-03) and `native/tests/test_four_conditions.py::test_condition_4_token`.
- **risk** high · **lenses** correctness, delivery, regression

## C6-05 — Phase 6 documentation
- **Summary / files / acceptance** as C4-13, for Phase 6: the interim CLAUDE.md section, changelog,
  known issues (sandbox residuals, `SCHOOL-PC` results).
- **dependsOn** C6-04 · **risk** low · **lenses** combined · **testCommands** `python .github/scripts/german_detail_lint.py`

---

# Phase 7 — product

## C7-01 — Packaging: one build, two installers, the signing hook, the parity gate
- **Summary.** `native/packaging/build.ps1` stages the app: the embeddable CPython 3.13 (URL and
  sha256 pinned in `native/packaging/python.pin`), `site\` from the lock
  (`pip install --require-hashes --only-binary :all: --target`), compiled `.pyc`, the source trees
  named by `native/engine/edubotics_sources.py`, the React student build, the trimmed JDK, the
  firmware, `VERSION`, the licence files and generated third-party lists.
  **The firmware (SPEC-B §8.1, §8.4).** The app embeds exactly ONE image: the pilot firmware the
  owner produced with `sign_release.sh --channel pilot`, identified by the committed
  `native/firmware/releases/<build_number>/SIGNED-MANIFEST.json` (the signed binary is a release
  asset and never in the repository). Every image that boots on a HAT is signed by the owner's
  offline release key; no build of ours signs firmware, holds a key or calls `sign_release.sh`.
  **Pin, fetch and check are SPEC-B §8.11's, word for word.** `native/packaging/firmware.pin` holds
  exactly `none` or the decimal `build_number` (for example `7`) — never a URL, a path or a hash. A
  number selects `native/firmware/releases/<build_number>/SIGNED-MANIFEST.json` from the same
  checkout; everything else is derived from that manifest. The binary's URL is
  `https://github.com/<channel>/releases/download/<release_tag>/<asset_name>` with `<channel>` the
  release channel the build already uses (`${{ vars.RELEASES_REPO || github.repository }}`) and the
  manifest's two fields (`hat-a-fw-b<n>`, `edubotics-hat_a-prod-b<n>.bin`; the owner uploaded the
  binary there by hand); an anonymous GET, no free URL for a tagged build. Before embedding,
  `build.ps1` runs its helper `native/packaging/fetch_firmware.py`, which fetches the bytes and hands
  them to spec B's `edubotics_boardproto.fwcheck.verify_signed_image` — size, signed and
  unsigned sha256, the ECDSA signature under the committed `release_pub.pem`, `key_sha256`, `board`,
  `variant`, `channel` `pilot`, `protocol` = the app's N — and NEVER a check of its own; any
  mismatch fails the build. It then writes `native/engine/edubotics_station/firmware_allowlist.py`,
  GENERATED from the same manifest (build number, signed sha256, protocol), never typed. An
  untagged development build may take the bytes from a local file (`build.ps1 -FirmwareFile <path>`),
  checked identically; with `none` it embeds no firmware and its station reports
  `firmware_embedded: false`. A TAGGED build is refused with `none`, with a number whose manifest is
  absent, and with an asset that fails the check.
  **Launcher and installers.** `native/packaging/launcher/EduBotics.c` is a small launcher
  (`pythonw -I -m edubotics_host`, an AppUserModelID of its own).
  `native/packaging/installer/EduBotics.iss` is ONE Inno Setup script built twice (conductor's
  decision Q-C2; an MSI only if a pilot school's IT asks): `EduBotics_Setup_User.exe`
  (`PrivilegesRequired=lowest`, into `%LOCALAPPDATA%\Programs\EduBotics Station`) and
  `EduBotics_Setup_Machine.exe` (admin, into `{autopf}\EduBotics Station`, writes the policy key's
  defaults, offers the task „iPad als Bildschirm erlauben"). **The two products share nothing on a
  PC:** the script has its OWN `AppId` (a new GUID fixed in the script), never
  `{B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}` (the WSL product's, CONFIRMED); it never installs into
  `{autopf}\EduBotics` (the WSL product's directory, CONFIRMED); and the app's data root is
  `%LOCALAPPDATA%\EduBoticsStation` (Appendix 4 B6), never `%LOCALAPPDATA%\EduBotics`, which holds the
  WSL product's `.env` and, since 2.41.1, each account's WSL disk (`…\EduBotics\wsl\ext4.vhdx`, spec
  A's D-A3). `sign.ps1` signs every PE when `EDUBOTICS_SIGN_CMD` is set and says so when it is not
  (pilot builds are unsigned, PLAN §2.2). The stage list ships `edubotics_armlink` and
  `edubotics_boardproto` and NEVER `edubotics_devboard`, `native/bench`, `native/spikes`,
  `native/firmware` sources or `native/tests`. `app_source_parity.py` diffs the staged tree against HEAD.
  **Workflows.** `.github/workflows/native-build.yml` (`workflow_call` + `workflow_dispatch`) builds,
  smokes and uploads both installers with their sha256. `VERSION` on `main` simply goes on (it is
  `3.0.N` since C4-00; the first native release is the next number the owner picks) and the
  preflight's three fallback-literal checks are replaced by the native one
  (`native/host/edubotics_host/version.py`). **`release.yml` on `main`: the cloud-only mode of C4-00
  is replaced by the native mode.** The marker `.github/release-mode` becomes `native`; the step
  `product` accepts exactly `native` on `main` (→ `client_build=true`; `cloud-only` and every other
  content fail the preflight, as before); the job `next-step-cloud` is deleted; `docker`, `installer`
  and `pi-agent-tarball` are replaced by ONE job `native-build`, which takes over `docker`'s `needs`
  and its whole `if:` (WP-A8's five lines and C4-00's `client_build == 'true'` line), and `next-step`
  needs it. WP-A8's `mode` step and its four cloud gates stay byte for byte, as do the jobs
  `supabase`, `trainer-image`, `trainer-image-var`, `cloud-api` and `teacher-web`. Order on a `main`
  tag: supabase and trainer-image → trainer-image-var → cloud-api → teacher-web → native-build →
  next-step.
- **dependsOn** C5-07, C5-08, C6-04 · **externalGates** `B-PROTOCOL-FREEZE` (spec B's B18), `B-S7`,
  `B-S8`, `B-SJ`, `B-FIRMWARE-ARTIFACT` (the first `--channel pilot` image and its
  `SIGNED-MANIFEST.json`), `OWNER-FW-CI` (spec B's release gate, asked when B18 merges: no pilot
  image can exist before the owner has answered it, so nothing can be embedded before), `OWNER-CI-EDIT`,
  `SCHOOL-PC`. NOT a gate of this package: the GPL licence review (x264 inside PyAV, the JDK) — owner
  Q-C10: before any NON-pilot distribution; this package records it as an open release condition
  (acceptance 10) and C7-02's runbook repeats it.
- **layers** native-packaging, CI/CD, 1 (installer)
- **files.may** `native/packaging/**`, `native/engine/edubotics_station/firmware_allowlist.py`
  (generated), `.github/workflows/native-build.yml`, `.github/workflows/release.yml`, `VERSION`,
  `native/host/edubotics_host/version.py`, `.github/scripts/third_party_python_notices.py`,
  `RAS/installer/assets/license.txt` and `THIRD_PARTY_NOTICES.txt` (the JDK and the native lists),
  `native/tests/test_packaging_shape.py`, `RAS/tests/test_native_release_shape.py`,
  `.github/release-mode`, `RAS/tests/test_main_cloud_only_release.py` (becomes the native-mode test),
  `RAS/tests/test_deploy_approval_gate.py` (the cloud-only arrivals become native arrivals; C4-00's
  mutations are re-anchored on `native-build`), `RAS/tests/test_ci_supply_chain_pins.py`,
  `docs/KNOWN-ISSUES.md` (the licence condition only)
- **files.mustNot** `RAS/installer/robotis_ai_setup.iss` and `RAS/installer/scripts/**` (deleted only by
  C7-06), W1/W2/W3/W6 workflow files, `native/firmware/**` (spec B's; the manifest is READ),
  `native/KEYS.md`, any private key anywhere
- **testCommands** T-NATIVE (`tests/test_packaging_shape.py`), T-DEPSFREE (`-p "test_native_release_shape.py"`,
  `-p "test_deploy_approval_gate.py"`, `-p "test_ci_supply_chain_pins.py"`), T-WIN (`native-build`),
  `zizmor --offline --no-progress --min-severity low .github/workflows/`
- **acceptance** (1) The per-user installer runs to the end as a standard user with no elevation
  prompt (`native-build` runs it as the standard user and starts `EduBotics.exe --selftest`, which
  imports the recorder closure, starts and stops the station, and exits 0). (2) The per-machine
  install runs the same self-test for a standard user from Program Files. (3) No network access
  during install (the job blocks outbound traffic for the install step). (4) `app_source_parity.py`
  fails when one staged file is altered, and when `edubotics_devboard` or a `bench` module is in the
  stage. (5) With a throw-away certificate in CI, every `.exe`, `.dll`, `.pyd` verifies; without
  one the build log states that the build is unsigned. (6) Uninstall removes the install directory
  only; it asks before it deletes the data root, default no, and then deletes exactly
  `%LOCALAPPDATA%\EduBoticsStation`.
  `test_packaging_shape.py::test_the_native_installer_shares_no_id_and_no_path_with_the_wsl_product`
  reads both `.iss` files: the AppIds differ, neither install directory is the other's, and no
  `[UninstallDelete]`, `[InstallDelete]` or Pascal delete of the native script names a path under
  `{localappdata}\EduBotics\`, `{autopf}\EduBotics\` or `{commonappdata}\EduBotics`. (7) The installed
  size is printed into the job summary; **no size bar is set by this spec** (S8). (8) Firmware:
  `test_packaging_shape.py` accepts for `firmware.pin` exactly `none` or `[1-9][0-9]*` and refuses a
  URL, a path, a hash and a blank; the fetch URL is built only from the channel and the manifest's
  `release_tag` and `asset_name`; the build calls `fwcheck.verify_signed_image` and contains no
  hashing or signature code of its own (`fetch_firmware.py` imports neither `hashlib` nor
  `cryptography`, by AST; `build.ps1` has no `Get-FileHash` on the firmware); a binary with one flipped bit, a manifest whose `channel` is not `pilot`, a
  tagged build with `none` and a tagged build whose manifest is absent each stop the build; the
  generated allowlist equals the manifest's signed sha256; no private key, no `-sign` call and no
  `sign_release.sh` invocation exists under `native/packaging` or `.github` (text scan). (9) On a
  `main` tag the simulated job graph (`test_deploy_approval_gate.py`'s evaluator, `client_build:
  'true'`) runs supabase, trainer-image, trainer-image-var, cloud-api, teacher-web, native-build,
  next-step; with an empty `client_build` `native-build` is skipped (the fail-closed gate of C4-00
  is kept); the executed test of the step `product` now expects `native` → `client_build=true` and
  `cloud-only`, a missing or any other marker → exit 1; WP-A8's tests are edited only where they
  name the three replaced jobs. (10) `docs/KNOWN-ISSUES.md` names the
  licence review as an open condition of any non-pilot distribution (owner Q-C10).
- **Failing test** `native/tests/test_packaging_shape.py::test_the_stage_list_names_every_source_tree`.
- **Settled with spec B (SPEC-B §8.11, M20).** Where the binary lives, what the pin names and who
  checks it are no longer open: the public release channel, release `hat-a-fw-b<n>`, asset
  `edubotics-hat_a-prod-b<n>.bin`, uploaded by the owner by hand; the pin is a number or `none`; the
  check is spec B's function.
- **risk** high · **lenses** correctness, delivery, regression

## C7-02 — The update channel, N and N−1, firmware updates
- **Summary.** Cloud API: `/version` gains additive keys for the native app (`native_version`,
  `native_min_version`, `native_user_url`, `native_user_sha256`, `native_machine_url`,
  `native_machine_sha256`, `native_manifest_sig`), URLs derived only from `GUI_RELEASE_REPO`, the tag
  and two fixed asset names (SA-01's rule).
  **The manifest signature (owner Q-C4: the owner creates the Ed25519 key, holds it offline and
  signs by hand; no CI secret).** `native/packaging/sign_manifest.py` is run by the owner on his own
  machine: it reads the private key from a path on his medium (never from the environment, never
  from a repository file), prints the canonical manifest and its base64 signature, and stores
  nothing. `native/host/edubotics_host/update_key.py` holds the PUBLIC key as a constant (the file
  the owner hands over, committed verbatim). `publish-native-version.yml` is W6's twin:
  dispatch-only, from `main`, inputs `version`, the two tested sha256 and `manifest_sig`; before it
  changes anything it proves that both assets download anonymously with the given hashes AND that
  `manifest_sig` verifies against `update_key.py` for exactly the manifest it is about to publish;
  then the Railway variables with `--skip-deploys` and the version last, `environment: production`.
  No workflow, script or test reads a private key or a signing secret; no agent creates, receives
  or holds one. Custody and the signing procedure are written into `native/KEYS.md` — spec B's one
  runbook for all three owner keys — in the section spec B reserved for the update-manifest key,
  and nowhere else.
  **The updater.** Host `updater.py` verifies the signature over the canonical manifest with the
  public key compiled into the host, downloads (HEAD pre-check, Content-Length, SHA-256), and for a
  per-user install runs the orderly end, the installer silently, and restarts; under `UpdateMode =
  managed` it only tells the user. A build older than `native_min_version` keeps the cloud pages and
  refuses `environment/start` with `APP_TOO_OLD_DE`.
  **Firmware.** The station's `firmware` status and `POST /api/system/firmware/update` (maintenance)
  call spec B's `firmware_update(image, sha256, build_number, progress_cb)` only with the ONE image
  the app embeds, whose sha256 is in the allowlist C7-01 generated from the owner-signed
  `SIGNED-MANIFEST.json`. An image that is not in it, a build number that is not above the board's,
  and an app without an embedded image (`firmware_embedded: false`) never reach the call. The
  embedded image was checked at build time by spec B's `fwcheck.verify_signed_image` (C7-01) and
  the board verifies the owner's signature again on its own; the app only chooses and streams. A firmware newer than the app is
  never replaced. RC-7.
  `docs/deploy/NATIVE-RELEASE.md`: the owner's runbook (tag, hardware test, sign the manifest, start
  the publish workflow, roll an advertisement back) and the release conditions — among them the GPL
  licence review before any non-pilot distribution (owner Q-C10).
- **dependsOn** C7-01 · **externalGates** `OWNER-MERGE-CLOUD`, `OWNER-CI-EDIT`, `OWNER-UPDATE-KEY` (an
  owner ACTION, decided by Q-C4: he generates the key pair as `native/KEYS.md` says and hands over
  the public key), `B-FIRMWARE-UPDATE` (spec B's B10), `B-FIRMWARE-ARTIFACT` and with it `OWNER-FW-CI`
  (as in C7-01: the firmware part needs an embedded pilot image), `SCHOOL-PC`
- **layers** 7, CI/CD, native-host, native-engine, 6
- **files.may** `RAS/cloud_training_api/app/routes/version.py`, `app/tests/test_version_native_keys.py`,
  `.github/workflows/publish-native-version.yml`, `native/packaging/sign_manifest.py`,
  `native/host/edubotics_host/updater.py`, `update_key.py`,
  `native/engine/edubotics_station/system_api.py`, `texts_de.py`, RC-7's files,
  `native/KEYS.md` (ONLY its reserved update-manifest-key section; every other line is spec B's),
  `docs/deploy/NATIVE-RELEASE.md`, `native/tests/test_updater.py`, `native/tests/test_update_manifest.py`,
  `RAS/tests/test_publish_native_version.py`, `RAS/tests/test_deploy_approval_gate.py`
- **files.mustNot** `publish-gui-version.yml`, the existing keys' code path in `version.py` (their
  values must be byte-identical with and without the new variables), any migration, every other
  section of `native/KEYS.md`, `native/firmware/**`, any private key anywhere
- **testCommands** T-CLOUD, T-NATIVE (`tests/test_updater.py tests/test_update_manifest.py`), T-DEPSFREE
  (`-p "test_publish_native_version.py"`, `-p "test_deploy_approval_gate.py"`), T-REACT
- **acceptance** (1) The WSL GUI's and the Pi's keys are unchanged. (2) A manifest with a wrong
  signature, a hash that does not match, a URL outside the release repository, or an older version
  is refused before any download is executed. (3) The forced modal offers „Überspringen" only after
  a failed attempt (today's rule); in managed mode there is no modal. (4) N−2: the station refuses
  robot work in German and every cloud page still works (Playwright with the stub cloud). (5) A
  firmware update is never started while a run, a recording or teleop is active, never to a lower
  build number, never with an image outside the generated allowlist (the fake link records every
  call), and only after the orderly end left the arm limp at rest AND the user confirmed the German
  dialog (spec B: consent and „arm laid down" are the engine's). (6) `test_update_manifest.py`, with
  a key pair generated inside the test: `sign_manifest.py`'s output verifies in `updater.py`; one
  changed byte of the manifest, a signature under another key and an empty signature are refused;
  a text and AST scan of `native/`, `.github/` and `robotis_ai_setup/` finds no private-key material,
  no `secrets.` reference to a signing key, and no private-key load outside `sign_manifest.py` and
  the tests. (7) The publish workflow's verification step, run under bash with stand-ins as
  `test_publish_gui_version.py` does, stops the run for a `manifest_sig` that does not verify,
  before the Railway CLI is installed. (8) The diff of `native/KEYS.md` lies entirely inside the
  reserved section.
- **Failing test** `app/tests/test_version_native_keys.py::test_native_keys_are_additive_and_pinned_to_the_release_repo`
  and `native/tests/test_update_manifest.py::test_one_changed_byte_is_refused`.
- **risk** high · **lenses** correctness, delivery, regression

## C7-03 — iPad as the station's screen: the lan zone
- **Summary.** Bind the lan listener only under the policy; port `pi_agent/pi_auth.py`'s sessions,
  ticket redeem and rate limits into `native/engine/edubotics_station/robot_login.py` WITHOUT claim and setup
  code; apply Appendix 4 B2's lan rules and B4's `auth/*` and `robot-key` routes; answer
  `/status.lan_mode` and `GET /api/system/lan/qr.svg`; the per-machine installer's task writes
  `LanMode = 1` and the inbound firewall rule for `LAN_PORT`; write the German IT sheet
  `docs/NATIVE-IT-NETZWERK.md` (the port, plain HTTP stated, the egress hosts). Run the iPad checklist
  (research/04 §5.2) against the station and record it in `docs/NATIVE-IPAD-CHECKLIST.md`.
- **dependsOn** C7-01 · **externalGates** `IPAD` (an iPad 9 and a USB-C iPad), `SCHOOL-LAN`, `OWNER-POLICY`
- **layers** native-engine, native-packaging, docs
- **files.may** `native/engine/edubotics_station/robot_login.py`, `front.py`, `zones.py`, `system_api.py`,
  `native/packaging/installer/EduBotics.iss` (the task), the lock (one pure-Python QR package, hashed),
  `native/tests/test_robot_login.py`, `test_robot_login_lockstep.py`, `test_front_lan_zone.py`,
  `docs/NATIVE-IT-NETZWERK.md`, `docs/NATIVE-IPAD-CHECKLIST.md`, RC-6's iPad card
- **files.mustNot** `RAS/pi_agent/**`, `MGR/src/features/piAuth/**`, `RAS/cloud_training_api/**`
- **testCommands** T-NATIVE (the three files), T-REACT
- **acceptance** (1) Without the policy the port is closed. (2) With it: no session → 401
  `login_required` on every protected path, an unknown path included; a ticket the cloud accepts sets
  the cookie with the Pi's TTL rule; a refused ticket is `ticket_rejected`; the cloud unreachable is
  503 `cloud_unreachable`. (3) The host cookie is worthless on the lan listener and the robot-session
  cookie on the local one. (4) Maintenance routes (factory reset, scene-camera profile, firmware
  update) need a teacher or an admin on a lan session — conductor's decision Q-C7, as on the Pi
  (SA-12). (5) `/auth/claim` and
  `/auth/setup-code` do not exist. (6) A PC window and an iPad session both receive the same
  `/task/status` stream; the second token push shows the SPA's existing take-over state. (7) Cookie
  name, TTLs and caps equal `pi_auth.py`'s while that file exists (lockstep test). (8) The checklist
  document lists every defect with the screen width; none is fixed here (O2).
- **Failing test** `native/tests/test_front_lan_zone.py::test_the_listener_exists_only_under_the_policy`.
- **risk** high · **lenses** correctness, delivery, regression

## C7-04 — The cloud-host build for iPad Safari (RC-12)
- **Summary.** A third build mode `REACT_APP_MODE=cloud`: the student app with the cloud pages only,
  no Pi detection, no rosbridge. `MGR/Dockerfile.cloud`, `nginx.cloud.conf.template` (the teacher
  web's headers and CSP generator), the CI job `cloud-host-build-validate`, and
  `railway-deploy-cloud-host.yml` (W3's shape, `environment: production`), whose deploy job runs only
  when the repository variable `CLOUD_HOST_SERVICE` is set.
- **dependsOn** C4-08b · **externalGates** `OWNER-CI-EDIT`, `OWNER-RAILWAY-SERVICE` (owner Q-C9: a NEW
  Railway service that the owner creates; no package and no workflow of this spec creates it), `IPAD`
- **layers** 6, CI/CD
- **files.may** RC-12's files, `MGR/Dockerfile.cloud`, `MGR/nginx.cloud.conf.template`,
  `.github/workflows/ci.yml` (the job), `.github/workflows/railway-deploy-cloud-host.yml`,
  `RAS/tests/test_pi_cloud_cors_lockstep.py`, `RAS/tests/test_deploy_approval_gate.py`
- **files.mustNot** `MGR/Dockerfile.web`, `nginx.web.*`, `WebApp.js`, `RAS/cloud_training_api/**`
- **testCommands** T-REACT, T-ESLINT, T-DEPSFREE (the two tests), `docker build -f MGR/Dockerfile.cloud` with placeholders + `nginx -t`
- **acceptance** On a public host the build is not a Pi, shows no hardware tab, opens no WebSocket and
  never calls `/api/system`; a student logs in and reaches Startseite, Training and „Meine Modelle";
  every cloud call it makes is on `_ANY_ORIGIN_BEARER_ROUTES`; the `web` and `student` builds'
  snapshots are unchanged.
- **Failing test** `MGR/src/__tests__/cloudHostMode.test.js::on a public host the cloud build is not a Pi`.
  CONFIRMED red today: `isPi = marker || !isLoopbackPage()`.
- **risk** medium · **lenses** correctness, delivery, regression

## C7-05 — The old product is still on this PC: the upload-first notice (owner Q-C3: no data import)
- **Summary.** There is NO import from the WSL product: no dataset, no calibration and no setting is
  copied (owner, 2026-11-25). A school uploads its datasets to Hugging Face from the old product
  before it removes it, and runs the arm check once more in the native app. This package only makes
  sure the school is TOLD, where it will look.
  `native/engine/edubotics_station/legacy_product.py::detect(read_registry, path_exists)` is pure and
  read-only, and it starts NO process — in particular never `wsl.exe` (on a PC without WSL its stub
  can offer to install WSL, PLAUSIBLE from Windows 11's behaviour and not probed; with WSL it can
  boot the VM). Three signals, each "found / not found / unreadable":
  (1) the WSL product's uninstall entry
  `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}_is1`,
  in the 64-bit and the 32-bit view;
  (2) a subkey of `HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss` whose `DistributionName` is
  `EduBotics` (this account owns a distro);
  (3) the file `%LOCALAPPDATA%\EduBotics\.env` (this account used the old GUI).
  Answer: `present` when any signal is found; `absent` only when all three were readable and not
  found; `unknown` otherwise — an unreadable registry is never reported as "no old product" (the
  three-answer rule of today's `distro_registration`, without the listing).
  `GET /api/system/status` gains the additive key `legacy_product: {"state": …, "acknowledged":
  bool}`; `POST /api/system/legacy-product/ack` (any session) stores `legacy_notice_ack: true` in
  `settings.json`. RC-6 gains ONE card, shown while `state` is not `absent` and not acknowledged:
  title `LEGACY_PRODUCT_TITLE_DE` „Frühere EduBotics-Version auf diesem PC"; body
  `LEGACY_PRODUCT_FOUND_DE` „Auf diesem PC ist noch die frühere EduBotics-Version eingerichtet. Die
  neue App übernimmt von dort keine Datensätze und keine Kalibrierung. Lade deine Datensätze in der
  früheren Version im Tab „Daten" zu Hugging Face hoch, bevor du sie deinstallierst. Den Arm-Check
  machst du in der neuen App einmal neu."; for `unknown` `LEGACY_PRODUCT_MAYBE_DE`, the same text
  opening with „Falls auf diesem PC noch die frühere EduBotics-Version genutzt wurde:"; the button
  „Verstanden". Write the German switch guide `docs/NATIVE-UMSTIEG.md`: upload first; install; the
  arm check again; then uninstall the old product (its uninstaller asks before it deletes its disk).
- **dependsOn** C7-01 (the native installer's own identity and data root exist, so the detector can
  never mistake the native app for the old product) · **externalGates** `SCHOOL-PC` (one PC with the
  WSL product installed: the card appears; one without: it does not)
- **layers** native-engine, 6, docs
- **files.may** `native/engine/edubotics_station/legacy_product.py`, `system_api.py`, `settings.py`,
  `texts_de.py`, `MGR/src/pages/SystemPage.js` (the card) and its test,
  `native/tests/test_legacy_product.py`, `native/tests/test_no_wsl_invocation.py`,
  `native/tests/fixtures/system_api_contract.json` and `native/tests/system_contract_intentional.py`
  (the additive key), `docs/NATIVE-UMSTIEG.md`
- **files.mustNot** `RAS/gui/**`, `RAS/installer/**`, `RAS/cloud_training_api/**`; any code that reads
  a dataset, a volume or a file inside a WSL distro
- **testCommands** T-NATIVE (`tests/test_legacy_product.py tests/test_no_wsl_invocation.py tests/test_system_api_contract.py tests/test_texts_de.py`),
  T-REACT, T-ESLINT
- **acceptance** (1) The table of the three signals' states (27 rows) answers `present` / `absent` /
  `unknown` as defined; the native app's own uninstall entry and its own data root never count.
  (2) `test_no_wsl_invocation.py` (AST and text over `native/engine`, `native/host`, `native/capture`,
  `native/sandbox`, `native/packaging`): no `wsl.exe`, no `--unregister`, no `--export`, no
  `--import`, no subprocess whose program is `wsl`; no registry open with a write mask on a key
  under `Lxss`; no file operation on a path under `%LOCALAPPDATA%\EduBotics\`, `%ProgramData%\EduBotics\`
  or `{autopf}\EduBotics\` other than the one existence check of signal (3). (3) The card is absent
  for `absent`, shown for `present` and `unknown`, gone after „Verstanden" and still gone after a
  restart; on a Pi (`host_kind: pi`, no such key) the System page is unchanged (snapshot). (4) Both
  sentences pass `test_texts_de.py`; neither promises that anything is taken over. (5) The guide
  names every step in German with literal umlauts and says plainly what an OMX dataset is still
  good for: it stays on Hugging Face, and a model trained on it cannot run on a V1 (C5-07).
- **Failing test** `native/tests/test_legacy_product.py::test_an_unreadable_registry_is_never_reported_as_absent`.
- **Decided here** (the conductor left it to this spec): the old product IS detected, by reading
  only; the notice is on the System page and in the guide; there is no installer page (trigger:
  pilot feedback). Spec B's spike S14 (WSL export) is dropped and nothing here waits for it.
- **risk** high (two layers: an additive station key and a React card; read-only) · **lenses** correctness, delivery, regression

## C7-06 — Remove the WSL stack from `main`
- **Summary.** First MOVE what the native app and spec B's tests use, then delete the rest. Every
  move is a WHOLE-FILE `git mv` with no content edit except the import and path lines named here:
  spec B's tests read these files by AST as the reference of the firmware's guards (SPEC-B §8.8),
  so nothing is extracted from, or deleted inside, a moved file in this package.
  - `RAS/docker/open_manipulator/feetech_arm_node.py`, `feetech_bus.py`, `feetech_geometry.py` and
    `activation_agent.py` → `native/engine/edubotics_robot/arm_bridge/v1_driver/` (file names kept;
    the ROS node class stays in `feetech_arm_node.py` as the reference and is never instantiated —
    an AST test says so);
  - the ONE `scene_split.py` → `native/capture/edubotics_capture/scene_split.py` (its twin and the twin
    lockstep test go together);
  - `camera_ingest_node.py` → `native/engine/edubotics_robot/camera_ingest_base.py`;
  - `RUN/` → `native/sandbox/edubotics_sandbox/runner/`;
  - `RAS/gui/app/camera_bridge.py`, `win_camera.py` and the camera constants →
    `native/capture/edubotics_capture/`.
  **Path readers carried in the same commit (SPEC-B §8.8).** `native/protocol/reference_paths.py` —
  spec B's ONE constant module and the only file of spec B this package edits: `DRIVER_NODE`,
  `DRIVER_BUS`, `DRIVER_GEOMETRY` get the new paths (`PROVISION_TOOL` and `CAPTURE_TIMELINE` do not
  move). `tools/feetech_provision.py`: its own `sys.path.insert(… / 'robotis_ai_setup' / 'docker' /
  'open_manipulator')` (CONFIRMED, lines 81–82). `_DOCKER` in `RAS/tests/test_feetech_bus.py` and
  `RAS/tests/test_activation_agent.py` (CONFIRMED). Every other surviving test that reads the
  directory by path (CONFIRMED by grep): `test_feetech_geometry.py`, `test_feetech_tower_check.py`,
  `test_tower_state_topic.py`, `test_retired_robot_types.py`, `test_scene_fixture_lockstep.py`,
  `test_scene_profile_value_table.py`, `test_scene_provenance_lockstep.py`,
  `test_robot_key_file_lockstep.py`, `test_camera_ingest_d21.py`, `test_camera_ingest_studio.py`,
  the roots of `test_no_icon_glyphs.py`, `SRV/test/test_feetech_goal_edge_ceiling.py`,
  `SRV/test/test_studio_camera.py`. And C's own: `native/engine/edubotics_sources.py`,
  `native/tests/tools/make_rail_fixture.py`, `native/packaging/build.ps1`'s stage list. The parts of
  `test_activation_agent.py` that test the entrypoint, the leader launch and the Dockerfile retire
  with those files and are named.
  **Deletes.** `open_manipulator/`, the rest of `RAS/docker/`, `RAS/gui/`, `RAS/installer/` (the licence
  assets move to `native/packaging/assets/`), `RAS/wsl_rootfs/`, `SRV/Dockerfile.*`, `SRV/launch/`,
  `MGR/Dockerfile`, `MGR/Dockerfile.opi`, `MGR/nginx.conf`, `MGR/nginx.opi.conf.template`,
  `docker-publish.yml`, `release-installer.yml`, the `ci.yml` jobs that validate only those files, the
  numpy-1.26.4 server step of `python-tests`, and the tests of deleted things.
  **`RAS/pi_agent/` is decided at the gate, not here** (Q-C12, deferred to `OWNER-GO-REMOVAL`): if the
  owner says delete, the directory and its tests go and `native/tests/test_robot_login_lockstep.py`
  is replaced in this commit by a recorded fixture of `pi_auth.py`'s constants; if he keeps it, the
  directory, its tests and their run in `python-tests` stay untouched and the lockstep test keeps
  reading `pi_auth.py`. Adds `RAS/tests/test_legacy_client_contract.py`.
- **dependsOn** C7-02 (the update channel exists), C7-05 (the upload-first notice and the switch
  guide have shipped: schools were told before anything is removed) · **externalGates** `TRIAL-PASSED`
  (the owner's judgement, C5-10), `OWNER-GO-REMOVAL` (the owner's go; it carries Q-C12 — delete
  `pi_agent/` from `main` or keep it; recommendation: delete, the Pi lives on `release/2.41`),
  `A-RELEASE-241-GREEN` (a 2.41.x tag on `release/2.41` builds without `main`), `OWNER-CI-EDIT`
- **layers** 1, 2, 3, 4, CI/CD, native-*
- **files.may** the trees named above, `native/protocol/reference_paths.py` (the three constants only),
  `tools/feetech_provision.py` (the one `sys.path` line), `native/engine/edubotics_sources.py`,
  `native/tests/tools/make_rail_fixture.py` (its path constant), `native/packaging/build.ps1`,
  `.github/workflows/ci.yml`, `.github/scripts/**` for the deleted jobs, `tools/docker-hub-cleanup.sh`,
  `tools/modal-cleanup.sh` (untouched), `RAS/tests/**`, `SRV/test/**` only for path changes
- **files.mustNot** `RAS/cloud_training_api/**`, `RAS/supabase/**`, `RAS/modal_*/**`,
  `RAS/runpod_training/**`, W1/W2/W3/W6 workflows, `PKG/workflow/scene_fixture.py`, any OMX code inside
  `PKG` (C7-07), `MGR/Dockerfile.web`, every other file of `native/protocol/**`, `native/armlink/**`,
  `native/firmware/**`; the CONTENT of a moved file beyond its import and path lines
- **testCommands** every T-* command; `python native/tests/tools/make_rail_fixture.py --check`;
  `python tools/feetech_provision.py --help`
- **acceptance** (1) `test_legacy_client_contract.py` (JSON fixtures of every request a 2.41.x GUI, Pi
  agent and React build makes to the cloud: `/version`'s old keys, `/pi/tickets`,
  `/pi/tickets/redeem`, the 40 any-origin bearer routes, the OMX trajectory tag) passes before and
  after. (2) Every moved file's tests pass from the new path; the twin lockstep tests are removed
  WITH the twins and named. (3) The commit message lists every deleted test file with "retired with
  <thing>" or its replacement; the count of tests in kept areas (server, cloud, React, native) does
  not fall. (4) `native-build` is the only build on `main` and `app_source_parity.py` passes. (5) No
  commit of this package is applied to `release/2.41`. (6) `git diff -M --stat` shows every moved
  file as a rename; its content diff is only the listed import and path lines. (7) A search for
  `docker/open_manipulator` and for the `os.path.join` spelling `'docker', 'open_manipulator'` over
  `*.py`, `*.sh`, `*.yml`, `*.ps1` finds nothing outside `docs/`. (8) Spec B's
  `test_native_reference_paths.py` and every `test_native_*.py` pass; `tools/feetech_provision.py
  --help` runs (its `feetech_bus` import resolves from the new directory); the rail fixture's
  `--check` passes. (9) No module under `native/` instantiates `FeetechArmNode` or calls the moved
  driver's `main()`.
- **Failing test** `RAS/tests/test_main_tree_shape.py::test_no_wsl_stack_on_main`.
- **risk** high · **lenses** correctness, delivery, regression

## C7-07 — Remove the OMX from the engine and the student build
- **Summary.** Delete the two OMX profiles (the default of `robot_profiles.resolve` becomes
  `v1_studio`; a stored OMX id resolves to it with a `[WARNUNG]`, like the retired types),
  `config/omx_f_config.yaml`, `workflow/ik_solver.py`, the OMX catalog and ladder constants, the
  `'omx_f'` fallbacks of `daten/http_server.py::_rig_robot_type` and `daten/library.py::Library.__init__`
  (they become `v1_studio`; VERIFICATION §6 C12), the OMX
  detector (`safety/collision_detector.py`) and every OMX branch of `collision_monitor.py`
  (`/collision_flag`, gpio, `reboot_dxl`), the OMX rows of the React asset tables and
  `public/omx-urdf/`. **Not removed:** the OMX entries inside the fixture contract block (removing
  them changes `contract_hash()` and would void every kit's arm check); the cloud's `omx_f` trajectory
  tag and every OMX rule of the cloud API (it serves the 2.41 fleet); the legacy tags in
  `DESTINATION_ROBOT_TYPES`.
- **dependsOn** C7-06 · **externalGates** `TRIAL-PASSED`, `OWNER-GO-REMOVAL`
- **layers** 5, 9, 6
- **files.may** `PKG/robot_profiles.py`, `SRV/config/omx_f_config.yaml`, `PKG/workflow/ik_solver.py`,
  `PKG/daten/http_server.py`, `PKG/daten/library.py` (the two defaults),
  `object_catalog.py`, `path_guard.py` (the fallbacks only), `PKG/safety/**`, `MGR/public/omx-urdf/**`,
  `MGR/public/robots/*omx*`, `MGR/src/components/icons/robotAssets.js`, `UrdfTwin.jsx` (`URDF_ASSETS`),
  their tests, `RAS/tests/test_retired_robot_types.py`
- **files.mustNot** `PKG/workflow/scene_fixture.py`, `arm_cells.py`, `RAS/cloud_training_api/**`,
  `MGR/src/WebApp.js` and everything the `web` build alone renders
- **testCommands** T-SERVER, T-DEPSFREE, T-REACT, T-ESLINT, T-CLOUD
- **acceptance** One profile; `EDUBOTICS_ROBOT_TYPE` absent or `omx_full` → `v1_studio` with the
  warning; the server test count change is listed (OMX goldens retired, named); the cloud suite and
  the teacher web's tests are untouched and green; `test/test_scene_fixture.py`'s block sha256 is
  unchanged.
- **Failing test** `SRV/test/test_robot_profiles.py::test_v1_is_the_only_profile_and_the_default`.
- **risk** high · **lenses** correctness, delivery, regression

## C7-08 — Engine step 2a: honest imports
- **Summary.** Delete `native/engine/compat_ros`. Every `import rclpy…`, `from std_msgs…`,
  `from physical_ai_interfaces…` in the server package and the moved native files becomes an import
  from `edubotics_runtime` and `edubotics_msgs` (the same classes). The differential scenarios keep
  importing through `native/tests/differential/ros_names.py`, which resolves to real rclpy in the
  reference image and to the runtime elsewhere. Test stubs (`RAS/tests/ros_stubs.py`, the `_stub`
  helpers) are updated mechanically.
- **dependsOn** C7-07 · **externalGates** —
- **layers** native-engine, 5, 9
- **files.may** `PKG/**` (import lines only), `native/**`, `SRV/test/**` and `RAS/tests/**` (stubs only)
- **files.mustNot** any behaviour: `git diff` outside import lines, stubs and the deleted shim is a defect
- **testCommands** every T-* command, T-DIFF
- **acceptance** No module named `rclpy`, `std_msgs`, … is importable in the app; all transcripts and
  differential properties unchanged; the server test count unchanged.
- **Failing test** `native/tests/test_no_ros_names.py::test_the_app_has_no_module_named_like_ros`.
- **risk** high · **lenses** correctness, delivery, regression

## C7-09 — Engine step 2b: the lane table
- **Summary.** `PKG/lanes.py` holds ONE table: every service, timer, subscription and guard condition
  of the node with its lane (`default`, `record`, `collision`, `preempt`, `hf`, one lane per ticker).
  The node registers through it instead of passing group objects. A test compares the table with
  `native/tests/fixtures/lane_table_step1.json`, the snapshot C4-05 dumped from today's groups by AST.
- **dependsOn** C7-08 · **externalGates** —
- **layers** native-engine, 5, 9
- **files.may** `PKG/lanes.py`, `PKG/physical_ai_server.py`, `communication/communicator.py`,
  `safety/collision_monitor.py`, `daten/node_service.py`, `timer/timer_manager.py`,
  `native/engine/edubotics_runtime/**`, `native/tests/test_lane_table.py`
- **files.mustNot** callback bodies
- **testCommands** every T-* command, T-DIFF
- **acceptance** The lane of every callback equals the snapshot's group; the runtime's scenario
  properties and all transcripts unchanged; R1–R6 scenarios pass against the lane API.
- **Failing test** `native/tests/test_lane_table.py::test_every_callback_keeps_its_lane`.
- **risk** high · **lenses** correctness, delivery, regression

## C7-10 — Engine step 2c: the node file by function
- **Summary.** Split `PKG/physical_ai_server.py` into mixin modules along research/01 §3.4's function
  table (manual control, record tick and commands, cloud inference, HF worker and token, calibration
  glue, simulator glue, workflow glue, boot). The 41 tests that load node methods by path move to one
  helper, `SRV/test/_node_source.py::function_def(name)`, which finds a method across the new files.
- **dependsOn** C7-09 · **externalGates** —
- **layers** 5, 8, 9
- **files.may** `PKG/physical_ai_server.py`, `PKG/node/**` (new), `SRV/test/**`, `RAS/tests/**` (the loaders only),
  `native/tests/tools/make_rail_fixture.py` (its loader of node methods only; `--check` must still pass)
- **files.mustNot** any method body; any constant's value
- **testCommands** every T-* command
- **acceptance** Every method's source text is byte-equal before and after (a script hashes each
  `FunctionDef` segment and compares the multiset); the server test count unchanged; transcripts
  unchanged.
- **Failing test** `SRV/test/test_node_split.py::test_every_method_text_is_unchanged`.
- **risk** high · **lenses** correctness, delivery, regression

## C7-11 — Documentation: CLAUDE.md rewritten for the native product
- **Summary.** Execute Appendix 3 C5 section by section: present tense, symbols not line numbers;
  remove the interim section; correct every entry of the main file's §10 (code-truth corrections);
  write the dated narrative into `docs/CLAUDE-CHANGELOG.md`; rewrite `docs/KNOWN-ISSUES.md`'s living
  status (open rig gates, sandbox residuals, unsigned pilot builds, the lan zone's plain HTTP, what
  `release/2.41` still carries, the licence review as a condition of non-pilot distribution, the
  trial bar being the owner's judgement); fold `docs/NATIVE-ARCHITECTURE.md` into CLAUDE.md and keep
  it as the long form; cite `SPEC-DECISIONS.md`'s answers where a rule rests on one (Q-C3: no data
  import; Q-C4: the update key; Q-C6: no activation rollback; Q-C7: maintenance on a lan session).
- **dependsOn** C7-10 · **externalGates** —
- **layers** docs
- **files.may** `CLAUDE.md`, `docs/CLAUDE-CHANGELOG.md`, `docs/KNOWN-ISSUES.md`, `docs/NATIVE-*.md`, `docs/deploy/*.md`
- **files.mustNot** everything else
- **testCommands** `python .github/scripts/german_detail_lint.py`, T-DEPSFREE (`-p "test_no_icon_glyphs.py"`)
- **acceptance** No sentence of CLAUDE.md names a file that does not exist on `main` (a script greps
  every backticked path); the "Common commands" counts are re-measured on the day and say on which
  interpreter; the ask-first list carries the five new items of Appendix 3 C5.
- **risk** medium (it restates safety rules) · **lenses** correctness, regression
