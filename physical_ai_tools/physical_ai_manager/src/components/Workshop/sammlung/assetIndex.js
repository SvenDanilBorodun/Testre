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
 * The Sammlung asset index: one view model per card (flyout) and per drawer
 * row, built from the block usage, the destination store, the recording list
 * and the last simulator results. Pure — no Blockly import — so the flyout
 * callbacks and the drawer share exactly one set of rules.
 *
 * Chip ORDER is part of the contract (a card shows the first chips first):
 * usage → versions/robot → overridden → unreachable → refused.
 */

import { DE, formatDe } from '../blocks/messages_de';
import { trajectoryMatchesRig } from '../../../utils/trajectoryIdentity';
import { formatMmDe, formatSecondsDe, robotShortLabelDe } from './format';

export const TITLE_MAX_CHARS = 22;
export const VARIABLE_VALUE_MAX_CHARS = 20;

export const ASSET_COLOURS = Object.freeze({
  variable: '#a78bfa',
  recording: '#3b82f6',
  pin: '#f59e0b',
  pose: '#14b8a6',
});

const ELLIPSIS = '…';

function ellipsize(text, max) {
  const s = String(text ?? '');
  return s.length > max ? s.slice(0, max) + ELLIPSIS : s;
}

const chip = (text, level) => ({ text, level });

function usageChip(total) {
  return total > 0 ? chip(formatDe(DE.CHIP_USED, total), 'ok') : chip(DE.CHIP_UNUSED, 'ok');
}

function mapGet(map, key) {
  return map && typeof map.get === 'function' ? map.get(key) : undefined;
}

function resultFor(lastPreviewResult, key) {
  if (!lastPreviewResult || typeof lastPreviewResult !== 'object') return null;
  if (!Object.prototype.hasOwnProperty.call(lastPreviewResult, key)) return null;
  const r = lastPreviewResult[key];
  return r && typeof r === 'object' ? r : null;
}

const isFiniteNumber = (v) => typeof v === 'number' && Number.isFinite(v);

/** `{x, y, z}` when `v` is a plain object with three finite numbers, else null. */
export function pointFromValue(v) {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
  if (![v.x, v.y, v.z].every(isFiniteNumber)) return null;
  return { x: v.x, y: v.y, z: v.z };
}

function timeOf(iso) {
  const t = typeof iso === 'string' ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}

// Newest first by created_at; equal (or unparsable) stamps keep API order.
function newestFirst(items) {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => (timeOf(b.item.created_at) - timeOf(a.item.created_at)) || (a.index - b.index))
    .map(({ item }) => item);
}

function buildRecordings(items, ctx) {
  const groups = new Map();
  for (const item of newestFirst(items)) {
    const name = typeof item.name === 'string' ? item.name : '';
    if (!name) continue;
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(item);
  }
  const out = [];
  for (const [name, versions] of groups) {
    const newest = versions[0];
    const use = mapGet(ctx.usage.replay, name);
    const total = use ? use.enabled + use.disabled : 0;
    const matchesRig = trajectoryMatchesRig(newest.robot_profile, ctx.robotType);
    const chips = [usageChip(total)];
    if (versions.length > 1) chips.push(chip(formatDe(DE.CHIP_VERSIONS, versions.length), 'warn'));
    if (!matchesRig) chips.push(chip(DE.CHIP_OTHER_ROBOT, 'bad'));
    const result = resultFor(ctx.lastPreviewResult, `rec:${newest.id}`);
    if (result && result.status === 'refused') chips.push(chip(DE.CHIP_SIM_REFUSED, 'bad'));
    const duration = isFiniteNumber(newest.duration_s) ? formatSecondsDe(newest.duration_s) : '—';
    const points = isFiniteNumber(newest.point_count) ? newest.point_count : '—';
    out.push({
      assetKind: 'recording',
      assetId: newest.id,
      assetName: name,
      title: ellipsize(name, TITLE_MAX_CHARS),
      meta: formatDe(DE.CARD_RECORDING_META, duration, points),
      chips,
      canPreview: !!ctx.capabilities.preview && matchesRig,
      colour: ASSET_COLOURS.recording,
      robotProfile: newest.robot_profile ?? null,
      matchesRig,
      usage: { enabled: use ? use.enabled : 0, disabled: use ? use.disabled : 0 },
      versions,
    });
  }
  return { recordings: out, names: new Set(groups.keys()) };
}

