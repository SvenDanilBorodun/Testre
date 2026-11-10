/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The Orange Pi's robot login, browser side. Every request is answered by a
// fake fetch that plays the pi-agent's /auth routes and the cloud's ticket
// mint, so the phases are driven by the same wire answers the real agent and
// API give (pi_agent/agent.py, cloud_training_api/app/routes/pi_tickets.py).
// SA-06 (2026-11-13): the Supabase token goes to the CLOUD only; the Pi gets
// a one-time ticket in the body.

import { vi } from 'vitest';
import {
  claimPi,
  endPiSession,
  endPiSessionOnLogout,
  ensurePiSession,
  exchange,
  getPiAuthState,
  noteUnauthorized,
  PI_AUTH_PHASE,
  PiLoginError,
  piLoginOf,
  probe,
  REFRESH_FRACTION,
  resetPiAuthForTests,
  setPiAccessToken,
  setPiResetCode,
  startPiAuth,
} from '../piAuth';

function json(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

/** A fake agent: `routes` maps a path to an answer (or a function of the init). */
function fakeAgent(routes) {
  const calls = [];
  const fetchFn = vi.fn(async (url, init = {}) => {
    const path = url.split('?')[0];
    calls.push({ path, init });
    const answer = routes[path];
    if (typeof answer === 'function') return answer(init, calls);
    if (answer instanceof Error) throw answer;
    return answer || json(404, { ok: false });
  });
  return { fetchFn, calls };
}

const STATE = '/api/system/auth/state';
const SESSION = '/api/system/auth/session';
const CLAIM = '/api/system/auth/claim';
const LOGOUT = '/api/system/auth/logout';
const CLOUD = 'https://cloud.test';
const MINT = `${CLOUD}/pi/tickets`;
const PI_ID = 'q8V0f2Lr3nC1sZtA9xY7wB';

/** The cloud's mint: a fresh ticket per call, named after the token it got. */
function mintOk() {
  let n = 0;
  return (init) => {
    n += 1;
    const token = (init.headers.Authorization || '').replace('Bearer ', '');
    return json(200, { ticket: `ticket-${n}-for-${token}`, expires_in: 60 });
  };
}

function start(fetchFn) {
  startPiAuth({ fetch: fetchFn, cloudApiUrl: CLOUD });
}

const agentCalls = (calls) => calls.filter((c) => c.path.startsWith('/api/system/'));

const flush = async () => {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
};

beforeEach(() => {
  resetPiAuthForTests();
});

afterEach(() => {
  vi.useRealTimers();
  resetPiAuthForTests();
});

describe('piLoginOf', () => {
  it('maps every phase onto what the login gate needs', () => {
    const of = (phase) => piLoginOf({ phase });
    expect(of(PI_AUTH_PHASE.OFF)).toBe('none');
    expect(of(PI_AUTH_PHASE.UNREACHABLE)).toBe('none');
    expect(of(PI_AUTH_PHASE.LEGACY)).toBe('none');
    expect(of(PI_AUTH_PHASE.PROBING)).toBe('pending');
    expect(of(PI_AUTH_PHASE.UNCLAIMED)).toBe('unclaimed');
    for (const p of [PI_AUTH_PHASE.LOCKED, PI_AUTH_PHASE.EXCHANGING,
      PI_AUTH_PHASE.SESSION, PI_AUTH_PHASE.ERROR]) {
      expect(of(p)).toBe('required');
    }
  });
});

describe('the probe', () => {
  it('stays off and asks nothing until started (every non-Pi rig)', async () => {
    const { fetchFn } = fakeAgent({});
    await probe();
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.OFF);
    expect(fetchFn).not.toHaveBeenCalled();
    await expect(ensurePiSession()).resolves.toBeUndefined();
  });

  it('reads an agent from before the login (404) as legacy', async () => {
    const { fetchFn } = fakeAgent({ [STATE]: json(404, { ok: false }) });
    start(fetchFn);
    await flush();
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.LEGACY);
    await expect(ensurePiSession()).rejects.toThrow(PiLoginError);
  });

  it('reads an unclaimed Pi, its name and its code status', async () => {
    const { fetchFn } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: false, session: false,
        hostname: 'edubotics-07', setup_code: 'void' }),
    });
    start(fetchFn);
    await flush();
    const s = getPiAuthState();
    expect(s.phase).toBe(PI_AUTH_PHASE.UNCLAIMED);
    expect(s.hostname).toBe('edubotics-07');
    expect(s.setupCode).toBe('void');
    await expect(ensurePiSession()).rejects.toMatchObject({ reason: 'unclaimed' });
  });

  it('retries with backoff while the agent does not answer', async () => {
    vi.useFakeTimers();
    let n = 0;
    const { fetchFn } = fakeAgent({
      [STATE]: () => {
        n += 1;
        if (n < 3) throw new TypeError('Failed to fetch');
        return json(200, { ok: true, auth: 1, claimed: true, session: false, pi_id: PI_ID });
      },
    });
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.UNREACHABLE);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(2000);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.LOCKED);
  });
});

