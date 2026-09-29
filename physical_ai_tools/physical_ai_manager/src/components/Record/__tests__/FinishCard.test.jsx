// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The finish card (spec §3.11): the three steps in their states, the upload
// percentage and bar, a failure's reason on its step, where the dataset was
// saved, the note line, and the actions.

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import FinishCard from '../FinishCard';

const STEP_LABELS = ['Datensatz abschließen', 'Zu Hugging Face hochladen (privat)', 'In deiner Datensatzliste eintragen'];
const steps = (states, extra = {}) => STEP_LABELS.map((label, i) => ({ key: `s${i}`, label, state: states[i], ...(extra[i] || {}) }));

describe('FinishCard', () => {
  it('while uploading: step 1 done, step 2 running with its percent and a bar, the background hint', () => {
    const onAction = vi.fn();
    render(
      <FinishCard
        eyebrow="Wird abgeschlossen"
        title="3 Episoden werden gespeichert"
        steps={steps(['done', 'now', ''], { 1: { pct: '40 %' } })}
        barPct={40}
        actions={[{ id: 'new', label: 'Neue Aufnahme', variant: 'ghost' }]}
        hint="Das Hochladen läuft im Hintergrund weiter."
        onAction={onAction}
      />
    );
    const all = document.querySelectorAll('.rec-step'); // eslint-disable-line testing-library/no-node-access
    expect([...all].map((s) => s.getAttribute('data-state'))).toEqual(['done', 'now', 'upcoming']);
    expect(screen.getByText('40 %')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '40');
    expect(screen.getByText('Das Hochladen läuft im Hintergrund weiter.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Neue Aufnahme' }));
    expect(onAction).toHaveBeenCalledWith('new');
  });

  it('done: every step done, where it was saved, and both actions', () => {
    render(
      <FinishCard
        eyebrow="Fertig"
        title="Dein Datensatz ist bereit"
        steps={steps(['done', 'done', 'done'])}
        savedAs={{ label: 'Privat gespeichert als', repoId: 'maxmuster/omx_f_Wuerfel' }}
        actions={[{ id: 'training', label: 'Weiter zum Training', variant: 'primary' }, { id: 'new', label: 'Neue Aufnahme' }]}
      />
    );
    expect(screen.getByText('Dein Datensatz ist bereit')).toBeInTheDocument();
    expect(screen.getByText('maxmuster/omx_f_Wuerfel')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Weiter zum Training' })).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).toBeNull();
  });

  it('a failed step carries its reason, and the note line shows under the title', () => {
    render(
      <FinishCard
        eyebrow="Fertig"
        title="Hochladen fehlgeschlagen"
        note="Die Aufnahme wurde nach der Kollision beendet."
        steps={steps(['done', 'failed', ''], { 1: { detail: 'Das Hochladen hat nicht begonnen.' } })}
      />
    );
    expect(screen.getByTestId('rec-finish-note')).toHaveTextContent('Die Aufnahme wurde nach der Kollision beendet.');
    expect(screen.getByText('Das Hochladen hat nicht begonnen.')).toBeInTheDocument();
  });
});
