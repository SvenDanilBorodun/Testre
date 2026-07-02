/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { DE } from './blocks/messages_de';

// Roboter Studio right dock — the tabbed, collapsible tool panel that replaced
// the tall stacked right column (camera + jog + record + 3D + Lernpfad, all
// stacked and scrolling off-screen on a 1920×1080 student display). A vertical
// tab rail on the far right; the body shows UP TO TWO open panels stacked
// vertically (resizable divider). A collapse button folds the whole dock to just
// the rail so the Blockly editor goes near-full width.
//
// CONTROLLED component: it owns NO open/collapsed state — the parent
// (WorkshopPage) holds `openIds` + `collapsed` (persisted) and the toggle logic,
// so it can also force a panel open (e.g. „Lernpfad" while a tutorial runs) and
// keep `debuggerVisible` in sync. This component only renders + reports intent.
//
// STABILITY INVARIANT (safety-relevant): each open panel is rendered as a
// `<section key={tab.id}>` that is ALWAYS a direct child of the same body
// container, and its content comes from `tab.render()` (invoked, NOT used as a
// `<tab.render/>` element type). React therefore keeps the panel INSTANCE mounted
// across "open a 2nd panel" / resize / re-render — a spurious remount of a live
// JogPanel/RecordPanel would fire their unmount teardown (re-torque the arm /
// cancel an in-progress recording). Panels unmount ONLY on a deliberate close,
// evict, or dock collapse.

const SPLIT_STORAGE_KEY = 'edubotics_workshop_dock_split';
const SPLIT_MIN = 0.18;
const SPLIT_MAX = 0.82;
const SPLIT_DEFAULT = 0.5;

function readSplit() {
  try {
    const raw = window.localStorage.getItem(SPLIT_STORAGE_KEY);
    if (raw == null) return SPLIT_DEFAULT;
    const n = Number(raw);
    if (!Number.isFinite(n)) return SPLIT_DEFAULT;
    return Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, n));
  } catch (e) {
    return SPLIT_DEFAULT;
  }
}

/**
 * @param {Array<{id:string,label:string,icon:string,render:()=>React.ReactNode,
 *   hidden?:boolean,busy?:boolean}>} tabs - tab registry (render is INVOKED).
 * @param {string[]} openIds - ids of the currently open panels (top→bottom).
 * @param {boolean} collapsed - dock folded to the rail only.
 * @param {(id:string)=>void} onToggleTab - rail click: open/close/evict (expand
 *   first when collapsed). The parent owns the busy-pin + 2-cap logic.
 * @param {(id:string)=>void} onCloseTab - the per-panel ✕ (close only).
 * @param {()=>void} onToggleCollapsed - fold/unfold the whole dock.
 * @param {number} width - desktop dock width in px (from the drag divider). Wired
 *   through a `--dock-w` CSS var so `w-full` still wins on mobile.
 */
