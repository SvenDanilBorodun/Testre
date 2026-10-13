// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The student's finished models for a cloud run (spec §6.4). Presentational:
// the rows come from useSupabaseTrainings (own + group, the ladder the API
// checks). Only an explicit click reserves (the panel debounces it); the list
// never pre-selects anything (F-9). While the cloud GPU starts (a reserve and
// its automatic retries, owner round 2) the list is LOCKED: every other row is
// disabled with the reason, and the chosen row keeps its look but a click on it
// sends nothing.

import React, { useId } from 'react';
import clsx from 'clsx';

import Icon from '../icons/Icon';
import { Led } from '../sucher';
import { cloudCopy } from './cloudCopy';

const ARCHIVE_PREFIX = 'EduBotics-Solutions/';

/** Succeeded trainings, newest finished first. Pure. */
export function cloudModelRows(jobs) {
  return (Array.isArray(jobs) ? jobs : [])
    .filter((j) => j && j.status === 'succeeded')
    .slice()
    .sort((a, b) => new Date(b.terminated_at || 0) - new Date(a.terminated_at || 0));
}

function shortName(modelName) {
  const name = typeof modelName === 'string' ? modelName : '';
  return name.startsWith(ARCHIVE_PREFIX) ? name.slice(ARCHIVE_PREFIX.length) : name;
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

/**
 * @param {object} p
 * @param {object[]} p.jobs
 * @param {boolean} p.loading
 * @param {?number} p.selectedId
 * @param {boolean} p.disabled   a Start or a run is in progress, or a task runs (G-3)
 * @param {?string} [p.lockedReason]  set while the cloud GPU starts: the list is locked
 * @param {(job: object) => void} p.onSelect
 */
export default function CloudModelList({ jobs, loading, selectedId, disabled, lockedReason = null, onSelect }) {
  const rows = cloudModelRows(jobs);
  const locked = Boolean(lockedReason);
  const lockHintId = useId();
  return (
    <section aria-label={cloudCopy.models.title} className="flex flex-col gap-2">
      <h3 className="su-lab">{cloudCopy.models.title}</h3>
      {locked && rows.length > 0 && (
        <div id={lockHintId} role="status" className="text-[13px] text-[var(--ink-2)]" data-testid="cloud-models-locked">
          {lockedReason}
        </div>
      )}
      {rows.length === 0 && loading && (
        <div className="text-sm text-[var(--ink-2)] flex items-center gap-2">
          <Icon name="loading" size={14} className="animate-spin" />
          {cloudCopy.models.loading}
        </div>
      )}
      {rows.length === 0 && !loading && (
        <div className="text-sm text-[var(--ink-2)]">{cloudCopy.models.empty}</div>
      )}
      <ul className="flex flex-col gap-2">
        {rows.map((job) => {
          const isAct = job.model_type === 'act';
          const selected = job.id === selectedId;
          const lockedOut = locked && !selected; // disabled, with the reason
          const inert = locked && selected; // looks chosen; a click sends nothing
          const off = disabled || !isAct || lockedOut;
          return (
            <li key={job.id}>
              <button
                type="button"
                aria-pressed={selected}
                aria-disabled={inert ? 'true' : undefined}
                aria-describedby={locked && rows.length > 0 ? lockHintId : undefined}
                disabled={off}
                title={lockedOut && isAct ? lockedReason : undefined}
                onClick={() => { if (!locked) onSelect(job); }}
                className={clsx(
                  'su-cmodel',
                  selected && 'su-cmodel--on',
                  off && 'su-cmodel--off',
                  inert && 'cursor-progress',
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-2.5 min-w-0">
                    <Led tone={selected ? 'sig' : 'off'} />
                    <span className="truncate font-bold text-[var(--ink)]">{shortName(job.model_name)}</span>
                  </span>
                  {job.workgroup_id && (
                    <span className="shrink-0 text-[11px] px-2 py-0.5 rounded-full bg-[var(--su-well)] text-[var(--ink-2)]">
                      {cloudCopy.models.group}
                    </span>
                  )}
                </div>
                <div className="su-cmodel__meta truncate">
                  {[job.dataset_name, formatDate(job.terminated_at)].filter(Boolean).join(' · ')}
                </div>
                {!isAct && <div className="su-cmodel__meta">{cloudCopy.models.notAct}</div>}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
