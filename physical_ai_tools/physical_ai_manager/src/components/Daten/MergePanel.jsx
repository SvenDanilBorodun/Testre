// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Zusammenführen" (the mockup's merge panel, spec §D5): the picked datasets,
// the seven compatibility checks BEFORE anything starts, the new dataset's
// name and where it will be saved (always the student's own namespace, §D2).
// The button stays disabled while any check fails or the name is unusable.

import React, { useMemo, useState } from 'react';
import Icon from '../icons/Icon';
import { releasePointerFocus } from '../Record/ActionBar';
import COPY from '../../features/editDataset/datenCopy';
import { mergeChecks } from '../../features/editDataset/model/mergeChecks';
import { fill } from '../../features/editDataset/model/format';
import { safeTaskName } from '../../utils/datasetName';

/** `<own>/<robot>_<safe name>` — the same rule as a recording (§D2). */
export function newDatasetId(own, robotType, name) {
  const safe = safeTaskName(name);
  return safe ? `${own}/${robotType}_${safe}` : '';
}

export default function MergePanel({
  entries, own, ownerNames, robotType, existingIds, onGo, onCancel, busy = false,
}) {
  const sel = useMemo(() => entries || [], [entries]);
  const [name, setName] = useState(null);
  const defaultName = sel[0] ? fill(COPY.merge.defaultName, { name: sel[0].display_name || sel[0].name }) : '';
  const value = name ?? defaultName;
  const { checks, ok } = useMemo(() => mergeChecks(sel), [sel]);
  const eps = sel.reduce((a, e) => a + (Number(e.total_episodes) || 0), 0);
  const target = newDatasetId(own, robotType || (sel[0] && sel[0].robot_type) || '', value.trim());
  const trimmed = value.trim();
  let nameError = '';
  if (!trimmed || !target) nameError = COPY.tool.errNoName;
  else if (existingIds && existingIds.has(target)) nameError = COPY.tool.errNameExists;
  const canGo = sel.length >= 2 && ok && !nameError && !busy;

  return (
    <section className="dat-merge-panel" aria-label={COPY.merge.title}>
      <div>
        <div className="dat-mp-h">
          <Icon name="mergeData" size={16} />
          {COPY.merge.title}
          <span className="dat-small">{fill(COPY.merge.selected, { n: sel.length })}</span>
        </div>
        <div className="dat-mp-sel">
          {sel.length ? sel.map((e) => (
            <span key={e.id}>
              {e.ns !== own
                ? fill(COPY.merge.chipPartner, { name: e.display_name || e.name, owner: (ownerNames && ownerNames[e.ns]) || e.ns, n: e.total_episodes })
                : fill(COPY.merge.chip, { name: e.display_name || e.name, n: e.total_episodes })}
            </span>
          )) : <span className="dat-small" style={{ border: 0, background: 'none', padding: 0 }}>{COPY.merge.pickHint}</span>}
        </div>
        <p className="dat-small" style={{ margin: '10px 0 0' }}>{COPY.merge.unchanged}</p>
      </div>
      <div>
        <ul className="dat-checks">
          {checks.map((c) => (
            <li key={c.id} data-check={c.id} data-ok={c.ok ? 'true' : 'false'}>
              <Icon name={c.ok ? 'checkCircle' : 'cancel'} size={16} className={c.ok ? 'dat-ok' : 'dat-bad'} />
              <span>{c.text}</span>
            </li>
          ))}
        </ul>
        <label className="dat-field" style={{ marginTop: 12 }}>
          {COPY.merge.nameLabel}
          <input value={value} onChange={(e) => setName(e.target.value)} autoComplete="off" />
        </label>
        {nameError && sel.length >= 2
          ? <ul className="dat-errs"><li>{nameError}</li></ul>
          : <div className="dat-small dat-mono" style={{ marginTop: 5 }}>{fill(COPY.merge.savedAs, { id: target })}</div>}
      </div>
      <div className="dat-mp-actions">
        <button
          type="button"
          className="dat-btn dat-btn-primary"
          disabled={!canGo}
          onClick={(e) => { releasePointerFocus(e); onGo({ name: trimmed, target }); }}
        >
          <Icon name="mergeData" size={16} />
          {sel.length >= 2 ? fill(COPY.merge.goCount, { n: eps }) : COPY.merge.go}
        </button>
        <button type="button" className="dat-btn" onClick={(e) => { releasePointerFocus(e); onCancel(); }}>
          {COPY.merge.cancel}
        </button>
      </div>
    </section>
  );
}
