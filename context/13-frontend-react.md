# 13 — React SPA (`physical_ai_manager`)

> **Layer:** Frontend
> **Location:** `Testre/physical_ai_tools/physical_ai_manager/`
> **Owner:** ROBOTIS upstream (hacked) — no overlay system; we modify in-place
> **Read this before:** touching React source, hooks, Redux, rosbridge integration, or Realtime.

The codebase is a CRA React 19 project with **two build modes**: `student` (default, ships in Docker container) and `web` (Railway deployment, admin/teacher dashboard).

---

## 1. Top-level structure

```
physical_ai_manager/
├── package.json               # React 19.1, Redux Toolkit 2.8.2, Supabase 2.49.8, ROSLIB 1.4.1, Recharts, Tailwind
├── public/
│   └── index.html
├── Dockerfile                 # student build (REACT_APP_MODE=student)
├── Dockerfile.web             # web build (REACT_APP_MODE=web), Railway deploy
├── nginx.conf                 # student nginx config
├── nginx.web.conf.template    # web nginx config (envsubst $PORT)
├── railway.json               # Railway service config (web deploy)
├── vercel.json                # STALE marker only — kept for `vercel dev`. Real web deploy is Railway
└── src/
    ├── App.js                 # mode switch → StudentApp | WebApp
    ├── App.css, index.css     # Tailwind + design tokens
    ├── index.js               # entry
    ├── StudentApp.js          # student mode root (5 tabs)
    ├── WebApp.js              # web mode root (admin/teacher dashboards)
    ├── constants/
    │   ├── appMode.js         # APP_MODE, SYNTHETIC_EMAIL_DOMAIN, usernameToEmail
    │   └── pageType.js        # HOME, RECORD, TRAINING, INFERENCE, EDIT_DATASET enum
    ├── store/                 # Redux Toolkit slices
    ├── features/              # feature-grouped slices
    ├── pages/                 # one file per page
    ├── components/            # reusable UI
    ├── hooks/                 # custom hooks
    ├── services/              # API clients (apiClient, cloudTrainingApi, meApi, teacherApi, adminApi, supabaseClient)
    ├── utils/                 # rosConnectionManager + utilities
    ├── lib/                   # 3rd-party wrappers
    └── fabrik/                # design system primitives (Btn, Card, Pill, etc.)
```

---

## 2. Mode switch

`src/constants/appMode.js`:

```javascript
export const APP_MODE = process.env.REACT_APP_MODE === 'web' ? 'web' : 'student';
export const SYNTHETIC_EMAIL_DOMAIN = 'edubotics.local';
export const usernameToEmail = (username) =>
  `${String(username).trim().toLowerCase()}@${SYNTHETIC_EMAIL_DOMAIN}`;
```

`src/App.js` (line 24): branches on `APP_MODE` → `<WebApp />` or `<StudentApp />`. Plus `useVersionCheck()` polls `/version.json` every 30 s.

**Build-time only:** `REACT_APP_MODE` is baked into the bundle at `npm run build`. The student Docker image runs the student build; the Railway web service runs the web build.

---

## 3. Auth flow

### LoginForm (`src/components/LoginForm.js`)

1. Username regex: `^[a-zA-Z0-9._-]+$` (rejects `@`, whitespace)
2. Convert to email: `usernameToEmail('max')` → `max@edubotics.local`
3. `supabase.auth.signInWithPassword({ email, password })`
4. Dispatch `setSession(data.session)` (authSlice)

### `getMe()` (services/meApi.js, called from StudentApp.js / WebApp.js)

After session obtained:
- GET `/me` from Railway API
- Returns: `{role, username, full_name, classroom_id, pool_total, allocated_total, pool_available, student_count}`
- Dispatch `setProfile()`

**Role-mismatch rejection:**
- `StudentApp` requires `role === 'student'` — else `signOut()` + German toast
- `WebApp` rejects `role === 'student'` — else `signOut()` + German toast
- `WebApp` accepts `teacher` and `admin`

### Session storage

Supabase manages JWT in localStorage/sessionStorage automatically. Auto-refreshes before expiry. On 401 from any API, the app calls `signOut()` + shows "Sitzung abgelaufen".

---

## 4. Redux store

`store.js` combines:
```js
{ tasks, ros, ui, training, editDataset, auth, teacher, admin }
```

