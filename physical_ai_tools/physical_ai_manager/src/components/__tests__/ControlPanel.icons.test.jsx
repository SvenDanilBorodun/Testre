// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Review round 1: the recording ControlPanel's icons.
//   * B2 — Start and Stopp are SOLID (the fill carried meaning), like the run
//     bar's and Vormachen's; „Aufnahme läuft" shows the filled red dot.
//   * R1-O1 — the Braille spinner is gone; the loader icon turns by ONE step
//     per /task/status message and never spins on its own.

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import tasksReducer, { setTaskStatus } from '../../features/tasks/taskSlice';
import uiReducer from '../../features/ui/uiSlice';
import rosReducer from '../../features/ros/rosSlice';
import TaskPhase from '../../constants/taskPhases';
import PageType from '../../constants/pageType';
import ControlPanel from '../ControlPanel';

vi.mock('../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({ sendRecordCommand: vi.fn() }),
}));

function mount(status = {}) {
  const store = configureStore({
    reducer: { tasks: tasksReducer, ui: uiReducer, ros: rosReducer },
    preloadedState: {
      ui: { ...uiReducer(undefined, { type: '@@INIT' }), currentPage: PageType.RECORD },
    },
  });
  act(() => { store.dispatch(setTaskStatus({ phase: TaskPhase.RECORDING, running: true, ...status })); });
  render(<Provider store={store}><ControlPanel /></Provider>);
  return store;
}

const iconIn = (button) => button.querySelector('svg[data-icon]'); // eslint-disable-line testing-library/no-node-access

test('Start and Stopp draw solid media icons', () => {
  mount();
  const start = iconIn(screen.getByRole('button', { name: /Start/ }));
  const stop = iconIn(screen.getByRole('button', { name: /Stopp/ }));
  expect(start.getAttribute('data-icon')).toBe('play');
  expect(stop.getAttribute('data-icon')).toBe('stop');
  expect(start.getAttribute('fill')).toBe('currentColor');
  expect(stop.getAttribute('fill')).toBe('currentColor');
});

test('„Aufnahme läuft" carries the filled red recording dot', () => {
  mount();
  const line = screen.getByText('Aufnahme läuft').parentElement; // eslint-disable-line testing-library/no-node-access
  const dot = line.querySelector('svg[data-icon="liveRecording"]'); // eslint-disable-line testing-library/no-node-access
  expect(dot).not.toBeNull();
  expect(dot.getAttribute('fill')).toBe('currentColor');
  expect(dot.getAttribute('class')).toMatch(/text-red-500/);
});

test('the liveness icon turns one step per status message, never on its own', () => {
  const store = mount();
  const indicator = screen.getByTestId('task-liveness');
  const svg = () => indicator.querySelector('svg[data-icon="loading"]'); // eslint-disable-line testing-library/no-node-access
  expect(svg()).not.toBeNull();
  // No CSS animation: it moves only with the messages.
  expect(svg().getAttribute('class')).not.toMatch(/animate-/);
  const angle = () => Number(/rotate\((-?[\d.]+)deg\)/.exec(svg().style.transform)[1]);
  const first = angle();
  act(() => { store.dispatch(setTaskStatus({ phase: TaskPhase.RECORDING, running: true, proceedTime: 1 })); });
  expect(angle()).toBe((first + 45) % 360);
  act(() => { store.dispatch(setTaskStatus({ phase: TaskPhase.RECORDING, running: true, proceedTime: 2 })); });
  expect(angle()).toBe((first + 90) % 360);
  // No message, no movement.
  expect(angle()).toBe((first + 90) % 360);
  // No glyph of the old Braille spinner anywhere.
  expect(document.body.textContent).not.toMatch(/[⠀-⣿]/);
});

// Final review, minor 1: a disabled button dims its icon with OPACITY. A
// semi-transparent colour painted a solid icon's fill AND stroke at 35 %, and
// the overlap drew a bright ring round a dimmed Stopp/Weiter.
test('a disabled button dims its icon by opacity, never by a translucent colour', () => {
  mount({ phase: TaskPhase.READY, running: false });
  const stop = iconIn(screen.getByRole('button', { name: /Stopp/ }));
  expect(screen.getByRole('button', { name: /Stopp/ })).toBeDisabled();
  expect(stop.style.opacity).toBe('0.35');
  expect(stop.style.color).not.toMatch(/rgba/);
});
