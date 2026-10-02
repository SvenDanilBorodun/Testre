// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The camera tiles (spec §3.12): ordered by the profile's camera roles and
// named in German, asked for silently on mount, again when the link comes
// back and again while the last answer failed or lacked a camera; a missing
// camera says „Kamerabild nicht verfügbar" only once the robot has answered;
// the rate badge only when /edubotics/signal_status exists.

import React from 'react';
import { render, screen, act } from '@testing-library/react';
import CameraTiles, { cameraTiles, topicRole } from '../CameraTiles';

// The bare stream cell is ImageGridCell's own business (its tests); here it is
// a marker that records what it was given.
vi.mock('../../ImageGridCell', () => ({
  __esModule: true,
  default: function ImageGridCellMock({ topic, bare, onStreamError }) {
    return (
      <button type="button" data-testid="cell" data-topic={topic} data-bare={bare ? 'true' : 'false'} onClick={onStreamError}>
        stream
      </button>
    );
  },
}));

const LABELS = { gripper: 'Greifer-Kamera', scene: 'Szenen-Kamera' };
const labelFor = (r) => LABELS[r] || r;
const OK = (list) => () => Promise.resolve({ success: true, image_topic_list: list });

const tiles = () => screen.queryAllByTestId('rec-camera-tile');

describe('camera tile helpers', () => {
  it('the role is the first path segment', () => {
    expect(topicRole('/gripper/image_raw')).toBe('gripper');
    expect(topicRole('scene/image_raw/compressed')).toBe('scene');
    expect(topicRole('')).toBe('');
  });

  it('orders by the profile roles, a missing role keeps its tile', () => {
    expect(cameraTiles(['/scene/image_raw', '/gripper/image_raw'], ['gripper', 'scene']))
      .toEqual([{ role: 'gripper', topic: '/gripper/image_raw' }, { role: 'scene', topic: '/scene/image_raw' }]);
    expect(cameraTiles(['/scene/image_raw'], ['gripper', 'scene']))
      .toEqual([{ role: 'gripper', topic: null }, { role: 'scene', topic: '/scene/image_raw' }]);
    expect(cameraTiles(['/scene/image_raw'], null)).toEqual([{ role: 'scene', topic: '/scene/image_raw' }]);
  });
});

describe('CameraTiles', () => {
  beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
  afterEach(() => { vi.useRealTimers(); });

  it('asks on mount and draws bare cells in the profile order with German names', async () => {
    const fetchTopics = vi.fn(OK(['/scene/image_raw', '/gripper/image_raw']));
    render(<CameraTiles connected fetchTopics={fetchTopics} roles={['gripper', 'scene']} labelFor={labelFor} fallbackText="Kamerabild nicht verfügbar" />);
    await act(async () => { await Promise.resolve(); });
    expect(fetchTopics).toHaveBeenCalledTimes(1);
    expect(tiles().map((t) => t.getAttribute('data-role'))).toEqual(['gripper', 'scene']);
    expect(screen.getByText('Greifer-Kamera')).toBeInTheDocument();
    expect(screen.getByText('Szenen-Kamera')).toBeInTheDocument();
    expect(screen.getAllByTestId('cell').map((c) => c.getAttribute('data-bare'))).toEqual(['true', 'true']);
    expect(screen.queryByText('Kamerabild nicht verfügbar')).toBeNull();
  });

  it('asks again when the link comes back', async () => {
    const fetchTopics = vi.fn(OK(['/gripper/image_raw']));
    const { rerender } = render(<CameraTiles connected fetchTopics={fetchTopics} roles={['gripper']} labelFor={labelFor} />);
    await act(async () => { await Promise.resolve(); });
    expect(fetchTopics).toHaveBeenCalledTimes(1);
    rerender(<CameraTiles connected={false} fetchTopics={fetchTopics} roles={['gripper']} labelFor={labelFor} />);
    rerender(<CameraTiles connected fetchTopics={fetchTopics} roles={['gripper']} labelFor={labelFor} />);
    await act(async () => { await Promise.resolve(); });
    expect(fetchTopics).toHaveBeenCalledTimes(2);
  });

  it('a failed or incomplete answer is asked again later; the missing camera says so', async () => {
    const fetchTopics = vi.fn()
      .mockImplementationOnce(() => Promise.reject(new Error('timeout')))
      .mockImplementationOnce(OK(['/scene/image_raw']))
      .mockImplementation(OK(['/scene/image_raw', '/gripper/image_raw']));
    render(<CameraTiles connected fetchTopics={fetchTopics} roles={['gripper', 'scene']} labelFor={labelFor} fallbackText="Kamerabild nicht verfügbar" retryMs={1000} />);
    await act(async () => { await Promise.resolve(); });
    // Never answered yet: no failure is claimed.
    expect(screen.queryByText('Kamerabild nicht verfügbar')).toBeNull();
    await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve(); });
    expect(fetchTopics).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Kamerabild nicht verfügbar')).toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve(); });
    expect(fetchTopics).toHaveBeenCalledTimes(3);
    expect(screen.queryByText('Kamerabild nicht verfügbar')).toBeNull();
    // Complete now: no more asking.
    await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve(); });
    expect(fetchTopics).toHaveBeenCalledTimes(3);
  });

  it('a stream that fails shows the fallback in its own tile', async () => {
    render(<CameraTiles connected fetchTopics={OK(['/gripper/image_raw'])} roles={['gripper']} labelFor={labelFor} fallbackText="Kamerabild nicht verfügbar" />);
    await act(async () => { await Promise.resolve(); });
    act(() => { screen.getByTestId('cell').click(); });
    expect(screen.getByText('Kamerabild nicht verfügbar')).toBeInTheDocument();
  });

  it('rate badges only with signal status: dot per verdict and the rounded rate', async () => {
    const signal = [
      { kind: 'camera', name: 'gripper', hz: 29.8, verdict: 'ok' },
      { kind: 'camera', name: 'scene', hz: 11.2, verdict: 'slow' },
      { kind: 'follower', name: 'follower', hz: 99, verdict: 'ok' },
    ];
    const { rerender } = render(<CameraTiles connected fetchTopics={OK(['/gripper/image_raw', '/scene/image_raw'])} roles={['gripper', 'scene']} labelFor={labelFor} signal={null} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByText(/Hz/)).toBeNull();
    rerender(<CameraTiles connected fetchTopics={OK(['/gripper/image_raw', '/scene/image_raw'])} roles={['gripper', 'scene']} labelFor={labelFor} signal={signal} />);
    expect(screen.getByText('30 Hz')).toBeInTheDocument();
    expect(screen.getByText('11 Hz')).toBeInTheDocument();
    const [grip, scene] = tiles();
    expect(grip.querySelector('.rec-dot')).toHaveAttribute('data-verdict', 'ok'); // eslint-disable-line testing-library/no-node-access
    expect(scene.querySelector('.rec-dot')).toHaveClass('warn'); // eslint-disable-line testing-library/no-node-access
    // The controller's own badge text wins (German decimal comma).
    rerender(
      <CameraTiles
        connected
        fetchTopics={OK(['/gripper/image_raw', '/scene/image_raw'])}
        roles={['gripper', 'scene']}
        labelFor={labelFor}
        signal={signal.map((s) => ({ ...s, hzText: s.name === 'scene' ? '11,2 Hz' : '—' }))}
      />
    );
    expect(screen.getByText('11,2 Hz')).toBeInTheDocument();
    expect(screen.getAllByText('—')).toHaveLength(1);
  });
});