### authSlice
- session, isAuthenticated, isLoading, profileLoaded
- role, username, fullName, classroomId
- Teacher-only: poolTotal, allocatedTotal, poolAvailable, studentCount
- Actions: `setSession`, `clearSession`, `setProfile`, `updateTeacherPool`

### rosSlice
- rosHost, rosbridgeUrl (`ws://${host}:9090`)
- imageTopicList (camera topics from `/image/get_available_list` ROS service)

### taskSlice
- taskInfo: name, type, instruction[], policyPath, fps, tags, warmupTime, episodeTime, resetTime, numEpisodes, privateMode, …
- taskStatus: robotType, running, phase, progress, currentEpisodeNumber, usedStorageSize, heartbeatStatus
- availableRobots, availableCameras, policyList, datasetList

### trainingSlice
- trainingInfo (persisted to localStorage): datasetRepoId, policyType, outputFolderName, seed, steps, batchSize, …
- isTraining, topicReceived, currentStep, currentLoss
- cloudJobsRefreshCounter, selectedTrainingId

### uiSlice
- currentPage (PageType enum)
- isLoading, error, notifications

### teacherSlice
- classrooms[], selectedClassroomId, selectedClassroom (full detail)
- studentTrainings ({ studentId: [trainings] })

### adminSlice
- teachers[], loading

**No middleware/listeners** — defaults only.

---

## 5. rosConnectionManager

`utils/rosConnectionManager.js` (singleton):
- `getConnection(rosbridgeUrl)` returns promise resolving when ROSLIB.Ros connected
- Reused until URL changes
- Reconnect: exp backoff `min(1000 * 2^attempt, 30000)`, max 30 attempts, 10 s conn timeout
- `setOnConnected()` callback fires when rosbridge re-establishes
- StudentApp.js registers `initializeSubscriptions` callback; cleanup on unmount

`hooks/useRosServiceCaller.js` returns 15 service callers:
- `sendRecordCommand`, `sendInferenceCommand`, `sendStopCommand`, `setRobotType`, `getRobotTypeList`
- `getPolicyList`, `getSavedPolicyList`, `getDatasetList`, `getModelWeightList`, `getAvailableImageList`
- `sendTrainingCommand`, `getTrainingInfo`
- `browseFile`, `sendEditDatasetCommand`, `getDatasetInfo`
- `registerHfUser`, `getRegisteredHfUser`, `huggingfaceControl`

All wrap `callService(serviceName, serviceType, request, timeoutMs=10000)`.

---

## 6. Cloud API client

### `services/apiClient.js`

```js
const API_URL = process.env.REACT_APP_CLOUD_API_URL;

export async function apiRequest(endpoint, method, accessToken, body) {
  const res = await fetch(`${API_URL}${endpoint}`, {
    method,
    headers: {
      'Authorization': `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}
