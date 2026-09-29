// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Review round 1 (R1-O2): a folder that cannot be listed is reported in
// German. The robot's own reason (file_browse_utils, English) and a transport
// error ("ROS connection failed …") reach the console only.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import FileBrowser from '../FileBrowser';

const mockBrowse = vi.hoisted(() => ({ fn: vi.fn() }));
vi.mock('../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({ browseFile: mockBrowse.fn }),
}));
const mockToast = vi.hoisted(() => Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }));
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

beforeEach(() => {
  mockBrowse.fn.mockReset();
  mockToast.error.mockClear();
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => { vi.restoreAllMocks(); });

test.each([
  ['a transport error', () => Promise.reject(new Error('ROS connection failed for service /browse_file'))],
  ['the robot\'s own English reason', () => Promise.resolve({ success: false, message: 'Cannot navigate to parent directory' })],
])('%s is shown as one German sentence', async (_label, answer) => {
  mockBrowse.fn.mockImplementation(answer);
  render(<FileBrowser initialPath="/root/ros2_ws" />);
  await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
  expect(mockToast.error).toHaveBeenLastCalledWith('Der Ordner konnte nicht geöffnet werden.');
  expect(screen.getByText('Der Ordner konnte nicht geöffnet werden.')).toBeInTheDocument();
  expect(screen.queryByText(/ROS connection|Cannot navigate/)).toBeNull();
  expect(console.warn).toHaveBeenCalled();
});
