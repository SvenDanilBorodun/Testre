/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/* eslint-disable testing-library/no-container, testing-library/no-node-access --
   these tests pin markup: data attributes of the robot pictures. */

// The collision e-stop modal's pictures are renders of the REAL robot (its URDF,
// by profile id), never a hand-drawn arm: the remedy picture after two failed
// homing attempts, and the arm glyph of step 2. An unknown profile draws no
// robot at all.

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import CollisionModal from '../CollisionModal';
import { PHASE_PICTURES } from '../icons/robotAssets';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));
const mockRos = vi.hoisted(() => ({
  homeFollower: vi.fn(() => Promise.resolve({ success: true, message: '' })),
  resumeTeleop: vi.fn(),
  forceResumeTeleop: vi.fn(),
}));
vi.mock('../../hooks/useRosServiceCaller', () => ({ __esModule: true, useRosServiceCaller: () => mockRos }));
vi.mock('react-hot-toast', () => ({ __esModule: true, default: { error: vi.fn() } }));

const state = (stage, robotProfile) => ({
  tasks: { collision: { active: true, stage, message: '', jointDistToHome: [] }, taskStatus: { robotProfile } },
});

const failTwice = async () => {
  const home = screen.getByRole('button', { name: 'Follower in Grundstellung fahren' });
  fireEvent.click(home);
  await waitFor(() => expect(mockRos.homeFollower).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(home).not.toBeDisabled());
  fireEvent.click(home);
  await waitFor(() => expect(mockRos.homeFollower).toHaveBeenCalledTimes(2));
};

beforeEach(() => {
  mockRos.homeFollower.mockClear();
});

describe('CollisionModal — the remedy picture', () => {
  it('after two failed homing attempts shows the real follower render, with the arrow as a registry icon', async () => {
    mockState = state('stopped', 'omx_full');
    const { container } = render(<CollisionModal />);
    expect(container.querySelector('[data-robot-picture]')).toBeNull();
    await failTwice();
    const pic = screen.getByRole('img', { name: 'Hindernis aus dem Bewegungsbereich des Arms entfernen' });
    expect(pic).toHaveAttribute('data-robot-picture', PHASE_PICTURES.omx_full.collision.file);
    expect(pic.querySelector('img').getAttribute('src')).toBe(`/robots/${PHASE_PICTURES.omx_full.collision.file}`);
    expect(pic.querySelector('svg[data-icon="arrowRight"]')).not.toBeNull();
    // No illustration is drawn by hand any more: every svg left is a registry icon.
    expect(container.querySelector('svg:not([data-icon])')).toBeNull();
  });

  it('the arrow sits between the obstacle and its ghost, pointing from the one to the other', async () => {
    mockState = state('stopped', 'omx_full');
    render(<CollisionModal />);
    await failTwice();
    const { obstacle, ghost } = PHASE_PICTURES.omx_full.collision.anchors;
    const arrow = screen.getByRole('img', { name: /Hindernis/ }).querySelector('span'); // eslint-disable-line testing-library/no-node-access
    expect(parseFloat(arrow.style.left)).toBeCloseTo(((obstacle[0] + ghost[0]) / 2) * 100, 3);
    expect(parseFloat(arrow.style.top)).toBeCloseTo(((obstacle[1] + ghost[1]) / 2) * 100, 3);
    expect(arrow.style.transform).toContain('rotate(');
  });

  it.each(['', 'omx_follower', 'v1_studio'])(
    'a profile with no collision picture (%j) shows none — the advice text stays',
    async (profile) => {
      mockState = state('stopped', profile);
      const { container } = render(<CollisionModal />);
      await failTwice();
      expect(container.querySelector('[data-robot-picture]')).toBeNull();
      expect(screen.getByText(/Hindernis ganz aus dem Bewegungsbereich nehmen/)).toBeInTheDocument();
    }
  );
});

describe('CollisionModal — the header icon', () => {
  it('step 1 is the warning icon, never a robot', () => {
    mockState = state('stopped', 'omx_full');
    const { container } = render(<CollisionModal />);
    expect(container.querySelector('svg[data-icon="warning"]')).not.toBeNull();
    expect(container.querySelector('[data-robot-glyph]')).toBeNull();
  });

  it('step 2 draws the real arm of the robot', () => {
    mockState = state('homed', 'omx_full');
    const { container } = render(<CollisionModal />);
    expect(container.querySelector('[data-robot-glyph="omx_f"]')).not.toBeNull();
    expect(container.querySelector('svg[data-icon="robotArm"]')).toBeNull();
  });

  it('step 2 with the profile unknown falls back to the warning icon, never another robot', () => {
    mockState = state('homed', '');
    const { container } = render(<CollisionModal />);
    expect(container.querySelector('[data-robot-glyph]')).toBeNull();
    expect(container.querySelector('svg[data-icon="warning"]')).not.toBeNull();
  });
});
