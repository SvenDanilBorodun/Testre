// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Startseite's token card, on a REAL store with the real thunks (only the
// cloud calls are mocked). Pinned: every state of the account and of the robot
// half says the right German thing and offers the right controls, the input is
// hardened and emptied before the request goes out, the two-step removal, and
// that the card never holds, shows or logs the token.

import React from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

import authReducer, { setIsLoading, setSession } from '../../../features/auth/authSlice';
import tasksReducer, { setHeartbeatStatus } from '../../../features/tasks/taskSlice';
import jetsonReducer, { setJetsonStatus } from '../../../store/jetsonSlice';
import uiReducer from '../../../features/ui/uiSlice';
import hfReducer, {
  accountFailed,
  accountLoaded,
  accountLoading,
  robotLegacyConfirmed,
  robotStateReceived,
  syncFailed,
  syncPhaseSet,
  syncPushed,
  syncSettleElapsed,
} from '../../../features/hfToken/hfTokenSlice';
import * as api from '../../../services/hfTokenApi';
import { HF_TOKEN_COPY as T } from '../../../features/hfToken/hfTokenCopy';
import HfTokenCard, { canMaskTextField, formatChecked, robotView } from '../HfTokenCard';

vi.mock('../../../services/hfTokenApi', () => ({
  __esModule: true,
  getHfToken: vi.fn(),
  putHfToken: vi.fn(),
  deleteHfToken: vi.fn(),
  revealHfToken: vi.fn(),
  verifyHfToken: vi.fn(),
}));

let mockCloudOnly = false;
vi.mock('../../../utils/cloudMode', () => ({
  __esModule: true,
  isCloudOnlyMode: () => mockCloudOnly,
}));

// Low-entropy fixture on purpose (the repository's secret scan covers history).
const TOKEN = `hf_${'a'.repeat(34)}`;
const FP = 'c1770a7966b0771e';
const OTHER = 'bbbbbbbbbbbbbbbb';

const storedBody = (o = {}) => ({
  stored: true, usable: true, hf_username: 'anna', hint: 'hf_…aaaa', fp: FP, role: 'write',
  validated_at: '2026-10-03T10:05:00Z', ...o,
});

const robot = (o = {}) => robotStateReceived(
  { v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false, ...o },
  1,
);
const stored = (o = {}) => accountLoaded({
  status: 'stored', hfUsername: 'anna', hint: 'hf_…aaaa', fp: FP, role: 'write',
  validatedAt: '2026-10-03T10:05:00Z', ...o,
});

function setup(actions = [], { withSlice = true, heartbeat = 'connected' } = {}) {
  const store = configureStore({
    reducer: {
      auth: authReducer,
      tasks: tasksReducer,
      jetson: jetsonReducer,
      ui: uiReducer,
      ...(withSlice ? { hfToken: hfReducer } : {}),
    },
  });
  store.dispatch(setSession({ access_token: 'jwt-1', user: { id: 'u1' } }));
  store.dispatch(setHeartbeatStatus(heartbeat));
  actions.forEach((a) => store.dispatch(a));
  const utils = render(<Provider store={store}><HfTokenCard /></Provider>);
  return { store, ...utils };
}

const input = () => screen.getByLabelText(T['card.input.aria']);
const button = (name) => screen.getByRole('button', { name: new RegExp(name) });

let consoleSpies;
beforeEach(() => {
  vi.clearAllMocks();
  mockCloudOnly = false;
  consoleSpies = ['log', 'info', 'warn', 'error', 'debug'].map((m) => vi.spyOn(console, m).mockImplementation(() => {}));
});
afterEach(() => {
  consoleSpies.forEach((s) => s.mockRestore());
});

