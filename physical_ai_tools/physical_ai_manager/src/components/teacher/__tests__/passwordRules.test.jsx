// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The password rule of the teacher web (owner decision 2026-11-10): a new or
// reset password is at least 12 characters for a teacher (set by an admin) and
// at least 8 for a student (set by a teacher). Every form states the rule in
// German and refuses BEFORE anything is sent; the API refuses the same way
// (cloud_training_api/app/services/passwords.py).

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import toast from 'react-hot-toast';
import CreateTeacherModal from '../../admin/CreateTeacherModal';
import CreateStudentModal from '../CreateStudentModal';
import PasswordResetModal from '../PasswordResetModal';
import {
  PASSWORD_MAX,
  STAFF_PASSWORD_MIN,
  STUDENT_PASSWORD_MIN,
  passwordErrorDe,
  passwordHintDe,
} from '../../../utils/passwordRules';

vi.mock('react-hot-toast', () => ({ __esModule: true, default: { success: vi.fn(), error: vi.fn() } }));

beforeEach(() => vi.clearAllMocks());

describe('passwordRules', () => {
  test('the numbers and the sentences', () => {
    expect([STAFF_PASSWORD_MIN, STUDENT_PASSWORD_MIN, PASSWORD_MAX]).toEqual([12, 8, 128]);
    expect(passwordHintDe('staff')).toBe('Mindestens 12 Zeichen (höchstens 128).');
    expect(passwordHintDe('student')).toBe('Mindestens 8 Zeichen (höchstens 128).');
    expect(passwordErrorDe('x'.repeat(11), 'staff')).toBe(
      'Das Passwort muss für Lehrkräfte und Admins mindestens 12 Zeichen lang sein.',
    );
    expect(passwordErrorDe('x'.repeat(12), 'staff')).toBeNull();
    expect(passwordErrorDe('x'.repeat(7), 'student')).toBe(
      'Das Passwort muss für Schülerinnen und Schüler mindestens 8 Zeichen lang sein.',
    );
    expect(passwordErrorDe('x'.repeat(8), 'student')).toBeNull();
    expect(passwordErrorDe('x'.repeat(129), 'student')).toBe(
      'Das Passwort darf höchstens 128 Zeichen lang sein.',
    );
    expect(passwordErrorDe(undefined, 'student')).not.toBeNull();
    // code points, like the server's len(): eight emoji are eight characters
    expect(passwordErrorDe('😀'.repeat(8), 'student')).toBeNull();
    expect(() => passwordErrorDe('x', 'teacher')).toThrow();
  });
});

function typeInto(input, value) {
  fireEvent.change(input, { target: { value } });
}

describe('CreateTeacherModal', () => {
  function renderIt() {
    const onSubmit = vi.fn().mockResolvedValue({});
    render(<CreateTeacherModal onClose={vi.fn()} onSubmit={onSubmit} />);
    typeInto(screen.getByRole('textbox', { name: /Vollständiger Name/ }), 'Frau Muster');
    typeInto(screen.getByRole('textbox', { name: /Benutzername/ }), 'frau.muster');
    const password = screen.getByRole('textbox', { name: /Start-Passwort/ });
    // a `submit` dispatched on the field bubbles to its form, like Enter in it
    return { onSubmit, password, form: password };
  }

  test('states the rule and refuses 11 characters before sending', () => {
    const { onSubmit, password, form } = renderIt();
    expect(screen.getByText('Mindestens 12 Zeichen (höchstens 128).')).toBeInTheDocument();
    typeInto(password, 'kurz-11zeic');
    expect(screen.getByRole('button', { name: 'Erstellen' })).toBeDisabled();
    fireEvent.submit(form);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(
      'Das Passwort muss für Lehrkräfte und Admins mindestens 12 Zeichen lang sein.',
    );
  });

  test('sends 12 characters', () => {
    const { onSubmit, password, form } = renderIt();
    typeInto(password, 'genau-12-zei');
    expect(screen.getByRole('button', { name: 'Erstellen' })).not.toBeDisabled();
    fireEvent.submit(form);
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ password: 'genau-12-zei' }));
  });
});

describe('CreateStudentModal', () => {
  function renderIt() {
    const onSubmit = vi.fn().mockResolvedValue({});
    render(
      <CreateStudentModal onClose={vi.fn()} onSubmit={onSubmit} poolAvailable={5} />,
    );
    typeInto(screen.getByRole('textbox', { name: /Vollständiger Name/ }), 'Max Muster');
    typeInto(screen.getByRole('textbox', { name: /Benutzername/ }), 'max.muster');
    const password = screen.getByRole('textbox', { name: /Start-Passwort/ });
    return { onSubmit, password, form: password };
  }

  test('refuses 7 characters before sending, sends 8', () => {
    const { onSubmit, password, form } = renderIt();
    expect(screen.getByText(/Mindestens 8 Zeichen \(höchstens 128\)\./)).toBeInTheDocument();
    typeInto(password, 'sieben7');
    fireEvent.submit(form);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(
      'Das Passwort muss für Schülerinnen und Schüler mindestens 8 Zeichen lang sein.',
    );
    typeInto(password, 'achtacht');
    fireEvent.submit(form);
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ password: 'achtacht' }));
  });
});

describe('PasswordResetModal', () => {
  test('a student reset needs 8 characters', () => {
    const onSubmit = vi.fn().mockResolvedValue({});
    render(
      <PasswordResetModal username="max" onClose={vi.fn()} onSubmit={onSubmit} />,
    );
    const input = screen.getByPlaceholderText('mindestens 8 Zeichen');
    typeInto(input, 'sieben7');
    expect(screen.getByRole('button', { name: 'Setzen' })).toBeDisabled();
    fireEvent.submit(input);
    expect(onSubmit).not.toHaveBeenCalled();
    typeInto(input, 'achtacht');
    fireEvent.submit(input);
    expect(onSubmit).toHaveBeenCalledWith('achtacht');
  });

  test('an admin resetting a teacher needs 12 characters', () => {
    const onSubmit = vi.fn().mockResolvedValue({});
    render(
      <PasswordResetModal username="frau.k" audience="staff" onClose={vi.fn()} onSubmit={onSubmit} />,
    );
    expect(screen.getByText(/Mindestens 12 Zeichen/)).toBeInTheDocument();
    expect(screen.getByText(/Gib das neue Passwort der Lehrkraft weiter\./)).toBeInTheDocument();
    const input = screen.getByPlaceholderText('mindestens 12 Zeichen');
    typeInto(input, 'achtacht-11');
    fireEvent.submit(input);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(
      'Das Passwort muss für Lehrkräfte und Admins mindestens 12 Zeichen lang sein.',
    );
    typeInto(input, 'zwoelf-zeich');
    fireEvent.submit(input);
    expect(onSubmit).toHaveBeenCalledWith('zwoelf-zeich');
  });
});
