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
 * The asset document: the ONE object the Sammlung drawer, the Vormachen
 * overlay and TeachHost use to reach „the document" — its Ziele store, which
 * assets the program uses and where, renames that rewrite the program,
 * insertion, and where the drawer sits.
 *
 * Two implementations share one contract (sammlung/__tests__/
 * assetDocument.contract.test.js runs the same assertions against both):
 *
 *   createBlocklyAssetDocument(workspace)      — this file: a thin wrapper
 *     over the functions the drawer and the overlay called directly before,
 *     with IDENTICAL behaviour (the existing drawer, overlay and host tests
 *     run unchanged against it);
 *   createCodeAssetDocument({...})             — code/codeAssetDocument.js,
 *     a Python/Java program.
 *
 * The contract:
 *   kind                         'blockly' | 'code'
 *   language                     '' | 'python' | 'java'
 *   workspace                    the Blockly workspace (null for code)
 *   getStore()                   the document's DestinationStore
 *   subscribe(onChange, {onFlyoutOpened})  → unsubscribe; onChange on every
 *                                change the lists depend on
 *   buildIndex(snapshot)         sammlung/assetIndex.js's view models
 *   usageRows(kind, key)         [{id, label, disabled}] — „Benutzt in"
 *   jump(id)                     show that use
 *   takenPlaceNames()            store names + names the program defines
 *   renamePlace(entryId, name)   store rename + every reference, one step
 *   deletePlace(entryId) / restorePlace(entry, index)
 *   renameRecordingTarget()      what assetCommands.renameRecording rewrites
 *                                through: {workspace} or {rewrite(from, to)}
 *   canEditVariables, renameVariable(id, name), deleteVariable(id)
 *   canInsertSnippets, insertSnippet(asset)
 *   insertProgram(items, opts)   Vormachen's „Als Programm einfügen"
 *   anchorLeft()                 the drawer's left edge in px
 *   closeFlyout(), hideChaff()
 */

import * as Blockly from 'blockly/core';
import { insertProgram } from '../teach/insertProgram';
import {
  deletePlace,
  deleteVariable,
  renamePlace,
  renameVariable,
  usageRows,
} from './assetCommands';
import { jumpToBlock } from './blockUsage';
import { getDestinationStore, takenDestinationNames } from './destinationStore';
import { buildWorkspaceAssetIndex } from './toolboxCategories';

function toolboxWidthOf(workspace) {
  try {
    return Math.max(0, Math.round(workspace?.getToolbox?.()?.getWidth?.() || 0));
  } catch (_) {
    return 0;
  }
}

/** The asset document of a Blockly workspace (see the contract above). */
export function createBlocklyAssetDocument(workspace) {
  const ws = workspace || null;
  return {
    kind: 'blockly',
    language: '',
    workspace: ws,
    getStore: () => getDestinationStore(ws),
    subscribe(onChange, { onFlyoutOpened } = {}) {
      const notify = typeof onChange === 'function' ? onChange : () => {};
      const unsubscribeStore = ws ? getDestinationStore(ws).subscribe(notify) : () => {};
      let listener = null;
      if (ws && typeof ws.addChangeListener === 'function') {
        listener = (e) => {
          if (!e) return;
          // The student opened a toolbox category: its flyout takes the
          // drawer's area. `clearSelection()` fires one with an EMPTY newItem.
          if (e.type === Blockly.Events.TOOLBOX_ITEM_SELECT) {
            if (e.newItem && typeof onFlyoutOpened === 'function') onFlyoutOpened();
            return;
          }
          if (!e.isUiEvent) notify();
        };
        ws.addChangeListener(listener);
      }
      return () => {
        unsubscribeStore();
        if (listener) {
          try {
            ws.removeChangeListener(listener);
          } catch (_) {
            // A workspace disposed before its subscriber.
          }
        }
      };
    },
    buildIndex: (snapshot) => buildWorkspaceAssetIndex(ws, snapshot || undefined),
    usageRows: (kind, key) => usageRows(ws, kind, key).map((row) => ({ ...row, id: row.blockId })),
    jump: (id) => jumpToBlock(ws, id),
    takenPlaceNames: () => takenDestinationNames(ws),
    renamePlace: (entryId, toName) => renamePlace({ workspace: ws, entryId, toName }),
    deletePlace: (entryId) => deletePlace({ workspace: ws, entryId }),
    restorePlace: (entry, index) => getDestinationStore(ws).restore(entry, index),
    renameRecordingTarget: () => ({ workspace: ws }),
    canEditVariables: true,
    renameVariable: (variableId, toName) => renameVariable({ workspace: ws, variableId, toName }),
    deleteVariable: (variableId) => deleteVariable({ workspace: ws, variableId }),
    canInsertSnippets: false,
    insertSnippet: () => ({ count: 0 }),
    insertProgram: (items, opts) => insertProgram(ws, items, opts),
    anchorLeft: () => toolboxWidthOf(ws),
    closeFlyout() {
      try {
        ws?.getToolbox?.()?.clearSelection?.();
      } catch (_) {
        // Headless or read-only workspace without a toolbox.
      }
    },
    hideChaff() {
      try {
        ws?.hideChaff?.();
      } catch (_) {
        // A disposed workspace.
      }
    },
  };
}

/**
 * The document a component works on: its `assetDoc` prop, else — for a caller
 * that still hands over a bare workspace — that workspace's Blockly document.
 */
export function assetDocumentOf(assetDoc, workspace) {
  if (assetDoc) return assetDoc;
  return workspace ? createBlocklyAssetDocument(workspace) : null;
}
