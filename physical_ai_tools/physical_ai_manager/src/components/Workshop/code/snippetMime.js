/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The drag-and-drop type of a Sammlung row dropped into the code editor: the
// drawer sets it, CodeEditor reads it; the payload is `{kind, name}` JSON.
// Its own module so the drawer (entry bundle) does not pull the insertion
// code (codeInsert.js, loaded with the lazy editor or on the first insert —
// review round 2, ni4).
export const SNIPPET_MIME = 'application/x-edubotics-snippet';