```

### Wrappers per route

- `services/cloudTrainingApi.js`: `getQuota`, `startCloudTraining`, `cancelCloudTraining`, `getTrainingJobs`, `getTrainingStatus`
- `services/meApi.js`: `getMe`, `exportMyData`, `requestDeletion`
- `services/teacherApi.js`: classrooms CRUD, students CRUD, credits, password reset, progress entries CRUD
- `services/adminApi.js`: teachers CRUD, credits, password reset

All accept `accessToken` (from `session.access_token`) as the first arg.

---

## 7. Supabase Realtime

### `useSupabaseTrainings` (hooks/useSupabaseTrainings.js)

1. Bootstrap: GET `/trainings/list` from Railway
2. Realtime channel: `supabase.channel('trainings:{userId}').on('postgres_changes', {event: '*', schema: 'public', table: 'trainings', filter: `user_id=eq.${userId}`})`
3. Events: INSERT, UPDATE, DELETE
4. `stripSecrets()` removes `worker_token`, `user_id`, `cloud_job_id` before pushing into Redux state (defense-in-depth)
5. `mergeJob()` upserts incoming row into list
6. **Fallback polling** every 30 s if realtime not subscribed (handles network blips)
7. Returns `{ jobs, loading, refetch, isRealtime }`

### Other realtime

None observed in this codebase. Training progress comes via ROS topic `/training/status` (rosbridge); cloud training progress via Supabase Realtime as above.

---

## 8. Pages inventory

### HomePage.js
Greeting, robot SVG, RobotTypeSelector, HeartbeatStatus, navigation links.

### RecordPage.js
ControlPanel (Start/Stop), ImageGrid (3-cell camera feed), InfoPanel (task metadata).
Hooks: `useRosServiceCaller` (sendRecordCommand), `useRosTopicSubscription` (heartbeat + task status).

### InferencePage.js
InferencePanel (policy path, user ID), ImageGrid, ControlPanel.

### TrainingPage.js
- If not cloud-only: LoginForm prompt
- TrainingControlPanel (Start local), DatasetSelector, PolicySelector, TrainingOptionInput
- MyModels (Supabase Realtime list), TrainingLiveChart (selected job)
- Hooks: `useSupabaseTrainings`, `useRefetchOnFocus`

### EditDatasetPage.js
DatasetSelector + Merge/Delete episode UI. Hooks: `sendEditDatasetCommand`.

### TeacherDashboard (src/pages/teacher/)
Sidebar: classrooms list. Right: ClassroomDetail (students + credits + progress entries). Modals: CreateClassroom, CreateStudent, PasswordReset.

### AdminDashboard (src/pages/admin/)
Teacher list + CreateTeacher modal + credit management.

---

## 9. Top components

| Component | File | Notes |
|---|---|---|
| ControlPanel | `components/ControlPanel.js` | Start/Stop/Skip with keyboard shortcuts; validates required fields per page; spinner + progress |
| ImageGrid | `components/ImageGrid.js` | 3-cell camera grid, auto-assigns ROS image topics, modal to reassign |
| ImageGridCell | `components/ImageGridCell.js` | Single feed display + frame-rate counter + fallback noise |
| FileBrowserModal | `components/FileBrowserModal.js` | Tree browser via `browseFile` ROS service |
| PolicyDownloadModal | `components/PolicyDownloadModal.js` | HF model download via HF status topic |
| MyModels | `components/MyModels.js` | Card grid of Supabase trainings; status pills; cancel button |
| TrainingLiveChart | `components/TrainingLiveChart.js` | Recharts loss-vs-step line; auto-picks newest running job |
| InferencePanel | `components/InferencePanel.js` | Policy path selector, user ID dropdown, instructions, HF token popup |
| DatasetSelector | `components/DatasetSelector.js` | Dropdown via `/training/get_dataset_list` ROS service |
| PolicySelector | `components/PolicySelector.js` | Dropdown via `getPolicyList()` ROS service, filtered by ALLOWED_POLICIES build env |
| TrainingControlPanel | `components/TrainingControlPanel.js` | ROS local-training flow (Start → live loss → Finish) via `sendTrainingCommand` |
| TrainingOptionInput | `components/TrainingOptionInput.js` | Numeric inputs for seed/batch/steps; auto-saves to localStorage |
| LoginForm | `components/LoginForm.js` | Username + password; synthetic email |
| RobotTypeSelector | `components/RobotTypeSelector.js` | Dropdown via `getRobotTypeList()`; persists to localStorage |
| EbUI primitives | `fabrik/EbUI.js` | `<Btn>`, `<Card>`, `<Pill>`, `<LogoMark>`, `<SectionHeader>` |

---

## 10. Custom hooks

| Hook | Purpose |
|---|---|
| `useRosServiceCaller` | Returns 15 bound ROS service callables |
| `useSupabaseTrainings` | Realtime trainings list + 30 s poll fallback |
| `useVersionCheck` | Polls `/version.json` every 30 s; reloads on `buildId` mismatch (with sessionStorage guard against reload loops) |
| `useRefetchOnFocus` | Refetch on tab focus / visibility; debounced 2 s |
| `useRosTopicSubscription` | Subscribes to 4 ROS topics (task, heartbeat, training, HF status); inits audio context for beeps |

---

## 11. Build &amp; version logic

### `useVersionCheck` (hooks/useVersionCheck.js)

1. Read `BUILT_ID = process.env.REACT_APP_BUILD_ID` (baked at build time)
2. Skip for `BUILT_ID === 'dev'` (local development)
3. Every 30 s: `fetch('/version.json?_=' + Date.now())` (cache-bust)
4. If `json.buildId !== BUILT_ID`: `window.location.reload()`
5. Guard: 60 s minimum between reloads (sessionStorage `__edubotics_version_reload_at`)

### `/version.json`

Written by Dockerfile at build time:
```json
{"buildId": "20260504-abcd123", "builtAt": "2026-05-04T12:00:00Z"}
```

nginx serves with `Cache-Control: no-store` so polling always gets fresh response.

---

## 12. WebApp dashboards

### TeacherDashboard
- Sidebar: classroom cards (name, student count)
- Main: ClassroomDetail with student list, credits, progress entries
- CRUD: CreateClassroom (max name 100 chars), CreateStudent (3-32 username), reset password
- Credits: `adjustStudentCredits(token, studentId, delta)` from teacherApi
- Progress entries: class-wide or per-student daily notes (listProgressEntries / createProgressEntry / patch / delete)
- Pool display: poolTotal / allocatedTotal / poolAvailable / studentCount (from /me)

### AdminDashboard
- Teacher list (name, username, credits)
- Create teacher (username + password + full_name + initial credits)
- Reset teacher password
- Allocate credits to teacher pool (cannot reduce below allocated_total)

---

## 13. i18n

**All strings hardcoded German** — no i18next or react-intl. Examples:
- LoginForm: "Willkommen zurück", "Ungültiger Benutzername"
- Pages: "Aufnahme", "Training", "Inferenz", "Daten"
- Time-of-day: "Guten Morgen", "Hallo", "Guten Abend"
- Errors: "Robotertyp auswählen", "Server nicht erreichbar"

Adding a non-German language would be a string-extraction project; not currently in scope.

---

## 14. Footguns

1. **Synthetic email domain hardcoded** — must match the backend (`bootstrap_admin.py` and `routes/teacher.py` use the same string `edubotics.local`). Typo here = no logins work.
2. **Username validation is frontend-only** — backend `validate_username()` should also enforce. Race risk if you change the regex on one side.
3. **Role mismatch rejection happens AFTER `/me` succeeds** — UI flickers briefly with the wrong role's UI before redirecting. Cosmetic, not blocking.
4. **ROS reconnect gives up after 30 attempts** — no user-visible "permanently lost" UI. Student must refresh page.
5. **localStorage keys are bare strings** — collision risk if reused. Naming convention not enforced.
6. **imageTopicList is in Redux but not localStorage** — resets on reload; user reassigns camera topics every session.
7. **Realtime → polling fallback is 30 s stale** — silent UX regression on network blips.
8. **No optimistic updates** — every CRUD shows a spinner for the full API call.
9. **MyModels doesn't paginate** — students with &gt; 100 trainings see truncated list.
10. **Cloud training credit error is a generic toast** — no retry hint.
11. **`useVersionCheck` skips on /version.json 404** — could mask stale bundles.
12. **JWT expiry is silent** — next ROS service call fails; user must refresh manually.
13. **`taskInfo` mutations are merged, not replaced** — partial server updates leave stale fields.
14. **iOS audio context suspended by default** — beep on recording start may fail silently.

For UX issues we've cataloged but not fixed (because they're upstream code), see [`22-frontend-followups.md`](22-frontend-followups.md).

---

## 15. Local dev

```bash
cd physical_ai_tools/physical_ai_manager
cp .env.example .env  # if exists, else create
# REACT_APP_SUPABASE_URL=https://fnnbysrjkfugsqzwcksd.supabase.co
# REACT_APP_SUPABASE_ANON_KEY=eyJ...
# REACT_APP_CLOUD_API_URL=http://localhost:8000      # (or Railway URL)
# REACT_APP_MODE=student                              # or "web"
# REACT_APP_ALLOWED_POLICIES=tdmpc,diffusion,act,vqbet,pi0,pi0fast,smolvla
npm install
npm start    # http://localhost:3000
```

For student mode: also need rosbridge running locally (`docker compose up`).
For web mode: just need a logged-in admin/teacher account.

Build:
```bash
npm run build      # outputs to build/
```

---

## 16. Cross-references

- API client → Cloud API: [`10-cloud-api.md`](10-cloud-api.md)
- Mode switch + Dockerfile.web → Docker: [`15-docker.md`](15-docker.md) §8
- Version baking: [`04-env-vars.md`](04-env-vars.md) §7
- Realtime subscription DB side: [`12-supabase.md`](12-supabase.md) §9
- ROS service contract: [`17-ros2-stack.md`](17-ros2-stack.md) §7
- UX follow-ups: [`22-frontend-followups.md`](22-frontend-followups.md)
- Known issues: [`21-known-issues.md`](21-known-issues.md) §3.6

---

**Last verified:** 2026-05-04.
