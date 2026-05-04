# WORKFLOW: Debugging

> **It's broken — where do I look first?**
> Per-stage triage with concrete commands. Read [`WORKFLOW.md`](WORKFLOW.md) first for the master rules.

---

## §1 — General triage rules

1. **Read the full error message.** Don't summarize, don't infer — read it character by character.
2. **Locate the layer.** [`00-INDEX.md`](00-INDEX.md) §4 routing table maps symptoms to files.
3. **Check the version.** Frequently the wrong image is running, the wrong .env is loaded, or the user has stale cache. Confirm you're debugging the version you think you are.
4. **Reproduce locally before fixing.** A fix without reproduction is a guess.
5. **Check [`21-known-issues.md`](21-known-issues.md)** for the area — it might already be a documented bug.

---

## §2 — Symptom → first-file triage

### "I just clicked install and it failed"

```bash
# Read the install log
notepad %TEMP%\Setup\ "Setup Log YYYY-MM-DD #001.txt"

# Read each PowerShell transcript
notepad %TEMP%\edubotics_install_*.log
```

Likely files: [`16-installer-wsl.md`](16-installer-wsl.md) → which step failed. Most common:
- WSL2 install needs reboot → user didn't reboot before retry. Check `.reboot_required` marker.
- Distro import: out of disk space (need 20 GB) or SHA256 mismatch
- Image pull: stall or network issue (check pull stall watchdog log)

### "The GUI starts but freezes during 'Arme scannen'"

```bash
# Check if usbipd attached
usbipd list

# Check distro can see the device
wsl -d EduBotics -- ls /dev/serial/by-id/

# Check the scanner container
wsl -d EduBotics -- docker logs robotis_arm_scanner

# Or run identify_arm.py manually
wsl -d EduBotics -- docker run --rm --privileged -v /dev:/dev \
    nettername/open-manipulator:latest \
    python3 /usr/local/bin/identify_arm.py /dev/serial/by-id/usb-ROBOTIS_OpenRB-150_...
```

Likely files: [`14-windows-gui.md`](14-windows-gui.md) §9 (DeviceManager workflow), [`17-ros2-stack.md`](17-ros2-stack.md) §3.

### "Arm doesn't move on startup"

```bash
# Watch the entrypoint phases
wsl -d EduBotics -- docker logs open_manipulator --tail 200 -f

# Phases (look for these markers in order):
# 1. "FOLLOWER_PORT/LEADER_PORT detected"
# 2. Leader launch + "/leader/joint_states received"
# 3. Leader position read (JSON dump)
# 4. Follower launch + sync trajectory ("publishing quintic trajectory...")
# 5. Sync verification ("follower reached target within tolerance" or "ERROR: tolerance exceeded")
# 6. Camera launches
```

Likely files: [`17-ros2-stack.md`](17-ros2-stack.md) §6 (entrypoint phases), [`docker/open_manipulator/entrypoint_omx.sh`](../robotis_ai_setup/docker/open_manipulator/entrypoint_omx.sh).

If sync verification fails (exit 2): joint trajectory tolerances + servo current limits in the xacro overlay. See [`17-ros2-stack.md`](17-ros2-stack.md) §5.

### "Compose fails to start"

```bash
# Validate compose syntax
wsl -d EduBotics --cd /mnt/c/Program\ Files/EduBotics/docker -- docker compose config

# Check .env values
type %LOCALAPPDATA%\EduBotics\.env

# Read logs from each service
wsl -d EduBotics -- docker compose -f /mnt/c/.../docker-compose.yml logs --tail 100
```

Likely files: [`15-docker.md`](15-docker.md) §4, [`14-windows-gui.md`](14-windows-gui.md) §11 (config_generator).

Common culprits:
- Bind-mount `/etc/timezone` failed → tzdata missing in rootfs (very rare; would have failed at install)
- Image pull failed → `docker compose pull` to retry
- `.s6-keep` mount path invalid → check `physical_ai_server/.s6-keep` exists in install dir

### "Recording crashes mid-session"

```bash
# Watch task status
wsl -d EduBotics -- docker exec physical_ai_server bash -c "
    source /opt/ros/jazzy/setup.bash &&
    ros2 topic echo /task/status
"

# Recording-specific overlay errors (in German):
wsl -d EduBotics -- docker logs physical_ai_server --tail 200 | grep -E "(FEHLER|RuntimeError|Traceback)"
```

Likely files: [`17-ros2-stack.md`](17-ros2-stack.md) §10 (state machine), §8 (DataManager), [`21-known-issues.md`](21-known-issues.md) §3.5.

Common errors:
- "JointTrajectory hat keine Punkte" → leader arm not publishing
- Missing joint error → robot config + arm hardware mismatch
- "Kameras..." mismatch → `omx_f_config.yaml` camera names vs running cameras
- RAM truncation → free up disk / RAM (overlay forces early save when &lt;2GB free)

### "React UI shows but says 'Server nicht erreichbar'"

