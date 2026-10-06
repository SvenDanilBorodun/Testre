// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// `/daten/command` (spec §J.3): the one rosbridge service every mutating Daten
// action goes through, normalised to ONE result shape the page can switch on:
//
//   {ok, code, message, result, oldImage, unreachable}
//
// `message` is the robot's German sentence (shown verbatim on a refusal);
// `result` the parsed `result_json`. An image without the service (≤ 2.26.0)
// makes rosbridge answer „Service /daten/command does not exist" at once
// (P10): `oldImage` is then true and the tab shows `copy.old.image` alone.
// A call that never reached the robot (no link, a timeout) is `unreachable`.

import { useCallback } from 'react';
import { useRosServiceCaller } from '../../../hooks/useRosServiceCaller';

const OLD_IMAGE = /does not exist/i;

function parseObject(text) {
  if (typeof text !== 'string' || !text) return {};
  try {
    const v = JSON.parse(text);
    return v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  } catch {
    return {};
  }
}

/**
 * Run one action through `call(action, args)` (useRosServiceCaller's
 * `datenCommand`) and normalise its answer. Never throws.
 */
export async function runDatenCommand(call, action, args = {}) {
  try {
    const r = await call(action, args);
    const ok = !!(r && r.success);
    return {
      ok,
      code: ok ? '' : String((r && r.code) || 'internal'),
      message: String((r && r.message) || ''),
      result: parseObject(r && r.result_json),
      oldImage: false,
      unreachable: false,
    };
  } catch (err) {
    const text = String((err && err.message) || err || '');
    const oldImage = OLD_IMAGE.test(text);
    return {
      ok: false,
      code: oldImage ? 'old_image' : 'unreachable',
      message: '',
      result: {},
      oldImage,
      unreachable: !oldImage,
    };
  }
}

/** `(action, args) => Promise<result>` for the components of the tab. */
export default function useDatenCommand() {
  const { datenCommand } = useRosServiceCaller();
  return useCallback((action, args) => runDatenCommand(datenCommand, action, args), [datenCommand]);
}
