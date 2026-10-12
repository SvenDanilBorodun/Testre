// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Inferenz page's two targets (spec §6.2/§6.3, R-6). Pinned:
//  - useJetsonConnection stays mounted for BOTH targets (discovery, the 30 s
//    heartbeat and the pagehide beacon must not depend on the target);
//  - a classroom whose Jetson discovery is pending shows the existing spinner;
//    a discovery that resolves `resume` (rosbridge moves to the Jetson) shows
//    the Jetson flow and the cloud view is never rendered against the Jetson's
//    rosbridge;
//  - „In der Cloud" is the default when the robot advertises it (owner A-Q5),
//    and a student without a classroom reaches it;
//  - the Jetson flow's texts are unchanged; choosing it lets a reservation go.

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import InferencePage from '../InferencePage';
import { cloudCopy } from '../../components/CloudInference/cloudCopy';
import TaskPhase from '../../constants/taskPhases';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { dismiss: vi.fn(), success: vi.fn(), error: vi.fn() },
  useToasterStore: () => ({ toasts: [] }),
}));

const mockJetsonHook = vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn(), status: 'unknown' }));
vi.mock('../../hooks/useJetsonConnection', () => ({
  __esModule: true,
  useJetsonConnection: () => mockJetsonHook(),
}));
vi.mock('../../hooks/useCameraLiveness', () => ({
  __esModule: true,
  useCameraLiveness: () => ({ liveTopics: [], liveCount: 0, allConfiguredAlive: false }),
}));
vi.mock('../../services/cloudConfig', () => ({
  __esModule: true,
  CLOUD_API_URL: 'https://api.test',
  isCloudApiConfigured: true,
  assertCloudApiConfigured: () => {},
}));
const mockCloudOnly = { current: false };
vi.mock('../../utils/cloudMode', () => ({
  __esModule: true,
  isCloudOnlyMode: () => mockCloudOnly.current,
}));

const mockCloudView = vi.fn();
vi.mock('../../components/CloudInference/CloudInferenceView', () => ({
  __esModule: true,
  default: (props) => {
    mockCloudView(props, mockState.ros.rosbridgeUrl);
    return <div data-testid="cloud-view" />;
  },
}));
vi.mock('../../components/ControlPanel', () => ({ __esModule: true, default: () => <div data-testid="control-panel" /> }));
vi.mock('../../components/ImageGrid', () => ({ __esModule: true, default: () => <div data-testid="image-grid" /> }));
vi.mock('../../components/InferencePanel', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/HeartbeatStatus', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/LoginForm', () => ({ __esModule: true, default: () => <div data-testid="login-form" /> }));

const releaseSpy = vi.hoisted(() => vi.fn());
vi.mock('../../features/cloudInference/controller', async (importOriginal) => {
  const real = await importOriginal();
  return { ...real, release: (...a) => { releaseSpy(...a); return real.release(...a); } };
});

const CAPS = { recordable: true, editable: true, trainable: true, inferable: true, roboter_studio: true, has_leader: true };

function baseState({ jetsonStatus = 'available', classroomId = 'c-1', capabilities = { ...CAPS, cloud_inference: true }, rosbridgeUrl = 'ws://localhost/rosbridge' } = {}) {
  return {
    auth: {
      isAuthenticated: true, isLoading: false, profileLoaded: true, username: 'lena', fullName: 'Lena Schmidt',
      classroomId, session: { access_token: 'jwt' },
    },
    tasks: {
      taskStatus: { robotType: 'omx_f', phase: TaskPhase.READY, running: false, capabilities },
      taskInfo: { tags: [] },
    },
    ros: { imageTopicList: [], rosbridgeUrl },
    ui: { isFirstLoad: { inference: false } },
    jetson: {
      status: jetsonStatus, online: true, ownerFullName: null, ownerUsername: null, error: null, heartbeatTransient: false,
    },
  };
}

beforeEach(() => {
  mockJetsonHook.mockClear();
  mockCloudView.mockClear();
  releaseSpy.mockClear();
  mockCloudOnly.current = false;
});