describe('the exchange', () => {
  const claimedNoSession = () => json(200, { ok: true, auth: 1, claimed: true, session: false, pi_id: PI_ID });

  it('mints a ticket from the cloud with the token, and hands the Pi only the ticket', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: mintOk(),
      [SESSION]: json(200, { ok: true, role: 'student', expires_in: 900 }),
    });
    setPiAccessToken('jwt-1', { resolved: true });
    start(fetchFn);
    await flush();
    await flush();
    await flush();
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(getPiAuthState().role).toBe('student');
    expect(getPiAuthState().piId).toBe(PI_ID);
    const mint = calls.find((c) => c.path === MINT);
    expect(mint.init.method).toBe('POST');
    expect(mint.init.headers.Authorization).toBe('Bearer jwt-1');
    expect(mint.init.credentials).toBe('omit');
    expect(JSON.parse(mint.init.body)).toEqual({ pi_id: PI_ID });
    const post = calls.find((c) => c.path === SESSION);
    expect(post.init.method).toBe('POST');
    expect(post.init.headers.Authorization).toBeUndefined();
    expect(post.init.credentials).toBe('same-origin');
    expect(JSON.parse(post.init.body)).toEqual({ ticket: 'ticket-1-for-jwt-1' });
    expect(post.init.body).not.toContain('"jwt-1"');
    await expect(ensurePiSession()).resolves.toBeUndefined();
  });

  it('a ticket the Pi refused is replaced once by a fresh one (a blue-green deploy)', async () => {
    let refuse = 1;
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: mintOk(),
      [SESSION]: () => {
        if (refuse > 0) {
          refuse -= 1;
          return json(401, { ok: false, code: 'ticket_rejected', message: 'Abgelehnt (Agent).' });
        }
        return json(200, { ok: true, role: 'student', expires_in: 900 });
      },
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    for (let i = 0; i < 4; i += 1) await flush(); // eslint-disable-line no-await-in-loop
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(calls.filter((c) => c.path === MINT).length).toBe(2);
    expect(calls.filter((c) => c.path === SESSION).map((c) => JSON.parse(c.init.body).ticket))
      .toEqual(['ticket-1-for-jwt-1', 'ticket-2-for-jwt-1']);
  });

  it('without the Pi id nothing is minted and the sentence says to update the Pi', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: false }),
      [MINT]: mintOk(),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await flush();
    await flush();
    expect(calls.some((c) => c.path === MINT)).toBe(false);
    expect(getPiAuthState().error.code).toBe('pi_id_missing');
  });

  it('a cloud the browser cannot reach backs off like one the Pi cannot', async () => {
    const { fetchFn } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: new TypeError('Failed to fetch'),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await flush();
    await flush();
    expect(getPiAuthState().error.code).toBe('cloud_unreachable');
    expect(getPiAuthState().error.message).toMatch(/von diesem Gerät aus/);
    await expect(ensurePiSession()).rejects.toMatchObject({ reason: 'retry_later' });
  });

  it('re-exchanges at 60 % of the session life', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: mintOk(),
      [SESSION]: json(200, { ok: true, role: 'student', expires_in: 100 }),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    const before = calls.filter((c) => c.path === SESSION).length;
    expect(before).toBe(1);
    // 60 % of 100 s, floored at 20 s → 60 s.
    expect(REFRESH_FRACTION).toBe(0.6);
    await vi.advanceTimersByTimeAsync(59000);
    expect(calls.filter((c) => c.path === SESSION).length).toBe(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(calls.filter((c) => c.path === SESSION).length).toBe(2);
  });

  it('re-exchanges at once when Supabase refreshed the token', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: mintOk(),
      [SESSION]: json(200, { ok: true, role: 'student', expires_in: 900 }),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await flush();
    await flush();
    await flush();
    setPiAccessToken('jwt-2');
    await flush();
    await flush();
    const mints = calls.filter((c) => c.path === MINT);
    expect(mints.map((c) => c.init.headers.Authorization)).toEqual(['Bearer jwt-1', 'Bearer jwt-2']);
    expect(calls.filter((c) => c.path === SESSION).map((c) => JSON.parse(c.init.body).ticket))
      .toEqual(['ticket-1-for-jwt-1', 'ticket-2-for-jwt-2']);
  });

  it('shows the agent sentence when the cloud cannot be asked, and keeps retrying', async () => {
    vi.useFakeTimers();
    let answer = json(503, { ok: false, code: 'cloud_unreachable', message: 'Cloud nicht erreichbar (Agent).' });
    const { fetchFn } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: mintOk(),
      [SESSION]: () => answer,
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.ERROR);
    expect(getPiAuthState().error).toEqual({ code: 'cloud_unreachable', message: 'Cloud nicht erreichbar (Agent).' });
    answer = json(200, { ok: true, role: 'student', expires_in: 900 });
    await vi.advanceTimersByTimeAsync(15000);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
  });

  it('without a token the gate refuses instead of opening a socket', async () => {
    const { fetchFn } = fakeAgent({ [STATE]: claimedNoSession() });
    start(fetchFn);
    await flush();
    await expect(ensurePiSession()).rejects.toMatchObject({ reason: 'login_required' });
  });
});

