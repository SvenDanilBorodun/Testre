/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// `signOutStudent` is the whole sign-out. What this pins is the half that was
// MISSING before it existed: the storage scrub deleted `edubotics_userId`, but
// `state.tasks.taskInfo.userId` was untouched, `useRosServiceCaller` sends
// exactly that value as `user_id` on START_RECORD, and `setTaskInfo`
// re-persists a truthy userId — so the next student recorded under the previous
// student's Hugging Face account, and their first keystroke wrote the deleted
// key straight back. Executed, before the fix:
//
//   setTaskInfo({userId:'schule-A'})                     -> storage 'schule-A'
//   clearStudentScopedStorage()                          -> storage null, Redux 'schule-A'
//   setTaskInfo({...taskInfo, taskName:'greifen'})       -> storage 'schule-A'  <- BACK
//
// Everything runs against the REAL ROOT REDUCER, re-imported under
// `vi.resetModules()` so every slice's module-load `localStorage.getItem` seed
// re-runs. That is the whole point: the previous version of this file built a
// four-reducer store by hand, so a slice added later was invisible to it — and
// one already was. A hand-built store can only ever test the list it was given.
//
// The source-level assertions about StudentApp's „Abmelden" control (that there
// are exactly two, that both carry the aria-label, that both are disabled while
// a task runs) live in sessionScope.test.js next to the "exactly one sign-out"
// glob, so the control is described in one place.

import { vi } from 'vitest';
import {
  STUDENT_SCOPED_KEYS,
  MACHINE_SCOPED_KEYS,
} from '../sessionScope';

const signOutMock = vi.fn(() => Promise.resolve({ error: null }));
const resetJetsonMock = vi.fn();

vi.mock('../../lib/supabaseClient', () => ({
  __esModule: true,
  supabase: { auth: { signOut: signOutMock } },
}));
// The Jetson release is an authenticated network call (a sendBeacon), not a
// reducer — mocked so the order assertion can observe it without a socket.
vi.mock('../../features/jetson/sessionReset', () => ({
  __esModule: true,
  resetJetsonOnLogout: resetJetsonMock,
}));

const SENTINEL = 'SENTINEL-A';

// One DISTINCT sentinel per STUDENT key, derived from the exported list so a new
// key cannot be added without getting one. Seeding only the two keys a slice
// happens to read today was how a new slice hydrating from a LISTED key still
// passed both suites.
//
// One value shape for all of them, carrying its sentinel through every reader in
// the tree: raw readers (`edubotics_userId`) end up holding the whole string, and
// the `JSON.parse` reader (`edubotics_trainingInfo`) yields an object — whose
// `seed`/`steps` are present because trainingSlice DISCARDS a parsed object
// missing either, which would silently make that half of the seed a no-op.
const seedStudentSentinels = () => {
  const map = new Map();
  STUDENT_SCOPED_KEYS.forEach((key, i) => {
    const sentinel = `SENTINEL-STUDENT-${i}`;
    map.set(key, sentinel);
    localStorage.setItem(
      key,
      JSON.stringify({ edubotics_sentinel: sentinel, seed: sentinel, steps: sentinel })
    );
  });
  return map;
};

/**
 * A REAL store, built after `vi.resetModules()` so every slice re-reads
 * localStorage exactly as it does on a fresh page load. Returns the store and
 * the actions from the SAME module registry.
 */
async function freshStore() {
  vi.resetModules();
  const { store } = await import('../../store/store');
  const { signOutStudent } = await import('../signOut');
  const tasks = await import('../../features/tasks/taskSlice');
  const training = await import('../../features/training/trainingSlice');
  const editDataset = await import('../../features/editDataset/editDatasetSlice');
  const workshop = await import('../../features/workshop/workshopSlice');
  const auth = await import('../../features/auth/authSlice');
  return { store, signOutStudent, tasks, training, editDataset, workshop, auth };
}

