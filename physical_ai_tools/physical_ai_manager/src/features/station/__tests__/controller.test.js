// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The camera station controller (station spec v6.13, §9.1) against a fake
// server and a fake GUI lease (fakeStation.js). The transport and the client
// id are mocked at their module boundary; everything else is the real
// controller on the fake clock.

import { createFakeStation } from './fakeStation';
import { STATION_DE, SESSION_END_DE, SESSION_REASON_DE } from '../stationCopy';

const mockFake = vi.hoisted(() => ({ current: null }));
vi.mock('../stationTransport', () => ({
  __esModule: true,
  isRosReady: () => mockFake.current.transport.isRosReady(),
  callSession: (...a) => mockFake.current.transport.callSession(...a),
  callCheck: (...a) => mockFake.current.transport.callCheck(...a),
  callCommission: (...a) => mockFake.current.transport.callCommission(...a),
  postStream: (...a) => mockFake.current.transport.postStream(...a),
  beaconStreamOff: (...a) => mockFake.current.transport.beaconStreamOff(...a),
  streamUrl: () => 'http://localhost:8769/station/stream',
}));

const mockIds = vi.hoisted(() => ({ id: 'tab-A', n: 0 }));
vi.mock('../clientId', () => ({
  __esModule: true,
  getClientId: () => mockIds.id,
  remintClientId: () => {
    mockIds.n += 1;
    mockIds.id = `tab-A-${mockIds.n}`;
    return mockIds.id;
  },
  checkDuplicateTab: () => Promise.resolve(mockIds.id),
}));

// eslint-disable-next-line import/first
import * as controller from '../controller';

const STATION_CAPS = { station_protocol: 6 };
let fake;

function snap() {
  return controller.getSnapshot();
}

async function advance(ms) {
  await vi.advanceTimersByTimeAsync(ms);
}

/** StudentApp's start + a known station rig. */
async function boot({ caps = STATION_CAPS, workshop = false } = {}) {
  controller.start();
  controller.configure({
    stationProtocol: caps ? caps.station_protocol : undefined,
    capabilitiesKnown: Boolean(caps),
    piMode: false,
    piModeResolved: true,
    jetsonConnected: false,
  });
  if (workshop) controller.setWorkshop({ open: true, leaderOn: false, leaderProbed: true });
  await advance(100);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-01T10:00:00Z'));
  controller._resetForTests();
  fake = createFakeStation();
  mockFake.current = fake;
  mockIds.id = 'tab-A';
  mockIds.n = 0;
});

afterEach(() => {
  controller._resetForTests();
  vi.useRealTimers();
});