function buildMissingRecordings(names, ctx) {
  if (ctx.trajectories.status !== 'ready') return [];
  const out = [];
  const replay = ctx.usage.replay instanceof Map ? ctx.usage.replay : new Map();
  for (const [name, use] of replay) {
    if (!use || use.enabled <= 0 || names.has(name)) continue;
    out.push({
      assetKind: 'missingRecording',
      assetId: '',
      assetName: name,
      title: ellipsize(name, TITLE_MAX_CHARS),
      meta: '',
      chips: [
        chip(DE.CHIP_MISSING, 'bad'),
        use.enabled === 1
          ? chip(DE.CHIP_SEARCHED_ONE_RECORDING, 'bad')
          : chip(formatDe(DE.CHIP_SEARCHED_MANY_RECORDING, use.enabled), 'bad'),
      ],
      canPreview: false,
      colour: ASSET_COLOURS.recording,
      blockIds: use.blockIds.slice(),
    });
  }
  return out;
}

const SOURCE_LABELS = {
  camera: 'CARD_SOURCE_CAMERA',
  sim: 'CARD_SOURCE_SIM',
  capture: 'CARD_SOURCE_TOUCH',
};

function sourceLabel(source) {
  return Object.prototype.hasOwnProperty.call(SOURCE_LABELS, source) ? DE[SOURCE_LABELS[source]] : '—';
}

function buildDestinationCard(entry, ctx) {
  const use = mapGet(ctx.usage.refs, entry.name);
  const total = use ? use.enabled + use.disabled : 0;
  const chips = [usageChip(total)];
  if (entry.robot_type && ctx.robotType && entry.robot_type !== ctx.robotType) {
    chips.push(chip(DE.CHIP_OTHER_ROBOT, 'warn'));
  }
  const pin = mapGet(ctx.usage.pinStatements, entry.name);
  const current = mapGet(ctx.usage.currentStatements, entry.name);
  if ((pin && pin.enabled) || (current && current.enabled)) {
    chips.push(chip(DE.CHIP_OVERRIDDEN, 'warn'));
  }
  const result = resultFor(ctx.lastPreviewResult, `dest:${entry.id}`);
  if (result && result.unreachable) chips.push(chip(DE.CHIP_UNREACHABLE, 'warn'));
  if (result && result.status === 'refused') chips.push(chip(DE.CHIP_SIM_REFUSED, 'bad'));
  const isPose = entry.kind === 'pose';
  const meta = isPose
    ? formatDe(DE.CARD_POSE_META, formatMmDe(entry.z),
      robotShortLabelDe(entry.robot_type) || DE.CARD_SOURCE_CAPTURE)
    : formatDe(DE.CARD_PLACE_META, formatMmDe(entry.x), formatMmDe(entry.y), sourceLabel(entry.source));
  return {
    assetKind: isPose ? 'pose' : 'pin',
    assetId: entry.id,
    assetName: entry.name,
    title: ellipsize(entry.name, TITLE_MAX_CHARS),
    meta,
    chips,
    canPreview: !!ctx.capabilities.preview,
    colour: isPose ? ASSET_COLOURS.pose : ASSET_COLOURS.pin,
    usage: { enabled: use ? use.enabled : 0, disabled: use ? use.disabled : 0 },
    entry,
  };
}

function buildProgramPins(ctx) {
  const out = [];
  const pins = ctx.usage.pinStatements instanceof Map ? ctx.usage.pinStatements : new Map();
  for (const [name, pin] of pins) {
    if (!pin || !pin.enabled) continue;
    const pinned = isFiniteNumber(pin.x) && isFiniteNumber(pin.y);
    out.push({
      assetKind: 'programPin',
      assetId: pin.blockId,
      assetName: name,
      title: ellipsize(name, TITLE_MAX_CHARS),
      meta: formatDe(DE.CARD_PROGRAM_PIN_META,
        pinned ? formatMmDe(pin.x) : '—', pinned ? formatMmDe(pin.y) : '—'),
      chips: [],
      canPreview: false,
      colour: ASSET_COLOURS.pin,
    });
  }
  return out;
}

