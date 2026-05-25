# On-demand robot-arm startup (Home → Leader-Sync)

## What this is

The ROBOTIS OMX arms used to come fully alive at container boot. Now they launch
**limp** (torque off, no motion) and only home + sync when the student clicks
**"Roboter starten"** on the Start page. The dashboard still opens immediately
after "Umgebung starten", exactly as before.

## Why

1. **UX**: the arm must not move until the student chooses to start it.
2. **Dataset safety**: the old boot-time sync *soft-failed and continued*, so a
   student could record on a follower↔leader that was structurally out of sync
   (e.g. joints 2/3/4 off by 0.5–0.9 rad). That bakes a wrong
   `observation.state ↔ action` correspondence into every dataset. The new flow
   makes sync **verification a blocking gate**.

## How it works

The follower ROS stack still launches at boot so `/joint_states` streams and the
container healthcheck/dependency chain are unchanged — but the xacro's
`disable_torque_at_init: true` keeps the servos unpowered (limp, no motion). Only
the *motion* is deferred.

When the student clicks the button, the frontend calls a ROS service (via
rosbridge) and a new node runs:

1. **Referenzierung (Homing)** — follower moves to a known home pose.
2. **Leader-Sync** — read the leader's *current* pose, move the follower to match.
3. **Prüfung (Verify, blocking)** — confirm convergence (0.30 rad on arm joints,
   gripper exempt, ≥50 % commanded motion). Only a **pass** reports `ready`.

Every move uses a quintic profile (zero velocity + acceleration at the endpoints)
whose duration scales with the largest joint delta, so motion is smooth and a
larger gap takes longer — never faster. On failure the UI shows the per-joint
error with German guidance and a **retry that does not require restarting the
environment**. Until `ready`, the Aufnahme / Inferenz / Roboter-Studio tabs stay
locked.

## ROS interface (stock message types — no new interfaces)

- **Service** `/edubotics/start_arm` — `std_srvs/srv/Trigger` → `{success, message}` (German).
- **Topic** `/edubotics/arm_state` — `std_msgs/msg/String` (latched), JSON
  `{phase, percent, message, error, per_joint_err}`;
  `phase ∈ idle | homing | reading_leader | syncing | verifying | ready | error`.

## Files

**Backend (`open_manipulator` container)**
- `robotis_ai_setup/docker/open_manipulator/arm_startup_node.py` — **new** node
  (service + state topic, home → sync → blocking verify; reuses the original
  quintic + verification thresholds verbatim).
- `robotis_ai_setup/docker/open_manipulator/entrypoint_omx.sh` — removed the
  boot-time sync + leader-pose read; starts the node (skipped on the Jetson
  `EDUBOTICS_FOLLOWER_ONLY=1` path). Torque-disable-on-SIGTERM untouched.
- `robotis_ai_setup/docker/open_manipulator/Dockerfile` — COPY the node.
- `robotis_ai_setup/docker/docker-compose.yml` — forward `EDUBOTICS_HOME_POSE`,
  `EDUBOTICS_ARM_SYNC_BASE_SEC`, `EDUBOTICS_ARM_SYNC_MAX_VEL`,
  `EDUBOTICS_ARM_SYNC_RETRIES`.
- `.github/workflows/ci.yml` — env-forwarding-guard scans the new node.

**Frontend (`physical_ai_manager`)**
- `src/components/RobotStartup.js` — **new** animated hero (arm wakes/rises,
  scanline, pulsing joints, progress bar + phase stepper, green ready / red error
  with per-joint chips).
- `src/store/armStartupSlice.js` — **new** Redux state (`armReady` gates the tabs).
- `src/hooks/useRosServiceCaller.js` — `startArm()`.
- `src/hooks/useRosTopicSubscription.js` — subscribe `/edubotics/arm_state`.
- `src/pages/HomePage.js` — mounts the hero; "Aufnahme starten" gated on `armReady`.
- `src/StudentApp.js` — locks Aufnahme / Inferenz / Roboter-Studio until `armReady`.
- `src/index.css` — startup animation keyframes.

## Tunables (env, defaults)

| Var | Default | Meaning |
|-----|---------|---------|
| `EDUBOTICS_HOME_POSE` | `0,0,0,0,0,0` | follower home pose (6 joints, rad) |
| `EDUBOTICS_ARM_SYNC_BASE_SEC` | `3.0` | minimum move duration |
| `EDUBOTICS_ARM_SYNC_MAX_VEL` | `0.6` | peak joint velocity cap (rad/s) |
| `EDUBOTICS_ARM_SYNC_RETRIES` | `2` | sync attempts before reporting failure |

## Verification status

`bash -n` + Python compile clean; 95 unittests pass; env-forwarding-guard passes;
React production build compiles (exit 0); scoped ESLint on all changed files is
clean. Two deep Opus reviews returned PASS (callback-group threading has no
deadlock, quintic + verify math is byte-equivalent to the original, follower
stays limp until the first trajectory, `ready` is gated strictly on a passing
verify). Not yet validated on hardware; not committed/pushed.
