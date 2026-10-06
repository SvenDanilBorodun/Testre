// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The locked progress dialog of a running job (spec §D4): driven by the job's
// row on /edubotics/daten_state — the step its stage stands for; the steps
// before it done, the one after it pending (an OUTLINE circle, `stepPending`).
// It cannot be closed: the job ends it.

import React from 'react';
import Dialog from './Dialog';
import Icon from '../../icons/Icon';

export default function ProgressDialog({ title, sub = '', steps = [], step = 0 }) {
  return (
    <Dialog title={title} icon="loading" iconTone="accent" iconSpin locked onClose={() => {}} testId="dat-progress">
      {sub ? <p className="dat-small">{sub}</p> : null}
      {steps.length ? (
        <ul className="dat-steps">
          {steps.map((s, k) => {
            const state = k < step ? 'done' : (k === step ? 'now' : '');
            return (
              <li key={s} className={state ? `dat-${state}` : undefined} data-step-state={state || 'pending'}>
                {state === 'done' ? <Icon name="checkCircle" size={16} /> : null}
                {state === 'now' ? <Icon name="loading" size={16} className="animate-spin" /> : null}
                {state === '' ? <Icon name="stepPending" size={16} /> : null}
                {s}
              </li>
            );
          })}
        </ul>
      ) : null}
    </Dialog>
  );
}
