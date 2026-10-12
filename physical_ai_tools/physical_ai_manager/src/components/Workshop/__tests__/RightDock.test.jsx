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
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RightDock from '../RightDock';

function makeTabs(opts = {}) {
  return [
    { id: 'camera', label: 'Kamera', icon: 'camera', render: () => <div>CAMERA_BODY</div> },
    {
      id: 'control',
      label: 'Steuern',
      icon: 'gamepad',
      busy: !!opts.controlBusy,
      render: () => <div>CONTROL_BODY</div>,
    },
    {
      id: 'record',
      label: 'Aufnehmen',
      icon: 'record',
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
    // Each rail button draws its registry icon, decorative.
    // eslint-disable-next-line testing-library/no-node-access
    const icon = screen.getByRole('button', { name: 'Kamera' }).querySelector('svg[data-icon="camera"]');
    expect(icon).toHaveAttribute('aria-hidden', 'true');
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

describe('RightDock — sizes are CSS variables (WI-1.7)', () => {
  test('two open panels: a split separator between them, shown below md too', () => {
    render(<RightDock tabs={makeTabs()} openIds={['camera', 'control']} collapsed={false} />);
    const sep = screen.getByRole('separator');
    expect(sep).toHaveAttribute('aria-orientation', 'horizontal');
    expect(sep).toHaveAttribute('aria-label', 'Aufteilung der beiden Werkzeuge');
    expect(sep.className.split(/\s+/)).not.toContain('hidden');
    // The two sections keep their labels and order, the separator between them.
    const sections = screen.getAllByRole('region');
    expect(sections.map((x) => x.getAttribute('aria-label'))).toEqual(['Kamera', 'Steuern']);
    // eslint-disable-next-line testing-library/no-node-access
    expect(sections[0].nextElementSibling).toBe(sep);
    // eslint-disable-next-line testing-library/no-node-access
    expect(sep.nextElementSibling).toBe(sections[1]);
    // The split is the body's variable (the divider writes it there).
    // eslint-disable-next-line testing-library/no-node-access
    expect(sep.parentElement.style.getPropertyValue('--dock-split')).toBe('0.5');
  });

  test('one open panel: no separator', () => {
    render(<RightDock tabs={makeTabs()} openIds={['camera']} collapsed={false} />);
    expect(screen.queryByRole('separator')).toBeNull();
  });

  test('a re-render keeps the very same <section> nodes (a live JogPanel is never remounted)', () => {
    const { rerender } = render(<RightDock tabs={makeTabs()} openIds={['camera', 'control']} collapsed={false} />);
    const before = screen.getAllByRole('region');
    rerender(<RightDock tabs={makeTabs({ controlBusy: true })} openIds={['camera', 'control']} collapsed={false} />);
    const after = screen.getAllByRole('region');
    expect(after[0]).toBe(before[0]);
    expect(after[1]).toBe(before[1]);
  });

  test('the dock never sets --dock-w itself — it reads the row\'s variable', () => {
    render(<RightDock tabs={makeTabs()} openIds={['camera']} collapsed={false} />);
    const aside = screen.getByRole('complementary', { name: 'Werkzeuge' });
    expect(aside.style.getPropertyValue('--dock-w')).toBe('');
    expect(aside.className).toContain('md:w-[var(--dock-w,380px)]');
  });

  test('a stored split is read back; a committed one is written', () => {
    window.localStorage.setItem('edubotics_workshop_dock_split', '0.3');
    try {
      render(<RightDock tabs={makeTabs()} openIds={['camera', 'control']} collapsed={false} />);
      const sep = screen.getByRole('separator');
      expect(sep).toHaveAttribute('aria-valuenow', '30');
      fireEvent.keyDown(sep, { key: 'ArrowDown' });
      expect(window.localStorage.getItem('edubotics_workshop_dock_split')).toBe('0.32');
      expect(sep).toHaveAttribute('aria-valuenow', '32');
    } finally {
      window.localStorage.removeItem('edubotics_workshop_dock_split');
    }
  });
});
