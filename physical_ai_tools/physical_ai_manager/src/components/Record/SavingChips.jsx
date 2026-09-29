// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The two chips of Speichern (spec §3.5): „Episode n wird gespeichert …" with
// the loader in the middle of the stage while the episode is saved, then
// „Episode n gespeichert" dropping in at the top. There is no progress bar:
// the streaming encoder reports no progress (H6).
//
// Timing is part of the design, not decoration. SAVING can last only two
// record ticks (~66 ms, H12), so the saving chip stays at least
// SAVING_MIN_MS once it appeared. The saved chip shows SAVED_MS for each NEW
// saved episode (`savedKey` changes); one that was already saved when the page
// opened is not announced again.

import React, { useEffect, useRef, useState } from 'react';
import Icon from '../icons/Icon';

export const SAVING_MIN_MS = 400;
export const SAVED_MS = 1200;

export default function SavingChips({ saving = false, savingText = '', savedKey = null, savedText = '' }) {
  const [holdSaving, setHoldSaving] = useState(false);
  const savingSinceRef = useRef(null);

  useEffect(() => {
    if (saving) {
      savingSinceRef.current = Date.now();
      setHoldSaving(true);
      return undefined;
    }
    if (savingSinceRef.current === null) return undefined;
    const left = SAVING_MIN_MS - (Date.now() - savingSinceRef.current);
    savingSinceRef.current = null;
    if (left <= 0) {
      setHoldSaving(false);
      return undefined;
    }
    const t = setTimeout(() => setHoldSaving(false), left);
    return () => clearTimeout(t);
  }, [saving]);

  // The last text shown, so the chip keeps its words during the hold.
  const savingTextRef = useRef(savingText);
  if (saving && savingText) savingTextRef.current = savingText;

  const [savedShown, setSavedShown] = useState(null);
  const firstKeyRef = useRef(savedKey);
  useEffect(() => {
    if (savedKey === null || savedKey === undefined || savedKey === firstKeyRef.current) return undefined;
    firstKeyRef.current = savedKey;
    setSavedShown({ key: savedKey, text: savedText });
    const t = setTimeout(() => setSavedShown(null), SAVED_MS);
    return () => clearTimeout(t);
    // savedText belongs to this key; a later text change alone is not an event.
  }, [savedKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const showSaving = saving || holdSaving;
  return (
    <>
      {showSaving ? (
        <div className="rec-chip-c" role="status" data-testid="rec-saving-chip">
          <Icon name="loading" className="animate-spin" size={26} />
          {saving ? savingText : savingTextRef.current}
        </div>
      ) : null}
      {savedShown ? (
        <div className="rec-chip-top" key={savedShown.key} role="status" data-testid="rec-saved-chip">
          <Icon name="check" size={18} strokeWidth={3} />
          {savedShown.text}
        </div>
      ) : null}
    </>
  );
}
