/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Neu": each notation is shown with its icon (Blöcke a puzzle piece, Python
// the custom snake, Java a coffee cup), drawn by components/icons.

/* eslint-disable testing-library/no-node-access */

import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import NewProgramDialog from '../NewProgramDialog';
import { CODE_DE } from '../codeMessagesDe';

describe('NewProgramDialog icons', () => {
  test('the trigger and every choice carry their icon; choosing still creates', () => {
    const onCreate = vi.fn();
    render(<NewProgramDialog onCreate={onCreate} />);
    const trigger = screen.getByRole('button', { name: CODE_DE.NEW_TITLE });
    expect(Array.from(trigger.querySelectorAll('svg')).map((s) => s.getAttribute('data-icon')))
      .toEqual(['sparkles', 'chevronDown']);
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: CODE_DE.NEW_TITLE });
    const choices = within(dialog).getAllByRole('button');
    expect(choices.map((b) => b.querySelector('svg').getAttribute('data-icon')))
      .toEqual(['blocks', 'python', 'java']);
    fireEvent.click(choices[1]);
    expect(onCreate).toHaveBeenCalledWith('python');
  });
});
