/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Redesign — RightDock is a CONTROLLED, tabbed, collapsible dock: it renders a
// rail button per non-hidden tab, shows the open panels' content, and reports
// open/close/collapse intent via callbacks. Busy panels can't be closed.

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RightDock from '../RightDock';

function makeTabs(opts = {}) {
  return [
    { id: 'camera', label: 'Kamera', icon: '📷', render: () => <div>CAMERA_BODY</div> },
    {
      id: 'control',
      label: 'Steuern',
      icon: '🎮',
      busy: !!opts.controlBusy,
      render: () => <div>CONTROL_BODY</div>,
    },
    {
      id: 'record',
      label: 'Aufnehmen',
      icon: '⏺',
      hidden: !!opts.recordHidden,
      render: () => <div>RECORD_BODY</div>,
    },
  ];
}

describe('RightDock', () => {
  test('renders a rail button for every non-hidden tab', () => {
    render(
      <RightDock tabs={makeTabs({ recordHidden: true })} openIds={[]} collapsed={false} />,
    );
    expect(screen.getByRole('button', { name: 'Kamera' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Steuern' })).toBeInTheDocument();
    // A hidden tab has no rail button.
    expect(screen.queryByRole('button', { name: 'Aufnehmen' })).toBeNull();
  });

  test('a rail click reports the toggle intent', async () => {
    const onToggleTab = vi.fn();
    render(
      <RightDock
        tabs={makeTabs()}
        openIds={[]}
        collapsed={false}
        onToggleTab={onToggleTab}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Steuern' }));
    expect(onToggleTab).toHaveBeenCalledWith('control');
  });

  test('open panels render their content; a hidden id is dropped', () => {
    render(
      <RightDock
        tabs={makeTabs({ recordHidden: true })}
        openIds={['camera', 'record']}
        collapsed={false}
      />,
    );
    expect(screen.getByText('CAMERA_BODY')).toBeInTheDocument();
    // 'record' is hidden → not rendered even though it's in openIds.
    expect(screen.queryByText('RECORD_BODY')).toBeNull();
  });

  test('the per-panel close button reports onCloseTab', async () => {
    const onCloseTab = vi.fn();
    render(
      <RightDock
        tabs={makeTabs()}
        openIds={['camera']}
        collapsed={false}
        onCloseTab={onCloseTab}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /Kamera Panel schließen/ }));
    expect(onCloseTab).toHaveBeenCalledWith('camera');
  });

  test('a busy panel cannot be closed (✕ disabled)', () => {
    render(
      <RightDock
        tabs={makeTabs({ controlBusy: true })}
        openIds={['control']}
        collapsed={false}
        onCloseTab={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: /Steuern Panel schließen/ })).toBeDisabled();
  });

  test('collapse hides the body and reports onToggleCollapsed', async () => {
    const onToggleCollapsed = vi.fn();
    const { rerender } = render(
      <RightDock
        tabs={makeTabs()}
        openIds={['camera']}
        collapsed={false}
        onToggleCollapsed={onToggleCollapsed}
      />,
    );
    expect(screen.getByText('CAMERA_BODY')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Werkzeuge einklappen' }));
    expect(onToggleCollapsed).toHaveBeenCalled();

    // When the parent flips collapsed, the body (and its panel content) is gone;
    // the rail (with the expand button) stays.
    rerender(
      <RightDock
        tabs={makeTabs()}
        openIds={['camera']}
        collapsed={true}
        onToggleCollapsed={onToggleCollapsed}
      />,
    );
    expect(screen.queryByText('CAMERA_BODY')).toBeNull();
    expect(screen.getByRole('button', { name: 'Werkzeuge ausklappen' })).toBeInTheDocument();
  });
});
