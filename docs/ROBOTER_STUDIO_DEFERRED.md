# Roboter Studio — Deferred Follow-up Work

> Deliverable from the 2026-05-10 end-to-end upgrade. This file lists every
> item the four deep-dive Opus 4.7 1M review agents flagged that was NOT
> applied during the rollout. Most items are quality polish or require
> server-side work in `physical_ai_server.py` (the upstream-overlay
> integration file) that wasn't touched. Each item has a severity tag,
> a one-line description of what to do, and a pointer to the relevant
> file:line.

## Conventions

- **CRITICAL** — would crash, leak data, or silently break a shipped feature.
- **HIGH** — student-visible bug or significant correctness gap.
- **MEDIUM** — performance / a11y / hygiene with measurable impact.
- **LOW** — polish, comments, doc-vs-code wording drift.

---

## 1. Backend wiring still missing (CRITICAL/HIGH)

These items make the phase-2 debugger and calibration-overhaul features
into UI-only decoration. The React side calls the new ROS services; the
Python overlay must register the handlers, otherwise every call returns
"service not found".

| # | Severity | Item | Where |
|---|---|---|---|
| 1.1 | CRITICAL | Register service callbacks for `WorkflowPause`, `WorkflowStep`, `WorkflowContinue`, `WorkflowSetBreakpoints` on the physical_ai_server node. Each callback calls `WorkflowManager.pause()` / `step()` / `resume()` / `set_breakpoints()`. | `robotis_ai_setup/docker/physical_ai_server/overlays/physical_ai_server.py` (extend the overlay) |
| 1.2 | CRITICAL | Register service callbacks for `CalibrationPreview`, `VerifyCalibration`, `CalibrationHistory`. These need new methods on `CalibrationManager`: `get_charuco_preview(camera) -> {detected, corners_x, corners_y, board_area_pct}`, `verify_pose(camera, world_x, world_y) -> {predicted_pixel_x, predicted_pixel_y, residual_mm}`, `list_history(camera) -> [{timestamp, step, reprojection_error_px, agreement_deg}]`. | Same overlay + `overlays/workflow/calibration_manager.py` |
| 1.3 | CRITICAL | Publish `/workflow/sensors` (SensorSnapshot.msg) at 5 Hz when a workflow is active. Wire `WorkflowManager` to expose follower joints + gripper opening + visible apriltag IDs + color pixel counts + visible YOLO classes. The React `useRosTopicSubscription.subscribeToWorkflowSensors` subscribes; the publisher is absent. | Same overlay + `overlays/workflow/workflow_manager.py` (add a snapshot accessor) |
| 1.4 | HIGH | Wire `cloud_vision_enabled` request field from `StartWorkflow.srv` into the `WorkflowManager.start(workflow_json, workflow_id, cloud_vision=...)` parameter. Build the `cloud_vision` dict on the server side: `translate=<synonym dict>`, `cloud_burst=lambda bgr, prompt: post_to_cloud_api(...)`. Without this, the open-vocab block always raises German "Cloud-Erkennung deaktiviert". | Same overlay |
| 1.5 | HIGH | Implement `CalibrationManager.compute_frame_quality(corners, board_area_pct, sharpness) -> int8` (1=POOR, 2=OK, 3=GOOD). Map to the `CalibrationCaptureFrame.srv` response's `quality` field. Coverage cell = `int8 (board_centroid_x // (img_w/4)) + 4*(board_centroid_y // (img_h/4))`. | `overlays/workflow/calibration_manager.py:_save_intrinsic_frame` |
| 1.6 | HIGH | Add the calibration history directory at `/root/.cache/edubotics/calibration/history/{camera}_{ISO timestamp}.yaml`, prune-keep-newest-5 on every save. | `overlays/workflow/calibration_manager.py:_solve_intrinsic`, `_solve_handeye` |
| 1.7 | MEDIUM | Use Postgres session-GUC `app.saved_by` (set via `SET LOCAL` from the cloud API on every save) so the `snapshot_workflow_version` trigger can record `saved_by` instead of leaving it NULL. | `015_workflow_versions.sql` + `cloud_training_api/app/routes/workflows.py:update_workflow` |

---

## 2. Frontend wiring still missing (HIGH/MEDIUM)

