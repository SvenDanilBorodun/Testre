// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The Inferenz page runs „In der Cloud" only (spec §6.2/§6.3, R-6). Pinned:
//  - a signed-in student with a robot reaches the cloud view, with or without a
//    classroom, and the view is handed the gating verdict derived on render;
//  - an older robot image (no `cloud_inference` capability) still gets the
//    cloud view, disabled, so its panel can say why (`old_image`);
//  - `?cloud=1` (no robot) renders the explanation screen, never the view;
//  - the auth gates keep their texts.

import React from 'react';
import { render, screen } from '@testing-library/react';
import InferencePage from '../InferencePage';
import TaskPhase from '../../constants/taskPhases';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { dismiss: vi.fn(), success: vi.fn(), error: vi.fn() },
  useToasterStore: () => ({ toasts: [] }),
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
    mockCloudView(props);
    return <div data-testid="cloud-view" />;
  },
}));
vi.mock('../../components/LoginForm', () => ({ __esModule: true, default: () => <div data-testid="login-form" /> }));

const CAPS = { recordable: true, editable: true, trainable: true, inferable: true, roboter_studio: true, has_leader: true };

function baseState({ classroomId = 'c-1', capabilities = { ...CAPS, cloud_inference: true } } = {}) {
  return {
    auth: {
      isAuthenticated: true, isLoading: false, profileLoaded: true, username: 'lena', fullName: 'Lena Schmidt',
      classroomId, session: { access_token: 'jwt' },
    },
    tasks: {
      taskStatus: { robotType: 'omx_f', phase: TaskPhase.READY, running: false, capabilities },
      taskInfo: { tags: [] },
    },
    ros: { imageTopicList: [], rosbridgeUrl: 'ws://localhost/rosbridge' },
  };
}

beforeEach(() => {
  mockCloudView.mockClear();
  mockCloudOnly.current = false;
});

describe('InferencePage — the cloud is the only target', () => {
  it('a robot that advertises the cloud gets the enabled cloud view', () => {
    mockState = baseState();
    render(<InferencePage isActive />);
    expect(screen.getByTestId('cloud-view')).toBeInTheDocument();
    expect(mockCloudView).toHaveBeenLastCalledWith(expect.objectContaining({
      isActive: true,
      cloud: { offered: true, enabled: true, reason: null },
    }));
  });

  it('a student without a classroom reaches the cloud', () => {
    mockState = baseState({ classroomId: null });
    render(<InferencePage />);
    expect(screen.getByTestId('cloud-view')).toBeInTheDocument();
  });

  it('an older robot image: the cloud view, disabled with its reason', () => {
    mockState = baseState({ capabilities: { ...CAPS } });
    render(<InferencePage />);
    expect(mockCloudView).toHaveBeenLastCalledWith(expect.objectContaining({
      cloud: { offered: true, enabled: false, reason: 'old_image' },
    }));
  });

  it('?cloud=1 (no robot) explains why and renders no cloud view', () => {
    mockCloudOnly.current = true;
    mockState = baseState();
    render(<InferencePage />);
    expect(screen.getByText('Inferenz braucht deinen Roboter')).toBeInTheDocument();
    expect(mockCloudView).not.toHaveBeenCalled();
  });

  it('the auth gates keep their texts', () => {
    mockState = baseState();
    mockState.auth.isAuthenticated = false;
    render(<InferencePage />);
    expect(screen.getByTestId('login-form')).toBeInTheDocument();

    mockState = baseState();
    mockState.auth.isLoading = true;
    render(<InferencePage />);
    expect(screen.getByText('Laden…')).toBeInTheDocument();

    mockState = baseState();
    mockState.auth.profileLoaded = false;
    render(<InferencePage />);
    expect(screen.getByText('Profil wird geladen…')).toBeInTheDocument();
    expect(mockCloudView).not.toHaveBeenCalled();
  });
});
