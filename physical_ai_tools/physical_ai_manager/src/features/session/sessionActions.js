// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// ONE broadcast action — `session/signedOut` — that every slice holding
// student-scoped state answers for ITSELF, in its own `extraReducers`.
//
// It replaces an enumeration inside `utils/signOut.js`, which imported four
// slices and dispatched their individual reset actions. That shape puts "which
// slices hold a student's data" in the one file that owns none of it, so a
// slice added later is invisible to it — and one already was: `workshop` kept
// `unsavedBlocklyJson`, `selectedWorkflowId` and `restrictedBlocks` through two
// rounds of review. A slice that answers for itself is covered on the day it is
// written, and `utils/__tests__/signOut.test.js` checks the property against
// the REAL root reducer instead of against a list.
//
// This is a leaf module because the dependency has to run both ways: the slices
// import the action, `utils/signOut.js` dispatches it. Declaring it in either
// would be a cycle.

import { createAction } from '@reduxjs/toolkit';

export const signedOut = createAction('session/signedOut');

export default signedOut;