let reloadMock;
let originalLocation;

beforeEach(() => {
  signOutMock.mockClear();
  signOutMock.mockImplementation(() => Promise.resolve({ error: null }));
  resetJetsonMock.mockClear();
  resetJetsonMock.mockImplementation(() => {});
  localStorage.clear();
  reloadMock = vi.fn();
  originalLocation = Object.getOwnPropertyDescriptor(window, 'location');
  // jsdom refuses real navigation (and logs about it); swap in a stub so the
  // reload is observable instead of noisy.
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...window.location, reload: reloadMock },
  });
});

afterEach(() => {
  if (originalLocation) Object.defineProperty(window, 'location', originalLocation);
  localStorage.clear();
});

describe('signOutStudent — no trace of the previous student', () => {
  it('leaves no trace anywhere in the store, including slices nobody enumerated', async () => {
    // THE anti-sixth-slice property. Seeded through STORAGE, resolved through
    // the REAL root reducer, asserted over the WHOLE state tree — so a slice
    // added tomorrow that hydrates from a student key is covered without this
    // file being touched. `session/signedOut` is what makes that possible: each
    // slice answers for itself instead of appearing on a list here.
    localStorage.setItem('edubotics_userId', SENTINEL);
    localStorage.setItem(
      'edubotics_trainingInfo',
      JSON.stringify({ seed: SENTINEL, steps: SENTINEL })
    );

    const { store, signOutStudent } = await freshStore();
    // Not vacuous: the sentinel really is in the store before the sign-out.
    expect(JSON.stringify(store.getState())).toContain(SENTINEL);

    await store.dispatch(signOutStudent({ reload: false }));

    expect(JSON.stringify(store.getState())).not.toContain(SENTINEL);
  });

  it('clears the student identity from BOTH storage and Redux', async () => {
    const { store, signOutStudent, tasks } = await freshStore();
    store.dispatch(tasks.setTaskInfo({ userId: 'schule-A' }));
    expect(localStorage.getItem('edubotics_userId')).toBe('schule-A');
    expect(store.getState().tasks.taskInfo.userId).toBe('schule-A');

    await store.dispatch(signOutStudent({ reload: false }));

    expect(localStorage.getItem('edubotics_userId')).toBeNull();
    expect(store.getState().tasks.taskInfo.userId).toBeUndefined();
  });

  it('leaves nothing for the next keystroke to re-persist', async () => {
    // THE regression. InfoPanel::handleChange dispatches
    // setTaskInfo({...info, [field]: value}) — the whole object — so a stale
    // Redux userId is written back into the key the scrub just deleted.
    const { store, signOutStudent, tasks } = await freshStore();
    store.dispatch(tasks.setTaskInfo({ userId: 'schule-A' }));
    await store.dispatch(signOutStudent({ reload: false }));

    store.dispatch(
      tasks.setTaskInfo({ ...store.getState().tasks.taskInfo, taskName: 'greifen' })
    );
    expect(localStorage.getItem('edubotics_userId')).toBeNull();
    expect(store.getState().tasks.taskInfo.userId).toBeUndefined();
  });

  it('does not resurrect the id from the module-load initialState snapshot', async () => {
    // taskSlice's `savedUserId` is read from localStorage at MODULE LOAD, so a
    // plain "reset to initialState" hands the id straight back. Seeding storage
    // BEFORE the reset-modules import is what makes that snapshot real here.
    localStorage.setItem('edubotics_userId', 'schule-A');
    const { store, signOutStudent } = await freshStore();
    expect(store.getState().tasks.taskInfo.userId).toBe('schule-A');

    await store.dispatch(signOutStudent({ reload: false }));
    expect(store.getState().tasks.taskInfo.userId).toBeUndefined();
  });

  it('does not resurrect the training form from ITS module-load snapshot either', async () => {
    localStorage.setItem(
      'edubotics_trainingInfo',
      JSON.stringify({ seed: 4242, steps: 12345, policyType: 'act' })
    );
    const { store, signOutStudent } = await freshStore();
    expect(store.getState().training.trainingInfo.steps).toBe(12345);

    await store.dispatch(signOutStudent({ reload: false }));
    expect(store.getState().training.trainingInfo.steps).not.toBe(12345);
    expect(store.getState().training.trainingInfo.policyType).toBeUndefined();
    expect(localStorage.getItem('edubotics_trainingInfo')).toBeNull();
  });

  it('resets the dataset-edit form and the Roboter-Studio editor', async () => {
    const { store, signOutStudent, editDataset, workshop } = await freshStore();
    store.dispatch(editDataset.setHFUserId('schule-A'));
    store.dispatch(workshop.setActiveTutorial({ id: 'lesson-3', step: 4 }));
    // D3: the tutorial ids were cleared but the toolbox restriction it imposes
    // and the unsaved program were not — and WorkshopPage seeds the editor from
    // `unsavedBlocklyJson` when the cloud fetch fails, so on a reload-less
    // sign-out the next student opened Roboter Studio holding these blocks.
    store.dispatch(workshop.setRestrictedBlocks(['edubotics_move_to']));
    store.dispatch(workshop.setUnsavedBlocklyJson({ blocks: 'schule-A program' }));
    store.dispatch(workshop.setSelectedWorkflowId('wf-123'));

    await store.dispatch(signOutStudent({ reload: false }));

    const s = store.getState();
    expect(s.editDataset.hfUserId).toBe('');
    expect(s.workshop.activeTutorialId).toBeNull();
    expect(s.workshop.activeTutorialStep).toBe(0);
    expect(s.workshop.restrictedBlocks).toBeNull();
    expect(s.workshop.unsavedBlocklyJson).toBeNull();
    expect(s.workshop.selectedWorkflowId).toBeNull();
  });

  it('keeps the rig calibration the workshop slice also holds', async () => {
    // The same slice is half student, half rig. A calibration describes the
    // CAMERA and the TABLE and is persisted server-side; clearing it would send
    // the next student through a 20-frame ChArUco capture for nothing.
    const { store, signOutStudent, workshop } = await freshStore();
    store.dispatch(workshop.markStepComplete('scene_intrinsic'));
    store.dispatch(workshop.markStepComplete('scene_handeye'));

    await store.dispatch(signOutStudent({ reload: false }));

    expect(store.getState().workshop.hasIntrinsicScene).toBe(true);
    expect(store.getState().workshop.hasHandeyeScene).toBe(true);
  });

  it('keeps exactly the three rig/nonce fields the training slice names', async () => {
    // The handler is a KEEP-LIST, so the survivors are the interesting half:
    // two rig facts and a monotonic refetch nonce that must never go backwards.
    const { store, signOutStudent, training } = await freshStore();
    store.dispatch(training.setTopicReceived(true));
    store.dispatch(training.triggerCloudJobsRefresh());
    store.dispatch(training.triggerCloudJobsRefresh());
    store.dispatch(training.setLastUpdate(1234567));
    store.dispatch(training.setSelectedTrainingId('job-9'));

    await store.dispatch(signOutStudent({ reload: false }));

    const t = store.getState().training;
    expect(t.topicReceived).toBe(true);
    expect(t.cloudJobsRefreshCounter).toBe(2);
    expect(t.lastUpdate).toBe(1234567);
    // ... and the student's own job selection is gone.
    expect(t.selectedTrainingId).toBeNull();
  });

  it('ends the auth session', async () => {
    const { store, signOutStudent, auth } = await freshStore();
    store.dispatch(auth.setSession({ access_token: 'jwt-abc', user: { email: 'a@b.c' } }));
    store.dispatch(auth.setQuota({ training_credits: 5, trainings_used: 2 }));

    await store.dispatch(signOutStudent({ reload: false }));

    expect(store.getState().auth.session).toBeNull();
    expect(store.getState().auth.isAuthenticated).toBe(false);
    expect(store.getState().auth.trainingCredits).toBe(0);
  });

  it('keeps the machine-scoped keys', async () => {
    const { store, signOutStudent } = await freshStore();
    MACHINE_SCOPED_KEYS.forEach((k) => localStorage.setItem(k, 'keep-me'));
    await store.dispatch(signOutStudent({ reload: false }));
    MACHINE_SCOPED_KEYS.forEach((k) => {
      expect(localStorage.getItem(k)).toBe('keep-me');
    });
  });

  it('scrubs every student-scoped key, not only the ones a slice reads', async () => {
    const { store, signOutStudent } = await freshStore();
    STUDENT_SCOPED_KEYS.forEach((k) => localStorage.setItem(k, 'x'));
    await store.dispatch(signOutStudent({ reload: false }));
    STUDENT_SCOPED_KEYS.forEach((k) => {
      expect(localStorage.getItem(k)).toBeNull();
    });
  });

  it('leaves no sentinel from ANY student key anywhere in the store', async () => {
    // Seeded from EVERY entry, not the two a slice reads today. Storage is what
    // a fresh document hydrates from, so a slice added tomorrow that reads a
    // listed key is covered here without this file being touched.
    const sentinels = seedStudentSentinels();
    const { store, signOutStudent } = await freshStore();

    // Not vacuous — the keys slices DO hydrate from are in the tree first, and
    // they are named so this half cannot silently become a tautology.
    const before = JSON.stringify(store.getState());
    expect(before).toContain(sentinels.get('edubotics_userId'));
    expect(before).toContain(sentinels.get('edubotics_trainingInfo'));

    await store.dispatch(signOutStudent({ reload: false }));

    const after = JSON.stringify(store.getState());
    const survivors = [...sentinels].filter(([, s]) => after.includes(s));
    expect(survivors).toEqual([]);
    for (const key of sentinels.keys()) {
      expect(localStorage.getItem(key)).toBeNull();
    }
  });
});