```bash
# Check rosbridge is listening
wsl -d EduBotics -- ss -tlnp | grep 9090

# Check from React side
curl http://localhost:9090/

# Container logs
wsl -d EduBotics -- docker logs physical_ai_server --tail 100 | grep rosbridge

# Healthcheck status
wsl -d EduBotics -- docker inspect physical_ai_server -f '{{.State.Health.Status}}'
```

Likely files: [`13-frontend-react.md`](13-frontend-react.md) §5 (rosConnectionManager), [`15-docker.md`](15-docker.md) §4 (compose healthcheck).

### "/trainings/start returns 400 / 403 / 500"

```bash
# Tail Railway logs
railway logs --service cloud_training_api

# What the request looked like (browser DevTools or curl):
curl -X POST https://scintillating-empathy-production-9efd.up.railway.app/trainings/start \
    -H "Authorization: Bearer $JWT" \
    -H "Content-Type: application/json" \
    -d '{"dataset_name":"foo/bar","model_type":"act","training_params":{"steps":1000}}'
```

Status codes:
- **400**: Pydantic validation. Check request body. Common: dataset_name missing `/`, model_type not in ALLOWED_POLICIES.
- **403 "No training credits remaining"**: P0003 from RPC. Check `users.training_credits` for the user.
- **404 "User profile not found"**: P0002. User row in `public.users` is missing (auth.users created without trigger firing? rare).
- **502 "HuggingFace Hub is temporarily unavailable"**: HF transient. Retry.
- **500**: Modal dispatch failed → check `modal app logs edubotics-training`. Or RPC failed → check Supabase logs.

Likely files: [`10-cloud-api.md`](10-cloud-api.md), [`12-supabase.md`](12-supabase.md) §7 (RPCs).

### "Training is stuck `running` forever"

See [`20-operations.md`](20-operations.md) §2 — full procedure with SQL.

Quick check:
```bash
modal app logs edubotics-training | grep <training_id>
```

Likely files: [`11-modal-training.md`](11-modal-training.md), [`10-cloud-api.md`](10-cloud-api.md) §6 (`_sync_modal_status`).

### "Inference crashes with 'erwartet Kameras...'"

This is the camera exact-match overlay rejecting a camera mismatch. Either:
- The model was trained with different camera names than what's now connected → check policy `config.json` `input_features` vs your `omx_f_config.yaml`
- The .env has wrong CAMERA_NAME_1 / CAMERA_NAME_2 → regenerate via GUI

```bash
# Show policy config
wsl -d EduBotics -- docker exec physical_ai_server cat \
    /root/.cache/huggingface/hub/models--<repo>/snapshots/*/pretrained_model/config.json | jq .input_features

# Show current camera config
type %LOCALAPPDATA%\EduBotics\.env | findstr CAMERA_NAME
```

Likely files: [`17-ros2-stack.md`](17-ros2-stack.md) §11 (inference loop), `overlays/inference_manager.py`.

### "WebView2 doesn't open the UI"

Symptom: GUI says "Web-Oberfläche wird im EduBotics-Fenster geöffnet" but nothing appears.

```bash
# Check Edge WebView2 is installed (Win11 ships it, but sometimes missing)
reg query "HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

# Check the watchdog log (if recently launched)
# Look at GUI log pane for "Web-Oberfläche fallback"
```

Falls back to system browser via `webbrowser.open()`. Likely files: [`14-windows-gui.md`](14-windows-gui.md) §7.

### "GUI throws 'Sitzung abgelaufen' immediately"

The Railway API rejected the JWT. Possible causes:
- JWT expired (check Supabase Auth setting "JWT expiry" — default 3600s)
- Service-role key was rotated and React still has old anon key cached → hard reload (Ctrl+Shift+R)
- Network proxy stripping `Authorization` header

### "Docker pull stalls forever"

Check the pull stall watchdog: in the GUI log pane, look for stdout-line-rate + disk-growth output.

```bash
# Manual pull with verbose
wsl -d EduBotics -- docker pull nettername/physical-ai-server:latest --quiet=false
```

If repeatedly stalling: the watchdog will kill dockerd + retry up to 4 times with exp backoff. After 4 failures: `_reset_dockerd()` is called.

Likely files: [`14-windows-gui.md`](14-windows-gui.md) §8 (pull stall watchdog).

---

## §3 — Logs cheatsheet

| Surface | Command |
|---|---|
| Railway FastAPI | `railway logs --service cloud_training_api` (or dashboard) |
| Modal worker | `modal app logs edubotics-training` |
| Modal MCP | `modal app logs example-mcp-server-stateless` |
| Container `physical_ai_server` | `wsl -d EduBotics -- docker logs physical_ai_server --tail 200 -f` |
| Container `open_manipulator` | `wsl -d EduBotics -- docker logs open_manipulator --tail 200 -f` |
| Container `physical_ai_manager` (nginx) | `wsl -d EduBotics -- docker logs physical_ai_manager --tail 200 -f` |
| WSL2 dockerd | `wsl -d EduBotics -- cat /var/log/dockerd.log` |
| Dockerd watchdog | `wsl -d EduBotics -- cat /var/log/dockerd-watchdog.log` |
| Windows GUI log | tkinter log pane (in-app), or `%TEMP%\edubotics_*.log` for elevated steps |
| Inno Setup install | `%TEMP%\Setup\Setup Log *.txt` |
| Supabase | dashboard → Logs (auth, postgres, realtime, functions) |

