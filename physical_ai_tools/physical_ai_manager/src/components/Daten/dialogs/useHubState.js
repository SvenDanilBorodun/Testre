// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The compare dialogs' one read (spec §J.4.4): `hubstate` of the dataset when
// the dialog opens. Its `hub.head` is the head the dialog SHOWS and therefore
// the one the action sends (`expected_hub_sha` of an upload, `revision` of a
// pull, the head of „Beide behalten"), so a hub that moved between the dialog
// and the click is caught by the robot (§E2).

import { useEffect, useState } from 'react';

export default function useHubState(fetchHubState, id) {
  const [state, setState] = useState({ status: 'loading', data: null });
  useEffect(() => {
    let cancelled = false;
    setState({ status: 'loading', data: null });
    fetchHubState(id).then((data) => {
      if (!cancelled) setState({ status: 'ready', data });
    }).catch(() => {
      if (!cancelled) setState({ status: 'error', data: null });
    });
    return () => { cancelled = true; };
  }, [fetchHubState, id]);
  return state;
}