describe('entering (§9.1 steps 1–2)', () => {
  it('enters by itself when the student opens Roboter Studio, and turns the stream on only after an accepted keepalive', async () => {
    await boot();
    expect(fake.commands('enter')).toHaveLength(0);
    controller.setWorkshop({ open: true, leaderOn: false, leaderProbed: true });
    // The enter answer arrives (20 ms), the first tick sends a keepalive.
    await advance(25);
    expect(snap().held).toBe(true);
    expect(fake.posts).toHaveLength(0); // nothing before the keepalive's answer
    await advance(25);
    expect(fake.commands('keepalive')).toHaveLength(1);
    await advance(30);
    expect(fake.postsOn()).toHaveLength(1);
    expect(fake.streamOn()).toBe(true);
    // Every POST follows an accepted keepalive.
    await advance(10000);
    expect(fake.postsOn().length).toBe(fake.commands('keepalive').length);
  });

  it('a refusal POSTs nothing: BUSY_OTHER_TAB, RETORQUE_PENDING, LEADER_ACTIVE, OTHER_MODE_ACTIVE', async () => {
    for (const knob of ['leaderActive', 'retorquePending', 'otherModeActive']) {
      controller._resetForTests();
      fake = createFakeStation();
      mockFake.current = fake;
      fake[knob] = true;
      // eslint-disable-next-line no-await-in-loop
      await boot({ workshop: true });
      // eslint-disable-next-line no-await-in-loop
      await advance(5000);
      expect({ knob, posts: fake.posts.length, held: snap().held }).toEqual({ knob, posts: 0, held: false });
    }
    // BUSY_OTHER_TAB: another window holds a fresh session.
    controller._resetForTests();
    fake = createFakeStation();
    mockFake.current = fake;
    fake.session_({ command: 'enter', client_id: 'tab-B' });
    await boot({ workshop: true });
    expect(snap().notice).toBe(SESSION_REASON_DE.BUSY_OTHER_TAB);
    expect(fake.posts).toHaveLength(0);
  });

  it('RETORQUE_PENDING retries every 2 s while the tab is open, and stops when it is closed', async () => {
    fake.retorquePending = true;
    await boot({ workshop: true });
    expect(fake.commands('enter')).toHaveLength(1);
    expect(snap().notice).toBe('Die Einrichtung wird gerade beendet – bitte kurz warten.');
    await advance(2050);
    expect(fake.commands('enter')).toHaveLength(2);
    controller.setWorkshop({ open: false });
    await advance(6000);
    expect(fake.commands('enter')).toHaveLength(2);
    controller.setWorkshop({ open: true, leaderOn: false, leaderProbed: true });
    fake.retorquePending = false;
    await advance(100);
    expect(snap().held).toBe(true);
  });

  it('BUSY_OTHER_TAB retries and takes over once the other window has been silent > 4 s', async () => {
    fake.session_({ command: 'enter', client_id: 'tab-B' });
    await boot({ workshop: true });
    expect(snap().held).toBe(false);
    // tab-B keeps alive for 3 s, then goes silent.
    const bId = fake.session.id;
    await advance(1500);
    fake.session_({ command: 'keepalive', session_id: bId, client_id: 'tab-B' });
    await advance(1500);
    fake.session_({ command: 'keepalive', session_id: bId, client_id: 'tab-B' });
    expect(snap().held).toBe(false);
    await advance(6000);
    expect(snap().held).toBe(true);
    expect(fake.session.owner).toBe('tab-A');
    // The old tab now hears OWNER_MISMATCH.
    expect(fake.session_({ command: 'keepalive', session_id: bId, client_id: 'tab-B' }).reason_code).toBe('OWNER_MISMATCH');
  });

  it('waits for the leader to be switched off (D7) and for a switch in progress', async () => {
    await boot();
    controller.setWorkshop({ open: true, leaderOn: true, leaderProbed: true });
    await advance(3000);
    expect(fake.commands('enter')).toHaveLength(0);
    expect(snap().leaderBlocked).toBe(true);
    // The switch runs (busy, then the readiness overlay).
    controller.reportLeaderSwitching('workshop', true);
    controller.setWorkshop({ open: true, leaderOn: false, leaderProbed: true });
    await advance(3000);
    expect(fake.commands('enter')).toHaveLength(0);
    controller.reportLeaderSwitching('workshop', false);
    await advance(50);
    expect(fake.commands('enter')).toHaveLength(1);
    expect(snap().held).toBe(true);
  });

  it('does not enter before the bridge has answered once', async () => {
    await boot();
    controller.setWorkshop({ open: true, leaderOn: false, leaderProbed: false });
    await advance(3000);
    expect(fake.commands('enter')).toHaveLength(0);
    controller.setWorkshop({ open: true, leaderOn: false, leaderProbed: true });
    await advance(50);
    expect(fake.commands('enter')).toHaveLength(1);
  });

  it('a LEADER_ACTIVE refusal re-arms when the leader is switched off', async () => {
    fake.leaderActive = true;
    await boot({ workshop: true });
    expect(snap().held).toBe(false);
    expect(snap().startOffered).toBe(false);
    fake.leaderActive = false;
    controller.setWorkshop({ open: true, leaderOn: true, leaderProbed: true });
    await advance(100);
    expect(fake.commands('enter')).toHaveLength(1);
    controller.setWorkshop({ open: true, leaderOn: false, leaderProbed: true });
    await advance(100);
    expect(snap().held).toBe(true);
  });

  it('is inactive in Pi mode and on a rig without station_protocol 6', async () => {
    controller.start();
    controller.configure({ stationProtocol: 6, capabilitiesKnown: true, piMode: true, piModeResolved: true });
    controller.setWorkshop({ open: true, leaderOn: false, leaderProbed: true });
    await advance(3000);
    expect(fake.calls).toHaveLength(0);
    expect(snap().active).toBe(false);
    controller._resetForTests();
    fake = createFakeStation();
    mockFake.current = fake;
    await boot({ caps: { station_protocol: 5 }, workshop: true });
    await advance(3000);
    expect(fake.commands('enter')).toHaveLength(0);
  });
});

