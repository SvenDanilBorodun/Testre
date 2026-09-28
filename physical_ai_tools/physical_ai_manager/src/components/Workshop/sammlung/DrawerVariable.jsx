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
 * Sammlung drawer detail of ONE variable. Of a Blockly variable: renaming
 * refuses a name another variable already holds (Blockly would silently merge
 * the two); deleting goes through Blockly's own „Variable löschen",
 * confirmation and all. Of a Python/Java variable (owner decision O8): no
 * rename, no delete — there is no language server (decision D7), so the
 * drawer says the program is where that happens; „Benutzt in" lists the code
 * lines, and Java says its values come only through Robot.zeige(…) (A8).
 *
 * The values come from the RUN (the [VAR:] sentinel): the last one with its age,
 * the last five (`workshop.variableHistory`, newest first), and — for a point
 * {x, y, z} — „Im Simulator zeigen", which highlights its violet marker. Values
 * are keyed by variable NAME, like the sentinel.
 */

import React, { useEffect, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import { setDrawerFocus } from '../../../features/workshop/studioAssetsSlice';
import { DE, formatDe } from '../blocks/messages_de';
import { CODE_DE } from '../code/codeMessagesDe';
import { assetDocumentOf } from './assetDocument';
import { displayValue, pointFromValue } from './assetIndex';
import { DetailRow, RenameField, UsageList } from './drawerParts';

export const DRAWER_VALUE_MAX_CHARS = 200;

const ownEntry = (map, name) => (
  map && typeof map === 'object' && Object.prototype.hasOwnProperty.call(map, name) ? map[name] : null
);

const timeDe = (ts) => {
  const d = new Date(ts);
  return Number.isFinite(d.getTime()) ? d.toLocaleTimeString('de-DE') : '—';
};

export default function DrawerVariable({
  assetDoc: assetDocProp = null, workspace = null, card, capabilities, onPreview,
}) {
  const dispatch = useDispatch();
  const assetDoc = assetDocumentOf(assetDocProp, workspace);
  const rows = assetDoc ? assetDoc.usageRows('variable', card.assetId) : [];
  const isCode = !!assetDoc && assetDoc.kind === 'code';
  const editable = !!assetDoc && assetDoc.canEditVariables !== false;
  const name = card.assetName;
  const current = useSelector((s) => ownEntry(s.workshop && s.workshop.variables, name));
  const history = useSelector((s) => ownEntry(s.workshop && s.workshop.variableHistory, name));
  const hasValue = !!current && typeof current === 'object' && 'value' in current;
  const point = hasValue ? pointFromValue(current.value) : null;
  const entries = Array.isArray(history) ? history : [];

  // The age ticks once a second while there is a value to age (a value newer
  // than the last tick reads „vor 0 s", never a negative age).
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!hasValue) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [hasValue]);
  const age = hasValue && Number.isFinite(current.ts)
    ? Math.max(0, Math.floor((now - current.ts) / 1000)) : 0;

  const handleRename = (draft) => {
    const result = assetDoc.renameVariable(card.assetId, draft);
    if (!result.ok) {
      toast.error(result.error);
      return false;
    }
    return true;
  };

  const handleDelete = () => {
    const result = assetDoc.deleteVariable(card.assetId);
    if (!result.ok) {
      toast.error(result.error);
      return;
    }
    // Blockly may still be asking; if the student declines, the variable stays
    // in the list and can be picked again.
    dispatch(setDrawerFocus(null));
  };

  return (
    <div className="p-3">
      {editable ? (
        <RenameField key={card.assetName} name={card.assetName} maxLength={64} onCommit={handleRename} />
      ) : (
        <h3 className="truncate text-base font-semibold text-gray-900" title={card.assetName}>{card.assetName}</h3>
      )}
      {isCode && assetDoc.language === 'java' && (
        <p className="mt-1 text-xs text-gray-600">{CODE_DE.VARIABLES_JAVA_NOTE}</p>
      )}
      {hasValue ? (
        <dl className="mt-2 space-y-1">
          <DetailRow label={DE.DRAWER_LAST_VALUE}>
            {formatDe(DE.CARD_VARIABLE_VALUE, displayValue(current.value, DRAWER_VALUE_MAX_CHARS), age)}
          </DetailRow>
        </dl>
      ) : (
        <p className="mt-2 text-sm text-gray-600">{DE.DRAWER_NO_VALUE}</p>
      )}
      {point && capabilities && capabilities.previewVariables === true && typeof onPreview === 'function' && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => onPreview({ kind: 'variable', id: card.assetId, name })}
            className="rounded border border-[var(--line)] px-2 py-1 text-sm hover:bg-gray-50"
          >
            {DE.DRAWER_SHOW_POINT}
          </button>
        </div>
      )}
      {entries.length > 0 && (
        <section aria-label={DE.DRAWER_LAST_VALUES} className="mt-3">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">{DE.DRAWER_LAST_VALUES}</h4>
          <ul className="mt-1 space-y-1">
            {entries.map((h, i) => (
              // Two entries may share a timestamp, so the position joins the key.
              <li key={`${h && h.ts}:${i}`} className="break-words text-sm text-gray-900">
                {`${displayValue(h && h.value, DRAWER_VALUE_MAX_CHARS)} · ${timeDe(h && h.ts)}`}
              </li>
            ))}
          </ul>
        </section>
      )}
      <UsageList
        assetDoc={assetDoc}
        rows={rows}
        nowhereText={isCode ? CODE_DE.VARIABLE_USED_NOWHERE : DE.DRAWER_USED_NOWHERE_VARIABLE}
      />
      {editable ? (
        <button
          type="button"
          onClick={handleDelete}
          className="mt-4 rounded border border-red-300 px-2 py-1 text-sm text-red-700 hover:bg-red-50"
        >
          {DE.DRAWER_DELETE_VARIABLE}
        </button>
      ) : (
        <p className="mt-4 text-xs text-gray-600">{CODE_DE.VARIABLES_EDIT_IN_CODE}</p>
      )}
    </div>
  );
}