describe('a cookie left by the previous user of this browser', () => {
  it('is ended, never adopted, when nobody is signed in', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: true, role: 'student', expires_in: 600 }),
      [LOGOUT]: json(200, { ok: true }),
    });
    setPiAccessToken(null, { resolved: true });
    start(fetchFn);
    await flush();
    expect(calls.some((c) => c.path === LOGOUT)).toBe(true);
    expect(getPiAuthState().phase).not.toBe(PI_AUTH_PHASE.SESSION);
  });

  it('is kept when the student is signed in', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: true, role: 'student', expires_in: 600 }),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await flush();
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(calls.some((c) => c.path === LOGOUT)).toBe(false);
  });
});

describe('the gate and a 401', () => {
  it('trusts a fresh session without asking again, then re-checks', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: true, role: 'student', expires_in: 900, pi_id: PI_ID }),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    const probes = () => calls.filter((c) => c.path === STATE).length;
    expect(probes()).toBe(1);
    await ensurePiSession();
    expect(probes()).toBe(1);
    await vi.advanceTimersByTimeAsync(11000);
    await ensurePiSession();
    expect(probes()).toBe(2);
  });

  it('a 401 re-checks once (debounced) and re-exchanges a lost session', async () => {
    vi.useFakeTimers();
    let session = true;
    const { fetchFn, calls } = fakeAgent({
      [STATE]: () => json(200, { ok: true, auth: 1, claimed: true, session, role: 'student', expires_in: 900, pi_id: PI_ID }),
      [MINT]: mintOk(),
      [SESSION]: () => {
        session = true;
        return json(200, { ok: true, role: 'student', expires_in: 900 });
      },
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    session = false; // the agent restarted
    noteUnauthorized();
    noteUnauthorized();
    noteUnauthorized();
    await vi.advanceTimersByTimeAsync(600);
    expect(calls.filter((c) => c.path === STATE).length).toBe(2);
    expect(calls.filter((c) => c.path === SESSION).length).toBe(1);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
  });

  it('noteUnauthorized is a no-op off a Pi', async () => {
    vi.useFakeTimers();
    noteUnauthorized();
    await vi.advanceTimersByTimeAsync(1000);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.OFF);
  });
});

