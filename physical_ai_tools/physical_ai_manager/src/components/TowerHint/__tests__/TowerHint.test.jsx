/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The V1's camera-tower hint pieces (spec 40 WP-HINT, 52 §5.3–§5.5): the
// animation (WP-ART's render, reduced motion on its last, safe frame), the
// Startseite's freeing panel and the stop dialog, and the copy (D88: forward
// only, every string German).

import fs from 'fs';
import path from 'path';
import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';

import TowerHint, { TOWER_KEEPALIVE_MS, TowerActivationHint, TowerStopDialog } from '../TowerHint';
import * as copy from '../towerHintCopy';

const mockHandGuide = vi.hoisted(() => vi.fn());
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({ handGuide: mockHandGuide }),
}));

function mockReducedMotion(matches) {
  window.matchMedia = vi.fn(() => ({
    matches, addEventListener: () => {}, removeEventListener: () => {},
  }));
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve(); // eslint-disable-line no-await-in-loop
  });
}

const strip = () => document.querySelector('.tower-hint__strip'); // eslint-disable-line testing-library/no-node-access
const hint = () => document.querySelector('[data-tower-hint]'); // eslint-disable-line testing-library/no-node-access

const originalMatchMedia = window.matchMedia;
beforeEach(() => {
  mockHandGuide.mockReset();
  mockHandGuide.mockImplementation(() => Promise.resolve({ success: true }));
});
afterEach(() => {
  cleanup();
  window.matchMedia = originalMatchMedia;
  vi.useRealTimers();
});

describe('TowerHint — the animation', () => {
  it.each([
    ['forward', false, 'v1_tower_hint_forward.webp'],
    ['forward', true, 'v1_tower_hint_forward_light.webp'],
    ['fall', false, 'v1_tower_hint_fall.webp'],
    ['fall', true, 'v1_tower_hint_fall_light.webp'],
  ])('%s (light %s) draws %s, decorative', (scene, light, file) => {
    render(<TowerHint profile="v1_studio" scene={scene} light={light} />);
    expect(hint().getAttribute('data-tower-hint')).toBe(scene);
    expect(hint().getAttribute('data-robot-picture')).toBe(file);
    expect(hint().getAttribute('aria-hidden')).toBe('true');
    expect(strip().getAttribute('src')).toBe(`/robots/${file}`);
    expect(strip().getAttribute('alt')).toBe('');
  });

  it.each([['omx_full'], ['omx_follower'], [''], [null], ['v1']])('profile %j draws nothing (the tower is the V1\'s)', (profile) => {
    const { container } = render(<TowerHint profile={profile} scene="forward" />);
    expect(container.innerHTML).toBe('');
  });

  it('reduced motion: the last frame (the safe pose), standing still', () => {
    mockReducedMotion(true);
    render(<TowerHint profile="v1_studio" scene="forward" />);
    expect(hint().className).toContain('tower-hint--still');
    expect(strip().style.animation).toBe('none');
    // Frame 23 of 24: the strip moved by 23/24 of its own width.
    const shift = Number(/translateX\(-([\d.]+)%\)/.exec(strip().style.transform)[1]);
    expect(shift).toBeCloseTo((23 / 24) * 100, 6);
  });

  it('full motion: the CSS steps through the strip', () => {
    mockReducedMotion(false);
    render(<TowerHint profile="v1_studio" scene="forward" />);
    expect(hint().className).not.toContain('tower-hint--still');
    expect(strip().getAttribute('style')).toBeNull();
  });

  it('the CSS: the plain style is the last frame, reduced motion stops the loop', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../towerHint.css'), 'utf8');
    // 23 / 24 of the strip: frame index 23, the safe pose — also what shows
    // wherever the animation does not run.
    expect(css).toMatch(/\.tower-hint__strip \{[^}]*transform: translateX\(-95\.8333%\);/);
    expect(css).toMatch(/animation-timing-function: steps\(23, end\);/);
    expect(css).toMatch(/72%, 100% \{ transform: translateX\(-95\.8333%\); \}/);
    expect(css).toContain('.tower-hint--still .tower-hint__strip { animation: none; }');
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\) \{\s*\.tower-hint__strip \{ animation: none; \}/);
  });
});

describe('the copy', () => {
  const strings = Object.entries(copy).filter(([, v]) => typeof v === 'string');

  it('the three hint sentences are the spec\'s, word for word (D88: forward only)', () => {
    expect(copy.TOWER_HINT_ACTIVATE_DE).toBe('Der Arm liegt so, dass er beim Aktivieren dem Kameraturm zu '
      + "nahe käme. Lockere ihn, lege ihn nach vorne und setze ihn wieder fest – dann geht's los.");
    expect(copy.TOWER_HINT_HOLD_DE).toBe('Halte den Arm mit einer Hand – gleich wird er weich.');
    expect(copy.TOWER_HINT_HOME_DE).toBe('Von hier würde die Fahrt in die Grundstellung dem Kameraturm zu '
      + 'nahe kommen. Bring den Arm erst mit „Steuern" nach vorne – dann geht die Grundstellung.');
    expect(copy.TOWER_HINT_STOP_DE).toBe('Beim Stoppen wird der Arm weich. Aus dieser Lage würde er nach '
      + 'hinten zum Kameraturm fallen. Fahre ihn zuerst in die Grundstellung (Roboter Studio → Steuern → '
      + 'Grundstellung) oder halte ihn beim Stoppen fest.');
  });

  it('no string tells the student to stand the arm up (D88)', () => {
    for (const [, text] of strings) expect(text).not.toMatch(/aufricht|aufstell|stell ihn auf|richte ihn auf/i);
  });

  it('every string is non-empty German text with literal umlauts, never a transliteration', () => {
    expect(strings.length).toBeGreaterThan(10);
    for (const [name, text] of strings) {
      expect(name).toMatch(/_DE$/);
      expect(text.trim()).not.toBe('');
      expect(text).not.toMatch(/\b(waere|wuerde|kaeme|druecke|laeuft|zurueck|fuer|ueber|Kameraturm zu nahe kaeme)\b/);
    }
  });
});

