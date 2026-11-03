// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// A fake of the robot's station session service (station spec v6.13, §5.2,
// §8.1) and of the GUI's Studio-stream lease (§3.3), for the controller tests.
// It runs on the test's (fake) clock: every answer arrives after `latency` ms,
// a downed rosbridge answers nothing (the caller's timeout fires), the GUI
// lease expires 10 s after the last `on`. Only what the page can observe is
// modelled; the server's own guarantees are WP-B's tests.

export const LEASE_MS = 10000;
export const KEEPALIVE_LOSS_MS = 10000;
export const TAKEOVER_SILENT_MS = 4000;

let seq = 0;

export function createFakeStation() {
  const fake = {
    // knobs
    latency: 20,
    guiLatency: 20,
    rosUp: true,
    guiUp: true,
    guiRefuse: null, // {status, message}
    configured: true,
    leaderActive: false,
    otherModeActive: false,
    retorquePending: false,
    workflowActive: false,
    sourceOk: true,
    oldImage: false,
    checkResults: [], // queued StationCheck camera results; default OK
    // state
    session: null, // {id, owner, generation, last, state, cameraOk, robotOk, sourceOk}
    ended: new Map(), // session_id -> end_reason
    generation: 0,
    epoch: 0,
    stream: { on: false, leaseUntil: 0 },
    // logs
    calls: [],
    posts: [],
    beacons: 0,
    inFlightPosts: 0,
    maxInFlightPosts: 0,
  };

  const now = () => Date.now();

  function expire() {
    const s = fake.session;
    if (s && now() - s.last > KEEPALIVE_LOSS_MS) endSession('KEEPALIVE_LOST');
  }

  function endSession(reason) {
    if (!fake.session) return;
    fake.ended.set(fake.session.id, reason);
    fake.session = null;
    fake.epoch += 1;
  }

  function base(extra = {}) {
    const s = fake.session;
    return {
      end_reason: '',
      camera_check_epoch: fake.epoch,
      accepted: false,
      state: s ? s.state : 'IDLE',
      reason_code: '',
      message: '',
      session_id: '',
      generation: s ? s.generation : fake.generation,
      station_configured: fake.configured,
      source_ok: s ? s.sourceOk : false,
      camera_ok: s ? s.cameraOk : false,
      robot_ok: s ? s.robotOk : false,
      verified: s ? (s.sourceOk && s.cameraOk && s.robotOk && s.state === 'ACTIVE') : false,
      workflow_active: fake.workflowActive,
      layout_id: 'OMX-STATION-A',
      calibration_digest: fake.configured ? 'd'.repeat(64) : '',
      station_protocol: 6,
      ...extra,
    };
  }

  function owned(s, clientId) {
    return s && s.owner === clientId;
  }

  fake.session_ = (req) => {
    expire();
    const { command, session_id: sid, client_id: cid } = req;
    const s = fake.session;
    if (command === 'status') {
      return base({ accepted: true, session_id: owned(s, cid) ? s.id : '' });
    }
    if (command === 'enter') {
      if (s && s.owner === cid) {
        s.last = now();
        s.cameraOk = false;
        fake.epoch += 1;
        return base({ accepted: true, reason_code: 'RESUMED', session_id: s.id });
      }
      if (s && now() - s.last <= TAKEOVER_SILENT_MS) {
        return base({ reason_code: 'BUSY_OTHER_TAB', message: 'Roboter Studio ist in einem anderen Fenster geöffnet.' });
      }
      if (s) {
        // takeover
        fake.ended.set(s.id, '');
        seq += 1;
        fake.generation += 1;
        fake.epoch += 1;
        fake.session = { id: `sess-${seq}`, owner: cid, generation: fake.generation, last: now(), state: 'ACTIVE', cameraOk: false, robotOk: false, sourceOk: fake.sourceOk, prevOwner: s.owner };
        return base({ accepted: true, session_id: fake.session.id });
      }
      if (fake.leaderActive) return base({ reason_code: 'LEADER_ACTIVE', message: 'Bitte zuerst den Leader-Arm ausschalten (Schalter in Roboter Studio).' });
      if (fake.retorquePending) return base({ reason_code: 'RETORQUE_PENDING', message: 'Die Einrichtung wird gerade beendet – bitte kurz warten.' });
      if (fake.otherModeActive) return base({ reason_code: 'OTHER_MODE_ACTIVE', message: 'Aufnahme läuft gerade.' });
      seq += 1;
      fake.generation += 1;
      fake.epoch += 1;
      fake.session = { id: `sess-${seq}`, owner: cid, generation: fake.generation, last: now(), state: 'ACTIVE', cameraOk: false, robotOk: false, sourceOk: fake.sourceOk };
      return base({ accepted: true, session_id: fake.session.id });
    }
    if (command === 'keepalive' || command === 'leave') {
      if (!s || s.id !== sid) {
        if (s && s.id !== sid && s.owner !== cid) return base({ reason_code: 'OWNER_MISMATCH' });
        if (s && s.owner === cid && s.id !== sid) return base({ reason_code: 'OWNER_MISMATCH' });
        const reason = fake.ended.get(sid) || '';
        return base({ reason_code: 'SESSION_ENDED', end_reason: reason });
      }
      if (command === 'keepalive') {
        s.last = now();
        s.sourceOk = fake.sourceOk;
        return base({ accepted: true, session_id: s.id });
      }
      if (fake.workflowActive) return base({ reason_code: 'WORKFLOW_ACTIVE', message: 'Stoppe zuerst dein Programm.', session_id: s.id });
      endSession('LEFT');
      return base({ accepted: true });
    }
    return base({ reason_code: '' });
  };

  fake.check_ = (req) => {
    expire();
    const s = fake.session;
    if (!s || s.id !== req.session_id || s.owner !== req.client_id) {
      return { accepted: false, success: false, result: 'NOT_OWNER', message: '' };
    }
    if (req.command === 'camera') {
      const result = fake.checkResults.length ? fake.checkResults.shift() : 'OK';
      if (result === 'OK') s.cameraOk = true;
      return {
        accepted: true, success: result === 'OK', result, message: '', camera_ok: s.cameraOk, robot_ok: s.robotOk,
        verified: s.cameraOk && s.robotOk, generation: s.generation, rms_mm: 1.2, max_mm: 2.4,
      };
    }
    if (req.command === 'robot') {
      s.robotOk = true;
      return { accepted: true, success: true, result: 'OK', message: '', camera_ok: s.cameraOk, robot_ok: true, verified: s.cameraOk, generation: s.generation, dot_id: 2, dot_dxy_mm: 1.1, dot_dz_mm: 0.4 };
    }
    return { accepted: true, success: true, result: 'OK' };
  };

  function answerLater(fn, req, timeoutMs) {
    fake.calls.push(req);
    return new Promise((resolve) => {
      if (fake.oldImage) {
        setTimeout(() => resolve({ ok: false, res: null, timedOut: false, oldImage: true, unreachable: false }), fake.latency);
        return;
      }
      if (!fake.rosUp) {
        setTimeout(() => resolve({ ok: false, res: null, timedOut: true, oldImage: false, unreachable: false }), timeoutMs);
        return;
      }
      setTimeout(() => {
        if (!fake.rosUp) {
          resolve({ ok: false, res: null, timedOut: true, oldImage: false, unreachable: false });
          return;
        }
        resolve({ ok: true, res: fn(req), timedOut: false, oldImage: false, unreachable: false });
      }, Math.min(fake.latency, timeoutMs));
    });
  }

  fake.transport = {
    isRosReady: () => fake.rosUp,
    callSession: (req, timeoutMs) => answerLater(fake.session_, req, timeoutMs),
    callCheck: (req, timeoutMs) => answerLater(fake.check_, req, timeoutMs),
    callCommission: (req, timeoutMs) => answerLater(() => ({ accepted: true, success: true, result: 'OK' }), req, timeoutMs),
    postStream: (on) => {
      fake.inFlightPosts += 1;
      fake.maxInFlightPosts = Math.max(fake.maxInFlightPosts, fake.inFlightPosts);
      fake.posts.push({ on: Boolean(on), at: now() });
      return new Promise((resolve) => {
        setTimeout(() => {
          fake.inFlightPosts -= 1;
          if (!fake.guiUp) {
            resolve({ ok: false, status: 0, body: {}, unreachable: true });
            return;
          }
          if (on && fake.guiRefuse) {
            resolve({ ok: false, status: fake.guiRefuse.status, body: { message: fake.guiRefuse.message }, unreachable: false });
            return;
          }
          if (on) fake.stream = { on: true, leaseUntil: now() + LEASE_MS };
          else fake.stream = { on: false, leaseUntil: 0 };
          resolve({ ok: true, status: 200, body: { ok: true }, unreachable: false });
        }, fake.guiLatency);
      });
    },
    beaconStreamOff: () => {
      fake.beacons += 1;
      fake.stream = { on: false, leaseUntil: 0 };
      return true;
    },
    streamUrl: () => 'http://localhost:8769/station/stream',
  };

  /** The GUI's stream as it stands now (the lease applied). */
  fake.streamOn = () => fake.stream.on && now() < fake.stream.leaseUntil;
  /** A stray `off` from elsewhere (a closing co-owner tab, a late sign-out). */
  fake.strayOff = () => { fake.stream = { on: false, leaseUntil: 0 }; };
  fake.commands = (command) => fake.calls.filter((c) => c.command === command);
  fake.postsOn = () => fake.posts.filter((p) => p.on);
  fake.postsOff = () => fake.posts.filter((p) => !p.on);

  return fake;
}
