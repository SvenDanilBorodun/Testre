/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The pure contract behind every 3D twin's motion. Driven with explicit clocks:
// `arrival` is the browser's performance.now() when a message landed, the stamp
// is the publisher's header.stamp, and frame(now) is one animation frame.

import {
  createJointStateInterpolator,
  stampToMs,
  INTERP_DELAY_MS,
  INTERP_MAX_GAP_MS,
  INTERP_MAX_JOINT_SPEED_RAD_S,
} from '../jointStateInterpolator';

const EPOCH_S = 1_700_000_000;
const msg = (stampMs, pose) => ({
  header: stampMs === null ? undefined : {
    stamp: { sec: EPOCH_S + Math.floor(stampMs / 1000), nanosec: Math.round((stampMs % 1000) * 1e6) },
  },
  name: Object.keys(pose),
  position: Object.values(pose),
});
const j1 = (res) => (res.pose ? res.pose.get('joint1') : undefined);

describe('stampToMs', () => {
  test('ROS 2 {sec, nanosec} and ROS 1 {secs, nsecs}', () => {
    expect(stampToMs({ header: { stamp: { sec: 10, nanosec: 500000000 } } })).toBe(10500);
    expect(stampToMs({ header: { stamp: { secs: 10, nsecs: 250000000 } } })).toBe(10250);
  });

  test('an unset or absent stamp is "no stamp"', () => {
    expect(stampToMs({ header: { stamp: { sec: 0, nanosec: 0 } } })).toBeNull();
    expect(stampToMs({ name: [], position: [] })).toBeNull();
    expect(stampToMs(null)).toBeNull();
  });
});

