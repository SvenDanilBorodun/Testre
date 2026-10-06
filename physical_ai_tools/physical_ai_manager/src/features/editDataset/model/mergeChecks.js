// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The merge panel's compatibility check BEFORE anything starts (spec §D5,
// §D1 step 2), computed from the library entries: the seven MERGE_CHECKS. The
// server re-checks and refuses in German (`incompatible_de`) if the page was
// wrong; the button stays disabled while any row fails.

import COPY from '../datenCopy';
import { MERGE_CHECKS } from '../datenContract';
import { fill, fmtFps, joinAnd } from './format';
import { cameraLabel, codecName, hasGripper, orderCameras, robotName } from './labels';

const C = COPY.merge.check;

const same = (values) => values.every((v) => v === values[0]);
const json = (v) => JSON.stringify(v === undefined ? null : v);

function cameraKeys(entry) {
  return (entry.cameras || []).map((c) => String(c.key || c.name || '')).sort();
}

function videoSignature(camera) {
  const c = camera || {};
  return json([c.codec, c.pix_fmt, c.height, c.width, c.fps]);
}

function statsSignature(entry) {
  const s = entry.stat_names || {};
  const out = {};
  for (const k of Object.keys(s).sort()) out[k] = [...(s[k] || [])].map(String).sort();
  return json(out);
}

/**
 * @param {object[]} entries the selected library entries (`local[]` rows of §J.4.1)
 * @returns {{checks: {id: string, ok: boolean, text: string}[], ok: boolean}}
 */
export function mergeChecks(entries) {
  const sel = (entries || []).filter(Boolean);
  if (sel.length < 2) return { checks: [{ id: 'min', ok: false, text: C.minTwo }], ok: false };
  const first = sel[0];
  const checks = [];

  // version: v3 on every side (an old-format dataset cannot be picked anyway,
  // so the row is shown only when it fails).
  const versionOk = sel.every((e) => /^v3/.test(String(e.codebase_version || '')));
  if (!versionOk) checks.push({ id: 'version', ok: false, text: C.versionBad });

  const robotOk = same(sel.map((e) => e.robot_type));
  checks.push({ id: 'robot', ok: robotOk, text: robotOk ? fill(C.robotOk, { robot: robotName(first.robot_type) }) : C.robotBad });

  const fpsList = [...new Set(sel.map((e) => Number(e.fps)))].sort((a, b) => a - b);
  const fpsOk = fpsList.length === 1;
  checks.push({
    id: 'fps',
    ok: fpsOk,
    text: fpsOk
      ? fill(C.fpsOk, { fps: fmtFps(fpsList[0]) })
      : fill(C.fpsBad, { list: joinAnd(fpsList.map(fmtFps), COPY.count.listAnd) }),
  });

  const camerasOk = same(sel.map((e) => json(cameraKeys(e))));
  checks.push({
    id: 'cameras',
    ok: camerasOk,
    text: camerasOk ? fill(C.camerasOk, { names: orderCameras(first.cameras).map(cameraLabel).join(', ') }) : C.camerasBad,
  });

  const jointsOk = same(sel.map((e) => json([(e.joints && e.joints.state) || [], (e.joints && e.joints.action) || []])));
  const stateNames = (first.joints && first.joints.state) || [];
  const gripper = hasGripper(stateNames);
  checks.push({
    id: 'joints',
    ok: jointsOk,
    text: jointsOk
      ? fill(gripper ? C.jointsOk : C.jointsOkNoGripper, { n: gripper ? stateNames.length - 1 : stateNames.length })
      : C.jointsBad,
  });

  // video: per camera key that EVERY side has, the same codec/pix_fmt/size/fps
  // (a camera only one side has is the `cameras` row's failure, not this one's).
  const byKey = (e, key) => (e.cameras || []).find((c) => String(c.key || c.name || '') === key);
  const keys = cameraKeys(first).filter((key) => sel.every((e) => byKey(e, key)));
  const videoOk = keys.every((key) => same(sel.map((e) => videoSignature(byKey(e, key)))));
  const firstCam = orderCameras(first.cameras)[0] || {};
  checks.push({
    id: 'video',
    ok: videoOk,
    text: videoOk
      ? fill(C.videoOk, { codec: codecName(firstCam.codec), w: firstCam.width ?? '–', h: firstCam.height ?? '–' })
      : C.videoBad,
  });

  const statsOk = same(sel.map(statsSignature));
  checks.push({ id: 'stats', ok: statsOk, text: statsOk ? C.statsOk : C.statsBad });

  // Every id of the contract is decided above (version only when it fails).
  const ok = checks.every((c) => c.ok);
  return { checks, ok };
}

/** The seven check ids of the contract, in order (for tests and the server's names). */
export const CHECK_IDS = MERGE_CHECKS;
