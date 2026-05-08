import { useCallback, useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import { supabase } from '../lib/supabaseClient';
import { listDatasets } from '../services/datasetsApi';

const POLL_FALLBACK_MS = 30000;

function mergeRow(prev, incoming) {
  const map = new Map(prev.map((d) => [d.id, d]));
  map.set(incoming.id, { ...(map.get(incoming.id) || {}), ...incoming });
  return Array.from(map.values()).sort(
    (a, b) => new Date(b.updated_at) - new Date(a.updated_at),
  );
}

/**
 * Returns the registered datasets visible to the current user (own +
 * group-shared) and keeps the list live.
 *
 * Bootstraps from `/datasets` (REST). Then opens up to two Supabase
 * Realtime channels on `public.datasets` (filter by owner_user_id and by
 * workgroup_id when set) — the same pattern as useSupabaseTrainings.
 *
 * Falls back to a 30s poll if neither channel is SUBSCRIBED.
 */
export default function useGroupDatasets() {
  const session = useSelector((s) => s.auth.session);
  const workgroupId = useSelector((s) => s.auth.workgroupId);
  const accessToken = session?.access_token;
  const userId = session?.user?.id;

  const [datasets, setDatasets] = useState([]);
  const [loading, setLoading] = useState(false);
  const [isOwnRealtime, setIsOwnRealtime] = useState(false);
  const [isGroupRealtime, setIsGroupRealtime] = useState(false);
  const isRealtime = isOwnRealtime || (workgroupId ? isGroupRealtime : false);

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
      const data = await listDatasets(accessToken);
      if (isMountedRef.current) setDatasets(data);
    } catch (e) {
      console.warn('[useGroupDatasets] refetch failed:', e?.message || e);
    } finally {
      if (isMountedRef.current) setLoading(false);
    }
  }, [accessToken]);

  fetchRef.current = refetch;

  useEffect(() => {
    if (!accessToken) {
      setDatasets([]);
      return;
    }
    refetch();
  }, [accessToken, refetch]);

  useEffect(() => {
    if (!userId) {
      setIsOwnRealtime(false);
      return undefined;
    }
    const channel = supabase
      .channel(`datasets:user:${userId}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'datasets',
          filter: `owner_user_id=eq.${userId}`,
        },
        (payload) => {
          if (!isMountedRef.current) return;
          const { eventType, new: newRow, old: oldRow } = payload;
          if (eventType === 'DELETE') {
            setDatasets((prev) => prev.filter((d) => d.id !== oldRow?.id));
            return;
          }
          if (newRow) {
            setDatasets((prev) => mergeRow(prev, newRow));
          }
        },
      )
      .subscribe((status) => {
        if (!isMountedRef.current) return;
        setIsOwnRealtime(status === 'SUBSCRIBED');
      });
    return () => {
      supabase.removeChannel(channel);
    };
  }, [userId]);

  useEffect(() => {
    if (!workgroupId) {
      setIsGroupRealtime(false);
      return undefined;
    }
    const channel = supabase
      .channel(`datasets:group:${workgroupId}`)
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'datasets',
          filter: `workgroup_id=eq.${workgroupId}`,
        },
        (payload) => {
          if (!isMountedRef.current) return;
          const { eventType, new: newRow, old: oldRow } = payload;
          if (eventType === 'DELETE') {
            setDatasets((prev) => prev.filter((d) => d.id !== oldRow?.id));
            return;
          }
          if (newRow) {
            setDatasets((prev) => mergeRow(prev, newRow));
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

  return { datasets, loading, refetch, isRealtime };
}
