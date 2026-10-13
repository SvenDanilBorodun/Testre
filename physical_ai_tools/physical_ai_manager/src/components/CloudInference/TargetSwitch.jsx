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

import Icon from '../icons/Icon';
import { releasePointerFocus } from '../sucher/pointerFocus';
import { cloudCopy } from './cloudCopy';

function Option({ active, disabled, icon, label, onClick, title }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      disabled={disabled}
      title={title}
      onClick={(e) => {
        releasePointerFocus(e);
        onClick();
      }}
    >
      <Icon name={icon} size={15} />
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
    <div className="su-target shrink-0 flex flex-wrap items-center gap-x-4 gap-y-2">
      <span id="inference-target-label" className="su-serif su-target__q">{cloudCopy.target.label}</span>
      <div
        role="group"
        aria-labelledby="inference-target-label"
        className="su-seg su-seg--light su-seg--accent su-seg--md su-target__seg"
      >
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
        <span className="text-[13px] text-[var(--ink-2)]" data-testid="cloud-disabled-reason">{reason}</span>
      )}
      <span className="su-lab ml-auto"><span aria-hidden="true">04 · </span>Inferenz</span>
    </div>
  );
}