describe('claim and sign-out', () => {
  it('a claim sends the code and a ticket, never the token, and yields a session', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: false, session: false, setup_code: 'active', pi_id: PI_ID }),
      [MINT]: mintOk(),
      [CLAIM]: json(200, { ok: true, role: 'teacher', expires_in: 900, message: 'Roboter eingerichtet.' }),
    });
    setPiAccessToken('jwt-t');
    start(fetchFn);
    await flush();
    setPiResetCode('ABCD-EFGH');
    await flush();
    expect(getPiAuthState().resetCode).toBe('ABCD-EFGH');
    const res = await claimPi('abcd-efgh');
    expect(res.ok).toBe(true);
    const post = calls.find((c) => c.path === CLAIM);
    expect(post.init.headers.Authorization).toBeUndefined();
    expect(JSON.parse(post.init.body)).toEqual({ code: 'abcd-efgh', ticket: 'ticket-1-for-jwt-t' });
    expect(calls.find((c) => c.path === MINT).init.headers.Authorization).toBe('Bearer jwt-t');
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(getPiAuthState().resetCode).toBeNull();
  });

  it('a refused claim answers the agent sentence', async () => {
    const { fetchFn } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: false, session: false, pi_id: PI_ID }),
      [MINT]: mintOk(),
      [CLAIM]: json(403, { ok: false, code: 'role_not_allowed', message: 'Nur Lehrkräfte oder Admins …' }),
    });
    setPiAccessToken('jwt-s');
    start(fetchFn);
    await flush();
    const res = await claimPi('ABCD-EFGH');
    expect(res).toEqual({ ok: false, code: 'role_not_allowed', message: 'Nur Lehrkräfte oder Admins …' });
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.UNCLAIMED);
  });

  it('ending the session drops it locally at once and posts a keepalive logout', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: true, role: 'student', expires_in: 900, pi_id: PI_ID }),
      [LOGOUT]: () => new Promise(() => {}), // never answers
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await flush();
    endPiSession();
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.LOCKED);
    const post = calls.find((c) => c.path === LOGOUT);
    expect(post.init.keepalive).toBe(true);
    expect(post.init.method).toBe('POST');
  });

  it('endPiSessionOnLogout is a no-op off a Pi and for a legacy agent', async () => {
    await expect(endPiSessionOnLogout()).resolves.toBe(false);
    const { fetchFn, calls } = fakeAgent({ [STATE]: json(404, {}) });
    start(fetchFn);
    await flush();
    await expect(endPiSessionOnLogout()).resolves.toBe(false);
    expect(calls.some((c) => c.path === LOGOUT)).toBe(false);
  });

  it('after the sign-out ended it, a new token is needed for a new session', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: true, role: 'student', expires_in: 900, pi_id: PI_ID }),
      [LOGOUT]: json(200, { ok: true }),
      [MINT]: mintOk(),
      [SESSION]: json(200, { ok: true, role: 'student', expires_in: 900 }),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await flush();
    await endPiSessionOnLogout();
    await expect(exchange()).resolves.toBeTruthy();
    expect(calls.some((c) => c.path === SESSION)).toBe(false);
  });
});

