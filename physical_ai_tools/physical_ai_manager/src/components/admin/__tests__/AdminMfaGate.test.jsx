// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The admin's second factor (owner decision 2026-11-10). supabase-js's MFA API
// is the one thing stubbed: the gate's decisions (dashboard at aal2, challenge
// with a verified factor, enrolment without one, back to the challenge on an
// `mfa_required` answer) are exercised against it.

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import AdminMfaGate, { MFA_DE } from '../AdminMfaGate';
import { notifyMfaRequired } from '../../../features/auth/mfaRequired';

const mfa = vi.hoisted(() => ({
  getAuthenticatorAssuranceLevel: vi.fn(),
  listFactors: vi.fn(),
  enroll: vi.fn(),
  unenroll: vi.fn(),
  challengeAndVerify: vi.fn(),
}));
vi.mock('../../../lib/supabaseClient', () => ({ supabase: { auth: { mfa } } }));

const VERIFIED = { id: 'f-1', factor_type: 'totp', status: 'verified' };
const level = (currentLevel) => ({ data: { currentLevel, nextLevel: 'aal2' }, error: null });

function renderGate() {
  const onLogout = vi.fn();
  render(
    <AdminMfaGate onLogout={onLogout}>
      <div>Admin-Dashboard</div>
    </AdminMfaGate>,
  );
  return { onLogout };
}

function enterCode(value) {
  fireEvent.change(screen.getByRole('textbox', { name: MFA_DE.codeLabel }), { target: { value } });
  fireEvent.click(screen.getByRole('button', { name: MFA_DE.confirm }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mfa.unenroll.mockResolvedValue({ data: {}, error: null });
});

describe('AdminMfaGate', () => {
  test('an aal2 session sees the dashboard and nothing else', async () => {
    mfa.getAuthenticatorAssuranceLevel.mockResolvedValue(level('aal2'));
    renderGate();
    expect(await screen.findByText('Admin-Dashboard')).toBeInTheDocument();
    expect(mfa.listFactors).not.toHaveBeenCalled();
  });

  test('aal1 with a verified factor: the challenge, then the dashboard', async () => {
    mfa.getAuthenticatorAssuranceLevel.mockResolvedValueOnce(level('aal1')).mockResolvedValue(level('aal2'));
    mfa.listFactors.mockResolvedValue({ data: { all: [VERIFIED], totp: [VERIFIED] }, error: null });
    mfa.challengeAndVerify.mockResolvedValue({ data: {}, error: null });
    renderGate();
    expect(await screen.findByRole('heading', { name: MFA_DE.challengeTitle })).toBeInTheDocument();
    expect(screen.getByText(MFA_DE.lostDevice)).toBeInTheDocument();
    expect(screen.queryByText('Admin-Dashboard')).not.toBeInTheDocument();
    enterCode('123 456');
    expect(await screen.findByText('Admin-Dashboard')).toBeInTheDocument();
    expect(mfa.challengeAndVerify).toHaveBeenCalledWith({ factorId: 'f-1', code: '123456' });
    expect(mfa.enroll).not.toHaveBeenCalled();
  });

  test('a wrong code says so in German and keeps the challenge', async () => {
    mfa.getAuthenticatorAssuranceLevel.mockResolvedValue(level('aal1'));
    mfa.listFactors.mockResolvedValue({ data: { all: [VERIFIED], totp: [VERIFIED] }, error: null });
    mfa.challengeAndVerify.mockResolvedValue({
      data: null,
      error: { code: 'mfa_verification_failed', status: 422, message: 'Invalid TOTP code entered' },
    });
    renderGate();
    await screen.findByRole('heading', { name: MFA_DE.challengeTitle });
    enterCode('000000');
    expect(await screen.findByText(MFA_DE.wrongCode)).toBeInTheDocument();
    expect(screen.queryByText('Invalid TOTP code entered')).not.toBeInTheDocument();
    expect(screen.queryByText('Admin-Dashboard')).not.toBeInTheDocument();
  });

  test('a code that is not six digits is refused before asking', async () => {
    mfa.getAuthenticatorAssuranceLevel.mockResolvedValue(level('aal1'));
    mfa.listFactors.mockResolvedValue({ data: { all: [VERIFIED], totp: [VERIFIED] }, error: null });
    renderGate();
    await screen.findByRole('heading', { name: MFA_DE.challengeTitle });
    enterCode('12a45');
    expect(await screen.findByText(MFA_DE.codeShape)).toBeInTheDocument();
    expect(mfa.challengeAndVerify).not.toHaveBeenCalled();
  });

  test('no verified factor: a stale one is removed, then the QR code, the secret and the check', async () => {
    mfa.getAuthenticatorAssuranceLevel.mockResolvedValueOnce(level('aal1')).mockResolvedValue(level('aal2'));
    mfa.listFactors.mockResolvedValue({
      data: { all: [{ id: 'old', factor_type: 'totp', status: 'unverified' }], totp: [] },
      error: null,
    });
    mfa.enroll.mockResolvedValue({
      data: { id: 'f-new', totp: { qr_code: 'data:image/svg+xml;utf-8,<qr/>', secret: 'JBSWY3DPEHPK3PXP' } },
      error: null,
    });
    mfa.challengeAndVerify.mockResolvedValue({ data: {}, error: null });
    renderGate();
    expect(await screen.findByRole('heading', { name: MFA_DE.enrollTitle })).toBeInTheDocument();
    expect(mfa.unenroll).toHaveBeenCalledWith({ factorId: 'old' });
    expect(mfa.enroll).toHaveBeenCalledWith(expect.objectContaining({ factorType: 'totp' }));
    expect(screen.getByRole('img', { name: MFA_DE.qrAlt })).toHaveAttribute(
      'src',
      'data:image/svg+xml;utf-8,<qr/>',
    );
    expect(screen.getByTestId('mfa-secret')).toHaveTextContent('JBSW Y3DP EHPK 3PXP');
    enterCode('654321');
    expect(await screen.findByText('Admin-Dashboard')).toBeInTheDocument();
    expect(mfa.challengeAndVerify).toHaveBeenCalledWith({ factorId: 'f-new', code: '654321' });
  });

  test('MFA switched off for the project is named, not a raw error', async () => {
    mfa.getAuthenticatorAssuranceLevel.mockResolvedValue(level('aal1'));
    mfa.listFactors.mockResolvedValue({ data: { all: [], totp: [] }, error: null });
    mfa.enroll.mockResolvedValue({ data: null, error: { code: 'mfa_totp_enroll_not_enabled', status: 422 } });
    const { onLogout } = renderGate();
    expect(await screen.findByText(MFA_DE.disabled)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: MFA_DE.logout }));
    expect(onLogout).toHaveBeenCalled();
  });

  test('an mfa_required answer later returns to the challenge', async () => {
    mfa.getAuthenticatorAssuranceLevel.mockResolvedValue(level('aal2'));
    mfa.listFactors.mockResolvedValue({ data: { all: [VERIFIED], totp: [VERIFIED] }, error: null });
    renderGate();
    await screen.findByText('Admin-Dashboard');
    act(() => notifyMfaRequired());
    expect(await screen.findByRole('heading', { name: MFA_DE.challengeTitle })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Admin-Dashboard')).not.toBeInTheDocument());
  });
});