| # | Severity | Item | Where |
|---|---|---|---|
| 2.1 | HIGH | Audio context lifecycle: closing the `AudioContext` on `rosbridgeUrl` change permanently mutes future SPEAK/TONE. Decouple from rosbridge subscriptions — create on first user gesture, never close on URL change. | `physical_ai_manager/src/hooks/useRosTopicSubscription.js:161-186, 355` |
| 2.2 | HIGH | `workflowStatusTopicRef` and `workflowSensorsTopicRef` are never unsubscribed on hook unmount; the main `cleanup()` only handles task/heartbeat/training/HF topics. Memory leak + duplicate subscribers per hook re-mount. | Same file, `cleanup` function |
| 2.3 | HIGH | Workflow status dispatches 4 actions per message (status, runState, paused, detections). Batch via `unstable_batchedUpdates` or combine into a single reducer — currently 4 React renders per WorkflowStatus tick. | Same file, `subscribeToWorkflowStatus` |
| 2.4 | MEDIUM | `SensorPanel` selector returns whole `sensorSnapshot` object → full re-render at 5 Hz. Split selectors per field with `shallowEqual`. Same for `VariableInspector`. | `physical_ai_manager/src/components/Workshop/SensorPanel.jsx`, `VariableInspector.jsx` |
| 2.5 | MEDIUM | `ToolbarButtons` 5s interval re-renders the entire toolbar. Extract `<AutosaveAgeLabel/>` as a memoized leaf. | `physical_ai_manager/src/components/Workshop/ToolbarButtons.jsx:73-76` |
| 2.6 | MEDIUM | `BreakpointList` rebinds the SVG click listener on every breakpoint toggle (dep includes `breakpoints` array). Use a ref so the listener is bound once. | `physical_ai_manager/src/components/Workshop/BreakpointList.jsx:88-92` |
| 2.7 | MEDIUM | `RunControls` IK warnings effect attaches warnings via `setWarningText` but never clears them. Old warnings persist on blocks across runs. | `physical_ai_manager/src/components/Workshop/RunControls.jsx:51-63` |
| 2.8 | MEDIUM | `setActiveTutorial({id: null})` doesn't clear `restrictedBlocks` inside the reducer — relies on a follow-up dispatch from SkillmapPlayer. If the component unmounts in the same tick, the toolbox stays restricted. Clear in the reducer. | `physical_ai_manager/src/features/workshop/workshopSlice.js:setActiveTutorial` |
| 2.9 | MEDIUM | PDF export button — `jsPDF` + `html-to-image` are in `package.json` but no button or generator is wired. Plan §6.6 specified it. | `ToolbarButtons.jsx` + new `pseudoCodeDeGenerator.js` |
| 2.10 | MEDIUM | LAB color debug overlay — show the captured LAB center + k×σ envelope on the camera feed when `edubotics_detect_color` runs. Helps students see why a yellow banana sometimes counts as red. | `physical_ai_manager/src/components/Workshop/CameraFeedOverlay.jsx` + new debug-overlay sub-component |
| 2.11 | LOW | Toolbar `Ctrl+Shift+E` keyboard shortcut for export mentioned in a comment but never wired. | `physical_ai_manager/src/components/Workshop/ToolbarButtons.jsx:79` |
| 2.12 | LOW | Cloud-only mode (`?cloud=1`) doesn't short-circuit WorkshopPage — it tries to call ROS services and the wizard never loads. Add a banner: "Roboter Studio benötigt eine Roboter-Verbindung." | `physical_ai_manager/src/pages/WorkshopPage.js` |
| 2.13 | LOW | `CameraFeedOverlay` uses `window.prompt` (blocks rendering, no Esc, no keyboard). Replace with a small modal. | `physical_ai_manager/src/components/Workshop/CameraFeedOverlay.jsx:74` |
| 2.14 | LOW | `CalibrationHistoryTab.jsx` referenced in the plan was never created. The `CalibrationHistory.srv` backend response shape is ready; just need the UI. | new `physical_ai_manager/src/components/Workshop/CalibrationHistoryTab.jsx` |

---

## 3. Tutorial polish (LOW)

| # | Severity | Item | Where |
|---|---|---|---|
| 3.1 | LOW | `CALIB_DIVERSITY_HINT` says "20 Bilder" but `framesRequired` defaults to 12 (or 14 for handeye). Fix the count. | `physical_ai_manager/src/components/Workshop/blocks/messages_de.js:166` |
| 3.2 | LOW | `roten_wuerfel_aufnehmen.json:9` — explain that "ziel" is a list (not a single detection) since `edubotics_detect_color` returns a list. | tutorials |
| 3.3 | LOW | `zaehle_blaue_objekte.json` step 3 — body says "wiederhole 3 mal" but the shadow default is 10. Add "ändere die 10 auf 3" hint. | tutorials |
| 3.4 | LOW | `stapele_drei_wuerfel.json` step 3 — body says "Funktion mit Ziel als Parameter" but doesn't tell the student where the `+` button is on `procedures_defnoreturn`. | tutorials |
| 3.5 | LOW | `sage_hallo.json` step 3 mixes `edubotics_play_sound` (legacy) with the new `edubotics_play_tone` taxonomy. Pick one consistently. | tutorials |

