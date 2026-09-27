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
 * The Sammlung drawer: manage the workflow's Variablen, Aufnahmen, Ziele and
 * Positionen — rename, delete (with „Rückgängig"), where each is used, and a
 * recording's older versions.
 *
 * It lives in the EDITOR box, never in the dock (the dock disappears in the
 * simulator), absolutely positioned beside the toolbox where a flyout would
 * open — flyout and drawer are the same area, so opening a category closes
 * the drawer. It never changes the editor box's size: no svgResize, no
 * remount. z-20 is above the editor only because the BlocklyWorkspace host
 * isolates Blockly's stacking context (the toolbox is z-index 70).
 *
 * ONE DRAWER FOR BOTH NOTATIONS. It reaches the document only through an
 * asset document (`assetDoc`, sammlung/assetDocument.js): a Blockly workspace
 * or a Python/Java program. A caller that hands a bare `workspace` gets that
 * workspace's Blockly document. Over a CODE program the drawer opens beside
 * the file sidebar, rows can be put into the program — „Einfügen", or dragged
 * into the editor as a `SNIPPET_MIME` snippet — and, since a code program has
 * no flyout cards, the drawer itself offers „Neu" through the page's own
 * provider actions (Vormachen, a camera click, the simulator table).
 */

import React, { useEffect, useMemo, useReducer } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import {
  closeDrawer,
  selectDrawer,
  selectRenameSplit,
  setDrawerFocus,
  setDrawerTab,
} from '../../../features/workshop/studioAssetsSlice';
import { DE, formatDe } from '../blocks/messages_de';
import { CODE_DE, formatCode } from '../code/codeMessagesDe';
import { SNIPPET_MIME } from '../code/snippetMime';
import { newActionsFor } from './newActions';
import { EMPTY_SAMMLUNG_PROVIDER } from './provider';
import { assetDocumentOf } from './assetDocument';
import DrawerRecording from './DrawerRecording';
import DrawerPlace from './DrawerPlace';
import DrawerVariable from './DrawerVariable';

export const DRAWER_TABS = Object.freeze([
  { id: 'variablen', label: DE.CATEGORY_VARIABLEN },
  { id: 'aufnahmen', label: DE.CATEGORY_AUFNAHMEN },
  { id: 'ziele', label: DE.CATEGORY_ZIELE },
  { id: 'positionen', label: DE.CATEGORY_POSITIONEN },
]);

/** The drag-and-drop type of a Sammlung row dropped into the code editor. */
export { SNIPPET_MIME };

// The row kinds a code program can take as a line of code.
const SNIPPET_KINDS = new Set(['recording', 'pin', 'pose']);

const CHIP_CLASSES = {
  ok: 'bg-green-100 text-green-800',
  warn: 'bg-amber-100 text-amber-800',
  bad: 'bg-red-100 text-red-800',
};

// The drawer's focus id of a list row: recordings by NAME (all versions are
// one recording), everything else by id.
function focusIdOf(card) {
  return card.assetKind === 'recording' || card.assetKind === 'missingRecording'
    ? card.assetName : card.assetId;
}

function cardsForTab(index, tab) {
  if (tab === 'variablen') return index.variables;
  if (tab === 'aufnahmen') return [...index.recordings, ...index.missingRecordings];
  if (tab === 'ziele') return index.pins;
  if (tab === 'positionen') return index.poses;
  return [];
}

function isTextInput(target) {
  if (!target || !target.tagName) return false;
  const tag = target.tagName.toUpperCase();
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

export default function SammlungDrawer({
  assetDoc: assetDocProp = null,
  workspace = null,
  provider = EMPTY_SAMMLUNG_PROVIDER,
  accessToken,
  workflowId,
  robotType,
  onPreview,
  saveWorkflowNow,
  refetchTrajectories,
}) {
  const dispatch = useDispatch();
  const drawer = useSelector(selectDrawer);
  const renameSplit = useSelector(selectRenameSplit);
  const [, rerender] = useReducer((n) => n + 1, 0);
  const assetDoc = useMemo(() => assetDocumentOf(assetDocProp, workspace), [assetDocProp, workspace]);

  // Re-render on every change the lists depend on: the destination store, the
  // provider snapshot and the program itself (usages, variables). A selected
  // toolbox category (Blockly) takes this area: the drawer closes.
  useEffect(() => {
    const unsubscribeDoc = assetDoc
      ? assetDoc.subscribe(rerender, { onFlyoutOpened: () => dispatch(closeDrawer()) })
      : () => {};
    const unsubscribeProvider = provider && typeof provider.subscribe === 'function'
      ? provider.subscribe(rerender) : () => {};
    return () => {
      unsubscribeDoc();
      unsubscribeProvider();
    };
  }, [assetDoc, provider, dispatch]);

  // Opening the drawer closes an open flyout (same area).
  useEffect(() => {
    if (assetDoc) assetDoc.closeFlyout();
  }, [assetDoc]);

  let snapshot = null;
  try {
    snapshot = provider && typeof provider.getSnapshot === 'function' ? provider.getSnapshot() : null;
  } catch (_) {
    snapshot = null;
  }
  const index = assetDoc ? assetDoc.buildIndex(snapshot || undefined) : null;
  const capabilities = (snapshot && snapshot.capabilities) || {};
  const items = (snapshot && snapshot.trajectories && Array.isArray(snapshot.trajectories.items))
    ? snapshot.trajectories.items : [];
  const tab = DRAWER_TABS.some((t) => t.id === drawer.tab) ? drawer.tab : 'aufnahmen';
  const cards = index ? cardsForTab(index, tab) : [];
  const focused = cards.find((c) => focusIdOf(c) === drawer.focusId) || null;
  const anchorLeft = assetDoc ? assetDoc.anchorLeft() : 0;
  const isCode = !!assetDoc && assetDoc.kind === 'code';
  const canInsert = !!assetDoc && assetDoc.canInsertSnippets === true;
  const newActions = isCode ? newActionsFor(capabilities, tab) : [];
  const snippetable = (card) => canInsert && SNIPPET_KINDS.has(card.assetKind);

  // A code document's insertion may load its module first (async); a
  // Blockly document answers at once — `await` takes both.
  const insert = async (card) => {
    let result;
    try {
      result = await assetDoc.insertSnippet({ kind: card.assetKind, name: card.assetName });
    } catch (err) {
      // The insertion module loads on demand (review round 2, ni4); a failed
      // load wrote nothing.
      console.error('insertSnippet failed:', err);
      toast.error(CODE_DE.SAMMLUNG_INSERT_FAILED);
      return;
    }
    if (result && result.error) {
      toast.error(result.error);
      return;
    }
    if (result && result.count > 0) {
      toast.success(formatCode(CODE_DE.INSERTED_AT, result.file, result.firstLine));
    }
  };

  const handleKeyDown = (e) => {
    if (e.key !== 'Escape' || isTextInput(e.target)) return;
    e.stopPropagation();
    dispatch(closeDrawer());
  };

  const renderDetail = () => {
    if (!focused) return null;
    const key = `${tab}:${drawer.focusId}`;
    const common = {
      assetDoc, workspace, card: focused, capabilities, onPreview,
    };
    if (tab === 'variablen') return <DrawerVariable key={key} {...common} />;
    if (tab === 'aufnahmen') {
      return (
        <DrawerRecording
          key={key}
          {...common}
          items={items}
          accessToken={accessToken}
          workflowId={workflowId}
          robotType={robotType}
          saveWorkflowNow={saveWorkflowNow}
          refetchTrajectories={refetchTrajectories}
        />
      );
    }
    return <DrawerPlace key={key} {...common} />;
  };

  return (
    <aside
      role="dialog"
      aria-label={DE.SAMMLUNG_TITLE}
      onKeyDown={handleKeyDown}
      className="absolute inset-y-0 z-20 flex flex-col bg-white border-r border-[var(--line)] shadow-xl"
      style={{ left: anchorLeft, width: `min(22rem, calc(100% - ${anchorLeft}px))` }}
    >
      <header className="flex items-center justify-between border-b border-[var(--line)] px-3 py-2">
        <h2 className="text-base font-semibold text-gray-900">{DE.SAMMLUNG_TITLE}</h2>
        <button
          type="button"
          aria-label={DE.DRAWER_CLOSE}
          title={DE.DRAWER_CLOSE}
          onClick={() => dispatch(closeDrawer())}
          className="rounded px-2 py-1 text-gray-600 hover:bg-gray-100"
        >
          ✕
        </button>
      </header>
      <div role="tablist" aria-label={DE.SAMMLUNG_TITLE} className="flex flex-wrap gap-1 border-b border-[var(--line)] px-2 py-1">
        {DRAWER_TABS.map((t) => {
          const count = index ? index.counts[t.id] : 0;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => dispatch(setDrawerTab(t.id))}
              className={'rounded px-2 py-1 text-sm '
                + (tab === t.id ? 'bg-gray-900 text-white' : 'text-gray-700 hover:bg-gray-100')}
            >
              {t.label}
              {' '}
              <span className="tabular-nums opacity-75">{count}</span>
            </button>
          );
        })}
      </div>
      {renameSplit && (
        <div role="alert" className="border-b border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {formatDe(DE.ERR_RENAME_SPLIT, renameSplit.cloudName)}
        </div>
      )}
      {newActions.length > 0 && (
        <div
          role="group"
          aria-label={CODE_DE.SAMMLUNG_NEW}
          className="flex flex-wrap items-center gap-1 border-b border-[var(--line)] px-2 py-1.5"
        >
          <span className="text-xs font-semibold text-gray-500">{CODE_DE.SAMMLUNG_NEW}</span>
          {newActions.map(({ label, action }) => (
            <button
              key={label}
              type="button"
              onClick={() => provider.dispatchAction(action)}
              className="rounded border border-[var(--line)] px-2 py-0.5 text-xs text-gray-800 hover:bg-gray-50"
            >
              {label}
            </button>
          ))}
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {cards.length === 0 ? (
          <p className="p-3 text-sm text-gray-600">{DE.DRAWER_EMPTY_TAB}</p>
        ) : (
          <ul className="divide-y divide-[var(--line)]">
            {cards.map((card) => {
              const id = focusIdOf(card);
              const selected = id === drawer.focusId;
              const insertable = snippetable(card);
              return (
                <li
                  key={`${card.assetKind}:${id}`}
                  className={insertable ? 'flex items-stretch' : undefined}
                  draggable={insertable ? 'true' : undefined}
                  onDragStart={insertable ? (e) => {
                    e.dataTransfer.setData(
                      SNIPPET_MIME, JSON.stringify({ kind: card.assetKind, name: card.assetName }),
                    );
                    e.dataTransfer.effectAllowed = 'copy';
                  } : undefined}
                >
                  <button
                    type="button"
                    aria-pressed={selected}
                    onClick={() => dispatch(setDrawerFocus(selected ? null : id))}
                    className={(insertable ? 'min-w-0 flex-1 ' : 'w-full ') + 'px-3 py-2 text-left '
                      + (selected ? 'bg-gray-100' : 'hover:bg-gray-50')}
                  >
                    <span className="block truncate text-sm font-semibold text-gray-900" title={card.assetName}>
                      {card.assetName}
                    </span>
                    {card.meta && <span className="block truncate text-xs text-gray-500">{card.meta}</span>}
                    {card.chips.length > 0 && (
                      <span className="mt-1 flex flex-wrap gap-1">
                        {card.chips.map((c) => (
                          <span key={c.text} className={`rounded px-1.5 text-xs ${CHIP_CLASSES[c.level] || ''}`}>
                            {c.text}
                          </span>
                        ))}
                      </span>
                    )}
                  </button>
                  {insertable && (
                    <button
                      type="button"
                      aria-label={`${CODE_DE.SAMMLUNG_INSERT}: ${card.assetName}`}
                      title={CODE_DE.SAMMLUNG_INSERT_TITLE}
                      onClick={() => insert(card)}
                      className="shrink-0 px-2 text-xs font-medium text-[var(--accent)] hover:bg-gray-50"
                    >
                      {CODE_DE.SAMMLUNG_INSERT}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {focused && <div className="border-t border-[var(--line)]">{renderDetail()}</div>}
      </div>
    </aside>
  );
}
