// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The cloud view's stage while the robot loads the model (INFERENCE_LOADING):
// the waiting card in its dark tone over the cameras, with its own clock that
// starts when the loading starts, and nothing once the run is under way. The
// overlay around it is not a live region itself, so the clock is never read
// out every second.
//
// The scene-camera warning (D58): above the panel with Start, only when the
// reserved model's camera and the robot's stated camera are both known and
// differ; it never disables Start.

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import TaskPhase from '../../../constants/taskPhases';
import { cloudCopy, fill } from '../cloudCopy';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => vi.fn(),
}));

vi.mock('../../ImageGrid', () => ({ __esModule: true, default: () => <div data-testid="image-grid" /> }));
vi.mock('../../HeartbeatStatus', () => ({ __esModule: true, default: () => <div data-testid="heartbeat" /> }));
const mockPanel = vi.fn();
vi.mock('../CloudInferencePanel', () => ({
  __esModule: true,
  default: (props) => {
    mockPanel(props);
    return <div data-testid="cloud-panel" />;
  },
}));
vi.mock('../../../hooks/useCameraLiveness', () => ({
  __esModule: true,
  useCameraLiveness: () => ({ liveCount: 2 }),
  default: () => ({ liveCount: 2 }),
}));
vi.mock('../../../hooks/useCloudInferenceState', () => ({ __esModule: true, default: () => null }));
let mockSession = {};
vi.mock('../../../hooks/useCloudInferenceSession', () => ({ __esModule: true, default: () => mockSession }));
let mockRobotScene = null;
const mockSceneHook = vi.fn();
vi.mock('../../../hooks/useSceneCameraProfile', async () => ({
  ...(await vi.importActual('../../../hooks/useSceneCameraProfile')),
  __esModule: true,
  default: (opts) => {
    mockSceneHook(opts);
    return opts?.enabled === false ? null : mockRobotScene;
  },
}));

// eslint-disable-next-line import/first
import CloudInferenceView from '../CloudInferenceView';

function state(phase) {
  return {
    tasks: { taskStatus: { robotType: 'omx_f', phase } },
    ros: { imageTopicList: ['/gripper/image_raw', '/scene/image_raw'], rosbridgeUrl: 'ws://localhost/rosbridge' },
  };
}

