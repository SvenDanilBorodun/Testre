/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The V1's home-glide prompt and the camera tower (spec 40 WP-HINT, 52 §5.3
// (b); decisions D56, D88).
//
// The server publishes on /edubotics/home_glide_state what a press of „Jetzt
// fahren" would get from the live pose (WP-12: the tower guard as a rung
// condition of the same planner). While that says `refused`, the prompt counts
// nothing down and offers no glide: it shows the animation, the sentence and
// „Hier stehen lassen" only. `clear` or no topic → today's prompt. The OMX
// prompt never subscribes (D1: nothing new for any other profile).

import React, { useEffect } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';

import { HOME_GLIDE_COUNTDOWN_S, HomeGlideProvider, useHomeGlide } from '../HomeGlidePrompt';

const TOWER_HINT_HOME_DE = 'Von hier würde die Fahrt in die Grundstellung dem Kameraturm zu nahe '
  + 'kommen. Bring den Arm erst mit „Steuern" nach vorne – dann geht die Grundstellung.';
const HOME_GLIDE_STATE_TOPIC = '/edubotics/home_glide_state';

const mockRos = vi.hoisted(() => ({
  homeArm: vi.fn(),
  handGuide: vi.fn(() => Promise.resolve({ success: true })),
}));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

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

function makeStore() {
  return configureStore({
    reducer: { ros: (state = { rosbridgeUrl: 'ws://localhost/rosbridge', rosHost: '', imageTopicList: [] }) => state },
  });
}

// A caller that offers the glide on a click (like „Arm festsetzen" in JogPanel).
function Offerer() {
  const { offerHomeGlide, homeGlideDialog } = useHomeGlide();
  return (
    <div>
      <button type="button" onClick={offerHomeGlide}>anbieten</button>
      {homeGlideDialog}
    </div>
  );
}

function OfferOnMount() {
  const { offerHomeGlide } = useHomeGlide();
  useEffect(() => { offerHomeGlide(); }, [offerHomeGlide]);
  return null;
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve(); // eslint-disable-line no-await-in-loop
  });
}

async function advance(ms) {
  for (let left = ms; left > 0; left -= 1000) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { vi.advanceTimersByTime(Math.min(1000, left)); });
  }
}

const glideTopics = () => mockTopics.filter((t) => t.opts.name === HOME_GLIDE_STATE_TOPIC);
let gen = 0;
async function deliver(glide) {
  const live = glideTopics().filter((t) => !t.unsubscribed);
  expect(live).toHaveLength(1);
  gen += 1;
  await act(async () => { live[0].cb({ data: JSON.stringify({ v: 1, glide, gen }) }); });
}

async function renderPage(robotProfile, child = <Offerer />) {
  const view = render(
    <Provider store={makeStore()}>
      <HomeGlideProvider robotProfile={robotProfile}>{child}</HomeGlideProvider>
    </Provider>,
  );
  await flush();
  return view;
}

const offer = () => fireEvent.click(screen.getByRole('button', { name: 'anbieten' }));

