// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// „Aufgabe" (spec §3.12, F6b, F6e): the four steppers stop at their limits, the
// name is capped at 60 characters, the estimate is drawn as given, the three
// save-name variants, the lock with its reason, the `task` header icon, and
// „Erweitert" — collapsed by default, with exactly five rows.

import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import TaskCard from '../TaskCard';

const LABELS = {
  title: 'Aufgabe',
  chipEditable: 'bearbeitbar',
  chipLocked: 'gesperrt',
  name: 'Aufgabenname',
  namePlaceholder: 'z. B. Würfel in die Schale',
  instruction: 'Aufgabenanweisung',
  instructionPlaceholder: 'z. B. Greife den roten Würfel und lege ihn in die Schale.',
  flow: 'Ablauf',
  dec: (l) => `${l} verringern`,
  inc: (l) => `${l} erhöhen`,
  advanced: 'Erweitert',
  upload: 'Nach dem Beenden hochladen',
  visibility: 'Sichtbarkeit',
  private: 'Privat',
  public: 'Öffentlich',
  userId: 'Benutzer-ID',
  reload: 'Neu laden',
  noUserId: 'Keine Benutzer-ID gefunden',
  fps: 'Bilder pro Sekunde',
  tags: 'Stichwörter',
};

// F6e limits (the page passes the model's STEPPER_LIMITS).
const STEPPERS = [
  { field: 'warmupTime', label: 'Aufwärmen', unit: 's', min: 0, max: 60, color: 'var(--rec-warm)' },
  { field: 'episodeTime', label: 'Episode', unit: 's', min: 3, max: 120, color: 'var(--rec-run)' },
  { field: 'resetTime', label: 'Zurücksetzen', unit: 's', min: 0, max: 60, color: 'var(--rec-reset)' },
  { field: 'numEpisodes', label: 'Episoden', unit: '', min: 1, max: 100, color: 'var(--ink-3)' },
];

const FORM = {
  taskName: 'Würfel in die Schale',
  taskInstruction: 'Greife den roten Würfel.',
  warmupTime: 5,
  episodeTime: 20,
  resetTime: 5,
  numEpisodes: 5,
  pushToHub: true,
  privateMode: true,
  userId: 'maxmuster',
  fps: 30,
  tags: ['omx_f', 'edubotics'],
};

function renderCard(props = {}) {
  const onChange = vi.fn();
  const utils = render(
    <TaskCard
      labels={LABELS}
      form={FORM}
      onChange={onChange}
      editable
      steppers={STEPPERS}
      estimate={{ parts: [[5, 'var(--rec-warm)'], [20, 'var(--rec-run)']], text: '≈ 2:05 min für 5 Episoden. Aufwärmen nur einmal am Anfang, nach der letzten Episode kein Zurücksetzen.' }}
      saveName={{ text: 'Wird privat auf Hugging Face gespeichert als', repoId: 'maxmuster/omx_f_Wuerfel-in-die-Schale' }}
      hfUsers={{ list: ['maxmuster', 'klasse7b'], reload: vi.fn() }}
      {...props}
    />
  );
  return { ...utils, onChange };
}

