/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The drag-and-drop type of a Sammlung row dropped into the code editor: the
// drawer sets it, CodeEditor reads it; the payload is `{kind, name}` JSON.
// Its own module so the drawer (entry bundle) does not pull the insertion
// code (codeInsert.js, loaded with the lazy editor or on the first insert —
// review round 2, ni4).
export const SNIPPET_MIME = 'application/x-edubotics-snippet';
