// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Aufnahme header's robot pill carries the REAL robot's glyph (the URDF's
// render, by profile id) and none while the profile is still unknown.

/* eslint-disable testing-library/no-node-access */

import React from 'react';
import { render, screen } from '@testing-library/react';
import RecordHeader from '../RecordHeader';

describe('RecordHeader — the robot pill', () => {
  it.each([
    ['omx_full', 'omx_full'], ['omx_follower', 'omx_f'], ['edu6_studio', 'edu6'], ['edu1_studio', 'edu1'],
  ])('draws the glyph of %s (%s) beside the robot\'s German name', (profile, glyph) => {
    render(<RecordHeader eyebrow="Aufnahme" title="Würfel" robotName="OMX – Voll" robotProfile={profile} />);
    const pill = screen.getByTestId('rec-robot-pill');
    expect(pill).toHaveTextContent('OMX – Voll');
    expect(pill.querySelector(`[data-robot-glyph="${glyph}"]`)).not.toBeNull();
    expect(pill.querySelector('svg')).toBeNull(); // nothing hand-drawn
  });

  it.each([null, undefined, ''])('with the profile %j the pill keeps its words and draws no robot', (profile) => {
    render(<RecordHeader eyebrow="Aufnahme" title="Würfel" robotName="OMX – Voll" robotProfile={profile} />);
    const pill = screen.getByTestId('rec-robot-pill');
    expect(pill).toHaveTextContent('OMX – Voll');
    expect(pill.querySelector('[data-robot-glyph]')).toBeNull();
  });

  it('no robot name, no pill', () => {
    render(<RecordHeader eyebrow="Aufnahme" title="Würfel" robotProfile="omx_full" />);
    expect(screen.queryByTestId('rec-robot-pill')).toBeNull();
  });
});
