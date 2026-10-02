// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// H7: the Aufnahme page forces the values it no longer offers at SEND time
// (optimized save on, rosbag2 and inference recording off, one instruction),
// whatever the store holds after the /task/status adopt path wrote into it.
// Inferenz sends exactly what it sent before.

import { renderHook } from '@testing-library/react';
import { useRosServiceCaller } from '../useRosServiceCaller';
import PageType from '../../constants/pageType';
import TaskCommand from '../../constants/taskCommand';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ isConnected: true })) },
}));

const mockRequests = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Service: function ServiceMock(opts) {
      this.name = opts.name;
      this.callService = (req, ok) => {
        mockRequests.push({ name: this.name, req });
        ok({ success: true, message: '' });
      };
    },
    ServiceRequest: function ServiceRequestMock(req) { Object.assign(this, req); },
  },
}));

const STORED = Object.freeze({
  taskName: 'Würfel',
  taskType: '',
  taskInstruction: ['  ', ' Greife den Würfel. ', 'zweite Anweisung'],
  policyPath: '',
  recordInferenceMode: true,
  userId: 'schule-A',
  fps: 30,
  tags: ['omx_f'],
  warmupTime: 5,
  episodeTime: 20,
  resetTime: 5,
  numEpisodes: 5,
  pushToHub: true,
  privateMode: true,
  // what an adopted multi-task / non-optimized session left behind (H7)
  useOptimizedSave: false,
  recordRosBag2: true,
});

function stateFor(page) {
  return {
    tasks: { taskInfo: STORED },
    training: { trainingInfo: {} },
    editDataset: {},
    ui: { currentPage: page },
    ros: { rosbridgeUrl: 'ws://localhost/rosbridge' },
  };
}

beforeEach(() => {
  mockRequests.length = 0;
});

describe('sendRecordCommand', () => {
  it('on the Aufnahme page forces the send-time values', async () => {
    mockState = stateFor(PageType.RECORD);
    const { result } = renderHook(() => useRosServiceCaller());
    await result.current.sendRecordCommand('start_record');
    const { name, req } = mockRequests[0];
    expect(name).toBe('/task/command');
    expect(req.command).toBe(TaskCommand.START_RECORD);
    expect(req.task_info).toMatchObject({
      task_name: 'Würfel',
      task_type: 'record',
      task_instruction: ['Greife den Würfel.'],
      use_optimized_save_mode: true,
      record_rosbag2: false,
      record_inference_mode: false,
      private_mode: true,
      push_to_hub: true,
      user_id: 'schule-A',
      warmup_time_s: 5,
      num_episodes: 5,
    });
  });

  it('on the Inferenz page sends what the store holds, as before', async () => {
    mockState = stateFor(PageType.INFERENCE);
    const { result } = renderHook(() => useRosServiceCaller());
    await result.current.sendRecordCommand('finish');
    const { req } = mockRequests[0];
    expect(req.command).toBe(TaskCommand.FINISH);
    expect(req.task_info).toMatchObject({
      task_type: 'inference',
      task_instruction: [' Greife den Würfel. ', 'zweite Anweisung'],
      use_optimized_save_mode: false,
      record_rosbag2: true,
      record_inference_mode: true,
    });
  });
});
