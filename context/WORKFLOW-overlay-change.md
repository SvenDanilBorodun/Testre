# WORKFLOW: Modifying an Overlay

> Strict checklist for editing any of the 7 overlays + 1 patch in `docker/`. Follow step by step.
> Read [`WORKFLOW.md`](WORKFLOW.md) first for the master rules.

The overlay system is the most fragile part of the infrastructure. One mistake and the build either silently ships unmodified upstream code, or fails loudly. **Always prefer fail-loud.**

---

## §1 — Inventory of overlays

### `physical_ai_server` overlays (5 files)
Located in `robotis_ai_setup/docker/physical_ai_server/overlays/`:
- `inference_manager.py` — replaces `physical_ai_server/inference/inference_manager.py`
- `data_manager.py` — replaces `physical_ai_server/data_processing/data_manager.py`
- `data_converter.py` — replaces `physical_ai_server/data_processing/data_converter.py`
- `omx_f_config.yaml` — replaces config (any path match)
- `physical_ai_server.py` — replaces `physical_ai_server/physical_ai_server.py`

### `physical_ai_server` patches (1 file)
`robotis_ai_setup/docker/physical_ai_server/patches/fix_server_inference.py` — pre-overlay regex patch on `server_inference.py` (init `_endpoints` + remove duplicate InferenceManager construction). Self-verifies.

### `open_manipulator` overlays (2 files)
Located in `robotis_ai_setup/docker/open_manipulator/overlays/`:
- `omx_f.ros2_control.xacro` — replaces follower's ros2_control xacro
- `hardware_controller_manager.yaml` — replaces follower's controller config

---

## §2 — Read first

Read in this order:
1. [`15-docker.md`](15-docker.md) §5 (Dockerfile mechanism), §6 (overlay catalog), §7 (patch script)
2. [`17-ros2-stack.md`](17-ros2-stack.md) §12 (overlay catalog with behavioral diffs)
3. The **upstream** version of the file you're about to overlay:
   - Either pull the base image: `docker run --rm robotis/physical-ai-server:amd64-0.8.2 cat /root/ros2_ws/src/.../inference_manager.py`
   - Or check `Testre/_upstream/` for the reference snapshot
