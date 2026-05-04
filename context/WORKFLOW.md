# WORKFLOW — How Claude Works in EduBotics

> **Master rulebook. Always-loaded. Read this before every task.**
> Specific workflows live in `WORKFLOW-*.md` files; this is the spine.

---

## §1 — The 5 non-negotiable rules

1. **Language boundary is sacred.**
   - **German**: every user-visible string. Tkinter labels, React UI, error messages returned in API responses that end up in front of a student/teacher, log strings the user reads.
   - **English**: code, comments, docstrings, log lines for the maintainer, internal API JSON keys, function names, commit messages.
   - When you write a new error, ask: _will a student or teacher read this?_ Yes → German. No → English.
   - Use the umlaut letters (`ä ö ü ß`) directly. Do **not** transliterate to `ae oe ue ss` (e.g., `Schueler` is a legacy artifact in some files; new code uses `Schüler`).

2. **The arm is real hardware. Safety is non-negotiable.**
   - Never disable joint-limit clamps, NaN guards, velocity caps, or stale-camera halts in `inference_manager.py` overlay.
   - Never remove torque-disable on SIGTERM in `entrypoint_omx.sh`.
   - Never bypass `_assert_classroom_owned()` / `_assert_student_owned()` / `get_current_teacher()` ownership checks in the cloud API.
   - If you genuinely need to relax a safety check, **stop and ask the user.**

3. **Overlays must fail loudly on no-op.**
   - Every overlay in `docker/physical_ai_server/overlays/` and `docker/open_manipulator/overlays/` is applied via `apply_overlay()` in the Dockerfile, which sha256-verifies the result.
   - If you add a new overlay, you **must** add it to the `apply_overlay` chain with a unique path filter and verify the build fails (with the expected error message) when the upstream file is missing. See [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md).
   - Patches (`patches/fix_server_inference.py`) must self-verify and exit non-zero on no-op.

4. **Service-role key bypasses RLS. Authorization is your job.**
   - Every Supabase query in `cloud_training_api/app/` runs as service-role.
   - Every endpoint that touches another user's data **must** call `_assert_classroom_owned()`, `_assert_student_owned()`, or check `auth.uid() == row.user_id` in Python before reading/writing.
   - One missed assertion = silent IDOR. RLS policies exist as defense-in-depth but are not the primary guard.

5. **Don't introduce drift between the 5 sources of truth for LeRobot.**
   - `physical_ai_tools/lerobot/` (static snapshot @ `989f3d05ba47…`)
   - Modal image: `modal_training/modal_app.py` `LEROBOT_COMMIT` constant
   - Base image: `robotis/physical-ai-server:amd64-0.8.2` clones `ROBOTIS-GIT/lerobot` jazzy branch
   - Overlay path: `docker/physical_ai_server/overlays/` does NOT overlay LeRobot itself
   - Recording's `codebase_version: "v2.1"` written into `meta/info.json`
   - Bumping LeRobot is a 5-place change in one PR. See [`WORKFLOW-replace-or-upgrade.md`](WORKFLOW-replace-or-upgrade.md).

---

## §2 — Before you write any code

Before editing, run this 4-step checklist:

1. **Read the relevant layer file** (see [`00-INDEX.md`](00-INDEX.md) §4 routing table). Don't grep blind.
2. **Check known issues** ([`21-known-issues.md`](21-known-issues.md)) for the area you're touching. If it's in the top-20, your fix may need to address the underlying issue, not just the surface symptom.
3. **Check current state**: `git log --oneline -20` and `git status` in the affected directory. The deep-dive docs may be stale relative to recent commits.
4. **Identify the verification step**: how will you know your change works? See §3.

If you cannot answer "how will I verify?" before writing, stop and design the verification first.

---

## §3 — Verification matrix (per file you change)

| If you change... | Verify by... |
|---|---|
| `cloud_training_api/app/routes/*.py` | Read the `app/main.py` router include, run `cd cloud_training_api && python -m uvicorn app.main:app --reload` locally, hit endpoint with curl or REST client, check Supabase row state |
| `cloud_training_api/app/auth.py` or `_assert_*` helpers | Verify with both correct and INCORRECT JWTs (impersonation test). Confirm 401/403 status codes |
| `modal_training/modal_app.py` (image build) | `modal deploy modal_app.py` then run a smoke training (e.g., `act` policy on a tiny dataset). Confirm cu121 torch (not cu130), no torchcodec |
| `modal_training/training_handler.py` | Smoke training; tail Modal logs (`modal app logs edubotics-training`); verify Supabase progress RPC writes; verify HF model upload + `repo_info` |
| `supabase/*.sql` | Apply forward in a branch DB, verify rollback, test with both anon-key (RLS active) AND service-role-key (RLS bypassed). See [`WORKFLOW-supabase-migration.md`](WORKFLOW-supabase-migration.md) |
| `docker/physical_ai_server/overlays/*` | `cd robotis_ai_setup/docker && REGISTRY=nettername ./build-images.sh` and confirm `apply_overlay` prints `Overlaid: …` for the changed file. Then do a full pipeline smoke test |
| `docker/physical_ai_server/Dockerfile` | Same — full rebuild + smoke. Watch for `ERROR: ... not found` (means upstream renamed something) |
| `docker/open_manipulator/entrypoint_omx.sh` | Build image, start container with attached USB devices, watch container logs for the 4 phases (validate hardware → leader → follower sync → cameras) |
| `physical_ai_manager/src/*` (React) | `cd physical_ai_manager && npm start`, verify in browser at `http://localhost:3000?cloud=1` (web mode) or with rosbridge running (student mode). Check console for errors |
| `gui/app/*.py` (Windows GUI) | Run on Windows: `cd gui && python main.py` (dev) or PyInstaller build + run installed `.exe`. Verify wizard flow with hardware connected |
| `installer/*.ps1` | Build installer (`iscc robotis_ai_setup.iss`), install on a clean Windows VM, verify each step's output. **Never test installer scripts on your dev machine without a VM** |
| `wsl_rootfs/*` | `cd wsl_rootfs && ./build_rootfs.sh`, then `wsl --import test-edubotics ... && wsl -d test-edubotics -- docker info` |
| `modal_mcp/mcp_server_stateless.py` | `modal serve mcp_server_stateless.py` in dev, test with `modal run -m mcp_server_stateless::test_tool --tool-name <name>` |