describe('createJointStateInterpolator', () => {
  test('the constants are the documented ones (UrdfTwin and SimScene both lean on the delay)', () => {
    expect(INTERP_DELAY_MS).toBe(100);
    expect(INTERP_MAX_GAP_MS).toBe(250);
    expect(INTERP_MAX_JOINT_SPEED_RAD_S).toBe(20);
  });

  test('the first sample is shown at once — there is nothing to blend from', () => {
    const it = createJointStateInterpolator();
    it.push(msg(0, { joint1: 0.3 }), 1000);
    expect(j1(it.frame(1000))).toBeCloseTo(0.3, 12);
  });

  test('blends linearly on the STAMP timeline, 100 ms behind the data', () => {
    const it = createJointStateInterpolator();
    // Samples 40 ms apart, delivered with constant latency 7 ms.
    it.push(msg(0, { joint1: 0.0 }), 1007);
    it.push(msg(40, { joint1: 0.4 }), 1047);
    it.push(msg(80, { joint1: 0.8 }), 1087);
    it.push(msg(120, { joint1: 1.2 }), 1127);
    // offset = 1007; render time = now − 1007 − 100.
    expect(j1(it.frame(1127))).toBeCloseTo(0.2, 9); // rt = 20
    expect(j1(it.frame(1147))).toBeCloseTo(0.4, 9); // rt = 40
    expect(j1(it.frame(1177))).toBeCloseTo(0.7, 9); // rt = 70
  });

  test('delivery JITTER does not reach the pose — the stamps set the pace', () => {
    // An evenly stamped stream (clock offset 1000, fastest delivery 7 ms) whose
    // 2nd and 3rd samples were held up by a busy main thread and landed together:
    // arrivals 1007, 1100, 1101, 1127. By arrival time that is a 93 ms stall and
    // then a 1 ms lurch; drawn on the stamps it is an even 0.02 rad per 20 ms.
    const clumped = createJointStateInterpolator();
    clumped.push(msg(0, { joint1: 0.0 }), 1007);
    clumped.push(msg(40, { joint1: 0.4 }), 1100);
    clumped.push(msg(80, { joint1: 0.8 }), 1101);
    clumped.push(msg(120, { joint1: 1.2 }), 1127);
    // offset = arrival − stamp of the FASTEST delivery (the 1st and the 4th).
    expect(clumped.offsetMs).toBe(1007 - EPOCH_S * 1000);
    const even = [1127, 1147, 1167, 1187].map((t) => j1(clumped.frame(t)));
    expect(even).toEqual([0.2, 0.4, 0.6, 0.8].map((v) => expect.closeTo(v, 9)));
  });

  test('holds at the newest sample — never extrapolates', () => {
    const it = createJointStateInterpolator();
    it.push(msg(0, { joint1: 0.0 }), 1000);
    it.push(msg(40, { joint1: 0.4 }), 1040);
    const res = it.frame(5000);
    expect(j1(res)).toBeCloseTo(0.4, 12);
    expect(res.pending).toBe(false);
    expect(j1(it.frame(9000))).toBeCloseTo(0.4, 12);
  });

  test('pending is true exactly while newer data waits to be shown', () => {
    const it = createJointStateInterpolator();
    it.push(msg(0, { joint1: 0.0 }), 1000);
    it.push(msg(40, { joint1: 0.4 }), 1040);
    // offset 1000 → rt = now − 1100.
    expect(it.frame(1060).pending).toBe(true);   // rt = −40: before the data
    expect(it.frame(1100).pending).toBe(true);   // rt = 0: at the first sample
    expect(it.frame(1120).pending).toBe(true);   // rt = 20: between
    expect(it.frame(1140).pending).toBe(false);  // rt = 40: at the newest
  });

  test('a gap wider than the blend limit STEPS at the later sample', () => {
    const it = createJointStateInterpolator();
    it.push(msg(0, { joint1: 0.0 }), 1000);
    it.push(msg(600, { joint1: 0.5 }), 1600);
    // offset = 1000; rt = now − 1100.
    expect(j1(it.frame(1300))).toBeCloseTo(0.0, 12);  // rt 200: hold
    expect(j1(it.frame(1650))).toBeCloseTo(0.0, 12);  // rt 550: still hold
    expect(j1(it.frame(1700))).toBeCloseTo(0.5, 12);  // rt 600: the step
  });

  test('an impossible per-joint jump steps while the other joints still blend', () => {
    const it = createJointStateInterpolator();
    // joint6 wraps +3.1 → −3.1 (a full-circle encoder); joint1 moves normally.
    it.push(msg(0, { joint1: 0.0, joint6: 3.1 }), 1000);
    it.push(msg(40, { joint1: 0.2, joint6: -3.1 }), 1040);
    const mid = it.frame(1120); // rt = 20: halfway
    expect(mid.pose.get('joint1')).toBeCloseTo(0.1, 9);
    expect(mid.pose.get('joint6')).toBeCloseTo(3.1, 12); // held, not swept round
    expect(it.frame(1140).pose.get('joint6')).toBeCloseTo(-3.1, 12);
  });

  test('the speed limit scales with the gap: a fast but real move still blends', () => {
    const it = createJointStateInterpolator();
    // 1.5 rad over 200 ms = 7.5 rad/s — the Edu:1's fastest joints do 7.87.
    it.push(msg(0, { joint2: 0.0 }), 1000);
    it.push(msg(200, { joint2: 1.5 }), 1200);
    expect(it.frame(1200).pose.get('joint2')).toBeCloseTo(0.75, 9); // rt = 100
  });

  test('a teleport published as "the pose again, then the jump" STEPS', () => {
    // The cross-layer contract with physical_ai_server::_publish_sim_teleport.
    // Left as one lone pose 120 ms after a motionless tail, a 1.3 rad reset is
    // under the 20 rad/s limit and BLENDS — the twin sweeps through poses the
    // arm never took. Re-sending the shown pose puts the jump behind a 3 ms gap.
    const blended = createJointStateInterpolator();
    blended.push(msg(0, { joint1: 1.3 }), 1000);
    blended.push(msg(120, { joint1: 0.0 }), 1120);      // the naive reset
    expect(j1(blended.frame(1160))).toBeGreaterThan(0.1);   // mid-sweep
    expect(j1(blended.frame(1160))).toBeLessThan(1.2);

    const stepped = createJointStateInterpolator();
    stepped.push(msg(0, { joint1: 1.3 }), 1000);
    stepped.push(msg(117, { joint1: 1.3 }), 1120);      // the hold, back-dated
    stepped.push(msg(120, { joint1: 0.0 }), 1120);      // the jump
    for (const t of [1160, 1200, 1216]) {
      expect(j1(stepped.frame(t))).toBe(1.3);           // held, never blended
    }
    expect(j1(stepped.frame(1221))).toBe(0.0);          // then the jump itself
  });

  test('a joint missing from a message keeps its last value', () => {
    const it = createJointStateInterpolator();
    it.push(msg(0, { joint1: 0.1, joint2: -0.7 }), 1000);
    it.push(msg(40, { joint1: 0.3 }), 1040);
    const res = it.frame(2000);
    expect(res.pose.get('joint1')).toBeCloseTo(0.3, 12);
    expect(res.pose.get('joint2')).toBeCloseTo(-0.7, 12);
  });

  test('non-finite values and malformed messages are ignored', () => {
    const it = createJointStateInterpolator();
    expect(it.push({ name: ['joint1'], position: [NaN] }, 1000)).toBe(false);
    expect(it.push({ name: 'joint1', position: [1] }, 1000)).toBe(false);
    expect(it.push(null, 1000)).toBe(false);
    expect(it.frame(1000).pose).toBeNull();
    expect(it.push(msg(0, { joint1: 0.2, joint2: Infinity }), 1000)).toBe(true);
    expect(it.frame(1000).pose.has('joint2')).toBe(false);
  });

  test('a stamp that jumps BACKWARDS starts a new timeline instead of sorting in', () => {
    const it = createJointStateInterpolator();
    it.push(msg(10000, { joint1: 0.5 }), 1000);
    it.push(msg(10040, { joint1: 0.6 }), 1040);
    // The node restarted: its clock is 5 s "earlier" than the old stream.
    it.push(msg(5000, { joint1: -0.2 }), 1100);
    expect(it.size).toBe(1);
    expect(j1(it.frame(1100))).toBeCloseTo(-0.2, 12);
  });

  test('a slightly out-of-order sample is dropped — not a reason to throw the buffer away', () => {
    const it = createJointStateInterpolator();
    it.push(msg(0, { joint1: 0.0 }), 1000);
    it.push(msg(40, { joint1: 0.4 }), 1040);
    expect(it.push(msg(30, { joint1: 9.9 }), 1041)).toBe(false);
    expect(it.size).toBe(2);
    expect(j1(it.frame(1120))).toBeCloseTo(0.2, 9); // rt 20: the untouched blend
  });

  test('the render clock never runs backwards when the offset estimate rises', () => {
    // A fast delivery sets a low offset; it ages out of a tiny window and a slow
    // delivery raises the estimate by 180 ms. Unclamped, the render time would
    // jump back from 70 to 21 and the drawn joint would sweep back 0.7 → 0.21.
    const it = createJointStateInterpolator({ offsetWindowMs: 50 });
    it.push(msg(0, { joint1: 0.0 }), 1000);
    it.push(msg(40, { joint1: 0.4 }), 1040);
    it.push(msg(80, { joint1: 0.8 }), 1080);
    expect(j1(it.frame(1170))).toBeCloseTo(0.7, 9);          // rt = 70
    it.push(msg(120, { joint1: 1.2 }), 1300);                // offset 1000 → 1180
    expect(it.offsetMs - (1000 - EPOCH_S * 1000)).toBe(180);
    expect(j1(it.frame(1301))).toBeCloseTo(0.7, 9);          // held, not 0.21
    expect(j1(it.frame(1350))).toBeCloseTo(0.7, 9);          // rt 70 still
    expect(j1(it.frame(1400))).toBeCloseTo(1.2, 9);          // rt 120: the newest
  });

  test('`moving` is true only while the drawn pose is a blend of DIFFERING samples', () => {
    const it = createJointStateInterpolator();
    it.push(msg(0, { joint1: 0.0 }), 1000);
    it.push(msg(40, { joint1: 0.4 }), 1040);
    it.push(msg(80, { joint1: 0.4 }), 1080);                 // the data has stopped
    expect(it.frame(1060).moving).toBe(false);              // before the data
    expect(it.frame(1120).moving).toBe(true);               // blending 0.0 → 0.4
    const resting = it.frame(1160);                         // blending 0.4 → 0.4
    expect(resting.moving).toBe(false);
    expect(resting.pending).toBe(true);                     // …but still pending
    expect(j1(resting)).toBe(0.4);
  });

  test('without stamps it falls back to arrival time (the pre-interpolation behaviour + the delay)', () => {
    const it = createJointStateInterpolator();
    it.push(msg(null, { joint1: 0.0 }), 1000);
    it.push(msg(null, { joint1: 0.4 }), 1040);
    expect(j1(it.frame(1120))).toBeCloseTo(0.2, 9); // rt = 1020
    expect(j1(it.frame(1500))).toBeCloseTo(0.4, 12);
  });

  test('a burst of near-identical stamps is one sample — the newest values win', () => {
    // What an older server image sends: 30 poses inside 0.02 ms.
    const it = createJointStateInterpolator();
    it.push(msg(0, { joint1: 0.0 }), 1000);
    it.push(msg(0.01, { joint1: 0.3 }), 1000.1);
    it.push(msg(0.02, { joint1: 0.9 }), 1000.2);
    expect(it.size).toBe(1);
    expect(j1(it.frame(2000))).toBeCloseTo(0.9, 12);
  });

  test('a background tab cannot grow the buffer without bound', () => {
    const it = createJointStateInterpolator();
    for (let i = 0; i < 1000; i += 1) it.push(msg(i * 33, { joint1: i * 1e-3 }), 1000 + i * 33);
    expect(it.size).toBeLessThanOrEqual(64);
    // …and on return it simply shows the newest.
    expect(j1(it.frame(1000 + 1000 * 33 + 500))).toBeCloseTo(0.999, 9);
  });

  test('delayMs = 0 degrades to "show the newest", the snap-per-message behaviour', () => {
    const it = createJointStateInterpolator({ delayMs: 0 });
    it.push(msg(0, { joint1: 0.0 }), 1000);
    it.push(msg(40, { joint1: 0.4 }), 1040);
    expect(j1(it.frame(1040))).toBeCloseTo(0.4, 12);
  });

  test('version counts accepted samples (the render loop idles on it)', () => {
    const it = createJointStateInterpolator();
    expect(it.version).toBe(0);
    it.push(msg(0, { joint1: 0.0 }), 1000);
    it.push({ name: ['joint1'], position: [NaN] }, 1010);
    expect(it.version).toBe(1);
  });
});
