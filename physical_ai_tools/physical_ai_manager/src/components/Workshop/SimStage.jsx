/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Roboter Studio — integrated SIM STAGE (the right region while "Test im
// Simulator" is on). WorkshopPage renders this in place of the RightDock; the
// Blockly editor stays on the LEFT (never remounted).
//
// THIN SHELL by design: it owns NO WebGL and NO grasp state. It renders:
//   1. a slim top bar (German title + the „Bahn" trail toggle/clear buttons —
//      the same controls the real-mode „3D-Ansicht" tab carries);
//   2. <SimScene layout="split" …/> — SimScene keeps SOLE ownership of the single
//      UrdfTwin 3D pane + the grasp-attach state machine, so there is exactly ONE
//      WebGL context and one /sim/joint_states subscription by construction;
//   3. a compact German feedback line surfacing the runtime signals the server
//      already emits (the reroute log + the IK-precheck unreachable warnings,
//      both already in Redux) — hidden when there is nothing to show;
//   4. a collapsible debug strip that RunControls' Debug button drives while in
//      sim (the dock's „Debug" tab no longer renders here, so the button would
//      otherwise be dead — see WorkshopPage's onToggleDebug wiring).
//
// three.js is reached ONLY through SimScene's existing React.lazy(UrdfTwin)
// boundary — this module is statically imported by the eager WorkshopPage, so it
// MUST NOT import three/urdf-loader directly (that would drag them into the entry
// bundle the white-screen greps watch). It imports SimScene + DebugPanel only.
//
// `showShadows`/`showReach` are passed to SimScene as LITERAL `true` on purpose:
// UrdfTwin configures its shadow map + reach ring at MOUNT time from a []-deps
// effect (they are not toggled state), so they must be build-time constants.
// Shipping them on is the plan's decision; the one-line rollback is flipping the
// literals to `false` here.

import React from 'react';
import { useSelector } from 'react-redux';
import SimScene from './SimScene';
import DebugPanel from './DebugPanel';
import { DE } from './blocks/messages_de';

// Most-recent runtime log line that signals a no-go reroute. motion.py logs
// „[WARNUNG] Sperrzone auf dem Weg — Ausweichroute wird gefahren." via ctx.log →
// /workflow/status → state.workshop.log; we scan newest-first for either marker.
function latestRerouteMessage(log) {
  if (!Array.isArray(log)) return null;
  for (let i = log.length - 1; i >= 0; i -= 1) {
    const text = log[i] && typeof log[i].text === 'string' ? log[i].text : '';
    if (text.includes('Sperrzone') || text.includes('Ausweichroute')) return text;
  }
  return null;
}

function SimStage({
  scene,
  onChange,
  catalog,
  catalogDims,
  workspace,
  debugOpen,
  onToggleDebug,
  showPath,
  onToggleShowPath,
  onClearPath,
  pathClearToken,
}) {
  // Runtime feedback signals — both already maintained in Redux by the existing
  // run path (workshopSlice.log + debuggerWarnings). Read-only here.
  const log = useSelector((s) => s.workshop.log);
  const debuggerWarnings = useSelector((s) => s.workshop.debuggerWarnings);
  const rerouteMessage = latestRerouteMessage(log);
  const warningCount = Array.isArray(debuggerWarnings) ? debuggerWarnings.length : 0;
  const hasFeedback = !!rerouteMessage || warningCount > 0;

  return (
    <aside
      className={
        'flex flex-col w-full md:h-full shrink-0 md:w-[55%] min-h-0 '
        + 'border-t md:border-t-0 md:border-l border-[var(--line)] bg-[var(--bg-sunk)]'
      }
      aria-label="Simulator"
    >
      {/* Slim top bar: title + the „Bahn" trail controls (mirrors the real-mode
          „3D-Ansicht" tab — the Debug button lives in RunControls, not here). */}
      <div className="shrink-0 flex items-center gap-1.5 flex-wrap px-3 py-2 border-b border-[var(--line)] bg-white">
        <span className="text-sm font-semibold text-[var(--ink)]">Simulator</span>
        <div className="ml-auto flex items-center gap-1.5 flex-wrap">
          <button
            type="button"
            onClick={onToggleShowPath}
            aria-pressed={!!showPath}
            title="Bewegungsbahn des Greifers anzeigen"
            className={
              'text-xs px-2.5 py-1 rounded-md border '
              + (showPath
                ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
            }
          >
            {showPath ? 'Bahn verbergen' : 'Bahn anzeigen'}
          </button>
          <button
            type="button"
            onClick={onClearPath}
            title="Bewegungsbahn löschen"
            className="text-xs px-2.5 py-1 rounded-md border bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]"
          >
            Bahn löschen
          </button>
        </div>
      </div>

      {/* Runtime feedback line — reroute + unreachable warnings (hidden empty). */}
      {hasFeedback && (
        <div className="shrink-0 flex flex-col gap-1 px-3 py-2 border-b border-[var(--line)] bg-[var(--bg-sunk)] text-xs">
          {rerouteMessage && (
            <div className="text-amber-700" role="status">
              {rerouteMessage}
            </div>
          )}
          {warningCount > 0 && (
            <div className="text-red-700" role="status">
              {`⚠ ${warningCount} Ziel(e) nicht erreichbar — siehe Markierungen an den Blöcken.`}
            </div>
          )}
        </div>
      )}

      {/* The integrated sim stage: SimScene owns the single UrdfTwin (3D pane on
          top, 2D placement/zone editor below). showShadows/showReach are LITERAL
          true — mount-time constants inside UrdfTwin (see the file-top note). */}
      <div className="flex-1 min-h-0 p-2 md:p-3 overflow-hidden">
        <SimScene
          layout="split"
          scene={scene}
          onChange={onChange}
          catalog={catalog}
          catalogDims={catalogDims}
          showPath={showPath}
          pathClearToken={pathClearToken}
          showShadows
          showReach
        />
      </div>

      {/* Collapsible debug strip — RunControls' Debug button toggles it in sim. */}
      {debugOpen && (
        <section
          className="shrink-0 h-64 flex flex-col border-t border-[var(--line)] bg-white overflow-hidden"
          aria-label={DE.DOCK_TAB_DEBUG}
        >
          <div className="flex items-center justify-between gap-2 px-2.5 py-1 border-b border-[var(--line)] bg-[var(--bg-sunk)] shrink-0">
            <span className="text-xs font-semibold text-[var(--ink)] flex items-center gap-1.5">
              <span aria-hidden="true">🔍</span>
              {DE.DOCK_TAB_DEBUG}
            </span>
            <button
              type="button"
              onClick={onToggleDebug}
              title="Debug schließen"
              aria-label="Debug schließen"
              className="text-sm leading-none px-1.5 py-0.5 rounded text-[var(--ink-4)] hover:bg-[var(--bg-sunk)] hover:text-[var(--ink)]"
            >
              ✕
            </button>
          </div>
          <div className="flex-1 min-h-0 overflow-auto p-2">
            <DebugPanel workspace={workspace} />
          </div>
        </section>
      )}
    </aside>
  );
}

export default SimStage;
