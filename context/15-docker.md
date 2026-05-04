# 15 — Docker / Overlays / Compose

> **Layer:** Container runtime + image build chain
> **Location:** `Testre/robotis_ai_setup/docker/` + the two `Dockerfile`s in `physical_ai_tools/physical_ai_manager/`
> **Owner:** Our code (overlays + thin layers); base images are ROBOTIS official
> **Read this before:** editing Dockerfiles, overlays, compose, build-images.sh, patches. **Always read [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md) before touching any overlay.**

---

## 1. Files

```
docker/
├── BASE_IMAGE_PINNING.md           # Strategy doc: M13 (immutable tags) + M14 (fail-loud overlays)
├── build-images.sh                 # Master build + push script
├── bump-upstream-digests.sh        # Helper to inspect upstream sha256 digests
├── docker-compose.yml              # Base compose (3 services on ros_net bridge)
├── docker-compose.gpu.yml          # GPU overlay (only physical_ai_server)
├── open_manipulator/
│   ├── Dockerfile                  # Thin layer over robotis/open-manipulator:amd64-4.1.4
│   ├── entrypoint_omx.sh           # 270-line PID-1 script (4 phases + sync verification)
│   ├── identify_arm.py             # GUI hardware-detection helper (NOT called by entrypoint)
│   └── overlays/
│       ├── omx_f.ros2_control.xacro
│       └── hardware_controller_manager.yaml
└── physical_ai_server/
    ├── Dockerfile                  # Thin layer over robotis/physical-ai-server:amd64-0.8.2
    ├── .s6-keep                    # Empty 1-byte file — bind-mounted to enable s6 service
    ├── overlays/
    │   ├── inference_manager.py    # Camera exact-match, NaN guard, joint clamp, velocity cap, stale halt
    │   ├── data_manager.py         # RAM truncation, video verify, episode validation, HF timeout
    │   ├── data_converter.py       # Empty trajectory guard, fps-aware action timing
    │   ├── omx_f_config.yaml       # Dual camera + joint order
    │   └── physical_ai_server.py   # Handles None returns from new safety envelope
    └── patches/
        └── fix_server_inference.py # Pre-overlay patch: init _endpoints + remove duplicate InferenceManager
```

---

## 2. Three-tier build chain

```
robotis/physical-ai-server:amd64-0.8.2          (ROBOTIS — ROS2 + PyTorch + LeRobot + s6)
  └─ nettername/physical-ai-server               (CRLF strip + patch + 5 overlays)

robotis/open-manipulator:amd64-4.1.4             (ROBOTIS — ROS2 + Dynamixel)
  └─ nettername/open-manipulator                 (entrypoint_omx.sh + identify_arm.py + 2 overlays)

physical_ai_tools/physical_ai_manager (build context)
  └─ nettername/physical-ai-manager              (React + nginx; REACT_APP_MODE baked at build)

nvidia/cuda:12.1.1-devel-ubuntu22.04             (CUDA base for Modal training image)
  └─ modal: edubotics-training                   (LeRobot@989f3d05 + torch cu121 + training_handler)
```

**Key principle:** ROBOTIS upstream images are pulled (or built once for open_manipulator with `BUILD_BASE=1`); our thin layer adds CRLF strip + patches + sha256-verified overlays. **LeRobot itself is NOT overlaid** — byte-identical to upstream `989f3d05`.

---

## 3. build-images.sh

`docker/build-images.sh` — master script. Read by env vars:

| Var | Default | Notes |
|---|---|---|
| `REGISTRY` | `nettername` | image prefix |
| `BUILD_BASE` | `0` | `1` rebuilds open_manipulator from source (~40 min) |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `CLOUD_API_URL` | required | passed to React build args |
| `ALLOWED_POLICIES` | `act` | passed as REACT_APP_ALLOWED_POLICIES (student build = act-only) |
| `OPEN_MANIPULATOR_DIR` / `PHYSICAL_AI_TOOLS_DIR` | auto-detected | overrides for non-standard layouts |

### Build order

1. **physical-ai-manager** — React SPA with build args (`REACT_APP_MODE=student` default, `REACT_APP_BUILD_ID=YYYYMMDD-shortSHA`)
2. **physical-ai-server** — pull `robotis/physical-ai-server:amd64-0.8.2` → thin Dockerfile
3. **open-manipulator** — pull `robotis/open-manipulator:amd64-4.1.4` (or BUILD_BASE=1) → thin Dockerfile

### Push phase

For each image, `docker push ${REGISTRY}/<name>:latest`. **Push loop aborts on first failure** — prevents shipping a half-updated image set to students. Reports what was pushed before the failure.

