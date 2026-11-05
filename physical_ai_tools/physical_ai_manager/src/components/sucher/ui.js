// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The „Sucher" scope switch. Imported FIRST by App.js (a side effect): the
// student build marks <html data-ui="sucher">, and every Sucher rule that could
// also match DOM the teacher web build renders is scoped under
// `:root[data-ui='sucher']`. The web build never sets it, so it renders exactly
// as before. (App.js, not index.js: the harness loads index.css + App and never
// index.js.)

import { APP_MODE } from '../../constants/appMode';

if (APP_MODE !== 'web' && typeof document !== 'undefined') document.documentElement.dataset.ui = 'sucher';
