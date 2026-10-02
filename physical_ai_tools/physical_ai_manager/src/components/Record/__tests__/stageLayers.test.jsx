// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The smaller stage pieces (spec §3.5 / §3.9 / §3.12): the recording frame,
// the two Speichern chips and their timings (≥ 400 ms, 1200 ms per saved
// episode), the problem banner, the stage card, the header and the switch.

import React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import RecordingFrame from '../RecordingFrame';
import SavingChips, { SAVING_MIN_MS, SAVED_MS } from '../SavingChips';
import ProblemBanner, { splitFirstSentence } from '../ProblemBanner';
import StageCard from '../StageCard';
import RecordHeader from '../RecordHeader';
import RecordStage from '../RecordStage';
import StageViewSwitch from '../StageViewSwitch';

function frameClock() {
  const subs = new Set();
  return {
    subscribe: (fn) => { subs.add(fn); return () => subs.delete(fn); },
    emit: (frac) => subs.forEach((fn) => fn({ frac })),
  };
}

describe('RecordingFrame', () => {
  const TEXT = { badge: 'REC 00:07', remaining: '13 s', until: 'noch bis zum Speichern' };

  it('shows nothing while not recording', () => {
    render(<RecordingFrame active={false} text={TEXT} />);
    expect(screen.queryByTestId('rec-recording-frame')).toBeNull();
  });

  it('badge with the live dot, the seconds left, and the bottom line shrinking with the clock', () => {
    const clock = frameClock();
    const { rerender } = render(<RecordingFrame active text={TEXT} subscribeFrame={clock.subscribe} />);
    expect(screen.getByText('REC 00:07')).toBeInTheDocument();
    expect(screen.getByText('noch bis zum Speichern')).toBeInTheDocument();
    expect(document.querySelector('svg[data-icon="liveRecording"]')).toHaveClass('rec-blink'); // eslint-disable-line testing-library/no-node-access
    act(() => clock.emit(0.75));
    expect(screen.getByTestId('rec-recbar').style.width).toBe('25%');
    expect(screen.getByTestId('rec-remaining')).not.toHaveClass('hot');
    rerender(<RecordingFrame active hot text={{ ...TEXT, remaining: '3 s' }} subscribeFrame={clock.subscribe} />);
    expect(screen.getByTestId('rec-remaining')).toHaveClass('hot');
  });
});

describe('SavingChips', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('the saving chip (loader icon, no bar) stays at least SAVING_MIN_MS', () => {
    const { rerender } = render(<SavingChips saving savingText="Episode 2 wird gespeichert …" />);
    const chip = screen.getByTestId('rec-saving-chip');
    expect(chip).toHaveTextContent('Episode 2 wird gespeichert …');
    expect(chip.querySelector('svg[data-icon="loading"]')).toHaveClass('animate-spin'); // eslint-disable-line testing-library/no-node-access
    expect(screen.queryByRole('progressbar')).toBeNull();
    act(() => { vi.advanceTimersByTime(66); });
    rerender(<SavingChips saving={false} savingText="" />);
    expect(screen.getByTestId('rec-saving-chip')).toHaveTextContent('Episode 2 wird gespeichert …');
    act(() => { vi.advanceTimersByTime(SAVING_MIN_MS - 66 + 5); });
    expect(screen.queryByTestId('rec-saving-chip')).toBeNull();
  });

  it('the saved chip drops in for SAVED_MS per new saved episode, not for one already saved at mount', () => {
    const { rerender } = render(<SavingChips savedKey="s1:1" savedText="Episode 1 gespeichert" />);
    expect(screen.queryByTestId('rec-saved-chip')).toBeNull();
    rerender(<SavingChips savedKey="s1:2" savedText="Episode 2 gespeichert" />);
    expect(screen.getByTestId('rec-saved-chip')).toHaveTextContent('Episode 2 gespeichert');
    act(() => { vi.advanceTimersByTime(SAVED_MS + 5); });
    expect(screen.queryByTestId('rec-saved-chip')).toBeNull();
  });
});

