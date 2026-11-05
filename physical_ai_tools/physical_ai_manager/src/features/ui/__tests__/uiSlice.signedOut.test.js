// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// `ui.hfUserList` is the accounts and organisations the ROBOT's token can push
// to, i.e. the account of whichever student last filled the robot's token slot.
// It used to be exempt from `session/signedOut` on the ground that it
// „describes the machine"; since the slot became per-student (migration 042) it
// is the previous student's, and the next student must not inherit it.

import reducer, { moveToPage, setHfUserList } from '../uiSlice';
import { signedOut } from '../../session/sessionActions';
import PageType from '../../../constants/pageType';

describe('uiSlice answers session/signedOut', () => {
  it('empties the Benutzer-ID list', () => {
    let state = reducer(undefined, setHfUserList(['anna', 'schule-a']));
    expect(state.hfUserList).toEqual(['anna', 'schule-a']);
    state = reducer(state, signedOut());
    expect(state.hfUserList).toEqual([]);
  });

  it('leaves which tab is open alone: that is not student data', () => {
    let state = reducer(undefined, moveToPage(PageType.RECORD));
    state = reducer(state, setHfUserList(['anna']));
    state = reducer(state, signedOut());
    expect(state.currentPage).toBe(PageType.RECORD);
  });

  it('is idempotent on an empty list', () => {
    const before = reducer(undefined, { type: '@@INIT' });
    expect(reducer(before, signedOut()).hfUserList).toEqual([]);
  });
});