4. The **current overlay** version (so you understand what's already changed)

---

## §3 — Decide the change scope

Two types of overlay changes:

### Type A: Modify existing overlay logic
You're editing one of the 5+2 existing overlays. Easier. Skip to §5.

### Type B: Add a new overlay
You're replacing an upstream file that's not currently overlaid. Requires:
- Adding the file to `overlays/`
- Adding an `apply_overlay <name> "<path_filter>"` line in the Dockerfile
- Verifying the path filter matches exactly one upstream file (or N files, if you intend to replace N copies)

Continue to §4 for Type B.

---

## §4 — Adding a new overlay (Type B)

### a. Identify the upstream file path

```bash
# Inside the base image:
docker run --rm robotis/physical-ai-server:amd64-0.8.2 \
    bash -c "find /root/ros2_ws -name 'TARGET_FILE.py'"
# Should print exactly one path. If empty: file doesn't exist (typo? wrong base image?)
# If multiple: pick a path filter that narrows to the right one.
```

### b. Choose path filter

The filter narrows `find`'s scope. Examples:
- `*/inference/*` matches `/root/ros2_ws/src/physical_ai_server/inference/inference_manager.py`
- `*/data_processing/*` matches `/root/ros2_ws/src/physical_ai_server/data_processing/*`
- `""` (empty) matches anywhere — use only when the filename is unique

**Make the filter as specific as possible** so a future upstream restructure doesn't match the wrong file silently.

### c. Copy the upstream file as your starting point

```bash
docker run --rm robotis/physical-ai-server:amd64-0.8.2 \
    cat /root/ros2_ws/src/.../TARGET_FILE.py \
    > robotis_ai_setup/docker/physical_ai_server/overlays/TARGET_FILE.py
```

This gives you a baseline diff. Any subsequent edits are intentional changes vs upstream.

### d. Add to Dockerfile

`physical_ai_server/Dockerfile` (the COPY line near top + the `apply_overlay` chain):

```dockerfile
# Add to COPY directives:
COPY overlays/TARGET_FILE.py /tmp/overlays/TARGET_FILE.py

# Add to apply_overlay chain:
apply_overlay TARGET_FILE.py "*/SOMEDIR/*" && \
```

### e. Test the build

```bash
cd robotis_ai_setup/docker
REGISTRY=nettername ./build-images.sh
```

Watch for:
- `Overlaid: /root/ros2_ws/...TARGET_FILE.py (sha256-old → sha256-new)` — success
- `Overlay already in place: ...` — sha256 matched (didn't actually replace; your overlay is identical to upstream — fine if intentional)
- `ERROR: TARGET_FILE.py not found in base image` — path filter wrong
- `ERROR: overlay cp failed on ...` — copy corruption (rare; usually filesystem issue)

---

## §5 — Modifying an existing overlay (Type A or after §4)

### a. Make minimal changes

The overlay is already a divergence from upstream. Add the minimum new behavior. **Don't refactor unrelated code** — that makes future upstream-merge harder.

### b. Add the safety boilerplate (where applicable)

If your change adds a safety check (joint clamp, NaN guard, etc.), make it **fail loud, not silent**:

```python
# GOOD: explicit failure
if action_array.shape != expected_shape:
    raise RuntimeError(
        f"Inferenz fehlgeschlagen: Action-Tensor hat Shape {action_array.shape}, "
        f"erwartet wird {expected_shape}"
    )

# BAD: silent skip
if action_array.shape != expected_shape:
    return None
```

(Returning None is OK when it's documented behavior the caller handles, like the camera mismatch in `inference_manager.py`. Otherwise raise.)

### c. Use German for student-visible errors

If the error reaches the React UI via TaskStatus.error or training row error_message, write it in German. Use proper umlauts (`ä ö ü ß`, NOT `ae oe ue ss`).

```python
raise RuntimeError(
    "Das Modell erwartet die Kameras {expected}, "
    "aber verbunden sind nur {provided}. Bitte überprüfe die Kamera-Zuordnung."
)
```

### d. Add docstring noting the overlay reason

At the top of any function you modify substantially, add a docstring or comment:

```python
def predict(self, images, state, ...):
    """
    Overlay: validates exact camera-name match (no silent alphabetical remap).
    See WORKFLOW-overlay-change.md and 17-ros2-stack.md §12.
    """
```

This helps future maintainers understand WHY the overlay diverges from upstream.

---

## §6 — Build + verify

```bash
cd robotis_ai_setup/docker
REGISTRY=nettername ./build-images.sh
```

**Watch the output for**:
- `Overlaid: <path> (sha256-old → sha256-new)` — your change landed
- `Overlay already in place` — you didn't actually change anything (or your edit produced an identical sha256, which is unlikely)
- Build errors → fix and rebuild

After build:
- [ ] Verify overlay sha256 inside the new image:
  ```bash
  docker run --rm nettername/physical-ai-server:latest \
      sha256sum /root/ros2_ws/src/.../inference_manager.py
  ```
- [ ] Compare against your overlay file:
  ```bash
  sha256sum robotis_ai_setup/docker/physical_ai_server/overlays/inference_manager.py
  ```
  Should match.

---

## §7 — End-to-end smoke test

Overlays affect runtime behavior. Type-checks don't catch this.

For overlays in `physical_ai_server`:
1. Start the full container stack: `wsl -d EduBotics -- docker compose up -d`
2. Connect to a real robot arm OR mock with replay
3. Trigger the affected behavior:
   - **inference_manager**: start inference with a trained policy → check arm moves correctly + camera mismatch raises German error
   - **data_manager**: record an episode → finish → verify mp4 + parquet on disk + no German `[FEHLER]` in logs
   - **data_converter**: record with leader arm → verify joint reordering correct
   - **omx_f_config.yaml**: check `ros2 topic echo /task/status` shows correct camera names
4. Check logs:
   ```bash
   wsl -d EduBotics -- docker logs physical_ai_server --tail 100
   ```

For overlays in `open_manipulator`:
1. Restart the open_manipulator container with hardware connected
2. Verify entrypoint phases complete without timeout
3. Verify follower reaches sync target within 0.08 rad tolerance (overlay-added check)
4. Check `ros2 control list_controllers` shows expected controllers

---

## §8 — If the build fails with `ERROR: ... not found`

This is the M14 fail-loud assertion working correctly. ROBOTIS upstream renamed/moved a file.

### Recovery procedure

1. **Don't** comment out the assertion. That's how silent breakage starts.
2. Find the new upstream path:
   ```bash
   docker run --rm robotis/physical-ai-server:amd64-X.Y.Z \
       bash -c "find /root/ros2_ws -name 'TARGET_FILE.py'"
   ```
3. Update the path filter in `apply_overlay`:
   ```dockerfile
   apply_overlay TARGET_FILE.py "*/NEW_DIR/*"
   ```
4. **Inspect the new upstream file** — has the API changed? Are the lines you overlay still in the same place? You may need to update the overlay too.
5. Rebuild + smoke test.

---

## §9 — If the patch script self-check fails

The patch `fix_server_inference.py` exits with codes 2 or 3 if its post-checks fail (no-op detection).

### Recovery procedure

1. Read the patch source: `robotis_ai_setup/docker/physical_ai_server/patches/fix_server_inference.py`
2. The two regexes target:
   - "init `_endpoints = {}` before first register_endpoint"
   - "remove duplicate InferenceManager construction"
3. Open the new upstream `server_inference.py`:
   ```bash
   docker run --rm robotis/physical-ai-server:amd64-X.Y.Z \
       cat /root/ros2_ws/src/.../server_inference.py
   ```
4. Has upstream fixed the bugs? If yes: the patch can be removed.
5. Has upstream just reformatted the code? Update the regex to match the new format.
6. **Always preserve the post-check**. Even if you update the regex, keep the `if "..." not in content: sys.exit(2)` check.

---

## §10 — Documentation update

Mandatory after merging an overlay change:

- [ ] [`17-ros2-stack.md`](17-ros2-stack.md) §12 — describe the new behavior added
- [ ] [`15-docker.md`](15-docker.md) §6 — overlay catalog table (if behavior summary needs an update)
- [ ] [`02-pipeline.md`](02-pipeline.md) — if the change affects recording or inference flow
- [ ] [`21-known-issues.md`](21-known-issues.md) — if your change addresses a known issue, mark it fixed (delete the entry, note in commit message)

---

## §11 — Anti-patterns (don't do these)

- **Don't** comment out an `apply_overlay` assertion. The fail-loud is a feature, not a bug.
- **Don't** add a new overlay without sha256 verification.
- **Don't** modify `physical_ai_tools/lerobot/` — it must remain byte-identical to upstream `989f3d05`. If you need to change LeRobot behavior, it goes in your application code (e.g., overlay) not LeRobot itself.
- **Don't** chain multiple unrelated changes into one overlay. One overlay file = one logical concern.
- **Don't** add German error messages for backend-only paths (logs the maintainer reads). Reserve German for student-facing errors.
- **Don't** silently swallow exceptions. Re-raise with context, or at minimum log + handle explicitly.
- **Don't** skip the smoke test. Type checks won't catch silent dataset breakage.
- **Don't** push the rebuilt image to `nettername/*:latest` until smoke test passes.

---

## §12 — Patch script changes

The patch script `fix_server_inference.py` is special because it runs BEFORE overlays.

### Updating the patch

1. Read the current script
2. If you need a new fix: add a new function/regex with its own self-check (`sys.exit(N)` for new code N)
3. Test against the upstream file:
   ```bash
   docker run --rm robotis/physical-ai-server:amd64-0.8.2 cat /root/.../server_inference.py > /tmp/upstream.py
   python robotis_ai_setup/docker/physical_ai_server/patches/fix_server_inference.py /tmp/upstream.py
   echo $?    # 0 = success
   diff /tmp/upstream.py /tmp/upstream.py.original  # what changed
   ```
4. Verify the post-check would catch a no-op (e.g., delete the regex match temporarily, ensure it exits non-zero)

---

## §13 — Cross-references

- Master rules: [`WORKFLOW.md`](WORKFLOW.md)
- Build mechanism: [`15-docker.md`](15-docker.md) §3-7
- ROS2 behavioral context: [`17-ros2-stack.md`](17-ros2-stack.md)
- Recording / inference pipelines: [`02-pipeline.md`](02-pipeline.md) §5, §8
- Base image pinning: [`docker/BASE_IMAGE_PINNING.md`](../robotis_ai_setup/docker/BASE_IMAGE_PINNING.md)
- Bumping base images: [`WORKFLOW-replace-or-upgrade.md`](WORKFLOW-replace-or-upgrade.md) §3
- Known issues this layer mitigates: [`21-known-issues.md`](21-known-issues.md) §3.3, §3.5, §3.8
