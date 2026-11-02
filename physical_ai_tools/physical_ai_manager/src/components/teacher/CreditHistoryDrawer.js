// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Stundenverlauf" (credit system v2, spec §10.4): the training-hour ledger of
// one student, one group or the teacher's own pool, newest first, paged by
// `before` („Ältere laden"). Every row is one ledger movement; a charge also
// says who stopped the training, whether it was waived, and the lost time with
// its reasons [Q-B]. A student's history ends with the refused starts.
//
// The same drawer serves the admin's per-teacher history (scope `teacher_all`)
// through `renderActions`, which adds „Ausgleich gutschreiben" / „Grund
// löschen" to a row; the drawer itself never writes.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import ReactDOM from 'react-dom';
import { Btn } from '../EbUI';
import Icon from '../icons/Icon';
import { hoursDe, lostMinutes } from '../../utils/hoursDe';

export const PAGE_SIZE = 50;
const HISTORY_FAILED_DE = 'Der Stundenverlauf konnte nicht geladen werden.';

export const ACTOR_LABELS = {
  admin: 'Admin',
  teacher: 'Lehrkraft',
  student: 'Schüler/in',
  system: 'System',
};

export const LOSS_LABELS = {
  pod_gone: 'GPU-Server weg',
  modal_ended: 'GPU-Server weg',
  heartbeat_lost: 'Verbindung zum GPU-Server verloren',
  stalled: 'Training hing',
  start_timeout: 'GPU-Server startete nicht',
  placement_guard: 'Ungeeigneter GPU-Server',
  'host_unfit:lerobot_crash': 'Training abgestürzt',
  'host_unfit:resume_crash': 'Fortsetzen abgestürzt',
  checkpoint_overdue: 'Keine Zwischenstände gespeichert',
  'host_unfit:lerobot_error_unconfirmed': 'Fehler gemeldet, noch nicht bestätigt',
  'host_unfit:content_unconfirmed': 'Zwischenstand abgelehnt, noch nicht bestätigt',
  'host_unfit:time_cap_early': 'Unbestätigtes Zeitlimit',
  'host_unfit:wrong_image': 'Falsche Software auf dem GPU-Server',
};

/** A loss reason's German label; any other `host_unfit:*` (or an unknown one) is „GPU-Server unbrauchbar". */
export function lossLabel(reason) {
  return LOSS_LABELS[reason] || 'GPU-Server unbrauchbar';
}

export const REFUSAL_LABELS = {
  role: () => 'Keine Schülerin/kein Schüler',
  policy: (modelType) => (modelType ? `Modelltyp nicht erlaubt (${modelType})` : 'Modelltyp nicht erlaubt'),
  no_hours: () => 'Keine freien Stunden',
  member_limit: () => 'Gruppenlimit erreicht',
};

export function refusalLabel(row) {
  const fn = REFUSAL_LABELS[row?.code];
  return fn ? fn(row.model_type) : String(row?.code ?? '—');
}

const actorOf = (e) => e?.actor ?? e?.actor_role ?? null;
const limitText = (v) => (typeof v === 'number' ? hoursDe(v) : 'kein Limit');

/** The „Art" label of one ledger row (spec §10.4). */
export function kindLabel(e) {
  switch (e?.kind) {
    case 'grant':
      return 'Vom Admin vergeben';
    case 'revoke':
      return actorOf(e) === 'system'
        ? 'Entzogen – das Konto der Lehrkraft wurde gelöscht'
        : 'Vom Admin entzogen';
    case 'assign':
      return 'Zugewiesen';
    case 'reclaim':
      return 'Zurückgeholt';
    case 'reserve':
      return 'Reserviert (Trainingsstart)';
    case 'charge':
      return e.stopped_by
        ? `Berechnet (gestoppt von ${e.stopped_by === 'teacher' ? 'Lehrkraft' : 'Schüler/in'})`
        : 'Berechnet';
    case 'release':
      return 'Zurück ins Guthaben';
    case 'migrate':
      return 'Umstellung auf Stunden';
    case 'compensate':
      return 'Ausgleich vom Admin';
    case 'limit_change':
      return `Gruppenlimit geändert: ${limitText(e.limit_old)} → ${limitText(e.limit_new)}`;
    default:
      return String(e?.kind ?? '—');
  }
}

