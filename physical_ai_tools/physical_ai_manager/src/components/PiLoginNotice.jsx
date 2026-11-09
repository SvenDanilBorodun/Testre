// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// One line above the page on an Orange Pi when the ROBOT login (not the
// EduBotics login) needs the student's attention: the agent is older than this
// page (version skew — nothing on the robot can work until it is updated), the
// exchange was refused or could not be checked, or it is still on its way.
// Renders nothing in every other state, and nothing off a Pi.

import React from 'react';
import { exchange, PI_AUTH_PHASE, PI_AUTH_TEXT_DE, usePiAuth } from '../features/piAuth/piAuth';
import Icon from './icons/Icon';

export const PI_LOGIN_NOTICE_DE = Object.freeze({
  exchanging: 'Anmeldung am Roboter …',
  retry: 'Erneut versuchen',
});

export default function PiLoginNotice({ piMode }) {
  const piAuth = usePiAuth();
  if (!piMode) return null;
  if (piAuth.phase === PI_AUTH_PHASE.LEGACY) {
    return (
      <div role="alert" className="mx-4 mt-3 rounded-[12px] bg-[var(--amber-wash)] px-3 py-2 text-sm text-[var(--ink)] flex items-start gap-2">
        <Icon name="warning" size={16} className="mt-0.5 shrink-0" />
        <span>{PI_AUTH_TEXT_DE.legacy}</span>
      </div>
    );
  }
  if (piAuth.phase === PI_AUTH_PHASE.ERROR && piAuth.error) {
    return (
      <div role="alert" className="mx-4 mt-3 rounded-[12px] bg-[var(--amber-wash)] px-3 py-2 text-sm text-[var(--ink)] flex items-start gap-2">
        <Icon name="warning" size={16} className="mt-0.5 shrink-0" />
        <span className="flex-1">{piAuth.error.message}</span>
        <button type="button" className="su-key su-key--sm" onClick={() => exchange()}>
          {PI_LOGIN_NOTICE_DE.retry}
        </button>
      </div>
    );
  }
  if (piAuth.phase === PI_AUTH_PHASE.EXCHANGING && !piAuth.expiresAt) {
    return (
      <div role="status" className="mx-4 mt-3 px-3 py-2 text-sm text-[var(--ink-2)] flex items-center gap-2">
        <Icon name="loading" size={16} className="animate-spin" />
        <span>{PI_LOGIN_NOTICE_DE.exchanging}</span>
      </div>
    );
  }
  return null;
}