---

## 4. Cloud / DB hardening (MEDIUM/LOW)

| # | Severity | Item | Where |
|---|---|---|---|
| 4.1 | MEDIUM | `/me/tutorial-progress/{tutorial_id}` PATCH lets a student set `completed: false` arbitrarily — tampering vector if completion is graded. Gate "uncomplete" behind a teacher route. | `cloud_training_api/app/routes/me.py:update_tutorial_progress_endpoint` |
| 4.2 | MEDIUM | `RateLimitMiddleware` prefix matching uses `startswith()` — a POST to `/vision/detectors` (hypothetical) matches `/vision/detect`. Anchor with `path == prefix or path.startswith(prefix + "/")`. | `cloud_training_api/app/main.py:193` |
| 4.3 | MEDIUM | `_RATE_LIMIT_RULES` bucket dict grows without bound across uvicorn lifetime. Add periodic GC: every N inserts, drop buckets whose newest entry is older than `window_s`. | Same file, `RateLimiter` |
| 4.4 | MEDIUM | `_client_ip` trusts `X-Forwarded-For` unconditionally. A request hitting `*.railway.app` directly with a spoofed XFF burns the victim's rate-limit bucket. Trust XFF only when `request.client.host` is in a known proxy CIDR. | Same file, `_client_ip` |
| 4.5 | MEDIUM | `/vision/detect` rate-limits per-IP. A 30-student classroom behind one NAT shares a bucket. Switch to per-user keying. | Same file |
| 4.6 | MEDIUM | Modal cold-start failure can return a 500 OK with `{detections: [], error: ...}` — the user gets degraded results AND consumes quota. Refund quota when `detections == []` AND `cold_start == True`. | `cloud_training_api/app/routes/vision.py` |
| 4.7 | MEDIUM | Image base64 validator decodes the bytes twice (once in validator, once at line ~170). Cache decoded bytes on the model. | `cloud_training_api/app/routes/vision.py` |
| 4.8 | MEDIUM | `vision_app.py` `cold_start = not getattr(self, "_warmed", False)` is per-container, but `_warmed` survives a snapshot resume → reports `False` on the first post-resume call. Record a wall-time-since-setup heuristic instead. | `modal_training/vision_app.py:detect` |
| 4.9 | LOW | Modal `score_threshold` default 0.10 surfaces many OWLv2 false positives. 0.20-0.30 is more typical for German prompts. | Same file |
| 4.10 | LOW | `me.py:list_tutorial_progress` doesn't paginate — unbounded with 100+ tutorials. | `cloud_training_api/app/routes/me.py` |
| 4.11 | LOW | `tutorial_progress.touch_tutorial_progress_updated_at` lacks `SET search_path = public`. Audit §J11. | `016_tutorial_progress.sql` |

---

## 5. Code hygiene (LOW)

| # | Severity | Item | Where |
|---|---|---|---|
| 5.1 | LOW | `STATEMENT_HANDLERS: dict[str, callable]` — `callable` is the built-in function, not a type. Should be `Callable[..., Any]`. | `overlays/workflow/handlers/__init__.py:18,38` |
| 5.2 | LOW | `WorkflowContext.motion_lock` declared `threading.Lock` but actual is `threading.RLock`. Type lies. | `overlays/workflow/workflow_manager.py:96` |
| 5.3 | LOW | `_eval_value` is ~170 lines with long if/elif. Refactor to a per-type evaluator dict for maintainability. | `overlays/workflow/interpreter.py` |
| 5.4 | LOW | `_pause_for_breakpoint` has a duplicate `from physical_ai_server.workflow.handlers.motion import WorkflowError` local import. Already imported at module top. | `overlays/workflow/interpreter.py:476` |
| 5.5 | LOW | `BroadcastShim` class is dead code (legacy compat) after the broadcast counter refactor. Delete. | `overlays/workflow/workflow_manager.py:782-813` |
| 5.6 | LOW | `cv2.FileStorage` writes (`_solve_intrinsic`, `_solve_handeye`) don't wrap `fs.release()` in `try/finally`. A mid-write exception leaks the fd + leaves corrupt YAML. | `overlays/workflow/calibration_manager.py:334-341, 408-416` |
| 5.7 | LOW | `safety_envelope.py` uses `print()` instead of a logger. Document that this is intentional (so logs land in `docker logs` even if logging config breaks) or migrate to logger. | `overlays/workflow/safety_envelope.py` |
| 5.8 | LOW | `vision_app.py:smoke_test` calls `__import__("torch")` twice when `torch` was already imported a line earlier. Cosmetic. | `modal_training/vision_app.py:217-218` |