describe('TaskCard', () => {
  it('has the task header icon, the title and the editable chip', () => {
    renderCard();
    const header = screen.getByRole('heading', { name: 'Aufgabe' });
    expect(header.querySelector('svg[data-icon="task"]')).not.toBeNull(); // eslint-disable-line testing-library/no-node-access
    expect(screen.getByText('bearbeitbar')).toBeInTheDocument();
  });

  it('the name field is capped at 60 characters and reports changes', () => {
    const { onChange } = renderCard();
    const name = screen.getByLabelText('Aufgabenname');
    expect(name).toHaveAttribute('maxLength', '60');
    fireEvent.change(name, { target: { value: 'Turm bauen' } });
    expect(onChange).toHaveBeenCalledWith('taskName', 'Turm bauen');
    fireEvent.change(screen.getByLabelText('Aufgabenanweisung'), { target: { value: 'Staple drei Würfel.' } });
    expect(onChange).toHaveBeenCalledWith('taskInstruction', 'Staple drei Würfel.');
  });

  it('steppers move by one and stop at the F6e limits', () => {
    const { onChange } = renderCard({
      form: { ...FORM, warmupTime: 0, episodeTime: 120, resetTime: 60, numEpisodes: 1 },
    });
    expect(screen.getByRole('button', { name: 'Aufwärmen verringern' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Episode erhöhen' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Zurücksetzen erhöhen' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Episoden verringern' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Aufwärmen erhöhen' }));
    expect(onChange).toHaveBeenLastCalledWith('warmupTime', 1);
    fireEvent.click(screen.getByRole('button', { name: 'Episode verringern' }));
    expect(onChange).toHaveBeenLastCalledWith('episodeTime', 119);
    fireEvent.click(screen.getByRole('button', { name: 'Episoden erhöhen' }));
    expect(onChange).toHaveBeenLastCalledWith('numEpisodes', 2);
  });

  it('the episode stepper cannot go below 3 s', () => {
    renderCard({ form: { ...FORM, episodeTime: 3 } });
    expect(screen.getByRole('button', { name: 'Episode verringern' })).toBeDisabled();
  });

  it('draws the estimate bar and its sentence', () => {
    renderCard();
    const est = screen.getByTestId('rec-estimate');
    expect(est).toHaveTextContent('≈ 2:05 min für 5 Episoden.');
    expect(est.querySelectorAll('.rec-est-bar span')).toHaveLength(2); // eslint-disable-line testing-library/no-node-access
  });

  it.each([
    [{ text: 'Wird privat auf Hugging Face gespeichert als', repoId: 'a/omx_f_x' }, false],
    [{ text: 'Wird ÖFFENTLICH auf Hugging Face gespeichert als', repoId: 'a/omx_f_x', public: true }, true],
    [{ text: 'Wird nur auf diesem Rechner gespeichert als', repoId: 'a/omx_f_x', local: true }, false],
  ])('the save-name line: %j', (saveName, amber) => {
    renderCard({ saveName });
    const line = screen.getByTestId('rec-savename');
    expect(line).toHaveTextContent(saveName.text);
    expect(line).toHaveTextContent('a/omx_f_x');
    expect(line.classList.contains('public')).toBe(amber);
  });

  it('locked: every control is off and the reason is shown', () => {
    renderCard({ editable: false, lockedReason: 'Nicht verbunden. Du kannst die Aufgabe bearbeiten, sobald der Roboter verbunden ist.' });
    expect(screen.getByText('gesperrt')).toBeInTheDocument();
    expect(screen.getByTestId('rec-locked')).toHaveTextContent('Nicht verbunden.');
    expect(screen.getByLabelText('Aufgabenname')).toBeDisabled();
    expect(screen.getByLabelText('Aufgabenanweisung')).toBeDisabled();
    STEPPERS.forEach((s) => {
      expect(screen.getByRole('button', { name: `${s.label} erhöhen` })).toBeDisabled();
    });
  });

  it('„Erweitert" is collapsed by default and holds exactly five rows', () => {
    const { onChange } = renderCard();
    const toggle = screen.getByRole('button', { name: 'Erweitert' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('rec-adv')).toBeNull();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const adv = screen.getByTestId('rec-adv');
    expect(adv.querySelectorAll(':scope > .rec-arow')).toHaveLength(5); // eslint-disable-line testing-library/no-node-access
    const rows = within(adv);
    expect(rows.getByRole('switch', { name: 'Nach dem Beenden hochladen' })).toHaveAttribute('aria-checked', 'true');
    expect(rows.getByRole('group', { name: 'Sichtbarkeit' })).toBeInTheDocument();
    expect(rows.getByLabelText('Benutzer-ID')).toHaveValue('maxmuster');
    expect(rows.getByRole('button', { name: 'Neu laden' })).toBeInTheDocument();
    expect(rows.getByLabelText('Bilder pro Sekunde')).toHaveValue(30);
    expect(rows.getByText('Stichwörter')).toBeInTheDocument();
    expect(rows.getByText('omx_f')).toBeInTheDocument();

    fireEvent.click(rows.getByRole('button', { name: 'Öffentlich' }));
    expect(onChange).toHaveBeenCalledWith('privateMode', false);
    fireEvent.click(rows.getByRole('switch', { name: 'Nach dem Beenden hochladen' }));
    expect(onChange).toHaveBeenCalledWith('pushToHub', false);
    fireEvent.change(rows.getByLabelText('Benutzer-ID'), { target: { value: 'klasse7b' } });
    expect(onChange).toHaveBeenCalledWith('userId', 'klasse7b');
    fireEvent.change(rows.getByLabelText('Bilder pro Sekunde'), { target: { value: '15' } });
    expect(onChange).toHaveBeenCalledWith('fps', 15);
  });

  it('no Benutzer-ID offered: says so, „Neu laden" asks again', () => {
    const reload = vi.fn();
    renderCard({ form: { ...FORM, userId: undefined }, hfUsers: { list: [], reload } });
    fireEvent.click(screen.getByRole('button', { name: 'Erweitert' }));
    expect(screen.getByText('Keine Benutzer-ID gefunden')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Neu laden' }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('a refused field is marked with the reason', () => {
    renderCard({ invalid: { field: 'taskName', messageDe: 'Bitte gib einen Aufgabennamen ein.' } });
    expect(screen.getByLabelText('Aufgabenname')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('Bitte gib einen Aufgabennamen ein.')).toBeInTheDocument();
  });
});