function RightDock({
  tabs = [],
  openIds = [],
  collapsed = false,
  onToggleTab,
  onCloseTab,
  onToggleCollapsed,
  width = 380,
}) {
  const [split, setSplit] = useState(readSplit);
  const bodyRef = useRef(null);
  const draggingRef = useRef(false);
  const splitRef = useRef(split);
  useEffect(() => { splitRef.current = split; }, [split]);

  const visibleTabs = tabs.filter((t) => t && !t.hidden);
  // Resolve open ids → tab objects, dropping hidden/unknown ids (e.g. a
  // persisted „control" while in the simulator, where that tab is hidden).
  const openTabs = openIds
    .map((id) => visibleTabs.find((t) => t.id === id))
    .filter(Boolean);
  const twoUp = openTabs.length === 2;

  const persistSplit = useCallback((value) => {
    try {
      window.localStorage.setItem(SPLIT_STORAGE_KEY, String(value));
    } catch (e) { /* quota / private mode — keep the in-memory value */ }
  }, []);

  const onDividerDown = useCallback((e) => {
    draggingRef.current = true;
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch (_) { /* jsdom */ }
  }, []);
  const onDividerMove = useCallback((e) => {
    if (!draggingRef.current || !bodyRef.current) return;
    const rect = bodyRef.current.getBoundingClientRect();
    if (!rect.height) return;
    const frac = (e.clientY - rect.top) / rect.height;
    setSplit(Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, frac)));
  }, []);
  const onDividerUp = useCallback((e) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch (_) { /* jsdom */ }
    persistSplit(splitRef.current);
  }, [persistSplit]);

  const railButton = (tab) => {
    const active = openIds.includes(tab.id);
    return (
      <button
        key={tab.id}
        type="button"
        onClick={() => onToggleTab && onToggleTab(tab.id)}
        aria-pressed={active}
        title={tab.label}
        className={
          'relative flex md:flex-col items-center justify-center gap-0.5 '
          + 'md:w-full px-2 md:px-1 py-1.5 rounded-md border text-[9px] font-medium '
          + 'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 '
          + (active
            ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
            : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
        }
      >
        <span className="text-base leading-none" aria-hidden="true">{tab.icon}</span>
        <span className="hidden md:block leading-tight">{tab.label}</span>
        {tab.busy && (
          <span
            className="absolute top-0.5 right-0.5 w-1.5 h-1.5 rounded-full bg-red-500 motion-safe:animate-pulse"
            aria-hidden="true"
          />
        )}
      </button>
    );
  };

  return (
    <aside
      style={collapsed ? undefined : { '--dock-w': `${width}px` }}
      className={
        'flex flex-col md:flex-row shrink-0 w-full md:h-full '
        + 'border-t md:border-t-0 md:border-l border-[var(--line)] bg-[var(--bg-sunk)] '
        + (collapsed ? 'md:w-[52px]' : 'md:w-[var(--dock-w)]')
      }
      aria-label="Werkzeuge"
    >
      {/* Body — the open panels (hidden when collapsed). Order [Body, Rail] puts
          the rail on the RIGHT on desktop and at the bottom on mobile. */}
      {!collapsed && (
        <div
          ref={bodyRef}
          className="order-2 md:order-1 flex-1 min-w-0 min-h-0 flex flex-col p-2 gap-2 overflow-hidden"
        >
          {openTabs.length === 0 ? (
            <div className="flex-1 flex items-center justify-center text-center px-4">
              <p className="text-xs text-[var(--ink-4)]">
                Wähle rechts ein Werkzeug (z. B. „Kamera" oder „Steuern").
                Du kannst zwei gleichzeitig öffnen.
              </p>
            </div>
          ) : (
            openTabs.map((tab, i) => (
              <React.Fragment key={tab.id}>
                {twoUp && i === 1 && (
                  <div
                    role="separator"
                    aria-orientation="horizontal"
                    onPointerDown={onDividerDown}
                    onPointerMove={onDividerMove}
                    onPointerUp={onDividerUp}
                    title="Größe ändern"
                    className="shrink-0 h-2 -my-0.5 flex items-center justify-center cursor-row-resize group"
                  >
                    <span className="w-10 h-1 rounded-full bg-[var(--line-2)] group-hover:bg-[var(--accent)]" />
                  </div>
                )}
                <section
                  className={
                    'flex flex-col min-h-0 overflow-hidden rounded-lg border '
                    + 'border-[var(--line)] bg-white '
                    + (twoUp ? '' : 'flex-1')
                  }
                  style={twoUp ? { flex: `${i === 0 ? split : 1 - split} 1 0px` } : undefined}
                  aria-label={tab.label}
                >
                  <div className="flex items-center justify-between gap-2 px-2.5 py-1 border-b border-[var(--line)] bg-[var(--bg-sunk)] shrink-0">
                    <span className="text-xs font-semibold text-[var(--ink)] truncate flex items-center gap-1.5">
                      <span aria-hidden="true">{tab.icon}</span>
                      {tab.label}
                    </span>
                    <button
                      type="button"
                      onClick={() => onCloseTab && onCloseTab(tab.id)}
                      disabled={!!tab.busy}
                      title={tab.busy ? DE.DOCK_CLOSE_BUSY : DE.DOCK_CLOSE_PANEL}
                      aria-label={`${tab.label} ${DE.DOCK_CLOSE_PANEL}`}
                      className="text-sm leading-none px-1.5 py-0.5 rounded text-[var(--ink-4)] hover:bg-[var(--bg-sunk)] hover:text-[var(--ink)] disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      ✕
                    </button>
                  </div>
                  <div className="flex-1 min-h-0 overflow-auto p-2">
                    {tab.render()}
                  </div>
                </section>
              </React.Fragment>
            ))
          )}
        </div>
      )}

      {/* Rail — collapse button + one button per (non-hidden) tab. */}
      <div
        className={
          'order-1 md:order-2 shrink-0 flex md:flex-col items-stretch gap-1 '
          + 'p-1.5 overflow-x-auto md:overflow-y-auto md:w-[52px] '
          + 'border-b md:border-b-0 md:border-l border-[var(--line)] bg-white'
        }
      >
        <button
          type="button"
          onClick={onToggleCollapsed}
          aria-pressed={collapsed}
          title={collapsed ? DE.DOCK_EXPAND : DE.DOCK_COLLAPSE}
          aria-label={collapsed ? DE.DOCK_EXPAND : DE.DOCK_COLLAPSE}
          className="flex items-center justify-center md:w-full px-2 md:px-1 py-1.5 rounded-md border border-[var(--line)] bg-white text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
        >
          <span className="text-sm leading-none" aria-hidden="true">
            {collapsed ? '⟨' : '⟩'}
          </span>
        </button>
        {visibleTabs.map(railButton)}
      </div>
    </aside>
  );
}

export default RightDock;