beforeEach(() => {
  vi.useFakeTimers();
  mockTopics.length = 0;
  gen = 0;
  mockRos.homeArm.mockReset();
  mockRos.homeArm.mockImplementation(() => Promise.resolve({ success: true, message: 'Der Arm steht in der Grundstellung.' }));
  mockRos.handGuide.mockClear();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('HomeGlidePrompt — the V1 under the camera tower', () => {
  it('refused: no countdown, no „Jetzt fahren", only „Hier stehen lassen" with the animation', async () => {
    await renderPage('v1_studio');
    await deliver('refused');
    offer();
    const dialog = screen.getByRole('alertdialog');
    expect(dialog).toBeInTheDocument();
    expect(screen.getByText(TOWER_HINT_HOME_DE)).toBeInTheDocument();
    expect(screen.queryByRole('timer')).toBeNull();
    expect(screen.queryByText(/Start in/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Jetzt fahren' })).toBeNull();
    const pic = document.querySelector('[data-tower-hint="forward"]'); // eslint-disable-line testing-library/no-node-access
    expect(pic).not.toBeNull();
    await advance(HOME_GLIDE_COUNTDOWN_S * 4000);
    expect(mockRos.homeArm).not.toHaveBeenCalled();
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    // „Hier stehen lassen" is the one way out, and it moves nothing.
    expect(screen.getByRole('button', { name: 'Hier stehen lassen' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Hier stehen lassen' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(mockRos.homeArm).not.toHaveBeenCalled();
    expect(mockToast).toHaveBeenCalledWith('Der Arm bleibt an seiner Position.', expect.anything());
  });

  it('refused while the warning counts down: the countdown stops before it can glide', async () => {
    await renderPage('v1_studio');
    await deliver('clear');
    offer();
    await advance(2000);
    expect(screen.getByText(`Start in ${HOME_GLIDE_COUNTDOWN_S - 2} Sekunden`)).toBeInTheDocument();
    await deliver('refused');
    expect(screen.queryByRole('button', { name: 'Jetzt fahren' })).toBeNull();
    await advance(HOME_GLIDE_COUNTDOWN_S * 3000);
    expect(mockRos.homeArm).not.toHaveBeenCalled();
  });

  it('a refused verdict that clears again restarts the FULL warning', async () => {
    await renderPage('v1_studio');
    await deliver('refused');
    offer();
    await advance(3000);
    await deliver('clear');
    expect(screen.getByText(`Start in ${HOME_GLIDE_COUNTDOWN_S} Sekunden`)).toBeInTheDocument();
    await advance((HOME_GLIDE_COUNTDOWN_S - 1) * 1000);
    expect(mockRos.homeArm).not.toHaveBeenCalled();
    await advance(1000);
    expect(mockRos.homeArm).toHaveBeenCalledTimes(1);
  });

  it('clear: today\'s prompt (countdown, „Jetzt fahren")', async () => {
    await renderPage('v1_studio');
    await deliver('clear');
    offer();
    expect(screen.getByText(`Start in ${HOME_GLIDE_COUNTDOWN_S} Sekunden`)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Jetzt fahren' })).toBeInTheDocument();
    expect(screen.queryByText(TOWER_HINT_HOME_DE)).toBeNull();
    await advance(HOME_GLIDE_COUNTDOWN_S * 1000);
    expect(mockRos.homeArm).toHaveBeenCalledTimes(1);
  });

  it('unjudgeable is not the tower\'s refusal: today\'s prompt, and the press says why', async () => {
    await renderPage('v1_studio');
    await deliver('unjudgeable');
    offer();
    expect(screen.getByRole('button', { name: 'Jetzt fahren' })).toBeInTheDocument();
  });

  it('no topic (legacy camera, an older image): today\'s prompt', async () => {
    await renderPage('v1_studio');
    offer();
    expect(screen.getByText(`Start in ${HOME_GLIDE_COUNTDOWN_S} Sekunden`)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Jetzt fahren' })).toBeInTheDocument();
  });

  it('the subscription is the provider\'s own and ends with it', async () => {
    const { unmount } = await renderPage('v1_studio');
    expect(glideTopics()).toHaveLength(1);
    expect(glideTopics()[0].opts).toMatchObject({ messageType: 'std_msgs/msg/String', queue_length: 1 });
    unmount();
    expect(glideTopics().every((t) => t.unsubscribed)).toBe(true);
  });
});

describe('HomeGlidePrompt — every other profile never subscribes (D1)', () => {
  it.each([['omx_full'], ['omx_follower'], [null], ['']])('%j', async (profile) => {
    await renderPage(profile, <><Offerer /><OfferOnMount /></>);
    expect(glideTopics()).toHaveLength(0);
    expect(mockTopics).toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Jetzt fahren' })).toBeInTheDocument();
  });
});
