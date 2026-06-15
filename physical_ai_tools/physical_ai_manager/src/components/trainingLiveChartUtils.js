// Pure, dependency-free helpers for the cloud-training live view.
//
// Kept in a separate module (no recharts / React import) so they can be unit
// tested in isolation and reused by TrainingLiveChart + MyModels without
// pulling the chart bundle into a test.

export const ACTIVE_STATUSES = new Set(['queued', 'running']);
export const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'canceled']);

// Below this many steps the whole-run average ETA is too noisy to show.
export const ETA_MIN_STEPS = 100;
// A running job whose last write is older than this "feels" frozen — we show a
// gentle hint rather than letting the student wonder.
export const STALE_AFTER_SECONDS = 90;

export function isActiveStatus(status) {
  return ACTIVE_STATUSES.has(status);
}

/**
 * Pick which job the live view focuses on.
 *  - an explicit selectedId wins if it still exists
 *  - otherwise the newest active (queued/running) job
 *  - otherwise the most recently terminated job
 *  - otherwise the newest job of any kind
 */
export function pickSelectedJob(jobs, selectedId) {
  if (!jobs || jobs.length === 0) return null;
  if (selectedId != null) {
    const pinned = jobs.find((j) => j.id === selectedId);
    if (pinned) return pinned;
  }
  const byNewest = (a, b) => new Date(b.requested_at) - new Date(a.requested_at);
  const active = jobs.filter((j) => ACTIVE_STATUSES.has(j.status)).sort(byNewest);
  if (active.length > 0) return active[0];
  const terminated = jobs.filter((j) => TERMINAL_STATUSES.has(j.status)).sort(byNewest);
  return terminated[0] || jobs[0];
}

export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  if (h > 0) return `${pad(h)}:${pad(m)}:${pad(sec)}`;
  return `${pad(m)}:${pad(sec)}`;
}

export function formatStep(step) {
  if (step == null) return '—';
  return Number(step).toLocaleString('de-DE');
}

export function formatLoss(loss) {
  if (loss == null || !Number.isFinite(loss)) return '—';
  const abs = Math.abs(loss);
  if (abs === 0) return '0';
  if (abs < 0.001 || abs >= 1000) return loss.toExponential(2);
  return loss.toFixed(4);
}

export function compactAxis(n) {
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(abs >= 10_000_000 ? 0 : 1)}M`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(abs >= 10_000 ? 0 : 1)}K`;
  return String(n);
}

/** Whole seconds elapsed between requested_at and now (or terminated_at). */
export function elapsedSeconds(job, nowMs) {
  if (!job?.requested_at) return 0;
  const start = new Date(job.requested_at).getTime();
  const end = job.terminated_at ? new Date(job.terminated_at).getTime() : nowMs;
  if (!Number.isFinite(start)) return 0;
  return Math.max(0, (end - start) / 1000);
}

/** Seconds since the worker last wrote progress, or null if unknown/terminal. */
export function progressAgeSeconds(job, nowMs) {
  if (!job || !ACTIVE_STATUSES.has(job.status)) return null;
  if (!job.last_progress_at) return null;
  const t = new Date(job.last_progress_at).getTime();
  if (!Number.isFinite(t)) return null;
  return Math.max(0, (nowMs - t) / 1000);
}

export function formatAge(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  if (seconds < 5) return 'gerade eben';
  if (seconds < 60) return `vor ${Math.round(seconds)} s`;
  if (seconds < 3600) return `vor ${Math.floor(seconds / 60)} min`;
  return `vor ${Math.floor(seconds / 3600)} h`;
}

export function progressPct(job) {
  const total = job?.total_steps || 0;
  const cur = job?.current_step || 0;
  if (total <= 0) return 0;
  return Math.min(100, Math.max(0, (cur / total) * 100));
}

/**
 * Estimate remaining seconds for a running job.
 * Prefers the recent throughput (steps/s over the last <=8 history points,
 * using the `t` ms-epoch timestamps the RPC stamps) so the cold-start phase
 * doesn't drag the estimate. Falls back to the whole-run average.
 * Returns null when there isn't enough signal yet.
 */
export function estimateEtaSeconds(job, history, elapsed) {
  if (!job || job.status !== 'running') return null;
  const total = job.total_steps || 0;
  const cur = job.current_step || 0;
  if (total <= 0 || cur <= 0 || cur >= total) return null;

  const pts = (Array.isArray(history) ? history : []).filter(
    (p) => p && Number.isFinite(p.s) && Number.isFinite(p.t),
  );
  if (pts.length >= 2) {
    const recent = pts.slice(-Math.min(pts.length, 8));
    const first = recent[0];
    const last = recent[recent.length - 1];
    const dSteps = last.s - first.s;
    const dSec = (last.t - first.t) / 1000;
    if (dSteps > 0 && dSec > 0) {
      const rate = dSteps / dSec; // steps per second
      const remaining = (total - cur) / rate;
      if (Number.isFinite(remaining) && remaining >= 0) return remaining;
    }
  }

  if (cur >= ETA_MIN_STEPS && Number.isFinite(elapsed) && elapsed > 0) {
    return ((total - cur) / cur) * elapsed;
  }
  return null;
}
