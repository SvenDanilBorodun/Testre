// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Training page's warning (owner decision D6, spec §E9, R-4): is the
// dataset picked for training changed here and not uploaded (`changed` /
// `conflict`), or only on this robot (`local`)? One link token and ONE
// `library?ids=<id>&hub=1` — with `ids=` the sidecar asks Hugging Face only for
// that repo. Everything that is not proof stays SILENT (null): `unknown`,
// `current`, `newer`, a robot that does not answer, cloud mode, an old image,
// a token that is not this student's, a failed ask. „Nur hier" needs the
// robot to hold this student's token (the hub was asked and said „absent").

import { useEffect, useState } from 'react';
import { useSelector } from 'react-redux';

import useDatenCommand from './useDatenCommand';
import { getJson, libUrl } from '../api/datenHttp';
import { REPO_ID_RE } from '../datenContract';
import { selectHfAccount, selectHfInSync } from '../../hfToken/hfTokenSelectors';
import { isCloudOnlyMode } from '../../../utils/cloudMode';

const REPO = new RegExp(REPO_ID_RE);

/** The warning a library reply proves for `repoId`: 'changed' | 'local' | null. */
export function warningFor(reply, repoId, { inSync, accountFp }) {
  if (!reply || !Array.isArray(reply.local)) return null;
  if (!reply.local.some((e) => e && e.id === repoId)) return null;
  const s = reply.sync && reply.sync[repoId] ? reply.sync[repoId].state : null;
  if (s === 'changed' || s === 'conflict') return 'changed';
  if (s === 'local') {
    const hub = reply.hub || {};
    const proven = inSync && hub.state === 'ok' && !!accountFp && hub.token_fp === accountFp;
    return proven ? 'local' : null;
  }
  return null;
}

/** @returns {'changed'|'local'|null} */
export default function useDatasetLocalChanges(repoId) {
  const command = useDatenCommand();
  const connected = useSelector((s) => s.tasks.heartbeatStatus === 'connected');
  const inSync = useSelector(selectHfInSync);
  const accountFp = useSelector((s) => selectHfAccount(s).fp);
  const [warning, setWarning] = useState(null);

  useEffect(() => {
    setWarning(null);
    const id = String(repoId || '');
    if (isCloudOnlyMode() || !connected || !REPO.test(id)) return undefined;
    let cancelled = false;
    (async () => {
      const r = await command('link', { library: true, datasets: [] });
      if (cancelled || !r.ok || !r.result.library_token) return; // an old image, a refusal: silent
      try {
        const reply = await getJson(libUrl(r.result.library_token, 'library', {
          ns: [id.split('/')[0]], hub: 1, ids: [id],
        }));
        if (!cancelled) setWarning(warningFor(reply, id, { inSync, accountFp }));
      } catch {
        /* a failed ask stays silent */
      }
    })();
    return () => { cancelled = true; };
  }, [repoId, connected, inSync, accountFp, command]);

  return warning;
}
