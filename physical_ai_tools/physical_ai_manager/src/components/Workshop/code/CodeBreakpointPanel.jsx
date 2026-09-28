/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Debug-Panel's „Haltepunkte" tab for a CODE program. It is a separate
// component from `BreakpointList`, not a flag on it, for three reasons that all
// bite at once: that one's hint tells the student to Alt-click a BLOCK, its
// per-id label resolves against a Blockly workspace a code workflow does not
// have, and its own `/workflow/set_breakpoints` push would be a SECOND push
// beside the one `CodeWorkspace` already owns.
//
// Java (A8): no breakpoints at all this round. The tab SAYS so rather than
// showing an empty list, which a student reads as „broken".

import React, { useCallback } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import { clearBreakpoints, removeBreakpoint } from '../../../features/workshop/workshopSlice';
import { DE } from '../blocks/messages_de';
import { isCodeBreakpointId } from './codeBreakpoints';
import { CODE_DE, formatCode } from './codeMessagesDe';
import Icon from '../../icons/Icon';

/**
 * `language` is the OPEN workflow's ('python' | 'java'). The set it lists is
 * `s.workshop.breakpoints` filtered to the ids this notation owns: a Blockly id
 * left over from another document is not a line and must not be shown as one
 * (the two id spaces share the field — codeBreakpoints.js).
 */
function CodeBreakpointPanel({ language }) {
  const dispatch = useDispatch();
  const breakpoints = useSelector((s) => s.workshop.breakpoints);
  const ids = (Array.isArray(breakpoints) ? breakpoints : []).filter(isCodeBreakpointId);

  const handleClear = useCallback(() => dispatch(clearBreakpoints()), [dispatch]);

  if (language === 'java') {
    return (
      <div className="text-sm" data-testid="code-breakpoints">
        <p className="text-xs text-[var(--ink-3)]">{CODE_DE.DEBUG_JAVA_NO_BREAKPOINTS}</p>
      </div>
    );
  }

  return (
    <div className="text-sm" data-testid="code-breakpoints">
      <p className="text-xs text-[var(--ink-3)] mb-2">{CODE_DE.DEBUG_BP_HINT_PY}</p>
      {ids.length === 0 ? (
        <p className="text-[var(--ink-4)]">{DE.DEBUG_NO_BREAKPOINTS}</p>
      ) : (
        <>
          <ul className="space-y-1 mb-2">
            {ids.map((id) => (
              <li
                key={id}
                className="flex items-center gap-2 px-2 py-1 rounded-md bg-red-50 border border-red-200"
              >
                <Icon name="dot" size="0.6em" fill="currentColor" className="text-red-500" />
                <span className="flex-1 truncate text-xs font-mono">{id}</span>
                <button
                  type="button"
                  onClick={() => dispatch(removeBreakpoint(id))}
                  className="text-xs text-red-700 hover:underline"
                  aria-label={formatCode(CODE_DE.DEBUG_BP_REMOVE, id)}
                >
                  <Icon name="close" />
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            onClick={handleClear}
            className="text-xs text-[var(--ink-3)] hover:underline"
          >
            {CODE_DE.DEBUG_BP_CLEAR}
          </button>
        </>
      )}
    </div>
  );
}

export default CodeBreakpointPanel;
