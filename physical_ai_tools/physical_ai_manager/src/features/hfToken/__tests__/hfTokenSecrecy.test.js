// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The one property this feature must never lose: the student's Hugging-Face
// token is never in Redux state, never in an action (payload OR meta), never in
// browser storage, never in a URL, never in a log line.
//
// Two halves, because they catch different mistakes:
//   1. A SOURCE FENCE over the code that touches the token. No console call, no
//      storage API, no createAsyncThunk (whose `pending` action carries the
//      thunk argument in `meta.arg`). Comment lines are dropped first: this
//      file's neighbours explain WHY those are banned in prose.
//   2. A BEHAVIOURAL test. A full save → push flow runs on the real reducers
//      behind a recording middleware, and the JSON of the final state and of
//      EVERY dispatched action is searched for the sentinel token (whole, and
//      two inner slices of it, so a truncated copy is caught too).

import fs from 'node:fs';
import path from 'node:path';
import { configureStore } from '@reduxjs/toolkit';

import authReducer, { setSession } from '../../auth/authSlice';
import uiReducer from '../../ui/uiSlice';
import hfReducer from '../hfTokenSlice';
import { signedOut } from '../../session/sessionActions';
import * as api from '../../../services/hfTokenApi';
import { setRobotToken } from '../robotChannel';
import {
  pushHfTokenToRobot,
  refreshHfTokenAccount,
  removeHfToken,
  saveHfToken,
  verifyHfToken,
} from '../hfTokenThunks';

vi.mock('../../../services/hfTokenApi', () => ({
  __esModule: true,
  getHfToken: vi.fn(),
  putHfToken: vi.fn(),
  deleteHfToken: vi.fn(),
  revealHfToken: vi.fn(),
  verifyHfToken: vi.fn(),
}));
vi.mock('../robotChannel', () => ({
  __esModule: true,
  setRobotToken: vi.fn(),
}));

const SRC = path.resolve(__dirname, '..', '..', '..');

// ── 1. the source fence ──────────────────────────────────────────────────────
// Everything in the token's path. B-2 adds hooks/useHfTokenSync.js and
// components/Home/HfTokenCard.jsx to this list when it creates them.
const FENCED_FILES = [
  'services/hfTokenApi.js',
];
const FENCED_DIRS = [
  'features/hfToken',
];
const MIN_FILES_IN_DIR = 5;

const COMMENT_LINE_RE = /^\s*(?:\/\/|\*|\/\*)/;
const stripComments = (text) => text
  .split('\n')
  .filter((ln) => !COMMENT_LINE_RE.test(ln))
  .join('\n');

function listSources(dirRel) {
  const out = [];
  const walk = (abs) => {
    for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
      const p = path.join(abs, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== '__tests__') walk(p);
      } else if (/\.jsx?$/.test(entry.name)) {
        out.push(path.relative(SRC, p).split(path.sep).join('/'));
      }
    }
  };
  walk(path.join(SRC, dirRel));
  return out;
}

const dirFiles = FENCED_DIRS.flatMap(listSources);
const FILES = [...new Set([...FENCED_FILES, ...dirFiles])].sort();
const read = (rel) => stripComments(fs.readFileSync(path.join(SRC, rel), 'utf8'));

const FORBIDDEN = [
  [/\bconsole\s*\./, 'a console call'],
  [/\blocalStorage\b|\bsessionStorage\b/, 'Web Storage'],
  [/\bidb(?:Get|Set|Del)\b|\bindexedDB\b/, 'IndexedDB'],
  [/\bcreateAsyncThunk\b/, 'createAsyncThunk (its pending action carries meta.arg)'],
  [/\bdocument\s*\.\s*cookie\b/, 'a cookie'],
  [/\blocation\s*\.\s*(?:search|hash|href)\s*=/, 'writing the token into the URL'],
  [/\bhistory\s*\.\s*(?:push|replace)State\b/, 'history state'],
];

describe('the token\'s code path has no way to leak it', () => {
  it('actually scanned the code (zero-file floor)', () => {
    expect(dirFiles.length).toBeGreaterThanOrEqual(MIN_FILES_IN_DIR);
    for (const must of [
      'features/hfToken/hfTokenSlice.js',
      'features/hfToken/hfTokenThunks.js',
      'features/hfToken/robotChannel.js',
      'features/hfToken/syncDecision.js',
      'services/hfTokenApi.js',
    ]) {
      expect(FILES).toContain(must);
    }
  });

  it.each(FORBIDDEN)('contains no %s', (re, what) => {
    const hits = FILES.filter((rel) => re.test(read(rel))).map((rel) => `${rel}: ${what}`);
    expect(hits).toEqual([]);
  });

  it('the thunks module is plain thunks: every exported verb is a function returning a function', async () => {
    const thunks = await import('../hfTokenThunks');
    const verbs = ['refreshHfTokenAccount', 'saveHfToken', 'removeHfToken', 'verifyHfToken',
      'pushHfTokenToRobot', 'clearHfTokenOnRobot', 'forceRetransfer'];
    for (const name of verbs) {
      const thunk = thunks[name]('x');
      expect(typeof thunk).toBe('function');
      // createAsyncThunk's creator carries these; a plain thunk creator does not.
      expect(thunks[name].pending).toBeUndefined();
      expect(thunks[name].typePrefix).toBeUndefined();
    }
  });
});