// JSON for the card; a string shows unquoted. Capped so a long list cannot
// widen the card's second line.
/**
 * A variable value as a student reads it: strings unquoted, everything else as
 * JSON, cut at `maxChars` with an ellipsis. The card uses 20, the drawer 200.
 */
export function displayValue(value, maxChars = VARIABLE_VALUE_MAX_CHARS) {
  let text;
  if (typeof value === 'string') text = value;
  else {
    try {
      text = JSON.stringify(value);
    } catch (_) {
      text = undefined;
    }
    if (typeof text !== 'string') text = String(value);
  }
  return ellipsize(text, maxChars);
}

function buildVariables(variables, ctx) {
  const list = (Array.isArray(variables) ? variables : [])
    .filter((v) => v && typeof v.id === 'string' && typeof v.name === 'string')
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, 'de'));
  const values = ctx.variableValues && typeof ctx.variableValues === 'object' ? ctx.variableValues : {};
  return list.map((v) => {
    const own = Object.prototype.hasOwnProperty.call(values, v.name) ? values[v.name] : null;
    const hasValue = !!own && typeof own === 'object' && 'value' in own;
    let meta = DE.CARD_VARIABLE_NO_VALUE;
    if (hasValue) {
      const ts = isFiniteNumber(own.ts) ? own.ts : ctx.now;
      const age = Math.max(0, Math.floor((ctx.now - ts) / 1000));
      meta = formatDe(DE.CARD_VARIABLE_VALUE, displayValue(own.value), age);
    }
    const uses = mapGet(ctx.usage.variableUses, v.id) || 0;
    return {
      assetKind: 'variable',
      assetId: v.id,
      assetName: v.name,
      title: ellipsize(v.name, TITLE_MAX_CHARS),
      meta,
      chips: [usageChip(uses)],
      canPreview: ctx.capabilities.previewVariables === true
        && hasValue && pointFromValue(own.value) !== null,
      colour: ASSET_COLOURS.variable,
    };
  });
}

const EMPTY_USAGE = Object.freeze({
  replay: new Map(),
  refs: new Map(),
  pinStatements: new Map(),
  currentStatements: new Map(),
  variableUses: new Map(),
});

/**
 * @returns {{recordings, missingRecordings, pins, poses, programPins, variables,
 *   counts: {variablen:number, aufnahmen:number, ziele:number, positionen:number}}}
 */
export function buildAssetIndex({
  usage,
  variables,
  destinations,
  trajectories,
  robotType,
  lastPreviewResult,
  variableValues,
  capabilities,
  now,
} = {}) {
  const ctx = {
    usage: { ...EMPTY_USAGE, ...(usage || {}) },
    trajectories: trajectories && typeof trajectories === 'object'
      ? trajectories : { status: 'none', items: [] },
    robotType: typeof robotType === 'string' ? robotType : '',
    lastPreviewResult: lastPreviewResult || {},
    variableValues: variableValues || {},
    capabilities: capabilities || {},
    now: isFiniteNumber(now) ? now : Date.now(),
  };
  const items = Array.isArray(ctx.trajectories.items)
    ? ctx.trajectories.items.filter((it) => it && typeof it === 'object')
    : [];
  const { recordings, names } = buildRecordings(items, ctx);
  const missingRecordings = buildMissingRecordings(names, ctx);
  const entries = (Array.isArray(destinations) ? destinations : [])
    .filter((e) => e && typeof e === 'object' && typeof e.name === 'string');
  const pins = entries.filter((e) => e.kind === 'pin').map((e) => buildDestinationCard(e, ctx));
  const poses = entries.filter((e) => e.kind === 'pose').map((e) => buildDestinationCard(e, ctx));
  const programPins = buildProgramPins(ctx);
  const variableCards = buildVariables(variables, ctx);
  return {
    recordings,
    missingRecordings,
    pins,
    poses,
    programPins,
    variables: variableCards,
    counts: {
      variablen: variableCards.length,
      aufnahmen: recordings.length,
      ziele: pins.length,
      positionen: poses.length,
    },
  };
}
