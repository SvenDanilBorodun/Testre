// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The toolbar's save status belongs to WorkshopPage (the CLOUD save, judged by
// content), so the slice keeps no save time of its own any more (WI-1.11, m8).
// `markWorkflowSaved` stays: it still clears the store's copy of the last edit.

import reducer, { markWorkflowSaved, setUnsavedBlocklyJson } from '../workshopSlice';
import { signedOut } from '../../session/sessionActions';

const initial = reducer(undefined, { type: '@@INIT' });

describe('workshopSlice — no save time of its own', () => {
  test('the initial state carries no lastSavedAt', () => {
    expect(Object.prototype.hasOwnProperty.call(initial, 'lastSavedAt')).toBe(false);
  });

  test('markWorkflowSaved clears the unsaved copy and stamps nothing', () => {
    const edited = reducer(initial, setUnsavedBlocklyJson({ blocks: {} }));
    const saved = reducer(edited, markWorkflowSaved());
    expect(saved.unsavedBlocklyJson).toBeNull();
    expect(Object.prototype.hasOwnProperty.call(saved, 'lastSavedAt')).toBe(false);
  });

  test('sign-out still clears the unsaved copy', () => {
    const edited = reducer(initial, setUnsavedBlocklyJson({ blocks: {} }));
    const out = reducer(edited, signedOut());
    expect(out.unsavedBlocklyJson).toBeNull();
    expect(Object.prototype.hasOwnProperty.call(out, 'lastSavedAt')).toBe(false);
  });
});