---

## 6. A11y / WCAG 2.2 AA (MEDIUM/LOW)

| # | Severity | Item | Where |
|---|---|---|---|
| 6.1 | MEDIUM | `BreakpointList` × close button is below 24×24 px target. Add `min-w-[24px] min-h-[24px]`. | `BreakpointList.jsx:121` |
| 6.2 | MEDIUM | `SkillmapPlayer` ▶ play buttons have no `aria-label` — only emoji content. SR reads "play button" with no tutorial context. | `SkillmapPlayer.jsx:158-165` |
| 6.3 | MEDIUM | `CameraFeedOverlay` clickable div lacks `role="button" tabIndex={0}` + `onKeyDown` — no keyboard access. | `CameraFeedOverlay.jsx` |
| 6.4 | MEDIUM | `ColorProfileStep` color swatch span lacks `aria-label` — SR users can't tell colors apart. | `ColorProfileStep.jsx:130` |
| 6.5 | MEDIUM | Multiple Tailwind animations (`transition-all`, `transition-colors`, `bg-yellow-50 transition-colors` on variable flash) aren't gated by `motion-safe:`. | scattered |
| 6.6 | LOW | `DebugPanel` `<div role="tabpanel">` lacks `aria-labelledby` to link to the active tab's button. | `DebugPanel.jsx:62` |
| 6.7 | LOW | `RunControls` badge wrapper has `aria-live="polite"` but on the whole subtree including emoji dots — SR re-reads emojis. Move to a text-only sibling. | `RunControls.jsx:269` |
| 6.8 | LOW | German text-expansion budget — every label should have ~30% headroom over its English equivalent. Audit toolbar buttons + dialog headings. | scattered |
| 6.9 | LOW | Right-click breakpoint hint string mentions "Rechtsklick" but only Alt+click is wired. Either implement right-click context menu or update the string. | `messages_de.js:DEBUG_BP_TOGGLE_HINT` |

---

## 7. Tooling / docs (LOW)

| # | Severity | Item | Where |
|---|---|---|---|
| 7.1 | LOW | `tools/eval_perception.py`, `tools/capture_eval_set.py`, `tools/onnx_smoke.py`, `tools/eval_open_vocab.py` are referenced in `tools/perception_eval.md` + `tools/dfine_finetune.md` but not present. Write them or update the docs (the "current state" headers help but the script paths still suggest readiness). | `tools/` |
| 7.2 | LOW | `D-FINE-N` ONNX is not actually downloaded by the Dockerfile. The env-flag works; the file just isn't in the image. Add `RUN curl -fL ... dfine_n.onnx && echo "<sha256>  dfine_n.onnx" \| sha256sum -c` once a hosted URL exists. | `robotis_ai_setup/docker/physical_ai_server/Dockerfile` |
| 7.3 | LOW | CLAUDE.md §14 drift table still lists `0.8.2` for `package.json` — we bumped to `0.9.0`. Update. | `Testre/CLAUDE.md` |
| 7.4 | LOW | CLAUDE.md §13.6 "Ask before:" list should mention applying Supabase migrations — they're irreversible (well, with rollback files, but mid-deploy is a moment of risk). | `Testre/CLAUDE.md` |
| 7.5 | LOW | Plan §6.6 PDF export — `jsPDF` and `html-to-image` are in package.json deps but no code uses them yet. Remove until needed, or implement. | `package.json` |
| 7.6 | LOW | The cost estimate for the cloud-vision path is given as three slightly different numbers across `vision_app.py:21-26`, the plan, and `CLAUDE.md` §8.3. Reconcile. | various |

---

## 8. Where to start

Recommended order, biggest student-impact first:

1. **Item 1.1–1.3** (server-side ROS handlers + SensorSnapshot publisher) — without these the entire phase-2 debugger and live calibration UX is dead.
2. **Item 1.4** (cloud_vision wiring) — without it, the open-vocab block + tutorial 6 don't work.
3. **Item 1.5–1.6** (frame quality + calibration history) — visible calibration UX upgrades.
4. **Item 2.1–2.2** (audio context + topic-unsubscribe leaks) — student-visible mute + memory bloat over long sessions.
5. **Item 4.6** (refund quota on cold-start empty results) — fairness fix.
6. **Item 2.10** (LAB color debug overlay) — pedagogically valuable.
7. **Item 2.9** (PDF export) — teacher-requested feature.
8. **Item 1.7** + 4.x cleanup — DB hygiene.
9. **Item 5.x and 6.x** — quality + a11y polish.
10. **Item 7.x** — docs + tooling.

Each item is independently shippable; pick the highest-impact subset that fits the next sprint.
