/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The Orange Pi's robot login, browser side. Every request is answered by a
// fake fetch that plays the pi-agent's /auth routes, so the phases are driven
// by the same wire answers the real agent gives (pi_agent/agent.py).

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
    startPiAuth({ fetch: fetchFn });
    await flush();
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.LEGACY);
    await expect(ensurePiSession()).rejects.toThrow(PiLoginError);
  });

  it('reads an unclaimed Pi, its name and its code status', async () => {
    const { fetchFn } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: false, session: false,
        hostname: 'edubotics-07', setup_code: 'void' }),
    });
    startPiAuth({ fetch: fetchFn });
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
        return json(200, { ok: true, auth: 1, claimed: true, session: false });
      },
    });
    startPiAuth({ fetch: fetchFn });
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.UNREACHABLE);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(2000);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.LOCKED);
  });
});

describe('the exchange', () => {
  const claimedNoSession = () => json(200, { ok: true, auth: 1, claimed: true, session: false });

  it('trades the Supabase token for a session, in the Authorization header only', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [SESSION]: json(200, { ok: true, role: 'student', expires_in: 900 }),
    });
    setPiAccessToken('jwt-1', { resolved: true });
    startPiAuth({ fetch: fetchFn });
    await flush();
    await flush();
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(getPiAuthState().role).toBe('student');
    const post = calls.find((c) => c.path === SESSION);
    expect(post.init.method).toBe('POST');
    expect(post.init.headers.Authorization).toBe('Bearer jwt-1');
    expect(post.init.credentials).toBe('same-origin');
    expect(post.init.body).not.toContain('jwt-1');
    await expect(ensurePiSession()).resolves.toBeUndefined();
  });

  it('re-exchanges at 60 % of the session life', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [SESSION]: json(200, { ok: true, role: 'student', expires_in: 100 }),
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
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
      [SESSION]: json(200, { ok: true, role: 'student', expires_in: 900 }),
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
    await flush();
    await flush();
    setPiAccessToken('jwt-2');
    await flush();
    const posts = calls.filter((c) => c.path === SESSION);
    expect(posts.map((c) => c.init.headers.Authorization)).toEqual(['Bearer jwt-1', 'Bearer jwt-2']);
  });

  it('shows the agent sentence when the cloud cannot be asked, and keeps retrying', async () => {
    vi.useFakeTimers();
    let answer = json(503, { ok: false, code: 'cloud_unreachable', message: 'Cloud nicht erreichbar (Agent).' });
    const { fetchFn } = fakeAgent({
      [STATE]: claimedNoSession(),
      [SESSION]: () => answer,
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.ERROR);
    expect(getPiAuthState().error).toEqual({ code: 'cloud_unreachable', message: 'Cloud nicht erreichbar (Agent).' });
    answer = json(200, { ok: true, role: 'student', expires_in: 900 });
    await vi.advanceTimersByTimeAsync(15000);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
  });

  it('without a token the gate refuses instead of opening a socket', async () => {
    const { fetchFn } = fakeAgent({ [STATE]: claimedNoSession() });
    startPiAuth({ fetch: fetchFn });
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
    startPiAuth({ fetch: fetchFn });
    await flush();
    expect(calls.some((c) => c.path === LOGOUT)).toBe(true);
    expect(getPiAuthState().phase).not.toBe(PI_AUTH_PHASE.SESSION);
  });

  it('is kept when the student is signed in', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: true, role: 'student', expires_in: 600 }),
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
    await flush();
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(calls.some((c) => c.path === LOGOUT)).toBe(false);
  });
});

describe('the gate and a 401', () => {
  it('trusts a fresh session without asking again, then re-checks', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: true, role: 'student', expires_in: 900 }),
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
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
      [STATE]: () => json(200, { ok: true, auth: 1, claimed: true, session, role: 'student', expires_in: 900 }),
      [SESSION]: () => {
        session = true;
        return json(200, { ok: true, role: 'student', expires_in: 900 });
      },
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
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
  it('a claim sends the code and the token and yields a session', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: false, session: false, setup_code: 'active' }),
      [CLAIM]: json(200, { ok: true, role: 'teacher', expires_in: 900, message: 'Roboter eingerichtet.' }),
    });
    setPiAccessToken('jwt-t');
    startPiAuth({ fetch: fetchFn });
    await flush();
    setPiResetCode('ABCD-EFGH');
    await flush();
    expect(getPiAuthState().resetCode).toBe('ABCD-EFGH');
    const res = await claimPi('abcd-efgh');
    expect(res.ok).toBe(true);
    const post = calls.find((c) => c.path === CLAIM);
    expect(post.init.headers.Authorization).toBe('Bearer jwt-t');
    expect(JSON.parse(post.init.body)).toEqual({ code: 'abcd-efgh' });
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    expect(getPiAuthState().resetCode).toBeNull();
  });

  it('a refused claim answers the agent sentence', async () => {
    const { fetchFn } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: false, session: false }),
      [CLAIM]: json(403, { ok: false, code: 'role_not_allowed', message: 'Nur Lehrkräfte oder Admins …' }),
    });
    setPiAccessToken('jwt-s');
    startPiAuth({ fetch: fetchFn });
    await flush();
    const res = await claimPi('ABCD-EFGH');
    expect(res).toEqual({ ok: false, code: 'role_not_allowed', message: 'Nur Lehrkräfte oder Admins …' });
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.UNCLAIMED);
  });

  it('ending the session drops it locally at once and posts a keepalive logout', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: true, role: 'student', expires_in: 900 }),
      [LOGOUT]: () => new Promise(() => {}), // never answers
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
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
    startPiAuth({ fetch: fetchFn });
    await flush();
    await expect(endPiSessionOnLogout()).resolves.toBe(false);
    expect(calls.some((c) => c.path === LOGOUT)).toBe(false);
  });

  it('after the sign-out ended it, a new token is needed for a new session', async () => {
    const { fetchFn, calls } = fakeAgent({
      [STATE]: json(200, { ok: true, auth: 1, claimed: true, session: true, role: 'student', expires_in: 900 }),
      [LOGOUT]: json(200, { ok: true }),
      [SESSION]: json(200, { ok: true, role: 'student', expires_in: 900 }),
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
    await flush();
    await endPiSessionOnLogout();
    await expect(exchange()).resolves.toBeTruthy();
    expect(calls.some((c) => c.path === SESSION)).toBe(false);
  });
});

