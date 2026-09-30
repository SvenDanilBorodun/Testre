// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The action bar (spec §3.4 / §3.6): the pill, the buttons with their key
// labels (the banned glyphs ⇧ ⌘ are spelled „Strg+Umschalt+X", H9), disabled
// buttons without a key hint, the loader on a starting Start, the question
// that takes the bar's place, and the sound switch.

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import ActionBar from '../ActionBar';
import EpisodeDots from '../EpisodeDots';

const PILL = { icon: 'liveRecording', title: 'Aufnahme', sub: 'Episode 2 von 5 · noch 12 s', color: 'var(--rec-run)' };
const RECORDING_BUTTONS = [
  { id: 'redo', label: 'Wiederholen', icon: 'again', kbd: '←', variant: 'ghost' },
  { id: 'saveNow', label: 'Jetzt speichern', icon: 'check', kbd: '→', variant: 'go' },
  { id: 'end', label: 'Beenden', icon: 'stop', kbd: 'Strg+Umschalt+X', variant: 'end' },
];
const MUTE = (muted, onToggle = () => {}) => ({ muted, onToggle, labelOff: 'Ton ausschalten', labelOn: 'Ton einschalten' });

describe('ActionBar', () => {
  it('is a named region with the pill, the buttons and their German key labels', () => {
    const onAction = vi.fn();
    render(<ActionBar ariaLabel="Aufnahmesteuerung" pill={PILL} buttons={RECORDING_BUTTONS} onAction={onAction} mute={MUTE(false)} />);
    expect(screen.getByRole('region', { name: 'Aufnahmesteuerung' })).toBeInTheDocument();
    expect(screen.getByText('Aufnahme')).toBeInTheDocument();
    expect(screen.getByText('Episode 2 von 5 · noch 12 s')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Wiederholen/ })).toHaveTextContent('←');
    expect(screen.getByRole('button', { name: /Jetzt speichern/ })).toHaveTextContent('→');
    expect(screen.getByRole('button', { name: /Beenden/ })).toHaveTextContent('Strg+Umschalt+X');
    fireEvent.click(screen.getByRole('button', { name: /Jetzt speichern/ }));
    expect(onAction).toHaveBeenCalledWith('saveNow');
  });

  it('a disabled button shows no key and does nothing; its title says why', () => {
    const onAction = vi.fn();
    render(
      <ActionBar
        ariaLabel="Aufnahmesteuerung"
        pill={PILL}
        buttons={[{ id: 'end', label: 'Beenden', icon: 'stop', kbd: 'Strg+Umschalt+X', variant: 'end', disabled: true, title: 'Erst das Kollisionsfenster abschließen.' }]}
        onAction={onAction}
      />
    );
    const b = screen.getByRole('button', { name: /Beenden/ });
    expect(b).toBeDisabled();
    expect(b).toHaveAttribute('title', 'Erst das Kollisionsfenster abschließen.');
    expect(b).not.toHaveTextContent('Strg+Umschalt+X');
    fireEvent.click(b);
    expect(onAction).not.toHaveBeenCalled();
  });

  it('a starting Start is disabled and shows the loader icon', () => {
    render(
      <ActionBar
        ariaLabel="Aufnahmesteuerung"
        pill={{ icon: 'loading', title: 'Startet …', sub: 'Aufnahme wird vorbereitet', color: 'var(--rec-reset)' }}
        buttons={[{ id: 'start', label: 'Aufnahme starten', icon: 'play', kbd: 'Leertaste', variant: 'primary', size: 'xl', disabled: true, spinning: true }]}
      />
    );
    const b = screen.getByRole('button', { name: /Aufnahme starten/ });
    expect(b).toBeDisabled();
    expect(b.querySelector('svg[data-icon="loading"]')).not.toBeNull(); // eslint-disable-line testing-library/no-node-access
  });

  it('the question takes the bar\'s place and reports the chosen answer', () => {
    const onAnswer = vi.fn();
    render(
      <ActionBar
        ariaLabel="Aufnahmesteuerung"
        pill={PILL}
        buttons={RECORDING_BUTTONS}
        onAnswer={onAnswer}
        question={{
          title: 'Episode 2 ist noch nicht fertig.',
          sub: 'Die Zeit läuft weiter, bis du dich entscheidest.',
          answers: [
            { id: 'keep', label: 'Behalten und beenden', icon: 'check', variant: 'ghost' },
            { id: 'discard', label: 'Verwerfen und beenden', variant: 'end' },
            { id: 'back', label: 'Weiter aufnehmen', variant: 'primary', kbd: 'Esc' },
          ],
        }}
      />
    );
    expect(screen.queryByRole('button', { name: /Wiederholen/ })).toBeNull();
    expect(screen.getByRole('group', { name: 'Episode 2 ist noch nicht fertig.' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Weiter aufnehmen/ })).toHaveTextContent('Esc');
    fireEvent.click(screen.getByRole('button', { name: 'Verwerfen und beenden' }));
    expect(onAnswer).toHaveBeenCalledWith('discard');
  });

  it('the sound switch names what it will do and toggles', () => {
    const onToggle = vi.fn();
    const { rerender } = render(<ActionBar ariaLabel="Aufnahmesteuerung" pill={PILL} mute={MUTE(false, onToggle)} />);
    fireEvent.click(screen.getByRole('button', { name: 'Ton ausschalten' }));
    expect(onToggle).toHaveBeenCalledTimes(1);
    rerender(<ActionBar ariaLabel="Aufnahmesteuerung" pill={PILL} mute={MUTE(true, onToggle)} />);
    expect(screen.getByRole('button', { name: 'Ton einschalten' })).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('EpisodeDots', () => {
  it('done with a check, the current one pulsing, a redo badge, and „+k" past twelve', () => {
    const dots = Array.from({ length: 12 }, (_, i) => ({ n: i + 1, done: i < 2, current: i === 2, redo: i === 1 }));
    render(<EpisodeDots label="Episoden" dots={dots} more="+3" color="var(--rec-run)" />);
    const group = screen.getByRole('group', { name: 'Episoden' });
    const items = group.querySelectorAll('.rec-epd'); // eslint-disable-line testing-library/no-node-access
    expect(items).toHaveLength(12);
    expect(items[0]).toHaveAttribute('data-state', 'done');
    expect(items[0].querySelector('svg[data-icon="check"]')).not.toBeNull(); // eslint-disable-line testing-library/no-node-access
    expect(items[1]).toHaveAttribute('data-redo', 'true');
    expect(items[1].querySelector('svg[data-icon="again"]')).not.toBeNull(); // eslint-disable-line testing-library/no-node-access
    expect(items[2]).toHaveAttribute('data-state', 'current');
    expect(items[2]).toHaveClass('cur');
    expect(items[3]).toHaveTextContent('4');
    expect(screen.getByText('+3')).toBeInTheDocument();
  });

  it('draws nothing without dots', () => {
    render(<EpisodeDots label="Episoden" dots={[]} />);
    expect(screen.queryByRole('group', { name: 'Episoden' })).toBeNull();
  });
});

// Review V2-3: a mouse click must not leave the focus on a button, or Space
// („Aufnahme starten") presses that button again. Keyboard focus stays.
describe('ActionBar — focus after a click', () => {
  it('a pointer click gives the focus back; a keyboard click keeps it', () => {
    render(<ActionBar ariaLabel="Aufnahmesteuerung" pill={PILL} buttons={RECORDING_BUTTONS} onAction={() => {}} mute={MUTE(false)} />);
    const redo = screen.getByRole('button', { name: /Wiederholen/ });
    redo.focus();
    fireEvent.click(redo, { detail: 1 });
    expect(redo).not.toHaveFocus();
    redo.focus();
    fireEvent.click(redo, { detail: 0 });
    expect(redo).toHaveFocus();
    const mute = screen.getByRole('button', { name: 'Ton ausschalten' });
    mute.focus();
    fireEvent.click(mute, { detail: 1 });
    expect(mute).not.toHaveFocus();
  });
});

// Review V2-7: the bar sheds only when its row really does not fit — measured
// after layout (jsdom measures nothing, so the measurement is injected here).
describe('ActionBar — shedding only when the row does not fit', () => {
  const TWO = [
    { id: 'skip', label: 'Jetzt weiter', icon: 'skipForward', kbd: '→', variant: 'primary' },
    { id: 'end', label: 'Beenden', icon: 'stop', kbd: 'Strg+Umschalt+X', variant: 'end' },
  ];
  const bar = () => screen.getByTestId('rec-actionbar');

  it('a row that fits sheds nothing: the dots keep their label and size', () => {
    render(<ActionBar ariaLabel="Aufnahmesteuerung" pill={PILL} buttons={RECORDING_BUTTONS} fitsOneRow={() => true} />);
    expect(bar()).not.toHaveAttribute('data-shed');
  });

  it('three buttons that do not fit shed the Beenden key first, then the other keys, then label and dots', () => {
    const { rerender } = render(
      <ActionBar ariaLabel="Aufnahmesteuerung" pill={PILL} buttons={RECORDING_BUTTONS} fitsOneRow={(_el, step) => step >= 1} />
    );
    expect(bar()).toHaveAttribute('data-shed', 'endKey');
    expect(bar()).toHaveClass('rec-shed-endkey');
    rerender(<ActionBar ariaLabel="Aufnahmesteuerung" pill={PILL} buttons={RECORDING_BUTTONS} fitsOneRow={(_el, step) => step >= 3} />);
    // A new measurement function is not a content change; the key is.
    rerender(
      <ActionBar
        ariaLabel="Aufnahmesteuerung"
        pill={{ ...PILL, sub: 'Episode 1 von 3 · noch 11 s' }}
        buttons={RECORDING_BUTTONS}
        fitsOneRow={(_el, step) => step >= 3}
      />
    );
    expect(bar()).toHaveAttribute('data-shed', 'endKey keys label');
  });

  it('with two buttons nothing is ever shed (the dots may wrap, like the mockup)', () => {
    render(<ActionBar ariaLabel="Aufnahmesteuerung" pill={PILL} buttons={TWO} fitsOneRow={() => false} />);
    expect(bar()).not.toHaveAttribute('data-shed');
    expect(screen.getByRole('button', { name: /Beenden/ })).toHaveTextContent('Strg+Umschalt+X');
  });
});
