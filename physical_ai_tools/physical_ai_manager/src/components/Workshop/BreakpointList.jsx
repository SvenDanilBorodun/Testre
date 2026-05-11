/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useCallback } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import {
  addBreakpoint,
  removeBreakpoint,
  clearBreakpoints,
} from '../../features/workshop/workshopSlice';
import { DE } from './blocks/messages_de';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';

function blockLabel(workspace, id) {
  if (!workspace || !id) return id;
  const block = workspace.getBlockById(id);
  if (!block) return id;
  // The fully-resolved human-facing description. Falls back to type
  // string if Blockly doesn't expose a getDescription helper.
  if (typeof block.toString === 'function') {
    try {
      return block.toString();
    } catch (e) {
      return block.type;
    }
  }
  return block.type;
}

function BreakpointList({ workspace }) {
  const dispatch = useDispatch();
  const breakpoints = useSelector((s) => s.workshop.breakpoints);
  const { callService } = useRosServiceCaller();

  // Push current breakpoint set to the workflow runtime whenever it
  // changes. Audit round-3 §M — only call the service when a workflow
  // is running or paused, so a student toggling 30 breakpoints before
  // pressing Start doesn't fire 30 best-effort RPCs that all return
  // "Es läuft kein Workflow." and burn a 10 s timeout each through the
  // useRosServiceCaller queue.
  const runState = useSelector((s) => s.workshop.runState);
  const paused = useSelector((s) => s.workshop.paused);
  useEffect(() => {
    const isLive = runState === 'running' || paused;
    if (!isLive) return;
    callService(
      '/workflow/set_breakpoints',
      'physical_ai_interfaces/srv/WorkflowSetBreakpoints',
      { block_ids: breakpoints },
    ).catch(() => { /* swallow — server may have just exited */ });
  }, [breakpoints, callService, runState, paused]);

  // Wire a workspace right-click handler that toggles breakpoints.
  // We use a documented Blockly hook (registry contextMenu) when
  // available, falling back to listening on the workspace's SVG.
  useEffect(() => {
    if (!workspace) return undefined;
    const onClick = (event) => {
      // Only toggle on Alt+click as a discoverable, single-click
      // alternative to the right-click menu (which is plugin-heavy
      // territory). Right-click to open Blockly's standard menu still
      // works.
      if (!event.altKey) return;
      const target = event.target;
      if (!target || typeof target.closest !== 'function') return;
      // Walk up to a Blockly block group. Audit round-3 §O —
      // depth-bounded loops missed deeply nested block elements; use
      // element.closest with an attribute selector instead so the walk
      // is unbounded but still cheap.
      const node = target.closest('[data-id], [data-block-id]');
      const id = node
        ? node.getAttribute('data-id') || node.getAttribute('data-block-id')
        : null;
      if (!id) return;
      event.preventDefault();
      event.stopPropagation();
      if (breakpoints.includes(id)) {
        dispatch(removeBreakpoint(id));
      } else {
        dispatch(addBreakpoint(id));
      }
    };
    const svgRoot = workspace.getParentSvg && workspace.getParentSvg();
    if (svgRoot) {
      svgRoot.addEventListener('click', onClick);
      return () => svgRoot.removeEventListener('click', onClick);
    }
    return undefined;
  }, [workspace, breakpoints, dispatch]);

  const handleClear = useCallback(() => {
    dispatch(clearBreakpoints());
  }, [dispatch]);

  return (
    <div className="text-sm">
      <p className="text-xs text-[var(--ink-3)] mb-2">
        {DE.DEBUG_BP_TOGGLE_HINT}
        {' '}Alt+Klick auf einen Block setzt einen Haltepunkt.
      </p>
      {breakpoints.length === 0 ? (
        <p className="text-[var(--ink-4)]">{DE.DEBUG_NO_BREAKPOINTS}</p>
      ) : (
        <>
          <ul className="space-y-1 mb-2">
            {breakpoints.map((id) => (
              <li
                key={id}
                className="flex items-center gap-2 px-2 py-1 rounded-md bg-red-50 border border-red-200"
              >
                <span className="text-red-500">●</span>
                <span className="flex-1 truncate text-xs font-mono">
                  {blockLabel(workspace, id)}
                </span>
                <button
                  type="button"
                  onClick={() => dispatch(removeBreakpoint(id))}
                  className="text-xs text-red-700 hover:underline"
                  aria-label={`Haltepunkt entfernen: ${id}`}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            onClick={handleClear}
            className="text-xs text-[var(--ink-3)] hover:underline"
          >
            Alle entfernen
          </button>
        </>
      )}
    </div>
  );
}

export default BreakpointList;
