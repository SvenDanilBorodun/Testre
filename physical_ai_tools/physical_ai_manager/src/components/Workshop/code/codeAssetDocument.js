/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The asset document of a Python/Java program — the second implementation of
// the contract in sammlung/assetDocument.js, run against the same assertions
// (sammlung/__tests__/assetDocument.contract.test.js).
//
// It owns nothing: the page (WorkshopPage) owns the files, the ONE detached
// Ziele store of the open code document, the cursor memory and the reveal
// request, and hands them in as functions — so an edit made here (a rename, an
// insertion) goes through the page's `applyCodeFiles`, which updates the ref a
// save reads SYNCHRONOUSLY before React re-renders.
//
// What differs from a Blockly document, deliberately:
//   * uses are CODE LINES — ids `<file>:L<line>` (codeBreakpoints.js), a call in
//     a comment counts as switched off (`disabled`), like a disabled block;
//   * a rename rewrites the exact string arguments of the reference calls in
//     every file (owner decision O5), never a pin()/pin_current() definition;
//   * variables are listed (declarations, the run's values, zeige names) but
//     never renamed or deleted — there is no language server (decision D7);
//   * the drawer sits beside the file sidebar, and rows can be inserted into
//     the program („Einfügen") directly below the line the student's cursor
//     is on, checked by codeInsert (owner decision R3-O4): without a cursor
//     nothing is written, and the answer carries the lines so Vormachen can
//     put them on the clipboard instead.

import { buildAssetIndex } from '../sammlung/assetIndex';
import { buildProgramSteps } from '../teach/insertProgram';
import { isDisplayableVariableName } from '../../../utils/variableName';
import { codeBreakpointId, parseCodeBreakpointId } from './codeBreakpoints';
import {
  codeDefinedPlaceNames,
  codeUsageMaps,
  collectCodeVariables,
  renameCodeAssetRefs,
  scanCodeAssets,
  variableOccurrences,
  variableOccurrencesAll,
} from './codeAssetUsage';
import { CODE_DE } from './codeMessagesDe';

// codeInsert.js (the insertion's structure analysis, with a Java statement
// parser) is not in the entry bundle (review round 2, ni4): it is loaded the
// first time a code document is created and awaited by an insertion. A load
// that failed (offline, a chunk replaced by a deploy) is not cached, so the
// next insertion asks again.
let codeInsertModule = null;
/** The insertion module, loaded once. */
export function loadCodeInsert() {
  if (!codeInsertModule) {
    codeInsertModule = import('./codeInsert').catch((err) => {
      codeInsertModule = null;
      throw err;
    });
  }
  return codeInsertModule;
}

// CodeWorkspace's file sidebar (`w-44`, 11rem at the 16 px root): the drawer
// opens right beside it, over the editor.
export const CODE_SIDEBAR_WIDTH_PX = 176;
// = assetCommands.USAGE_LABEL_MAX_CHARS, the Blockly rows' cap.
const USAGE_LABEL_MAX_CHARS = 60;

function lineText(files, file, line) {
  const content = files && typeof files[file] === 'string' ? files[file] : '';
  return (content.split('\n')[line - 1] || '').trim();
}

function usageLabel(files, file, line) {
  const text = `${file}:${line} · ${lineText(files, file, line)}`;
  return text.length > USAGE_LABEL_MAX_CHARS ? `${text.slice(0, USAGE_LABEL_MAX_CHARS)}…` : text;
}

// Project order (file order of the object), then line.
function sortRows(files, rows) {
  const order = new Map(Object.keys(files || {}).map((f, i) => [f, i]));
  return rows.slice().sort((a, b) => ((order.get(a.file) ?? 0) - (order.get(b.file) ?? 0)) || (a.line - b.line));
}

// One row per line: two uses on one line are one place to jump to.
function toUsageRows(files, rows) {
  const seen = new Set();
  const out = [];
  for (const r of sortRows(files, rows)) {
    const id = codeBreakpointId(r.file, r.line);
    if (seen.has(id)) continue;
    seen.add(id);
    out.push({ id, label: usageLabel(files, r.file, r.line), disabled: !!r.inComment });
  }
  return out;
}

/**
 * @param {object} args
 * @param {'python'|'java'} args.language
 * @param {object} args.store - the code document's DestinationStore (created
 *   ONCE per document by the page: `createDetachedDestinationStore`).
 * @param {() => object} args.getFiles - the current `{path: content}`.
 * @param {(files: object) => void} args.applyFiles - the page's applyCodeFiles.
 * @param {({file, line}) => void} args.requestReveal - scroll the editor there.
 * @param {() => ({file, line}|null)} args.getCursor - the last cursor, or null
 *   when the student never clicked into the editor.
 * @param {({file, line}) => void} [args.setCursor] - where the next insertion
 *   goes after this one (below what was just inserted).
 * @param {() => any} [args.getDocumentToken] - the open document's identity:
 *   it changes the moment the page starts replacing the document. An
 *   insertion waiting for its module checks it before writing (review round
 *   3, mb9).
 */