describe('InferencePage — targets', () => {
  it('cloud is the default when the robot advertises it, with the switch', () => {
    mockState = baseState();
    render(<InferencePage />);
    expect(screen.getByTestId('cloud-view')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: cloudCopy.target.cloud })).toHaveAttribute('aria-pressed', 'true');
    expect(mockJetsonHook).toHaveBeenCalled();
  });

  it('a student without a classroom reaches the cloud', () => {
    mockState = baseState({ jetsonStatus: 'unknown', classroomId: null });
    render(<InferencePage />);
    expect(screen.getByTestId('cloud-view')).toBeInTheDocument();
    expect(screen.queryByText('Du bist keiner Klasse zugeordnet')).toBeNull();
  });

  it('a pending classroom discovery shows the existing spinner, not the cloud', () => {
    mockState = baseState({ jetsonStatus: 'unknown' });
    render(<InferencePage />);
    expect(screen.getByText('Klassen-Jetson wird gesucht…')).toBeInTheDocument();
    expect(mockCloudView).not.toHaveBeenCalled();
  });

  it('discovery resolving `resume` after mount shows the Jetson flow; the cloud view never meets the Jetson rosbridge', () => {
    mockState = baseState({ jetsonStatus: 'unknown' });
    const { rerender } = render(<InferencePage />);
    // resume: rosbridge moves to the Jetson and the status becomes connected
    mockState = baseState({ jetsonStatus: 'connected', rosbridgeUrl: 'ws://10.0.0.5:9091' });
    rerender(<InferencePage />);
    expect(screen.getByText('Verbunden mit Klassen-Jetson')).toBeInTheDocument();
    expect(screen.getByTestId('control-panel')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: cloudCopy.target.cloud })).toBeNull();
    for (const [, url] of mockCloudView.mock.calls) expect(url).not.toContain(':9091');
    expect(mockCloudView).not.toHaveBeenCalled();
    // the Jetson lifecycle hook ran on every render of both phases
    expect(mockJetsonHook.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('useJetsonConnection is mounted for the cloud target too', () => {
    mockState = baseState({ jetsonStatus: 'available' });
    render(<InferencePage />);
    expect(screen.getByTestId('cloud-view')).toBeInTheDocument();
    expect(mockJetsonHook).toHaveBeenCalled();
  });

  it('choosing the Jetson shows its flow with the unchanged texts and lets a reservation go', () => {
    mockState = baseState({ jetsonStatus: 'available' });
    render(<InferencePage />);
    fireEvent.click(screen.getByRole('button', { name: cloudCopy.target.jetson }));
    expect(releaseSpy).toHaveBeenCalled();
    expect(screen.getByText('Klassen-Jetson bereit')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Verbinde mit Klassen-Jetson' })).toBeInTheDocument();
    expect(screen.queryByTestId('cloud-view')).toBeNull();
  });

  it('an older robot image: cloud offered but disabled, the Jetson flow by default', () => {
    mockState = baseState({ jetsonStatus: 'no_jetson', capabilities: { ...CAPS } });
    render(<InferencePage />);
    expect(screen.getByText('Kein Klassen-Jetson eingerichtet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: cloudCopy.target.cloud })).toBeDisabled();
    expect(screen.getByText(cloudCopy.disabled.old_image)).toBeInTheDocument();
  });

  it('?cloud=1 offers no cloud target: the Jetson flow alone, texts unchanged', () => {
    mockCloudOnly.current = true;
    mockState = baseState({ jetsonStatus: 'unknown', classroomId: null });
    render(<InferencePage />);
    expect(screen.getByText('Du bist keiner Klasse zugeordnet')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: cloudCopy.target.cloud })).toBeNull();
  });

  it('the auth gates keep their texts', () => {
    mockState = baseState();
    mockState.auth.isAuthenticated = false;
    render(<InferencePage />);
    expect(screen.getByTestId('login-form')).toBeInTheDocument();
  });
});