describe('ProblemBanner', () => {
  it('splits the first sentence off and sets it bold', () => {
    expect(splitFirstSentence('Nur noch 1,2 GB frei. Lösche alte Datensätze.')).toEqual(['Nur noch 1,2 GB frei.', 'Lösche alte Datensätze.']);
    expect(splitFirstSentence('Ein Satz.')).toEqual(['Ein Satz.', '']);
  });

  it('draws nothing without a problem', () => {
    render(<ProblemBanner problem={null} />);
    expect(screen.queryByTestId('rec-banner')).toBeNull();
  });

  it.each([['warn', 'warning'], ['bad', 'warning'], ['info', 'info']])('%s banner with its icon, polite', (kind, icon) => {
    render(<ProblemBanner problem={{ kind, textDe: 'Die Szenen-Kamera liefert nur 11 statt 30 Bilder pro Sekunde. Steck die Kamera direkt am PC ein.' }} />);
    const b = screen.getByRole('status');
    expect(b).toHaveAttribute('aria-live', 'polite');
    expect(b).toHaveAttribute('data-kind', kind);
    expect(b.querySelector(`svg[data-icon="${icon}"]`)).not.toBeNull(); // eslint-disable-line testing-library/no-node-access
    expect(b.querySelector('b')).toHaveTextContent('Die Szenen-Kamera liefert nur 11 statt 30 Bilder pro Sekunde.'); // eslint-disable-line testing-library/no-node-access
  });

  it('the „Startseite" in a leader-not-activated problem is a real link (Q5)', () => {
    const onGoHome = vi.fn();
    render(
      <ProblemBanner
        problem={{ kind: 'bad', textDe: 'Der Roboter ist nicht aktiviert, der Leader-Arm sendet keine Daten. Aktiviere ihn auf der Startseite.', linkToHome: true }}
        homeLabel="Startseite"
        onGoHome={onGoHome}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: 'Startseite' }));
    expect(onGoHome).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('status')).toHaveTextContent('Aktiviere ihn auf der Startseite.');
  });
});

describe('StageCard / RecordHeader / RecordStage / StageViewSwitch', () => {
  it('the stage card names the situation', () => {
    render(<StageCard kind="offline" title="Keine Verbindung zum Roboter" body="Die Umgebung läuft nicht." />);
    const card = screen.getByTestId('rec-stage-card');
    expect(card).toHaveAttribute('data-kind', 'offline');
    expect(screen.getByRole('heading', { name: 'Keine Verbindung zum Roboter' })).toBeInTheDocument();
    expect(card.querySelector('svg[data-icon="unplugged"]')).not.toBeNull(); // eslint-disable-line testing-library/no-node-access
  });

  it('the header shows the task name and the robot', () => {
    render(<RecordHeader eyebrow="Aufnahme" title="Würfel in die Schale" robotName="OMX – Voll" connection={<span>Verbunden</span>} />);
    expect(screen.getByRole('heading', { level: 1, name: 'Würfel in die Schale' })).toBeInTheDocument();
    expect(screen.getByText('Aufnahme')).toBeInTheDocument();
    expect(screen.getByTestId('rec-robot-pill')).toHaveTextContent('OMX – Voll');
    expect(screen.getByText('Verbunden')).toBeInTheDocument();
  });

  it('the stage draws the red frame while recording and pulses it when hot', () => {
    const { rerender } = render(<RecordStage color="var(--rec-run)" recording view="both" />);
    expect(screen.getByTestId('rec-stage')).toHaveClass('is-run');
    expect(screen.getByTestId('rec-stage')).not.toHaveClass('is-last3');
    expect(screen.getByTestId('rec-tiles')).toHaveClass('v-both');
    rerender(<RecordStage color="var(--rec-run)" recording hot view="cams" />);
    expect(screen.getByTestId('rec-stage')).toHaveClass('is-last3');
  });

  it('the switch reports a new choice only', () => {
    const onChange = vi.fn();
    render(
      <StageViewSwitch
        groupLabel="Ansicht"
        value="cams"
        onChange={onChange}
        options={[{ value: 'cams', label: 'Kameras' }, { value: '3d', label: '3D' }, { value: 'both', label: 'Beides' }]}
      />
    );
    expect(screen.getByRole('group', { name: 'Ansicht' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Kameras' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Kameras' }));
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Beides' }));
    expect(onChange).toHaveBeenCalledWith('both');
  });
});
