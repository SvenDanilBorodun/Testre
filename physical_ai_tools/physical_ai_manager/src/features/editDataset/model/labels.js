// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// What the page calls a robot, a codec, a joint and a camera (spec §J.6
// tables, §F1 camera order), pure, with the fallbacks the spec names: a robot
// or joint by its id, a codec upper-cased, a camera by its key's last part.

import COPY from '../datenCopy';
import { fill } from './format';

const own = (table, key) => (typeof key === 'string' && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : null);

/** „OpenMANIPULATOR-X" for `omx_f`; the id itself otherwise. */
export function robotName(robotType) {
  return own(COPY.robots, robotType) || String(robotType || '–');
}

/** „H.264" for `h264`/`libx264`, „H.265", „AV1"; else the id upper-cased. */
export function codecName(codec) {
  return own(COPY.codecs, codec) || String(codec || '–').toUpperCase();
}

/** „Gelenk 2 · Schulter" for `joint2`; the name itself otherwise. */
export function jointLabel(name) {
  return own(COPY.joints, name) || String(name || '–');
}

/** The short form a sentence names a joint by: „Gelenk 2" (the part before „ · "). */
export function jointShort(name) {
  return jointLabel(name).split(' · ')[0];
}

/** The gripper column is the LAST one when it is named `gripper*` (spec §F6, never hard-coded). */
export function hasGripper(names) {
  const list = Array.isArray(names) ? names : [];
  return list.length > 0 && /^gripper/i.test(String(list[list.length - 1]));
}

/** A camera key's last part: `observation.images.gripper` → `gripper`. */
export function cameraRole(camera) {
  const name = camera && (camera.name || camera.key);
  const s = String(name || '');
  return s.split('.').pop();
}

/** „Greifer-Kamera", „Szenen-Kamera", or the key's last part. */
export function cameraLabel(camera) {
  const role = cameraRole(camera);
  if (role === 'gripper') return COPY.camera.gripper;
  if (role === 'scene') return COPY.camera.scene;
  return role || '–';
}

const ORDER = { gripper: 0, scene: 1 };

/**
 * The cameras in the mockup's VISUAL order: Greifer, Szene, then the others by
 * key (§F1). Each keeps its `index` (the clip route's camera number).
 */
export function orderCameras(cameras) {
  const list = (Array.isArray(cameras) ? cameras : []).map((c, i) => ({ ...c, index: Number.isInteger(c && c.index) ? c.index : i }));
  return list.sort((a, b) => {
    const ra = ORDER[cameraRole(a)] ?? 2;
    const rb = ORDER[cameraRole(b)] ?? 2;
    if (ra !== rb) return ra - rb;
    return String(a.key || a.name || '').localeCompare(String(b.key || b.name || ''));
  });
}

/** The camera that drives the clock: `scene` when there is one, else the first. */
export function driverCamera(cameras) {
  const ordered = orderCameras(cameras);
  return ordered.find((c) => cameraRole(c) === 'scene') || ordered[0] || null;
}

/** „Greifer- und Szenen-Kamera" (the fetch dialog's camera line), else the labels joined. */
export function camerasPhrase(cameras) {
  const ordered = orderCameras(cameras);
  const roles = ordered.map(cameraRole);
  if (roles.length === 2 && roles[0] === 'gripper' && roles[1] === 'scene') {
    return fill(COPY.fetch.camerasPair, { a: COPY.camera.gripperShort, b: COPY.camera.sceneShort });
  }
  return ordered.map(cameraLabel).join(', ');
}
