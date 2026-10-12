// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import TargetSwitch from '../TargetSwitch';
import { cloudCopy } from '../cloudCopy';

describe('TargetSwitch', () => {
  it('renders nothing when the cloud is not offered', () => {
    const { container } = render(<TargetSwitch cloud={{ offered: false, enabled: false, reason: null }} target="jetson" onChoose={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('offers both targets, marks the active one and reports a choice', () => {
    const onChoose = vi.fn();
    render(<TargetSwitch cloud={{ offered: true, enabled: true, reason: null }} target="cloud" onChoose={onChoose} />);
    expect(screen.getByText(cloudCopy.target.label)).toBeInTheDocument();
    const cloudBtn = screen.getByRole('button', { name: cloudCopy.target.cloud });
    const jetsonBtn = screen.getByRole('button', { name: cloudCopy.target.jetson });
    expect(cloudBtn).toHaveAttribute('aria-pressed', 'true');
    expect(jetsonBtn).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(jetsonBtn);
    expect(onChoose).toHaveBeenCalledWith('jetson');
  });

  it('a disabled cloud option says why', () => {
    for (const reason of ['not_configured', 'no_robot', 'not_inferable', 'old_image']) {
      const { unmount } = render(<TargetSwitch cloud={{ offered: true, enabled: false, reason }} target="jetson" onChoose={() => {}} />);
      expect(screen.getByRole('button', { name: cloudCopy.target.cloud })).toBeDisabled();
      expect(screen.getByTestId('cloud-disabled-reason')).toHaveTextContent(cloudCopy.disabled[reason]);
      unmount();
    }
  });

  it('is locked while a cloud run is live', () => {
    const onChoose = vi.fn();
    render(<TargetSwitch cloud={{ offered: true, enabled: true, reason: null }} target="cloud" onChoose={onChoose} lockedReason="Läuft in der Cloud" />);
    expect(screen.getByRole('button', { name: cloudCopy.target.cloud })).toBeDisabled();
    expect(screen.getByRole('button', { name: cloudCopy.target.jetson })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: cloudCopy.target.jetson }));
    expect(onChoose).not.toHaveBeenCalled();
  });
});
