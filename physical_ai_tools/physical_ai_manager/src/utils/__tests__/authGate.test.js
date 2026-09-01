/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The student login gate. A German school runs EduBotics on Windows student PCs
// under ONE shared Windows account; „Umgebung starten" spawns a fresh WebView2
// window whose `?fresh=` nonce makes utils/bootScrub wipe the previous student's
// Supabase session before any module reads storage. Until this gate the student
// could then jog, hand-guide, program in Roboter Studio and RECORD entirely
// logged out — only Training and Inferenz asked for a session.
//
// Three properties are pinned, and they fail for different reasons:
//   1. the CARVE-OUT — the Orange Pi's System tab is never gated. It is the
//      Pi's whole setup wizard AND its only repair surface, and the Pi boots
//      with the robot tier down BY DESIGN, so a gate over it is a brick. The
//      carve-out must be exactly that page and exactly that mode — too wide and
//      the Pi is ungated, too narrow and Windows leaks a bypass.
//   2. the ESCAPE PREDICATE — an escape offered on a WRONG PASSWORD is not an
//      escape, it is a bypass button, and identity is the thing being enforced.
//      Only "we could not ask" qualifies.
//   3. the WIRING — a pure function stays perfectly green while its call site
//      stops asking it the right question. StudentApp.js is therefore fenced as
//      SOURCE TEXT, the idiom sessionScope.test.js uses.

import fs from 'node:fs';
import path from 'node:path';
import PageType from '../../constants/pageType';
import { studentAuthGateDecision, isCloudUnreachableAuthError } from '../authGate';

const STUDENT_APP = path.resolve(__dirname, '..', '..', 'StudentApp.js');
const appSrc = fs.readFileSync(STUDENT_APP, 'utf8');

/** Signed out, bootstrap finished, on a Windows rig, no escape taken. */
function base(overrides = {}) {
  return {
    isAuthLoading: false,
    isAuthenticated: false,
    offlineOverride: false,
    piMode: false,
    page: PageType.HOME,
    ...overrides,
  };
}

const EVERY_PAGE = Object.values(PageType);

describe('studentAuthGateDecision', () => {
  it('never traps an Orange Pi on its System tab', () => {
    // docker-compose.opi.yml brings the manager up with the robot tier DOWN,
    // and the student reaches „Umgebung starten" only through this page. The
    // Netzwerk-Check that lives here is also the tool that diagnoses why the
    // login itself failed.
    expect(
      studentAuthGateDecision(
        base({ piMode: true, page: PageType.SYSTEM, isAuthenticated: false })
      )
    ).toBe('app');
  });

  it('gates every OTHER page on a Pi', () => {
    // The carve-out is the System tab alone: Aufnahme, Roboter Studio, Daten,
    // Training and Inferenz all require a login on a Pi exactly as on Windows.
    expect(
      studentAuthGateDecision(
        base({ piMode: true, page: PageType.WORKSHOP, isAuthenticated: false })
      )
    ).toBe('login');
    expect(
      studentAuthGateDecision(
        base({ piMode: true, page: PageType.RECORD, isAuthenticated: false })
      )
    ).toBe('login');
  });

  it('does not leak the carve-out to a Windows rig', () => {
    // Windows has no System tab at all (it is piOnly), so a page-only condition
    // would be a bypass reachable by anything that can set the page.
    expect(
      studentAuthGateDecision(
        base({ piMode: false, page: PageType.SYSTEM, isAuthenticated: false })
      )
    ).toBe('login');
  });

  it('lets a signed-in student through on every page', () => {
    for (const page of EVERY_PAGE) {
      expect(studentAuthGateDecision(base({ isAuthenticated: true, page }))).toBe('app');
      expect(
        studentAuthGateDecision(base({ isAuthenticated: true, page, piMode: true }))
      ).toBe('app');
    }
  });

  it('lets the offline escape through, without flashing a spinner', () => {
    // offlineOverride sits ABOVE isAuthLoading: a student already working must
    // not be thrown back to „Laden…" when the bootstrap re-runs.
    expect(
      studentAuthGateDecision(base({ isAuthenticated: false, offlineOverride: true }))
    ).toBe('app');
    expect(
      studentAuthGateDecision(
        base({ isAuthenticated: false, offlineOverride: true, isAuthLoading: true })
      )
    ).toBe('app');
  });

  it('shows the spinner only while the session lookup is in flight', () => {
    expect(
      studentAuthGateDecision(base({ isAuthenticated: false, isAuthLoading: true }))
    ).toBe('loading');
    expect(
      studentAuthGateDecision(base({ isAuthenticated: false, isAuthLoading: false }))
    ).toBe('login');
  });
});

