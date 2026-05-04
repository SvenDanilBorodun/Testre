# EduBotics Context Index

> **Future Claude: read this first, then `WORKFLOW.md`, then route to the file you need.**
> Total reading time for the always-loaded set (this + WORKFLOW.md): ~5 minutes.
> Layer files (10-18) are ~1500–3000 words each — read only the one(s) relevant to your task.

EduBotics is a German-language educational platform for ROBOTIS OpenMANIPULATOR robots: students record demonstrations on a real arm, train ML policies on cloud GPU (Modal), and run inference back on the arm. Distributed as a Windows .exe that ships a bundled WSL2 distro + 3 Docker containers. **No Docker Desktop dependency.**

---

## What Claude must do before any task

1. Read this file (you are here).
2. Read [`WORKFLOW.md`](WORKFLOW.md) — the master rulebook (language boundary, commit style, verification, when-to-ask-the-user).
3. Pick the relevant layer file from §1 and the relevant workflow from §3.
4. Skim [`21-known-issues.md`](21-known-issues.md) if the task touches a system listed in the top-20 — to avoid reintroducing known bugs.
5. **Then** write code.

---

## §0 — Always-loaded reference

| File | Read when | Length |
|---|---|---|
| [`00-INDEX.md`](00-INDEX.md) | Entry point — every session | ~600 words |
| [`WORKFLOW.md`](WORKFLOW.md) | Entry point — every session | ~1500 words |
| [`01-architecture.md`](01-architecture.md) | Forming the mental model of the whole system | ~2000 words |
| [`02-pipeline.md`](02-pipeline.md) | Tracing one student workflow end-to-end | ~3000 words |
| [`03-glossary.md`](03-glossary.md) | When you hit a term you don't recognize (OMX-F, .s6-keep, ROS_DOMAIN_ID, etc.) | ~800 words |
| [`04-env-vars.md`](04-env-vars.md) | Anywhere config changes — Railway, Modal, Docker, GUI, .env | ~1500 words |

---

## §1 — Layer deep dives (read on demand)

| Layer | File | Read when |
|---|---|---|
| Cloud API (FastAPI on Railway) | [`10-cloud-api.md`](10-cloud-api.md) | Adding routes, RPCs, auth, rate limits, dedup logic |
| Modal training worker | [`11-modal-training.md`](11-modal-training.md) | Editing `modal_app.py` / `training_handler.py`, bumping LeRobot, changing image build |
| Supabase schema | [`12-supabase.md`](12-supabase.md) | Adding migrations, RLS, triggers, RPCs |
| React SPA (student + web) | [`13-frontend-react.md`](13-frontend-react.md) | Touching `physical_ai_manager/src/`, hooks, Redux, rosbridge, Realtime |
| Windows tkinter GUI | [`14-windows-gui.md`](14-windows-gui.md) | Editing `gui/app/*.py`, tkinter wizard, WebView2, USB scanning, Docker pulls |
| Docker / overlays / compose | [`15-docker.md`](15-docker.md) | Editing Dockerfiles, overlays, compose, build-images.sh, patches |
| Windows installer + WSL2 rootfs | [`16-installer-wsl.md`](16-installer-wsl.md) | Editing `.iss`, `.ps1` scripts, rootfs Dockerfile, `start-dockerd.sh`, `wsl.conf` |
| ROS2 robot stack | [`17-ros2-stack.md`](17-ros2-stack.md) | Editing `open_manipulator/`, `physical_ai_server/`, launch files, xacros, controllers, behavior trees |
| Modal MCP server (autonomous gateway) | [`18-modal-mcp.md`](18-modal-mcp.md) | Editing `modal_mcp/mcp_server_stateless.py`, exposed tools, bearer auth |

---

## §2 — Operations &amp; governance

