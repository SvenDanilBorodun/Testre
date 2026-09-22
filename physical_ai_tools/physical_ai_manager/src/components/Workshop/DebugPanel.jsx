/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useState } from 'react';
import { DE } from './blocks/messages_de';
import SensorPanel from './SensorPanel';
import VariableInspector from './VariableInspector';
import BreakpointList from './BreakpointList';
import CodeBreakpointPanel from './code/CodeBreakpointPanel';
import { isCodeLanguage } from './code/codeProject';

const TABS = ['sensors', 'variables', 'breakpoints'];

function tabLabel(tab) {
  switch (tab) {
    case 'sensors':
      return DE.DEBUG_TAB_SENSORS;
    case 'variables':
      return DE.DEBUG_TAB_VARIABLES;
    case 'breakpoints':
      return DE.DEBUG_TAB_BREAKPOINTS;
    default:
      return tab;
  }
}

/**
 * `codeLanguage` is the OPEN workflow's, '' for a Blockly one. Only the
 * „Haltepunkte" tab differs: a code program has no blocks to Alt-click and no
 * Blockly workspace to resolve a label against, and Java has no breakpoints at
 * all this round (A8) — so that tab is a different component, not a flag.
 * „Sensoren" and „Variablen" are notation-independent and stay as they are.
 */
function DebugPanel({ workspace, codeLanguage = '' }) {
  const [tab, setTab] = useState('sensors');
  const isCode = isCodeLanguage(codeLanguage);

  return (
    <aside
      className="bg-white rounded-lg border border-[var(--line)] flex flex-col h-full overflow-hidden"
      aria-label="Debug-Panel"
    >
      <div role="tablist" className="flex border-b border-[var(--line)] bg-[var(--bg-sunk)]">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            tabIndex={tab === t ? 0 : -1}
            onClick={() => setTab(t)}
            className={
              'flex-1 px-3 py-2 text-sm font-medium border-b-2 '
              + 'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 '
              + (tab === t
                ? 'border-blue-500 text-[var(--ink)] bg-white'
                : 'border-transparent text-[var(--ink-3)] hover:text-[var(--ink)]')
            }
          >
            {tabLabel(t)}
          </button>
        ))}
      </div>
      <div role="tabpanel" className="flex-1 overflow-auto p-3">
        {tab === 'sensors' && <SensorPanel />}
        {tab === 'variables' && <VariableInspector />}
        {tab === 'breakpoints' && (isCode
          ? <CodeBreakpointPanel language={codeLanguage} />
          : <BreakpointList workspace={workspace} />)}
      </div>
    </aside>
  );
}

export default DebugPanel;