describe('a refused exchange is not repeated (VA-03, 2026-11-12, adapted to the tickets)', () => {
  // The rosbridge reconnect loop calls ensurePiSession() every ≤ 2 s on a Pi,
  // and each exchange costs the cloud a ticket mint (counted against the
  // school's failed-authentication budget, one NAT address for everybody) and
  // a redeem.
  const claimedNoSession = () => json(200, { ok: true, auth: 1, claimed: true, session: false, pi_id: PI_ID });
  const posts = (calls) => calls.filter((c) => c.path === SESSION);
  const mints = (calls) => calls.filter((c) => c.path === MINT);

  async function settle(gate) {
    try {
      await gate();
    } catch (e) {
      return e;
    }
    return null;
  }

  it('a token the cloud rejected is never sent again by the gate, the error retry or a 401', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: () => json(401, { detail: 'Ungültige oder abgelaufene Sitzung.' }),
    });
    setPiAccessToken('jwt-gone');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.ERROR);
    expect(getPiAuthState().error.code).toBe('token_rejected');
    expect(posts(calls).length).toBe(0); // nothing reached the Pi
    const before = mints(calls).length;
    expect(before).toBe(1);
    for (let i = 0; i < 10; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      const err = await settle(ensurePiSession);
      expect(err).toBeInstanceOf(PiLoginError);
      expect(err.reason).toBe('login_required');
    }
    expect(mints(calls).length - before).toBe(0);
    noteUnauthorized();
    await vi.advanceTimersByTimeAsync(60000); // the 15 s error retry and a 401's probe
    expect(mints(calls).length - before).toBe(0);
    setPiAccessToken('jwt-gone'); // the same token again changes nothing
    await vi.advanceTimersByTimeAsync(0);
    expect(mints(calls).length - before).toBe(0);
    expect(getPiAuthState().error.message).toMatch(/EduBotics-Cloud hat die Anmeldung nicht angenommen/);
  });

  it('a ticket refused twice in a row is treated like a rejected token', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: mintOk(),
      [SESSION]: () => json(401, { ok: false, code: 'ticket_rejected', message: 'Abgelehnt (Agent).' }),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(posts(calls).length).toBe(2); // the first ticket and its one replacement
    expect(getPiAuthState().error).toEqual({ code: 'ticket_rejected', message: 'Abgelehnt (Agent).' });
    for (let i = 0; i < 5; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      const err = await settle(ensurePiSession);
      expect(err.reason).toBe('login_required');
    }
    noteUnauthorized();
    await vi.advanceTimersByTimeAsync(60000);
    expect(posts(calls).length).toBe(2);
    expect(mints(calls).length).toBe(2);
  });

  it('a new token is sent once', async () => {
    vi.useFakeTimers();
    let accept = false;
    const mint = mintOk();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: (init) => (accept ? mint(init) : json(401, { detail: 'x' })),
      [SESSION]: json(200, { ok: true, role: 'student', expires_in: 900 }),
    });
    setPiAccessToken('jwt-old');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(mints(calls).length).toBe(1);
    accept = true;
    setPiAccessToken('jwt-new');
    await vi.advanceTimersByTimeAsync(0);
    expect(mints(calls).map((c) => c.init.headers.Authorization))
      .toEqual(['Bearer jwt-old', 'Bearer jwt-new']);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    await ensurePiSession();
    expect(mints(calls).length).toBe(2);
    expect(posts(calls).length).toBe(1);
  });

  it('a new token that is rejected too is sent once, and then not again', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: () => json(401, { detail: 'x' }),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    setPiAccessToken('jwt-2');
    await vi.advanceTimersByTimeAsync(0);
    for (let i = 0; i < 5; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await settle(ensurePiSession);
    }
    await vi.advanceTimersByTimeAsync(60000);
    expect(mints(calls).map((c) => c.init.headers.Authorization))
      .toEqual(['Bearer jwt-1', 'Bearer jwt-2']);
  });

  for (const code of ['rate_limited', 'cloud_unreachable']) {
    it(`after „${code}“ the gate waits 15 s before it asks again`, async () => {
      vi.useFakeTimers();
      let accept = false;
      const status = code === 'rate_limited' ? 429 : 503;
      const { fetchFn, calls } = fakeAgent({
        [STATE]: claimedNoSession(),
        [MINT]: mintOk(),
        [SESSION]: () => (accept
          ? json(200, { ok: true, role: 'student', expires_in: 900 })
          : json(status, { ok: false, code })),
      });
      setPiAccessToken('jwt-1');
      start(fetchFn);
      await vi.advanceTimersByTimeAsync(0);
      expect(getPiAuthState().error.code).toBe(code);
      expect(posts(calls).length).toBe(1);
      for (let i = 0; i < 7; i += 1) {
        // eslint-disable-next-line no-await-in-loop
        const err = await settle(ensurePiSession);
        expect(err).toBeInstanceOf(PiLoginError);
        // eslint-disable-next-line no-await-in-loop
        await vi.advanceTimersByTimeAsync(2000); // the reconnect loop's pace
      }
      noteUnauthorized();
      await vi.advanceTimersByTimeAsync(600);
      expect(posts(calls).length).toBe(1); // 14.6 s: nothing sent
      accept = true;
      await vi.advanceTimersByTimeAsync(400); // the scheduled retry at 15 s
      expect(posts(calls).length).toBe(2);
      expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
      await ensurePiSession();
      expect(posts(calls).length).toBe(2);
    });
  }

  it('after the back-off the gate may ask again', async () => {
    vi.useFakeTimers();
    let n = 0;
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: mintOk(),
      [SESSION]: () => {
        n += 1;
        return n < 3
          ? json(503, { ok: false, code: 'cloud_unreachable' })
          : json(200, { ok: true, role: 'student', expires_in: 900 });
      },
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(15000); // the timer's retry: refused again
    expect(posts(calls).length).toBe(2);
    expect(await settle(ensurePiSession)).toBeInstanceOf(PiLoginError);
    expect(posts(calls).length).toBe(2);
    vi.setSystemTime(Date.now() + 15001);
    expect(await settle(ensurePiSession)).toBeNull();
    expect(posts(calls).length).toBe(3);
  });

  it('the rejected token is kept in memory only', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const { fetchFn } = fakeAgent({
      [STATE]: claimedNoSession(),
      [MINT]: json(401, { detail: 'x' }),
    });
    setPiAccessToken('jwt-secret-shape');
    start(fetchFn);
    await flush();
    await flush();
    await settle(ensurePiSession);
    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });
});

