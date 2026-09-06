// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// „Deine Arbeit" — what the student has actually made, from the cloud.
//
// FOUR fetches, once on mount plus `useRefetchOnFocus`. Deliberately NOT live:
// no Realtime channel, no interval. Start is where every session begins and
// where idle browsers sit for a whole lesson; the cloud API runs a single
// uvicorn worker with an in-process rate limiter, and `/workflows` is per-user
// keyed, so thirty tabs polling would be a self-inflicted outage. Coming back
// to the window is the only moment these numbers can have changed for a reason
// the student cares about.
//
// EVERY COUNT IS NULLABLE ON PURPOSE. `null` means "we could not ask" and
// renders as „—"; `0` means "we asked and there are none". The `|| 0` that
// collapses the two is the exact bug this page is being rebuilt to remove — it
// reports a network failure as an empty portfolio.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSelector } from 'react-redux';

import { listDatasets } from '../services/datasetsApi';
import { listWorkflows } from '../services/workflowApi';
import { getQuota, getTrainingJobs } from '../services/cloudTrainingApi';
import useRefetchOnFocus from './useRefetchOnFocus';

/** Settle a promise into `{ok, value}` so one failed fetch cannot blank the others. */
async function settle(promise) {
  try {
    return { ok: true, value: await promise };
  } catch (err) {
    return { ok: false, value: null, error: err };
  }
}

const asArray = (v) => (Array.isArray(v) ? v : []);

/**
 * Sum `episode_count` across datasets, counting only the rows that HAVE one.
 *
 * The column is nullable (`datasets.episode_count INTEGER`, and DatasetRegister
 * declares `int | None`), so older or partially-registered datasets carry no
 * count. `reduce((a, d) => a + (d.episode_count || 0), 0)` would report too few
 * episodes with the confidence of a measurement. Returns null when not a single
 * dataset has a count — „—" is right there, 0 is not.
 */
export function sumEpisodes(datasets) {
  let total = 0;
  let known = 0;
  for (const d of asArray(datasets)) {
    const n = d && d.episode_count;
    if (typeof n === 'number' && Number.isFinite(n) && n >= 0) {
      total += n;
      known += 1;
    }
  }
  return known > 0 ? total : null;
}

/**
 * The student's OWN saved programs.
 *
 * `GET /workflows` returns the caller's workflows PLUS the classroom's
 * templates (see routes/workflows.py::list_workflows), so the raw length would
 * credit a student with every template their teacher published. Filtering on
 * both `owner_user_id` and `!is_template` is what makes the number theirs —
 * the second test alone would still count a template they authored, which for
 * a student is not a thing that exists but for a teacher previewing the
 * student app would be.
 */
export function ownWorkflows(workflows, userId) {
  if (!userId) return [];
  return asArray(workflows).filter((w) => w && w.owner_user_id === userId && !w.is_template);
}

/** Newest first by whichever timestamp the row carries. */
function newestBy(rows, field) {
  let best = null;
  let bestTs = -Infinity;
  for (const r of asArray(rows)) {
    const ts = Date.parse((r && r[field]) || '');
    if (Number.isFinite(ts) && ts > bestTs) {
      bestTs = ts;
      best = r;
    }
  }
  return best;
}

/**
 * German relative date, coarse on purpose: this is a "when did I last touch
 * this" cue in a weekly lesson, not a timestamp. Returns '' for anything
 * unparseable rather than „Invalid Date".
 */
export function relativeDe(iso) {
  const ts = Date.parse(iso || '');
  if (!Number.isFinite(ts)) return '';
  const mins = Math.floor((Date.now() - ts) / 60000);
  if (mins < 0) return 'gerade eben';
  if (mins < 2) return 'gerade eben';
  if (mins < 60) return `vor ${mins} Minuten`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours === 1 ? 'vor 1 Stunde' : `vor ${hours} Stunden`;
  const days = Math.floor(hours / 24);
  if (days === 1) return 'gestern';
  if (days < 31) return `vor ${days} Tagen`;
  const months = Math.floor(days / 30);
  return months === 1 ? 'vor 1 Monat' : `vor ${months} Monaten`;
}

const EMPTY = Object.freeze({
  datasetCount: null,
  episodeCount: null,
  modelCount: null,
  workflowCount: null,
  creditsRemaining: null,
  creditsTotal: null,
  runningTrainings: null,
  lastDataset: null,
  lastWorkflow: null,
});

/**
 * @returns {{work: object, loading: boolean, reload: function}}
 */
export default function useStudentWork({ enabled = true } = {}) {
  const token = useSelector((s) => s.auth.session?.access_token);
  const userId = useSelector((s) => s.auth.session?.user?.id);
  const [work, setWork] = useState(EMPTY);
  const [loading, setLoading] = useState(false);
  // Guards a late response from a previous token/user overwriting a newer one
  // — a real path on this page, where „Arme scannen" hands the PC to the next
  // student and the app re-mounts under a different session.
  const runIdRef = useRef(0);

  const load = useCallback(async () => {
    if (!enabled || !token) {
      setWork(EMPTY);
      return;
    }
    const runId = runIdRef.current + 1;
    runIdRef.current = runId;
    setLoading(true);

    const [datasets, trainings, workflows, quota] = await Promise.all([
      settle(listDatasets(token)),
      settle(getTrainingJobs(token)),
      settle(listWorkflows(token)),
      settle(getQuota(token)),
    ]);

    if (runIdRef.current !== runId) return; // a newer load already landed

    // `is_owned` is computed server-side against the viewer; a group-shared
    // dataset a classmate recorded is visible here but is not this student's
    // work, so it is excluded from the count and from „Zuletzt aufgenommen".
    const ownDatasets = datasets.ok
      ? asArray(datasets.value).filter((d) => d && d.is_owned)
      : null;
    const jobs = trainings.ok ? asArray(trainings.value) : null;
    const own = workflows.ok ? ownWorkflows(workflows.value, userId) : null;

    setWork({
      datasetCount: ownDatasets ? ownDatasets.length : null,
      episodeCount: ownDatasets ? sumEpisodes(ownDatasets) : null,
      // NOTE: `/trainings/list` includes a workgroup's shared trainings and
      // TrainingJob carries no owner id, so this cannot be narrowed to "mine"
      // on the client. The card says so in German when the student is in a
      // group rather than quietly presenting the group's models as theirs.
      modelCount: jobs ? jobs.filter((j) => j && j.status === 'succeeded').length : null,
      runningTrainings: jobs
        ? jobs.filter((j) => j && (j.status === 'running' || j.status === 'queued')).length
        : null,
      workflowCount: own ? own.length : null,
      creditsRemaining: quota.ok && typeof quota.value?.remaining === 'number'
        ? quota.value.remaining : null,
      creditsTotal: quota.ok && typeof quota.value?.training_credits === 'number'
        ? quota.value.training_credits : null,
      lastDataset: ownDatasets ? newestBy(ownDatasets, 'created_at') : null,
      lastWorkflow: own ? newestBy(own, 'updated_at') : null,
    });
    setLoading(false);
  }, [enabled, token, userId]);

  useEffect(() => {
    load();
  }, [load]);

  useRefetchOnFocus(enabled ? load : undefined);

  return useMemo(() => ({ work, loading, reload: load }), [work, loading, load]);
}
