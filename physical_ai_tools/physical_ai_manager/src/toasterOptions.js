// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The one <Toaster>'s options (App.js; the student app and the teacher web
// build share it). `success.icon` / `error.icon` replace react-hot-toast's own
// animated check and cross with the app's Lucide icons
// (components/icons/toast.js), so a bare `toast.success(…)` / `toast.error(…)`
// looks like every toast that names its icon.
//
// Chosen by the build (spec §6.3, D9):
//   - web: white text on a dark toast, a green success and a red error toast —
//     exactly as before.
//   - student („Sucher"): a white paper slip with ink text out of a dark slot
//     (`.su-toast::before`, sucher.css), the success / error slip marked by a
//     green / red left edge. react-hot-toast 2.5.2 merges a toast's style
//     shallowly as {...style, ...[type].style, ...toast.style}; the per-type
//     objects are nevertheless complete, so neither level depends on the other.
// react-hot-toast's own enter/exit animation is kept in both.

import { APP_MODE } from './constants/appMode';
import { toastIcon } from './components/icons/toast';

const TEXT = Object.freeze({
  maxWidth: '500px',
  wordWrap: 'break-word',
  whiteSpace: 'pre-wrap',
  lineHeight: '1.4',
});

const WEB_TOASTER_OPTIONS = Object.freeze({
  duration: 3000,
  style: { background: '#363636', color: '#fff', ...TEXT },
  success: {
    duration: 3000,
    icon: toastIcon('success'),
    style: { background: '#10b981', ...TEXT },
  },
  error: {
    duration: 6000,
    icon: toastIcon('error'),
    style: { background: '#ef4444', ...TEXT },
  },
});

const PAPER = Object.freeze({
  background: '#FFFFFF',
  color: '#232322',
  borderRadius: '0 0 12px 12px',
  boxShadow: '0 0 0 1px #D2CEC5, 0 10px 20px -12px rgba(0,0,0,.4)',
  ...TEXT,
});

const STUDENT_TOASTER_OPTIONS = Object.freeze({
  duration: 3000,
  className: 'su-toast',
  style: { ...PAPER },
  success: {
    duration: 3000,
    icon: toastIcon('success'),
    style: { ...PAPER, borderLeft: '4px solid #1B6C32' },
  },
  error: {
    duration: 6000,
    icon: toastIcon('error'),
    style: { ...PAPER, borderLeft: '4px solid #B42318' },
  },
});

export const TOASTER_OPTIONS = APP_MODE === 'web' ? WEB_TOASTER_OPTIONS : STUDENT_TOASTER_OPTIONS;