### Smoke checks

`set -e` at top → script exits on any unhandled error. Directory existence is checked before pull. No post-push verification (trusts Docker exit codes).

---

## 4. docker-compose.yml

3 services on `ros_net` bridge network. **All ports loopback-bound** (`127.0.0.1:<port>`) so rosbridge isn't exposed on the school LAN.

### Service 1: `open_manipulator`

```yaml
image: ${REGISTRY:-nettername}/open-manipulator:latest
container_name: open_manipulator
restart: unless-stopped
tty: true
cap_add: [SYS_NICE]
ulimits: { rtprio: 99, rttime: -1, memlock: 8428281856 }
environment:
  ROS_DOMAIN_ID: ${ROS_DOMAIN_ID:-30}
  FOLLOWER_PORT: ${FOLLOWER_PORT}
  LEADER_PORT: ${LEADER_PORT}
  CAMERA_DEVICE_1: ${CAMERA_DEVICE_1}, CAMERA_NAME_1: ${CAMERA_NAME_1}
  CAMERA_DEVICE_2: ${CAMERA_DEVICE_2}, CAMERA_NAME_2: ${CAMERA_NAME_2}
volumes:
  - /dev:/dev
  - /dev/shm:/dev/shm
  - /etc/timezone:/etc/timezone:ro
  - /etc/localtime:/etc/localtime:ro
privileged: true
mem_limit: 2g
pids_limit: 512
healthcheck:
  test: ["CMD-SHELL", "source /opt/ros/jazzy/setup.bash && ros2 topic list | grep -q joint_states"]
  interval: 10s, timeout: 5s, retries: 3, start_period: 120s
networks: [ros_net]
```

### Service 2: `physical_ai_server`

```yaml
depends_on:
  open_manipulator: { condition: service_healthy }   # waits for /joint_states
ports:
  - "127.0.0.1:8080:8080"   # web_video_server
  - "127.0.0.1:9090:9090"   # rosbridge — UNAUTHENTICATED, loopback only
volumes:
  - /dev/shm:/dev/shm        # DDS inter-container
  - ai_workspace:/workspace  # named volume — datasets, recordings
  - huggingface_cache:/root/.cache/huggingface
  - /var/run/robotis/agent_sockets/physical_ai_server:/var/run/agent
  - ./physical_ai_server/.s6-keep:/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server:ro
mem_limit: 6g
pids_limit: 1024
healthcheck:
  test: ["CMD-SHELL", "echo > /dev/tcp/127.0.0.1/9090"]   # rosbridge alive
  interval: 10s, timeout: 5s, retries: 3, start_period: 60s
```

