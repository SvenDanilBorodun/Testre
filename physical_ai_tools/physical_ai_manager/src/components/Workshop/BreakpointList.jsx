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
  // changes. Service is best-effort — if the workflow isn't running
  // yet the call will return false success.
  useEffect(() => {
    callService(
      '/workflow/set_breakpoints',
      'physical_ai_interfaces/srv/WorkflowSetBreakpoints',
      { block_ids: breakpoints },
    ).catch(() => { /* swallow — server may not be running yet */ });
  }, [breakpoints, callService]);

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
      if (!target) return;
      // Walk up to a Blockly block group via dataset attribute.
      let id = null;
      let el = target;
      for (let i = 0; i < 8 && el; i += 1) {
        if (el.getAttribute) {
          const candidate = el.getAttribute('data-id') || el.getAttribute('data-block-id');
          if (candidate) {
            id = candidate;
            break;
          }
        }
        el = el.parentElement;
      }
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
