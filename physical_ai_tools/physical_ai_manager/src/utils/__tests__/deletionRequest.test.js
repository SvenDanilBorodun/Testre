// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// GDPR Art. 17 deletion requests (launch plan step 5): the dates the queue and
// the student's notice show, and the ONE reading of a deletion's answer that the
// roster's delete and the queue's confirmation share.

import { describe, expect, it, vi } from 'vitest';
import {
  DELETION_DE,
  announceStudentDeletion,
  deadlineState,
  formatDateDe,
} from '../deletionRequest';

describe('formatDateDe', () => {
  it('shows the Berlin calendar date', () => {
    expect(formatDateDe('2026-12-03T10:00:00+00:00')).toBe('03.12.2026');
    // 23:30 UTC on 31 Dec is already 1 Jan in Berlin
    expect(formatDateDe('2026-12-31T23:30:00+00:00')).toBe('01.01.2027');
  });

  it('never invents a date', () => {
    for (const bad of [null, undefined, '', 'gestern', 17]) expect(formatDateDe(bad)).toBe('–');
  });
});

describe('deadlineState', () => {
  const now = Date.parse('2026-11-20T12:00:00Z');
  it('tells overdue, soon (≤ 7 days) and ok apart', () => {
    expect(deadlineState('2026-11-19T12:00:00Z', now)).toBe('overdue');
    expect(deadlineState('2026-11-27T12:00:00Z', now)).toBe('soon');
    expect(deadlineState('2026-11-28T12:00:01Z', now)).toBe('ok');
    expect(deadlineState(null, now)).toBe('unknown');
  });
});

describe('announceStudentDeletion', () => {
  const toastApi = () => ({ success: vi.fn(), error: vi.fn() });

  it('says „Schüler gelöscht" only when the erasure completed', () => {
    const t = toastApi();
    expect(announceStudentDeletion({ ok: true, hf_erasure_complete: true }, t)).toBe('complete');
    expect(t.success).toHaveBeenCalledWith('Schüler gelöscht');
    expect(t.error).not.toHaveBeenCalled();
  });

  it('shows the server sentence for a partial erasure, for 15 s', () => {
    const t = toastApi();
    const res = { ok: true, hf_erasure_complete: false, hf_failures: [{}], detail: 'Das Konto wurde gelöscht, aber …' };
    expect(announceStudentDeletion(res, t)).toBe('partial');
    expect(t.error).toHaveBeenCalledWith('Das Konto wurde gelöscht, aber …', { duration: 15000 });
    expect(t.success).not.toHaveBeenCalled();
  });

  it('names the remaining repos itself when the server sent no detail', () => {
    const t = toastApi();
    announceStudentDeletion({ ok: true, hf_erasure_complete: false, hf_failures: [{}, {}] }, t);
    expect(t.error.mock.calls[0][0]).toBe(DELETION_DE.hfRemaining(2));
    expect(t.error.mock.calls[0][0]).toContain('2 HuggingFace-');
  });

  it('keeps an older API’s bare {ok: true} a plain success', () => {
    const t = toastApi();
    announceStudentDeletion({ ok: true }, t);
    expect(t.success).toHaveBeenCalledWith('Schüler gelöscht');
  });
});

describe('the copy', () => {
  it('names the Art. 12(3) deadline and has no decline', () => {
    expect(DELETION_DE.deadlineNote).toContain('Art. 12 Abs. 3 DSGVO');
    expect(DELETION_DE.deadlineNote).toContain('einen Monat');
    expect(Object.keys(DELETION_DE).some((k) => /decline|reject|ablehn/i.test(k))).toBe(false);
    expect(DELETION_DE.openCount(1)).toBe('1 offene Löschanfrage');
    expect(DELETION_DE.openCount(3)).toBe('3 offene Löschanfragen');
    expect(DELETION_DE.studentNotice('03.11.2026', '03.12.2026')).toContain('spätestens bis zum 03.12.2026');
  });
});
