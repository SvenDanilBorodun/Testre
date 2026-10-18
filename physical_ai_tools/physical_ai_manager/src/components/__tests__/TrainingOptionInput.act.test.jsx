// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// For ACT the server decides three of the six „Weitere Optionen" (RunPod spec
// §4.11, owner decisions D4/E3): the batch size is fixed at 8, `num_workers`
// follows the GPU host's cores and LeRobot's `save_freq` is fixed for resume.
// A field the server ignores must not be offered, so for ACT the rows
// „Worker", „Batch-Größe" and „Speicher-Intervall" are not rendered. Every
// other policy (and a form with no policy chosen yet) keeps all six.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import trainingReducer, { selectPolicyType } from '../../features/training/trainingSlice';
import TrainingOptionInput from '../TrainingOptionInput';

const ALL_ROWS = ['Seed', 'Worker', 'Batch-Größe', 'Schritte', 'Log-Intervall', 'Speicher-Intervall'];
const HIDDEN_FOR_ACT = ['Worker', 'Batch-Größe', 'Speicher-Intervall'];

function renderWithPolicy(policyType) {
  const store = configureStore({ reducer: { training: trainingReducer } });
  if (policyType !== undefined) store.dispatch(selectPolicyType(policyType));
  render(
    <Provider store={store}>
      <TrainingOptionInput />
    </Provider>,
  );
  return store;
}

describe('TrainingOptionInput for ACT', () => {
  // The API's rule is `model_type.lower() == "act"`; the form uses the same one.
  test.each(['act', 'ACT'])('hides Worker, Batch-Größe and Speicher-Intervall (%s)', (policyType) => {
    renderWithPolicy(policyType);
    for (const label of HIDDEN_FOR_ACT) {
      expect(screen.queryByText(label)).toBeNull();
      expect(screen.queryByPlaceholderText(`${label} eingeben`)).toBeNull();
    }
    for (const label of ['Seed', 'Schritte', 'Log-Intervall']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getAllByRole('spinbutton')).toHaveLength(3);
    expect(screen.getByRole('button', { name: 'Standardwerte' })).toBeInTheDocument();
  });

  test.each(['diffusion', 'vqbet', 'tdmpc', 'pi0', 'smolvla', undefined])(
    'keeps all six rows for %s',
    (policyType) => {
      renderWithPolicy(policyType);
      for (const label of ALL_ROWS) {
        expect(screen.getByText(label)).toBeInTheDocument();
      }
      expect(screen.getAllByRole('spinbutton')).toHaveLength(6);
    },
  );
});
