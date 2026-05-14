import { useCallback, useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import { supabase } from '../lib/supabaseClient';
import { listTutorialProgress } from '../services/tutorialApi';

const POLL_FALLBACK_MS = 30000;

/**
 * Audit U1: realtime subscriber for `public.tutorial_progress`. The
 * table is in the supabase_realtime publication (migration 016, audit
 * round-3 §J) precisely so the teacher dashboard can live-update —
 * but there was no consumer in the codebase before this hook, so
 * `tutorial_progress` updates only reached the UI on page reload.
 *
 * Mirrors `useSupabaseWorkflows`: bootstrap via the REST endpoint,
 * subscribe to per-user postgres_changes filtered by user_id, fall
 * back to a 30 s poll when no channel is active. Returns a map keyed
 * by `tutorial_id` so SkillmapPlayer can render check-marks per row
 * without scanning a flat list each render.
 *
 * Token-rotation race guard is the same belt-and-suspenders pattern
 * useSupabaseWorkflows uses (`inFlightTokenRef`): a fetch started
 * before signOut completes won't write to state for the next user.
 */
export default function useSupabaseTutorialProgress() {
  const session = useSelector((s) => s.auth.session);
  const accessToken = session?.access_token;
  const userId = session?.user?.id;

  // progress is { [tutorialId]: { current_step, completed_at, updated_at } }
  const [progress, setProgress] = useState({});
  const [loading, setLoading] = useState(false);
  const [isRealtime, setIsRealtime] = useState(false);

  const isMountedRef = useRef(true);
  const inFlightTokenRef = useRef(null);
  const fetchRef = useRef(null);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const refetch = useCallback(async () => {
    if (!accessToken) return;
    inFlightTokenRef.current = accessToken;
    setLoading(true);
    try {
      const rows = await listTutorialProgress(accessToken);
      if (!isMountedRef.current) return;
      if (inFlightTokenRef.current !== accessToken) return;
      const map = {};
      for (const r of rows || []) {
        if (r && r.tutorial_id) {
          map[r.tutorial_id] = r;
        }
      }
      setProgress(map);
    } catch (e) {
      // Swallow — students without a row yet just have an empty map.
      // The teacher dashboard is the only consumer that strictly
      // needs visibility; the SkillmapPlayer renders fine on an empty
      // progress map.
      if (process?.env?.NODE_ENV !== 'production') {
        // eslint-disable-next-line no-console
        console.warn('[useSupabaseTutorialProgress] refetch failed:', e?.message || e);
      }
    } finally {
      if (isMountedRef.current && inFlightTokenRef.current === accessToken) {
        setLoading(false);
      }
    }
  }, [accessToken]);

  fetchRef.current = refetch;

  useEffect(() => {
    if (!accessToken) {
      setProgress({});
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
      .channel(`tutorial_progress:user:${userId}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'tutorial_progress',
          filter: `user_id=eq.${userId}`,
        },
        (payload) => {
          if (!isMountedRef.current) return;
          const { eventType, new: newRow, old: oldRow } = payload;
          if (eventType === 'DELETE' && oldRow?.tutorial_id) {
            setProgress((prev) => {
              const next = { ...prev };
              delete next[oldRow.tutorial_id];
              return next;
            });
            return;
          }
          if (newRow?.tutorial_id) {
            setProgress((prev) => ({ ...prev, [newRow.tutorial_id]: newRow }));
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

  return { progress, loading, refetch, isRealtime };
}