export function createCodeAssetDocument({
  language, store, getFiles, applyFiles, requestReveal, getCursor, setCursor, getDocumentToken,
}) {
  // Loaded now, so the first „Einfügen" waits for nothing.
  loadCodeInsert().catch(() => {});
  const files = () => {
    const f = typeof getFiles === 'function' ? getFiles() : null;
    return f && typeof f === 'object' && !Array.isArray(f) ? f : {};
  };
  const scan = () => scanCodeAssets(files(), language);

  const variableList = (snapshot, s) => {
    const project = files();
    const zeigeRows = s.variables;
    const names = [];
    const seen = new Set();
    const push = (n) => {
      if (typeof n !== 'string' || seen.has(n) || !isDisplayableVariableName(n)) return;
      if (n === '__proto__' || n === 'constructor' || n === 'prototype') return;
      seen.add(n);
      names.push(n);
    };
    collectCodeVariables(project, language).forEach((v) => push(v.name));
    for (const n of zeigeRows.keys()) push(n);
    const values = snapshot && snapshot.variableValues && typeof snapshot.variableValues === 'object'
      ? snapshot.variableValues : {};
    Object.keys(values).forEach(push);
    // ONE scan for every name (review m8), not one per variable.
    const uses = variableOccurrencesAll(project, language, names);
    return names.map((name) => ({
      id: name,
      name,
      uses: (uses.get(name) || []).length + (zeigeRows.get(name) || []).length,
    }));
  };

  // Async: the insertion module may still be loading. The files and the
  // cursor are read AFTER it arrived, so the lines go into the latest text —
  // of the SAME document: one the student opened meanwhile gets nothing
  // (review round 3, mb9: the old document's language, the new one's files).
  const documentToken = () => (typeof getDocumentToken === 'function' ? getDocumentToken() : null);
  const insertLines = async (lineOf) => {
    const token = documentToken();
    const { insertAtTarget, insertionTarget, ...spell } = await loadCodeInsert();
    if (documentToken() !== token) return { count: 0, error: CODE_DE.INSERT_DOCUMENT_CHANGED };
    const lines = lineOf(spell);
    if (!Array.isArray(lines) || lines.length === 0) return { count: 0 };
    const project = files();
    const cursor = typeof getCursor === 'function' ? getCursor() : null;
    const target = insertionTarget(project, language, cursor);
    // The student chooses the spot (R3-O4): no cursor, or one where a line
    // cannot stand or never runs — nothing is written, the hint says why.
    // Without a cursor the lines ride along, for the clipboard.
    if (target.notFound) {
      return target.noCursor
        ? {
          count: 0, error: target.hint, noCursor: true, lines,
        }
        : { count: 0, error: target.hint };
    }
    const content = typeof project[target.file] === 'string' ? project[target.file] : '';
    const res = insertAtTarget(content, target, lines, language);
    applyFiles({ ...project, [target.file]: res.content });
    if (typeof setCursor === 'function') setCursor({ file: target.file, line: res.lastLine });
    if (typeof requestReveal === 'function') requestReveal({ file: target.file, line: res.lastLine });
    return {
      count: lines.length, file: target.file, firstLine: res.firstLine, lastLine: res.lastLine,
    };
  };

  return {
    kind: 'code',
    language,
    workspace: null,
    getStore: () => store,
    subscribe(onChange) {
      if (!store || typeof store.subscribe !== 'function') return () => {};
      return store.subscribe(typeof onChange === 'function' ? onChange : () => {});
    },
    buildIndex(snapshot) {
      const snap = snapshot && typeof snapshot === 'object' ? snapshot : {};
      const s = scan();
      const variables = variableList(snap, s);
      return buildAssetIndex({
        usage: codeUsageMaps(s, variables),
        variables,
        destinations: store ? store.getEntries() : [],
        trajectories: snap.trajectories,
        robotType: snap.robotType,
        lastPreviewResult: snap.lastPreviewResult,
        variableValues: snap.variableValues,
        capabilities: snap.capabilities,
        now: Date.now(),
      });
    },
    usageRows(kind, key) {
      if (!key) return [];
      const project = files();
      const s = scan();
      if (kind === 'recording') return toUsageRows(project, s.replay.get(String(key).trim()) || []);
      if (kind === 'pin' || kind === 'pose' || kind === 'ref') {
        return toUsageRows(project, s.refs.get(String(key).trim()) || []);
      }
      if (kind === 'variable') {
        return toUsageRows(project, [
          ...variableOccurrences(project, language, key).map((o) => ({ ...o, inComment: false })),
          ...(s.variables.get(key) || []),
        ]);
      }
      return [];
    },
    jump(id) {
      const at = parseCodeBreakpointId(id);
      if (at && typeof requestReveal === 'function') requestReveal({ file: at.path, line: at.line });
    },
    takenPlaceNames() {
      const out = [];
      const seen = new Set();
      const push = (n) => {
        if (n && !seen.has(n)) {
          seen.add(n);
          out.push(n);
        }
      };
      (store ? store.getEntries() : []).forEach((e) => push(e.name));
      // A commented-out pin keeps its name reserved (a disabled block's does).
      codeDefinedPlaceNames(files(), language, { includeComments: true }).forEach(push);
      return out;
    },
    renamePlace(entryId, toName) {
      const result = store.rename(entryId, toName);
      if (result.ok && result.oldName !== result.entry.name) {
        const { files: next, count } = renameCodeAssetRefs(files(), language, 'place', result.oldName, result.entry.name);
        if (count > 0) applyFiles(next);
      }
      return result;
    },
    deletePlace: (entryId) => store.remove(entryId),
    restorePlace: (entry, index) => store.restore(entry, index),
    renameRecordingTarget: () => ({
      rewrite: (from, to) => {
        const { files: next, count } = renameCodeAssetRefs(files(), language, 'recording', from, to);
        if (count > 0) applyFiles(next);
        return count;
      },
    }),
    canEditVariables: false,
    renameVariable: () => ({ ok: false }),
    deleteVariable: () => ({ ok: false }),
    canInsertSnippets: true,
    insertSnippet: (asset) => insertLines(({ snippetLines }) => snippetLines(asset, language)),
    insertProgram: (items, opts) => insertLines(
      ({ stepsToCode }) => stepsToCode(buildProgramSteps(items, opts), language),
    ),
    anchorLeft: () => CODE_SIDEBAR_WIDTH_PX,
    closeFlyout: () => {},
    hideChaff: () => {},
  };
}