describe('resuming (§9.1 step 1, D10)', () => {
  it('a reload that lands on HOME resumes through status, whatever page is open', async () => {
    fake.session_({ command: 'enter', client_id: 'tab-A' });
    const sid = fake.session.id;
    await advance(5000); // the reload takes 5 s
    await boot({ workshop: false });
    await advance(100);
    expect(fake.commands('status')).toHaveLength(1);
    expect(fake.commands('status')[0].client_id).toBe('tab-A');
    expect(snap().held).toBe(true);
    expect(snap().sessionId).toBe(sid);
    expect(fake.session.id).toBe(sid);
  });

  it('sends the resume status as soon as the local rosbridge connects, before the capabilities', async () => {
    fake.session_({ command: 'enter', client_id: 'tab-A' });
    fake.rosUp = false;
    controller.start();
    controller.configure({ stationProtocol: undefined, capabilitiesKnown: false, piMode: false, piModeResolved: true });
    await advance(2000);
    expect(fake.commands('status')).toHaveLength(0);
    fake.rosUp = true;
    await advance(600);
    expect(fake.commands('status')).toHaveLength(1);
    await advance(100);
    // Resumed before the capabilities arrived.
    expect(fake.commands('enter')).toHaveLength(1);
    expect(fake.session.owner).toBe('tab-A');
  });

  it('ignores the resume status of an older server (no such service)', async () => {
    fake.oldImage = true;
    await boot();
    await advance(3000);
    expect(fake.commands('status')).toHaveLength(1);
    expect(snap().held).toBe(false);
  });
});

