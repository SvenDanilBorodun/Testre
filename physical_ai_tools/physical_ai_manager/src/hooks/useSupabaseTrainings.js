import { useCallback, useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import { supabase } from '../lib/supabaseClient';
import { getTrainingJobs } from '../services/cloudTrainingApi';

// Railway /trainings/list reconciliation cadence (catches dead/stalled workers
// + terminal transitions that only the Modal-reconciliation layer detects).
const POLL_FALLBACK_MS = 30000;
// Direct-Supabase live cadence while a job is active. Cheap single-table read;
// runs REGARDLESS of realtime status so a "subscribed but silent" channel can
// never freeze the chart.
const ACTIVE_POLL_MS = 6000;

// Explicit, secret-free column set for the direct live read. Pulling only what
// the UI needs avoids ever putting worker_token on the wire (the realtime path
// delivers the whole row and strips it client-side; this is strictly tighter).
const LIVE_COLUMNS =
  'id,status,model_name,model_type,dataset_name,current_step,total_steps,' +
  'current_loss,loss_history,error_message,requested_at,terminated_at,' +
  'last_progress_at,workgroup_id';

function isActiveJob(j) {
  return j && (j.status === 'queued' || j.status === 'running');
}

function stripSecrets(row) {
  if (!row) return row;
  // worker_token is a per-row secret, cloud_job_id and user_id are internal.
  const { worker_token: _wt, user_id: _uid, cloud_job_id: _cj, ...safe } = row;
  return safe;
}

function mergeJob(prev, incoming) {
  const map = new Map(prev.map((j) => [j.id, j]));
  map.set(incoming.id, { ...(map.get(incoming.id) || {}), ...incoming });
  return Array.from(map.values()).sort(
    (a, b) => new Date(b.requested_at) - new Date(a.requested_at),
  );
}

/**
 * Returns the user's training jobs and keeps the list live.
 *
 * Primary channel: Supabase Realtime on `public.trainings` with filter
 * `user_id=eq.<uid>`. New rows appear <500ms after insert; progress updates
 * stream in as the Modal worker bumps `current_step` / `loss_history`.
 *
 * When the user is in a workgroup we open a *second* channel filtered on
 * `workgroup_id=eq.<gid>` so siblings' trainings stream in too. Supabase
 * Realtime filters are single-column, so OR is implemented as two channels.
 *
 * Bootstrap: one call to the Railway `/trainings/list` endpoint so we benefit
 * from its Modal-reconciliation layer (wedged workers, stale `running` rows).
 *
 * Fallback: if the realtime channel is not SUBSCRIBED, a 30s interval re-hits
 * the Railway list endpoint. This self-heals after network blips / server
 * disconnects.
 */
export default function useSupabaseTrainings() {
  const session = useSelector((s) => s.auth.session);
  const workgroupId = useSelector((s) => s.auth.workgroupId);
  const accessToken = session?.access_token;
  const userId = session?.user?.id;

  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [isUserRealtime, setIsUserRealtime] = useState(false);
  const [isGroupRealtime, setIsGroupRealtime] = useState(false);
  // We treat the hook as "realtime" when at least one channel is subscribed.
  const isRealtime = isUserRealtime || (workgroupId ? isGroupRealtime : false);

  const isMountedRef = useRef(true);
  const fetchRef = useRef(null);
  const directRef = useRef(null);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const refetch = useCallback(async () => {
    if (!accessToken) return;
    setLoading(true);
    try {
      const data = await getTrainingJobs(accessToken);
      if (isMountedRef.current) setJobs(data);
    } catch (e) {
      console.warn('[useSupabaseTrainings] refetch failed:', e?.message || e);
    } finally {
      if (isMountedRef.current) setLoading(false);
    }
  }, [accessToken]);

  fetchRef.current = refetch;

  // Direct-Supabase live read of the user's (and group's) training rows. This
  // is the belt-and-suspenders against the documented failure where the
  // realtime channel reports SUBSCRIBED but delivers zero postgres_changes
  // events (RLS-recursion class, migration 031) — the chart would freeze and
  // jump only at the end because the 30s poll below never engages while
  // isRealtime is true. This read goes straight to the row over REST (RLS
  // scopes it to the caller, same gate the realtime channel uses), so it keeps
  // current_step / loss_history / last_progress_at flowing every few seconds
  // no matter what the websocket is doing. Cheap: one filtered single-table
  // select, no Modal/Railway round-trip.
  const pollActiveDirect = useCallback(async () => {
    if (!userId) return;
    try {
      let query = supabase.from('trainings').select(LIVE_COLUMNS);
      query = workgroupId
        ? query.or(`user_id.eq.${userId},workgroup_id.eq.${workgroupId}`)
        : query.eq('user_id', userId);
      const { data, error } = await query.order('requested_at', { ascending: false });
      if (error || !data || !isMountedRef.current) return;
      setJobs((prev) => {
        let next = prev;
        for (const row of data) next = mergeJob(next, row);
        return next;
      });
    } catch (e) {
      // Network blip — the Railway reconciliation poll is the backstop.
      console.warn('[useSupabaseTrainings] direct poll failed:', e?.message || e);
    }
  }, [userId, workgroupId]);

  directRef.current = pollActiveDirect;

  useEffect(() => {
    if (!accessToken) {
      setJobs([]);
      return;
    }
    refetch();
  }, [accessToken, refetch]);

  // Channel A: own trainings (user_id=eq.<uid>)
  useEffect(() => {
    if (!userId) {
      setIsUserRealtime(false);
      return undefined;
    }

    const channel = supabase
      .channel(`trainings:user:${userId}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'trainings',
          filter: `user_id=eq.${userId}`,
        },
        (payload) => {
          if (!isMountedRef.current) return;
          const { eventType, new: newRow, old: oldRow } = payload;
          if (eventType === 'DELETE') {
            setJobs((prev) => prev.filter((j) => j.id !== oldRow?.id));
            return;
          }
          if (newRow) {
            setJobs((prev) => mergeJob(prev, stripSecrets(newRow)));
          }
        },
      )
      .subscribe((status) => {
        if (!isMountedRef.current) return;
        setIsUserRealtime(status === 'SUBSCRIBED');
      });

    return () => {
      supabase.removeChannel(channel);
    };
  }, [userId]);

  // Channel B: group siblings' trainings (workgroup_id=eq.<gid>). Only
  // active when the user is in a group; tears down on group change/leave.
  useEffect(() => {
    if (!workgroupId) {
      setIsGroupRealtime(false);
      return undefined;
    }

    const channel = supabase
      .channel(`trainings:group:${workgroupId}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'trainings',
          filter: `workgroup_id=eq.${workgroupId}`,
        },
        (payload) => {
          if (!isMountedRef.current) return;
          const { eventType, new: newRow, old: oldRow } = payload;
          if (eventType === 'DELETE') {
            setJobs((prev) => prev.filter((j) => j.id !== oldRow?.id));
            return;
          }
          if (newRow) {
            setJobs((prev) => mergeJob(prev, stripSecrets(newRow)));
          }
        },
      )
      .subscribe((status) => {
        if (!isMountedRef.current) return;
        setIsGroupRealtime(status === 'SUBSCRIBED');
      });

    return () => {
      supabase.removeChannel(channel);
    };
  }, [workgroupId]);

  const hasActive = jobs.some(isActiveJob);

  // Railway reconciliation poll (30s): when realtime is down (catch new rows /
  // status the websocket isn't delivering) OR a job is active (only the Modal
  // reconciliation in /trainings/list flips a dead/stalled worker to failed —
  // the direct read below would otherwise show it "running" forever).
  useEffect(() => {
    if (!accessToken) return undefined;
    if (isRealtime && !hasActive) return undefined;
    const id = setInterval(() => fetchRef.current?.(), POLL_FALLBACK_MS);
    return () => clearInterval(id);
  }, [isRealtime, hasActive, accessToken]);

  // Live progress heartbeat (6s): runs whenever a job is active, independent of
  // the realtime channel. This is what keeps the chart moving in real time.
  useEffect(() => {
    if (!accessToken || !hasActive) return undefined;
    const id = setInterval(() => directRef.current?.(), ACTIVE_POLL_MS);
    return () => clearInterval(id);
  }, [accessToken, hasActive]);

  return { jobs, loading, refetch, isRealtime };
}
