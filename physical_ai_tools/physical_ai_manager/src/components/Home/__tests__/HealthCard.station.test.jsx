// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Startseite's calibration row on a camera station (station spec v6.13,
// §9.2): HealthCard hands CalibrationStatus's additive station fields (§8.4)
// to deriveHealth, so a station reads „Kamerastation eingerichtet" / „nicht
// eingerichtet" instead of „0 von 3" forever. An older image sends no station
// field and reads as a manual rig.

import React from 'react';
import { render, screen } from '@testing-library/react';
import HealthCard from '../HealthCard';

let mockState;
vi.mock('react-redux', () => ({ __esModule: true, useSelector: (sel) => sel(mockState) }));
const mockCalib = vi.hoisted(() => ({ res: null }));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({
    getCalibrationStatus: () => Promise.resolve(mockCalib.res),
    getImageTopicList: () => Promise.resolve({ success: true, image_topic_list: ['/scene/image_raw'] }),
  }),
}));
vi.mock('../../../utils/piMode', async () => {
  const actual = await vi.importActual('../../../utils/piMode');
  return { ...actual, usePiMode: () => ({ piMode: false, agentStatus: null }) };
});

beforeEach(() => {
  mockState = {
    tasks: {
      heartbeatStatus: 'connected',
      taskStatus: { capabilities: { roboter_studio: true, station_protocol: 6, camera_roles: ['scene'] }, error: '' },
    },
  };
});

describe('HealthCard on a camera station', () => {
  it('reads „Kamerastation eingerichtet" from the station fields', async () => {
    mockCalib.res = {
      success: true, has_scene_intrinsics: false, has_scene_handeye: false, has_table_plane: false,
      station_protocol: 6, station_rig: true, station_configured: true,
    };
    render(<HealthCard jointsLive />);
    expect(await screen.findByText('Kamerastation eingerichtet')).toBeInTheDocument();
    expect(screen.queryByText('0 von 3')).toBe(null);
  });

  it('reads „Kamerastation nicht eingerichtet" on a fresh station', async () => {
    mockCalib.res = {
      success: true, has_scene_intrinsics: false, has_scene_handeye: false, has_table_plane: false,
      station_rig: true, station_configured: false,
    };
    render(<HealthCard jointsLive />);
    expect(await screen.findByText('Kamerastation nicht eingerichtet')).toBeInTheDocument();
  });

  it('an image without the station fields reads as a manual rig', async () => {
    mockCalib.res = { success: true, has_scene_intrinsics: true, has_scene_handeye: false, has_table_plane: false };
    render(<HealthCard jointsLive />);
    expect(await screen.findByText('1 von 3')).toBeInTheDocument();
  });
});