describe('the app-wide timer (§9.1 step 3, D13)', () => {
  async function entered() {
    await boot({ workshop: true });
    await advance(100);
    expect(snap().held).toBe(true);
  }

  it('keeps the session through a tab switch', async () => {
    await entered();
    controller.setWorkshop({ open: false });
    await advance(30000);
    expect(snap().held).toBe(true);
    expect(fake.session && fake.session.owner).toBe('tab-A');
    expect(fake.streamOn()).toBe(true);
  });

  it('SESSION_ENDED stops the timer, POSTs off and says why', async () => {
    await entered();
    fake.ended.set(fake.session.id, 'NATIVE_TIMEOUT');
    fake.session = null;
    const keepalives = fake.commands('keepalive').length;
    await advance(2100);
    expect(snap().held).toBe(false);
    expect(snap().notice).toBe(SESSION_END_DE.NATIVE_TIMEOUT);
    expect(snap().startOffered).toBe(true);
    expect(fake.posts[fake.posts.length - 1].on).toBe(false);
    await advance(10000);
    expect(fake.commands('keepalive').length).toBe(keepalives + 1);
  });

  it('OWNER_MISMATCH stops the timer and POSTs nothing', async () => {
    await entered();
    // tab-B takes over after tab-A went quiet (a frozen tab).
    fake.rosUp = false;
    await advance(4500);
    fake.rosUp = true;
    fake.session_({ command: 'enter', client_id: 'tab-B' });
    const postsBefore = fake.posts.length;
    await advance(2100);
    expect(snap().held).toBe(false);
    expect(fake.posts.length).toBe(postsBefore);
    expect(snap().notice).toBe(SESSION_REASON_DE.OWNER_MISMATCH);
  });

  it('no answer from the server POSTs nothing and says the station is ending; an answer within 10 s heals', async () => {
    await entered();
    fake.rosUp = false;
    const postsBefore = fake.posts.length;
    await advance(6000);
    expect(fake.posts.length).toBe(postsBefore);
    expect(snap().notice).toBe(STATION_DE.linkLost);
    fake.rosUp = true;
    await advance(2100);
    expect(snap().held).toBe(true);
    expect(snap().notice).toBe('');
    expect(fake.streamOn()).toBe(true);
  });

  it('an outage longer than 10 s ends the session', async () => {
    await entered();
    fake.rosUp = false;
    await advance(11000);
    fake.rosUp = true;
    await advance(2100);
    expect(snap().held).toBe(false);
    expect(snap().notice).toBe(SESSION_END_DE.KEEPALIVE_LOST);
  });

  it('a failed GUI POST says the EduBotics program is unreachable', async () => {
    await entered();
    fake.guiUp = false;
    await advance(2100);
    expect(snap().streamError).toBe(STATION_DE.guiUnreachable);
    fake.guiUp = true;
    await advance(2100);
    expect(snap().streamError).toBe('');
  });

  it('a GUI refusal of `on` shows the GUI sentence and the next tick tries again', async () => {
    await entered();
    fake.guiRefuse = { status: 409, message: 'Leader-Umschaltung läuft.' };
    await advance(2100);
    expect(snap().streamError).toBe('Leader-Umschaltung läuft.');
    fake.guiRefuse = null;
    await advance(2100);
    expect(snap().streamError).toBe('');
    expect(fake.streamOn()).toBe(true);
  });

  it('skips a tick whose previous keepalive is still pending', async () => {
    await entered();
    fake.latency = 1400; // under the 1.5 s timeout, over nothing else
    const before = fake.commands('keepalive').length;
    await advance(2000); // tick at +2 s sends one
    await advance(1000);
    // the next tick at +4 s: the first answered at +3.4 s → not pending
    fake.latency = 1499;
    await advance(4000);
    // No two keepalives ever outstanding at the same time.
    const ka = fake.commands('keepalive').length - before;
    expect(ka).toBeLessThanOrEqual(3);
  });

  it('never overlaps two keepalive calls', async () => {
    await entered();
    let outstanding = 0;
    let maxOutstanding = 0;
    const orig = fake.transport.callSession;
    fake.transport.callSession = (req, t) => {
      if (req.command !== 'keepalive') return orig(req, t);
      outstanding += 1;
      maxOutstanding = Math.max(maxOutstanding, outstanding);
      return new Promise((resolve) => {
        // answers after 3 s: past the 1.5 s timeout of the controller's own call
        setTimeout(() => { outstanding -= 1; resolve({ ok: true, res: fake.session_(req) }); }, 3000);
      });
    };
    await advance(12000);
    expect(maxOutstanding).toBe(1);
  });

  it('a stray `off` at any phase of the 2 s cycle heals within one tick', async () => {
    await entered();
    for (let phase = 0; phase < 2000; phase += 137) {
      // eslint-disable-next-line no-await-in-loop
      await advance(phase % 400);
      fake.strayOff();
      // eslint-disable-next-line no-await-in-loop
      await advance(2000 + 100);
      expect({ phase, on: fake.streamOn() }).toEqual({ phase, on: true });
    }
  });

  it('serializes the GUI POSTs (one in flight at a time)', async () => {
    await entered();
    fake.guiLatency = 1900;
    await advance(20000);
    expect(fake.maxInFlightPosts).toBe(1);
  });

  it('a claimed Jetson deactivates the controller: timer stopped, stream off', async () => {
    await entered();
    controller.configure({ stationProtocol: 6, capabilitiesKnown: true, piMode: false, piModeResolved: true, jetsonConnected: true });
    await advance(100);
    expect(snap().active).toBe(false);
    expect(snap().held).toBe(false);
    expect(fake.posts[fake.posts.length - 1].on).toBe(false);
    const ka = fake.commands('keepalive').length;
    await advance(10000);
    expect(fake.commands('keepalive').length).toBe(ka);
  });
});