describe('HfTokenCard — the account half', () => {
  it('says it is loading, and offers nothing, while the cloud has not answered', () => {
    setup();
    expect(screen.getByText(T['card.unknown'])).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByLabelText(T['card.input.aria'])).toBeNull();
  });

  it('renders in a store WITHOUT the slice (every page test) as "unknown"', () => {
    setup([], { withSlice: false });
    expect(screen.getByText(T['card.unknown'])).toBeInTheDocument();
    expect(screen.getByText(T['card.title'])).toBeInTheDocument();
  });

  it('treats "loading" like "unknown"', () => {
    setup([accountLoading()]);
    expect(screen.getByText(T['card.unknown'])).toBeInTheDocument();
  });

  it('says an older server does not support it, and an unconfigured one is for the teacher', () => {
    const a = setup([accountFailed({ kind: 'unsupported' })]);
    expect(screen.getByText(T['card.unsupported'])).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
    a.unmount();
    setup([accountFailed({ kind: 'unavailable' })]);
    expect(screen.getByText(T['card.unavailable'])).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('offers "Erneut laden" after a failed load and re-asks the cloud', async () => {
    const { store } = setup([accountFailed('error')]);
    expect(screen.getByText(T['card.loadError'])).toBeInTheDocument();
    await userEvent.click(button(T['card.reload']));
    expect(store.getState().hfToken.kick).toBe(1);
  });

  it('shows the steps, a hardened field and a disabled save button when no token is stored', () => {
    setup([accountLoaded({ status: 'none' })]);
    expect(screen.getByText(T['card.none.body'])).toBeInTheDocument();
    for (const step of ['step1', 'step2', 'step3', 'step4']) {
      expect(screen.getByText(T[`card.none.${step}`])).toBeInTheDocument();
    }
    expect(screen.getByText(T['card.pill.none'])).toBeInTheDocument();
    const field = input();
    expect(field).toHaveAttribute('spellcheck', 'false');
    expect(field).toHaveAttribute('autocapitalize', 'off');
    expect(field).toHaveAttribute('placeholder', T['card.input.placeholder']);
    // the common password-manager extensions are told to keep away (review j)
    expect(field).toHaveAttribute('data-1p-ignore', 'true');
    expect(field).toHaveAttribute('data-lpignore', 'true');
    expect(field).toHaveAttribute('data-bwignore', 'true');
    expect(field).toHaveAttribute('data-form-type', 'other');
    expect(field.getAttribute('name') || '').not.toMatch(/pass|token/i);
    // no <form> anywhere: the password manager must not be offered a submit
    expect(document.body.innerHTML).not.toContain('<form');
    expect(button(T['card.save'])).toBeDisabled();
  });

  describe('the field is no password field to a password manager, and still masked (review j)', () => {
    const realCss = globalThis.CSS;
    afterEach(() => { globalThis.CSS = realCss; });

    it('a browser that masks a text field gets type="text" + -webkit-text-security, autocomplete off', () => {
      globalThis.CSS = { supports: (prop, value) => prop === '-webkit-text-security' && value === 'disc' };
      expect(canMaskTextField()).toBe(true);
      setup([accountLoaded({ status: 'none' })]);
      const field = input();
      expect(field).toHaveAttribute('type', 'text');
      expect(field).toHaveAttribute('autocomplete', 'off');
    });

    it('a browser that cannot falls back to a password field (masked beats a visible token)', () => {
      globalThis.CSS = { supports: () => false };
      expect(canMaskTextField()).toBe(false);
      setup([accountLoaded({ status: 'none' })]);
      const field = input();
      expect(field).toHaveAttribute('type', 'password');
      // Chrome ignores autocomplete="off" on a password field (audit M8).
      expect(field).toHaveAttribute('autocomplete', 'new-password');
    });

    it('no CSS object at all, or one that throws, is "cannot"', () => {
      globalThis.CSS = undefined;
      expect(canMaskTextField()).toBe(false);
      globalThis.CSS = { supports: () => { throw new Error('nope'); } };
      expect(canMaskTextField()).toBe(false);
    });
  });

  it('the offline login escape says why the status never loads (review i)', () => {
    const { store } = setup([]);
    expect(screen.getByText(T['card.unknown'])).toBeInTheDocument();
    act(() => {
      store.dispatch(setSession(null));
      store.dispatch(setIsLoading(false));
    });
    expect(screen.getByText(T['card.offline'])).toBeInTheDocument();
    expect(screen.queryByText(T['card.unknown'])).toBeNull();
  });

  it('shows the explanatory sentence and an input on an unusable token, and lets the student remove it', async () => {
    setup([stored(), accountLoaded({ status: 'unusable', fp: FP, hfUsername: 'anna', hint: 'hf_…aaaa' })]);
    expect(screen.getByText(T['card.unusable'])).toBeInTheDocument();
    expect(input()).toBeInTheDocument();
    expect(button(T['card.remove'])).toBeInTheDocument();
  });
});

describe('HfTokenCard — saving', () => {
  it('empties the field BEFORE the request is sent, trims the token and then shows the stored view', async () => {
    let release;
    api.putHfToken.mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
    const { store, container } = setup([accountLoaded({ status: 'none' })]);
    await userEvent.type(input(), `  ${TOKEN}  `);
    await userEvent.click(button(T['card.save']));

    // the request is on its way and the secret is already gone from the page
    expect(api.putHfToken).toHaveBeenCalledWith('jwt-1', TOKEN);
    expect(input()).toHaveValue('');
    expect(container.innerHTML).not.toContain(TOKEN);
    expect(screen.getByText(T['card.saving'])).toBeInTheDocument();

    release(storedBody({ account_changed: false }));
    expect(await screen.findByText('anna')).toBeInTheDocument();
    expect(store.getState().hfToken.account.status).toBe('stored');
    expect(container.innerHTML).not.toContain(TOKEN);
  });

  it('saves on Enter, too', async () => {
    api.putHfToken.mockResolvedValue(storedBody());
    setup([accountLoaded({ status: 'none' })]);
    await userEvent.type(input(), `${TOKEN}{Enter}`);
    expect(api.putHfToken).toHaveBeenCalledWith('jwt-1', TOKEN);
  });

  it('shows the server\'s German how-to for a refused token and keeps the card on "none"', async () => {
    const detail = 'Dieses Token hat nur Leserechte. Zum Hochladen braucht EduBotics ein Token mit Schreibrechten.';
    api.putHfToken.mockRejectedValue(Object.assign(new Error('x'), { status: 422, detail }));
    const { store } = setup([accountLoaded({ status: 'none' })]);
    await userEvent.type(input(), TOKEN);
    await userEvent.click(button(T['card.save']));
    expect(await screen.findByRole('alert')).toHaveTextContent(detail);
    expect(store.getState().hfToken.account.status).toBe('none');
    expect(input()).toHaveValue('');
  });

  it('warns when the new token belongs to another Hugging-Face account', async () => {
    api.putHfToken.mockResolvedValue(storedBody({ account_changed: true }));
    setup([accountLoaded({ status: 'none' })]);
    await userEvent.type(input(), TOKEN);
    await userEvent.click(button(T['card.save']));
    expect(await screen.findByText(T['card.accountChanged'])).toBeInTheDocument();
  });

  it('writes nothing to the console while saving', async () => {
    api.putHfToken.mockResolvedValue(storedBody());
    setup([accountLoaded({ status: 'none' })]);
    await userEvent.type(input(), TOKEN);
    await userEvent.click(button(T['card.save']));
    await screen.findByText('anna');
    // `error` may carry React's own "not wrapped in act" noise from this test
    // environment; what must never happen is the token in ANY console argument,
    // or the card itself logging anything at the other levels.
    for (const spy of consoleSpies) {
      const printed = spy.mock.calls.flat().map((arg) => String(arg)).join('\n');
      expect(printed).not.toContain(TOKEN.slice(3));
    }
    const [log, info, warn, , debug] = consoleSpies;
    for (const spy of [log, info, warn, debug]) expect(spy).not.toHaveBeenCalled();
  });
});

describe('HfTokenCard — the stored view and its robot pill', () => {
  const pill = (key) => screen.getByText(T[key]);

  it('shows the proven name, the hint and when it was last checked', () => {
    setup([stored(), robot({ present: true, fp: FP })]);
    expect(screen.getByText(T['card.stored.as'], { exact: false })).toBeInTheDocument();
    expect(screen.getByText('anna')).toBeInTheDocument();
    expect(screen.getByText('hf_…aaaa')).toBeInTheDocument();
    expect(screen.getByText(new RegExp(T['card.stored.checked']))).toBeInTheDocument();
    expect(screen.queryByText(TOKEN)).toBeNull();
  });

  it('active: the robot holds exactly this token', () => {
    setup([stored(), robot({ present: true, fp: FP })]);
    expect(pill('card.pill.active')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: new RegExp(T['card.retry']) })).toBeNull();
  });

  it('"gespeichert" while a connected robot has not reported yet, "nicht verbunden" when the link is down', () => {
    const a = setup([stored()]);
    expect(pill('card.pill.stored')).toBeInTheDocument();
    a.unmount();
    setup([stored()], { heartbeat: 'disconnected' });
    expect(pill('card.pill.noLink')).toBeInTheDocument();
  });

  it('says an old image or a Jetson image takes no personal token', () => {
    const a = setup([stored(), robotLegacyConfirmed()]);
    expect(pill('card.pill.notAccepted')).toBeInTheDocument();
    a.unmount();
    setup([stored(), robot({ accepts: false })]);
    expect(pill('card.pill.notAccepted')).toBeInTheDocument();
  });

  it('working: a push is on its way, or the robot differs and nothing failed yet', () => {
    const a = setup([stored(), robot({ present: true, fp: OTHER }), syncPhaseSet('pushing')]);
    expect(pill('card.pill.working')).toBeInTheDocument();
    a.unmount();
    setup([stored(), robot({ present: true, fp: OTHER })]);
    expect(pill('card.pill.working')).toBeInTheDocument();
  });

  it('waiting: the robot is busy, with the reason', () => {
    setup([stored(), robot({ busy: true })]);
    expect(pill('card.pill.waiting')).toBeInTheDocument();
    expect(screen.getByText(T['card.waitingNote'])).toBeInTheDocument();
  });

  it('taken over: another account\'s token is in the slot, and „Erneut übertragen" takes it back', async () => {
    // A takeover needs my push to have been SEEN first; a state older than the
    // push is not one (review c, the next test).
    const { store } = setup([
      stored(), syncPushed({ fp: FP }), robot({ present: true, fp: FP }), robot({ present: true, fp: OTHER }),
    ]);
    expect(pill('card.pill.takenOver')).toBeInTheDocument();
    expect(screen.getByText(T['card.takenOverNote'])).toBeInTheDocument();
    await userEvent.click(button(T['card.retry']));
    expect(store.getState().hfToken.sync.lastOwnFp).toBeNull();
    expect(store.getState().hfToken.kick).toBe(1);
  });

  it('failed: two failures in a row say so, show the robot\'s own sentence and offer a retry', async () => {
    const { store } = setup([
      stored(), robot({ present: true, fp: OTHER }),
      syncFailed({ message: 'Während einer Aufnahme kann das Token nicht geändert werden.' }),
    ]);
    // one failure is still "working": no red flash for a harmless busy race
    expect(pill('card.pill.working')).toBeInTheDocument();
    store.dispatch(syncFailed({ message: 'Während einer Aufnahme kann das Token nicht geändert werden.' }));
    expect(await screen.findByText(T['card.pill.failed'])).toBeInTheDocument();
    expect(screen.getByText(T['card.failedNote'])).toBeInTheDocument();
    expect(screen.getByText('Während einer Aufnahme kann das Token nicht geändert werden.')).toBeInTheDocument();
    expect(button(T['card.retry'])).toBeInTheDocument();
  });

  it('failed: an open circuit breaker says so even with no failure, and offers the retry', () => {
    // Three automatic writes in a row and the slot is STILL empty: the machine
    // stops writing and hands the decision to the student.
    setup([
      stored(), robot({ present: false }),
      syncPushed({ fp: FP }, 1), syncPushed({ fp: FP }, 2), syncPushed({ fp: FP }, 3),
    ]);
    expect(pill('card.pill.failed')).toBeInTheDocument();
    expect(button(T['card.retry'])).toBeInTheDocument();
  });

  it('no „taken over" flash after a push the robot has not shown yet (review c)', () => {
    // The previous student's token is still in the slot when my push succeeds:
    // until a state shows mine, that slot proves nothing.
    const { store } = setup([stored(), robot({ present: true, fp: OTHER }), syncPushed({ fp: FP }, 1000)]);
    expect(screen.queryByText(T['card.pill.takenOver'])).toBeNull();
    expect(pill('card.pill.working')).toBeInTheDocument();
    act(() => { store.dispatch(robot({ present: true, fp: OTHER })); }); // a stale state after the push
    expect(screen.queryByText(T['card.pill.takenOver'])).toBeNull();
    act(() => { store.dispatch(robot({ present: true, fp: FP })); });    // the robot shows it
    expect(pill('card.pill.active')).toBeInTheDocument();
  });

  it('taken over wins over failed: the sentence has to name the cause', () => {
    setup([
      stored(), robot({ present: true, fp: OTHER }),
      syncPushed({ fp: FP }, 1), syncPushed({ fp: FP }, 2), syncPushed({ fp: FP }, 3),
      // the robot never showed any of the three pushes within the wait
      syncSettleElapsed(10_000),
    ]);
    expect(pill('card.pill.takenOver')).toBeInTheDocument();
  });

  it('a claimed Jetson replaces the robot pill with the Jetson note', () => {
    setup([stored(), setJetsonStatus('connected'), robot({ present: true, fp: OTHER })]);
    expect(screen.getByText(T['card.jetsonNote'])).toBeInTheDocument();
    expect(screen.queryByText(T['card.pill.working'])).toBeNull();
    expect(screen.queryByText(T['card.pill.active'])).toBeNull();
  });
});