// ── 2. the Redux sentinel ────────────────────────────────────────────────────
// Sequential letters, low entropy on purpose: the repository's secret scan runs
// over the whole history and flags a random-looking `hf_…` literal.
const SENTINEL = 'hf_' + 'abcdefghijklmnopqrstuvwxyzABCDEFGH';
const SENTINEL_FP = '006da5aabd66bbc9';
const PIECES = [SENTINEL, SENTINEL.slice(3), SENTINEL.slice(5, 25), SENTINEL.slice(12)];

function recordingStore() {
  const actions = [];
  const recorder = () => (next) => (action) => {
    actions.push(action);
    return next(action);
  };
  const store = configureStore({
    reducer: { auth: authReducer, hfToken: hfReducer, ui: uiReducer },
    middleware: (getDefault) => getDefault().concat(recorder),
  });
  store.dispatch(setSession({ access_token: 'jwt-1', user: { id: 'u1' } }));
  return { store, actions };
}

const dump = (store, actions) => [
  JSON.stringify(store.getState()),
  ...actions.map((a) => JSON.stringify(a)),
].join('\n');

const body = (o = {}) => ({
  stored: true, usable: true, hf_username: 'anna', hint: `hf_…${SENTINEL.slice(-4)}`, fp: SENTINEL_FP,
  role: 'write', validated_at: '2026-10-03T10:00:00Z', ...o,
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Redux never sees the token', () => {
  it('a full save → push flow leaves it out of the state and out of every action', async () => {
    const { store, actions } = recordingStore();
    api.putHfToken.mockResolvedValue(body({ account_changed: false }));
    api.revealHfToken.mockResolvedValue({ token: SENTINEL, fp: SENTINEL_FP });
    setRobotToken.mockResolvedValue({ success: true, message: 'Dein Hugging-Face-Token ist auf dem Roboter aktiv.' });

    expect((await store.dispatch(saveHfToken(SENTINEL))).ok).toBe(true);
    store.dispatch({
      type: 'hfToken/robotStateReceived',
      payload: { v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false, receivedAt: 1 },
    });
    expect((await store.dispatch(pushHfTokenToRobot())).ok).toBe(true);

    // Not vacuous: the token really travelled — to the cloud once, and to the
    // robot once, which is the ONLY place it may end up.
    expect(api.putHfToken).toHaveBeenCalledWith('jwt-1', SENTINEL);
    expect(setRobotToken).toHaveBeenCalledTimes(1);
    expect(setRobotToken).toHaveBeenCalledWith(SENTINEL);
    const types = actions.map((a) => a.type);
    expect(types).toEqual(expect.arrayContaining([
      'hfToken/accountSaved', 'hfToken/syncPhaseSet', 'hfToken/syncPushed',
    ]));

    const all = dump(store, actions);
    for (const piece of PIECES) expect(all).not.toContain(piece);
    // every dispatched value is a plain action: no thunk function, no `meta.arg`
    expect(actions.every((a) => a && typeof a === 'object' && typeof a.type === 'string')).toBe(true);
    expect(actions.filter((a) => a.meta && 'arg' in a.meta)).toEqual([]);
    // what the state does keep is the fingerprint and the 4-character hint
    expect(store.getState().hfToken.account.fp).toBe(SENTINEL_FP);
    expect(store.getState().hfToken.sync.lastOwnFp).toBe(SENTINEL_FP);
  });

  it('a refused token, a failed push and a verify leave it out as well', async () => {
    const { store, actions } = recordingStore();
    const refused = Object.assign(new Error('x'), {
      status: 422, detail: `Das sieht nicht nach einem Token aus: ${SENTINEL}`,
    });
    api.putHfToken.mockRejectedValueOnce(refused);
    const result = await store.dispatch(saveHfToken(SENTINEL));
    expect(result.ok).toBe(false);
    expect(result.error).not.toContain(SENTINEL);

    api.getHfToken.mockResolvedValue(body());
    await store.dispatch(refreshHfTokenAccount());
    api.verifyHfToken.mockResolvedValue(body());
    await store.dispatch(verifyHfToken());
    api.revealHfToken.mockResolvedValue({ token: SENTINEL, fp: SENTINEL_FP });
    setRobotToken.mockResolvedValue({ success: false, message: `Abgelehnt: ${SENTINEL}` });
    await store.dispatch(pushHfTokenToRobot({ automatic: false }));
    setRobotToken.mockRejectedValueOnce(new Error(`kaputt ${SENTINEL}`));
    api.revealHfToken.mockResolvedValue({ token: SENTINEL, fp: SENTINEL_FP });
    await store.dispatch(pushHfTokenToRobot({ automatic: false }));
    api.deleteHfToken.mockResolvedValue({ stored: false });
    await store.dispatch(removeHfToken());

    expect(actions.length).toBeGreaterThan(8);
    const all = dump(store, actions);
    for (const piece of PIECES) expect(all).not.toContain(piece);
  });

  it('a sign-out in the middle of a push leaves nothing behind', async () => {
    const { store, actions } = recordingStore();
    api.getHfToken.mockResolvedValue(body());
    await store.dispatch(refreshHfTokenAccount());
    store.dispatch({
      type: 'hfToken/robotStateReceived',
      payload: { v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false, receivedAt: 1 },
    });
    let release;
    api.revealHfToken.mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
    const pending = store.dispatch(pushHfTokenToRobot());
    store.dispatch(signedOut());
    release({ token: SENTINEL, fp: SENTINEL_FP });
    await pending;

    expect(setRobotToken).not.toHaveBeenCalled();
    const all = dump(store, actions);
    for (const piece of PIECES) expect(all).not.toContain(piece);
    expect(store.getState().hfToken.account.fp).toBeNull();
  });
});
