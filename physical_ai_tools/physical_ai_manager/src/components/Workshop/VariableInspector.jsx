/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React from 'react';
import { useSelector } from 'react-redux';
import { DE } from './blocks/messages_de';

function fmtValue(v) {
  if (v === null) return 'null';
  if (v === undefined) return '–';
  if (typeof v === 'string') return JSON.stringify(v);
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(3);
  try {
    return JSON.stringify(v);
  } catch (e) {
    return String(v);
  }
}

function VariableInspector() {
  const variables = useSelector((s) => s.workshop.variables);
  const entries = Object.entries(variables || {});

  if (entries.length === 0) {
    return (
      <p className="text-[var(--ink-4)] text-sm">{DE.DEBUG_NO_VARIABLES}</p>
    );
  }

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-[var(--ink-3)] border-b border-[var(--line)]">
          <th className="py-1 pr-2 font-medium">Name</th>
          <th className="py-1 px-2 font-medium">Wert</th>
          <th className="py-1 pl-2 font-medium text-right">Aktualisiert</th>
        </tr>
      </thead>
      <tbody>
        {entries.map(([name, info]) => {
          const ageMs = Date.now() - (info?.ts || 0);
          const flash = ageMs < 500;
          return (
            <tr
              key={name}
              className={
                'border-b border-[var(--line-soft)] '
                + (flash
                  ? 'bg-yellow-50 transition-colors'
                  : 'transition-colors')
              }
            >
              <td className="py-1 pr-2 font-mono text-xs">{name}</td>
              <td className="py-1 px-2 font-mono text-xs break-all">{fmtValue(info?.value)}</td>
              <td className="py-1 pl-2 text-xs text-[var(--ink-4)] text-right">
                {ageMs < 1000 ? 'jetzt' : `vor ${Math.round(ageMs / 1000)} s`}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export default VariableInspector;
