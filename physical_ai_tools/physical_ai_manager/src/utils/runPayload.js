/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * The /workflow/start size cap, shared by every sender of a run payload
 * (`RunControls` today, simulator previews next).
 *
 * The SERVER stays the authority: it refuses a payload over
 * `workflow_manager.MAX_WORKFLOW_JSON_BYTES` with „Workflow-JSON ist zu groß",
 * which names no cause and no remedy. This mirror exists ONLY so a client can
 * name the likely cause first — when recordings ride along they almost always
 * are it. Bytes are counted in UTF-8, as the server's `len(encode())` does, so
 * an „ä" counts twice.
 */

/** Mirror of workflow_manager.MAX_WORKFLOW_JSON_BYTES (256 KiB). */
export const RUN_PAYLOAD_MAX_BYTES = 256 * 1024;

export function runPayloadBytes(text) {
  return new TextEncoder().encode(String(text)).length;
}

export function exceedsRunPayloadCap(text) {
  return runPayloadBytes(text) > RUN_PAYLOAD_MAX_BYTES;
}

export const RUN_PAYLOAD_TOO_BIG_RECORDINGS_DE =
  'Die aufgenommenen Bewegungen in diesem Programm sind zusammen zu groß '
  + 'für einen Start. Bitte kürzere Bewegungen aufnehmen oder weniger '
  + 'verschiedene Bewegungen im selben Programm abspielen.';
