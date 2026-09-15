/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * Sammlung drawer detail of ONE recording: every cloud row sharing its name.
 * The newest row is the take a replay block plays; the older rows are listed
 * as versions that are not played. A missing recording (a replay block names
 * it, the workflow has none) shows its usages only.
 */

import React, { useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import * as workflowApi from '../../../services/workflowApi';
import {
  selectDrawer,
  selectLastPreviewResult,
  setDrawerFocus,
  setPreviewTempo,
  setRenameSplit,
} from '../../../features/workshop/studioAssetsSlice';
import { previewKeyForRecording } from '../../../utils/simPreview';
import { DE, formatDe } from '../blocks/messages_de';
import {
  deleteRecordingRows,
  renameRecording,
  restoreKeepsPlayedTake,
  restoreRecordingRows,
  usageRows,
} from './assetCommands';
import {
  formatRecordedAtDe,
  formatSecondsDe,
  formatVersionDateDe,
  robotLongLabelDe,
} from './format';
import {
  DetailRow,
  InlineConfirm,
  RenameField,
  UsageList,
  deleteUsedText,
  showRenameSplitToast,
  showUndoToast,
} from './drawerParts';

const isFiniteNumber = (v) => typeof v === 'number' && Number.isFinite(v);

function secondsDe(v) {
  return isFiniteNumber(v) ? formatSecondsDe(v) : '—';
}

// The run bar's three tempos; the drawer's choice lives in `drawer.previewTempo`.
const PREVIEW_TEMPO_BUTTONS = [
  { value: 0.5, label: DE.RUN_TEMPO_SLOW },
  { value: 1.0, label: DE.RUN_TEMPO_NORMAL },
  { value: 2.0, label: DE.RUN_TEMPO_FAST },
];

// The newest result among this recording's versions (each version is its own
// preview key), or null.
function latestVersionResult(lastPreviewResult, versions) {
  let best = null;
  versions.forEach((v) => {
    const r = lastPreviewResult ? lastPreviewResult[previewKeyForRecording(v.id)] : null;
    if (r && (!best || (r.ts || 0) >= (best.ts || 0))) best = r;
  });
  return best;
}

function robotLabel(profile) {
  if (!profile) return DE.DRAWER_ROBOT_LEGACY;
  const long = robotLongLabelDe(profile);
  return long ? `${long} (${profile})` : profile;
}

export default function DrawerRecording({
  workspace,
  card,
  items,
  accessToken,
  workflowId,
  capabilities,
  onPreview,
  saveWorkflowNow,
  refetchTrajectories,
}) {
  const dispatch = useDispatch();
  const drawer = useSelector(selectDrawer);
  const lastPreviewResult = useSelector(selectLastPreviewResult);
  const previewTempo = (drawer && drawer.previewTempo) || 1.0;
  // { text, yesLabel, onYes, onNo } — one inline confirm at a time.
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);
  // A rename in flight targets these same rows (and may rename them back), so
  // the delete buttons wait for it.
  const [renaming, setRenaming] = useState(false);

  const name = card.assetName;
  const missing = card.assetKind === 'missingRecording';
  const versions = Array.isArray(card.versions) ? card.versions : [];
  const newest = versions[0] || null;
  const rows = usageRows(workspace, 'recording', name);
  const refetch = () => { if (typeof refetchTrajectories === 'function') refetchTrajectories(); };

  const ask = (text, yesLabel) => new Promise((resolve) => {
    setConfirm({
      text,
      yesLabel,
      onYes: () => { setConfirm(null); resolve(true); },
      onNo: () => { setConfirm(null); resolve(false); },
    });
  });

  const handleRename = async (draft) => {
    setRenaming(true);
    try {
      return await commitRename(draft);
    } finally {
      setRenaming(false);
    }
  };

  const commitRename = async (draft) => {
    const result = await renameRecording({
      workspace,
      api: workflowApi,
      accessToken,
      workflowId,
      fromName: name,
      toName: draft,
      items,
      saveWorkflowNow,
      confirmReplace: (to) => ask(formatDe(DE.CONFIRM_REPLACE_RECORDING, to), DE.CONFIRM_YES_REPLACE),
    });
    setConfirm(null);
    if (result.cancelled) return false;
    const to = String(draft ?? '').trim();
    if (result.ok) {
      if (to !== name) {
        dispatch(setDrawerFocus(to));
        toast.success(formatDe(DE.TOAST_RENAMED, name, to));
        refetch();
      }
      return true;
    }
    if (result.persistent) {
      dispatch(setRenameSplit({ cloudName: to }));
      dispatch(setDrawerFocus(to));
      showRenameSplitToast(result.error);
      refetch();
      return true;
    }
    toast.error(result.error);
    // A validation refusal changed nothing; every other failure may have.
    if (result.error !== DE.ERR_RECORDING_NAME) refetch();
    return false;
  };

  const removeRows = async (targetRows, { clearFocus }) => {
    setBusy(true);
    try {
      const result = await deleteRecordingRows({
        api: workflowApi, accessToken, workflowId, rows: targetRows,
      });
      if (result.deleted.length > 0) {
        if (clearFocus && result.ok) dispatch(setDrawerFocus(null));
        const targetIds = new Set(targetRows.map((r) => r.id));
        const remaining = versions.filter((v) => !targetIds.has(v.id)).concat(result.failed);
        // A restored row is stamped NOW and would become the played take, so
        // „Rückgängig" is offered only when nothing newer of this name is left.
        if (restoreKeepsPlayedTake(result.deleted, remaining)) {
          showUndoToast(formatDe(DE.TOAST_DELETED, name), async () => {
            const restored = await restoreRecordingRows({
              api: workflowApi, accessToken, workflowId, deleted: result.deleted,
            });
            if (!restored.ok) toast.error(restored.error);
            refetch();
          });
        } else {
          toast.success(formatDe(DE.TOAST_DELETED, name));
        }
      }
      if (!result.ok) toast.error(result.error);
      refetch();
    } finally {
      setBusy(false);
    }
  };

  const handleDeleteAll = async () => {
    if (rows.length > 0) {
      const yes = await ask(deleteUsedText(name, rows.length), DE.CONFIRM_YES_DELETE);
      if (!yes) return;
    }
    await removeRows(versions, { clearFocus: true });
  };

  // An older version's delete cannot be undone (restoreKeepsPlayedTake), so it asks first.
  const handleDeleteVersion = async (row) => {
    const yes = await ask(DE.CONFIRM_DELETE_VERSION, DE.CONFIRM_YES_DELETE);
    if (!yes) return;
    await removeRows([row], { clearFocus: false });
  };

  if (missing || !newest) {
    return (
      <div className="p-3">
        <h3 className="truncate text-base font-semibold text-gray-900" title={name}>{name}</h3>
        <UsageList workspace={workspace} rows={rows} nowhereText={DE.DRAWER_USED_NOWHERE_RECORDING} />
      </div>
    );
  }

  const fps = isFiniteNumber(newest.fps) ? newest.fps.toLocaleString('de-DE') : '—';
  const points = isFiniteNumber(newest.point_count) ? newest.point_count : '—';
  const older = versions.slice(1);
  const previewEnabled = !!(capabilities && capabilities.preview && typeof onPreview === 'function');
  const lastResult = latestVersionResult(lastPreviewResult, versions);
  // The drawer plays at its own tempo; the flyout ▶ always plays at 1.0.
  const playVersion = (row) => onPreview(
    { kind: 'recording', id: row.id, name, robotProfile: row.robot_profile ?? null },
    { tempo: previewTempo },
  );

  return (
    <div className="p-3">
      <RenameField name={name} maxLength={40} onCommit={handleRename} disabled={busy} />
      <dl className="mt-2 space-y-1">
        <DetailRow label={DE.DRAWER_DURATION}>
          {formatDe(DE.DRAWER_DURATION_VALUE, secondsDe(newest.duration_s), points, fps)}
        </DetailRow>
        <DetailRow label={DE.DRAWER_ROBOT}>{robotLabel(newest.robot_profile)}</DetailRow>
        <DetailRow label={DE.DRAWER_RECORDED_AT}>{formatRecordedAtDe(newest.created_at)}</DetailRow>
      </dl>
      {previewEnabled && card.canPreview && (
        <section className="mt-3" aria-label={DE.PREVIEW_START}>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">{DE.PREVIEW_START}</h4>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => playVersion(newest)}
              className="rounded border border-[var(--line)] px-2 py-1 text-sm hover:bg-gray-50"
            >
              {DE.PREVIEW_PLAY}
            </button>
            <div className="inline-flex items-center gap-1" role="group" aria-label={DE.RUN_TEMPO_LABEL}>
              {PREVIEW_TEMPO_BUTTONS.map((t) => (
                <button
                  key={t.value}
                  type="button"
                  aria-pressed={previewTempo === t.value}
                  onClick={() => dispatch(setPreviewTempo(t.value))}
                  className={
                    'rounded border px-2 py-0.5 text-xs '
                    + (previewTempo === t.value
                      ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                      : 'border-[var(--line)] text-gray-700 hover:bg-gray-50')
                  }
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>
          {lastResult && lastResult.status === 'refused' && lastResult.message && (
            <p className="mt-1 text-sm text-red-700">{lastResult.message}</p>
          )}
        </section>
      )}
      <UsageList workspace={workspace} rows={rows} nowhereText={DE.DRAWER_USED_NOWHERE_RECORDING} />
      {older.length > 0 && (
        <section className="mt-3">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">{DE.DRAWER_OLDER_VERSIONS}</h4>
          <ul className="mt-1 space-y-1">
            {older.map((row) => (
              <li key={row.id} className="flex items-center gap-2 text-sm">
                <span className="min-w-0 flex-1 text-gray-700">
                  {`${formatVersionDateDe(row.created_at)} · ${secondsDe(row.duration_s)} s · ${DE.DRAWER_VERSION_NOT_PLAYED}`}
                </span>
                {previewEnabled && (
                  <button
                    type="button"
                    aria-label={DE.PREVIEW_START}
                    title={DE.PREVIEW_START}
                    onClick={() => playVersion(row)}
                    className="rounded px-2 py-0.5 text-gray-700 hover:bg-gray-50"
                  >
                    ▶
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy || renaming || !!confirm}
                  onClick={() => handleDeleteVersion(row)}
                  className="rounded px-2 py-0.5 text-red-700 hover:bg-red-50 disabled:opacity-50"
                >
                  {DE.DRAWER_DELETE_VERSION}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
      {confirm && (
        <InlineConfirm text={confirm.text} yesLabel={confirm.yesLabel} onYes={confirm.onYes} onNo={confirm.onNo} />
      )}
      <button
        type="button"
        disabled={busy || renaming || !!confirm}
        onClick={handleDeleteAll}
        className="mt-4 rounded border border-red-300 px-2 py-1 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50"
      >
        {DE.DRAWER_DELETE_RECORDING}
      </button>
    </div>
  );
}