---

## §4 — Common diagnostic commands

### Check ROS topics inside container

```bash
wsl -d EduBotics -- docker exec physical_ai_server bash -c "
    source /opt/ros/jazzy/setup.bash &&
    source /root/ros2_ws/install/setup.bash &&
    ros2 topic list &&
    ros2 topic hz /joint_states &&
    ros2 topic echo /task/status --once
"
```

### Check overlay was applied

```bash
wsl -d EduBotics -- docker exec physical_ai_server sha256sum \
    /root/ros2_ws/src/.../inference_manager.py
# Compare against host:
sha256sum robotis_ai_setup/docker/physical_ai_server/overlays/inference_manager.py
# Should match
```

If they don't match → overlay didn't apply during build → rebuild.

### Check what env the API is running with

```bash
railway variables    # lists env vars (some may be redacted)
railway run env | grep -E "(SUPABASE|MODAL|HF_TOKEN)"  # alternative
```

### Verify Modal Secret

```bash
modal secret list
# Show vars (not values):
modal secret create edubotics-training-secrets --from-dotenv .env --force  # to update
```

### Inspect Supabase row state

```bash
psql $SUPABASE_DATABASE_URL -c "
    SELECT id, status, current_step, total_steps, last_progress_at, error_message
    FROM trainings WHERE id = $TRAINING_ID;
"
```

### Inspect React build version

```bash
curl http://localhost/version.json
# {"buildId": "20260504-abcd123", "builtAt": "..."}
# Compare against `git rev-parse --short HEAD`
```

### Snapshot WSL2 distro state

```powershell
wsl --list --verbose                    # all distros + state
wsl -d EduBotics -- df -h               # disk usage in distro
wsl -d EduBotics -- docker ps -a        # all containers
wsl -d EduBotics -- docker images       # image inventory
wsl -d EduBotics -- docker volume ls    # named volumes
```

---

## §5 — Reproducing on a clean machine

When debugging an installer issue, **never** test on your dev machine without a snapshot. Recommended flow:

1. Hyper-V VM with Win11 Pro, 8 GB RAM, 60 GB disk
2. Snapshot post-install of clean OS
3. Run `EduBotics_Setup.exe`
4. Reproduce the issue
5. Restore snapshot, try again with the fix

Avoid:
- Testing on a machine that already has Docker Desktop
- Testing on Win11 Home (we explicitly reject — `install_prerequisites.ps1`)
- Testing on a machine without Hyper-V enabled

---

## §6 — When to escalate

If you've spent &gt;30 min on a bug and you don't have a hypothesis:

- **Stop** writing code
- Read all logs (compose, dockerd, container, Railway, Modal — see §3)
- Check [`21-known-issues.md`](21-known-issues.md) for matches
- Check `git log` for recent changes that might have introduced this
- If still stuck: tell the user what you tried, what you observed, and what you'd try next

The user has more context than the docs. Don't burn cycles on a bug they could solve in 30 seconds.

---

## §7 — Known traps (don't fall in these)

1. **"`docker info` says no docker"** in WSL2 — boot context has empty PATH. Run `start-dockerd.sh` manually.
2. **"`/etc/timezone` not found"** — tzdata missing in rootfs (would have broken install — distro is corrupt; reimport).
3. **Multi-layer pull corrupting** — Docker 29.x snapshotter regression. We pin 27.5.1.
4. **"longrun\r is not a valid type"** — CRLF in s6 service file. Dockerfile sed should have stripped it. If not: rebuild.
5. **"erwartet Kameras [a, b], aber verbunden sind nur [c, d]"** — overlay is doing its job. Check policy config.json vs your camera config.
6. **GUI stuck "Hardware-Verbindungen werden geprüft..."** — `_start_environment` thread early-returned without resetting `self.running`. Old bug; if you see it on current code, log it.
7. **`/trainings/start` 60s dedupe returning the same training ID** — your retry hit the dedupe window. Wait 60s.
8. **JWT expired** — Supabase default is 3600s. Refresh in the React app.
9. **`wsl --unregister` ate my datasets** — VHDX with named volumes is gone. Document, recover from HF push if available, set up bind-mount workaround for next student.
10. **Modal worker silent** — check Modal Secret has `SUPABASE_URL` + `SUPABASE_ANON_KEY` + `HF_TOKEN`. Empty secret = silent failure.

---

## §8 — Cross-references

- Routing by symptom: [`00-INDEX.md`](00-INDEX.md) §4
- Per-layer details: [`10-cloud-api.md`](10-cloud-api.md) … [`18-modal-mcp.md`](18-modal-mcp.md)
- Operations runbook: [`20-operations.md`](20-operations.md)
- Known issues: [`21-known-issues.md`](21-known-issues.md)
- Common gotchas table: [`02-pipeline.md`](02-pipeline.md) §9
