/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// SimStage — the thin sim-mode shell rendered in place of the RightDock. It owns
// no WebGL/grasp state: it renders SimScene(layout="split") with mount-time
// shadow/reach constants, a German runtime-feedback line (reroute log + IK
// unreachable warnings, both from Redux), and a collapsible debug strip. SimScene
// (→ lazy UrdfTwin) and DebugPanel are mocked so these are pure-DOM assertions.

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SimStage from '../SimStage';

// react-redux: selector-aware stub over a mutable module-level state.
let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

// SimScene stub — capture the props (the lazy UrdfTwin never mounts under it).
const mockSimScene = vi.hoisted(() => vi.fn());
vi.mock('../SimScene', () => ({
  __esModule: true,
  default: (props) => {
    mockSimScene(props);
    return <div data-testid="sim-scene" data-layout={props.layout} />;
  },
}));

// DebugPanel stub — only rendered while the strip is open.
vi.mock('../DebugPanel', () => ({
  __esModule: true,
  default: (props) => (
    <div data-testid="debug-panel" data-has-ws={String(!!props.workspace)} />
  ),
}));

const NOOP = () => {};

function baseState(over = {}) {
  return { workshop: { log: [], debuggerWarnings: [], ...over } };
}

function renderStage(props = {}) {
  return render(
    <SimStage
      scene={{ objects: [], zones: [] }}
      onChange={NOOP}
      catalog={[['Würfel', 'wuerfel']]}
      catalogDims={{ wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 } }}
      workspace={{ id: 'ws' }}
      debugOpen={false}
      onToggleDebug={NOOP}
      showPath={false}
      onToggleShowPath={NOOP}
      onClearPath={NOOP}
      pathClearToken={0}
      {...props}
    />,
  );
}

beforeEach(() => {
  mockState = baseState();
  mockSimScene.mockClear();
});

describe('SimStage', () => {
  test('renders SimScene(layout="split") with catalogDims + mount-time shadow/reach constants', () => {
    renderStage();
    expect(screen.getByTestId('sim-scene')).toBeInTheDocument();
    const props = mockSimScene.mock.calls[0][0];
    expect(props.layout).toBe('split');
    // showShadows/showReach ship as LITERAL true (mount-time constants inside
    // UrdfTwin) — must not arrive as toggled state.
    expect(props.showShadows).toBe(true);
    expect(props.showReach).toBe(true);
    // catalogDims is threaded through verbatim (stable reference from the parent).
    expect(props.catalogDims).toEqual({
      wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 },
    });
    // Trail props forwarded.
    expect(props.showPath).toBe(false);
    expect(props.pathClearToken).toBe(0);
    expect(props.scene).toEqual({ objects: [], zones: [] });
  });

  test('the „Bahn" toggle reflects showPath and calls onToggleShowPath', async () => {
    const onToggleShowPath = vi.fn();
    renderStage({ showPath: true, onToggleShowPath });
    const btn = screen.getByRole('button', { name: 'Bahn verbergen' });
    await userEvent.click(btn);
    expect(onToggleShowPath).toHaveBeenCalledTimes(1);
  });

  test('the „Bahn löschen" button calls onClearPath', async () => {
    const onClearPath = vi.fn();
    renderStage({ onClearPath });
    await userEvent.click(screen.getByRole('button', { name: 'Bahn löschen' }));
    expect(onClearPath).toHaveBeenCalledTimes(1);
  });

  test('debug strip is hidden when debugOpen is false', () => {
    renderStage({ debugOpen: false });
    expect(screen.queryByTestId('debug-panel')).toBeNull();
  });

  test('debug strip renders DebugPanel (with workspace) when debugOpen; ✕ calls onToggleDebug', async () => {
    const onToggleDebug = vi.fn();
    renderStage({ debugOpen: true, onToggleDebug });
    const panel = screen.getByTestId('debug-panel');
    expect(panel).toBeInTheDocument();
    expect(panel.getAttribute('data-has-ws')).toBe('true');
    await userEvent.click(screen.getByRole('button', { name: 'Debug schließen' }));
    expect(onToggleDebug).toHaveBeenCalledTimes(1);
  });

  test('status line surfaces the MOST RECENT reroute log entry', () => {
    mockState = baseState({
      log: [
        { ts: 1, text: '[WARNUNG] Sperrzone A — Ausweichroute wird gefahren.' },
        { ts: 2, text: 'Zwischenmeldung ohne Signal.' },
        { ts: 3, text: '[WARNUNG] Sperrzone B — Ausweichroute wird gefahren.' },
      ],
    });
    renderStage();
    expect(screen.getByText(/Sperrzone B — Ausweichroute wird gefahren\./)).toBeInTheDocument();
    expect(screen.queryByText(/Sperrzone A/)).toBeNull();
  });

  test('status line surfaces the unreachable-warning count', () => {
    mockState = baseState({
      debuggerWarnings: [
        { block_id: 'b1', message: 'x' },
        { block_id: 'b2', message: 'y' },
      ],
    });
    renderStage();
    expect(screen.getByText(/2 Ziel\(e\) nicht erreichbar/)).toBeInTheDocument();
  });

  test('status line is hidden entirely when there is no reroute log and no warnings', () => {
    mockState = baseState({ log: [{ ts: 1, text: 'Programm gestartet.' }], debuggerWarnings: [] });
    renderStage();
    expect(screen.queryByText(/Sperrzone|Ausweichroute/)).toBeNull();
    expect(screen.queryByText(/nicht erreichbar/)).toBeNull();
  });
});