describe('isCloudUnreachableAuthError', () => {
  it('is true for the MEASURED unreachable-host shape', () => {
    // Measured against http://192.0.2.1:9999 with @supabase/auth-js 2.103.1:
    // 10514 ms, then AuthRetryableFetchError / "fetch failed" / status 0.
    expect(
      isCloudUnreachableAuthError({
        name: 'AuthRetryableFetchError',
        message: 'fetch failed',
        status: 0,
      })
    ).toBe(true);
  });

  it('is true for a gateway that answered on the network layer only', () => {
    // auth-js's NETWORK_ERROR_CODES — a captive portal or a proxy in front of
    // a Supabase project that is paused or down.
    expect(
      isCloudUnreachableAuthError({ name: 'AuthRetryableFetchError', status: 503 })
    ).toBe(true);
    expect(isCloudUnreachableAuthError({ status: 500 })).toBe(true);
  });

  it('is FALSE when the service judged THIS request', () => {
    // A wrong password is the one state in which refusing is the entire point:
    // the cloud is up, and identity is what the gate enforces. Same for a rate
    // limit — an escape there would just be a retry-free bypass.
    expect(
      isCloudUnreachableAuthError({
        name: 'AuthApiError',
        message: 'Invalid login credentials',
        status: 400,
      })
    ).toBe(false);
    expect(isCloudUnreachableAuthError({ status: 429 })).toBe(false);
    expect(isCloudUnreachableAuthError({ status: 403 })).toBe(false);
  });

  it('is true for the unconfigured-build stub, which throws with NO status', () => {
    // lib/supabaseClient's buildStub() proxy. A manager image built without the
    // Supabase build args must not brick a classroom — BuildConfigBanner is
    // already screaming in red above the gate.
    expect(
      isCloudUnreachableAuthError(
        new Error('Supabase ist in dieser Build-Version nicht konfiguriert.')
      )
    ).toBe(true);
  });

  it('is false for no error at all', () => {
    expect(isCloudUnreachableAuthError(null)).toBe(false);
    expect(isCloudUnreachableAuthError(undefined)).toBe(false);
  });
});

// ── The WIRING, as source text ──────────────────────────────────────────────
describe('StudentApp asks the gate the right question', () => {
  it('feeds studentAuthGateDecision all five inputs', () => {
    // A call site that stops passing `piMode` leaves every assertion above
    // green while bricking every Orange Pi in the room.
    const call = /studentAuthGateDecision\(\{([\s\S]*?)\}\)/.exec(appSrc);
    if (!call) throw new Error('StudentApp does not call studentAuthGateDecision({…})');
    const args = call[1];
    for (const input of [
      'isAuthLoading',
      'isAuthenticated',
      'offlineOverride',
      'piMode',
      'page',
    ]) {
      expect(args).toContain(input);
    }
  });

  it('makes the auth bootstrap TOTAL, so the spinner can always clear', () => {
    // authSlice starts isLoading TRUE and the gate renders „Laden…" for it, so
    // a getSession() that rejects — offline, DNS filtered, a paused project —
    // would hang the whole app on a spinner that can never clear. The original
    // chain had neither .catch nor .finally; the defect was latent only because
    // StudentApp did not read isLoading at all.
    const start = appSrc.indexOf('.getSession()');
    expect(start).toBeGreaterThan(-1);
    const end = appSrc.indexOf('.onAuthStateChange', start);
    expect(end).toBeGreaterThan(start);
    const chain = appSrc.slice(start, end);
    expect(chain).toContain('.catch(');
    expect(chain).toContain('.finally(');
    // …and the clear must be in the .finally, not in the .then, or the error
    // path is exactly the one that hangs.
    expect(chain).toMatch(/\.finally\(\s*\(\)\s*=>\s*dispatch\(setIsLoading\(false\)\)\s*\)/);
  });

  it('survives an UNCONFIGURED build, where supabase.auth throws synchronously', () => {
    // A manager image built with no Supabase build args gets lib/supabaseClient's
    // buildStub() Proxy, whose EVERY property is a function that throws — so
    // `supabase.auth.getSession` throws before any promise exists and there is
    // no .catch to reach it. A throw inside useEffect is uncaught (this app has
    // no ErrorBoundary), so the whole SPA white-screens instead of rendering the
    // gate beside the red BuildConfigBanner. The synchronous try/catch is the
    // only thing that covers it, and its handler MUST clear isLoading too or
    // the gate hangs on „Laden…" for exactly that build.
    const gs = appSrc.indexOf('.getSession()');
    expect(gs).toBeGreaterThan(-1);
    const effectStart = appSrc.lastIndexOf('useEffect(() => {', gs);
    const effectEnd = appSrc.indexOf('}, [dispatch]);', gs);
    expect(effectStart).toBeGreaterThan(-1);
    expect(effectEnd).toBeGreaterThan(gs);
    const effect = appSrc.slice(effectStart, effectEnd);

    // The try must OPEN before the first supabase touch, or it guards nothing.
    const tryAt = effect.indexOf('try {');
    expect(tryAt).toBeGreaterThan(-1);
    expect(tryAt).toBeLessThan(effect.indexOf('supabase.auth'));

    const catchAt = effect.search(/\}\s*catch\s*\(/);
    expect(catchAt).toBeGreaterThan(tryAt);
    expect(effect.slice(catchAt)).toContain('dispatch(setIsLoading(false))');
  });

  it('renders the gate INSIDE <main>, never as a top-level return', () => {
    // CollisionModal — the teleop force/collision e-stop — and PiUpdateGate are
    // siblings of the layout div, OUTSIDE <main>. The ROS node keeps an
    // in-flight task across a window handover, so a recording can be running
    // when a fresh window opens: a full-screen `return <LoginForm/>` would hide
    // the e-stop behind a login form. The rail staying on screen is also what
    // carries a Pi student to the System tab the carve-out exists for.
    // Anchored on the ELEMENTS, never on the words: StudentApp's own comment
    // above <main> narrates this rule in prose, and a fence a comment can
    // satisfy is not a fence.
    const mainOpen = appSrc.indexOf('<main className=');
    const mainClose = appSrc.indexOf('</main>');
    expect(mainOpen).toBeGreaterThan(-1);
    expect(mainClose).toBeGreaterThan(mainOpen);
    const loginForm = appSrc.indexOf('<LoginForm');
    expect(loginForm).toBeGreaterThan(mainOpen);
    expect(loginForm).toBeLessThan(mainClose);
    // The e-stop is still mounted outside it.
    expect(appSrc.indexOf('<CollisionModal />')).toBeGreaterThan(mainClose);
  });
});
