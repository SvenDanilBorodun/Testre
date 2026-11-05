// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

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