const CLOUD = { offered: true, enabled: true, reason: null };

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-12T10:00:00Z'));
  mockSession = { phase: 'idle', summary: null, sceneCameraProfile: null };
  mockRobotScene = null;
  mockPanel.mockClear();
  mockSceneHook.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('CloudInferenceView while the model loads', () => {
  it('shows the dark waiting card with the loading texts and a clock from the start of the loading', () => {
    mockState = state(TaskPhase.INFERENCE_LOADING);
    render(<CloudInferenceView isActive cloud={CLOUD} />);
    const card = screen.getByTestId('cloud-wait');
    expect(card).toHaveAttribute('data-kind', 'model');
    expect(card).toHaveClass('ciw-dark');
    expect(screen.getByRole('status')).toHaveTextContent(cloudCopy.status.loading);
    expect(screen.getByRole('status')).toHaveTextContent(cloudCopy.status.loadingSub);
    expect(screen.getByTestId('cloud-wait-elapsed')).toHaveTextContent('00:00');
    act(() => { vi.advanceTimersByTime(7000); });
    expect(screen.getByTestId('cloud-wait-elapsed')).toHaveTextContent('00:07');
    expect(screen.queryByTestId('cloud-wait-retry')).toBeNull();
  });

  it('the overlay is not a live region of its own (the clock is never announced)', () => {
    mockState = state(TaskPhase.INFERENCE_LOADING);
    render(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(screen.getAllByRole('status')).toHaveLength(1);
    // eslint-disable-next-line testing-library/no-node-access -- an ancestor walk is the point
    const liveAncestor = screen.getByTestId('cloud-wait-elapsed').closest('[aria-live], [role="status"]');
    expect(liveAncestor).toBeNull();
  });

  it('nothing over the cameras once the run is under way, or before it', () => {
    mockState = state(TaskPhase.INFERENCING);
    const { rerender } = render(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(screen.queryByTestId('cloud-wait')).toBeNull();
    mockState = state(TaskPhase.READY);
    rerender(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(screen.queryByTestId('cloud-wait')).toBeNull();
  });

  it('the clock is gone with the overlay', () => {
    mockState = state(TaskPhase.INFERENCE_LOADING);
    const { rerender } = render(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(vi.getTimerCount()).toBe(1);
    mockState = state(TaskPhase.INFERENCING);
    rerender(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(screen.queryByTestId('cloud-wait')).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('CloudInferenceView warns when the model\'s scene camera differs from the robot\'s (D58)', () => {
  const SUMMARY = { type: 'act', expected_cameras: ['gripper', 'scene'], model_repo: 'EduBotics-Solutions/m' };

  function show({ model, robot, session = {} }) {
    mockState = state(TaskPhase.READY);
    mockSession = { phase: 'reserved', trainingId: 7, summary: SUMMARY, sceneCameraProfile: model, ...session };
    mockRobotScene = robot;
    return render(<CloudInferenceView isActive cloud={CLOUD} />);
  }

  function sentence(then, now) {
    return fill(cloudCopy.sceneMismatch, {
      then: cloudCopy.sceneCameraNames[then],
      now: cloudCopy.sceneCameraNames[now],
    });
  }

  it('a model of the earlier camera on a robot with the tower camera', () => {
    show({ model: 'legacy', robot: 'tower_v1' });
    expect(screen.getByTestId('cloud-scene-mismatch')).toHaveTextContent(sentence('legacy', 'tower_v1'));
    expect(screen.getByTestId('cloud-scene-mismatch')).toHaveTextContent(
      'Dieses Modell wurde mit Aufnahmen der bisherigen Szenen-Kamera trainiert, dein Roboter filmt jetzt mit '
      + 'der Turm-Kamera. Es kann deshalb schlechter greifen. Am besten nimmst du neue Episoden auf und trainierst neu.',
    );
  });

  it('and the reverse', () => {
    show({ model: 'tower_v1', robot: 'legacy' });
    expect(screen.getByTestId('cloud-scene-mismatch')).toHaveTextContent(sentence('tower_v1', 'legacy'));
  });

  it('stands above the panel with Start, in the same column', () => {
    show({ model: 'legacy', robot: 'tower_v1' });
    const warning = screen.getByTestId('cloud-scene-mismatch');
    const panel = screen.getByTestId('cloud-panel');
    // eslint-disable-next-line no-bitwise -- DOCUMENT_POSITION_FOLLOWING is a bit flag
    expect(warning.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // eslint-disable-next-line testing-library/no-node-access -- the shared column is the point
    expect(warning.parentElement).toBe(panel.parentElement);
  });

  it('nothing when they agree, when either is unknown, when the answer had no key or no statement ever came', () => {
    const quiet = [
      { model: 'legacy', robot: 'legacy' },
      { model: 'tower_v1', robot: 'tower_v1' },
      { model: null, robot: 'tower_v1' },
      { model: 'legacy', robot: null },
      { model: null, robot: null },
      { model: undefined, robot: 'tower_v1' }, // an older API: the reserve answer had no key
      { model: 'legacy', robot: null, session: { phase: 'running' } }, // the robot never stated a camera
    ];
    for (const c of quiet) {
      const { unmount } = show(c);
      expect(screen.queryByTestId('cloud-scene-mismatch')).toBeNull();
      expect(screen.queryByText(/schlechter greifen/)).toBeNull();
      unmount();
    }
  });

  it('never disables Start: the panel gets exactly the same props with and without the warning', () => {
    const { unmount } = show({ model: 'legacy', robot: 'legacy' });
    const without = mockPanel.mock.calls.at(-1)[0];
    unmount();
    show({ model: 'legacy', robot: 'tower_v1' });
    expect(screen.getByTestId('cloud-scene-mismatch')).toBeInTheDocument();
    const withWarning = mockPanel.mock.calls.at(-1)[0];
    expect(Object.keys(withWarning).sort()).toEqual(['cloud', 'cloudState', 'isActive']);
    expect(withWarning).toEqual(without);
  });

  it('the robot\'s statement is read only while the view is active', () => {
    mockState = state(TaskPhase.READY);
    mockSession = { phase: 'reserved', sceneCameraProfile: 'legacy' };
    mockRobotScene = 'tower_v1';
    const { rerender } = render(<CloudInferenceView isActive={false} cloud={CLOUD} />);
    expect(mockSceneHook).toHaveBeenLastCalledWith({ enabled: false });
    expect(screen.queryByTestId('cloud-scene-mismatch')).toBeNull();
    rerender(<CloudInferenceView isActive cloud={CLOUD} />);
    expect(mockSceneHook).toHaveBeenLastCalledWith({ enabled: true });
    expect(screen.getByTestId('cloud-scene-mismatch')).toBeInTheDocument();
  });
});
