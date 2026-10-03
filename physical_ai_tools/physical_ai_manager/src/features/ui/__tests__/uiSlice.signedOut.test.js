// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
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