describe('leaving (§9.1 step 4)', () => {
  async function entered() {
    await boot({ workshop: true });
    await advance(100);
    expect(snap().held).toBe(true);
  }

  it('„Station verlassen": leave, then off; no automatic re-entry until the next visit', async () => {
    await entered();
    const res = await (async () => {
      const p = controller.leave();
      await advance(200);
      return p;
    })();
    expect(res.left).toBe(true);
    expect(fake.session).toBe(null);
    expect(fake.posts[fake.posts.length - 1].on).toBe(false);
    expect(snap().startOffered).toBe(true);
    await advance(10000);
    expect(fake.commands('enter')).toHaveLength(1);
    // The next visit of the tab enters again.
    controller.setWorkshop({ open: false });
    controller.setWorkshop({ open: true, leaderOn: false, leaderProbed: true });
    await advance(100);
    expect(fake.commands('enter')).toHaveLength(2);
    expect(snap().held).toBe(true);
  });

  it('refused with WORKFLOW_ACTIVE while a real program runs: the timer restarts', async () => {
    await entered();
    fake.workflowActive = true;
    await advance(2100);
    expect(snap().workflowActive).toBe(true);
    const p = controller.leave();
    await advance(200);
    const res = await p;
    expect(res.left).toBe(false);
    expect(res.message).toBe(STATION_DE.stopProgramFirst);
    expect(snap().held).toBe(true);
    const ka = fake.commands('keepalive').length;
    await advance(4100);
    expect(fake.commands('keepalive').length).toBeGreaterThan(ka);
    expect(fake.postsOff()).toHaveLength(0);
  });

  it('a leave answered SESSION_ENDED still POSTs off', async () => {
    await entered();
    const sid = fake.session.id;
    fake.ended.set(sid, 'KEEPALIVE_LOST');
    fake.session = null;
    const p = controller.leave();
    await advance(200);
    expect((await p).left).toBe(true);
    expect(fake.postsOff()).toHaveLength(1);
  });

  it('a leave answered OWNER_MISMATCH or not at all POSTs nothing', async () => {
    await entered();
    fake.rosUp = false;
    const p = controller.leave();
    await advance(9000);
    expect((await p).left).toBe(true);
    expect(fake.postsOff()).toHaveLength(0);
  });

  it('never POSTs a late `on` after the leave: a keepalive answered after the leave changes nothing', async () => {
    await entered();
    // Hold the next keepalive's answer back until the leave is over.
    let release = null;
    const orig = fake.transport.callSession;
    fake.transport.callSession = (req, t) => {
      if (req.command === 'keepalive' && !release) {
        return new Promise((resolve) => {
          release = () => resolve({ ok: true, res: fake.session_({ ...req }) });
        });
      }
      return orig(req, t);
    };
    await advance(2100); // that keepalive is now in flight (and stays)
    expect(release).not.toBe(null);
    // The answer was computed while the session lived: accepted.
    const accepted = fake.session_({ command: 'keepalive', session_id: snap().sessionId, client_id: 'tab-A' });
    expect(accepted.accepted).toBe(true);
    fake.transport.callSession = (req, t) => (req.command === 'keepalive'
      ? new Promise((resolve) => { release = () => resolve({ ok: true, res: accepted }); })
      : orig(req, t));
    const p = controller.leave();
    await advance(500);
    expect((await p).left).toBe(true);
    const postsAtLeave = fake.posts.length;
    expect(fake.posts[postsAtLeave - 1].on).toBe(false);
    release(); // the old keepalive answers „accepted" only now
    await advance(5000);
    expect(fake.posts.length).toBe(postsAtLeave);
    expect(fake.streamOn()).toBe(false);
  });

  it('a queued `on` behind a slow POST is dropped once the leave has begun', async () => {
    await entered();
    fake.guiLatency = 2500; // POSTs pile up: the next tick's `on` waits behind one in flight
    await advance(4100); // tick 2's `on` is in flight, tick 3's `on` is queued behind it
    expect(fake.inFlightPosts).toBe(1);
    expect(fake.commands('keepalive').length).toBeGreaterThanOrEqual(3);
    const p = controller.leave();
    const postsAtLeave = fake.posts.length;
    await advance(8000);
    expect((await p).left).toBe(true);
    // After the leave began: only its `off`, never a queued `on`.
    expect(fake.posts.slice(postsAtLeave).map((x) => x.on)).toEqual([false]);
  });

  it('waits for a GUI POST in flight before the leave and its off', async () => {
    await entered();
    fake.guiLatency = 900;
    await advance(2030); // keepalive answered, an `on` POST in flight
    const order = [];
    const origPost = fake.transport.postStream;
    fake.transport.postStream = (on) => { order.push(`post:${on}`); return origPost(on); };
    const origCall = fake.transport.callSession;
    fake.transport.callSession = (req, t) => { order.push(req.command); return origCall(req, t); };
    const p = controller.leave();
    await advance(3000);
    await p;
    expect(order[0]).toBe('leave');
    expect(order).toEqual(['leave', 'post:false']);
    expect(fake.maxInFlightPosts).toBe(1);
  });
});

describe('pagehide (§9.1 step 5)', () => {
  it('only the tab that holds the session sends the beacon, and no leave', async () => {
    await boot({ workshop: true });
    await advance(100);
    window.dispatchEvent(new Event('pagehide'));
    expect(fake.beacons).toBe(1);
    expect(fake.commands('leave')).toHaveLength(0);
  });

  it('a refused or non-owner tab sends no beacon and no off', async () => {
    fake.session_({ command: 'enter', client_id: 'tab-B' });
    await boot({ workshop: true });
    window.dispatchEvent(new Event('pagehide'));
    expect(fake.beacons).toBe(0);
    expect(fake.posts).toHaveLength(0);
  });
});