| File | Read when |
|---|---|
| [`20-operations.md`](20-operations.md) | Rotating secrets, investigating stuck training, rolling back migrations/images, deletion under GDPR, deploy procedures |
| [`21-known-issues.md`](21-known-issues.md) | **Before touching any safety-critical path** (arm control, recording, training dispatch). Top-20 triage list + cross-cutting themes |
| [`22-frontend-followups.md`](22-frontend-followups.md) | UX punch-list for the React SPA (upstream issues we've identified but not patched) |
| [`23-rollout-accounts.md`](23-rollout-accounts.md) | Deploying the teacher/admin account system (post-merge ops checklist) |

---

## §3 — Workflows (HOW Claude works)

Strict checklists. Follow them step-by-step. Don't improvise around the verification steps.

| File | Read when |
|---|---|
| [`WORKFLOW.md`](WORKFLOW.md) | Every session. The master rules. |
| [`WORKFLOW-add-feature.md`](WORKFLOW-add-feature.md) | Adding a new feature that crosses layers (e.g., new training param, new dashboard widget) |
| [`WORKFLOW-replace-or-upgrade.md`](WORKFLOW-replace-or-upgrade.md) | Bumping LeRobot version, base image digests, Python deps, Node deps |
| [`WORKFLOW-supabase-migration.md`](WORKFLOW-supabase-migration.md) | Adding a new SQL migration (forward + rollback + RLS) |
| [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md) | Modifying any of the 5 overlays in `docker/physical_ai_server/overlays/` or `docker/open_manipulator/overlays/` |
| [`WORKFLOW-debug.md`](WORKFLOW-debug.md) | "It's broken — where do I look first?" Per-stage triage |

---

## §4 — Routing by task type

| User says... | Read these files |
|---|---|
| "Add a new policy type" | `WORKFLOW-add-feature.md` → `10-cloud-api.md` (`ALLOWED_POLICIES`, `POLICY_MAX_TIMEOUT_HOURS`) → `11-modal-training.md` → `13-frontend-react.md` (build-time allowlist) |
| "Bump LeRobot to v0.3.x" | `WORKFLOW-replace-or-upgrade.md` → `11-modal-training.md` → `15-docker.md` (base image overlay alignment) → `17-ros2-stack.md` (recording schema `codebase_version`) |
| "New Supabase column / RPC / RLS policy" | `WORKFLOW-supabase-migration.md` → `12-supabase.md` → `10-cloud-api.md` |
| "Add a teacher dashboard feature" | `13-frontend-react.md` → `10-cloud-api.md` (routes/teacher.py) → `12-supabase.md` |
| "Recording is broken / camera mismatch" | `WORKFLOW-debug.md` → `17-ros2-stack.md` (overlay validation) → `15-docker.md` (overlay application) → `21-known-issues.md` §3.5 |
| "Training stalled / Modal worker hung" | `WORKFLOW-debug.md` → `11-modal-training.md` → `10-cloud-api.md` (`_sync_modal_status`, `STALLED_WORKER_MINUTES`) → `20-operations.md` §2 |
| "GUI installer fails / WSL distro broken" | `16-installer-wsl.md` → `WORKFLOW-debug.md` → `21-known-issues.md` §3.1 |
| "Arm moves wrong / safety incident" | `21-known-issues.md` §2.2 + §3.3 + §3.8 (read first!) → `17-ros2-stack.md` (overlays + entrypoint) → `WORKFLOW-overlay-change.md` |
| "Add a Modal MCP tool" | `18-modal-mcp.md` → `WORKFLOW-add-feature.md` |
| "Update env vars / secrets" | `04-env-vars.md` → `20-operations.md` §1 |

---

## §5 — When this index is wrong

The codebase moves. If you find this index pointing at a file that no longer exists, or describing a layer that's been refactored, **say so explicitly to the user** and propose updating the index. Don't paper over the drift — drift is the enemy of context.

The single source of truth is always the code. These files describe what's true _at the time they were written_. Verify against `git log` and the current file when stakes are high.

---

**Index last verified:** 2026-05-04. If git shows changes after that date in `Testre/robotis_ai_setup/`, `Testre/physical_ai_tools/`, or `Testre/open_manipulator/` that materially change the architecture, update the relevant layer file and bump this date.
