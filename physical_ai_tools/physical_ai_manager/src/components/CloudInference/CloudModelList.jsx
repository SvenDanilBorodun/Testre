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
      <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">{cloudCopy.models.title}</h3>
      {locked && rows.length > 0 && (
        <div id={lockHintId} role="status" className="text-xs text-[var(--ink-3)]" data-testid="cloud-models-locked">
          {lockedReason}
        </div>
      )}
      {rows.length === 0 && loading && (
        <div className="text-sm text-[var(--ink-3)] flex items-center gap-2">
          <Icon name="loading" size={14} className="animate-spin" />
          {cloudCopy.models.loading}
        </div>
      )}
      {rows.length === 0 && !loading && (
        <div className="text-sm text-[var(--ink-3)]">{cloudCopy.models.empty}</div>
      )}
      <ul className="flex flex-col gap-1.5">
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
                  'w-full text-left px-3 py-2 rounded-[var(--radius-sm)] border transition',
                  selected
                    ? 'border-[color:var(--accent)] bg-[color:var(--accent-soft,rgba(16,185,129,0.08))]'
                    : 'border-[var(--line)] bg-white hover:bg-[var(--bg-sunk)]',
                  off && 'opacity-60 cursor-not-allowed hover:bg-white',
                  inert && 'cursor-progress',
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-medium text-[var(--ink)]">{shortName(job.model_name)}</span>
                  {job.workgroup_id && (
                    <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-[var(--bg-sunk)] text-[var(--ink-2)]">
                      {cloudCopy.models.group}
                    </span>
                  )}
                </div>
                <div className="mt-0.5 text-xs text-[var(--ink-3)] truncate">
                  {[job.dataset_name, formatDate(job.terminated_at)].filter(Boolean).join(' · ')}
                </div>
                {!isAct && <div className="mt-0.5 text-xs text-[var(--ink-3)]">{cloudCopy.models.notAct}</div>}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
