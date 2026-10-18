// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// „Standardwerte" sets the SAME numbers a fresh form starts with (RunPod spec
// §4.11). It used to set numWorkers 4, steps 100000, evalFreq 20000 and
// saveFreq 20000 — values no other place in the product uses, so the button
// changed the training a student had set up instead of resetting it.

import reducer, {
  setBatchSize,
  setDefaultTrainingInfo,
  setNumWorkers,
  setSaveFreq,
  setSeed,
  setSteps,
  selectPolicyType,
  setDatasetRepoId,
  setOutputFolderName,
} from '../trainingSlice';

const DEFAULTS = {
  seed: 1000,
  numWorkers: 2,
  batchSize: 8,
  steps: 50000,
  evalFreq: 0,
  logFreq: 100,
  saveFreq: 10000,
};

function edited() {
  let state = reducer(undefined, { type: '@@init' });
  for (const action of [
    setSeed(7), setNumWorkers(9), setBatchSize(64), setSteps(123), setSaveFreq(5),
    selectPolicyType('act'), setDatasetRepoId('alice/omx_f_pick'), setOutputFolderName('mein-modell'),
  ]) {
    state = reducer(state, action);
  }
  return state;
}

describe('setDefaultTrainingInfo', () => {
  test('resets the seven numbers to the fresh form defaults', () => {
    const next = reducer(edited(), setDefaultTrainingInfo());
    for (const [key, value] of Object.entries(DEFAULTS)) {
      expect(next.trainingInfo[key]).toBe(value);
    }
  });

  test('equals what a fresh store starts with', () => {
    const fresh = reducer(undefined, { type: '@@init' }).trainingInfo;
    const reset = reducer(edited(), setDefaultTrainingInfo()).trainingInfo;
    for (const key of Object.keys(DEFAULTS)) {
      expect(reset[key]).toBe(fresh[key]);
    }
  });

  test('keeps the dataset, the policy and the output name', () => {
    const next = reducer(edited(), setDefaultTrainingInfo()).trainingInfo;
    expect(next.policyType).toBe('act');
    expect(next.datasetRepoId).toBe('alice/omx_f_pick');
    expect(next.outputFolderName).toBe('mein-modell');
  });
});
