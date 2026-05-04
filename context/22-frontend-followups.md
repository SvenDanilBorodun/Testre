# 22 — Frontend UX Follow-ups

> **What this file is:** a punch-list of UX issues in `physical_ai_tools/physical_ai_manager/` (the React SPA). Companion to [`13-frontend-react.md`](13-frontend-react.md) (how the frontend works) and [`21-known-issues.md`](21-known-issues.md) §3.6 (severity-ranked).
>
> These are upstream code issues we've identified but not patched. See "How to address these later" at the bottom.

**Why is this only a list and not a fix?** The React app is upstream ROBOTIS
code. We don't ship overlay patches against built JavaScript because every fix
becomes a fragile string-replace against a transpiled bundle that breaks on the
next upstream bump. The right way to address these is either (a) submit a PR
to `ROBOTIS-GIT/physical_ai_tools` upstream, or (b) maintain a fork. Until
then, this list captures the findings so they aren't lost.

## What is already mitigated server-side

| # | Audit finding | Mitigation in this codebase |
|---|---|---|
| Double-click on "Start training" creates duplicate paid jobs | API-side 60s dedupe window in [training.py](cloud_training_api/app/routes/training.py) (`_find_recent_duplicate`). Frontend can spam `/start`; the API returns the same `training_id`. |
| Stale "running" rows from dead workers block credit checks | API-side sweep at the top of `/start` reconciles every running row before counting credits. |
| Untyped `training_params` could request 1B steps | Pydantic `TrainingParams` model with hard upper bounds (`steps≤500k`, `batch≤256`, `timeout≤12h`). |

## Open frontend issues — by severity

### CRITICAL — silent data loss

1. **ROS disconnect during recording** — when rosbridge:9090 drops mid-episode,
   the recording continues silently and the episode is corrupt. There is no
   heartbeat / abort path. Located in
   [src/components/ControlPanel.js:300-350](physical_ai_tools/physical_ai_manager/src/components/ControlPanel.js)
   and the rosConnectionManager hook.
   *Fix idea:* watch the rosbridge connection state, abort the current episode
   if it drops, show a German toast `Verbindung zum Roboter verloren — Aufnahme abgebrochen`.

2. **No confirmation on Finish / Delete dataset** — one wrong click destroys
   hours of recordings.
   *Fix idea:* modal `Sicher? X Episoden werden geloescht.` with 3-second
   delay before confirm is enabled.

### MAJOR — usability

3. **Loading states missing** — `Start training` button shows no spinner during
   API call; students click multiple times. (Mitigated by API dedupe but the UX
   is still confusing.)
   *Fix idea:* disable the button + show inline spinner from request to response.

4. **Modal hell** — 3 stacked modals for policy selection, Escape doesn't close
   them consistently, no breadcrumb. Located in `FileBrowserModal.js`,
   `PolicyDownloadModal.js`, `InferencePanel.js`.

5. **Polling that never stops** — leaving the Training page does not clear the
   polling interval. Browser tab keeps hitting Railway forever.
   *Fix idea:* `useEffect` cleanup return on `TrainingPage` mount.

6. **Image stream memory leak** — `<img>` tags removed from DOM without
   clearing `src`. Long sessions eat browser memory.

### MEDIUM — onboarding & accessibility

7. **No empty states / first-run onboarding** — students see blank grids with
   no hint of what to do.

8. **Browser zoom 125%+ breaks layout** — fixed `w-24` widths overflow.

9. **Accessibility** — color-only status indicators, sparse `aria-label`s,
   undocumented keyboard shortcuts that are listed in tooltips but never wired
   up.

10. **Stale state in multi-task mode** — `started` ref not synced with Redux,
    rapid Start/Stop clicks leave the UI inconsistent.

### LOW — polish

11. **`useEffect` without dependency array** in `ImageGrid.js` line 66-71 —
    runs on every render.

12. **`useRosServiceCaller` 10s timeout fails silently** — no retry, no
    contextual toast.

13. **No dark mode** — long sessions in a bright UI.

14. **No "Test mode" / skip-hardware option** — students without both arms
    cannot reach the GUI flow.

## How to address these later

When time permits:

1. Pick a small batch (e.g. items 1, 2, 3) and submit them as a PR to
   `ROBOTIS-GIT/physical_ai_tools`. They are general improvements that benefit
   any user, not just our edu fork.
2. If upstream is slow, fork `physical_ai_manager`, host the fork at a stable
   git tag, and switch the Docker build context to point at the fork instead
   of the upstream repo.
3. Whatever path is chosen, **do not** ship runtime DOM monkey-patches against
   the built bundle — they will break on the next upstream bump and be
   impossible to debug.
