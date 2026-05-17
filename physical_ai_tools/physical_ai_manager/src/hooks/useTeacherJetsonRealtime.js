// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import { useEffect } from 'react';
import { supabase } from '../lib/supabaseClient';

/**
 * Subscribe to Supabase realtime updates on public.jetsons filtered by
 * classroom_id. Fires `onChange` (debounced via setTimeout 200ms to
 * coalesce write bursts from claim/release/heartbeat) whenever any
 * jetson row in this classroom changes — caller is expected to refetch
 * via the Cloud API to get the enriched JetsonInfo (owner_username,
 * online status derived from last_seen_at, etc).
 *
 * The jetsons table is published via supabase_realtime in migration
 * 019. RLS lets classroom members (incl. teachers) read their own
 * classroom's row — so this works without service-role.
 *
 * If realtime is unavailable (websocket blocked, project setting off),
 * the hook silently no-ops. Callers still see fresh data via the
 * 30-second polling fallback patterns elsewhere; realtime is purely a
 * UX upgrade for the active-classroom case.
 */
export default function useTeacherJetsonRealtime(classroomId, onChange) {
  useEffect(() => {
    if (!classroomId || typeof onChange !== 'function') return undefined;
    let timer = null;
    let channel = null;
    try {
      channel = supabase
        .channel(`jetsons-classroom-${classroomId}`)
        .on(
          'postgres_changes',
          {
            event: '*',
            schema: 'public',
            table: 'jetsons',
            filter: `classroom_id=eq.${classroomId}`,
          },
          () => {
            // Debounce: claim/heartbeat/release can fire 3+ rows in
            // quick succession; one refetch covers them all.
            if (timer) clearTimeout(timer);
            timer = setTimeout(() => {
              try { onChange(); } catch (_) { /* swallow */ }
            }, 200);
          }
        )
        .subscribe();
    } catch (_err) {
      // supabaseClient may be a Proxy that throws on first method call
      // when REACT_APP_SUPABASE_URL is unset (BuildConfigBanner path).
      // Realtime isn't critical; silently degrade.
      channel = null;
    }
    return () => {
      if (timer) clearTimeout(timer);
      if (channel) {
        try { supabase.removeChannel(channel); } catch (_) { /* swallow */ }
      }
    };
  }, [classroomId, onChange]);
}
