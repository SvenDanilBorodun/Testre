# Roboter Studio: Architecture, System Behavior & Troubleshooting Guide

## Executive Summary

Roboter Studio is a visual programming environment that combines a browser-based Blockly canvas with a real-time ROS 2 kinematic, perception, and trajectory execution backend. 

### Core Question:
> **Will every workflow that can possibly be built work correctly and have expected behavior?**  
> **Answer: NO.** While Blockly ensures syntactic validity (blocks connect without syntax errors), physical, kinematic, perceptual, concurrency, and safety constraints mean that many theoretically constructible workflows will fail, throw errors, or behave unexpectedly.

---

## 1. System Architecture & Block Execution

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│                                 BROWSER FRONTEND (React)                                │
│                                                                                          │
│   ┌──────────────────────────┐    ┌───────────────────────────┐   ┌──────────────────┐   │
│   │   BlocklyWorkspace.jsx   │    │  SimStage / SimScene.jsx  │   │  RightDock.jsx   │   │
│   │  (Custom Blocks, AST)    │    │  (2D Canvas + 3D WebGL)   │   │(Jog, Record, Cal)│   │
│   └────────────┬─────────────┘    └─────────────┬─────────────┘   └────────┬─────────┘   │
│                │                                │                          │             │
│   ┌────────────┴────────────────────────────────┴──────────────────────────┴─────────┐   │
│   │ Redux workshopSlice ── useAutosave.js (IndexedDB / sessionStorage) ── sessionScope│   │
│   └─────────────────────────────────────────────┬─────────────────────────────────────┘   │
└─────────────────────────────────────────────────┼────────────────────────────────────────┘
                                                  │ rosbridge (WebSocket)
                                                  ▼
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│                           BACKEND ENGINE (physical_ai_server)                            │
│                                                                                          │
│   ┌──────────────────────────────────────────────────────────────────────────────────┐   │
│   │ WorkflowManager (workflow_manager.py)                                            │   │
│   │  • Main Daemon Thread + up to 16 Hat Threads (_run_hat_handler)                  │   │
│   │  • Locks: motion_lock (RLock), var_lock, claim_lock, Conditions                  │   │
│   │  • Pre-flight IK & No-Go Zone (Sperrzonen) Validation                            │   │
│   └────────────────────────────────────────┬─────────────────────────────────────────┘   │
│                                            │                                             │
│   ┌────────────────────────────────────────┴─────────────────────────────────────────┐   │
│   │ Interpreter (interpreter.py)                                                     │   │
│   │  • AST Walker (Statement Handlers vs. Value Evaluators)                          │   │
│   │  • Native Control Blocks: if/else, repeat, while/until, forever, wait_until      │   │
│   │  • Multi-Object Loop: edubotics_while_visible (empty-debounce, stall protection) │   │
│   │  • Procedure Registry & Scoped Parameter Shadowing                               │   │
│   └────────────────────────────────────────┬─────────────────────────────────────────┘   │
│                                            │                                             │
│   ┌────────────────────────────────────────┴─────────────────────────────────────────┐   │
│   │ Execution Handlers (handlers/)                                                   │   │
│   │  • motion.py: Quintic trajectories, velocity floors, IK, workspace floor margin │   │
│   │  • path_guard.py: Minkowski inflated no-go zones, swept line collision routing   │   │
│   │  • perception_blocks.py: AprilTag detection, catalog mapping, tag yaw, reclaims  │   │
│   │  • destinations.py & trajectory.py: Click-to-pin targets, Contract-B playback    │   │
│   │  • sim_world.py / sim_arm.py: Shared mutable virtual scene & nearest-grasp logic │   │
│   └──────────────────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Problems & Direct Fixes

Below is the complete breakdown of problems, causes, and solutions:

### Section A: System & Codebase Bugs (For Developers)

#### 1. Debugger Controls (Pause/Step) Inactive
* **Problem**: The Pause, Step, and Breakpoint buttons in the UI do not pause or step the robot on unpatched servers.
* **Cause**: `physical_ai_server.py` lacks registered ROS service callbacks for `WorkflowPause`, `WorkflowStep`, `WorkflowContinue`, and `WorkflowSetBreakpoints`.
* **Fix**: Register service callbacks in `robotis_ai_setup/docker/physical_ai_server/overlays/physical_ai_server.py` that call `WorkflowManager.pause()`, `step()`, `resume()`, and `set_breakpoints()`.

#### 2. Live Sensor Panel Is Empty
* **Problem**: The UI `SensorPanel` remains blank during workflow execution.
* **Cause**: The React hook `useRosTopicSubscription` subscribes to `/workflow/sensors`, but the backend node does not publish `SensorSnapshot.msg`.
* **Fix**: Implement a 5 Hz publisher in `physical_ai_server.py` broadcasting joint positions, gripper openings, and visible AprilTag IDs.

#### 3. Audio Muting on Reconnection
* **Problem**: Speech (`edubotics_speak_de`) and tone blocks permanently stop making sound after a network/rosbridge reconnect.
* **Cause**: In `useRosTopicSubscription.js`, changing `rosbridgeUrl` closes the browser's `AudioContext`.
* **Fix**: Decouple `AudioContext` lifecycle from rosbridge URL changes. Instantiate it once on first user interaction and never close it on reconnects.

#### 4. Stale IK Warnings on Blocks
* **Problem**: Yellow warning icons placed on blocks during pre-flight IK failure remain visible even after fixing coordinates and re-running.
* **Cause**: `RunControls.jsx` calls `block.setWarningText(...)` when an issue is detected, but never clears warnings on subsequent runs.
* **Fix**: Clear all workspace block warnings (`block.setWarningText(null)`) at the start of every run in `RunControls.jsx`.

