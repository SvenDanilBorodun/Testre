/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useRef, useState } from 'react';
import { generatePython } from './pythonCodeGen';

// Read-only „Code"-Vorschau: renders the student's Blockly program as real
// Python (English `robot.*` API). PURE DISPLAY — the code is never executed;
// the real run goes through the server-side interpreter. Regenerated whenever
// the block tree changes (`changeToken`), reading the LIVE workspace.
//
// `generatePython` lazily pulls in `blockly/python` (a separate async chunk),
// so this component (and `blockly/python`) only load when the panel is opened.
function CodeView({ workspace, changeToken }) {
  const [code, setCode] = useState('');
  // 'idle' (no workspace) | 'loading' (first generation) | 'ready' | 'error'
  const [status, setStatus] = useState('idle');
  // Monotonic request id so a slow generation can't overwrite a newer result.
  const reqIdRef = useRef(0);

  useEffect(() => {
    if (!workspace) {
      setCode('');
      setStatus('idle');
      return undefined;
    }
    let cancelled = false;
    const myReq = reqIdRef.current + 1;
    reqIdRef.current = myReq;
    // Only show the loading state before the first successful generation; later
    // regenerations keep the previous code visible (no flicker on every edit).
    setStatus((prev) => (prev === 'ready' ? 'ready' : 'loading'));
    generatePython(workspace)
      .then((src) => {
        if (cancelled || myReq !== reqIdRef.current) return;
        setCode(src);
        setStatus('ready');
      })
      .catch((e) => {
        if (cancelled || myReq !== reqIdRef.current) return;
        // eslint-disable-next-line no-console
        console.warn('CodeView: Python-Vorschau konnte nicht erzeugt werden', e);
        setStatus('error');
      });
    return () => {
      cancelled = true;
    };
  }, [workspace, changeToken]);

  let body;
  if (status === 'error') {
    body = (
      <p className="px-3 py-4 text-xs text-[var(--danger,#dc2626)]">
        Code-Vorschau konnte nicht erzeugt werden.
      </p>
    );
  } else if (status === 'loading') {
    body = (
      <p className="px-3 py-4 text-xs text-[var(--ink-3)]">
        Code-Vorschau wird erstellt …
      </p>
    );
  } else if (!code.trim()) {
    body = (
      <p className="px-3 py-4 text-xs text-[var(--ink-3)]">
        Noch keine Blöcke — ziehe links Bausteine in den Arbeitsbereich.
      </p>
    );
  } else {
    body = (
      <pre
        className="m-0 px-3 py-2 text-[12px] leading-relaxed font-mono text-[var(--ink)] whitespace-pre overflow-auto max-h-64"
      >
        <code>{code}</code>
      </pre>
    );
  }

  return (
    <div className="text-[var(--ink)]">
      <p className="px-3 pt-2 text-[11px] text-[var(--ink-3)]">
        Automatisch aus deinen Blöcken erzeugt · nur zum Ansehen
      </p>
      {body}
    </div>
  );
}

export default CodeView;