describe('signOutStudent — the local teardown finishes before the remote call', () => {
  it('has Redux clean WHILE the revoke is still in flight', async () => {
    // The awaited revoke is a real window: the whole document keeps running
    // across it. With the broadcast on the FAR side, `auth.isAuthenticated` was
    // still true and storage was already scrubbed, so ONE /task/status tick
    // (~2 s cadence, against a revoke of hundreds of ms) re-adopted the previous
    // student's id into Redux AND — via setTaskInfo's re-persist — back into the
    // key the scrub had just deleted. The reload then re-hydrated it.
    const { store, signOutStudent, auth } = await freshStore();
    store.dispatch(auth.setSession({ access_token: 'jwt-abc' }));

    let observed = null;
    let release;
    signOutMock.mockImplementation(() => new Promise((resolve) => {
      // Sampled from inside the revoke — the only place the window is visible.
      observed = { ...store.getState().auth };
      release = () => resolve({ error: null });
    }));

    const pending = store.dispatch(signOutStudent({ reload: false }));
    await Promise.resolve();
    release();
    await pending;

    expect(observed).not.toBeNull();
    expect(observed.isAuthenticated).toBe(false);
    expect(observed.session).toBeNull();
  });

  it('re-scrubs whatever the await window wrote back', async () => {
    // Redux is clean and the /task/status adopt is identity-gated by now, but
    // `setTaskInfo` re-persists a truthy userId from whatever object ITS caller
    // captured — an InfoPanel handler, a poll, any closure that survived the
    // await. The second scrub closes the window instead of enumerating which
    // callers are still alive.
    const { store, signOutStudent, tasks } = await freshStore();
    signOutMock.mockImplementation(() => {
      store.dispatch(tasks.setTaskInfo({ userId: 'schule-A' }));
      expect(localStorage.getItem('edubotics_userId')).toBe('schule-A');
      return Promise.resolve({ error: null });
    });

    await store.dispatch(signOutStudent({ reload: false }));

    expect(localStorage.getItem('edubotics_userId')).toBeNull();
  });
});

