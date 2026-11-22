/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The V1's placement hint on „Roboter aktivieren" (spec 40 WP-HINT, 52 §5.3
// (a) and §5.4; decisions D56, D57 Q1, D88).
//
// The driver publishes its OWN verdict for the activation glide from the live
// pose on /edubotics/tower_state (WP-T1). While it is not `clear`, the card
// holds the activation back, shows the rendered animation and the decided
// sentence, and offers the one way out: free the arm (two clicks, the second
// one „Ich halte ihn"), lay it forward, lock it again — through the existing
// /workshop/hand_guide with JogPanel's 15 s keepalive. Without the topic (any
// other profile, the legacy camera, an older image) the card is today's card,
// byte for byte: the driver stays the fence (D1).
//
// The strings are pinned as literals on purpose: D88 decided the activation
// sentence word for word.

import React, { Profiler } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';

import ActivationCard from '../ActivationCard';

const TOWER_HINT_ACTIVATE_DE = 'Der Arm liegt so, dass er beim Aktivieren dem Kameraturm zu '
  + "nahe käme. Lockere ihn, lege ihn nach vorne und setze ihn wieder fest – dann geht's los.";
const TOWER_HINT_HOLD_DE = 'Halte den Arm mit einer Hand – gleich wird er weich.';
const TOWER_STATE_TOPIC = '/edubotics/tower_state';

let mockActivation;
const activate = vi.fn(() => Promise.resolve(true));
vi.mock('../../../hooks/useRobotActivation', async () => {
  const actual = await vi.importActual('../../../hooks/useRobotActivation');
  return {
    ...actual,
    __esModule: true,
    default: () => mockActivation,
  };
});

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

const mockHandGuide = vi.hoisted(() => vi.fn());
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({ handGuide: mockHandGuide }),
}));

vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ isConnected: true })) },
}));

const mockTopics = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.opts = opts;
      this.cb = null;
      this.unsubscribed = false;
      this.subscribe = (cb) => { this.cb = cb; };
      this.unsubscribe = () => { this.unsubscribed = true; };
      mockTopics.push(this);
    },
  },
}));

// The four capability fixtures of HomePage.test.js (the card is gated on no
// capability, so it must look the same on every one of them).
const FULL = {
  recordable: true, editable: true, trainable: true,
  inferable: true, roboter_studio: true, has_leader: true,
};
const CAPS = {
  omx_full: FULL,
  omx_follower: { ...FULL, recordable: false, has_leader: false },
  unknown: null,
  partial: { recordable: false },
};

function setUp({
  status = null, calling = false, error = null, connected = true, running = false,
  workflowRunning = false, caps = null, profile = 'v1_studio',
  rosbridgeUrl = 'ws://localhost/rosbridge',
} = {}) {
  mockActivation = { status, activate, calling, error };
  mockState = {
    tasks: {
      heartbeatStatus: connected ? 'connected' : 'disconnected',
      taskStatus: { running, capabilities: caps, robotProfile: profile },
    },
    ros: { rosbridgeUrl },
    workshop: {
      runState: workflowRunning ? 'running' : 'idle',
      paused: false,
      calibration: { framesCaptured: 0, calibrated: false },
    },
  };
}

const towerTopics = () => mockTopics.filter((t) => t.opts.name === TOWER_STATE_TOPIC);

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve(); // eslint-disable-line no-await-in-loop
  });
}

let gen = 0;
async function deliver(glide, fall = 'back') {
  const live = towerTopics().filter((t) => !t.unsubscribed);
  expect(live).toHaveLength(1);
  gen += 1;
  await act(async () => {
    live[0].cb({ data: JSON.stringify({ v: 1, glide, fall, part: glide === 'refused' ? 'head' : null, gen }) });
  });
}

async function renderCard(opts) {
  setUp(opts);
  const view = render(<ActivationCard />);
  await flush();
  return view;
}

const activateButton = () => screen.getByRole('button', { name: 'Roboter aktivieren' });