describe('sign-out (§9.1 step 4)', () => {
  it('starts the leave, chains off on its answer, and re-mints the client id', async () => {
    await boot({ workshop: true });
    await advance(100);
    controller.endStationSessionOnLogout();
    expect(snap().held).toBe(false);
    expect(mockIds.id).toBe('tab-A-1');
    expect(fake.commands('leave')).toHaveLength(1);
    expect(fake.commands('leave')[0].client_id).toBe('tab-A');
    await advance(200);
    expect(fake.postsOff()).toHaveLength(1);
    const ka = fake.commands('keepalive').length;
    await advance(10000);
    expect(fake.commands('keepalive').length).toBe(ka);
  });

  it('a tab without the session only re-mints its id', async () => {
    await boot();
    controller.endStationSessionOnLogout();
    await advance(500);
    expect(fake.commands('leave')).toHaveLength(0);
    expect(fake.posts).toHaveLength(0);
    expect(mockIds.id).toBe('tab-A-1');
  });
});

describe('the automatic camera check (§7.1)', () => {
  it('runs once per (session_id, epoch); NO_FRAME, WRONG_SIZE and BUSY do not use up the occasion', async () => {
    fake.checkResults = ['NO_FRAME', 'WRONG_SIZE', 'BUSY', 'MOVED'];
    await boot({ workshop: true });
    await advance(10000);
    const cams = fake.calls.filter((c) => c.command === 'camera');
    expect(cams).toHaveLength(4);
    expect(snap().cameraCheck.result).toBe('MOVED');
    await advance(10000);
    expect(fake.calls.filter((c) => c.command === 'camera')).toHaveLength(4);
    // „Kamera prüfen" repeats it on demand.
    const p = controller.runCameraCheck();
    await advance(100);
    await p;
    expect(snap().cameraOk).toBe(true);
  });

  it('runs again after a new epoch (a resume), and after a server respawn with the epoch back at 0', async () => {
    await boot({ workshop: true });
    await advance(3000);
    expect(fake.calls.filter((c) => c.command === 'camera')).toHaveLength(1);
    // A stream gap revokes the camera: a new epoch.
    fake.session.cameraOk = false;
    fake.epoch += 1;
    await advance(2100);
    expect(fake.calls.filter((c) => c.command === 'camera')).toHaveLength(2);
    // The server node respawns: the session is gone, the counter restarts.
    fake.ended.set(fake.session.id, '');
    fake.session = null;
    fake.epoch = 0;
    await advance(2100);
    expect(snap().held).toBe(false);
    controller.startSession();
    await advance(3000);
    expect(fake.calls.filter((c) => c.command === 'camera')).toHaveLength(3);
  });

  it('does not run on an unconfigured station', async () => {
    fake.configured = false;
    await boot({ workshop: true });
    await advance(6000);
    expect(fake.calls.filter((c) => c.command === 'camera')).toHaveLength(0);
    expect(snap().stationConfigured).toBe(false);
  });
});

describe('the re-solve job (§7.3)', () => {
  it('polls StationCheck status every 2 s until the job ends, then shows the result', async () => {
    await boot({ workshop: true });
    await advance(3000);
    let polls = 0;
    const orig = fake.transport.callCheck;
    fake.transport.callCheck = (req, t) => {
      if (req.command === 'resolve_camera') {
        return Promise.resolve({ ok: true, res: { accepted: true, success: true, result: 'QUEUED', job_id: 7 } });
      }
      if (req.command === 'status') {
        polls += 1;
        const done = polls >= 3;
        return Promise.resolve({
          ok: true,
          res: {
            accepted: true, success: done, result: done ? 'OK' : '', job_id: 7, job_state: done ? 'done' : 'running',
            camera_ok: done, robot_ok: false, verified: false, message: '',
          },
        });
      }
      return orig(req, t);
    };
    await controller.resolveCamera();
    expect(snap().resolveJob.jobId).toBe(7);
    await advance(2100);
    expect(snap().resolveJob.jobState).toBe('running');
    await advance(4200);
    expect(snap().resolveJob.jobState).toBe('done');
    expect(snap().checking).toBe('');
    await advance(6000);
    expect(polls).toBe(3);
  });
});
