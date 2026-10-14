// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The „Sucher" scope switch. Imported FIRST by App.js (a side effect): the
// student build marks <html data-ui="sucher">, and every Sucher rule that could
// also match DOM the teacher web build renders is scoped under
// `:root[data-ui='sucher']`. The web build never sets it, so it renders exactly
// as before. (App.js, not index.js: the harness loads index.css + App and never
// index.js.)

import { APP_MODE } from '../../constants/appMode';

if (APP_MODE !== 'web' && typeof document !== 'undefined') document.documentElement.dataset.ui = 'sucher';