describe('SA-06: no request to the Pi carries the Supabase token', () => {
  it('across a probe, an exchange, a refresh, a claim and a sign-out', async () => {
    vi.useFakeTimers();
    let claimed = false;
    const { fetchFn, calls } = fakeAgent({
      [STATE]: () => json(200, { ok: true, auth: 1, claimed, session: false, pi_id: PI_ID,
        setup_code: 'active' }),
      // An opaque ticket, as the cloud mints it (never derived from the token).
      [MINT]: () => json(200, { ticket: `opaque-ticket-${calls.length}`, expires_in: 60 }),
      [CLAIM]: () => {
        claimed = true;
        return json(200, { ok: true, role: 'teacher', expires_in: 100 });
      },
      [SESSION]: json(200, { ok: true, role: 'teacher', expires_in: 100 }),
      [LOGOUT]: json(200, { ok: true }),
    });
    setPiAccessToken('eyJhbGciOi.secret-token-1.sig');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    await claimPi('ABCD-EFGH');
    await vi.advanceTimersByTimeAsync(61000); // the refresh at 60 %
    setPiAccessToken('eyJhbGciOi.secret-token-2.sig');
    await vi.advanceTimersByTimeAsync(0);
    noteUnauthorized();
    await vi.advanceTimersByTimeAsync(1000);
    await endPiSessionOnLogout();
    const toPi = agentCalls(calls);
    expect(toPi.length).toBeGreaterThanOrEqual(5);
    for (const c of toPi) {
      const headers = (c.init && c.init.headers) || {};
      expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain('authorization');
      expect(String(c.init && c.init.body)).not.toMatch(/secret-token/);
      expect(c.path).not.toMatch(/secret-token/);
    }
    const toCloud = calls.filter((c) => c.path === MINT);
    expect(toCloud.length).toBeGreaterThanOrEqual(3);
    for (const c of toCloud) {
      expect(c.init.headers.Authorization).toMatch(/^Bearer eyJhbGciOi\.secret-token-[12]\.sig$/);
      expect(c.init.credentials).toBe('omit');
    }
    // Every request leaves for exactly one of two places: the Pi or the cloud.
    expect(toPi.length + toCloud.length).toBe(calls.length);
  });
});

