// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Aufnahme page's view of /edubotics/daten_state (spec §G11, R-8): while
// a Start waits in STARTING and the robot still uploads the SAME dataset (the
// previous session's auto-upload, a Daten upload), the page says „Wartet, bis
// das Hochladen fertig ist …". The robot waits by itself; this is words only.
//
// The Aufnahme 2.0 render invariant (F-5): it subscribes ONLY while `active`
// (the page is in STARTING) and returns a boolean that changes — and so
// re-renders the page — only when the answer changes, never per message.
// An older image has no such topic: the answer stays false.

import { useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';

import { busyMap, getDatenStateSnapshot, subscribeDatenState } from './useDatenState';

/** Does a daten_state payload say `repoId` is uploading? */
export function isUploading(payload, repoId) {
  return !!repoId && busyMap(payload)[repoId] === 'upload';
}

/**
 * @param {string} repoId the dataset the Start is for (`<user>/<robot>_<task>`)
 * @param {boolean} active true while the page is in its STARTING wait
 * @returns {boolean} waitingForUpload
 */
export default function useDatenStartWait(repoId, active) {
  const url = useSelector((s) => s.ros.rosbridgeUrl);
  const [waiting, setWaiting] = useState(false);
  const shownRef = useRef(false);

  useEffect(() => {
    const show = (v) => {
      if (shownRef.current === v) return;
      shownRef.current = v;
      setWaiting(v);
    };
    if (!active || !repoId || !url) {
      show(false);
      return undefined;
    }
    show(isUploading(getDatenStateSnapshot().payload, repoId));
    const unsubscribe = subscribeDatenState(url, (snap) => show(isUploading(snap.payload, repoId)));
    return () => {
      unsubscribe();
    };
  }, [active, repoId, url]);

  return active ? waiting : false;
}
