/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useState } from 'react';
import { DE } from './blocks/messages_de';
import Icon from '../icons/Icon';
import ResizeHandle from './layout/ResizeHandle';
import { DOCK_SPLIT, readDockSplit, writeDockSplit } from './layout/layoutPrefs';
import { LAYOUT_DE, fillLayout } from './layout/layoutCopy';

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
// JogPanel would fire its unmount teardown (re-torque the arm). Panels unmount
// ONLY on a deliberate close,
// evict, or dock collapse.
//
// SIZES are CSS variables, each written only by its layout/ResizeHandle.jsx:
// the dock's WIDTH is `--dock-w`, set on the editor row by the page's divider
// (this aside only reads it — it must never set it in its own `style`, which
// would shadow the row's value), and the two-panel split is `--dock-split`, set
// on the body by the divider below. A drag changes only the variable, so the
// panels never re-render while it runs.

/**
 * @param {Array<{id:string,label:string,icon:string,render:()=>React.ReactNode,
 *   hidden?:boolean,busy?:boolean}>} tabs - tab registry (render is INVOKED).
 * @param {string[]} openIds - ids of the currently open panels (top→bottom).
 * @param {boolean} collapsed - dock folded to the rail only.
 * @param {(id:string)=>void} onToggleTab - rail click: open/close/evict (expand
 *   first when collapsed). The parent owns the busy-pin + 2-cap logic.
 * @param {(id:string)=>void} onCloseTab - the per-panel ✕ (close only).
 * @param {()=>void} onToggleCollapsed - fold/unfold the whole dock.
 */
function RightDock({
  tabs = [],
  openIds = [],
  collapsed = false,
  onToggleTab,
  onCloseTab,
  onToggleCollapsed,
}) {
  // The committed two-panel split (the share of the TOP panel).
  const [split, setSplit] = useState(readDockSplit);

  const visibleTabs = tabs.filter((t) => t && !t.hidden);
  // Resolve open ids → tab objects, dropping hidden/unknown ids (e.g. a
  // persisted „control" while in the simulator, where that tab is hidden).
  const openTabs = openIds
    .map((id) => visibleTabs.find((t) => t.id === id))
    .filter(Boolean);
  const twoUp = openTabs.length === 2;

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
        <Icon name={tab.icon} className="text-base" />
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
      // Below md the dock stacks UNDER the editor. Expanded it takes exactly
      // half the row (`h-1/2`): the Blockly editor keeps the other half, and the
      // panels get a DEFINITE height to share — content-sized, two open panels
      // (each `flex: n 1 0px`) collapsed to zero. Collapsed it is just the rail.
      // From md up it sits beside the editor at full height.
      className={
        'flex flex-col md:flex-row shrink-0 w-full md:h-full '
        + (collapsed ? '' : 'h-1/2 ')
        + 'border-t md:border-t-0 md:border-l border-[var(--line)] bg-[var(--bg-sunk)] '
        + (collapsed ? 'md:w-[52px]' : 'md:w-[var(--dock-w,380px)]')
      }
      aria-label="Werkzeuge"
    >
      {/* Body — the open panels (hidden when collapsed). Order [Body, Rail] puts
          the rail on the RIGHT on desktop and at the bottom on mobile. */}
      {!collapsed && (
        <div
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
                {/* The split divider: inside the SAME Fragment, before the
                    second section, so the sections keep their keys and stay
                    direct children of this body. Shown below md too — there
                    the expanded dock is half the row and two panels share it. */}
                {twoUp && i === 1 && (
                  <ResizeHandle
                    orientation="horizontal"
                    cssVar="--dock-split"
                    unit="fraction"
                    value={split}
                    defaultValue={DOCK_SPLIT.default}
                    bounds={() => ({ min: DOCK_SPLIT.min, max: DOCK_SPLIT.max })}
                    fromPointer={(e, r) => (e.clientY - r.top) / r.height}
                    growKey="ArrowDown"
                    step={0.02}
                    largeStep={0.1}
                    label={LAYOUT_DE.DOCK_SPLIT}
                    valueText={(v) => fillLayout(LAYOUT_DE.VALUE_PERCENT, Math.round(v * 100))}
                    onCommit={(v) => { setSplit(v); writeDockSplit(v); }}
                    showBelowMd
                    className="-my-0.5"
                  />
                )}
                <section
                  className={
                    'flex flex-col min-h-0 overflow-hidden rounded-lg border '
                    + 'border-[var(--line)] bg-white '
                    + (twoUp ? '' : 'flex-1')
                  }
                  style={twoUp
                    ? { flex: i === 0 ? 'var(--dock-split,0.5) 1 0px' : 'calc(1 - var(--dock-split,0.5)) 1 0px' }
                    : undefined}
                  aria-label={tab.label}
                >
                  <div className="flex items-center justify-between gap-2 px-2.5 py-1 border-b border-[var(--line)] bg-[var(--bg-sunk)] shrink-0">
                    <span className="text-xs font-semibold text-[var(--ink)] truncate flex items-center gap-1.5">
                      <Icon name={tab.icon} />
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
                      <Icon name="close" />
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
          <Icon name={collapsed ? 'chevronLeft' : 'chevronRight'} size={16} />
        </button>
        {visibleTabs.map(railButton)}
      </div>
    </aside>
  );
}

export default RightDock;