describe('VB-04: a factory reset and a re-claim give the Pi a new id', () => {
  const OLD_ID = 'old-pi-id-AAAAAAAAAAAAAA';
  const NEW_ID = 'new-pi-id-BBBBBBBBBBBBBB';

  /** A Pi that is reset and claimed again on cue: its /auth/state then names
   * NEW_ID, and its redeem accepts only a ticket the cloud minted for the id
   * it holds (the cloud binds every ticket to the pi_id it was minted for). */
  function resettablePi() {
    let piId = OLD_ID;
    let session = false;
    const agent = fakeAgent({
      [STATE]: () => json(200, { ok: true, auth: 1, claimed: true, session, pi_id: piId,
        role: session ? 'student' : undefined, expires_in: session ? 900 : undefined }),
      [MINT]: (init) => json(200, { ticket: `ticket-for-${JSON.parse(init.body).pi_id}`, expires_in: 60 }),
      [SESSION]: (init) => {
        const { ticket } = JSON.parse(init.body);
        if (ticket === `ticket-for-${piId}`) {
          session = true;
          return json(200, { ok: true, role: 'student', expires_in: 900 });
        }
        return json(401, { ok: false, code: 'ticket_rejected', message: 'Abgelehnt (Agent).' });
      },
    });
    return {
      ...agent,
      resetAndReclaim() {
        piId = NEW_ID;
        session = false; // a reset revokes every session
      },
    };
  }

  const mintedFor = (calls) => calls.filter((c) => c.path === MINT).map((c) => JSON.parse(c.init.body).pi_id);

  it('a tab holding the old id re-reads the state once and mints for the new id', async () => {
    vi.useFakeTimers();
    const pi = resettablePi();
    setPiAccessToken('jwt-1');
    start(pi.fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(getPiAuthState().piId).toBe(OLD_ID);

    pi.resetAndReclaim(); // during the lesson: a teacher resets the Pi and claims it again
    setPiAccessToken('jwt-2'); // the next Supabase refresh exchanges at once, no probe first
    await vi.advanceTimersByTimeAsync(0);

    const s = getPiAuthState();
    expect(s.phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(s.error).toBeNull();
    expect(s.piId).toBe(NEW_ID);
    // the first session, two refused tickets for the stale id, then ONE for the new id
    expect(mintedFor(pi.calls)).toEqual([OLD_ID, OLD_ID, OLD_ID, NEW_ID]);
    await expect(ensurePiSession()).resolves.toBeUndefined();
  });

  it('the same for the 60 % refresh', async () => {
    vi.useFakeTimers();
    const pi = resettablePi();
    setPiAccessToken('jwt-1');
    start(pi.fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    pi.resetAndReclaim();
    await vi.advanceTimersByTimeAsync(900 * 1000 * REFRESH_FRACTION + 10);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(getPiAuthState().piId).toBe(NEW_ID);
    expect(mintedFor(pi.calls)).toEqual([OLD_ID, OLD_ID, OLD_ID, NEW_ID]);
  });

  it('when the id did not change, the second refusal stands: one re-read, no further mint', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: false, pi_id: PI_ID }),
      [MINT]: mintOk(),
      [SESSION]: () => json(401, { ok: false, code: 'ticket_rejected', message: 'Abgelehnt (Agent).' }),
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().error).toEqual({ code: 'ticket_rejected', message: 'Abgelehnt (Agent).' });
    expect(calls.filter((c) => c.path === MINT).length).toBe(2);
    // the start-up probe and exactly one re-read after the second refusal
    expect(calls.filter((c) => c.path === STATE).length).toBe(2);
  });

  it('a re-read that finds the Pi unclaimed hands over to the claim screen', async () => {
    vi.useFakeTimers();
    let claimed = true;
    let piId = OLD_ID;
    const { fetchFn, calls } = fakeAgent({
      [STATE]: () => json(200, { ok: true, auth: 1, claimed, session: false, pi_id: piId,
        setup_code: claimed ? undefined : 'active' }),
      [MINT]: mintOk(),
      [SESSION]: () => {
        // reset while the exchange was under way, not claimed again yet
        claimed = false;
        piId = NEW_ID;
        return json(401, { ok: false, code: 'ticket_rejected', message: 'Abgelehnt (Agent).' });
      },
    });
    setPiAccessToken('jwt-1');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.UNCLAIMED);
    expect(getPiAuthState().piId).toBe(NEW_ID);
    expect(calls.filter((c) => c.path === MINT).length).toBe(2);
  });

  it('a claim screen holding an old id mints once more for the new one', async () => {
    vi.useFakeTimers();
    let piId = OLD_ID;
    const { fetchFn, calls } = fakeAgent({
      [STATE]: () => json(200, { ok: true, auth: 1, claimed: false, session: false, pi_id: piId,
        setup_code: 'active' }),
      [MINT]: (init) => json(200, { ticket: `ticket-for-${JSON.parse(init.body).pi_id}`, expires_in: 60 }),
      [CLAIM]: (init) => {
        const { ticket } = JSON.parse(init.body);
        if (ticket === `ticket-for-${piId}`) return json(200, { ok: true, role: 'teacher', expires_in: 900 });
        return json(401, { ok: false, code: 'ticket_rejected', message: 'Abgelehnt (Agent).' });
      },
    });
    setPiAccessToken('jwt-teacher');
    start(fetchFn);
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.UNCLAIMED);
    piId = NEW_ID; // reset again while the claim screen was open
    const result = await claimPi('ABCD-EFGH');
    expect(result.ok).toBe(true);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(calls.filter((c) => c.path === MINT).map((c) => JSON.parse(c.init.body).pi_id))
      .toEqual([OLD_ID, OLD_ID, NEW_ID]);
  });
});