describe('HfTokenCard — replace, verify, remove', () => {
  it('replace opens the input, cancel closes it again', async () => {
    setup([stored(), robot({ present: true, fp: FP })]);
    expect(screen.queryByLabelText(T['card.input.aria'])).toBeNull();
    await userEvent.click(button(T['card.replace']));
    expect(input()).toBeInTheDocument();
    await userEvent.click(button(T['card.removeAbort']));
    expect(screen.queryByLabelText(T['card.input.aria'])).toBeNull();
  });

  it('replace saves the new token and closes the input', async () => {
    api.putHfToken.mockResolvedValue(storedBody({ fp: OTHER, hint: 'hf_…bbbb' }));
    const { store } = setup([stored(), robot({ present: true, fp: FP })]);
    await userEvent.click(button(T['card.replace']));
    await userEvent.type(input(), TOKEN);
    await userEvent.click(button(T['card.save']));
    await waitFor(() => expect(store.getState().hfToken.account.fp).toBe(OTHER));
    await waitFor(() => expect(screen.queryByLabelText(T['card.input.aria'])).toBeNull());
  });

  it('verify asks the cloud again and shows a refusal in German', async () => {
    const detail = 'Hugging Face hat dein gespeichertes Token abgelehnt (widerrufen oder abgelaufen). Bitte speichere ein neues.';
    api.verifyHfToken.mockRejectedValue(Object.assign(new Error('x'), { status: 422, detail }));
    setup([stored(), robot({ present: true, fp: FP })]);
    await userEvent.click(button(T['card.verify']));
    expect(await screen.findByRole('alert')).toHaveTextContent(detail);
    expect(api.verifyHfToken).toHaveBeenCalledWith('jwt-1');
  });

  it('remove is two steps: ask, then confirm', async () => {
    api.deleteHfToken.mockResolvedValue({ stored: false });
    const { store } = setup([stored(), robot({ present: true, fp: FP })]);
    await userEvent.click(button(T['card.remove']));
    expect(api.deleteHfToken).not.toHaveBeenCalled();
    expect(button(T['card.removeConfirm'])).toBeInTheDocument();

    await userEvent.click(button(T['card.removeAbort']));
    expect(api.deleteHfToken).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: new RegExp(T['card.removeConfirm']) })).toBeNull();

    await userEvent.click(button(T['card.remove']));
    await userEvent.click(button(T['card.removeConfirm']));
    expect(api.deleteHfToken).toHaveBeenCalledWith('jwt-1');
    await waitFor(() => expect(store.getState().hfToken.account.status).toBe('none'));
    expect(await screen.findByText(T['card.none.body'])).toBeInTheDocument();
  });

  it('remove: a failure says so in German and keeps the token', async () => {
    api.deleteHfToken.mockRejectedValue(Object.assign(new Error('x'), { status: 500, detail: 'Bad Gateway' }));
    const { store } = setup([stored(), robot({ present: true, fp: FP })]);
    await userEvent.click(button(T['card.remove']));
    await userEvent.click(button(T['card.removeConfirm']));
    expect(await screen.findByRole('alert')).toHaveTextContent(T['card.removeError']);
    expect(store.getState().hfToken.account.status).toBe('stored');
  });
});