describe('signOutStudent — a revoke that fails WITHOUT throwing', () => {
  const SB_KEY = 'sb-fnnbysrjkfugsqzwcksd-auth-token';

  it('sweeps the session key auth-js left behind', async () => {
    // @supabase/auth-js::_signOut returns `{ error }` and SKIPS
    // `_removeSession()` for any admin-signOut status other than 404/401/403:
    // the promise resolves, nothing throws, and the persisted session survives.
    // A captive portal answering 502 is the realistic classroom case — and the
    // reload would then restore the PREVIOUS student's session while presenting
    // a completed handover.
    const { store, signOutStudent } = await freshStore();
    localStorage.setItem(SB_KEY, '{"access_token":"still-valid"}');
    localStorage.setItem(`${SB_KEY}-code-verifier`, 'pkce');
    signOutMock.mockResolvedValueOnce({ error: { status: 502, message: 'Bad Gateway' } });

    await store.dispatch(signOutStudent({ reload: false }));

    expect(localStorage.getItem(SB_KEY)).toBeNull();
    expect(localStorage.getItem(`${SB_KEY}-code-verifier`)).toBeNull();
  });

  it('sweeps it when the revoke throws, too', async () => {
    const { store, signOutStudent } = await freshStore();
    localStorage.setItem(SB_KEY, '{"access_token":"still-valid"}');
    signOutMock.mockRejectedValueOnce(new Error('network down'));

    await store.dispatch(signOutStudent({ reload: false }));

    expect(localStorage.getItem(SB_KEY)).toBeNull();
  });

  it('leaves the key to auth-js on a clean revoke', async () => {
    // The sweep is a FALLBACK. On the normal path supabase.auth.signOut() is
    // what removes the key, and it also revokes server-side — hand-deleting it
    // would skip the revoke, which is why nothing else in src/ touches it. The
    // mock does not remove it, so a still-present key here proves the sweep did
    // not run rather than proving the key survives in production.
    const { store, signOutStudent } = await freshStore();
    localStorage.setItem(SB_KEY, '{"access_token":"x"}');
    await store.dispatch(signOutStudent({ reload: false }));
    expect(localStorage.getItem(SB_KEY)).not.toBeNull();
  });
});

