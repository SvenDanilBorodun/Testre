// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// releasePointerFocus moved to sucher/pointerFocus.js; the Aufnahme page's
// ActionBar re-exports the SAME function for its existing importers.

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { releasePointerFocus } from '../pointerFocus';
import { releasePointerFocus as fromActionBar } from '../../Record/ActionBar';

describe('releasePointerFocus', () => {
  it('ActionBar re-exports the very same function', () => {
    expect(fromActionBar).toBe(releasePointerFocus);
  });

  it('a pointer click (detail > 0) blurs, a keyboard click (detail 0) keeps the focus', () => {
    render(<button type="button" onClick={releasePointerFocus}>x</button>);
    const b = screen.getByRole('button', { name: 'x' });
    b.focus();
    fireEvent.click(b, { detail: 1 });
    expect(b).not.toHaveFocus();
    b.focus();
    fireEvent.click(b, { detail: 0 });
    expect(b).toHaveFocus();
  });

  it('tolerates a missing event', () => {
    expect(() => releasePointerFocus(undefined)).not.toThrow();
    expect(() => releasePointerFocus({ detail: 2, currentTarget: null })).not.toThrow();
  });
});