#### 5. Locked Toolbox After Leaving Tutorial
* **Problem**: When a student exits a tutorial, block categories in the toolbox remain locked/hidden.
* **Cause**: `setActiveTutorial({id: null})` in `workshopSlice.js` did not clear `restrictedBlocks` inside the Redux reducer.
* **Fix**: Update the `setActiveTutorial` reducer to reset `state.restrictedBlocks = []` whenever tutorial ID is set to `null`.

---

### Section B: Kinematics & Physical Hardware (For Operators & Workflows)

#### 6. "Außerhalb des Arbeitsbereichs" (Out of Reach)
* **Problem**: The arm halts immediately with an unreachable position error.
* **Cause**: The target position lies outside the reachable radius ($R < 0.10\text{ m}$ or $R > 0.28\text{ m}$ for OMX).
* **Fix**: Position physical objects and pinned destination targets within the reachable green annulus ($10\text{ cm}$ to $28\text{ cm}$ from the base center).

#### 7. Table Collision Floor Refusal
* **Problem**: The arm refuses to descend to pick up an object.
* **Cause**: Target height is lower than the safety floor ($Z < z_{\text{table}} + 0.01\text{ m}$) or table calibration is missing.
* **Fix**: Run the 3-step **Calibration Wizard** (specifically the Table Touch step) so the robot accurately measures the table plane $z_{\text{table}}$.

#### 8. "Bewegung durch Sperrzone blockiert"
* **Problem**: Arm stops when attempting to travel across the workspace.
* **Cause**: An obstacle box (Sperrzone) completely blocks the path, and all automatic rerouting attempts (direct, lift-and-travel, base-swing) fail.
* **Fix**: Resize or reposition the obstacle boxes in the scene editor so the arm has space to lift above or swing around them.

---

### Section C: Perception & Computer Vision (For Workflows)

#### 9. "Kein Greifziel" (Unguarded Greifziel Crash)
* **Problem**: A workflow crashes with `GraspSkip: Kein Greifziel`.
* **Cause**: `find_object` found no object (due to occlusion or absence), returning `None`, and the workflow immediately passed `None` to `move_above` or `descend_to`.
* **Fix**: Always guard split-grasp motions with an `if` condition:
  ```text
  setze Ziel = finde Würfel
  falls (Ziel) dann:
      fahre über Ziel
      senke auf Ziel
      schließe um Ziel
      hebe an
  ```

#### 10. `Solange sichtbar` Loop Halts After 3 Passes
* **Problem**: Multi-object loop stops early with `[WARNUNG] kein Fortschritt`.
* **Cause**: Custom motion blocks were used without claiming the tag, leading the stall guard to detect zero progress.
* **Fix**: Use the built-in `greife <Typ>` block (which auto-claims tags), or insert `merke Ziel als erledigt` (`mark_done`) after each pick-and-place.

#### 11. Optical Rejection & Tag Jitter
* **Problem**: AprilTags fail to be detected or are skipped during pickup.
* **Cause**: Lighting glare, motion blur, or tag warping violates the $50\%$ edge length tolerance (`_TAG_EDGE_TOL_FRAC`) or circular yaw stability ($R < 0.9$).
* **Fix**: Ensure diffuse, non-glaring top-down illumination and apply AprilTags completely flat on object surfaces.

---

### Section D: Logic, Control Flow & Concurrency (For Workflows)

#### 12. "Bewegung blockiert" (10s Motion Timeout)
* **Problem**: A workflow aborts with `WorkflowError: Bewegung blockiert`.
* **Cause**: An event hat block (`Wenn Ereignis...`) attempts to move the arm while the main program is also commanding a move, exceeding the 10-second `motion_lock` timeout.
* **Fix**: Do not command arm movements simultaneously across multiple threads. Use `sende Ereignis` (broadcast) to sequence motions one after another.

#### 13. `Warte bis` Auto-Continuation (5-Minute Timeout)
* **Problem**: A `Warte bis` (Wait until) block gives up and continues running downstream blocks even when the condition was never met.
* **Cause**: Built-in classroom safety ceiling (`WAIT_UNTIL_MAX_SECONDS = 300.0s`) prevents workflows from hanging permanently on impossible conditions.
* **Fix**: Ensure conditions are achievable within 5 minutes, or verify the condition explicitly inside an `if` block before executing actions.

#### 14. 10,000 Loop Iteration Limit
* **Problem**: A loop crashes with `InterpreterError: Maximum von 10000 Wiederholungen erreicht`.
* **Cause**: Standard `repeat` and `while` loops have a hard limit of $10,000$ iterations to prevent freezing the server.
* **Fix**: For intentionally infinite monitoring loops, use the dedicated `wiederhole fortlaufend` (Forever) block instead of counting loops.

---

## 3. Quick Reference: Best Practices for Reliable Workflows

1. **Always Home First**: Start workflows with `fahre zur Heimposition` to ensure a clean, known starting configuration.
2. **Guard Detections**: Never move to a target without verifying `falls (Ziel)` first.
3. **Claim Objects**: In custom loops, always call `merke Ziel als erledigt` after picking an object.
4. **Calibrate Before Running**: Ensure the camera and table plane are calibrated to avoid height errors.
5. **Serialize Motion**: Never drive the arm concurrently in both a `When` block and the main stack.