/** The lines under a row's label: its reason, and a charge's waiver and lost time. */
export function detailLines(e) {
  const lines = [];
  if (e?.reason_erased) lines.push('Grund: (gelöscht)');
  else if (typeof e?.reason === 'string' && e.reason) lines.push(`Grund: ${e.reason}`);
  if (e?.kind === 'charge') {
    if (e.waiver) lines.push('Nicht berechnet: Fehler vor dem ersten Trainingsschritt');
    if (typeof e.lost_seconds === 'number' && e.lost_seconds >= 60) {
      lines.push(`Verlorene Zeit: ${lostMinutes(e.lost_seconds)} Min. (nicht berechnet)`);
      const losses = Array.isArray(e.losses) ? e.losses : [];
      if (losses.length) lines.push(`Gründe: ${losses.map(lossLabel).join(', ')}`);
    }
  }
  return lines;
}

/** The „Stunden" cell: a non-movement record (switch-over, limit) has no hours. */
export function hoursCell(e) {
  return e?.kind === 'migrate' || e?.kind === 'limit_change' ? '—' : hoursDe(e?.hours);
}

function trainingCell(e) {
  if (e?.model_name) return e.model_name;
  return e?.training_id !== null && e?.training_id !== undefined ? `#${e.training_id}` : '—';
}

