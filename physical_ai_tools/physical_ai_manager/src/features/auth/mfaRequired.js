// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The cloud API answers every admin route with 403 `{"code": "mfa_required"}`
// while the session is not two-factor (aal2, owner decision 2026-11-10;
// cloud_training_api/app/auth.py::get_current_admin). services/apiClient.js
// reports that answer here, and components/admin/AdminMfaGate.jsx, the one
// listener, returns the admin to its TOTP screen. A module-level signal, not
// Redux: it carries no state, only "ask for the second factor again".

export const MFA_REQUIRED_CODE = 'mfa_required';

const listeners = new Set();

/** Subscribe; returns the unsubscribe function. */
export function onMfaRequired(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function notifyMfaRequired() {
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch {
      // one listener's failure never stops another
    }
  }
}
