// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The page layout (spec §3.12, review V2-1): the page is the `rec` size
// container; the problem banner and the action bar are ONE footer group, a
// FLOW sibling after the stage (never absolute or fixed); the narrow layout
// (≤ 1150 px of page) makes that group sticky at the bottom of its column, so
// the banner can never slide under the bar; a stage card centres its content
// safely (its top is always reachable); and the phase colours are defined on
// the page. Read from record.css itself (jsdom computes no container queries) and
// from the rendered DOM order.

import fs from 'fs';
import path from 'path';
import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

import tasksReducer, { setHeartbeatStatus, setTaskStatus } from '../../../features/tasks/taskSlice';
import uiReducer from '../../../features/ui/uiSlice';
import rosReducer from '../../../features/ros/rosSlice';
import trainingReducer from '../../../features/training/trainingSlice';
import jetsonReducer from '../../../store/jetsonSlice';
import TaskPhase from '../../../constants/taskPhases';
import RecordPage from '../../../pages/RecordPage';

vi.mock('../../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({
    sendRecordCommand: vi.fn(),
    getRegisteredHFUser: () => Promise.resolve({ success: true, user_id_list: [] }),
    getImageTopicList: () => Promise.resolve({ success: true, image_topic_list: [] }),
  }),
}));
vi.mock('../../../hooks/useSignalStatus', () => ({ __esModule: true, default: () => ({ payload: null, receivedAt: null }) }));
vi.mock('../../../hooks/useRsBridgeStatus', () => ({ __esModule: true, default: () => ({ available: false, probed: true }) }));
vi.mock('../../../hooks/useRobotActivation', () => ({ __esModule: true, default: () => ({ status: null }) }));
vi.mock('../../HeartbeatStatus', () => ({ __esModule: true, default: () => null }));
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.dismiss = vi.fn();
  fn.error = vi.fn();
  return { __esModule: true, default: fn, useToasterStore: () => ({ toasts: [] }) };
});

const CSS = fs.readFileSync(path.resolve(__dirname, '../record.css'), 'utf8');

// The declarations of the first rule whose selector is exactly `selector`.
function rule(css, selector) {
  const re = new RegExp(`(^|\\n)\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`);
  const m = re.exec(css);
  return m ? m[2] : null;
}
// The body of an @container block.
function containerBlock(css, query) {
  const at = css.indexOf(`@container ${query}`);
  if (at < 0) return null;
  let depth = 0;
  for (let i = css.indexOf('{', at); i < css.length; i += 1) {
    if (css[i] === '{') depth += 1;
    if (css[i] === '}') { depth -= 1; if (depth === 0) return css.slice(css.indexOf('{', at) + 1, i); }
  }
  return null;
}

describe('record.css', () => {
  it('the page is the `rec` inline-size container and defines the four phase colours', () => {
    const page = rule(CSS, '.rec-page');
    expect(page).toMatch(/container-type:\s*inline-size/);
    expect(page).toMatch(/container-name:\s*rec/);
    ['--rec-warm: #D08A1E', '--rec-run: #E23B3B', '--rec-save: #2F9E63', '--rec-reset: #0A7F7A']
      .forEach((token) => expect(page).toContain(token));
  });

  it('wide: stage + 360 px column', () => {
    expect(rule(CSS, '.rec-grid')).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)\s+360px/);
  });

  it('neither the action bar nor the footer is ever absolute or fixed, and the bar is never sticky on its own', () => {
    const bars = [...CSS.matchAll(/\.rec-actionbar\s*\{([^}]*)\}/g)].map((m) => m[1]);
    expect(bars.length).toBeGreaterThan(0);
    bars.forEach((b) => expect(b).not.toMatch(/position:\s*(absolute|fixed|sticky)/));
    const footers = [...CSS.matchAll(/\.rec-footer\s*\{([^}]*)\}/g)].map((m) => m[1]);
    expect(footers.length).toBeGreaterThan(0);
    footers.forEach((b) => expect(b).not.toMatch(/position:\s*(absolute|fixed)/));
  });

  it('narrow (≤ 1150 px of page): one column, a 470 px stage at most, banner + bar sticky together', () => {
    const narrow = containerBlock(CSS, 'rec (max-width: 1150px)');
    expect(narrow).not.toBeNull();
    expect(narrow).toMatch(/\.rec-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
    expect(narrow).toMatch(/\.rec-stage\s*\{[^}]*470px/);
    expect(narrow).toMatch(/\.rec-footer\s*\{[^}]*position:\s*sticky[^}]*bottom:\s*0[^}]*background:\s*var\(--bg\)/);
    expect(narrow).toMatch(/\.rec-main\s*\{[^}]*overflow:\s*auto/);
    // A banner takes room from the stage, not from under the footer.
    expect(narrow).toMatch(/\.rec-left:has\(> \.rec-footer > \.rec-banner\) > \.rec-stage\s*\{[^}]*height:/);
    // The finish card joins the flow and the stage grows with it.
    expect(narrow).toMatch(/\.rec-finishcard\s*\{[^}]*position:\s*relative/);
    expect(narrow).toMatch(/\.rec-stage:has\(> \.rec-finishcard\)\s*\{[^}]*height:\s*auto/);
  });

  it('a stage card centres its content safely: its top never leaves the card', () => {
    const card = rule(CSS, '.rec-statecard');
    expect(card).not.toMatch(/justify-content:\s*center/);
    expect(card).toMatch(/overflow:\s*auto/);
    expect(rule(CSS, '.rec-statecard > :first-child')).toMatch(/margin-top:\s*auto/);
    expect(rule(CSS, '.rec-statecard > :last-child')).toMatch(/margin-bottom:\s*auto/);
  });
});

