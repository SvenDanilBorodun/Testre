// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// „Impressum · Datenschutz · Nutzungsbedingungen" (launch plan step 4,
// 2026-11-11). Each link appears as soon as the cloud API names its address
// (features/legal/legalLinks.js) and renders NOTHING otherwise — no empty bar,
// no placeholder, no error. Every link opens in a new tab without an opener or
// a referrer.
//
// Shown on: LoginForm (student, teacher web, the Pi claim screen's login), the
// teacher and admin dashboards (variant="footer"), the admin's two-factor
// screen, the Pi claim screen and the Pi System page.

import React from 'react';
import { useLegalLinks } from '../features/legal/legalLinks';

export const LEGAL_NAV_LABEL = 'Rechtliche Hinweise';

export function LegalLinkList({ links, className = '' }) {
  if (!links || links.length === 0) return null;
  return (
    <nav aria-label={LEGAL_NAV_LABEL} className={className}>
      <ul className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {links.map((link) => (
          <li key={link.key}>
            <a
              href={link.href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-[var(--ink-3)] underline-offset-2 hover:underline hover:text-[var(--ink)] focus-visible:underline"
            >
              {link.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/**
 * @param {object} props
 * @param {'inline'|'footer'} [props.variant] — `footer`: a bar of its own at the
 *   bottom of a full-height page column (the dashboards).
 * @param {string} [props.className]
 */
export default function LegalLinks({ variant = 'inline', className = '' }) {
  const links = useLegalLinks();
  if (links.length === 0) return null;
  if (variant === 'footer') {
    return (
      <footer
        className={`shrink-0 border-t border-[var(--line)] bg-white px-4 py-2 flex justify-end ${className}`}
        data-testid="legal-footer"
      >
        <LegalLinkList links={links} />
      </footer>
    );
  }
  return <LegalLinkList links={links} className={className} />;
}
