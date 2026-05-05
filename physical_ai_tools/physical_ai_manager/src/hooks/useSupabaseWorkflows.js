import { useCallback, useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import { supabase } from '../lib/supabaseClient';
import { listWorkflows } from '../services/workflowApi';

const POLL_FALLBACK_MS = 30000;

function mergeWorkflow(prev, incoming) {
  const map = new Map(prev.map((w) => [w.id, w]));
  map.set(incoming.id, { ...(map.get(incoming.id) || {}), ...incoming });
  return Array.from(map.values()).sort(
    (a, b) => new Date(b.updated_at) - new Date(a.updated_at),
  );
}

/**
 * Returns the user's workflows + classroom templates and keeps the list live.
 * Mirrors useSupabaseTrainings: bootstrap from /workflows REST endpoint,
 * subscribe to public.workflows postgres_changes filtered by owner_user_id,
 * fall back to a 30s polling refetch when realtime isn't subscribed.
 */
export default function useSupabaseWorkflows() {
  const session = useSelector((s) => s.auth.session);
  const accessToken = session?.access_token;
  const userId = session?.user?.id;

  const [workflows, setWorkflows] = useState([]);
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
      const data = await listWorkflows(accessToken);
      if (isMountedRef.current) setWorkflows(data);
    } catch (e) {
      console.warn('[useSupabaseWorkflows] refetch failed:', e?.message || e);
    } finally {
      if (isMountedRef.current) setLoading(false);
    }
  }, [accessToken]);

  fetchRef.current = refetch;

  useEffect(() => {
    if (!accessToken) {
      setWorkflows([]);
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
      .channel(`workflows:${userId}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'workflows',
          filter: `owner_user_id=eq.${userId}`,
        },
        (payload) => {
          if (!isMountedRef.current) return;
          const { eventType, new: newRow, old: oldRow } = payload;
          if (eventType === 'DELETE') {
            setWorkflows((prev) => prev.filter((w) => w.id !== oldRow?.id));
            return;
          }
          if (newRow) {
            setWorkflows((prev) => mergeWorkflow(prev, newRow));
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

  return { workflows, loading, refetch, isRealtime };
}
