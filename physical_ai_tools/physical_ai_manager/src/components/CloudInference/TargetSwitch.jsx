// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Wo soll dein Modell laufen?" — In der Cloud | Klassen-Jetson (spec §6.3).
// Presentational: the target itself is derived by InferencePage
// (features/cloudInference/gating.js) and the choice lives in its React state
// (no browser storage, D3). A disabled cloud option says why; the whole switch
// is locked while a cloud run is live.

import React from 'react';
import clsx from 'clsx';

import Icon from '../icons/Icon';
import { cloudCopy } from './cloudCopy';

function Option({ active, disabled, icon, label, onClick, title }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      disabled={disabled}
      title={title}
      onClick={onClick}
      className={clsx(
        'inline-flex items-center gap-2 px-3 py-1.5 rounded-[var(--radius-sm)] text-sm font-medium transition',
        active
          ? 'bg-[color:var(--accent)] text-white shadow-pop'
          : 'bg-white text-[var(--ink-2)] border border-[var(--line)] hover:bg-[var(--bg-sunk)]',
        disabled && 'opacity-50 cursor-not-allowed hover:bg-white',
      )}
    >
      <Icon name={icon} size={16} />
      <span>{label}</span>
    </button>
  );
}

/**
 * @param {object} p
 * @param {{offered: boolean, enabled: boolean, reason: ?string}} p.cloud
 * @param {'cloud'|'jetson'} p.target  the target shown now
 * @param {(target: 'cloud'|'jetson') => void} p.onChoose
 * @param {?string} [p.lockedReason]  set while a cloud run is live
 */
export default function TargetSwitch({ cloud, target, onChoose, lockedReason = null }) {
  if (!cloud || !cloud.offered) return null;
  const reason = cloud.enabled ? null : cloudCopy.disabled[cloud.reason] || null;
  const locked = Boolean(lockedReason);
  return (
    <div
      className="shrink-0 flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2 border-b border-[var(--line)]"
      style={{ background: 'var(--bg)' }}
    >
      <span id="inference-target-label" className="text-sm text-[var(--ink-2)]">{cloudCopy.target.label}</span>
      <div role="group" aria-labelledby="inference-target-label" className="flex items-center gap-2">
        <Option
          active={target === 'cloud'}
          disabled={locked || !cloud.enabled}
          icon="cloud"
          label={cloudCopy.target.cloud}
          title={lockedReason || reason || undefined}
          onClick={() => onChoose('cloud')}
        />
        <Option
          active={target === 'jetson'}
          disabled={locked}
          icon="cpu"
          label={cloudCopy.target.jetson}
          title={lockedReason || undefined}
          onClick={() => onChoose('jetson')}
        />
      </div>
      {reason && (
        <span className="text-xs text-[var(--ink-3)]" data-testid="cloud-disabled-reason">{reason}</span>
      )}
    </div>
  );
}
