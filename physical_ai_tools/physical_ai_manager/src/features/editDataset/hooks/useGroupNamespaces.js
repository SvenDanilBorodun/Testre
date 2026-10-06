// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Whose datasets the Daten tab lists (owner decision D2, spec §C1): the signed-
// in student's own Hugging Face account plus every member of their CURRENT
// workgroup that has one — never an organisation. UI scoping only, never a
// security boundary. The members come from `GET /me/group-members` once per
// session (cached per signed-in user, so a sign-out never hands the list to
// the next student); a failure lists the student's own account alone.

import { useEffect, useMemo, useState } from 'react';
import { useSelector } from 'react-redux';

import { getGroupMembers } from '../../../services/meApi';

// user id → Promise<{members}> (one request per session and user).
const cache = new Map();

/** Test seam. */
export function resetGroupNamespacesCache() {
  cache.clear();
}

function loadMembers(userId, accessToken) {
  if (!cache.has(userId)) {
    const p = getGroupMembers(accessToken).catch((err) => {
      cache.delete(userId); // a later mount may ask again
      throw err;
    });
    cache.set(userId, p);
  }
  return cache.get(userId);
}

/**
 * @returns {{status: 'idle'|'loading'|'ready'|'error', own: string|null,
 *   namespaces: string[], names: Object<string, string>}}
 *   `namespaces` starts with the student's own; `names` maps a partner's
 *   namespace to their display name (the owner chip).
 */
export default function useGroupNamespaces() {
  const own = useSelector((s) => s.auth.hfUsername) || null;
  const userId = useSelector((s) => s.auth.session?.user?.id) || null;
  const accessToken = useSelector((s) => s.auth.session?.access_token) || null;
  const [state, setState] = useState({ key: null, status: 'idle', members: [] });

  useEffect(() => {
    if (!own || !userId || !accessToken) {
      setState({ key: null, status: 'idle', members: [] });
      return undefined;
    }
    let cancelled = false;
    setState((prev) => (prev.key === userId && prev.status === 'ready' ? prev : { key: userId, status: 'loading', members: [] }));
    loadMembers(userId, accessToken).then((body) => {
      if (cancelled) return;
      const members = Array.isArray(body && body.members) ? body.members : [];
      setState({ key: userId, status: 'ready', members });
    }).catch((err) => {
      if (cancelled) return;
      console.warn('[daten] group members unavailable:', err && err.message);
      setState({ key: userId, status: 'error', members: [] });
    });
    return () => { cancelled = true; };
    // The token string rotates on every Supabase refresh; the list belongs to
    // the user, not to one token.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [own, userId]);

  return useMemo(() => {
    if (!own) return { status: state.status, own: null, namespaces: [], names: {} };
    const namespaces = [own];
    const names = {};
    state.members.forEach((m) => {
      const ns = m && typeof m.hf_username === 'string' ? m.hf_username.trim() : '';
      if (!ns || m.is_me || ns === own || namespaces.includes(ns)) return;
      namespaces.push(ns);
      names[ns] = (m.full_name && String(m.full_name).trim()) || ns;
    });
    return { status: state.status, own, namespaces, names };
  }, [own, state]);
}
