// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The browser half of the robot contract (C4/C8). The robot half is
// cloud_contract.py; robotis_ai_setup/tests/test_cloud_inference_lockstep.py
// compares the two. Pinned here: the names, the key order, and that a malformed
// state is UNKNOWN (null), never a made-up fact.

import * as contract from '../robotContract';
import { parseCloudState, STATE_KEYS } from '../robotContract';
import fs from 'fs';
import path from 'path';

const good = (o = {}) => JSON.stringify({
  v: 1, seq: 7, active: true, phase: 'running', rtt_ms_p50: 110.5, plan_age_ms: 80, gpu_ms_p50: 23,
  hold_s: null, ends_in_s: 1500.2, stale_camera: null, end_text_de: null, ...o,
});

describe('robotContract', () => {
  it('names the services, the topic and the keys', () => {
    expect(contract.START_SERVICE).toBe('/edubotics/cloud_inference/start');
    expect(contract.START_SERVICE_TYPE).toBe('physical_ai_interfaces/srv/StartCloudInference');
    expect(contract.STOP_SERVICE).toBe('/edubotics/cloud_inference/stop');
    expect(contract.STOP_SERVICE_TYPE).toBe('std_srvs/srv/Trigger');
    expect(contract.STATE_TOPIC).toBe('/edubotics/cloud_inference_state');
    expect(contract.STATE_SCHEMA_VERSION).toBe(1);
    expect(contract.STATE_STALE_MS).toBe(6000);
    expect(STATE_KEYS).toEqual(['v', 'seq', 'active', 'phase', 'rtt_ms_p50', 'plan_age_ms', 'gpu_ms_p50', 'hold_s', 'ends_in_s', 'stale_camera', 'end_text_de']);
    expect(contract.PHASES).toEqual(['idle', 'loading', 'running', 'holding', 'ended']);
    expect(contract.START_REQUEST_KEYS).toEqual(['task_info', 'session_token', 'edge_token']);
    expect(Object.isFrozen(STATE_KEYS)).toBe(true);
  });

  it('keeps every constant a plain literal export (the lockstep test reads the text)', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'robotContract.js'), 'utf8');
    for (const name of ['START_SERVICE', 'START_SERVICE_TYPE', 'STOP_SERVICE', 'STOP_SERVICE_TYPE', 'STATE_TOPIC']) {
      expect(src).toMatch(new RegExp(`export const ${name} = '[^']+';`));
    }
    expect(src).toMatch(/export const STATE_SCHEMA_VERSION = 1;/);
    expect(src).toMatch(/export const STATE_STALE_MS = 6000;/);
    expect(src).toMatch(/export const STATE_KEYS = Object\.freeze\(\[[^\]]+\]\);/);
  });

  it('parses a valid state into exactly the keys', () => {
    const s = parseCloudState(good());
    expect(Object.keys(s)).toEqual([...STATE_KEYS]);
    expect(s.rtt_ms_p50).toBe(110.5);
    expect(parseCloudState(good({ active: false, phase: 'ended', end_text_de: 'Cloud-Inferenz gestoppt.' })).end_text_de)
      .toBe('Cloud-Inferenz gestoppt.');
    expect(parseCloudState(good({ phase: 'holding', hold_s: 1.2, stale_camera: 'scene' })).stale_camera).toBe('scene');
  });

  it('returns null for anything malformed, never a made-up fact', () => {
    const bad = [
      undefined, null, '', 'not json', '[]', '7', 'null',
      good({ v: 2 }), good({ seq: '7' }), good({ active: 'yes' }), good({ phase: 'flying' }),
      good({ rtt_ms_p50: -1 }), good({ gpu_ms_p50: 'fast' }), good({ ends_in_s: 7 }).replace('"ends_in_s":7', '"ends_in_s":1e999'),
      good({ stale_camera: 3 }), good({ end_text_de: {} }), good({ hold_s: true }),
    ];
    for (const b of bad) expect(parseCloudState(b)).toBeNull();
    const missing = JSON.parse(good());
    delete missing.plan_age_ms;
    expect(parseCloudState(JSON.stringify(missing))).toBeNull();
    // NaN cannot be JSON; a hand-written payload with it fails JSON.parse
    expect(parseCloudState('{"v":1,"seq":1,"active":true,"phase":"running","rtt_ms_p50":NaN}')).toBeNull();
  });
});