describe('the rendered page', () => {
  it('the stage, then ONE footer with the banner slot and the action bar, in the left column; the cards on the right', () => {
    const store = configureStore({
      reducer: { tasks: tasksReducer, ui: uiReducer, ros: rosReducer, training: trainingReducer, jetson: jetsonReducer },
    });
    store.dispatch(setHeartbeatStatus('connected'));
    store.dispatch(setTaskStatus({ phase: TaskPhase.READY, running: false, topicReceived: true, robotType: 'omx_f' }));
    render(<Provider store={store}><RecordPage /></Provider>);

    const pageEl = screen.getByTestId('rec-page');
    expect(pageEl).toHaveClass('rec-page');
    const stage = screen.getByTestId('rec-stage');
    const barEl = screen.getByTestId('rec-actionbar');
    const left = stage.parentElement; // eslint-disable-line testing-library/no-node-access
    expect(left).toHaveClass('rec-left');
    const footer = screen.getByTestId('rec-footer');
    expect(footer.parentElement).toBe(left); // eslint-disable-line testing-library/no-node-access
    expect(barEl.parentElement).toBe(footer); // eslint-disable-line testing-library/no-node-access
    const kids = [...left.children]; // eslint-disable-line testing-library/no-node-access
    expect(kids).toEqual([stage, footer]);
    const inFooter = [...footer.children]; // eslint-disable-line testing-library/no-node-access
    expect(inFooter[inFooter.length - 1]).toBe(barEl);
    const right = screen.getByTestId('rec-task-card').parentElement; // eslint-disable-line testing-library/no-node-access
    expect(right).toHaveClass('rec-right');
    expect(right).toContainElement(screen.getByTestId('rec-session-card'));
    expect(barEl.style.position).toBe('');
  });
});

describe('the narrow page scrolls back to the stage when a session starts', () => {
  it('Start from a scrolled page brings .rec-main to the top; staying in READY does not', () => {
    const store = configureStore({
      reducer: { tasks: tasksReducer, ui: uiReducer, ros: rosReducer, training: trainingReducer, jetson: jetsonReducer },
    });
    store.dispatch(setHeartbeatStatus('connected'));
    store.dispatch(setTaskStatus({ phase: TaskPhase.READY, running: false, topicReceived: true, robotType: 'omx_f' }));
    render(<Provider store={store}><RecordPage /></Provider>);
    const main = screen.getByTestId('rec-page').firstChild; // eslint-disable-line testing-library/no-node-access
    main.scrollTo = vi.fn();
    main.scrollTop = 400;
    act(() => {
      store.dispatch(setTaskStatus({ phase: TaskPhase.READY, running: false, topicReceived: true, usedCpu: 1 }));
    });
    expect(main.scrollTo).not.toHaveBeenCalled();
    act(() => {
      store.dispatch(setTaskStatus({
        phase: TaskPhase.WARMING_UP, running: true, topicReceived: true, taskType: 'record', totalTime: 5, numEpisodes: 3,
      }));
    });
    expect(main.scrollTo).toHaveBeenCalledWith(expect.objectContaining({ top: 0 }));
  });
});
