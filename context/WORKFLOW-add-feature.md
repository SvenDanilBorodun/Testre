# WORKFLOW: Adding a Feature

> Strict checklist for adding new functionality to EduBotics. Follow step by step.
> Read [`WORKFLOW.md`](WORKFLOW.md) first for the master rules.

---

## §1 — Decide which layers the feature touches

EduBotics has 9 layers. A feature touching the cloud training flow might span:
1. Supabase (new column / RPC / RLS)
2. Cloud API (new route / Pydantic model)
3. React (new UI / API call)
4. Modal worker (new training param)

Map your feature to layers **before writing any code**. If you can't draw the diagram, you don't understand the feature yet — ask the user.

---

## §2 — Read before you write

- Read [`00-INDEX.md`](00-INDEX.md) §4 routing table — find the layer file(s).
- Read those layer files in full (10-cloud-api, 11-modal-training, 13-frontend-react, etc.).
- Read [`21-known-issues.md`](21-known-issues.md) for any §3.x section matching your layers.
- Run `git log --oneline -20` on each affected directory to check for recent changes.

If a known issue overlaps your feature, you have two choices:
1. **Address the known issue** as part of your work (preferred when it's directly in the path).
2. **Document the conflict** in your PR description and proceed without making it worse.

---

## §3 — Cross-layer feature checklist

For each layer the feature touches, answer YES to all before considering it done.

### If you touch Supabase

See [`WORKFLOW-supabase-migration.md`](WORKFLOW-supabase-migration.md). Summary:
- [ ] Created migration file with next number (e.g., `008_my_feature.sql`)
- [ ] Created rollback file in `supabase/rollback/008_my_feature_rollback.sql`
- [ ] Updated `rollback/README.md` if non-trivial
- [ ] Idempotent: `IF NOT EXISTS`, `CREATE OR REPLACE FUNCTION`, etc.
- [ ] If new RPC: `SECURITY DEFINER`, `GRANT TO service_role` (NOT `authenticated`)
- [ ] If new table: RLS policies for student / teacher / admin
- [ ] Tested forward + rollback locally on a branch DB
- [ ] Updated [`12-supabase.md`](12-supabase.md) §2-7 with new schema + RPC + index
- [ ] If error code: register in [`12-supabase.md`](12-supabase.md) §13

### If you touch the cloud API (Railway FastAPI)

- [ ] Created/updated Pydantic model with bounds (max length, value range)
- [ ] Endpoint depends on `get_current_*` for auth
- [ ] Authorization helper called: `_assert_classroom_owned()` / `_assert_student_owned()` / `_assert_entry_owned()` (or equivalent for new resource type)
- [ ] **Verified the auth check by attempting access with a different user's JWT** (manual or test)
- [ ] Error codes match section §6 of [`10-cloud-api.md`](10-cloud-api.md): 400/401/403/404/409/429/500/502/503
- [ ] User-facing error message is German if returned to React
- [ ] Backend log message is English
- [ ] Updated rate limit rules in `main.py` if endpoint is expensive or abusable
- [ ] Updated `ALLOWED_ORIGINS` if a new frontend deployment serves the endpoint
- [ ] Run `uvicorn app.main:app --reload` locally + curl test

### If you touch the React SPA

- [ ] Component lives in the right directory: `pages/` for routes, `components/` for reusable, `features/` for slice-grouped
- [ ] State lives in the right slice (auth/ros/ui/training/teacher/admin/editDataset/tasks)
- [ ] API client added to `services/`, not inlined in components
- [ ] All user-visible strings are German
- [ ] Loading state shown during async calls (spinner / disabled button)
- [ ] Error toast on failure (`react-hot-toast`)
- [ ] No console.log left in committed code
- [ ] Tested in both `REACT_APP_MODE=student` and `REACT_APP_MODE=web` if relevant
- [ ] Cleanup: useEffect returns cleanup function for subscriptions / timers
- [ ] Started dev server (`npm start`) and tested in browser before declaring done

### If you touch the Modal worker

- [ ] Updated `training_handler.py` with new param flow (preflight → subprocess args → progress)
- [ ] If new env var: added to Modal Secret + documented in [`11-modal-training.md`](11-modal-training.md) §8 + [`04-env-vars.md`](04-env-vars.md) §3
- [ ] If new error: German error message in `_update_supabase_status` calls (student-facing) + English in stdout (logs)
- [ ] **`modal deploy modal_app.py` after changing the image** (image is immutable per deploy)
- [ ] **`modal serve` smoke test** with a tiny dataset before production deploy
- [ ] Verified force-reinstall cu121 still works if you changed image deps

### If you touch overlays

See [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md). Summary:
- [ ] Overlay file changed
- [ ] `apply_overlay` line in Dockerfile updated if filename changed
- [ ] Build passes: `cd docker && REGISTRY=nettername ./build-images.sh`
- [ ] `Overlaid: ...` log line confirms the file was actually replaced
- [ ] Pipeline smoke test: recording, training, inference

### If you touch the Windows GUI

- [ ] All new tkinter strings are German
- [ ] Long-running operations use a daemon thread + UI updates via `self.root.after(0, ...)`
- [ ] Subprocess invocations use `CREATE_NO_WINDOW` flag (except UAC + WebView2)
- [ ] Docker calls go through `_docker_cmd()` (i.e. `wsl -d EduBotics -- docker ...`), not raw `docker`
- [ ] `wsl --` calls go through `wsl_bridge.run()` for distro-pinning
- [ ] If new env var override: added `EDUBOTICS_*` constant in `constants.py`
- [ ] If GUI version-bump: updated `Testre/VERSION`, `gui/app/constants.APP_VERSION`, `installer/robotis_ai_setup.iss AppVersion`
- [ ] Tested with hardware connected (or mocked via test harness)

### If you touch the installer

- [ ] **Tested on a clean Windows 11 Pro VM** (never on dev machine without snapshot)
- [ ] Each PowerShell step's transcript readable in install dialog
- [ ] Exit codes correct (0 = success, 1 = fatal, etc.)
- [ ] `.reboot_required` marker logic intact if you touched WSL2 install
- [ ] SHA256 verifications intact for all third-party downloads

### If you touch the Modal MCP server

- [ ] New tool has type hints + docstring (FastMCP exposes them)
- [ ] **Read-only by default** — write tools require explicit user approval
- [ ] Bearer auth still enforced (`/mcp/*` paths)
- [ ] `modal serve` + `modal run -m mcp_server_stateless::test_tool --tool-name <new>` smoke test
- [ ] `modal deploy mcp_server_stateless.py` after dev test

---

## §4 — Verification matrix

For every change, you **must** answer "how will I verify this works?" BEFORE writing code. Common patterns:

| Change type | Verification |
|---|---|
| New API route | `curl` with valid JWT (success path) AND with wrong-role JWT (auth path) |
| New SQL RPC | `psql` invocation as service_role + as anon (RLS test) |
| New React page | dev server + click through happy path + click through error path |
| New overlay | full image rebuild + smoke test recording or inference |
| New env var | unset → expected default behavior; set to junk → expected error |

If you can't articulate the verification, **stop and ask the user.**

---

## §5 — Documentation update

For every PR that adds a feature, update the relevant context file(s):

- New API route → [`10-cloud-api.md`](10-cloud-api.md) §6 / §7 / §8 endpoint inventory
- New env var → [`04-env-vars.md`](04-env-vars.md) §2-9 (whichever surface)
- New ROS topic / service → [`17-ros2-stack.md`](17-ros2-stack.md) §7, §9
- New custom Postgres error code → [`12-supabase.md`](12-supabase.md) §13 + [`03-glossary.md`](03-glossary.md) "P00xx codes"
- New layer concept worth a glossary entry → [`03-glossary.md`](03-glossary.md)
- Update the "Last verified" footer of any layer file you materially changed

**No PR is complete without doc sync.**

---

## §6 — Common feature recipes

### "Add a new training policy type"

1. **Cloud API** (`routes/training.py`):
   - Add to `ALLOWED_POLICIES` env var (default + Railway dashboard)
   - Add to `POLICY_MAX_TIMEOUT_HOURS` dict
2. **Modal** (`training_handler._build_training_command`):
   - Verify LeRobot accepts the new `--policy.type=X` arg in v0.2.0 @ 989f3d05
3. **React** (`physical_ai_manager`):
   - Add to `REACT_APP_ALLOWED_POLICIES` build arg if students should see it (currently `act` only)
   - PolicySelector dropdown picks up automatically if the ROS service `getPolicyList` returns the type
4. **Test**: smoke training with the new type on a tiny dataset.

### "Add a new dashboard widget for teachers"

1. **Cloud API** (`routes/teacher.py`):
   - Add new endpoint with `get_current_teacher` dep
   - Add `_assert_*_owned` helper for new resource if it has owner semantics
2. **React** (`features/teacher/`, `services/teacherApi.js`, `pages/teacher/TeacherDashboard.js`):
   - Add slice action / API call / component
3. **Migration** (if new column): see [`WORKFLOW-supabase-migration.md`](WORKFLOW-supabase-migration.md)
4. **Test**: log in as teacher, perform action; log in as different teacher, verify isolation.

### "Add a new MCP tool for autonomous queries"

1. Edit `modal_mcp/mcp_server_stateless.py`, add `@mcp.tool()` function
2. Read-only Supabase query? Use `_supa_get(path, params)`
3. External API? Use `httpx.AsyncClient(timeout=15)`
4. Add to test_tool smoke list
5. `modal deploy`
6. Update [`18-modal-mcp.md`](18-modal-mcp.md) §6 with the new tool

### "Add a new env var"

1. Identify which surface(s) consume it (Railway / Modal / Docker / GUI / React).
2. For each surface:
   - Read it via `os.environ.get(name, default)` or equivalent
   - Document default + meaning in code comment
3. Update [`04-env-vars.md`](04-env-vars.md) §2-9.
4. If it's a React build arg: rebuild + push image (not runtime-changeable).
5. If it's a Modal Secret: `modal secret create ... --from-dotenv` + redeploy.

---

## §7 — Anti-patterns (don't do these)

- **Don't** add a feature that crosses 3+ layers without first reading all the layer files. You will miss something.
- **Don't** rely on RLS as the primary auth mechanism in the cloud API. Service-role bypasses it.
- **Don't** add an overlay without sha256 verification.
- **Don't** add a Modal Secret without redeploying.
- **Don't** add a React feature without testing both student + web modes if applicable.
- **Don't** add a feature gate via comment-out. Use a flag if you need a flag.
- **Don't** add documentation that just describes WHAT the code does. Doc the WHY (architecture, tradeoffs, gotchas).
- **Don't** forget the German UI strings. Code reviewers will catch English in the React UI immediately.

---

## §8 — Cross-references

- Master rules: [`WORKFLOW.md`](WORKFLOW.md)
- Supabase migrations: [`WORKFLOW-supabase-migration.md`](WORKFLOW-supabase-migration.md)
- Overlay changes: [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md)
- Replacing/upgrading: [`WORKFLOW-replace-or-upgrade.md`](WORKFLOW-replace-or-upgrade.md)
- Debugging: [`WORKFLOW-debug.md`](WORKFLOW-debug.md)