function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('de-DE', {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

/** `[] | {entries, refusals}` → `{entries, refusals}`. */
export function normalizeHistory(res) {
  if (Array.isArray(res)) return { entries: res, refusals: [] };
  return {
    entries: Array.isArray(res?.entries) ? res.entries : [],
    refusals: Array.isArray(res?.refusals) ? res.refusals : [],
  };
}

/**
 * @param {string}   title          „Stundenverlauf · <name>" / „Stundenverlauf · Lehrer-Pool"
 * @param {function} load           ({before, limit}) → Promise of the API answer
 * @param {boolean}  showRefusals   the student scope: list the refused starts
 * @param {function} renderActions  (entry, patchEntry) → node, the admin's row actions
 */
export default function CreditHistoryDrawer({ title, load, showRefusals = false, renderActions, onClose }) {
  const [entries, setEntries] = useState([]);
  const [refusals, setRefusals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    loadRef.current({ before: null, limit: PAGE_SIZE })
      .then((res) => {
        if (!alive) return;
        const page = normalizeHistory(res);
        setEntries(page.entries);
        setRefusals(page.refusals);
        setHasMore(page.entries.length >= PAGE_SIZE);
      })
      .catch((err) => { if (alive) setError((err && err.message) || HISTORY_FAILED_DE); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  const loadMore = async () => {
    if (loadingMore || entries.length === 0) return;
    const before = entries.reduce((m, e) => (typeof e.id === 'number' && e.id < m ? e.id : m), Infinity);
    if (!Number.isFinite(before)) return;
    setLoadingMore(true);
    try {
      const page = normalizeHistory(await loadRef.current({ before, limit: PAGE_SIZE }));
      setEntries((prev) => [...prev, ...page.entries.filter((e) => !prev.some((p) => p.id === e.id))]);
      setHasMore(page.entries.length >= PAGE_SIZE);
    } catch (err) {
      setError((err && err.message) || HISTORY_FAILED_DE);
    } finally {
      setLoadingMore(false);
    }
  };

  const patchEntry = useCallback((id, patch) => {
    setEntries((prev) => prev.map((e) => (e.id === id ? { ...e, ...patch } : e)));
  }, []);

  // Rendered into <body>: the drawer opens from a table row, where a <div>
  // is not valid content.
  return ReactDOM.createPortal(
    <div className="fixed inset-0 z-40 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0 bg-black/30 backdrop-blur-sm" />
      <aside
        role="dialog"
        aria-label={title}
        className="relative w-[720px] max-w-full h-full bg-white border-l border-[var(--line)] shadow-pop flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-5 border-b border-[var(--line)] flex items-center justify-between gap-4">
          <h3 className="font-semibold text-[var(--ink)] truncate">{title}</h3>
          <button
            type="button"
            className="w-9 h-9 rounded-[var(--radius-sm)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] hover:text-[var(--ink)] flex items-center justify-center transition shrink-0"
            onClick={onClose}
            aria-label="Schließen"
          >
            <Icon name="close" size={20} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
          {loading ? (
            <div className="text-[var(--ink-3)] text-sm">Laden…</div>
          ) : (
            <>
              {error && (
                <div role="alert" className="text-sm text-[color:var(--danger)] bg-[var(--danger-wash)] rounded-[var(--radius-sm)] px-3 py-2">
                  {error}
                </div>
              )}
              {entries.length === 0 && !error ? (
                <div className="text-[var(--ink-3)] text-sm">Noch keine Einträge.</div>
              ) : entries.length > 0 ? (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-[10px] uppercase font-mono tracking-wider text-[var(--ink-3)] border-b border-[var(--line)]">
                        <th className="py-2 pr-3">Datum</th>
                        <th className="py-2 pr-3">Art</th>
                        <th className="py-2 pr-3">Stunden</th>
                        <th className="py-2 pr-3">Training</th>
                        <th className="py-2 pr-3">Von</th>
                      </tr>
                    </thead>
                    <tbody>
                      {entries.map((e) => (
                        <tr key={e.id} className="border-b border-[var(--line)] last:border-b-0 align-top" data-testid={`credit-row-${e.id}`}>
                          <td className="py-2 pr-3 font-mono text-xs text-[var(--ink-3)] whitespace-nowrap">{formatDate(e.at)}</td>
                          <td className="py-2 pr-3 text-[var(--ink)]">
                            <div>{kindLabel(e)}</div>
                            {detailLines(e).map((line) => (
                              <div key={line} className="text-[11px] text-[var(--ink-3)] mt-0.5">{line}</div>
                            ))}
                            {renderActions ? renderActions(e, (patch) => patchEntry(e.id, patch)) : null}
                          </td>
                          <td className="py-2 pr-3 font-mono text-xs text-[var(--ink)] whitespace-nowrap">{hoursCell(e)}</td>
                          <td className="py-2 pr-3 font-mono text-[11px] text-[var(--ink-2)] truncate max-w-[22ch]" title={trainingCell(e)}>
                            {trainingCell(e)}
                          </td>
                          <td className="py-2 pr-3 text-xs text-[var(--ink-2)] whitespace-nowrap">
                            {ACTOR_LABELS[actorOf(e)] || '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
              {hasMore && (
                <Btn variant="secondary" size="sm" onClick={loadMore} disabled={loadingMore}>
                  <Icon name="history" /> Ältere laden
                </Btn>
              )}
              {showRefusals && (
                <section aria-label="Abgelehnte Starts" className="pt-4 border-t border-[var(--line)]">
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)] mb-2">
                    Abgelehnte Starts
                  </h4>
                  {refusals.length === 0 ? (
                    <div className="text-[var(--ink-3)] text-sm">Noch keine Einträge.</div>
                  ) : (
                    <ul className="space-y-1 text-sm">
                      {refusals.map((r, i) => (
                        <li key={r.id ?? `${r.at}-${i}`} className="flex gap-3">
                          <span className="font-mono text-xs text-[var(--ink-3)] whitespace-nowrap">{formatDate(r.at)}</span>
                          <span className="text-[var(--ink)]">{refusalLabel(r)}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              )}
            </>
          )}
        </div>
      </aside>
    </div>,
    document.body,
  );
}