describe('TowerActivationHint — the Startseite\'s freeing panel', () => {
  const callsWith = (on) => mockHandGuide.mock.calls.filter((c) => c[0] === on).length;

  async function free() {
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_FREE_DE }));
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_HOLDING_DE }));
    await flush();
  }

  it('shows the cream arm on the dark screen and the activation sentence', () => {
    render(<TowerActivationHint profile="v1_studio" />);
    expect(hint().getAttribute('data-robot-picture')).toBe('v1_tower_hint_forward_light.webp');
    expect(screen.getByText(copy.TOWER_HINT_ACTIVATE_DE)).toBeInTheDocument();
  });

  it('reports itself active from the first click until the arm is locked again', async () => {
    const onActiveChange = vi.fn();
    render(<TowerActivationHint profile="v1_studio" onActiveChange={onActiveChange} />);
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_FREE_DE }));
    expect(onActiveChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_CANCEL_DE }));
    expect(onActiveChange).toHaveBeenLastCalledWith(false);
    await free();
    expect(onActiveChange).toHaveBeenLastCalledWith(true);
    expect(screen.getByText(copy.TOWER_HINT_LIMP_DE)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_LOCK_DE }));
    await flush();
    expect(onActiveChange).toHaveBeenLastCalledWith(false);
  });

  it('the keepalive period is JogPanel\'s 15 s, well inside the server\'s 30 s re-torque', () => {
    expect(TOWER_KEEPALIVE_MS).toBe(15000);
  });

  it('„festsetzen" during a keepalive in flight waits for it, so the arm cannot be freed again after the lock', async () => {
    vi.useFakeTimers();
    render(<TowerActivationHint profile="v1_studio" />);
    await free();
    let answerKeepalive;
    mockHandGuide.mockImplementation((on) => (on
      ? new Promise((r) => { answerKeepalive = r; })
      : Promise.resolve({ success: true })));
    await act(async () => { vi.advanceTimersByTime(TOWER_KEEPALIVE_MS); });
    expect(callsWith(true)).toBe(2);
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_LOCK_DE }));
    await flush();
    expect(callsWith(false)).toBe(0);
    await act(async () => { answerKeepalive({ success: true }); });
    expect(callsWith(false)).toBe(1);
    expect(mockHandGuide.mock.calls[mockHandGuide.mock.calls.length - 1][0]).toBe(false);
  });

  it('unmounted while „lockern" is on its way: locks again once it answers', async () => {
    let answer;
    mockHandGuide.mockImplementation((on) => (on
      ? new Promise((r) => { answer = r; })
      : Promise.resolve({ success: true })));
    const { unmount } = render(<TowerActivationHint profile="v1_studio" />);
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_FREE_DE }));
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_HOLDING_DE }));
    await flush();
    unmount();
    expect(callsWith(false)).toBe(1);
    await act(async () => { answer({ success: true }); });
    expect(callsWith(false)).toBe(2);
  });

  it('a robot that cannot be reached says so in German', async () => {
    mockHandGuide.mockImplementation(() => Promise.reject(new Error('Service call timeout for /workshop/hand_guide')));
    render(<TowerActivationHint profile="v1_studio" />);
    await free();
    expect(screen.getByText(copy.TOWER_UNREACHABLE_DE)).toBeInTheDocument();
    expect(screen.queryByText(/timeout/)).toBeNull();
    expect(screen.getByRole('button', { name: copy.TOWER_FREE_DE })).toBeInTheDocument();
  });

  it('the robot\'s log tags never reach the student', async () => {
    mockHandGuide.mockImplementation(() => Promise.resolve({ success: false, message: '[FEHLER] Arm konnte nicht freigeschaltet werden.' }));
    render(<TowerActivationHint profile="v1_studio" />);
    await free();
    expect(screen.getByText('Arm konnte nicht freigeschaltet werden.')).toBeInTheDocument();
  });
});

describe('TowerStopDialog', () => {
  it('closed: nothing', () => {
    render(<TowerStopDialog open={false} profile="v1_studio" onCancel={() => {}} onConfirm={() => {}} />);
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('open: the limp-fall strip, the sentence, two choices, focus on „Abbrechen"', () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(<TowerStopDialog open profile="v1_studio" onCancel={onCancel} onConfirm={onConfirm} />);
    const dialog = screen.getByRole('alertdialog');
    expect(dialog).toHaveAccessibleName(copy.TOWER_HINT_STOP_TITLE_DE);
    expect(dialog).toHaveAccessibleDescription(copy.TOWER_HINT_STOP_DE);
    expect(hint().getAttribute('data-robot-picture')).toBe('v1_tower_hint_fall.webp');
    expect(screen.getByRole('button', { name: copy.TOWER_CANCEL_DE })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_CANCEL_DE }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: copy.TOWER_STOP_ANYWAY_DE }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