describe('logoutBlockReason', () => {
  // Imported lazily: a top-level `import … from '../signOut'` evaluates the
  // module — and its supabaseClient import — before the hoisted vi.mock factory's
  // variables exist.
  let logoutBlockReason;
  let LOGOUT_BLOCK_TITLES_DE;
  beforeAll(async () => {
    ({ logoutBlockReason, LOGOUT_BLOCK_TITLES_DE } = await import('../signOut'));
  });

  const live = { robotLinkLive: true, taskRunning: false, workflowRunning: false, calibrationCapturing: false };

  it('blocks a running recording / inference', () => {
    expect(logoutBlockReason({ ...live, taskRunning: true })).toBe('task');
  });

  it('blocks a running Roboter-Studio program — a SECOND surface', () => {
    // Roboter Studio activity lives in state.workshop, never in
    // tasks.taskStatus, so a guard that checks only the latter misses it.
    expect(logoutBlockReason({ ...live, workflowRunning: true })).toBe('workflow');
  });

  it('blocks a calibration with captured frames', () => {
    expect(logoutBlockReason({ ...live, calibrationCapturing: true })).toBe('calibration');
  });

  it('blocks nothing when the rig is idle', () => {
    expect(logoutBlockReason(live)).toBeNull();
  });

  it('blocks NOTHING once rosbridge is gone, whatever the last tick said', () => {
    // THE regression this gate exists for. Every input is written only by a ROS
    // message and nothing clears them on a drop (`resetTaskStatus` is dispatched
    // nowhere; the watchdog writes only heartbeatStatus), so a socket death
    // while the last tick said RECORDING latched „Abmelden" shut for the life of
    // the document — telling the student in German to stop a recording they can
    // no longer reach, on the shared PC they are trying to hand over.
    for (const stale of ['taskRunning', 'workflowRunning', 'calibrationCapturing']) {
      expect(
        logoutBlockReason({ ...live, robotLinkLive: false, [stale]: true })
      ).toBeNull();
    }
  });

  it('has German wording for every reason it can return', () => {
    const reasons = ['task', 'workflow', 'calibration'].map((r) =>
      logoutBlockReason({ ...live, [`${r === 'task' ? 'taskRunning' : r === 'workflow' ? 'workflowRunning' : 'calibrationCapturing'}`]: true })
    );
    for (const reason of reasons) {
      const title = LOGOUT_BLOCK_TITLES_DE[reason];
      expect(typeof title).toBe('string');
      // Names WHICH activity blocks, not just "not possible" — and in German
      // with literal umlauts (Rule §1).
      expect(title).toMatch(/^Abmelden nicht möglich, solange /);
    }
    // The three texts are distinct: a shared string would tell a student to stop
    // the wrong thing.
    expect(new Set(reasons.map((r) => LOGOUT_BLOCK_TITLES_DE[r])).size).toBe(3);
  });
});