For UI changes: **start the dev server and test in a browser before reporting done.** Type checking and tests don't catch UX regressions.

---

## §4 — Commit conventions

This repo uses one-line commit messages, present tense, English. Examples from `git log`:

- `Pin docker 27.5.1 to fix snapshotter regression on WSL2`
- `Move .env to %LOCALAPPDATA% to avoid UAC on regen`
- `Bump open-manipulator base image to amd64-4.1.4`
- `Add per-policy timeout caps to /trainings/start`

**Don't:**
- Don't write multi-paragraph commit bodies unless the user asks.
- Don't add `Co-Authored-By: Claude` unless the user has explicitly asked you to commit.
- Don't squash unrelated changes into one commit. One concept per commit.
- Don't bypass pre-commit hooks (`--no-verify`). Fix the underlying issue.

**Don't commit unless the user explicitly says "commit this".** Match the scope: if they say "commit the migration", commit only the migration file, not your unrelated cleanup.

---

## §5 — Adding new code

- **Default to writing no comments.** Names should explain themselves. Only add a comment when the WHY is non-obvious — a hidden constraint, a workaround for a specific bug, behavior that would surprise a reader.
- **Don't write comments that reference your task** ("added for issue #123", "fix for the recording bug"). Those belong in the PR description and rot as the codebase evolves.
- **Don't add error handling for impossible scenarios.** Trust internal code. Validate at the system boundary (user input, external APIs) only.
- **Don't preserve unused old code "just in case".** Delete it. `git` is the backup.
- **Don't add backwards-compatibility shims for code only you call.** Refactor the callers in the same change.
- **Three similar lines is better than a premature abstraction.** Wait for the fourth before extracting.

---

## §6 — When to ask the user

You can act autonomously on:
- Reading any file
- Editing code with low blast radius (single layer, no infra effects)
- Running local tests, lints, type checks
- Building Docker images locally
- Reading/writing files in the project
- Spawning sub-agents for research

**Ask the user before:**
- Pushing to remote (`git push`)
- Creating/closing/commenting on PRs or issues
- `wsl --unregister` (destroys VHDX with named volumes inside)
- Force-pushing
- `docker compose down -v` (deletes named volumes — datasets gone)
- Modal `cancel` on a running training (charges credit if completion was imminent)
- Supabase `delete-user` or `auth.admin.delete_user` calls
- Rotating production secrets
- Editing CI/CD config
- Changing safety-critical paths in `inference_manager.py` (joint clamp, NaN guard, stale-camera halt)
- Removing/renaming files that other layers reference (overlay targets, ROS topics, env var names)

When in doubt: ask. Cost of asking is one round-trip. Cost of an unwanted destructive action can be hours of student data.

---

## §7 — When the docs are wrong

These context files describe the codebase **at a point in time**. The single source of truth is the code itself.

If you read a context file that points at a function/file/flag that no longer exists:
1. Tell the user explicitly: _"the context says X but I see Y in the code; the docs may be stale."_
2. Propose updating the affected context file as part of your work.
3. Trust the code.

If you make a change that materially affects what's documented in a context file (rename a function the docs cite, change an env var name, refactor a layer), **update the context file in the same PR**. The whole point of these docs is that they stay in sync; you're now responsible for that.

Update the `last verified at` line at the bottom of any layer file you touched.

---

## §8 — When things go wrong

- **Don't use destructive shortcuts to make obstacles disappear.** If a pre-commit hook fails, fix the issue — don't `--no-verify`. If a test fails, understand why — don't `@pytest.skip`. If a build fails with `ERROR: overlay not found`, that's the build telling you upstream renamed something — don't bypass the assertion.
- **Investigate before deleting.** Unfamiliar files might be the user's in-progress work. `git status` first.
- **Resolve merge conflicts**, don't discard changes.
- **Read the full error message.** Don't summarize, don't infer — read it.

---

## §9 — Mental model

EduBotics is a **vertically integrated stack** for teaching. Every layer is in service of one student workflow:

```
Install (.exe) → Setup (GUI wizard) → Record (ROS2) → Train (Modal) → Inference (ROS2)
```

The hard-won design choices are:
- **Bundled WSL2 distro** instead of Docker Desktop — no licensing prompts, no tray sprawl
- **Service-role key + Python ownership checks** instead of anon key + RLS — historical, but everywhere now
- **Overlay-with-sha256-verify** instead of forking upstream — pull upstream improvements, fix bugs surgically
- **Local LeRobot snapshot** that matches Modal training image — same code on robot and GPU
- **German UI, English code** — target audience is German students; maintainer is one person

When in doubt about an architectural choice, assume it was made deliberately and ask the user before reverting it.
