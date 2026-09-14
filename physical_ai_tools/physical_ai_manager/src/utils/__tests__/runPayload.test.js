/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// The client mirror of workflow_manager.MAX_WORKFLOW_JSON_BYTES. The server
// compares `len(workflow_json.encode('utf-8'))` with `>`, so exactly the cap is
// allowed and a multi-byte character counts as its UTF-8 length.
import { describe, it, expect } from 'vitest';
import {
  RUN_PAYLOAD_MAX_BYTES,
  runPayloadBytes,
  exceedsRunPayloadCap,
  RUN_PAYLOAD_TOO_BIG_RECORDINGS_DE,
} from '../runPayload';

describe('run payload cap', () => {
  it('is 256 KiB', () => {
    expect(RUN_PAYLOAD_MAX_BYTES).toBe(256 * 1024);
  });

  it('allows exactly the cap and refuses one byte more', () => {
    expect(exceedsRunPayloadCap('a'.repeat(256 * 1024))).toBe(false);
    expect(exceedsRunPayloadCap('a'.repeat(256 * 1024 + 1))).toBe(true);
  });

  it('counts UTF-8 bytes, so „ä" is two', () => {
    expect(runPayloadBytes('ä')).toBe(2);
    // 256*1024 - 1 ASCII bytes + one „ä" = cap + 1.
    expect(exceedsRunPayloadCap(`${'a'.repeat(256 * 1024 - 1)}ä`)).toBe(true);
    expect(exceedsRunPayloadCap(`${'a'.repeat(256 * 1024 - 2)}ä`)).toBe(false);
  });

  it('names the recordings in German', () => {
    expect(RUN_PAYLOAD_TOO_BIG_RECORDINGS_DE).toMatch(/aufgenommenen Bewegungen/);
    expect(RUN_PAYLOAD_TOO_BIG_RECORDINGS_DE).toMatch(/groß/);
  });
});
