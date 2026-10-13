// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import Key from '../Key';
import RoundKey from '../RoundKey';

describe('Key', () => {
  it('a button of type button with the variant and size classes', () => {
    render(<Key>Speichern</Key>);
    const b = screen.getByRole('button', { name: 'Speichern' });
    expect(b).toHaveAttribute('type', 'button');
    expect(b).toHaveClass('su-key', 'su-key--default', 'su-key--md');
  });

  it.each([['signal', 'lg'], ['dark', 'sm'], ['flat', 'md'], ['danger', 'md']])('variant %s size %s', (variant, size) => {
    render(<Key variant={variant} size={size}>Los</Key>);
    expect(screen.getByRole('button', { name: 'Los' })).toHaveClass(`su-key--${variant}`, `su-key--${size}`);
  });

  it('draws a leading icon and a key label; the label is part of the name (as on the Aufnahme bar)', () => {
    render(<Key icon="play" kbd="Leertaste" variant="signal">Aufnahme starten</Key>);
    const b = screen.getByRole('button', { name: /^Aufnahme starten\s*Leertaste$/ });
    // eslint-disable-next-line testing-library/no-node-access
    expect(b.querySelector('svg[data-icon="play"]')).not.toBeNull();
    // eslint-disable-next-line testing-library/no-node-access
    expect(b.querySelector('kbd.su-kbd')).toHaveTextContent('Leertaste');
  });

  it('calls the handler, releases the focus after a pointer click, keeps it after a keyboard click', () => {
    const onClick = vi.fn();
    render(<Key onClick={onClick}>Weiter</Key>);
    const b = screen.getByRole('button', { name: 'Weiter' });
    b.focus();
    fireEvent.click(b, { detail: 1 });
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(b).not.toHaveFocus();
    b.focus();
    fireEvent.click(b, { detail: 0 });
    expect(onClick).toHaveBeenCalledTimes(2);
    expect(b).toHaveFocus();
  });

  it('passes disabled, title, aria and a submit type through; a disabled key fires nothing', () => {
    const onClick = vi.fn();
    render(<Key type="submit" disabled title="Erst verbinden" aria-describedby="x" onClick={onClick}>Senden</Key>);
    const b = screen.getByRole('button', { name: 'Senden' });
    expect(b).toBeDisabled();
    expect(b).toHaveAttribute('type', 'submit');
    expect(b).toHaveAttribute('title', 'Erst verbinden');
    expect(b).toHaveAttribute('aria-describedby', 'x');
    fireEvent.click(b);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('forwards a ref to the button', () => {
    const ref = React.createRef();
    render(<Key ref={ref}>R</Key>);
    expect(ref.current).toBe(screen.getByRole('button', { name: 'R' }));
  });
});

describe('RoundKey', () => {
  afterEach(() => vi.restoreAllMocks());

  it('a visible label below the disc names the button', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<RoundKey icon="home" label="Start" />);
    const b = screen.getByRole('button', { name: 'Start' });
    expect(b).toHaveClass('su-rkey', 'su-rkey--labelled');
    // eslint-disable-next-line testing-library/no-node-access
    expect(b.querySelector('.su-rkey__label')).toHaveTextContent('Start');
    expect(err).not.toHaveBeenCalled();
  });

  it('without a label it needs an aria-label (console.error outside production)', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(<RoundKey icon="refresh" />);
    expect(err).toHaveBeenCalledWith(expect.stringMatching(/aria-label/));
    err.mockClear();
    render(<RoundKey icon="refresh" aria-label="Neu laden" title="Neu laden" />);
    expect(err).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Neu laden' })).toHaveAttribute('title', 'Neu laden');
  });

  it('size sets the disc; the icon is 0.38 of it; on = the signal disc', () => {
    render(<RoundKey icon="play" aria-label="Abspielen" size={60} on />);
    const b = screen.getByRole('button', { name: 'Abspielen' });
    expect(b).toHaveClass('su-rkey--on');
    // eslint-disable-next-line testing-library/no-node-access
    const disc = b.querySelector('.su-rkey__disc');
    expect(disc.style.width).toBe('60px');
    expect(disc.style.height).toBe('60px');
    // eslint-disable-next-line testing-library/no-node-access
    const svg = disc.querySelector('svg[data-icon="play"]');
    expect(svg.getAttribute('width')).toBe(String(Math.round(60 * 0.38)));
  });

  it('pointer-focus release, keyboard keeps it; disabled fires nothing', () => {
    const onClick = vi.fn();
    const { rerender } = render(<RoundKey icon="stop" aria-label="Stopp" onClick={onClick} />);
    const b = screen.getByRole('button', { name: 'Stopp' });
    b.focus();
    fireEvent.click(b, { detail: 1 });
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(b).not.toHaveFocus();
    b.focus();
    fireEvent.click(b, { detail: 0 });
    expect(b).toHaveFocus();
    rerender(<RoundKey icon="stop" aria-label="Stopp" onClick={onClick} disabled />);
    fireEvent.click(b);
    expect(onClick).toHaveBeenCalledTimes(2);
    expect(b).toBeDisabled();
  });
});