describe('HfTokenCard — where it does not belong', () => {
  it('renders nothing in cloud-only mode', () => {
    mockCloudOnly = true;
    const { container } = setup([stored()]);
    expect(container).toBeEmptyDOMElement();
  });

  it('never contains a bare dash, the words of other cards or an activation control', () => {
    // HomePage.test.js looks these up with getByText: one stray match breaks it.
    const { container } = setup([accountLoaded({ status: 'none' })]);
    const text = container.textContent;
    for (const forbidden of [
      'Aufnahme starten', 'Umgebung starten', 'Nicht bereit', 'Kein Kontakt zum Roboter',
      'Noch nichts aufgenommen', 'Roboter aktivieren', 'vollständig', '2 von 2', 'in der Gruppe',
    ]) {
      expect(text).not.toContain(forbidden);
    }
    expect(within(container).queryByText('—')).toBeNull();
  });
});

describe('robotView (pure)', () => {
  const base = {
    robot: { known: true, accepts: true, present: true, fp: OTHER, busy: false, legacy: false },
    heartbeatConnected: true, jetsonConnected: false, inSync: false, working: false, decision: 'push', failed: false,
  };

  it('picks one view per situation, in a fixed order', () => {
    expect(robotView({ ...base, jetsonConnected: true }).note).toBe('card.jetsonNote');
    expect(robotView({ ...base, inSync: true }).pill).toBe('card.pill.active');
    expect(robotView({ ...base, decision: 'taken_over', failed: true }).pill).toBe('card.pill.takenOver');
    expect(robotView({ ...base, failed: true, working: true }).pill).toBe('card.pill.failed');
    expect(robotView({ ...base, working: true, decision: 'wait' }).pill).toBe('card.pill.working');
    expect(robotView({ ...base, decision: 'wait' }).pill).toBe('card.pill.waiting');
    expect(robotView(base).pill).toBe('card.pill.working');
  });

  it('only a failed or taken-over view offers the retry button', () => {
    expect(robotView({ ...base, failed: true }).retry).toBe(true);
    expect(robotView({ ...base, decision: 'taken_over' }).retry).toBe(true);
    expect(robotView({ ...base, inSync: true }).retry).toBe(false);
    expect(robotView(base).retry).toBe(false);
  });
});

describe('formatChecked', () => {
  it('formats an ISO time the German way and survives garbage', () => {
    expect(formatChecked('2026-10-03T10:05:00Z')).toMatch(/\d{2}\.\d{2}\.\d{2},? \d{2}:\d{2}/);
    expect(formatChecked('')).toBe('');
    expect(formatChecked(null)).toBe('');
    expect(formatChecked('kein Datum')).toBe('');
  });
});
