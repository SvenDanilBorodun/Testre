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
import { useDispatch } from 'react-redux';
import toast from 'react-hot-toast';
import * as workflowApi from '../../../services/workflowApi';
import { setDrawerFocus, setRenameSplit } from '../../../features/workshop/studioAssetsSlice';
import { DE, formatDe } from '../blocks/messages_de';
import {
  deleteRecordingRows,
  renameRecording,
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
  // { text, yesLabel, onYes, onNo } — one inline confirm at a time.
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);

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
        showUndoToast(formatDe(DE.TOAST_DELETED, name), async () => {
          const restored = await restoreRecordingRows({
            api: workflowApi, accessToken, workflowId, deleted: result.deleted,
          });
          if (!restored.ok) toast.error(restored.error);
          refetch();
        });
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
      {capabilities && capabilities.preview && card.canPreview && typeof onPreview === 'function' && (
        <button
          type="button"
          onClick={() => onPreview({
            kind: 'recording', id: newest.id, name, robotProfile: newest.robot_profile ?? null,
          })}
          className="mt-2 rounded border border-[var(--line)] px-2 py-1 text-sm hover:bg-gray-50"
        >
          {`▶ ${DE.PREVIEW_START}`}
        </button>
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
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => removeRows([row], { clearFocus: false })}
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
        disabled={busy || !!confirm}
        onClick={handleDeleteAll}
        className="mt-4 rounded border border-red-300 px-2 py-1 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50"
      >
        {DE.DRAWER_DELETE_RECORDING}
      </button>
    </div>
  );
}