describe('VC-10: a Pi whose state flips claimed↔unclaimed cannot make a tab mint in a loop', () => {
  /** The verifier's scenario: /auth/state alternates claimed and unclaimed on
   * every read, and the redeem refuses every ticket. Before the fix the tab
   * looped exchange → re-read (unclaimed) → probe (claimed) → exchange with no
   * pause; under fake timers it ran out of heap, and against a cloud that
   * answers 429 after 30 mints it minted 31 tickets in 5 s. */
  function flippingPi({ cloudLimit = Infinity } = {}) {
    let reads = 0;
    let mints = 0;
    const { fetchFn, calls } = fakeAgent({
      [STATE]: () => {
        reads += 1;
        const claimed = reads % 2 === 1;
        return json(200, { ok: true, auth: 1, claimed, session: false, pi_id: PI_ID,
          setup_code: claimed ? undefined : 'active' });
      },
      [MINT]: () => {
        mints += 1;
        if (mints > cloudLimit) return json(429, { detail: 'Zu viele Anfragen.' });
        return json(200, { ticket: `t-${mints}`, expires_in: 60 });
      },
      [SESSION]: () => json(401, { ok: false, code: 'ticket_rejected', message: 'Abgelehnt (Agent).' }),
    });
    return { fetchFn, mints: () => calls.filter((c) => c.path === MINT).length, calls };
  }

  it('mints at most one chain per pause, however long the Pi keeps flipping', async () => {
    vi.useFakeTimers();
    const pi = flippingPi();
    setPiAccessToken('jwt-1');
    start(pi.fetchFn);
    await vi.advanceTimersByTimeAsync(5000);
    expect(pi.mints()).toBeLessThanOrEqual(2); // one chain: a ticket and its one replacement
    await vi.advanceTimersByTimeAsync(55000);
    // one chain per 15 s pause: at most 2 mints per 15 s, i.e. ≤ 10 in a minute
    expect(pi.mints()).toBeLessThanOrEqual(10);
    expect(pi.mints()).toBeGreaterThan(2); // it keeps trying, slowly
    const stateReads = pi.calls.filter((c) => c.path === STATE).length;
    expect(stateReads).toBeLessThanOrEqual(12);
  });

  it('the gate and a 401 do not cut the pause short', async () => {
    vi.useFakeTimers();
    const pi = flippingPi();
    setPiAccessToken('jwt-1');
    start(pi.fetchFn);
    await vi.advanceTimersByTimeAsync(100);
    const before = pi.mints();
    for (let i = 0; i < 20; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await ensurePiSession().catch(() => {});
      noteUnauthorized();
      // eslint-disable-next-line no-await-in-loop
      await vi.advanceTimersByTimeAsync(600);
    }
    // 12 s of a reconnect loop and 401s: still within the first pause
    expect(pi.mints()).toBe(before);
  });

  it('a new Supabase token is still exchanged at once', async () => {
    vi.useFakeTimers();
    const pi = flippingPi();
    setPiAccessToken('jwt-1');
    start(pi.fetchFn);
    await vi.advanceTimersByTimeAsync(100);
    const before = pi.mints();
    setPiAccessToken('jwt-2');
    await vi.advanceTimersByTimeAsync(0);
    expect(pi.mints()).toBeGreaterThan(before);
  });

  it('against a cloud that rate-limits, the tab stays far below the limit', async () => {
    vi.useFakeTimers();
    const pi = flippingPi({ cloudLimit: 30 });
    setPiAccessToken('jwt-1');
    start(pi.fetchFn);
    await vi.advanceTimersByTimeAsync(60000);
    expect(pi.mints()).toBeLessThanOrEqual(10);
  });
});