**No /dev access** for physical_ai_server (it doesn't touch USB). Privileged for shared memory + caps.

### Service 3: `physical_ai_manager`

```yaml
depends_on:
  physical_ai_server: { condition: service_healthy }
ports: ["127.0.0.1:80:80"]
mem_limit: 512m, pids_limit: 128
healthcheck: { test: ["CMD-SHELL", "wget -q -O - http://localhost/version.json"], ... }
```

Stateless nginx, no volumes.

### `docker-compose.gpu.yml` (overlay)

10 lines, adds GPU only for `physical_ai_server`:

```yaml
services:
  physical_ai_server:
    runtime: nvidia
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]
```

GUI selects: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d` if `nvidia-smi` succeeds.

### Volumes

```yaml
volumes:
  ai_workspace:       # unbounded (no size cap)
  huggingface_cache:  # advisory cap in comments only
```

**Both volumes live inside the WSL2 distro VHDX. `wsl --unregister` destroys them** ([§9 of known-issues](21-known-issues.md)).

---

## 5. physical_ai_server/Dockerfile (thin layer)

3 operations, in order:

### Op 1: CRLF strip

```dockerfile
RUN find /etc/s6-overlay/s6-rc.d -type f \
    \( -name "*.sh" -o -name "type" -o -name "run" -o -name "up" \
       -o -name "finish" -o -name "notification-fd" -o -name "*.conf" \
       -o -name "dependencies.d" \) \
    -exec sed -i 's/\r$//' {} + && \
find /usr/local/lib/s6-services -type f -name "*.sh" \
    -exec sed -i 's/\r$//' {} +
```

Targets s6-overlay text files. **Why:** Windows Git CRLF makes s6 reject `longrun\r` as invalid type at runtime. Only text files (explicit name match), avoid binary corruption.

### Op 2: Patch script

```dockerfile
COPY patches/fix_server_inference.py /tmp/fix_server_inference.py
RUN TARGETS=$(find /root/ros2_ws -name "server_inference.py" -path "*/inference/*") && \
    [ -n "$TARGETS" ] || { echo "ERROR: server_inference.py not found"; exit 1; } && \
    for f in $TARGETS; do \
        python3 /tmp/fix_server_inference.py "$f" || \
          { echo "ERROR: patch verification failed on $f"; exit 1; } \
    done && \
    rm /tmp/fix_server_inference.py
```

Patch script must self-verify (exit non-zero on no-op). See §7 for the script.

### Op 3: Overlays via `apply_overlay()` shell function

```dockerfile
RUN set -e && \
    apply_overlay() {
        local name="$1" path_filter="$2" src="/tmp/overlays/$1"
        local targets
        if [ -n "$path_filter" ]; then
            targets=$(find /root/ros2_ws -name "$name" -path "$path_filter")
        else
            targets=$(find /root/ros2_ws -name "$name")
        fi
        [ -z "$targets" ] && {
            echo "ERROR: $name not found in base image — overlay cannot be applied"
            return 1
        }
        local expected=$(sha256sum "$src" | cut -d' ' -f1)
        for f in $targets; do
            local before=$(sha256sum "$f" | cut -d' ' -f1)
            if [ "$before" = "$expected" ]; then
                echo "Overlay already in place: $f"
                continue
            fi
            cp "$src" "$f"
            local after=$(sha256sum "$f" | cut -d' ' -f1)
            [ "$after" != "$expected" ] && {
                echo "ERROR: overlay cp failed on $f ($after != $expected)"
                return 1
            }
            echo "Overlaid: $f ($before -> $after)"
        done
    } && \
    apply_overlay inference_manager.py "*/inference/*" && \
    apply_overlay data_manager.py "*/data_processing/*" && \
    apply_overlay data_converter.py "*/data_processing/*" && \
    apply_overlay omx_f_config.yaml "" && \
    apply_overlay physical_ai_server.py "*/physical_ai_server/physical_ai_server.py" && \
    rm -rf /tmp/overlays
```

**Properties:**
- Fails loudly if `find` returns empty (M14 commitment)
- sha256-verifies before AND after copy (catches copy corruption)
- Idempotent: skips if hash already matches
- Logs each replacement with hash transition

**Adding a new overlay:** add the file to `/tmp/overlays/`, add an `apply_overlay <file> <filter>` line. See [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md).

---

## 6. The 5 overlays

| Overlay | Replaces upstream | Adds |
|---|---|---|
| `inference_manager.py` | `physical_ai_server/inference/inference_manager.py` | Camera exact-match (no silent alphabetical remap), stale-camera halt (5 s threshold), image shape validation, NaN/inf reject, joint-limit clamp, velocity rate-limit (set_action_limits API) |
| `data_manager.py` | `physical_ai_server/data_processing/data_manager.py` | RAM cushion + truncation flag, video file verification (catches silent encoder failures), episode validation (timestamp gap detection), camera-name resume check, HF upload + token timeouts |
| `data_converter.py` | `physical_ai_server/data_processing/data_converter.py` | Empty trajectory guard (German error), missing-joint context-rich error, extra-joint warning, fps-aware action message timing |
| `omx_f_config.yaml` | `physical_ai_server/config/omx_f_config.yaml` | Dual camera config (gripper + scene), exact joint order |
| `physical_ai_server.py` | `physical_ai_server/physical_ai_server.py` | Handles None returns from new safety envelope (skips publish on rejected actions) |

For source-level diffs and behavioral details, see [`17-ros2-stack.md`](17-ros2-stack.md) §12.

---

## 7. patches/fix_server_inference.py

Pre-overlay patch script. Two fixes, both with self-verification.

### Fix 1: init `self._endpoints = {}`

```python
if "self._endpoints = {}" not in content:
    content = content.replace(
        "        # Register the ping endpoint by default\n",
        "        self._endpoints = {}\n\n        # Register the ping endpoint by default\n",
    )
# Verify:
if "self._endpoints = {}" not in content:
    print("ERROR: fix 1 no-op")
    sys.exit(2)
```

### Fix 2: remove duplicate `InferenceManager` construction

Regex matches `socket.bind(...) + blank line + full InferenceManager(...)` block, replaces with just the socket.bind line.

```python
dup_pattern = re.compile(
    r"(self\.socket\.bind\([^)]+\)\n)\n"
    r"        self\.inference_manager = InferenceManager\(\n"
    r"            policy_type=policy_type,\n"
    r"            policy_path=policy_path,\n"
    r"            device=device\n"
    r"        \)\n",
)
content = dup_pattern.sub(r"\1", content)
# Verify:
if len(re.findall(r"self\.inference_manager = InferenceManager\(", content)) > 1:
    print("ERROR: fix 2 no-op")
    sys.exit(3)
```

### Exit codes
- 0: both fixes applied or already present
- 1: usage error
- 2: fix 1 no-op (post-check failed)
- 3: fix 2 no-op (post-check failed)

**Risk:** if upstream reformats `server_inference.py` (rename, indentation change, new construction), the regex stops matching → fix 2 no-ops → exit 3. Build fails. Then update the regex. **Don't bypass the assertion.**

---

## 8. open_manipulator/Dockerfile

```dockerfile
FROM robotis/open-manipulator:amd64-4.1.4
RUN apt-get update && apt-get install -y --no-install-recommends \
        v4l-utils python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --break-system-packages dynamixel-sdk==4.0.3
COPY entrypoint_omx.sh /entrypoint_omx.sh
COPY identify_arm.py /usr/local/bin/identify_arm.py
RUN sed -i 's/\r$//' /entrypoint_omx.sh /usr/local/bin/identify_arm.py \
    && chmod +x /entrypoint_omx.sh /usr/local/bin/identify_arm.py
# ... apply_overlay omx_f.ros2_control.xacro and hardware_controller_manager.yaml
ENTRYPOINT ["/entrypoint_omx.sh"]
```

For entrypoint details, see [`17-ros2-stack.md`](17-ros2-stack.md) §5.

---

## 9. physical_ai_manager Dockerfiles

### Student build (`Dockerfile`)

```dockerfile
# Stage 1: build
FROM node:22 AS build
ARG REACT_APP_SUPABASE_URL REACT_APP_SUPABASE_ANON_KEY REACT_APP_CLOUD_API_URL
ARG REACT_APP_MODE=student
ARG REACT_APP_ALLOWED_POLICIES=act
ARG REACT_APP_BUILD_ID=dev
ENV REACT_APP_MODE=$REACT_APP_MODE
# (similar for other ARGs → ENV)
COPY package*.json .
RUN npm install
COPY . .
RUN npm run build
RUN echo "{\"buildId\":\"$REACT_APP_BUILD_ID\",\"builtAt\":\"$(date -u +%FT%TZ)\"}" > build/version.json

# Stage 2: nginx
FROM nginx:1.27.5-alpine
COPY --from=build /build /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
```

### Web build (`Dockerfile.web`)

Similar but:
- `ARG REACT_APP_MODE=web`
- `ARG REACT_APP_ALLOWED_POLICIES=tdmpc,diffusion,act,vqbet,pi0,pi0fast,smolvla` (full)
- Stage 2: copies `nginx.web.conf.template` to `/etc/nginx/templates/default.conf.template`
- ENV `PORT=8080` (Railway dynamic port)
- ENV `NGINX_ENVSUBST_FILTER="^PORT$"` (only substitute $PORT, not $uri)
- nginx:alpine runs envsubst at startup, replacing `${PORT}` in template
- EXPOSE 8080

### nginx.conf (student)

```nginx
location = /index.html {
    add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;
    add_header Pragma "no-cache" always;
    expires -1;
}
location = /version.json {
    add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;
    expires -1;
}
location /static/ {
    expires 1y;
    add_header Cache-Control "public, immutable" always;
}
location / {
    try_files $uri /index.html;   # SPA fallback
}
```

### nginx.web.conf.template (web)

Same caching strategy + adds security headers (HSTS 2y, X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy strict-origin-when-cross-origin, Permissions-Policy blocks camera/mic/geo/payment). Headers re-declared in nested `location` blocks (nginx inheritance quirk).

---

## 10. The `.s6-keep` mystery

`physical_ai_server/.s6-keep` is an empty 1-byte file mounted at:

```yaml
- ./physical_ai_server/.s6-keep:/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server:ro
```

s6-overlay enables services by detecting their name as a file in `user/contents.d/`. The base image **defines** the service but leaves it disabled (no file in contents.d). The compose mount **is** how it's enabled.

**Remove the mount → server container starts but the ROS node never runs.** This is a load-bearing hack; if you refactor compose, preserve this mount.

---

## 11. Build reproducibility

### Pinned base images (M13 commitment, see `BASE_IMAGE_PINNING.md`)

| Image | Pinned tag |
|---|---|
| physical_ai_server base | `robotis/physical-ai-server:amd64-0.8.2` |
| open_manipulator base | `robotis/open-manipulator:amd64-4.1.4` |
| Modal training base | `nvidia/cuda:12.1.1-devel-ubuntu22.04` |

**Never** use `:latest` for base images. ROBOTIS retagging silently can ship a different ROS2 distro / LeRobot version / Python version / file paths.

### Upgrade workflow (when intentionally bumping)

See [`WORKFLOW-replace-or-upgrade.md`](WORKFLOW-replace-or-upgrade.md). Summary:
1. `docker pull <upstream>:<new-tag>` — verify it exists
2. Update Dockerfile FROM line
3. Rebuild: `cd docker && REGISTRY=nettername ./build-images.sh`
4. **Full pipeline smoke test**: recording, training, inference. Watch for `ERROR: ... not found` from `apply_overlay` (means upstream renamed something — fix the path filter).
5. One PR, one base-image bump.

### `bump-upstream-digests.sh`

Helper that runs `docker buildx imagetools inspect <upstream>:<tag>` and prints SHA256 digests + ready-to-use `sed` commands for digest-pinning. Manual review required.

### `versions.env`

`docker/versions.env` holds `IMAGE_TAG=latest` (read by `pull_images.ps1` and `verify_system.ps1`). Mutable tag → reproducibility risk. Consider pinning to digest or semver tag.

---

## 12. CRLF cleanup locations

Where the build strips Windows Git CRLF:

1. `physical_ai_server/Dockerfile` (lines 13-19): s6-overlay service text files
2. `open_manipulator/Dockerfile` (lines 14-15): entrypoint_omx.sh + identify_arm.py

**Why:** Windows Git with `core.autocrlf=true` (default) converts LF→CRLF on checkout. If `.gitattributes` doesn't enforce LF, CRLF lands in the image. s6-overlay rejects `longrun\r` as invalid type; bash misreads `#!/bin/bash\r`.

**Defensive belt-and-suspenders:** `.gitattributes` should set `* text=auto eol=lf` for shell scripts; the Dockerfile sed is a safety net.

---

## 13. Footguns

1. **Don't use `:latest` for ROBOTIS base images** in Dockerfiles — silent breakage on upstream retag.
2. **Don't bypass `apply_overlay` assertions.** A failing build is correct: it means upstream renamed something. Fix the path filter, don't comment out the check.
3. **Don't add overlays without sha256 verify** — they'll silently no-op on next upstream rename.
4. **Don't skip the patch verifier.** Self-checks are mandatory.
5. **Don't store secrets in build args** — they're visible in `docker history` and `docker inspect`. Use BuildKit `--secret` or fetch at container start.
6. **Don't mount /dev unless you need it** — `physical_ai_server` doesn't touch USB; `open_manipulator` does.
7. **Don't bind ports to 0.0.0.0** — keep loopback for rosbridge/web_video_server. School LAN exposure.
8. **Don't drop `.s6-keep`** — the server container starts but the ROS node never runs.
9. **Don't change `network_mode: host` or remove the bridge** — DNS-by-service-name relies on it.
10. **Don't `docker compose down -v`** without confirming with the user — `-v` deletes named volumes (recorded datasets gone).

---

## 14. Local dev build

```bash
cd robotis_ai_setup/docker
REGISTRY=nettername \
SUPABASE_URL=https://fnnbysrjkfugsqzwcksd.supabase.co \
SUPABASE_ANON_KEY=eyJ... \
CLOUD_API_URL=https://scintillating-empathy-production-9efd.up.railway.app \
./build-images.sh
```

Skip push (manual edit `build-images.sh` or Ctrl+C before push phase).

Smoke test compose locally (inside WSL2):
```bash
wsl -d EduBotics --cd /mnt/c/Program\ Files/EduBotics/docker -- docker compose config
wsl -d EduBotics --cd /mnt/c/Program\ Files/EduBotics/docker -- docker compose up -d
wsl -d EduBotics -- docker logs physical_ai_server --tail 50
```

---

## 15. Cross-references

- ROS2 stack inside the containers: [`17-ros2-stack.md`](17-ros2-stack.md)
- Recording / inference data flow: [`02-pipeline.md`](02-pipeline.md) §5, §8
- React build: [`13-frontend-react.md`](13-frontend-react.md) §11
- Installer + WSL2 rootfs (which hosts these containers): [`16-installer-wsl.md`](16-installer-wsl.md)
- Adding/changing an overlay: [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md)
- Bumping base images: [`WORKFLOW-replace-or-upgrade.md`](WORKFLOW-replace-or-upgrade.md)
- Known issues: [`21-known-issues.md`](21-known-issues.md) §3.4

---

**Last verified:** 2026-05-04.