describe('a refused exchange is not repeated (VA-03, 2026-11-12)', () => {
  // The rosbridge reconnect loop calls ensurePiSession() every ≤ 2 s on a Pi,
  // and each exchange costs the cloud a GET /me that counts against the
  // school's failed-authentication budget (one NAT address for everybody).
  const claimedNoSession = () => json(200, { ok: true, auth: 1, claimed: true, session: false });
  const posts = (calls) => calls.filter((c) => c.path === SESSION);

  async function settle(gate) {
    try {
      await gate();
    } catch (e) {
      return e;
    }
    return null;
  }

  it('a rejected token is never sent again by the gate, the error retry or a 401', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [SESSION]: () => json(401, { ok: false, code: 'token_rejected', message: 'Abgelehnt (Agent).' }),
    });
    setPiAccessToken('jwt-gone');
    startPiAuth({ fetch: fetchFn });
    await vi.advanceTimersByTimeAsync(0);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.ERROR);
    expect(getPiAuthState().error.code).toBe('token_rejected');
    const before = posts(calls).length;
    expect(before).toBe(1);
    for (let i = 0; i < 10; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      const err = await settle(ensurePiSession);
      expect(err).toBeInstanceOf(PiLoginError);
      expect(err.reason).toBe('login_required');
    }
    expect(posts(calls).length - before).toBe(0);
    noteUnauthorized();
    await vi.advanceTimersByTimeAsync(60000); // the 15 s error retry and a 401's probe
    expect(posts(calls).length - before).toBe(0);
    setPiAccessToken('jwt-gone'); // the same token again changes nothing
    await vi.advanceTimersByTimeAsync(0);
    expect(posts(calls).length - before).toBe(0);
    expect(getPiAuthState().error.message).toBe('Abgelehnt (Agent).');
  });

  it('a new token is sent once', async () => {
    vi.useFakeTimers();
    let accept = false;
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [SESSION]: () => (accept
        ? json(200, { ok: true, role: 'student', expires_in: 900 })
        : json(401, { ok: false, code: 'token_rejected' })),
    });
    setPiAccessToken('jwt-old');
    startPiAuth({ fetch: fetchFn });
    await vi.advanceTimersByTimeAsync(0);
    expect(posts(calls).length).toBe(1);
    accept = true;
    setPiAccessToken('jwt-new');
    await vi.advanceTimersByTimeAsync(0);
    expect(posts(calls).map((c) => c.init.headers.Authorization))
      .toEqual(['Bearer jwt-old', 'Bearer jwt-new']);
    expect(getPiAuthState().phase).toBe(PI_AUTH_PHASE.SESSION);
    await ensurePiSession();
    expect(posts(calls).length).toBe(2);
  });

  it('a new token that is rejected too is sent once, and then not again', async () => {
    vi.useFakeTimers();
    const { fetchFn, calls } = fakeAgent({
      [STATE]: claimedNoSession(),
      [SESSION]: () => json(401, { ok: false, code: 'token_rejected' }),
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
    await vi.advanceTimersByTimeAsync(0);
    setPiAccessToken('jwt-2');
    await vi.advanceTimersByTimeAsync(0);
    for (let i = 0; i < 5; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await settle(ensurePiSession);
    }
    await vi.advanceTimersByTimeAsync(60000);
    expect(posts(calls).map((c) => c.init.headers.Authorization))
      .toEqual(['Bearer jwt-1', 'Bearer jwt-2']);
  });

  for (const code of ['rate_limited', 'cloud_unreachable']) {
    it(`after „${code}“ the gate waits 15 s before it asks again`, async () => {
      vi.useFakeTimers();
      let accept = false;
      const status = code === 'rate_limited' ? 429 : 503;
      const { fetchFn, calls } = fakeAgent({
        [STATE]: claimedNoSession(),
        [SESSION]: () => (accept
          ? json(200, { ok: true, role: 'student', expires_in: 900 })
          : json(status, { ok: false, code })),
      });
      setPiAccessToken('jwt-1');
      startPiAuth({ fetch: fetchFn });
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
      [SESSION]: () => {
        n += 1;
        return n < 3
          ? json(503, { ok: false, code: 'cloud_unreachable' })
          : json(200, { ok: true, role: 'student', expires_in: 900 });
      },
    });
    setPiAccessToken('jwt-1');
    startPiAuth({ fetch: fetchFn });
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
      [SESSION]: json(401, { ok: false, code: 'token_rejected' }),
    });
    setPiAccessToken('jwt-secret-shape');
    startPiAuth({ fetch: fetchFn });
    await flush();
    await flush();
    await settle(ensurePiSession);
    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });
});
