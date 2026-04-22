import { useCallback, useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import { supabase } from '../lib/supabaseClient';
import { getTrainingJobs } from '../services/cloudTrainingApi';

const POLL_FALLBACK_MS = 30000;

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
 * Bootstrap: one call to the Railway `/trainings/list` endpoint so we benefit
 * from its Modal-reconciliation layer (wedged workers, stale `running` rows).
 *
 * Fallback: if the realtime channel is not SUBSCRIBED, a 30s interval re-hits
 * the Railway list endpoint. This self-heals after network blips / server
 * disconnects.
 */
export default function useSupabaseTrainings() {
  const session = useSelector((s) => s.auth.session);
  const accessToken = session?.access_token;
  const userId = session?.user?.id;

  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [isRealtime, setIsRealtime] = useState(false);

  const isMountedRef = useRef(true);
  const fetchRef = useRef(null);

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

  useEffect(() => {
    if (!accessToken) {
      setJobs([]);
      return;
    }
    refetch();
  }, [accessToken, refetch]);

  useEffect(() => {
    if (!userId) {
      setIsRealtime(false);
      return undefined;
    }

    const channel = supabase
      .channel(`trainings:${userId}`)
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
        setIsRealtime(status === 'SUBSCRIBED');
      });

    return () => {
      supabase.removeChannel(channel);
    };
  }, [userId]);

  useEffect(() => {
    if (isRealtime || !accessToken) return undefined;
    const id = setInterval(() => fetchRef.current?.(), POLL_FALLBACK_MS);
    return () => clearInterval(id);
  }, [isRealtime, accessToken]);

  return { jobs, loading, refetch, isRealtime };
}
