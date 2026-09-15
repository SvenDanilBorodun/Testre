/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/*
 * The drawer detail of a Blockly variable: its last value with age, the last
 * five values (`workshop.variableHistory`, through the REAL workshop reducer),
 * and „Im Simulator zeigen" for a point — against a real headless workspace.
 * The page's preview router is the `onPreview` spy.
 */

import React from 'react';
import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, within, act } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import studioAssetsReducer, { openDrawer } from '../../../../features/workshop/studioAssetsSlice';
import workshopReducer, { clearVariables, setVariable } from '../../../../features/workshop/workshopSlice';
import { DE } from '../../blocks/messages_de';
import { createSammlungProvider } from '../provider';
import SammlungDrawer from '../SammlungDrawer';

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() }),
}));

let ws;

beforeAll(() => {
  Blockly.setLocale(De);
});

afterEach(() => {
  vi.restoreAllMocks();
  if (ws) ws.dispose();
  ws = null;
});

function setup({ name = 'Punkt', previewVariables = true } = {}) {
  ws = new Blockly.Workspace();
  ws.getToolbox = () => ({ getWidth: () => 118, clearSelection: vi.fn() });
  const variable = ws.createVariable(name);
  const provider = createSammlungProvider({
    capabilities: { hardware: true, drawer: true, preview: true, previewVariables },
  });
  const redux = configureStore({ reducer: { studioAssets: studioAssetsReducer, workshop: workshopReducer } });
  redux.dispatch(openDrawer({ tab: 'variablen', focusId: variable.getId() }));
  const onPreview = vi.fn();
  render(
    <Provider store={redux}>
      <SammlungDrawer workspace={ws} provider={provider} onPreview={onPreview} />
    </Provider>,
  );
  return { redux, onPreview, variableId: variable.getId() };
}

const timeDe = (ts) => new Date(ts).toLocaleTimeString('de-DE');

describe('DrawerVariable — values', () => {
  it('without a value says so and offers no point button', () => {
    setup();
    expect(screen.getByText(DE.DRAWER_NO_VALUE)).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: DE.DRAWER_LAST_VALUES })).toBeNull();
    expect(screen.queryByRole('button', { name: DE.DRAWER_SHOW_POINT })).toBeNull();
  });

  it('shows the last value with its age and the history list, newest first', () => {
    let clock = Date.parse('2026-09-15T10:00:00Z');
    vi.spyOn(Date, 'now').mockImplementation(() => clock);
    const { redux } = setup({ name: 'Zahl' });
    const stamps = [];
    act(() => {
      for (const value of [1, 2, 2, 3, 4, 5, 6]) {
        clock += 1000;
        redux.dispatch(setVariable({ name: 'Zahl', value }));
        // A history entry is stamped only when the value changed.
        if (redux.getState().workshop.variableHistory.Zahl[0].ts === clock) stamps.push(clock);
      }
    });
    expect(screen.queryByText(DE.DRAWER_NO_VALUE)).toBeNull();
    expect(screen.getByText(DE.DRAWER_LAST_VALUE)).toBeInTheDocument();
    expect(screen.getByText('6 · vor 0 s')).toBeInTheDocument();

    const list = screen.getByRole('region', { name: DE.DRAWER_LAST_VALUES });
    expect(within(list).getByRole('heading', { name: DE.DRAWER_LAST_VALUES })).toBeInTheDocument();
    // Seven sets, one unchanged (2 → 2): six distinct values, the newest five kept.
    const rows = within(list).getAllByRole('listitem').map((li) => li.textContent);
    expect(rows).toEqual([6, 5, 4, 3, 2].map((v, i) => `${v} · ${timeDe(stamps[stamps.length - 1 - i])}`));
  });

  it('a long value is cut at 200 characters', () => {
    const { redux } = setup({ name: 'Text' });
    act(() => { redux.dispatch(setVariable({ name: 'Text', value: 'a'.repeat(250) })); });
    const list = screen.getByRole('region', { name: DE.DRAWER_LAST_VALUES });
    expect(within(list).getByRole('listitem').textContent.startsWith(`${'a'.repeat(200)}… · `)).toBe(true);
  });

  it('a Start (clearVariables) empties the drawer again', () => {
    const { redux } = setup({ name: 'Zahl' });
    act(() => { redux.dispatch(setVariable({ name: 'Zahl', value: 3 })); });
    expect(screen.getByRole('region', { name: DE.DRAWER_LAST_VALUES })).toBeInTheDocument();
    act(() => { redux.dispatch(clearVariables()); });
    expect(screen.getByText(DE.DRAWER_NO_VALUE)).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: DE.DRAWER_LAST_VALUES })).toBeNull();
  });
});

describe('DrawerVariable — „Im Simulator zeigen"', () => {
  it('a point-shaped value offers the button, which asks the page to show it', () => {
    const { redux, onPreview, variableId } = setup();
    act(() => { redux.dispatch(setVariable({ name: 'Punkt', value: { x: 0.1, y: 0, z: 0.05 } })); });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SHOW_POINT }));
    expect(onPreview).toHaveBeenCalledWith({ kind: 'variable', id: variableId, name: 'Punkt' });
  });

  it('a value that is not a point offers no button', () => {
    const { redux } = setup();
    act(() => { redux.dispatch(setVariable({ name: 'Punkt', value: [0.1, 0, 0.05] })); });
    expect(screen.queryByRole('button', { name: DE.DRAWER_SHOW_POINT })).toBeNull();
  });

  it('no button while the page has not enabled variable previews', () => {
    const { redux } = setup({ previewVariables: false });
    act(() => { redux.dispatch(setVariable({ name: 'Punkt', value: { x: 0.1, y: 0, z: 0.05 } })); });
    expect(screen.queryByRole('button', { name: DE.DRAWER_SHOW_POINT })).toBeNull();
  });
});