beforeEach(() => {
  activate.mockClear();
  mockHandGuide.mockReset();
  mockHandGuide.mockImplementation(() => Promise.resolve({ success: true, message: '' }));
  mockTopics.length = 0;
  gen = 0;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('ActivationCard — the tower hint holds the activation back', () => {
  it.each(['refused', 'unjudgeable'])('glide %s: the button is disabled, the animation and the sentence show', async (glide) => {
    await renderCard({ status: { state: 'idle', hasLeader: false, required: true, message: '' } });
    await deliver(glide);
    expect(activateButton()).toBeDisabled();
    expect(screen.getByText(TOWER_HINT_ACTIVATE_DE)).toBeInTheDocument();
    // The animation: the forward strip, rendered from the real V1 URDF.
    const pic = document.querySelector('[data-tower-hint="forward"]'); // eslint-disable-line testing-library/no-node-access
    expect(pic).not.toBeNull();
    expect(pic.getAttribute('data-robot-picture')).toMatch(/^v1_tower_hint_forward(_light)?\.webp$/);
    fireEvent.click(activateButton());
    expect(activate).not.toHaveBeenCalled();
  });

  it('the agent unknown (status null) and refused: still held back', async () => {
    await renderCard({ status: null });
    await deliver('refused');
    expect(activateButton()).toBeDisabled();
    expect(screen.getByText(TOWER_HINT_ACTIVATE_DE)).toBeInTheDocument();
  });

  it('an ACTIVE follower-only V1 that lies back is not re-homed either', async () => {
    // „Grundstellung erneut anfahren" calls /edubotics/activate too.
    await renderCard({ status: { state: 'active', hasLeader: false, required: true, message: '' } });
    await deliver('refused');
    const rehome = screen.getByRole('button', { name: 'Grundstellung erneut anfahren' });
    expect(rehome).toBeDisabled();
    fireEvent.click(rehome);
    expect(activate).not.toHaveBeenCalled();
    expect(screen.getByText(TOWER_HINT_ACTIVATE_DE)).toBeInTheDocument();
  });

  it('clear: today\'s card, byte for byte, and the activation runs', async () => {
    setUp({ status: { state: 'idle', hasLeader: false, required: true, message: '' }, profile: 'omx_follower' });
    const { container: omx } = render(<ActivationCard />);
    await flush();
    const today = omx.innerHTML;
    cleanup();
    const { container } = await renderCard({ status: { state: 'idle', hasLeader: false, required: true, message: '' } });
    await deliver('refused');
    expect(container.innerHTML).not.toBe(today);
    await deliver('clear');
    expect(container.innerHTML).toBe(today);
    fireEvent.click(activateButton());
    expect(activate).toHaveBeenCalledTimes(1);
  });

  it('a message it cannot read is no verdict: today\'s card', async () => {
    const { container } = await renderCard({ status: null });
    const before = container.innerHTML;
    const live = towerTopics()[0];
    await act(async () => { live.cb({ data: '{kaputt' }); });
    await act(async () => { live.cb({ data: JSON.stringify({ v: 2, glide: 'refused', fall: 'back', gen: 1 }) }); });
    await act(async () => { live.cb({ data: JSON.stringify({ v: 1, glide: 'gesperrt', fall: 'back', gen: 2 }) }); });
    expect(container.innerHTML).toBe(before);
    expect(activateButton()).not.toBeDisabled();
  });

  it.each(['clear', 'refused'])('glide %s re-sent at 2 Hz: the card commits nothing (S-1)', async (glide) => {
    setUp({ status: { state: 'idle', hasLeader: false, required: true, message: '' } });
    let commits = 0;
    render(<Profiler id="card" onRender={() => { commits += 1; }}><ActivationCard /></Profiler>);
    await flush();
    await deliver(glide);
    const before = commits;
    for (let i = 0; i < 30; i += 1) await deliver(glide); // eslint-disable-line no-await-in-loop
    expect(commits).toBe(before);
    await deliver(glide === 'clear' ? 'refused' : 'clear');
    expect(commits).toBeGreaterThan(before);
  });

  it.each([['omx_full'], ['omx_follower'], ['']])('profile %j: the card never subscribes to the tower (D1)', async (profile) => {
    await renderCard({ status: null, profile });
    expect(towerTopics()).toHaveLength(0);
  });

  it('no bridge: no subscription', async () => {
    await renderCard({ status: null, connected: false });
    expect(towerTopics()).toHaveLength(0);
  });
});

// No topic → today's card. The snapshots were taken from the card as it was
// before the hint existed; the V1 with a silent topic and an OMX (which never
// subscribes) must both render exactly that.
describe('ActivationCard — no topic: today\'s card on all four capability fixtures', () => {
  async function todayAndSilent(caps) {
    setUp({ caps, profile: 'omx_full' });
    const { container: omx } = render(<ActivationCard />);
    await flush();
    const omxHtml = omx.innerHTML;
    cleanup();
    const { container } = await renderCard({ caps, profile: 'v1_studio' });
    expect(container.innerHTML).toBe(omxHtml);
    return container.innerHTML;
  }

  it('omx_full', async () => {
    expect(await todayAndSilent(CAPS.omx_full)).toMatchInlineSnapshot(`"<section class="su-plate su-plate--plate su-act"><div role="img" aria-label="unbekannt" data-tone="neutral" class="su-act__disc su-act__disc--idle"><span class="su-act__core">unbekannt</span></div><div class="su-act__body"><div class="su-act__head"><h3 class="su-act__title">Roboter aktivieren</h3><span aria-hidden="true" class="su-led su-led--off su-led--none" style="width: 9px; height: 9px;"></span></div><p class="su-act__state su-serif su-act__state--long">Beim Start bewegt sich nichts. Wenn du hier drückst, fährt der Follower-Arm zuerst in die Grundstellung, gleicht sich dann an den Leader-Arm an — und erst danach folgt er deinen Bewegungen.</p><div class="su-act__action"><button class="eb-btn eb-btn--primary eb-btn--lg inline-flex items-center gap-1.5 font-medium rounded-[var(--radius-sm)] transition h-12 px-5 text-sm bg-[var(--accent)] text-white hover:brightness-110 active:brightness-95 border border-transparent disabled:opacity-50 disabled:cursor-not-allowed">Roboter aktivieren</button></div></div></section>"`);
  });

  it('omx_follower (recordable false)', async () => {
    expect(await todayAndSilent(CAPS.omx_follower)).toMatchInlineSnapshot(`"<section class="su-plate su-plate--plate su-act"><div role="img" aria-label="unbekannt" data-tone="neutral" class="su-act__disc su-act__disc--idle"><span class="su-act__core">unbekannt</span></div><div class="su-act__body"><div class="su-act__head"><h3 class="su-act__title">Roboter aktivieren</h3><span aria-hidden="true" class="su-led su-led--off su-led--none" style="width: 9px; height: 9px;"></span></div><p class="su-act__state su-serif su-act__state--long">Beim Start bewegt sich nichts. Wenn du hier drückst, fährt der Roboterarm in die Grundstellung. Halte den Bereich um den Arm frei.</p><div class="su-act__action"><button class="eb-btn eb-btn--primary eb-btn--lg inline-flex items-center gap-1.5 font-medium rounded-[var(--radius-sm)] transition h-12 px-5 text-sm bg-[var(--accent)] text-white hover:brightness-110 active:brightness-95 border border-transparent disabled:opacity-50 disabled:cursor-not-allowed">Roboter aktivieren</button></div></div></section>"`);
  });

  it('unknown capabilities (null)', async () => {
    expect(await todayAndSilent(CAPS.unknown)).toMatchInlineSnapshot(`"<section class="su-plate su-plate--plate su-act"><div role="img" aria-label="unbekannt" data-tone="neutral" class="su-act__disc su-act__disc--idle"><span class="su-act__core">unbekannt</span></div><div class="su-act__body"><div class="su-act__head"><h3 class="su-act__title">Roboter aktivieren</h3><span aria-hidden="true" class="su-led su-led--off su-led--none" style="width: 9px; height: 9px;"></span></div><p class="su-act__state su-serif su-act__state--long">Beim Start bewegt sich nichts. Wenn du hier drückst, fährt der Follower-Arm zuerst in die Grundstellung, gleicht sich dann an den Leader-Arm an — und erst danach folgt er deinen Bewegungen.</p><div class="su-act__action"><button class="eb-btn eb-btn--primary eb-btn--lg inline-flex items-center gap-1.5 font-medium rounded-[var(--radius-sm)] transition h-12 px-5 text-sm bg-[var(--accent)] text-white hover:brightness-110 active:brightness-95 border border-transparent disabled:opacity-50 disabled:cursor-not-allowed">Roboter aktivieren</button></div></div></section>"`);
  });

  it('a PARTIAL manifest', async () => {
    expect(await todayAndSilent(CAPS.partial)).toMatchInlineSnapshot(`"<section class="su-plate su-plate--plate su-act"><div role="img" aria-label="unbekannt" data-tone="neutral" class="su-act__disc su-act__disc--idle"><span class="su-act__core">unbekannt</span></div><div class="su-act__body"><div class="su-act__head"><h3 class="su-act__title">Roboter aktivieren</h3><span aria-hidden="true" class="su-led su-led--off su-led--none" style="width: 9px; height: 9px;"></span></div><p class="su-act__state su-serif su-act__state--long">Beim Start bewegt sich nichts. Wenn du hier drückst, fährt der Follower-Arm zuerst in die Grundstellung, gleicht sich dann an den Leader-Arm an — und erst danach folgt er deinen Bewegungen.</p><div class="su-act__action"><button class="eb-btn eb-btn--primary eb-btn--lg inline-flex items-center gap-1.5 font-medium rounded-[var(--radius-sm)] transition h-12 px-5 text-sm bg-[var(--accent)] text-white hover:brightness-110 active:brightness-95 border border-transparent disabled:opacity-50 disabled:cursor-not-allowed">Roboter aktivieren</button></div></div></section>"`);
  });
});

describe('ActivationCard — freeing the V1 (52 §5.4, D57 Q1)', () => {
  const IDLE = { state: 'idle', hasLeader: false, required: true, message: '' };
  const callsWith = (on) => mockHandGuide.mock.calls.filter((c) => c[0] === on).length;

  async function freeArm() {
    fireEvent.click(screen.getByRole('button', { name: 'Arm lockern' }));
    fireEvent.click(screen.getByRole('button', { name: 'Ich halte ihn' }));
    await flush();
    await flush();
  }

  it('hand_guide(true) only after „Ich halte ihn"', async () => {
    await renderCard({ status: IDLE });
    await deliver('refused');
    expect(mockHandGuide).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Arm lockern' }));
    // „Arm lockern" only asks: the arm is about to go limp in the student's hand.
    expect(mockHandGuide).not.toHaveBeenCalled();
    expect(screen.getByText(TOWER_HINT_HOLD_DE)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Ich halte ihn' }));
    await flush();
    expect(mockHandGuide).toHaveBeenCalledTimes(1);
    expect(mockHandGuide).toHaveBeenCalledWith(true);
    expect(screen.getByRole('button', { name: 'Arm festsetzen' })).toBeInTheDocument();
    expect(activateButton()).toBeDisabled();
  });

  it('„Abbrechen" before „Ich halte ihn" frees nothing', async () => {
    await renderCard({ status: IDLE });
    await deliver('refused');
    fireEvent.click(screen.getByRole('button', { name: 'Arm lockern' }));
    fireEvent.click(screen.getByRole('button', { name: 'Abbrechen' }));
    expect(mockHandGuide).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Arm lockern' })).toBeInTheDocument();
  });

  it('keeps the limp session alive every 15 s, and stops after „Arm festsetzen"', async () => {
    vi.useFakeTimers();
    await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    expect(callsWith(true)).toBe(1);
    await act(async () => { vi.advanceTimersByTime(14999); });
    expect(callsWith(true)).toBe(1);
    await act(async () => { vi.advanceTimersByTime(1); });
    expect(callsWith(true)).toBe(2);
    await act(async () => { vi.advanceTimersByTime(15000); });
    expect(callsWith(true)).toBe(3);
    fireEvent.click(screen.getByRole('button', { name: 'Arm festsetzen' }));
    await flush();
    await flush();
    expect(mockHandGuide).toHaveBeenLastCalledWith(false);
    await act(async () => { vi.advanceTimersByTime(60000); });
    expect(callsWith(true)).toBe(3);
  });

  it('never stacks a keepalive on one still in flight', async () => {
    vi.useFakeTimers();
    await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    mockHandGuide.mockImplementation((on) => (on ? new Promise(() => {}) : Promise.resolve({ success: true })));
    await act(async () => { vi.advanceTimersByTime(15000); });
    expect(callsWith(true)).toBe(2);
    await act(async () => { vi.advanceTimersByTime(45000); });
    expect(callsWith(true)).toBe(2);
  });

  it('sends no keepalive while „Arm festsetzen" is on its way (it would free the arm again)', async () => {
    vi.useFakeTimers();
    await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    mockHandGuide.mockImplementation((on) => (on ? Promise.resolve({ success: true }) : new Promise(() => {})));
    fireEvent.click(screen.getByRole('button', { name: 'Arm festsetzen' }));
    await flush();
    await act(async () => { vi.advanceTimersByTime(45000); });
    expect(callsWith(true)).toBe(1);
    expect(callsWith(false)).toBe(1);
  });

  it('sends no keepalive while the card is disabled (the bridge is gone)', async () => {
    vi.useFakeTimers();
    const { rerender } = await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    mockState.tasks.heartbeatStatus = 'disconnected';
    rerender(<ActivationCard />);
    await flush();
    await act(async () => { vi.advanceTimersByTime(45000); });
    expect(callsWith(true)).toBe(1);
    // The session is still on screen: the student can lock the arm.
    expect(screen.getByRole('button', { name: 'Arm festsetzen' })).toBeInTheDocument();
  });

  it('locks the arm on unmount', async () => {
    const { unmount } = await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    expect(callsWith(false)).toBe(0);
    unmount();
    expect(mockHandGuide).toHaveBeenLastCalledWith(false);
    expect(callsWith(false)).toBe(1);
  });

  it('locks the arm when the page goes away (pagehide)', async () => {
    await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    act(() => { window.dispatchEvent(new Event('pagehide')); });
    expect(callsWith(false)).toBe(1);
  });

  it('never activates while the arm is limp or the verdict is not clear', async () => {
    await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    // The student laid it forward: the driver now says clear, but the arm is
    // still limp — the panel stays and the button stays held back.
    await deliver('clear', 'forward');
    expect(screen.getByRole('button', { name: 'Arm festsetzen' })).toBeInTheDocument();
    expect(activateButton()).toBeDisabled();
    fireEvent.click(activateButton());
    expect(activate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Arm festsetzen' }));
    await flush();
    await flush();
    expect(mockHandGuide).toHaveBeenLastCalledWith(false);
    // Locked and clear: today's card, and now the activation runs.
    expect(screen.queryByRole('button', { name: 'Arm festsetzen' })).toBeNull();
    expect(screen.queryByText(TOWER_HINT_ACTIVATE_DE)).toBeNull();
    expect(activateButton()).not.toBeDisabled();
    fireEvent.click(activateButton());
    expect(activate).toHaveBeenCalledTimes(1);
  });

  it('locked again but still lying back: the hint and „Arm lockern" come back', async () => {
    await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    fireEvent.click(screen.getByRole('button', { name: 'Arm festsetzen' }));
    await flush();
    await flush();
    expect(screen.getByText(TOWER_HINT_ACTIVATE_DE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Arm lockern' })).toBeInTheDocument();
    expect(activateButton()).toBeDisabled();
  });

  it('a refused „lockern" says why and frees nothing', async () => {
    vi.useFakeTimers();
    mockHandGuide.mockImplementation(() => Promise.resolve({
      success: false, message: 'Arm konnte nicht freigeschaltet werden — bitte die Umgebung neu starten.',
    }));
    await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    expect(screen.getByText(/Arm konnte nicht freigeschaltet werden/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Arm festsetzen' })).toBeNull();
    await act(async () => { vi.advanceTimersByTime(45000); });
    expect(callsWith(true)).toBe(1);
  });

  it('a refused „festsetzen" (the edge refusal) keeps the session and says why', async () => {
    await renderCard({ status: IDLE });
    await deliver('refused');
    await freeArm();
    mockHandGuide.mockImplementation((on) => Promise.resolve(on
      ? { success: true }
      : { success: false, message: 'Bitte das Gelenk von Hand ein Stück Richtung Mitte zurückdrehen (etwa 10° genügen).' }));
    fireEvent.click(screen.getByRole('button', { name: 'Arm festsetzen' }));
    await flush();
    await flush();
    expect(screen.getByText(/Richtung Mitte zurückdrehen/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Arm festsetzen' })).toBeInTheDocument();
    expect(activateButton()).toBeDisabled();
  });

  it.each([
    ['a recording runs', { running: true }],
    ['a program runs', { workflowRunning: true }],
    ['the robot is being activated', { calling: true }],
  ])('„Arm lockern" is refused while %s', async (_label, extra) => {
    await renderCard({ status: IDLE, ...extra });
    await deliver('refused');
    expect(screen.getByRole('button', { name: 'Arm lockern' })).toBeDisabled();
  });
});
