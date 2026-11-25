# SPEC C — Appendix 5: work packages of Phase 4 (engine step 1) and Phase 5 (recording, Daten, Cloud-Inferenz on the V1)

Belongs to `SPEC-C-native-app.md` (§4 there is the index; §4.1 defines the test command ids
T-NATIVE, T-SERVER, T-DEPSFREE, T-CLOUD, T-REACT, T-ESLINT, T-DIFF, T-WIN and the gate ids). Every
package is one commit. "Fails today" was checked on 2026-11-25 only where it says CONFIRMED; otherwise
the failing test is specified and the coder must run it red before writing the change.

Paths: `SRV` = `physical_ai_tools/physical_ai_server`, `PKG` = `SRV/physical_ai_server`,
`MGR` = `physical_ai_tools/physical_ai_manager`, `RAS` = `robotis_ai_setup`.

**Revision 2 (2026-11-25).** The owner's answers (`SPEC-DECISIONS.md`), spec B §8.1–§8.8 as revised and
spec A's WP-A8 are folded in. New here: C4-0R and C4-05b. Rewritten: C4-00, C4-09, C5-01, C5-08,
C5-10. Touched: C4-01b, C4-04, C4-12, C5-02. The main file's §13 lists every change.

**Revision 3 (2026-11-25, final round).** Owner Q-C13 = (b): C4-00 is rewritten once more — `main`
gets a CLOUD-ONLY release mode instead of a refusal. Spec B's §8.6 (fixture shape confirmed), §8.9
(`heartbeat()` and `setpoint()` never block) and §8.10 (SETPOINT has no stream watchdog) are folded
into C4-0R and C4-09; a `setpoint_lost` latch now ENDS a cloud run (conductor's decision).

---

# Phase 4 — engine step 1

## C4-0R — The rail-message fixture that spec B's driver-parity test reads
- **Summary.** Deliver `native/tests/fixtures/rail_messages_v1.json`: REAL V1 rail messages, that is,
  what today's producers hand to `/leader/joint_trajectory`, produced by running those producers and
  never typed by hand. Spec B's B13 names this file as an external gate
  (`test_native_armlink_traj.py::DriverParity`, SPEC-B §8.6) and reads it; spec B never edits it.
  This package has no dependency and no gate: it runs on today's `main`.
  Write the generator `native/tests/tools/make_rail_fixture.py` (numpy only; no ROS, no shim, no
  LeRobot). It imports `physical_ai_server.workflow.trajectory_builder` and
  `physical_ai_server.workflow.handlers.trajectory` from the checkout (both import and run without ROS
  and without LeRobot — CONFIRMED); it loads the node's `_trajectory_publisher`, `_hold_follower_at_measured_pose` and
  `_run_manual_home_glide` by AST out of `PKG/physical_ai_server.py` (the loader pattern of
  `SRV/test/test_manual_relock_home_glide.py`); `DataConverter.set_action_duration_from_fps` and
  `tensor_array2joint_msgs` by AST out of `PKG/data_processing/data_converter.py` (that module imports
  `cv_bridge` at its top until C4-05); the driver's `build_boot_home` with `HOME_JOINTS_RAD`,
  `GRIPPER_OPEN_RAD`, `LOOP_HZ`, `BOOT_HOME_DURATION_S` by AST out of
  `RAS/docker/open_manipulator/feetech_arm_node.py`. Message classes are plain stand-ins local to the
  tool. No clock and no random number; and no input on which a producer evaluates a transcendental
  function on the way to the message (the manual home glide starts from a pose whose route is
  `direct`), so the output is equal on every platform.
- **The file (schema fixed here; CONFIRMED by spec B, SPEC-B §8.6 "the fixture's shape": B13's reader
  takes exactly this and fails — never skips, never coerces — on any deviation).** A non-empty JSON
  ARRAY, one entry per message in the order the producers emitted them; each entry is an object with
  EXACTLY these five keys:
  - `producer` — string, one of the table below;
  - `joint_names` — list of strings as published;
  - `points` — list of `{"positions": [float, …], "time_from_start": {"sec": int, "nanosec": int}}` in
    published order; `sec` and `nanosec` exactly as `_trajectory_publisher` writes them
    (`sec = int(t)`, `nanosec = int((t − int(t))·1e9)`: a TRUNCATION, so 1/30 s is 33 333 333 ns);
  - `measured_q_at_receipt` — six finite radians in the order `joint1 … joint5, RL_joint`: the
    follower pose at the moment the message is received (the motion's start pose for its first
    chunk, the previous chunk's last point after that);
  - `torque_on` — bool.

  Velocities are not in the file (the driver ignores them). One entry per line
  (`json.dumps(entry, separators=(',', ':'))`), the array brackets on lines of their own. Beside it
  `rail_messages_v1.meta.json`: `{"v": 1, "generator": "make_rail_fixture.py", "producers": {name:
  count}, "sources": {repo path: sha256 of the file a producer was loaded from}}`; spec B's reader
  does not need it.
- **Producers** (each entry comes from the real function named; the inputs are literals in the tool):

  | `producer` | Real code driven | What it contributes |
  |---|---|---|
  | `workflow_chunk` | `build_segment` → `chunked_publish` → `_trajectory_publisher`, joint names = the V1's `total_joint_order` | 30-point chunks, first point at 0.033333333 s, last at 1.0 s, times re-based per chunk (CONFIRMED by my probe) |
  | `workflow_short` | the same for a move shorter than one chunk | one message with fewer than 30 points |
  | `replay_lead_in`, `replay_chunk` | `handlers/trajectory.resegment_trajectory` on a 25 fps Contract-B take built in the tool, then `chunked_publish` with exactly the arguments `handlers/trajectory.replay_trajectory` passes | the quintic lead-in and the linearly joined take, chunked as a real replay is (the function runs here: a 40-point take with a lead-in gives 84 waypoints, the first at 0.0333 s — CONFIRMED; the chunk sizes are whatever the real call produces, not a number of this spec) |
  | `manual_home_glide` | `_run_manual_home_glide` (AST, a fake `self` as in `test_manual_relock_home_glide.py`) from a pose with a direct route | the chunks of a leg stretched to `_MANUAL_HOME_PEAK_RAD_S` / `_MANUAL_HOME_MIN_DURATION_S` |
  | `hold_measured` | `_hold_follower_at_measured_pose` | ONE point at `_TORQUE_ON_HOLD_TIME_FROM_START_S` (0.05 s): the stop of an aborted glide, sent on the V1 too |
  | `inference_point` | `tensor_array2joint_msgs` at fps 30 and fps 15 | ONE point at `int(1.5e9 / fps)` ns: 0.05 s and 0.1 s (0.05 s CONFIRMED) |
  | `activation_home` | the driver's `build_boot_home` from a limp pose, handed to `_trajectory_publisher` — what C4-09's `home.py` will send | 150 points, 0.02 s … 3.0 s, smallest step 19 999 999 ns after the truncation (CONFIRMED): more than one 60-point board frame |
  | `workflow_chunk_while_limp` | one `workflow_chunk` entry with `torque_on: false` | the driver drops it and keeps what runs |

- **dependsOn** — · **externalGates** — (this file IS the external gate of spec B's B13)
- **layers** tests (native); the seam with spec B
- **files.may** `native/tests/tools/make_rail_fixture.py`, `native/tests/fixtures/rail_messages_v1.json`,
  `native/tests/fixtures/rail_messages_v1.meta.json`, `RAS/tests/test_native_rail_fixture.py`
- **files.mustNot** everything else; in particular `PKG/**`, `RAS/docker/**`, `native/armlink/**`,
  `native/protocol/**` (spec B's)
- **testCommands** T-DEPSFREE (`-p "test_native_rail_fixture.py"`);
  `python native/tests/tools/make_rail_fixture.py --check`
- **acceptance** (1) `--check` regenerates every entry in memory and finds it equal to the file
  (floats compared exactly); `--write` is the only way the file changes. (2) Every entry has exactly
  the five keys, six finite `measured_q_at_receipt` values, an integer `sec` ≥ 0 and
  `0 ≤ nanosec < 10⁹`. (3) Every producer of the table occurs at least once; at least one message
  has more than 60 points, at least two have exactly one, and at least one has `torque_on: false`
  (the three kinds spec B's reader requires and names when one is missing); `torque_on` is a JSON
  boolean, `sec` and `nanosec` are JSON integers. (4) The driver's own `_trajectory_cb`,
  extracted by AST and run on each entry with `_torque_on = entry.torque_on` and `_last_positions =
  measured_q_at_receipt`, installs a trajectory for every `torque_on: true` entry and none for the
  `false` one. (5) The two `.py` files carry the EduBotics header.
- **Failing test** `RAS/tests/test_native_rail_fixture.py::test_the_fixture_is_what_the_producers_emit`
  (a `unittest` in spec B's naming, so today's `python-tests` job runs it with no CI edit). Red today:
  the file is absent. Feasibility CONFIRMED: `scratch/architect-C/probes/rail_fixture_probe.py`
  produced six such entries from the real `trajectory_builder`, the AST-loaded `_trajectory_publisher`
  and the driver's `build_boot_home`, with exactly the five keys.
- **Later owners of the tool's paths.** C7-06 (the driver file moves) and C7-10 (the node file is
  split) update the tool's path constants in their own commits and re-run `--check`.
- **risk** medium (test data another spec's proof depends on; no product code) · **lenses** correctness, delivery

## C4-00 — `main` stops building the WSL product; a tag on `main` is a cloud-only release (owner Q-C1, Q-C13 = b)
- **Summary.** Starts only after spec A's WP-A8 is merged and `release/2.41` is cut (SPEC-A §12, steps
  8 and 11). From then on a WSL fix is a PR into `release/2.41`, and a 2.41.x tag there runs THAT
  branch's `release.yml` in maintenance mode. On `main` a tag becomes a CLOUD-ONLY release: it runs
  W1 (`supabase`), W2a (`trainer-image`), W2b (`trainer-image-var`), W2 (`cloud-api`) and W3
  (`teacher-web`) in today's order and builds NO client: W4 (`docker`), W5 (`installer`) and W5b
  (`pi-agent-tarball`) are skipped. The owner chose this over a refusal (Q-C13 = b, which pre-approves
  exactly this CI edit) so that the trainer image keeps a release path: a handler or lock edit
  still ships only with a tag, which rebuilds the trainer and redeploys the API together (CLAUDE.md
  Rule §3's coupling, unchanged).
- **The edits — beside WP-A8's lines, never inside them.**
  1. `.github/workflows/docker-publish.yml`: remove the whole `push:` trigger (today `branches:
     [main]` with the paths `robotis_ai_setup/docker/**`, `open_manipulator/**`,
     `physical_ai_tools/**`, `tools/**` — CONFIRMED); keep `workflow_call` and `workflow_dispatch`.
     WP-A8 does not touch this file.
  2. New file `.github/release-mode`, one line: `cloud-only`. This committed marker is the ONLY
     thing that says what a release from `main` builds. C7-01 changes it to `native`.
  3. `VERSION` on `main` becomes `3.0.0`, with the three fallback literals (the four-site rule,
     unchanged). Every cloud-only release is the next `3.0.N`. `release/2.41` stays on `2.41.x`, so
     the two tag lines can never meet.
  4. `.github/workflows/release.yml`, job `version-preflight`:
     - one new line under `outputs:` — `client_build: ${{ steps.product.outputs.client_build }}`
       (always `'true'` or `'false'` when the job succeeds);
     - ONE new step directly after WP-A8's step `id: mode` and before the channel check:
     ```yaml
           - name: Decide what this release builds (clients, or the cloud only)
             id: product
             env:
               MAINTENANCE: ${{ steps.mode.outputs.maintenance }}
               FILE_VERSION: ${{ steps.version.outputs.version }}
             run: |
               set -euo pipefail
               case "$MAINTENANCE" in
                 true)
                   echo "client_build=true" >> "$GITHUB_OUTPUT"
                   echo "Release product: clients (maintenance release from a release/* branch)."
                   exit 0 ;;
                 false) ;;
                 *)
                   echo "::error::The release mode is '${MAINTENANCE}', neither true nor false: nothing is released."
                   exit 1 ;;
               esac
               MARKER=.github/release-mode
               if [ ! -f "$MARKER" ]; then
                 echo "::error::${MARKER} is missing. What a release from main builds is never guessed: nothing is released."
                 exit 1
               fi
               MODE="$(tr -d '[:space:]' < "$MARKER")"
               if [ "$MODE" != "cloud-only" ]; then
                 echo "::error::${MARKER} holds '${MODE}'. This release.yml knows only 'cloud-only' (spec C, C4-00); nothing is released."
                 exit 1
               fi
               MAJOR="${FILE_VERSION%%.*}"
               if ! [ "$MAJOR" -ge 3 ] 2>/dev/null; then
                 echo "::error::A cloud-only release from main needs VERSION 3.0.0 or higher (found ${FILE_VERSION}): v2.x tags belong to release/2.41."
                 exit 1
               fi
               echo "client_build=false" >> "$GITHUB_OUTPUT"
               echo "Release product: cloud only (database, trainer image, cloud API, teacher web). No client is built."
     ```
     No `if:`, no `continue-on-error`, no `${{ }}` inside `run`.
  5. `release.yml`, job `docker`: ONE line appended at the end of its `if:` block, after the five
     lines WP-A8 wrote (which stay byte for byte, as do its `needs`):
     `&& needs.version-preflight.outputs.client_build == 'true'`. `installer` and `pi-agent-tarball`
     are not edited: they need `docker`, then `installer`, to have SUCCEEDED, so they skip with it.
     `next-step` is not edited either and is skipped with `installer`.
  6. `release.yml`, ONE new job `next-step-cloud` (so no line of WP-A8's `next-step` is touched):
     `needs: [version-preflight, teacher-web]`, `if: always() && needs.version-preflight.result ==
     'success' && needs.version-preflight.outputs.client_build == 'false' && needs.teacher-web.result
     == 'success'`, `permissions: {}`, `timeout-minutes: 2`, one step that writes the run summary:
     the tag is a cloud-only release from `main`; the database, the trainer image and its Railway
     variable, the cloud API and the teacher web were released; no Docker image, no installer and no
     Pi tarball was built and there is no GitHub Release for the tag; nothing is advertised to any
     PC or Pi, and `publish-gui-version` (W6) must NOT be run for it.
  Nothing else in `release.yml` changes. The cloud jobs need no edit at all: WP-A8's gates
  (`maintenance != 'true'`) already let them run for a commit on `main`.
- **How the mode is decided and switched off.** Decided by the committed marker, read by the step
  above; never by a missing value: a missing or unknown marker, or a `maintenance` output that is
  neither `true` nor `false`, FAILS the preflight and nothing runs. Fail closed twice over: the
  client jobs are gated with `== 'true'`, so an output that is somehow empty builds no client
  (WP-A8's own direction is kept for the cloud jobs, `!= 'true'`: a production deploy is never
  skipped by a missing value). Switched off by C7-01, which sets the marker to `native`, makes the
  step accept exactly `native` (→ `client_build=true`), replaces the three client jobs by
  `native-build` and deletes `next-step-cloud`.
- **Version numbering and W6.** A cloud-only tag is `v3.0.N` (`N` counts cloud-only releases); the
  step refuses a `2.x` VERSION, so it cannot collide with `release/2.41`'s `v2.41.x`. It creates no
  GitHub Release (only W5 does), so W6 can never advertise it: W6 is not run for it (the runbook
  says so), and if it is started by mistake it stops before it changes anything — its first proof
  is that the release `v<version>` exists with both assets (`test_publish_gui_version.py`'s case
  „no such release", CONFIRMED). The fleet's advertised version stays whatever the last W6 set.
  Trainer images are tagged `edubotics-trainer:3.0.N`.
- **The conflict surface with WP-A8, named.** Shared FILE `release.yml`: the `outputs:` block of
  `version-preflight` (one added line), the step list (one step after `mode`), `docker`'s `if:` (one
  appended line), one new job. No line WP-A8 wrote is edited: its `fetch-depth: 0`,
  `outputs.maintenance`, the `mode` step, the `if:` of `supabase`, `trainer-image`,
  `trainer-image-var`, `cloud-api`, `docker`'s first five lines and `needs`, and `next-step`. Shared
  TEST file `RAS/tests/test_deploy_approval_gate.py`: WP-A8's functions stay; WP-A8's two `main`
  ARRIVALS („a release tag", „a dispatched release", which expect every job to run) are replaced by
  the cloud-only arrivals below — that is the behaviour the owner changed — and every arrival gains
  the stated output `needs.version-preflight.outputs.client_build` (the evaluator refuses an
  unstated name). A tag on `release/2.41` runs the `release.yml` of its OWN commit, which never has
  these edits.
- **What stops on `main` with this package** (stated so nobody finds out later):
  (a) `:latest` freezes at the last build before this commit (the fleet pins `IMAGE_TAG`).
  (b) `docker-publish.yml::smoke-test` no longer runs for `main`: the LeRobot-only server tests and
  the recorder, Daten and code smokes inside the ROS images. The LeRobot-only tests return with
  C4-01's proof runs, the recorder and Daten smokes with C4-05b, the code smoke with C6-04 (main
  file §12.1, K13).
  (c) A hand dispatch of `docker-publish.yml` or `release-installer.yml` on `main` would still build
  from `main`'s frozen WSL sources; the runbook forbids it (they are dispatched on `release/2.41`).
  (d) The channel check of `version-preflight` still runs for a cloud-only tag (it is not edited):
  a cloud-only release needs a usable release channel although it uploads nothing.
  Unchanged: W2 and W3 on a merge by path, W1 by the owner's button, W6 dispatched from `main` for
  2.41.x releases.
- **dependsOn** — · **externalGates** `A-BRANCH-CUT` (2.41.1 shipped, `release/2.41` exists), `A-WP-A8`
  (WP-A8 is merged on `main`, hence in `v2.41.1` and on the branch). The CI edit is approved: owner
  Q-C1 (stop building the WSL product) and Q-C13 = (b) (the cloud-only mode), for exactly the edits
  above; anything more is a new ask.
- **layers** CI/CD, docs (the runbook)
- **files.may** `.github/workflows/docker-publish.yml`, `.github/workflows/release.yml` (the output
  line, the step, the appended line, the new job), `.github/release-mode` (new), `VERSION`,
  `RAS/installer/robotis_ai_setup.iss` (`AppVersion` only), `RAS/gui/app/constants.py` and
  `RAS/pi_agent/constants.py` (the fallback literal only), `RAS/tests/test_deploy_approval_gate.py`,
  `RAS/tests/test_main_cloud_only_release.py` (new), `RAS/tests/test_ci_supply_chain_pins.py` only
  where a shape it pins moves, `docs/deploy/DEPLOY.md` (one section: „A cloud-only release from
  main"), `CLAUDE.md` (Rule §6: one paragraph)
- **files.mustNot** `supabase-migrate.yml`, `railway-deploy-*.yml`, `publish-gui-version.yml`,
  `trainer-image.yml`, `release-installer.yml`, `ci.yml`, `.github/scripts/refuse_rollback.sh`,
  `.github/zizmor.yml`, every line of `release.yml` that WP-A8 wrote, the steps „Read the VERSION
  file", „Assert the pushed tag matches the VERSION file" and the channel check, anything else
- **Test changes in `test_deploy_approval_gate.py`** (no existing assertion is weakened):
  - `_arrivals()['release.yml']` becomes (label, context, must_run, must_skip), each context stating
    `maintenance` and `client_build`:
    „a cloud-only tag on main" (`push`, `refs/tags/v3.0.1`, `'false'`, `'false'`) and „a dispatched
    cloud-only release" (`workflow_dispatch`, `refs/heads/main`, `'false'`, `'false'`): must_run
    `{version-preflight, supabase, trainer-image, trainer-image-var, cloud-api, teacher-web,
    next-step-cloud}`, must_skip `{docker, installer, pi-agent-tarball, next-step}`;
    WP-A8's „a maintenance tag" with `client_build: 'true'`: its must_run and must_skip as WP-A8
    wrote them, plus `next-step-cloud` in must_skip;
    „a main tag whose product output is empty" (`'false'`, `''`): must_run the five cloud jobs and
    `version-preflight`, must_skip `{docker, installer, pi-agent-tarball, next-step, next-step-cloud}`
    — the job graph itself is closed even where the preflight's own failure is not modelled.
  - `release_call_problems` keeps proving, for the two cloud-only arrivals, that W1 is called with
    no input that skips `apply-production`.
  - `test_the_review_mutations_turn_it_red` gains four mutations, each with a unique anchor and
    each turning `workflow_problems()` non-empty: „W4 runs in a cloud-only release" (remove the
    appended line of `docker`'s `if:`); „clients on an empty mode" (`client_build == 'true'` →
    `client_build != 'false'` in `docker`'s `if:`); „the cloud half skipped" (add
    `&& needs.version-preflight.outputs.client_build == 'true'` to `cloud-api`'s `if:`); „no
    cloud-only summary" (`== 'false'` → `== 'true'` in `next-step-cloud`'s `if:`). WP-A8's own
    mutations keep their anchors (the five lines of `docker`'s `if:` are untouched) and stay red.
  - New `test_a_cloud_only_release_cannot_run_w4_w5_w5b`: for `client_build` in `'false'`, `''`,
    `'TRUE'`, `'yes'`, `'cloud-only'` and for both events, with `maintenance: 'false'`, the simulation
    answers `skipped` for `docker`, `installer` and `pi-agent-tarball`; and structurally `docker`'s
    `if:` contains `needs.version-preflight.outputs.client_build == 'true'`, `installer` still
    requires `needs.docker.result == 'success'` and `pi-agent-tarball` `needs.installer.result ==
    'success'` (there is no second way in).
  - New executed class `ReleaseProductIsDecidedByTheMarker` (modelled on WP-A8's
    `ReleaseModeIsDecidedByAncestry`: the step's `run` extracted with `yaml.safe_load`, run under
    `bash -c` in a temporary directory with a temporary `GITHUB_OUTPUT`): `MAINTENANCE=true` →
    rc 0, exactly `client_build=true`, whatever the marker; `false` + marker `cloud-only` +
    `FILE_VERSION=3.0.1` → rc 0, exactly `client_build=false`; marker absent, empty, `native`,
    `cloud-only` followed by a second line, or `Cloud-Only` → rc 1, one `::error::` line, no output
    line; `MAINTENANCE` empty or `maybe` → rc 1; `FILE_VERSION=2.41.5` or `x.0.0` with the marker →
    rc 1. Structure: the step's `id` is `product`, it follows the step with `id: mode` directly, has
    no `if:` and no `continue-on-error`, and `outputs.client_build` is
    `${{ steps.product.outputs.client_build }}`.
- **testCommands** T-DEPSFREE (`-p "test_deploy_approval_gate.py"`, `-p "test_main_cloud_only_release.py"`,
  `-p "test_ci_supply_chain_pins.py"`, `-p "test_publish_gui_version.py"`, `-p "test_refuse_rollback.py"`),
  `zizmor --offline --no-progress --min-severity low .github/workflows/`,
  `python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" .github/workflows/release.yml .github/workflows/docker-publish.yml`
- **acceptance** (1) `docker-publish.yml` has no `push` key under `on:`. (2) In the simulation a tag
  and a dispatch on `main` run exactly `version-preflight`, `supabase`, `trainer-image`,
  `trainer-image-var`, `cloud-api`, `teacher-web`, `next-step-cloud` and skip `docker`, `installer`,
  `pi-agent-tarball`, `next-step`; a maintenance tag behaves exactly as WP-A8 specified.
  (3) `ProductionJobsCannotBeSkippedSilently`, `ReleaseModeIsDecidedByAncestry`,
  `test_every_cloud_job_names_the_maintenance_gate`, `CiRunsOnReleaseBranches`, `MigrationsAreManual`
  and `W6IsManual` pass; every review mutation, WP-A8's and the four new ones, stays red.
  (4) `test_main_cloud_only_release.py`: the marker file holds exactly `cloud-only`; `VERSION` and
  the three literals are equal and their major is at least 3; no job of `release.yml` other than
  `installer` and `pi-agent-tarball` (and the workflow `installer` calls) uses
  `softprops/action-gh-release`; `docs/deploy/DEPLOY.md` says that W6 is not run
  for a cloud-only tag and that a trainer handler or lock edit ships with such a tag. (5)
  `git diff` of `release.yml` against WP-A8's state shows only added lines. (6) zizmor reports no
  finding.
- **Failing tests** `ProductionJobsCannotBeSkippedSilently.test_no_production_run_is_skipped_softened_or_redirected`
  with the cloud-only arrivals (red on WP-A8's `release.yml`: `docker`, `installer` and
  `pi-agent-tarball` run for a `main` tag — that is today's behaviour, CONFIRMED by reading the
  job `if:`s), `test_a_cloud_only_release_cannot_run_w4_w5_w5b`,
  `ReleaseProductIsDecidedByTheMarker` (no such step), and
  `test_main_cloud_only_release.py::test_docker_publish_has_no_push_trigger` (CONFIRMED red today).
- **risk** high (the pipeline that writes production; a third release mode) · **lenses** correctness, delivery, regression

## C4-01 — The native Python stack: one hash lock, the proof workflow, and the server suite green on it
- **Summary.** Add the engine's dependency set as `native/packaging/requirements-engine.in` and its
  hash locks (`requirements-engine.win.lock` for cp313 `x86_64-pc-windows-msvc`,
  `requirements-engine.linux.lock` for CI), written by `.github/scripts/python_locks.sh write`. Start
  from the versions of spec B's S7 spike lock (`native/spikes/windows/stack/spike-lock-cp313-win.txt`,
  spike-only) when its record exists; the product lock is this package's. Write the proof workflow as
  a FILE THAT IS NOT A WORKFLOW ON `main`: `native/ci/native-proof.yml` (trigger exactly
  `push: branches: ["spike/c-**"]`; jobs `native-linux` on ubuntu and `native-windows` on
  `windows-2025`, each installing ONLY the lock with `--require-hashes --only-binary :all:` on Python
  3.13 and running `SRV/test` and `native/tests`; the Windows job runs the tests as a standard user
  through spec B's `native/spikes/windows/run_as_standard_user.ps1`). Add the bridge test
  `RAS/tests/test_native_c_suite.py`, which runs `python -m pytest native/tests -m depsfree` in a
  child process, so today's `python-tests` job proves the dependency-free part of every later package
  with NO edit of `ci.yml`. Add the registry `native/tests/platform_skips.json` (test id → reason →
  the package that closes it) and make `SRV/test/conftest.py` fail the run when the set of tests
  skipped for the platform differs from the registry. Run
  `test_tower_guard.py::test_runtime_budget_here` on Windows report-only.
- **dependsOn** C4-00 · **externalGates** `B-S7`, `B-S8` (informs, does not block), `B-STDUSER-HARNESS`, `SPIKE-RUN`
- **layers** native-packaging, 5 (recording stack pins), tests
- **files.may** `native/packaging/requirements-engine.in`, `native/packaging/requirements-engine.*.lock`,
  `.github/scripts/python_locks.sh`, `native/ci/native-proof.yml`, `native/ci/README.md`,
  `native/ci/results/**`, `native/tests/conftest.py`, `native/tests/test_stack_lock.py`,
  `native/tests/test_native_proof_workflow.py`, `native/tests/platform_skips.json`,
  `SRV/test/conftest.py`, `RAS/tests/test_native_c_suite.py`, `RAS/tests/test_lerobot_pin_lockstep.py`,
  `RAS/tests/test_python_hash_locks.py`, `RAS/tests/test_ci_supply_chain_pins.py`
- **files.mustNot** `.github/workflows/**` (nothing under it), `PKG/**` (no source change here),
  `RAS/cloud_training_api/**`, `RAS/modal_*/**`, `native/spikes/**` (spec B's)
- **testCommands** T-NATIVE (`tests/test_stack_lock.py tests/test_native_proof_workflow.py`), T-SERVER on
  the native lock, T-DEPSFREE (`-p "test_native_c_suite.py"`, `-p "test_lerobot_pin_lockstep.py"`,
  `-p "test_python_hash_locks.py"`), `bash .github/scripts/python_locks.sh check`, T-WIN
- **acceptance** (1) The lock pins `lerobot==0.5.1` (no extras), a numpy 2.x inside LeRobot's
  `>=2.0,<2.3`, `torch==2.7.0` (CPU), no `torchcodec`, no `rerun-sdk`, `wandb`, `cmake`,
  `imageio-ffmpeg`. (2) `test_lerobot_pin_lockstep.py` reads the new site. (3) On ubuntu the server
  suite on the lock has 0 failures; every skip is in the registry or is an existing `importorskip`.
  (4) On `windows-2025` every failure is a registry entry with `closes_by` (expected today:
  `data_processing/hub_sync` and its importers → C5-05, AF_UNIX code-RPC tests → C6-02). (5) No test
  file is edited to skip. (6) `test_native_proof_workflow.py` holds the workflow file to the rules
  spec B set for its spike workflow: trigger exactly the spike branches, `permissions: {contents: read}`,
  every `uses:` pinned to the SHA the repo already pins, `persist-credentials: false`, no `secrets.`,
  no `${{` inside `run:`, a timeout on every job. (7) The run's summary JSON is copied to
  `native/ci/results/C4-01/` by the build session.
- **Failing test** `native/tests/test_stack_lock.py::test_every_entry_is_hashed_and_the_recorder_closure_imports`
  (imports `torch`, `av`, `pyarrow`, `cv2`, `pupil_apriltags`, `lerobot.datasets.lerobot_dataset`,
  `aiohttp`, `cryptography`; asserts `av.codec.Codec('libx264','w')`). Red today: the file and lock do
  not exist.
- **Evidence I have.** On this host the server suite on Python 3.12.13 / numpy 2.2.6 / OpenCV 4.13.0
  ran 4 217 passed, 53 failed, 13 skipped, 14 errors; **none of the failures is a numpy-2 or OpenCV
  result** (28 × missing `cryptography`, 15 × the host's LeRobot is 0.5.2 not 0.5.1, 20 × the owner's
  uncommitted `test_autocal_service.py` work) — CONFIRMED, so numpy 2 is PLAUSIBLY clean; the proof
  run on the real lock decides.
- **risk** high · **lenses** correctness, delivery, regression

## C4-01b — The proof workflow runs on `main`
- **Summary.** Not now: Q-C8 is decided (conductor, 2026-11-25, the same as spec B's Q-B1) as "no
  `ci.yml` change now, proof runs on `spike/c-*` branches"; the owner is asked again before C4-12
  (gate `OWNER-CI-REASK` there). Only after a yes: add `.github/workflows/native-proof.yml`
  (`on: workflow_call`, otherwise a byte-equal copy of `native/ci/native-proof.yml`'s jobs) and ONE
  job in `ci.yml`, `native-proof`, that calls it. From then on every package that changes
  `native/ci/native-proof.yml` changes the copy in the same commit (a test compares them).
- **dependsOn** C4-01 · **externalGates** `OWNER-CI-EDIT`
- **layers** CI/CD
- **files.may** `.github/workflows/native-proof.yml`, `.github/workflows/ci.yml` (the one job),
  `RAS/tests/test_native_proof_copy.py`, `RAS/tests/test_ci_supply_chain_pins.py`
- **files.mustNot** every other workflow; every other job of `ci.yml`
- **testCommands** T-DEPSFREE (`-p "test_native_proof_copy.py"`), `zizmor --min-severity low .github/workflows`
- **acceptance** The two files' `jobs:` blocks are byte-equal; `ci.yml` gained exactly one job; a pull
  request to `main` runs `native-linux` and `native-windows`; no secret is referenced.
- **Failing test** `RAS/tests/test_native_proof_copy.py::test_the_workflow_on_main_equals_its_source`.
- **risk** high · **lenses** correctness, delivery, regression

## C4-02 — Remove what the owner removed
- **Summary.** Delete, in the server package, the interfaces and React: rosbag recording
  (`Communicator`'s rosbag client, probe and five methods, `handle_rosbag_recording`,
  `RosbagNotReadyException`, the rosbag path in `data_manager.py`); local GPU inference
  (`inference/inference_manager.py`, the START_INFERENCE branch of `user_interaction_callback`,
  `_eager_load_policy`, `_inference_timer_callback`, `_inference_policy_loading`); the services
  `/training/command`, `/training/get_training_info`, `/browse_file`, `/dataset/get_info`,
  `/get_saved_policies`, `/get_policy_list`, the old `/dataset/edit` and the topic `/training/status`;
  the seven unserved calibration `.srv` files plus the `.srv`/`.msg` files only those services used; the
  `download` mode of `/huggingface/control` (it answers one German refusal) and MyModels' robot
  download (RC-11); the OMX joystick subscription and `handle_joystick_trigger`. Keep, untouched: the
  `'inference'` timer name, `operation_mode`, `on_inference`, `_preflight_contract`,
  `PHASE_INFERENCE_LOADING`, `set_action_duration_from_fps`, `TaskInfo.record_rosbag2`, the Training
  page's four robot-side list services, `chunk_engine.py` (it also ships to Modal).
- **dependsOn** C4-01 · **externalGates** —
- **layers** 5, 8, 6, interfaces
- **files.may** `PKG/physical_ai_server.py`, `PKG/communication/communicator.py`,
  `PKG/data_processing/data_manager.py`, `PKG/inference/inference_manager.py` (delete),
  `PKG/training/**` (only what the removed services used), `physical_ai_tools/physical_ai_interfaces/**`,
  `MGR/src/components/MyModels.js`, `MGR/src/hooks/useRosServiceCaller.js`,
  `MGR/src/hooks/useRosTopicSubscription.js`, their tests, `SRV/test/**` and `RAS/tests/**` for the
  removed surfaces (each deleted test named in the commit message with its reason),
  `native/tests/test_wire_inventory.py` (new), `MGR/src/__tests__/wireInventory.test.js` (new),
  `.github/scripts/enum_parity.py` (only if a removed type is named there)
- **files.mustNot** `PKG/inference/chunk_engine.py`, `PKG/inference/cloud_*.py`,
  `physical_ai_interfaces/msg/TaskInfo.msg`, `PKG/safety/**`, `PKG/workflow/**`, `PKG/daten/**`,
  every `RAS/docker/**` file (the frozen WSL files are deleted only by C7-06)
- **testCommands** T-SERVER, T-DEPSFREE, T-REACT, T-ESLINT, `python .github/scripts/enum_parity.py`,
  T-NATIVE (`tests/test_wire_inventory.py`)
- **acceptance** (1) `test_wire_inventory.py`: the set of service and topic names the package serves
  (AST over `_init_ros_service`, `Communicator.init_services`, `DatenService._wire_ros`, every
  `create_publisher`) equals the set React names (resolved from `MGR/src`, non-test) plus the arm-side
  six plus an allowlist that holds exactly `/huggingface/control` (kept, refuses download) and
  `/leader/joint_trajectory`. (2) `grep -rn "rosbag\|InferenceManager" PKG` finds only the comment that
  explains `record_rosbag2`. (3) `import physical_ai_server.physical_ai_server` no longer imports
  `torch` or `lerobot.policies` at module scope (asserted with `sys.modules` in a child interpreter).
  (4) The server test count is reported before and after; every removed test is listed with the
  removed surface it tested.
- **Failing test** `native/tests/test_wire_inventory.py::test_served_equals_used`. Red today: five
  served services have no React literal (`/get_policy_list`, `/get_saved_policies`, `/browse_file`,
  `/dataset/edit`, `/dataset/get_info`), two more are named only in hook functions no component calls,
  and `/training/status` is subscribed but never published (CONFIRMED: my scan found 31 service names
  and 20 topics in React; the node serves 35 endpoints).
- **risk** high · **lenses** correctness, delivery, regression

## C4-03 — Messages generated from the IDL text at import
- **Summary.** Write `native/engine/edubotics_idl/` (`parser.py`: the grammar the repo uses — primitives,
  unbounded arrays, nested types, constants, one default; `build.py`: classes with rclpy's attribute
  behaviour, strict JSON ⇄ message conversion with rosbridge's error texts) and the shim packages
  `native/engine/compat_ros/{std_msgs,std_srvs,sensor_msgs,trajectory_msgs,builtin_interfaces,geometry_msgs,nav_msgs,physical_ai_interfaces}`
  that build their classes at import from `physical_ai_tools/physical_ai_interfaces/{msg,srv}` and from
  the standard definitions copied as text into `native/engine/edubotics_idl/std/`. Nothing generated is
  checked in.
- **dependsOn** C4-02 · **externalGates** —
- **layers** native-engine, interfaces
- **files.may** `native/engine/edubotics_idl/**`, `native/engine/compat_ros/**` (not `rclpy/`),
  `native/tests/test_idl.py`, `native/tests/differential/scenarios/r20_message_field_types.py`,
  `native/tests/fixtures/rclpy_reference/r20_*.json`
- **files.mustNot** `physical_ai_tools/physical_ai_interfaces/**`, `PKG/**`
- **testCommands** T-NATIVE (`tests/test_idl.py`), `python .github/scripts/enum_parity.py`, T-DIFF (r20)
- **acceptance** (1) Every `.msg`/`.srv` in the interfaces package and every standard type of
  Appendix 1 A1 builds from `{}`. (2) `TaskInfo().private_mode is True`. (3) The wire constants equal
  what `enum_parity.py` reads from the text. (4) For the fields the package reads
  (`JointState.position/velocity/effort`, `CompressedImage.data`, `JointTrajectoryPoint.positions`),
  the Python type after assignment from a list, a tuple, a numpy array and `bytes` equals what real
  rclpy stores (scenario r20, recorded in the reference image). (5) JSON → message refuses a wrong
  type and an unknown field with the two texts of Appendix 1 A5 rows 3 and 4.
- **Failing test** `native/tests/test_idl.py::test_every_type_builds_from_empty` (red: no package).
- **risk** medium · **lenses** correctness, regression

## C4-04 — The compatibility runtime and its differential proof
- **Summary.** Write `native/engine/edubotics_runtime/` (context, node, callback groups, single- and
  multi-threaded executors, timer, guard condition, publisher/subscription bus with QoS depth and
  latching, service/client with futures, clock, logger, parameters, the shutdown control channel) and
  the shim `native/engine/compat_ros/rclpy/`. Write the scenario scripts r01 … r19 of Appendix 1 A3, the
  recorder `native/tests/differential/record_reference.sh`, the checked-in reference fixtures, and the
  job `compat-differential` in `native/ci/native-proof.yml`.
- **dependsOn** C4-03 · **externalGates** `B-S11` (Windows timer primitive), `SPIKE-RUN`
- **layers** native-engine, CI/CD
- **files.may** `native/engine/edubotics_runtime/**`, `native/engine/compat_ros/rclpy/**`,
  `native/tests/differential/**`, `native/tests/fixtures/rclpy_reference/**`,
  `native/tests/test_differential.py`, `native/tests/test_runtime_surface.py`,
  `native/tests/test_compat_isolated.py`, `native/tests/test_node_file_is_where_tests_expect.py`,
  `native/ci/native-proof.yml` (job `compat-differential`; plus its copy if C4-01b has landed)
- **files.mustNot** `PKG/**`, `SRV/test/**`, `RAS/docker/**` (`build-images.sh` and
  `image_source_parity.sh` are only READ by `test_compat_isolated.py`)
- **testCommands** T-DIFF, T-NATIVE (`tests/test_differential.py tests/test_runtime_surface.py tests/test_compat_isolated.py`), T-WIN
- **acceptance** Every row of Appendix 1 A3 marked "differential" passes on ubuntu, `windows-2025` and
  macOS; the reference fixtures carry the image digest; `test_runtime_surface.py` finds no attribute
  call on a shimmed name that the runtime lacks; the Windows leg records the timer-grid jitter (p50,
  p99) of scenario `r11_timer_grid` into the job summary — the pass bar on Windows is spec B's S11
  result, **not a number of this spec**. Three runtime-only extension points for C4-09 (Appendix 1
  A1 rule 4; not on the shim surface, unit-tested in `test_runtime_surface.py`):
  `cospin.register(node, with_node=name)` — the node is added to whichever executor `add_node`s the
  node called `name`, and removed with it; `context.find_node(name)`; `MultiThreadedExecutor.busy_workers()`.
  And `Publisher.publish` is safe and never blocks when called from a thread that is not an executor
  worker (`::test_publish_from_a_foreign_thread`: 10 000 publishes from a plain thread while a
  subscriber's executor is blocked return within 1 s in total and arrive in order).
- **Default / alternative.** Default timer wait: one scheduler thread per executor, `time.monotonic()`
  deadlines, `time.sleep` slices, `timeBeginPeriod(1)` and the power-throttling opt-out set by the
  robot tier at start. If S11 shows the bar is missed: a high-resolution waitable timer through
  `ctypes` behind the same `runtime/timer.py::_wait_until`.
- **Failing test** `native/tests/test_differential.py::test_r11_timer_grid` (red: no runtime). The
  reference half is CONFIRMED obtainable: my probe produced Appendix 1 A2 on real rclpy.
- **risk** high · **lenses** correctness, delivery, regression

## C4-05 — The robot tier boots: today's node on the runtime, no arm, no camera
- **Summary.** Write `native/engine/edubotics_robot/` (`__main__.py`, `bootstrap.py::install` — the one place
  that adds `native/engine/compat_ros` and the source table's paths to `sys.path` — and `control.py`, the
  reader of the parent's control pipe). `python -m edubotics_robot` sets the forwarded environment,
  `HF_HUB_DISABLE_TELEMETRY=1`, the calibration and data directories, then calls the node file's
  UNCHANGED `main()`. Replace `cv_bridge` in `PKG/data_processing/data_converter.py` by the decode
  `inference/chunk_engine.py` already uses for the cloud path. Add `native/engine/edubotics_sources.py` (the
  table of repo paths the native packages import from) and dump today's callback-group table
  (`native/tests/tools/dump_lane_table.py` → `native/tests/fixtures/lane_table_step1.json`: every
  `create_service` / `create_timer` / `create_subscription` / guard condition of the package with its
  group, by AST), the snapshot C7-09 is held to.
- **dependsOn** C4-04 · **externalGates** `SPIKE-RUN`
- **layers** native-engine, 5
- **files.may** `native/engine/edubotics_robot/__init__.py`, `__main__.py`, `bootstrap.py`, `control.py`,
  `native/engine/edubotics_sources.py`, `PKG/data_processing/data_converter.py`,
  `native/tests/test_robot_tier_boot.py`, `native/tests/test_bootstrap_order.py`,
  `native/tests/tools/dump_lane_table.py`, `native/tests/fixtures/lane_table_step1.json`,
  `native/tests/test_lane_snapshot.py`, `native/tests/test_decode_parity.py`, `native/tests/differential/scenarios/r21_decode_parity.py`,
  `native/tests/fixtures/rclpy_reference/r21_*.json`, `RAS/tests/test_data_manager_record_fsm.py` (its
  `cv_bridge` stub only)
- **files.mustNot** `PKG/physical_ai_server.py` (not one byte: `git diff --stat` must not list it),
  `PKG/communication/**`, `PKG/safety/**`, `PKG/timer/**`, `PKG/workflow/**`
- **testCommands** T-NATIVE (`tests/test_robot_tier_boot.py tests/test_bootstrap_order.py tests/test_decode_parity.py`), T-SERVER, T-DEPSFREE, T-DIFF (r14, r16, r21), T-WIN
- **acceptance** (1) The child answers `READY` on stdout within its start budget and its bus shows the
  three node names of R12. (2) `/heartbeat` at 1 Hz ± 0.15 s for 10 s; an idle `/task/status` tick
  carries `robot_type 'v1_studio'`, `robot_profile`, `capabilities_json`; `/edubotics/signal_status`
  is latched. (3) A boot whose `init_ros_params` raises (the test points the parameter loader at
  an unreadable YAML) leaves the node alive and degraded exactly as today (`communicator is None`, the
  German `[FEHLER]`, the 3 × 30 s retry), and the heartbeat still ticks. (4) `shutdown` on the control
  pipe, and closing the pipe, both make `spin()` return and the `finally` block run once; the process
  exits 0 within 5 s when no recording is open. (5) Decode: bit-equal to the reference image's
  `cv_bridge` result where both sides run the same OpenCV build, else every pixel within 1 level and
  the channel order exact (fixture JPEGs: `RAS/tests/fixtures/scene_split_1080p.jpg` and two 640 × 480
  frames). (6) `test_lane_snapshot.py`: the dumped table equals the checked-in snapshot, and it
  names the default group for the cloud start and stop services, `/task/command`, the inference
  timer, the two 25 Hz samplers and the collision one-shots (R1–R3).
- **Failing test** `native/tests/test_robot_tier_boot.py::test_heartbeat_and_identity`. Red today:
  `import physical_ai_server.physical_ai_server` fails without ROS (CONFIRMED:
  `ModuleNotFoundError: No module named 'physical_ai_interfaces'` in my import probe).
- **risk** high · **lenses** correctness, delivery, regression

## C4-05b — The recorder and Daten smokes on the native stack (Linux leg)
- **Summary.** C4-00 took the image smokes away from `main`; this package brings the two recorder
  smokes back on the native stack as soon as the runtime exists, BEFORE the packages that edit the
  recorder further (C5-01, C5-05, C5-06). `.github/scripts/record_fsm_smoke.py`: when
  `EDUBOTICS_SMOKE_RUNTIME=native` it installs the shim through `edubotics_robot.bootstrap.install()`,
  builds `TaskInfo` from the shim and runs two NEW shutdown scenarios in place of the two signal
  ones — `shutdown_control_after_two_episodes` (the parent writes `shutdown` on the control pipe) and
  `shutdown_parent_gone_after_two_episodes` (the parent closes the pipe) — with the same child
  set-up and the same on-disk assertions. Without the variable the script behaves exactly as today
  (the ROS images on `release/2.41` keep their SIGINT and SIGTERM scenarios; the script needs rclpy
  only in that child — CONFIRMED by reading). `.github/scripts/daten_smoke.py` imports LeRobot and
  the server package only (CONFIRMED) and runs unchanged. Add the job `native-smokes-linux` to
  `native/ci/native-proof.yml` (ubuntu, the native lock, `HF_HUB_OFFLINE=1`).
- **dependsOn** C4-05 · **externalGates** `SPIKE-RUN` (a `spike/c-…` run until C4-01b has landed)
- **layers** CI/CD, 5
- **files.may** `.github/scripts/record_fsm_smoke.py`, `native/ci/native-proof.yml` (plus its copy if
  C4-01b has landed), `native/tests/test_smoke_modes.py`
- **files.mustNot** `.github/scripts/daten_smoke.py` (it must pass as it is; a needed edit is a
  finding to report, not to make here), every existing scenario's assertions, `PKG/**`
- **testCommands** on the native lock: `EDUBOTICS_SMOKE_RUNTIME=native python .github/scripts/record_fsm_smoke.py`,
  `python .github/scripts/daten_smoke.py`; T-NATIVE (`tests/test_smoke_modes.py`); the Linux leg of T-WIN
- **acceptance** (1) Both smokes exit 0 on ubuntu with the native lock. (2) `test_smoke_modes.py`
  (AST): the scenario list without the variable equals today's; with it, today's list with exactly
  the two signal scenarios replaced by the two control scenarios; no assertion of a kept scenario
  changed. (3) The two control scenarios load exactly the two saved episodes, leave no crash marker
  and no streaming temp file, and the next session resumes the dataset.
- **Failing test** `native/tests/test_smoke_modes.py::test_native_mode_swaps_exactly_the_two_shutdown_scenarios`.
- **risk** medium (tests and a CI definition; no product code) · **lenses** correctness, regression, delivery

## C4-06 — The rosbridge subset and the MJPEG source inside the robot tier
- **Summary.** Write `native/engine/edubotics_robot/rosbridge.py` (Appendix 1 A5: `subscribe`,
  `unsubscribe`, `call_service`; per-client min/min merge; latched replay; concurrent calls; default
  filling; base64; every publishing op refused), `mjpeg.py` (`multipart/x-mixed-replace` passthrough
  of the newest JPEG of a topic) and `internal_http.py` (aiohttp on `127.0.0.1:0`, the internal secret
  header on every request). Write the transcript harness and the cases t01, t02 of Appendix 1 A7.
- **dependsOn** C4-05 · **externalGates** `SPIKE-RUN`
- **layers** native-engine
- **files.may** `native/engine/edubotics_robot/rosbridge.py`, `mjpeg.py`, `internal_http.py`,
  `native/tests/test_rosbridge_subset.py`, `native/tests/differential/rosbridge_transcript.py`,
  `native/tests/fixtures/rosbridge_reference/**`, `native/tests/characterisation/**`
- **files.mustNot** `PKG/**`, `MGR/**`
- **testCommands** T-NATIVE (`tests/test_rosbridge_subset.py`), T-DIFF (rosbridge + t01, t02), and the
  roslib check `node native/tests/js/roslib_client.mjs` (the app's own `roslib` 1.4.1 from
  `MGR/node_modules` against the adapter: subscribe, call, unsubscribe)
- **acceptance** All 14 rows of Appendix 1 A5; `task_status_is_not_replayed`; a client `publish` on
  `/leader/joint_trajectory` reaches no subscriber (asserted on the bus) and logs one WARNING; a
  request without the internal secret is closed before the WebSocket upgrade; t01 and t02 differ from
  the reference only by `INTENTIONAL` entries.
- **Failing test** `native/tests/test_rosbridge_subset.py::test_missing_service_answer_is_byte_equal`
  with the expected value CONFIRMED on real rosbridge:
  `{"op":"service_response","service":"/nope/service","values":"Service /nope/service does not exist","result":false,"id":"c1"}`.
- **risk** high · **lenses** correctness, delivery, regression

## C4-07 — The station front: zones, sessions, static files, proxies
- **Summary.** Write `native/engine/edubotics_station/` front (`__main__.py`, `front.py`, `zones.py`,
  `session.py`, `ports.py`, `static.py`, `proxy.py`, `logging_redact.py`): Appendix 4 B2 and B3 for the
  LOCAL zone and the internal hop. The lan zone's listener is NOT bound in this package (C7-03).
- **dependsOn** C4-06 · **externalGates** `B-S12` (a second Windows account gets nothing), `SPIKE-RUN`
- **layers** native-engine
- **files.may** the eight files above, `native/tests/test_front_local_zone.py`,
  `native/tests/test_front_rebinding.py`, `native/tests/test_front_proxy.py`,
  `native/tests/test_front_no_token_in_logs.py`, `native/tests/test_stable_port.py`
- **files.mustNot** `MGR/nginx*.conf*` (they are the reference, read by the tests), `PKG/**`
- **testCommands** T-NATIVE (the five files), T-WIN
- **acceptance** (1) `Host: evil.example` → connection closed, on every path (the cases of
  `RAS/tests/test_student_nginx_rebinding.py`, ported). (2) No cookie → 401 on `/`, `/rosbridge`,
  `/video/stream`, `/daten-api/x`, `/api/system/status`; `/__session` with a used or wrong key → 403.
  (3) WebSocket upgrade with a foreign or absent `Origin` → 403 before the relay opens. (4) The four
  security headers on every response; the cache rules of B2. (5) A request whose path or `Referer`
  contains `/daten-api/` produces no log record that contains the token (35 request shapes, the
  T1-1 list of `datenNginx.test.js`). (6) `stable_port(sid)` returns the same port on a second call
  and the next free one when the first is taken; the chosen port is persisted. (7) The relay keeps a
  `/rosbridge` connection open across a robot-tier restart attempt only until the tier's socket
  closes, then closes the browser side with code 1012 so `rosConnectionManager` reconnects.
- **Failing test** `native/tests/test_front_local_zone.py::test_every_path_needs_the_host_cookie`.
- **risk** high · **lenses** correctness, delivery, regression

## C4-08a — The station's `/api/system`, settings, and ownership of the robot tier
- **Summary.** Write `native/engine/edubotics_station/system_api.py`, `settings.py`, `policy.py`,
  `supervisor.py` (start order arm link → capture → robot tier; stop order robot tier → arm link →
  capture; robot-tier respawn after 2.0 s, R13), `robot_env.py` (`FORWARDED`, `RETIRED`),
  `protokoll.py`, `netcheck.py`, `texts_de.py`; record the contract fixture from the Pi agent
  (Appendix 4 B4). The arm link and the capture process are started through two small adapters that
  this package gives FAKE implementations for tests; the real ones arrive with C4-09 and C4-10.
- **dependsOn** C4-07 · **externalGates** —
- **layers** native-engine
- **files.may** the files above, `native/tests/tools/record_system_contract.py`,
  `native/tests/fixtures/system_api_contract.json`, `native/tests/system_contract_intentional.py`,
  `native/tests/test_system_api_contract.py`, `native/tests/test_station_lifecycle.py`,
  `native/tests/test_robot_env.py`, `native/tests/test_policy.py`, `native/tests/test_texts_de.py`,
  `.github/scripts/german_detail_lint.py` (one scope), `RAS/tests/test_scene_profile_value_table.py`
- **files.mustNot** `RAS/pi_agent/**` (read only), `MGR/**`
- **testCommands** T-NATIVE (the five test files), T-DEPSFREE (`-p "test_scene_profile_value_table.py"`),
  `python .github/scripts/german_detail_lint.py`
- **acceptance** (1) Key-set and type equality with the fixture for every route of B4, differences
  only in `system_contract_intentional.py`. (2) The station stops a robot tier it finds running at its
  own start and nothing but `POST /environment/start` starts one. (3) `environment/start` while
  `hardware_ready` is false → 409 with a German `message`; while a start is in flight → 503 within
  the bounded wait (the Pi's `_LIFECYCLE_LOCK_WAIT_S` rule), never a blocked request. (4)
  `test_robot_env.py`: every `EDUBOTICS_*` name the server package reads is in `FORWARDED` or
  `RETIRED`; `RETIRED` names the compose-only keys with the reason. (5) Three robot-tier exits inside
  60 s stop the respawn and put a German `[FEHLER]` line into the Protokoll.
- **Failing test** `native/tests/test_system_api_contract.py::test_status_keys_and_types`.
- **risk** high · **lenses** correctness, delivery, regression

## C4-08b — React: station mode (RC-1 … RC-6)
- **Summary.** The six named React changes of Appendix 4 B5, rows RC-1 to RC-6, and nothing else.
- **dependsOn** C4-08a · **externalGates** —
- **layers** 6
- **files.may** exactly the files named in RC-1 … RC-6 and their test files
- **files.mustNot** every other file under `MGR/src`; `MGR/nginx*`; `MGR/Dockerfile*`
- **testCommands** T-REACT, T-ESLINT
- **acceptance** (1) With marker `{"pi":false,"station":"windows"}` on a loopback page: the System tab
  is in the nav, the System page renders without a login, `piAuth` stays `off`, `robotIsRemote()` is
  false, `rsControlBase` answers `/api/system`. (2) With `{"pi":true,"station":"windows"}` or on a
  non-loopback host: today's Pi behaviour, byte for byte (the existing Pi tests are untouched and
  green). (3) Without a marker on a loopback page: today's Windows behaviour (existing tests green).
  (4) The teacher web build's snapshot tests are unchanged. (5) `SystemPage` snapshot for
  `host_kind: 'pi'` equals today's.
- **Failing test** `MGR/src/utils/__tests__/piMode.station.test.js::a station marker on a loopback page shows the System tab and starts no robot login`.
  Red today: `fetchPiMarker` returns a boolean and the nav filter is `piMode || !n.piOnly` (CONFIRMED).
- **risk** medium · **lenses** correctness, regression

## C4-09 — The arm bridge: the arm link in place of the arm container
- **Summary.** Write `native/engine/edubotics_robot/arm_bridge/` against spec B's seam contract as
  revised on 2026-11-25 (SPEC-B §8.2–§8.8; `edubotics_armlink.client.ArmLinkClient`).
  - `link_iface.py` (the interface the bridge uses, frozen here), `link_adapter.py` (the ONLY module
    that imports `edubotics_armlink`), `fake_link.py` (an in-process fake of the interface).
  - **Hosting.** The bridge is one runtime node of its own, `edubotics_arm_bridge`, created by
    `bootstrap` BEFORE the node file's `main()` runs (so its rail subscription exists first, R9d) and
    registered with `edubotics_runtime.cospin.register(node, with_node='physical_ai_server')`: when
    `main()` adds the server node to its `MultiThreadedExecutor(num_threads=8)`, the runtime adds the
    bridge node to the SAME executor. Callback groups: `hb` (MutuallyExclusive: the heartbeat timer
    alone), `rail` (MutuallyExclusive: the rail subscription alone, so messages reach the link in
    publish order), `svc` (Reentrant: torque, home, activate), `tick` (MutuallyExclusive: the 20 Hz
    mode watch and the 1 Hz state re-publishes).
  - `heartbeat.py` — **SPEC-B §8.5, exactly.** A timer of period `HB_PERIOD_S = 0.05` (20 Hz) in
    `hb`. Its callback reads the server node's `_sensor_alive_mono` (the node found through
    `edubotics_runtime.context.find_node('physical_ai_server')`), computes `sensor_age_s =
    time.monotonic() − _sensor_alive_mono` and calls `client.heartbeat(sensor_age_s)` only when it is
    below `HB_SENSOR_FRESH_S = 0.25`. No server node yet, a `None` stamp or an older one: it sends
    NOTHING. One tick sends at most one heartbeat (a late tick never sends the missed ones). Never
    from another group, never from a thread that is not an executor worker.
    `signal_status.INGEST_ALIVE_S` (1.0 s) is NOT this bound. `client.heartbeat()` itself never
    blocks and never raises (SPEC-B §8.9: one latest-wins slot, a writer thread owns the pipe); it
    answers `True` (handed over) or `False` (the arm link is gone). The adapter adds NO drop layer of
    its own and only counts the `False` answers.
  - `state.py` — `on_follower` runs on the client's reader thread and only maps, builds and
    publishes (the runtime's `publish` is a non-blocking enqueue): `t = clock.map(arrival_mono_ns /
    1e9, board_us / 1e6)` with ONE unchanged `capture_timeline.ClockMap` instance (SPEC-B §8.7),
    replaced by a fresh instance when `on_event` reports `boot` or `on_link_state` reports a
    reconnect (the same thread). It publishes `/joint_states` (names `joint1 … joint5, RL_joint`,
    position, velocity, `load_frac` as effort) with `header.stamp = t`: seconds of the ENGINE's
    `time.monotonic()`, not wall time. (The only reader of that stamp in the server package is
    `Communicator`; React's `jointStateInterpolator.js` absorbs any constant offset — both CONFIRMED.)
  - `rail.py` — the one subscriber of `/leader/joint_trajectory` (group `rail`). Outside inference
    every message is handed over UNCHANGED as one `traj(joint_names, points, mode='replace',
    purpose='program')` with `joint_names` as published and `points = [(sec + nanosec·1e-9,
    positions), …]`: no clamp, no fill, no reorder, no time shift, no validation — acceptance,
    filling from the measured pose, clamping and the board mapping are the arm link's (§8.6).
    Inference (the server node's `on_inference` is true): the FIRST message of a run switches the
    board to `mode('setpoint')`, every message's last point is one `setpoint(q)` (which never blocks,
    §8.9), and the board STAYS in `setpoint` for the whole run: SETPOINT has no stream watchdog
    (SPEC-B §8.10) — while heartbeats arrive it holds the last goal through any gap, so
    Cloud-Inferenz's empty-queue holds of up to 15 s (`stall_end_s`) need nothing from the bridge.
    `on_inference` turning false calls `mode('hold')` at the next 20 Hz watch tick. There is no idle
    timer and no mode dance. One bridge-internal lock serialises the two mode changes and the
    setpoints; it is held only around link calls, is no node lock and takes none.
    **A `setpoint_lost` latch ENDS the cloud run** (conductor's decision on SPEC-B §8.10's open
    point; the board latched because heartbeats stopped for 1.0 s, so the engine was blind): the
    event, and likewise `armlink_gone` (code 69) during a run, makes the bridge create ONE one-shot
    timer on the server node WITHOUT a callback group — the default group, so the end is serialised
    with the cloud tick and `/task/command` (R1, R2) — whose callback calls the node's one idempotent
    teardown `_end_inference(texts_de.CLOUD_ARM_LINK_LOST_DE, end_reason='robot_error')` and cancels
    itself. `CLOUD_ARM_LINK_LOST_DE` is C's own sentence: „Die Cloud-Inferenz wurde beendet, weil die
    Verbindung zum Roboterarm länger als eine Sekunde unterbrochen war. Der Arm hält seine Stellung.
    Bitte starte die Inferenz neu." Nothing continues silently: after the latch the bridge sends no
    setpoint and no `mode`, and it NEVER calls `clear_latch` on its own. The event also sets the
    activation state to `failed` with spec B's sentence for the latch, exactly as a lost board does:
    the latch is cleared only inside a new `/edubotics/activate` (and C5-02's `leader-enable`), i.e.
    by a student's press. A cloud start before that meets the board's `latched` refusal at its first
    `mode('setpoint')`, and the bridge ends that run the same way with the refusal's `sentence_de`.
    A rail message has no reply channel, so a refusal is handled by its code: `torque_off` → one
    English log line at most every 5 s (the driver's rate), nothing shown; `empty_trajectory` →
    nothing; `non_finite`, `unknown_joints`, `time_range`, `bus_fault`, `latched`, `bad_mode` → one
    entry on the latched `/edubotics/arm_events` and the code's `sentence_de` as a `/task/status`
    notice, at most one per code per 5 s.
  - `torque.py` (`/v1/set_torque` + the legacy alias: `True` → `energise()`, `False` →
    `torque_off()`; the answer's `message` is the result's `sentence_de`), `home.py` (`/v1/home` +
    `/edubotics/home_arm`; Appendix 2 T13; the glide it plans is sent as `traj(…, purpose='home')`),
    `tower_state.py` (T14), `activation.py` (`/edubotics/activate`, `/edubotics/activation_state`,
    the profile statement T6; in this package the sequence is `home` only, and nothing starts it but
    the student's press: the `EDUBOTICS_REQUIRE_ACTIVATION=0` rollback does not exist in the native
    app, owner Q-C6), `events.py` (`on_event`, `on_link_state`, `on_teleop_state`: every code spec B
    marks as displayed reaches the page with spec B's `sentence_de`, unchanged, through ONE generic
    path — a `/task/status` notice, the latched `/edubotics/arm_events`, and for link and leader
    verdicts the arm rows of the station's `/status`; the bridge holds no per-code table and no
    sentence for a code of spec B).
  The STATION starts the arm-link process (`python -I -m edubotics_armlink --ipc <address> …`, the
  authkey on its first stdin line) and hands address and key to the robot tier on the robot tier's
  stdin; a scan with the robot tier down uses a short-lived arm-link process of its own (`connect`,
  `firmware_info`, `shutdown`), because the arm link serves one client at a time.
- **Thread budget (SPEC-B §8.5: the heartbeat group must never be starved for 0.95 s).** The main
  executor keeps the node's 8 workers. No worker is reserved and the heartbeat has no priority: a
  heartbeat that flowed while every other worker is stuck would prove nothing. The budget is fenced
  instead:
  (a) `native/engine/edubotics_robot/thread_budget.py` holds ONE table: every callback group of the
  robot tier (C4-05's lane snapshot plus the bridge's four) with its kind, `worst_block_s` and the
  reason; and a list of SCENARIOS, the sets of callbacks that can legitimately block at the same
  time under the node's mode arbitration, each with two browser clients — „program start"
  (`/workflow/start` in the default group across `_set_follower_torque(True)`, about 5.2 s; the
  torque service in `svc`; two `/get_registered_hf_user` waits of 8 s in `_hf_cb_group`),
  „recording" (a record tick across the 1 s discard, a `/task/command`, the rail callback, two HF
  waits), „manual" (a jog drive, the hand-guide exit waiting for `_manual_lock`, the rail callback,
  the torque service, two HF waits). The bridge's own entries follow SPEC-B §8.9: the `hb` callback
  never waits (`heartbeat()` returns in microseconds), a setpoint never waits, and a request method
  (`traj`, `energise`, `mode`, `stop`, …) waits at most `REQUEST_TIMEOUT_S` = 2.0 s — the
  `worst_block_s` of the `rail` callback and of each `svc` callback's link call.
  `test_heartbeat_budget.py::test_the_table_is_complete_and_no_scenario_takes_more_than_six_workers`
  fails when a group of the snapshot is missing from the table, when a group with `worst_block_s ≥
  0.2` is in no scenario, or when a scenario blocks more than 6 of the 8 workers.
  (b) `::test_no_scenario_starves_the_heartbeat` runs every scenario on the real runtime — each
  member a callback in its real kind of group sleeping its `worst_block_s` (capped at 6 s), all
  started together — with the bridge's heartbeat against the fake link: the largest gap between two
  heartbeats is at most 0.5 s (about half of spec B's bound; the measured maximum is recorded per CI
  leg). `::test_a_saturated_executor_sends_no_heartbeat` blocks all 8 workers for 1.5 s and asserts
  the OPPOSITE: no heartbeat during the block (the proof is honest) and exactly one within 0.1 s
  after it (no burst).
  (c) `::test_no_gil_hold_reaches_the_bound`: a probe thread (sleep 10 ms, record the overshoot) runs
  beside each named CPU-heavy operation executed for real — the tower guard's `dense_leg` over the
  800-leg set, one arm-check solve on the synthetic kit, `_ik_precheck` and `json.loads` on a
  256 KiB program, the rosbridge encoding of one 1280 × 960 base64 frame — and its largest overshoot
  is below 0.3 s on the Linux and macOS legs; on `windows-2025` it is recorded, and the slow
  laptop's number is `SCHOOL-PC`'s (no bar is invented for it).
  In the product the heartbeat callback measures its own lateness: a gap above 0.5 s writes ONE
  English WARN line with `executor.busy_workers()`.
- **dependsOn** C4-05, C4-08a · **externalGates** `B-ARMLINK-API` (spec B's B14 merged: dev IPC),
  `B-SOFTBOARD` (B08), `OWNER-SIGNOFF-BOOT` (PLAN §2.4 "Boot", "Energise": given in principle, signed
  after the rig run), `RIG-V1-HAT` (real proof). The frozen protocol (`B-PROTOCOL-FREEZE`, B18) is
  needed before C7-01, not before this package.
- **layers** native-engine, 3 (arm bring-up)
- **files.may** `native/engine/edubotics_robot/arm_bridge/**`, `native/engine/edubotics_robot/bootstrap.py`,
  `thread_budget.py`, `texts_de.py`, `native/engine/edubotics_station/armlink_adapter.py`,
  `supervisor.py`, `system_api.py` (the arm rows of `/status` and `POST /scan-arms` only),
  `native/tests/test_arm_bridge_*.py`, `native/tests/test_heartbeat_budget.py`,
  `native/tests/test_activation_bridge.py`, `native/tests/test_tower_state_bridge.py`,
  `native/tests/test_no_devboard_import.py`
- **files.mustNot** `RAS/docker/open_manipulator/*.py` (imported as libraries, not edited), `PKG/**`,
  `native/armlink/**`, `native/protocol/**`, `native/firmware/**`, `native/devboard/**` (spec B's),
  `native/tests/fixtures/rail_messages_v1.json` (C4-0R's; read only)
- **testCommands** T-NATIVE (`tests/test_arm_bridge_*.py tests/test_heartbeat_budget.py tests/test_activation_bridge.py tests/test_tower_state_bridge.py tests/test_no_devboard_import.py`),
  T-DEPSFREE (`-p "test_feetech_*.py"`, `-p "test_tower_state_topic.py"`, `-p "test_activation_agent.py"`,
  `-p "test_native_armlink_*.py"`)
- **acceptance** (1) Before `/edubotics/activate` the link sees `connect`, heartbeats and one
  `energise()` and nothing else; `energise()` is sent from the bridge's first `tick` after the
  executor spins (a heartbeat already flows), never from `bootstrap`. (2) A home glide the floor or
  tower decision refuses calls no link method, leaves torque on and answers the driver's German
  sentence unchanged. (3) For EVERY entry of `native/tests/fixtures/rail_messages_v1.json` published
  on the rail outside inference, the fake link records exactly one `traj` whose `joint_names` equal
  the entry's and whose points equal `(sec + nanosec·1e-9, positions)` of the entry: nothing added,
  dropped or reordered. (4) During inference each message is exactly one `setpoint`; a 15 s gap
  between two messages produces, against spec B's soft board with heartbeats flowing, no
  `setpoint_lost` event, unchanged goal registers during the gap, and NO `mode` call between the
  run's `mode('setpoint')` and its closing `mode('hold')`. (4b) With a cloud run open (a node whose
  `_cloud_run` is set and `on_inference` is true), a `setpoint_lost` event from the link — and,
  separately, `armlink_gone` — ends the run within 1 s: `_end_inference` ran exactly once, inside a
  default-group callback of the server node, with `CLOUD_ARM_LINK_LOST_DE` and `end_reason=
  'robot_error'`; `/task/status` READY carries the sentence; the link saw no `clear_latch`, no
  `setpoint` and no `mode` after the event; a second event ends nothing (idempotent); the
  activation state is `failed`. A cloud start while the latch stands ends at once with the board's
  `latched` sentence and sends no setpoint; a following `/edubotics/activate` sends `clear_latch` for
  the set latches before its home glide, and a cloud run after it works. (4c)
  Killing the arm-link process: `heartbeat()` answers `False`, the bridge calls no further link
  method, `/status` shows spec B's sentence for `armlink_gone`, and the station restarts the arm
  link and the robot tier as a pair (a new process needs a new client and a new authkey, §8.9),
  with the supervisor's back-off. (5) A refused `energise()` answers `success
  False` with the result's `sentence_de`, byte for byte, and `_last_torque_reason()` carries it.
  (6) `test_arm_bridge_heartbeat.py`, the fake link recording thread and time: no heartbeat before
  the server node exists, none while `_sensor_alive_mono` is `None` or 0.25 s old or older, 20 ± 2
  per second otherwise, each from an executor worker, each carrying `sensor_age_s`; and an AST check
  that `PhysicalAIServer._sensor_loop` still assigns `self._sensor_alive_mono` before `spin_once`
  (the private name this bridge reads). (7) `/joint_states` stamps are the mapped engine-monotonic
  time: non-decreasing, never later than `time.monotonic()` at publish, and after a simulated `boot`
  event the next stamp comes from a fresh `ClockMap`. (8) `/edubotics/tower_state` is never re-judged
  while the follower sample's mode says a trajectory executes. (9) `on_link_state` reporting the
  board lost or `armlink_gone`, the event `boot`, or a latched `link_lost` / `setpoint_lost` sets the
  activation state to `failed` with the code's sentence and requires a new activate, which is the
  only place (with C5-02's `leader-enable`) that calls `clear_latch`. (10) `test_arm_bridge_events.py` iterates EVERY code of
  `edubotics_boardproto.codes` that spec B marks as displayed, the `fp_code` sub-codes 200–219
  included (218 `torque_limit_ram` among them): the sentence the page receives equals
  `texts_de.sentence(code, **params)` byte for byte on each surface (notice, `/edubotics/arm_events`,
  the `/status` arm rows), and `empty_trajectory` reaches no surface. (11) `app_too_old` →
  `hardware_ready: false` with the code's sentence; `firmware_too_old` → the update offer of C7-02;
  nothing ever downgrades. (12) `test_no_devboard_import.py`: no module under `native/engine`,
  `native/capture`, `native/host`, `native/sandbox` imports `edubotics_devboard` or `native/bench`
  outside `native/tests`. (13) One end-to-end test runs the bridge against spec B's soft board
  (`edubotics_devboard.softboard`) on the Linux and macOS legs. (14) The three budget tests above.
- **Failing tests** `native/tests/test_arm_bridge_home.py::test_a_refused_glide_calls_no_link_method_and_keeps_torque`
  (input: joints from `RAS/tests/fixtures/v1_limp_starts_b.json` that the driver's check refuses),
  `native/tests/test_arm_bridge_heartbeat.py::test_no_heartbeat_once_the_sensor_stamp_is_a_quarter_second_old`,
  and `native/tests/test_arm_bridge_rail.py::test_a_setpoint_lost_latch_ends_the_cloud_run_once_in_german`
  (input: the fake link raises the event `setpoint_lost` 0.2 s into an open cloud run; asserts
  acceptance 4b; red until the bridge exists — and a bridge that only logs the event, or that clears
  the latch and goes on, fails it).
- **Default / alternative.** Default: spec B's client is SPEC-B §8.3 and §8.9–§8.10 as settled on
  2026-11-25. If a method changes before B18, only `link_adapter.py` changes; `link_iface.py` is
  this package's. Nothing of this package waits for an answer of spec B any more (M18 and M19 are
  settled, main file §12.2). The node's `_end_inference` is a private name this bridge calls: an
  AST check in `test_arm_bridge_rail.py` holds its signature (`reason_de`, `end_reason`, `publish`).
- **risk** high · **lenses** correctness, delivery, regression

## C4-10 — The capture process and its ingest
- **Summary.** Write `native/capture/edubotics_capture/` (`__main__.py`: builds `camera_bridge.CameraBridge`
  from the roles the station passes, runs `scene_split.selftest()` first, serves the station's
  enumeration, preview and probe requests on its control pipe) and
  `native/engine/edubotics_robot/capture_ingest.py::NativeCameraIngest` (Appendix 2 T4). Add the
  `CONNECT_PREAMBLE` constant to `camera_bridge._ConnectMixin._connect`. Replace C4-08a's fake capture
  adapter by the real one.
- **dependsOn** C4-08a · **externalGates** `B-S16` (MSMF in `opencv-python-headless`), `SPIKE-RUN`,
  `O5` (wrist camera hardware; software path does not wait)
- **layers** native-capture, native-engine, 2 (one constant in the old GUI's bridge)
- **files.may** `native/capture/edubotics_capture/**`, `native/engine/edubotics_robot/capture_ingest.py`,
  `native/engine/edubotics_station/capture_adapter.py`, `RAS/gui/app/camera_bridge.py` (the preamble only),
  `native/tests/test_capture_*.py`, `native/tests/test_system_scene_profile.py`,
  `RAS/tests/test_scene_capture_owner_guard.py` (scan root)
- **files.mustNot** `RAS/gui/app/scene_split.py`, `RAS/docker/open_manipulator/scene_split.py`,
  `RAS/docker/open_manipulator/camera_ingest_node.py`, `RAS/gui/app/win_camera.py`
- **testCommands** T-NATIVE (`tests/test_capture_*.py tests/test_system_scene_profile.py`), T-DEPSFREE
  (`-p "test_camera_*.py"`, `-p "test_scene_*.py"`), T-WIN
- **acceptance** (1) With `CONNECT_PREAMBLE == b''` the old bridge sends byte-identical streams
  (`test_camera_bridge_d21.py` unchanged and green). (2) The ingest publishes nothing for a connection
  without the preamble and binds only `127.0.0.1`. (3) Under `tower_v1` an id-1 frame that is not
  640 × 480 is dropped (G7b) and ids 16/17 appear on their topics. (4) Ticking the tower box runs the
  probe; a refusal stores `legacy` and answers `SCENE_PROBE_REFUSED_DE`. (5) Killing the capture
  process makes the station restart it, and the recorder sees a source stop, not a crash.
- **Default / alternative.** Default: one OpenCV provider (`opencv-python-headless`) for every
  process. If S16 shows the headless wheel has no MSMF backend: the capture process alone gets
  `opencv-python` in its own site directory (`{app}\site-capture`); no process ever imports both.
- **Failing test** `native/tests/test_capture_ingest.py::test_a_connection_without_the_preamble_publishes_nothing`.
- **risk** high · **lenses** correctness, delivery, regression

## C4-11 — The tower kit is whole: inventory gate
- **Summary.** Tests only, plus the glue they expose as missing: `native/tests/test_tower_kit_inventory.py`
  asserts, for every row T1 … T23 of Appendix 2, that the piece is reachable in a running robot tier
  (topic exists with its QoS, service answers, symbol imported from the stated file);
  `native/tests/test_tower_kit_e2e.py` runs, against the robot tier with the fake link and
  detection-level frames from `SRV/test/autocal_sim.py::mat_check_frames`, the sequence statement
  `tower_v1` → `/calibration/start mat_check` → state `needs_arm_check/none`, then with a stored arm
  check → `ok`, then a real `/workflow/start` passes `program_start_gate`, and under `legacy` is
  refused with `camera_not_tower`. Adds transcript case t06.
- **dependsOn** C4-09, C4-10 · **externalGates** `RIG-V1-HAT` + the kit (the arm check itself)
- **layers** native-engine, 9
- **files.may** `native/tests/test_tower_kit_inventory.py`, `native/tests/test_tower_kit_e2e.py`,
  `native/tests/test_stop_flow.py`, `native/tests/characterisation/t06_*`,
  `native/engine/edubotics_station/system_api.py` (only `GET /status`'s running-tier keys if the e2e shows a gap)
- **files.mustNot** `PKG/workflow/**`, `PKG/scene_provenance.py`
- **testCommands** T-NATIVE (the three files), T-SERVER (`test/test_autocal_*.py test/test_tower_guard.py test/test_scene_*.py test/test_studio_camera*.py`)
- **acceptance** 23 inventory rows pass or are marked `carried_by: <later package>` exactly as
  Appendix 2 says (T18 → C5-05, T19 → C5-05, T8's rest pose → C5-04, T23 → C6-02); no row is absent.
- **Failing test** `native/tests/test_tower_kit_inventory.py::test_every_piece_is_live` (red until
  C4-09 and C4-10 are in).
- **risk** medium · **lenses** correctness, regression

## C4-12 — The host: window, session bootstrap, supervision, close guard; the Phase 4 end-to-end run
- **Summary.** Write `native/host/edubotics_host/` (`__main__.py`, `window.py` — pywebview `edgechromium`,
  persistent non-roaming profile under `<data root>\webview` (`<data root>` is
  `%LOCALAPPDATA%\EduBoticsStation`, Appendix 4 B6 — not the WSL product's folder), `private_mode=False`; `supervisor.py` —
  starts the station, restarts it with back-off, shows the German crash page from a local file while
  it is down; `session.py` — mints the one-time key, navigates to `/__session?k=…&next=/?_v=<version>&fresh=<nonce>`;
  `single_instance.py` — a `Local\` mutex named for the station (`EduBoticsStation-…`, never a name
  the WSL product's GUI uses), focus the existing window; `close_guard.py` — cancels the
  window's close, raises `edubotics:close-requested`, falls back to a native German message box when
  the page does not answer within 5 s; `texts_de.py`) and RC-8. Add the Playwright run
  `native/tests/e2e/phase4.spec.mjs`.
- **dependsOn** C4-08b, C4-11 · **externalGates** `SPIKE-RUN`, `SCHOOL-PC` (WebView2 on a locked-down PC),
  `RIG-V1-HAT` (the gate's real half), `OWNER-CI-REASK` (Q-C8: the conductor puts the CI question to
  the owner again before this package starts; either answer lets it proceed — yes: C4-01b lands
  first and T-WIN runs on `main`; no: spike runs as before)
- **layers** native-host, 6
- **files.may** `native/host/edubotics_host/**`, `MGR/src/components/StationCloseGuard.jsx` + its test,
  `MGR/src/StudentApp.js` (the mount only), `native/tests/test_host_*.py`, `native/tests/e2e/**`
- **files.mustNot** `RAS/gui/**`, `native/engine/edubotics_station/**` except `system_api.py`'s `POST /host/close`
- **testCommands** T-NATIVE (`tests/test_host_*.py`), T-REACT, `npx playwright test native/tests/e2e/phase4.spec.mjs` (Chromium, against the station with the fake link and synthetic cameras), T-WIN
- **acceptance** (1) A second launch focuses the first window and exits. (2) Killing the station shows
  the crash page within 3 s and the app is usable again after the restart without a manual reload.
  (3) The one-time key is never in a log, never in `argv` of any process (asserted by listing the
  children's command lines). (4) Playwright: login (against a stub cloud), System page → „Umgebung
  starten", the Startseite twin moves with the fake link's joints, both camera tiles show frames,
  „Roboter aktivieren" reaches `active`, the calibration panel reaches `ok` with the synthetic kit.
  (5) Closing the window with a running recording finalizes it (the saved episodes load) before the
  processes exit.
- **Failing test** `native/tests/test_host_session.py::test_the_key_is_single_use_and_never_on_a_command_line`.
- **risk** high · **lenses** correctness, delivery, regression

## C4-13 — Phase 4 documentation
- **Summary.** Add the section „Native app (in progress)" to CLAUDE.md (Appendix 3 C5's interim rule),
  `docs/NATIVE-ARCHITECTURE.md` (process tree, zones, how to run the station from a checkout), the
  dated narrative in `docs/CLAUDE-CHANGELOG.md`, and in `docs/KNOWN-ISSUES.md` the open gates
  (`RIG-V1-HAT`, `SCHOOL-PC`, the Windows budget of the tower guard, S11's bar).
- **dependsOn** C4-12 · **externalGates** —
- **layers** docs · **files.may** `CLAUDE.md`, `docs/NATIVE-ARCHITECTURE.md`, `docs/CLAUDE-CHANGELOG.md`, `docs/KNOWN-ISSUES.md` · **files.mustNot** everything else
- **testCommands** T-DEPSFREE (`-p "test_no_icon_glyphs.py"`), `python .github/scripts/german_detail_lint.py`
- **acceptance** Every sentence of CLAUDE.md that Phase 4 made false for `main` is named in the new
  section with the branch it still holds on; symbols, not line numbers.
- **risk** low · **lenses** combined

---

# Phase 5 — recording, Daten, Cloud-Inferenz on the V1

## C5-01 — The V1 records: profile flags, the wrist camera role, the recorded action, source-stamped arm data
- **Summary.** `_V1_STUDIO`: `recordable`, `editable`, `trainable`, `inferable`, `has_leader` true,
  `follower_only` false, `display_name_de` „V1", a `help_de` without „Nur für Roboter Studio";
  `camera_roles=('scene','gripper')`. `config/v1_studio_config.yaml`: the `gripper` camera and
  observation, and two new list parameters: `leader_action_topic: [/leader/action]` and
  `engine_stamped_sources: [follower, leader]` (read by `init_ros_params` like the existing ones;
  absent in the OMX config = today's behaviour).
  **The leader source.** When the config names `leader_action_topic`, `Communicator`'s LEADER SOURCE
  subscribes there (a `JointTrajectory` with a header), while `joint_publishers` keeps publishing
  the command rail.
  **The clocks (SPEC-B §8.7; `ClockMap` and `SourceHistory` are NOT edited).** For a source kind
  listed in `engine_stamped_sources`, the message's header stamp IS the sample's time on the
  engine's `time.monotonic()` clock; the arm bridge put it there — for the follower from the
  unchanged `ClockMap` fed the arm link's `arrival_mono_ns` and `board_us` (C4-09's `state.py`), for
  the leader action `(request_mono_ns + arrival_mono_ns) / 2`. `Communicator` appends such a message
  as `_append_history(source, t, 0.0, msg)` with `t = _stamp_seconds(msg)`: the history's arrival AND
  time are the sample's own time (with stamp 0 the unchanged `ClockMap` answers its arrival
  argument). The engine's receipt time is used for neither. A stamp that is 0, not finite, or more
  than `ENGINE_STAMP_FUTURE_TOL_S = 0.005` ahead of `time.monotonic()` is not trusted: that message
  takes today's path (arrival = receipt, stamp 0) and a counter rises, reported in the take's
  English `capture take` line. `_note_arrival` and `_follower_last_arrival_mono` keep the engine's
  RECEIPT time: the source-stop rule and the stale-readback rule judge delivery, as today.
  **The sampler.** `SlotSampler(fps, cameras, leader_stamped=False)` and `TakeIntegrity(cameras, fps,
  leader_stamped=False)`. With `True` the leader is read with `_newest_le` instead of
  `_newest_le_backdated` (its times are real; back-dating would move them), its gap timeline uses
  the plain times, and `gap_source` counts a leader gap WITHOUT the follower-delivery discount: that
  discount exists because a server stall delays an UNSTAMPED leader, while a stamped leader's gap is
  a real hole. The node passes `leader_stamped = 'leader' in engine_stamped_sources` at its one
  construction site (`init_robot_control_parameters_from_user_task`).
  **The arm bridge** publishes `/leader/action` from `on_leader` (the client's reader thread,
  non-blocking): a one-point `JointTrajectory`, `joint_names` the six V1 names, `positions` = spec
  B's `LeaderSample.action_rad` — the mapped leader reading BEFORE any clamp — and `header.stamp =
  (request_mono_ns + arrival_mono_ns) // 2` nanoseconds as `sec` / `nanosec`; only while
  `on_teleop_state` says `ON`, never in `PREPARED`, where samples already flow.
  `init_ros_params` drops the `gripper` camera when `EDUBOTICS_WRIST_CAMERA != '1'`. START_RECORD is
  refused with `record_texts_de.TELEOP_OFF_START_DE` while teleop is off.
- **dependsOn** C4-11 · **externalGates** `B-BENCH-TELEOPLAG` (O8; its bar is set against the OMX
  teleop-lag baseline, which still exists), `B-S11-XPROC` (spec B's S11 proves on Windows that two
  processes' `time.monotonic_ns()` are one clock — the premise of every stamp above), `B-S11`, `O4`
  (mapping), `O5-geometry`, `B-S13`
- **layers** 5, native-engine, 2/3 (the two thin registries until C7-06)
- **files.may** `PKG/robot_profiles.py`, `SRV/config/v1_studio_config.yaml`,
  `PKG/communication/communicator.py`, `PKG/communication/capture_timeline.py` (ONLY `SlotSampler` and
  `TakeIntegrity`: the `leader_stamped` parameter and its three effects), `PKG/physical_ai_server.py`
  (`init_ros_params`' parameter list, the two constructor calls, the START_RECORD refusal),
  `PKG/data_processing/record_texts_de.py`, `native/engine/edubotics_robot/arm_bridge/leader_action.py`,
  `RAS/gui/app/constants.py` and `RAS/pi_agent/constants.py` (the `v1_studio` rows, kept in lockstep),
  `MGR/src/components/Record/model/recordCopy.js` only if the sentence is shown from the page, the
  tests of all of them, `SRV/test/test_board_stamps_keep_spacing.py`,
  `SRV/test/test_recorded_action_is_the_leader_reading.py`, `native/tests/test_leader_action.py`,
  `native/tests/test_two_process_clock.py`
- **files.mustNot** `capture_timeline.py::ClockMap` and `::SourceHistory` (a test hashes the two class
  bodies against today's text), `SRV/config/omx_f_config.yaml`, `PKG/safety/**`, `PKG/workflow/**`,
  `PKG/data_processing/data_manager.py`'s FSM
- **testCommands** T-SERVER, T-DEPSFREE (`-p "test_robot_profile_lockstep.py"`, `-p "test_record_r5_lockstep.py"`),
  T-NATIVE (`tests/test_leader_action.py tests/test_two_process_clock.py`), T-REACT
- **acceptance** (1) With the OMX config nothing changes: the golden record tests and
  `test_capture_timeline*.py` pass unedited, and the sampler's default arguments reproduce today's
  decisions on the existing fixtures. (2) `test_board_stamps_keep_spacing.py`: 100 Hz follower
  messages whose stamps are 10 ms apart, delivered to the real follower callback in ONE burst after
  simulated engine stalls of 0.3, 0.8, 1.5 and 3.0 s, sit in the history 10 ± 1 ms apart; the same
  messages on today's path collapse (the test's negative control). (3) A take recorded from the
  fake link stores, for every frame, `action == mapped leader reading` at the newest leader sample
  at or before the frame's observation instant — also when the link reports that the SENT goal was
  clamped (the fake clamps joint 3; the dataset must not show the clamp). (4) Engine stalls of 0.5,
  1.0 and 3.0 s between two frames produce no `Signalaussetzer` re-record and no lost slot beyond
  the existing `CATCHUP_MAX_S` rule; a REAL leader hole of 0.3 s (the fake link sends no leader
  sample) re-records the take, also while follower samples are missing for 0.15 s at the same time.
  (5) A stamped leader sample is never moved: with jittered stamps (two samples 1.5 ms apart) the
  chosen action's time equals the sample's stamp. (6) A stamp 20 ms in the future is not used and
  is counted. (7) Teleop off → START answers `TELEOP_OFF_START_DE`; teleop turning off during a take
  ends the session by the existing source-stop rule naming the leader. (8) Two processes'
  `time.monotonic()` readings taken around one pipe round trip differ by less than the round trip,
  on every CI leg.
- **Failing tests** `SRV/test/test_board_stamps_keep_spacing.py::test_a_three_second_engine_stall_keeps_the_spacing`
  — CONFIRMED red on today's path by my probe with the real `SourceHistory`: after stalls of 0.8,
  1.5 and 3.0 s the burst's smallest spacing is 0.005 ms, and it stays 9.998–10.003 ms when the
  history is fed the sample's own time (`scratch/architect-C/probes/board_stamp_probe.py`). And
  `SRV/test/test_recorded_action_is_the_leader_reading.py::test_a_clamped_goal_is_not_what_is_recorded`
  (red today: the V1's leader source IS the rail, `v1_studio_config.yaml`
  `leader:/leader/joint_trajectory`, CONFIRMED). Why `leader_stamped` exists: the same probe shows
  today's `_backdated` moving 32 of 400 correctly stamped leader samples by up to 8.5 ms.
- **Default / alternative.** Default: relay in the arm link, leader samples stamped by the arm link.
  If the teleop-lag bench fails and the owner's fallback applies (leader on the HAT's bus, teleop on
  the board): `/leader/action` is fed from the board's state frame and its stamp goes through the
  follower's mapping (board time); nothing above `leader_action.py` changes. If `B-S11-XPROC` fails
  on Windows: `arm_bridge/clock_offset.py` (then a file of this package) estimates the offset
  between the two processes per connection with an IPC round-trip bracket and adds it before the
  mapping; the estimate's bound joins the teleop-lag record.
- **risk** high · **lenses** correctness, delivery, regression

## C5-02 — Teleop is a state: activation `home → sync → teleop`, the gates re-keyed, link loss
- **Summary.** The arm bridge publishes the latched `/edubotics/teleop_state`
  (`{"v":1,"teleop":bool,"reason":…,"seq":n}`), subscribed on the sensor node.
  `CollisionMonitorMixin.leader_appears_active()` answers that state for a profile whose config names
  `leader_action_topic`, and today's freshness rule otherwise — so the five node call sites and the
  trip gate change by ONE predicate. Activation becomes `home → sync → teleop`. The sync glide is a
  NODE operation, because the planner, the arm model and the tower guard live there: a new mixin
  `PKG/guarded_glide.py::GuardedGlideMixin` gives the node `run_guarded_glide(target_full, label_de)`,
  which is the manual home glide (`_run_manual_home_glide` / `_plan_manual_home_legs`) with the target
  as a parameter: the same transient manual claim under `_mode_lock`, the same floor-checked ladder
  (`home_planner.plan_floor_checked_home_route(ctx, q_start, target, …, tower=…)`, CONFIRMED to take an
  arbitrary target), the same tower rung, slow peak velocity, stop handling and German refusals. It is
  NOT a service (rosbridge cannot reach it); the arm bridge, which runs in the same process, calls it
  with `teleop_prepare()`'s `leader_action_q` as the target — all six joints, the claw included,
  because spec B's `teleop_on()` refuses unless every one of the six is within 0.30 rad and the
  PREPARE is at most 10 s old. After the glide it prepares again; a leader that moved meanwhile gets
  ONE more glide, then activation fails with „Bitte den Leader-Arm ruhig halten und erneut aktivieren."
  Only then `teleop_on()`. `/api/system/roboter-studio/leader-disable` calls `teleop_off()` at once;
  `leader-enable` is the student's action that runs sync + start. `on_teleop_state(LOST)` (the board's `teleop_lost` / `link_lost` latch) publishes one German notice;
  the bridge calls `clear_latch` only inside `leader-enable` or a new activate, never on its own.
- **dependsOn** C5-01 · **externalGates** `B-BENCH-LINKLOSS`, `OWNER-SIGNOFF-TELEOP-GATES`
  (PLAN §2.4: link loss in teleop, resume rule, sync glide guarded — each in principle, conditional on
  its bench), `RIG-V1-HAT`
- **layers** native-engine, 5, 8, 9 (the gates' consumers)
- **files.may** `PKG/safety/collision_monitor.py` (`leader_appears_active` only),
  `PKG/guarded_glide.py` (new), `PKG/physical_ai_server.py` (ONLY: the mixin in the class bases, and
  `_plan_manual_home_legs` gaining an optional `target` whose default reproduces today's call),
  `native/engine/edubotics_robot/arm_bridge/teleop.py`, `activation.py`, `sync_glide.py`,
  `native/engine/edubotics_station/system_api.py` (the three roboter-studio routes),
  `native/engine/edubotics_robot/texts_de.py`, `native/tests/test_teleop_gates.py`,
  `native/tests/test_sync_glide.py`, `native/tests/test_link_loss_flow.py`,
  `SRV/test/test_collision_workflow_gate_c1_high6.py`
- **files.mustNot** the five leader-gate call sites in `PKG/physical_ai_server.py` (`_cloud_leader_active`,
  `_manual_leader_blocks`, `workshop_record_callback`'s `start_leader`, `_leader_teach_sample`,
  `_launch_workflow`; a test asserts their text is unchanged), `PKG/workflow/home_planner.py`,
  `PKG/workflow/fixture_guard.py`, `MGR/**`
- **testCommands** T-NATIVE (the three files), T-SERVER, T-REACT
- **acceptance** With teleop on, each of these answers its existing German refusal and sends nothing
  to the link: a real `/workflow/start`; `/edubotics/cloud_inference/start` (a RUNNING cloud run is
  ended within 1 s of teleop turning on); `/workshop/jog` (incl. mode `home`); `/workshop/hand_guide`
  on; `/workshop/record` `start`; `/workshop/replay`. `start_leader` REQUIRES teleop on. NOT gated,
  each asserted: a simulator `/workflow/start`, `hand_guide` off, `/workshop/capture_pose`,
  `/workshop/mark_destination`. A sync glide the tower guard or the floor refuses moves nothing, and
  activation ends `failed` with the planner's German sentence. `teleop_on()` is never called without a
  PREPARE of the same attempt; a `teleop_not_synced` answer ends the attempt with the code's sentence;
  teleop state `LOST` is never left except through `leader-enable` or a new activate.
  **A leader that is not limp is shown, never repaired** (SPEC-B §8.4; the leader is provisioned
  with `Max_Torque_Limit = 0`, owner Q-B2, and the arm link checks it before every PREPARE): a
  `teleop_prepare()` refused with `leader_fingerprint` (`fp_code` 209 `max_torque` or 218
  `torque_limit_ram`) or `leader_torqued` ends activation `failed` and answers `leader-enable` 409,
  both with exactly spec B's sentence from `texts_de.sentence(code, **params)`, parameters filled by
  spec B (for 218 it names the servo and asks to switch the leader's 12 V supply off and on again,
  SPEC-B appendix protocol §4.3); the same sentence stands in the leader row of the System page's
  `/status` (C4-09's generic path). The engine sends nothing to
  the leader, offers no retry loop and adds no sentence of its own
  (`test_teleop_gates.py::test_a_leader_that_is_not_limp_fails_activation_with_spec_b_s_sentence`,
  parametrised over the three codes).
- **Failing test** `native/tests/test_teleop_gates.py::test_a_real_program_is_refused_while_teleop_is_on`.
  Red today: on the V1 `leader_appears_active()` is always False (the subscription is never created,
  `_init_collision_monitor`'s early return, CONFIRMED).
- **risk** high · **lenses** correctness, delivery, regression

## C5-03 — The V1 collision stop's engine half: the two-step recovery
- **Summary.** For a profile with `collision_source='board'` (new `ArmProfile` field; `v1_studio`
  only), `CollisionMonitorMixin` arms no detector and no `/collision_flag`; instead the arm bridge's
  collision event (spec B: the `COLLISION_HOLD` latch, the board already froze the source and relaxed
  in place) enters the EXISTING flow at `_trigger_collision_stop`'s consequences: discard
  the running episode, bump the record generation, `COLLISION(7)`, the CollisionModal; `HOME_FOLLOWER`
  calls `clear_latch({'collision'})` and glides to the V1 HOME (`safe_home_arm_rad` = HOME, owner Q16) through the floor-and-tower ladder;
  `RESUME_TELEOP` runs C5-02's sync + start; `FORCE_RESUME_TELEOP` keeps its meaning. Joint names,
  the safe home and the tolerance come from the profile, never from the OMX constants.
- **dependsOn** C5-02 · **externalGates** `B-BENCH-COLLISION`, `O10`, `OWNER-SIGNOFF-COLLISION`
- **layers** native-engine, 5 (safety path)
- **files.may** `PKG/safety/collision_monitor.py`, `PKG/robot_profiles.py` (the one field),
  `native/engine/edubotics_robot/arm_bridge/collision.py`, `SRV/test/test_board_collision_fsm.py`,
  `native/tests/test_collision_bridge.py`
- **files.mustNot** `PKG/safety/collision_detector.py`, the OMX branches' behaviour (their tests
  `RAS/tests/test_collision_*.py` and `SRV/test/test_collision_workflow_gate_c1_high6.py` must pass
  unedited), `MGR/src/components/CollisionModal.js`
- **testCommands** T-SERVER, T-DEPSFREE (`-p "test_collision_*.py"`), T-NATIVE (`tests/test_collision_bridge.py`)
- **acceptance** (1) A board stop during a recording take discards exactly that take and the session
  resumes on the same dataset after recovery (the existing seamless-resume assertions, on the V1
  path). (2) The one-shot timers of the recovery are created with no `callback_group=` (R2). (3) With
  O10 = "teleop and recording only" (the default this package builds): a board `collision_stop` event
  that arrives while a program, a manual drive or a cloud run owns the arm is treated as an arm fault
  (C4-09's `events.py`: the run ends in German), never as the modal flow. (4) Nothing here changes a
  sample (the trip discards).
- **Default / alternative.** Default scope: the board's compiled `{TELEOP}`. If the owner widens O10:
  the bridge sends `config_tighten('collision_scope_add', …)` at robot-tier start, the same event
  enters the modal flow in the widened modes and step 2 becomes „Fortsetzen" without teleop; the
  change is confined to `collision.py`'s mode table and needs a new sign-off.
- **Failing test** `SRV/test/test_board_collision_fsm.py::test_a_board_stop_discards_the_take_and_opens_the_modal`.
- **risk** high · **lenses** correctness, delivery, regression

## C5-04 — The orderly end: glide to the rest pose, then torque off
- **Summary.** Add `REST_POSE` per arm to the fixture contract block (`scene_fixture.py`), update its
  sha256 pin, re-export `sceneFixture.json`, mirror it in `feetech_geometry.py`. `POST
  /environment/stop` and `POST /host/close` ask the robot tier for the orderly end: end what runs
  (recording finalized without upload, cloud bye, program stop), then, when the arm is torqued and
  activated, call C5-02's `run_guarded_glide(REST_POSE, …)` (the floor-checked ladder with the tower
  rung), verify arrival, torque off. When the plan is refused: answer 409 `rest_glide_refused` with the fall
  direction; the page shows the existing tower question and re-sends with `force: true`, which torques
  off without the glide. A limp arm is never glided.
- **dependsOn** C5-02 · **externalGates** `O7/RG-H1` (HOME decides the pose), `OWNER-SIGNOFF-REST-POSE`
  (the value and the contract change), `RIG-V1-HAT`
- **layers** 9 (fixture contract), native-engine, 6 (`sceneFixture.json`, RC-8's refusal branch)
- **files.may** `PKG/workflow/scene_fixture.py`, `SRV/test/test_scene_fixture.py`,
  `MGR/src/components/Workshop/fixture/sceneFixture.json`, `RAS/docker/open_manipulator/feetech_geometry.py`
  (the mirror literal), `native/engine/edubotics_robot/orderly_end.py`, `native/engine/edubotics_station/system_api.py`,
  `MGR/src/components/StationCloseGuard.jsx`, `MGR/src/pages/SystemPage.js` (the 409 branch),
  `classroom_kit/*.pdf` (regenerated: the hash line), `native/tests/test_orderly_end.py`,
  `SRV/test/test_rest_pose_route.py`
- **files.mustNot** `PKG/workflow/fixture_guard.py`, `home_planner.py`, `arm_cells.py`, any `TOWER_*`
  constant
- **testCommands** T-SERVER, T-DEPSFREE (`-p "test_scene_fixture_lockstep.py"`), T-NATIVE (`tests/test_orderly_end.py`), T-REACT
- **acceptance** (1) `test_rest_pose_route.py`: from HOME, from the observation pose and from every
  corner of the printed zone at hover height the ladder finds a route to `REST_POSE` that the tower
  guard passes with the uncalibrated residual, and at `REST_POSE` `limp_fall_direction` is not `back`.
  (2) A refused plan moves nothing and changes no torque until `force`. (3) `contract_hash()` changed
  exactly once; a stored arm check from before answers `needs_arm_check/contract_changed`. (4) Stop
  order and budgets hold (`native/tests/test_stop_budget.py`).
- **The value is NOT in this spec.** It is derived in this package by search under the constraints of
  acceptance (1) and confirmed on the rig; the owner signs the number.
- **Failing test** `SRV/test/test_rest_pose_route.py::test_every_start_reaches_the_rest_pose_clear_of_the_tower`.
- **risk** high · **lenses** correctness, delivery, regression

## C5-05 — Daten and the recorder on Windows: the portability layer
- **Summary.** Add `PKG/utils/platform_compat.py` (stdlib only): `lock_file`, `fsync_dir`,
  `replace_with_retry`, `rename_dir_with_retry`, `spawn_low_priority`, `kill_tree`, `parent_alive`,
  `runtime_dir`, `is_reparse_point`, `link_or_copy`, `data_root`. Use it at exactly the sites of
  research/03 §7.6 that I re-found in the code: `hub_sync.py` (module-top `import fcntl`,
  `stage_lock`, `swap_in`, `recover`), `daten/download_worker.py` (the cleaner script's `fcntl` and
  `/proc`), `daten/node_service.py` (`os.killpg`, the `nice` command prefix, `start_new_session`),
  `data_processing/edit_worker.py` (`nice`), `daten/link_tokens.py` (`os.link`, `SECRET_DIR`),
  `daten/contract.py` (`SECRET_DIR`, `CLIP_TMP_DIR` become functions of `runtime_dir`),
  `scene_provenance.py` (the unguarded directory open; `O_NOFOLLOW`), `dataset_paths.py` (the three
  roots honour `EDUBOTICS_DATA_ROOT`), `inference/cloud_contract.py::ACTIVE_MARKER_PATH`,
  `physical_ai_server.py::_init_code_rpc`'s directory and the four `EDUBOTICS_CALIB_DIR` defaults.
  The sidecar closes its readers of a dataset before a swap of that dataset (a lease notice on its
  control line) and the swap retries with back-off.
- **dependsOn** C4-11 · **externalGates** `B-S10`, `B-S9` (long paths), `SPIKE-RUN`
- **layers** 5 (Daten, recorder), native-engine
- **files.may** `PKG/utils/platform_compat.py` and the files named above, their tests,
  `SRV/test/test_platform_compat.py`, `native/tests/platform_skips.json` (entries removed),
  `RAS/tests/test_daten_contract_lockstep.py`
- **files.mustNot** `PKG/data_processing/v3_surgery.py`'s engine, `dataset_sync.decide`, the upload's
  gate order in `hub_sync.upload`
- **testCommands** T-SERVER (ubuntu AND windows legs), T-DEPSFREE, `python .github/scripts/daten_smoke.py` on both legs
- **acceptance** (1) On Linux and macOS every changed function behaves as before (the existing Daten
  tests pass unedited; `stage_lock` still uses `flock`). (2) On Windows: `import
  physical_ai_server.daten.library` works; `write_stamp` writes the stamp; a download and an edit
  start (no `FileNotFoundError: 'nice'`); `kill_tree` ends a worker and its children;
  `daten_smoke.py` passes with a second process holding one mp4 of the dataset open during the swap.
  (3) The data root is `<data root>\datasets`, never under a roaming folder, and a dataset name whose
  longest file path would exceed 240 characters is refused at START in German.
- **Default / alternative.** Default: quiesce the sidecar's readers + retry. If S10 shows a swap still
  fails under a real-time scanner: generation directories (`<name>.gen-<n>` plus an atomically
  replaced pointer file), confined to `hub_sync.swap_in` / `recover` and `dataset_paths`.
- **Failing test** `SRV/test/test_platform_compat.py::test_hub_sync_imports_without_fcntl` — CONFIRMED
  red today: with `fcntl` unimportable, `hub_sync`, `daten.hub_reads`, `daten.library` and
  `daten.node_service` fail to import (my simulation); and
  `SRV/test/test_scene_provenance.py::test_write_stamp_on_a_platform_without_directory_fsync`.
- **risk** high · **lenses** correctness, delivery, regression

## C5-06 — The Hugging Face token lives in memory
- **Summary.** `hf_token_store` gets backends: the file slot (today, kept for POSIX test runs) and a
  memory slot owned by the robot-tier process, selected by `EDUBOTICS_HF_TOKEN_SLOT=memory`. Children
  (the HF worker, the download worker, the sidecar) read through `hf_token_store.read()`, which in a
  child asks the robot tier over a `multiprocessing.connection` channel with an `authkey` handed to
  the child at spawn. One factory, `hf_token_store.hub_api()`, builds every `HfApi`; the robot tier
  sets `HF_HUB_DISABLE_IMPLICIT_TOKEN=1` and `HF_HOME=<data root>\hf`. The public token key file goes
  to `<data root>\run\robot-key\`.
- **dependsOn** C5-05 · **externalGates** —
- **layers** 5, native-engine
- **files.may** `PKG/data_processing/hf_token_store.py`, `hf_api_worker.py`, `hub_sync.py` (the
  `HfApi()` line), `data_manager.py` (its `HfApi()` lines), `PKG/daten/hub_reads.py`,
  `download_worker.py`, `PKG/data_processing/hf_token_crypto.py` (`KEY_FILE_DIR` resolution),
  `native/engine/edubotics_robot/bootstrap.py`, `SRV/test/test_hf_token_memory.py`, transcript t03
- **files.mustNot** the wire (`SetHFUser.srv`, the state topic's keys), `hf_token_crypto`'s envelope
- **testCommands** T-SERVER, T-DEPSFREE (`-p "test_hf_*.py"`), T-NATIVE (t03)
- **acceptance** (1) With the memory slot no file anywhere under the data root or `%TEMP%` contains
  the token after a set, an upload and a download (the test greps the tree). (2) A child that holds no
  `authkey` gets no token. (3) AST fence: no `HfApi(` call in the package outside `hub_api()` and the
  two that pass an explicit `token=`. (4) The file slot's behaviour is unchanged
  (`test_hf_token_node_wiring.py` passes unedited). (5) After the robot tier exits the token is gone
  (a new tier reports `stored: false`).
- **Failing test** `SRV/test/test_hf_token_memory.py::test_no_file_holds_the_token`.
- **risk** high · **lenses** correctness, delivery, regression

## C5-07 — A model from another robot is refused
- **Summary.** Cloud API: `POST /inference/sessions` answers two additive keys, `dataset_robot_type`
  and `dataset_joint_names`, read anonymously from the training dataset's `meta/info.json` at its
  pinned revision by the pattern of `inference_sessions.dataset_scene_profile` (capped body, bounded
  wait, definite answers cached, never fails a reserve; `null` when unknown).
  `StartCloudInference.srv` gains `string model_robot_type` and `string[] model_joint_names`. The page
  keeps and sends them and disables Start with the reason (RC-10). The node refuses the start, before
  the mode claim, when both are known and `model_robot_type != data_robot_type` or the joint name
  lists differ, with `cloud_texts_de.OTHER_ROBOT_DE`.
- **dependsOn** C5-01 · **externalGates** `OWNER-MERGE-CLOUD` (a merge to `main` deploys W2)
- **layers** 7, 8, 6, interfaces
- **files.may** `RAS/cloud_training_api/app/services/inference_sessions.py`, `app/routes/inference.py`,
  `app/tests/test_inference_dataset_robot.py`, `app/tests/test_platform_token_scope.py`,
  `physical_ai_tools/physical_ai_interfaces/srv/StartCloudInference.srv`,
  `PKG/physical_ai_server.py` (`start_cloud_inference_callback`), `PKG/inference/cloud_texts_de.py`,
  `PKG/inference/cloud_contract.py`, the files of RC-10, `RAS/tests/test_cloud_inference_lockstep.py`,
  `SRV/test/test_cloud_inference_node.py`
- **files.mustNot** `RAS/modal_inference/**` (no Modal deploy), any migration, `cloud_protocol.py`
- **testCommands** T-CLOUD, T-SERVER, T-DEPSFREE (`-p "test_cloud_inference_lockstep.py"`), T-REACT
- **acceptance** (1) An OMX-trained model (`robot_type omx_f`, names ending `gripper_joint_1`) on a V1
  is refused in German at the page and at the node; nothing is claimed, no client is started. (2)
  Unknown on either side starts as today (refuse on proof). (3) An older page (no fields) and an older
  API (no keys) both keep working. (4) The lookup sends no `Authorization` header and never delays the
  reserve beyond the existing scene lookup's bound.
- **Failing test** `SRV/test/test_cloud_inference_node.py::test_a_model_of_another_robot_is_refused_before_the_claim`.
- **risk** high · **lenses** correctness, delivery, regression

## C5-08 — The recorder and Daten smokes on Windows, as a standard user
- **Summary.** The native modes of both smokes exist since C4-05b (Linux). Add the job
  `native-smokes-windows` to `native/ci/native-proof.yml`: it runs `record_fsm_smoke.py`
  (`EDUBOTICS_SMOKE_RUNTIME=native`) and `daten_smoke.py` as a local standard user through spec B's
  `native/spikes/windows/run_as_standard_user.ps1`, now that the portability layer (C5-05) and the
  memory token (C5-06) make them runnable there. Add the stop-budget test.
- **dependsOn** C4-05b, C5-05, C5-06 · **externalGates** `B-STDUSER-HARNESS`, `SPIKE-RUN`
- **layers** CI/CD, 5
- **files.may** `native/ci/run_as_standard_user.ps1` (only if spec B delivers none),
  `native/ci/native-proof.yml` (plus its copy if C4-01b has landed), `native/tests/test_stop_budget.py`;
  `.github/scripts/record_fsm_smoke.py` and `daten_smoke.py` ONLY for a Windows path or process
  primitive the run proves necessary (each such line named in the commit message)
- **files.mustNot** the scenarios' assertions (none is loosened; a scenario may only gain a platform
  twin), `PKG/**`
- **testCommands** T-WIN (`native-smokes-windows`); on ubuntu the two commands of C4-05b
- **acceptance** Both smokes exit 0 as a non-admin user on `windows-2025`; the job fails when the
  process token is elevated (it asserts `IsUserAnAdmin() == False` first); the shutdown twins load
  exactly the two saved episodes and leave no crash marker; `test_stop_budget.py` holds node 30 s <
  robot-tier kill 40 s < station stop 120 s.
- **Failing test** the job itself (red until C5-05).
- **risk** medium · **lenses** correctness, delivery, regression

## C5-09 — V1 pictures for the Aufnahme page and the collision modal (RC-9)
- **Summary.** Render the three V1 strips and the V1 leader glyph with the existing renderer, register
  them in `robotAssets.js`.
- **dependsOn** C5-01 · **externalGates** `O4` (the leader's look, if it differs from a V1),
  `OWNER-RENDER-TOOL` (CLAUDE.md names the untracked renderer `tools/frontend-harness/render-robot-pictures.sh`;
  it is ABSENT from this checkout, CONFIRMED — the owner provides it, and this package commits it under
  `tools/frontend-harness/` so the pictures are reproducible)
- **layers** 6 · **files.may** RC-9's files, `tools/frontend-harness/**` · **files.mustNot** every other `MGR/src` file
- **testCommands** T-REACT, T-ESLINT
- **acceptance** `phasePicture('v1_studio', 'warmup'|'reset'|'collision')` answer strips whose files
  are pinned by size and sha256; an unknown profile still draws none.
- **Failing test** `icons/__tests__/robotAssets.test.js::the V1 has its three phase pictures`.
- **risk** low · **lenses** combined

## C5-10 — Phase 5 documentation and the trial protocol
- **Summary.** Update the interim CLAUDE.md section, the changelog and known issues; write
  `docs/NATIVE-TRIAL.md`, the protocol of the gate `TRIAL-PASSED`: record → train → Cloud-Inferenz on
  the V1. There is NO measured OMX train-and-run baseline (owner, spec B's Q-B4: skipped, no paid OMX
  trials); the V1's success bar is the owner's judgement at the gate. So the document fixes what is
  RECORDED, not what passes: the task and its set-up (objects, positions, lighting, the camera
  profile), the number of recorded episodes and who recorded them, the training's id, steps and
  hours, the number of Cloud-Inferenz trials, a written rule for counting one trial as a success
  (decided by the owner before the trials), the raw per-trial table, every refusal or stop with its
  German sentence, and a signature block: the owner's decision (passed / not passed), the date and
  his reasons in his words. The one comparison that still has a baseline is teleop lag (spec B's OMX
  teleop-lag baseline); the document cites that record and does not restate its number.
- **dependsOn** C5-08 · **externalGates** —
- **layers** docs · **files.may** `CLAUDE.md`, `docs/CLAUDE-CHANGELOG.md`, `docs/KNOWN-ISSUES.md`, `docs/NATIVE-TRIAL.md`
- **testCommands** `python .github/scripts/german_detail_lint.py`
- **acceptance** The trial document contains no pass number and no reference to an OMX success rate;
  it says in one sentence that the bar is the owner's judgement (`SPEC-DECISIONS.md`, Q-B4) and that
  this amends PLAN §5's "Pass bars" for this gate; `docs/KNOWN-ISSUES.md` lists `TRIAL-PASSED` as
  open with the same sentence.
- **risk** low · **lenses** combined