describe('signOutStudent — ordering and failure modes', () => {
  it('releases the Jetson lock with the still-valid JWT from the store, before signOut', async () => {
    const order = [];
    resetJetsonMock.mockImplementation(() => order.push('jetson'));
    signOutMock.mockImplementation(() => {
      order.push('signOut');
      return Promise.resolve({ error: null });
    });

    const { store, signOutStudent, auth } = await freshStore();
    store.dispatch(auth.setSession({ access_token: 'jwt-abc' }));
    const { setJetsonInfo } = await import('../../store/jetsonSlice');
    store.dispatch(setJetsonInfo({ jetson_id: 'jetson-1' }));

    await store.dispatch(signOutStudent({ reload: false }));

    // The thunk reads both out of getState — no caller threads them in any more.
    expect(resetJetsonMock).toHaveBeenCalledWith(
      expect.any(Function),
      'jwt-abc',
      'jetson-1'
    );
    expect(order).toEqual(['jetson', 'signOut']);
  });

  it('reloads by default and not when asked not to', async () => {
    const { store, signOutStudent } = await freshStore();
    await store.dispatch(signOutStudent());
    expect(reloadMock).toHaveBeenCalledTimes(1);

    reloadMock.mockClear();
    await store.dispatch(signOutStudent({ reload: false }));
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it('resets Redux even when the reload happens', async () => {
    // The reload is the completeness backstop, never the mechanism: a student
    // who signs out mid-recording can decline StudentApp's `beforeunload`
    // prompt, and then no reload happens at all.
    const { store, signOutStudent, tasks } = await freshStore();
    store.dispatch(tasks.setTaskInfo({ userId: 'schule-A' }));
    await store.dispatch(signOutStudent());
    expect(store.getState().tasks.taskInfo.userId).toBeUndefined();
    expect(reloadMock).toHaveBeenCalled();
  });

  it('still resets when the Supabase revoke throws', async () => {
    const { store, signOutStudent, tasks } = await freshStore();
    store.dispatch(tasks.setTaskInfo({ userId: 'schule-A' }));
    signOutMock.mockRejectedValueOnce(new Error('network down'));

    await expect(
      store.dispatch(signOutStudent({ reload: false }))
    ).resolves.toBeUndefined();

    expect(store.getState().tasks.taskInfo.userId).toBeUndefined();
    expect(localStorage.getItem('edubotics_userId')).toBeNull();
  });
});
