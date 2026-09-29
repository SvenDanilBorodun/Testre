// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The one <Toaster>'s options (App.js; the student app and the teacher web
// build share it). White text on a dark toast, a green success and a red error
// toast. `success.icon` / `error.icon` replace react-hot-toast's own animated
// check and cross with the app's Lucide icons (components/icons/toast.js), so
// a bare `toast.success(…)` / `toast.error(…)` looks like every toast that
// names its icon.

import { toastIcon } from './components/icons/toast';

const TEXT = Object.freeze({
  maxWidth: '500px',
  wordWrap: 'break-word',
  whiteSpace: 'pre-wrap',
  lineHeight: '1.4',
});

export const TOASTER_OPTIONS = Object.freeze({
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
