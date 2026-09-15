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
 * Sammlung drawer detail of ONE Ziel (`kind: 'pin'`) or Position
 * (`kind: 'pose'`) of the workflow's destination store. Renaming rewrites the
 * reference blocks in the same undo step; deleting never touches a block.
 */

import React, { useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import {
  selectDrawer,
  selectLastPreviewResult,
  setDrawerFocus,
} from '../../../features/workshop/studioAssetsSlice';
import { placeGripperState } from '../../../utils/armProfile';
import { previewKeyForDestination } from '../../../utils/simPreview';
import { DE, formatDe } from '../blocks/messages_de';
import { deletePlace, renamePlace, usageRows } from './assetCommands';
import { getDestinationStore, sanitizeDestinationNameInput } from './destinationStore';
import { formatMmDe, robotLongLabelDe } from './format';
import {
  DetailRow,
  InlineConfirm,
  RenameField,
  UsageList,
  deleteUsedText,
  showUndoToast,
} from './drawerParts';

function sourceLabel(entry) {
  if (entry.source === 'camera') return DE.CARD_SOURCE_CAMERA;
  if (entry.source === 'sim') return DE.CARD_SOURCE_SIM;
  if (entry.source === 'capture') {
    // A Position is a measured arm pose; a Ziel taught by touch sits on the table.
    return entry.kind === 'pose' ? DE.CARD_SOURCE_CAPTURE : DE.CARD_SOURCE_TOUCH;
  }
  return '—';
}

export default function DrawerPlace({ workspace, card, capabilities, onPreview }) {
  const dispatch = useDispatch();
  const [confirm, setConfirm] = useState(null);
  const entry = card.entry;
  const isPose = entry.kind === 'pose';
  const rows = usageRows(workspace, entry.kind, entry.name);
  const robot = robotLongLabelDe(entry.robot_type);
  const drawer = useSelector(selectDrawer);
  const results = useSelector(selectLastPreviewResult);
  const previewTempo = (drawer && drawer.previewTempo) || 1.0;
  const lastResult = results ? results[previewKeyForDestination(entry.id)] : null;
  // „Greifer merken": a Position's captured gripper, only when its S3 joint
  // snapshot classifies on THIS arm (none from an older server).
  const robotCaps = useSelector((s) => (s.tasks && s.tasks.taskStatus ? s.tasks.taskStatus.capabilities : null));
  const gripper = isPose ? placeGripperState(entry, robotCaps) : null;

  const handleRename = (draft) => {
    const result = renamePlace({ workspace, entryId: entry.id, toName: draft });
    if (!result.ok) {
      toast.error(result.error);
      return false;
    }
    if (result.oldName !== result.entry.name) {
      toast.success(formatDe(DE.TOAST_RENAMED, result.oldName, result.entry.name));
    }
    return true;
  };

  const remove = () => {
    const result = deletePlace({ workspace, entryId: entry.id });
    if (!result.ok) {
      toast.error(result.error);
      return;
    }
    dispatch(setDrawerFocus(null));
    showUndoToast(formatDe(DE.TOAST_DELETED, result.entry.name), () => {
      const restored = getDestinationStore(workspace).restore(result.entry, result.index);
      if (!restored.ok) toast.error(restored.error);
    });
  };

  const handleDelete = () => {
    if (rows.length === 0) {
      remove();
      return;
    }
    setConfirm({
      text: deleteUsedText(entry.name, rows.length),
      onYes: () => { setConfirm(null); remove(); },
      onNo: () => setConfirm(null),
    });
  };

  return (
    <div className="p-3">
      <RenameField
        key={entry.name}
        name={entry.name}
        maxLength={24}
        sanitize={sanitizeDestinationNameInput}
        onCommit={handleRename}
      />
      <dl className="mt-2 space-y-1">
        <DetailRow label={DE.DRAWER_COORDS}>
          {formatDe(DE.DRAWER_COORDS_VALUE, formatMmDe(entry.x), formatMmDe(entry.y), formatMmDe(entry.z))}
        </DetailRow>
        <DetailRow label={DE.DRAWER_SOURCE}>
          {robot ? `${sourceLabel(entry)} · ${robot}` : sourceLabel(entry)}
        </DetailRow>
        {gripper && (
          <DetailRow label={DE.DRAWER_STATE}>
            {gripper === 'closed' ? DE.TEACH_GRIPPER_CLOSED : DE.TEACH_GRIPPER_OPEN}
          </DetailRow>
        )}
      </dl>
      {capabilities && capabilities.preview && typeof onPreview === 'function' && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => onPreview(
              { kind: entry.kind, id: entry.id, name: entry.name },
              { tempo: previewTempo },
            )}
            className="rounded border border-[var(--line)] px-2 py-1 text-sm hover:bg-gray-50"
          >
            {`▶ ${DE.PREVIEW_START}`}
          </button>
          {lastResult && lastResult.status === 'refused' && lastResult.message && (
            <p className="mt-1 text-sm text-red-700">{lastResult.message}</p>
          )}
          {lastResult && lastResult.unreachable && (
            <p className="mt-1 text-sm text-amber-800">
              {lastResult.unreachableMessage
                ? `${DE.CHIP_UNREACHABLE}: ${lastResult.unreachableMessage}`
                : DE.CHIP_UNREACHABLE}
            </p>
          )}
        </div>
      )}
      <UsageList
        workspace={workspace}
        rows={rows}
        nowhereText={isPose ? DE.DRAWER_USED_NOWHERE_POSE : DE.DRAWER_USED_NOWHERE_PLACE}
      />
      {confirm && (
        <InlineConfirm text={confirm.text} yesLabel={DE.CONFIRM_YES_DELETE} onYes={confirm.onYes} onNo={confirm.onNo} />
      )}
      <button
        type="button"
        disabled={!!confirm}
        onClick={handleDelete}
        className="mt-4 rounded border border-red-300 px-2 py-1 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50"
      >
        {isPose ? DE.DRAWER_DELETE_POSE : DE.DRAWER_DELETE_PLACE}
      </button>
    </div>
  );
}
