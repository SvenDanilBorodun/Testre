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
 * subscribe to public.workflows postgres_changes filtered by owner_user_id.
 *
 * When the user is in a workgroup, also subscribes to a second channel on
 * `workgroup_id=eq.<gid>` so peers' shared workflows stream in. Supabase
 * Realtime filters are single-column, so OR is implemented as two channels.
 *
 * Falls back to a 30s polling refetch when no realtime channel is subscribed.
 */
export default function useSupabaseWorkflows() {
  const session = useSelector((s) => s.auth.session);
  const workgroupId = useSelector((s) => s.auth.workgroupId);
  const accessToken = session?.access_token;
  const userId = session?.user?.id;

  const [workflows, setWorkflows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [isUserRealtime, setIsUserRealtime] = useState(false);
  const [isGroupRealtime, setIsGroupRealtime] = useState(false);
  const isRealtime = isUserRealtime || (workgroupId ? isGroupRealtime : false);

  const isMountedRef = useRef(true);
  const fetchRef = useRef(null);
  const inFlightTokenRef = useRef(null);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  // Audit §3.11 — guard against the realtime polling fallback racing a
  // slow listWorkflows fetch when the accessToken changes mid-flight.
  // Each fetch records the token it was started with; the result is
  // dropped if the token has rotated by the time it resolves.
  const refetch = useCallback(async () => {
    if (!accessToken) return;
    inFlightTokenRef.current = accessToken;
    setLoading(true);
    try {
      const data = await listWorkflows(accessToken);
      if (!isMountedRef.current) return;
      if (inFlightTokenRef.current !== accessToken) return;
      setWorkflows(data);
    } catch (e) {
      console.warn('[useSupabaseWorkflows] refetch failed:', e?.message || e);
    } finally {
      if (isMountedRef.current && inFlightTokenRef.current === accessToken) {
        setLoading(false);
      }
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

  // Channel A: own workflows.
  useEffect(() => {
    if (!userId) {
      setIsUserRealtime(false);
      return undefined;
    }

    const channel = supabase
      .channel(`workflows:user:${userId}`)
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
        setIsUserRealtime(status === 'SUBSCRIBED');
      });

    return () => {
      supabase.removeChannel(channel);
    };
  }, [userId]);

  // Channel B: group-shared workflows.
  useEffect(() => {
    if (!workgroupId) {
      setIsGroupRealtime(false);
      return undefined;
    }
    const channel = supabase
      .channel(`workflows:group:${workgroupId}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'workflows',
          filter: `workgroup_id=eq.${workgroupId}`,
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
        setIsGroupRealtime(status === 'SUBSCRIBED');
      });

    return () => {
      supabase.removeChannel(channel);
    };
  }, [workgroupId]);

  useEffect(() => {
    if (isRealtime || !accessToken) return undefined;
    const id = setInterval(() => fetchRef.current?.(), POLL_FALLBACK_MS);
    return () => clearInterval(id);
  }, [isRealtime, accessToken]);

  return { workflows, loading, refetch, isRealtime };
}
