// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import React from 'react';
import { render, screen } from '@testing-library/react';
import CloudRunFacts, { formatMmSs } from '../CloudRunFacts';
import { cloudCopy } from '../cloudCopy';
import TaskPhase from '../../../constants/taskPhases';

const state = (o = {}) => ({
  v: 1, seq: 3, active: true, phase: 'running', rtt_ms_p50: 101.6, plan_age_ms: 80.2, gpu_ms_p50: 23.4,
  hold_s: null, ends_in_s: 1499.8, stale_camera: null, end_text_de: null, ...o,
});

describe('CloudRunFacts — facts only', () => {
  it('no state (an older image, a stale topic) shows no facts', () => {
    const { container } = render(<CloudRunFacts state={null} robotPhase={TaskPhase.READY} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the running line from /task/status and the facts the topic carries', () => {
    render(<CloudRunFacts state={state()} robotPhase={TaskPhase.INFERENCING} />);
    expect(screen.getByText(cloudCopy.status.running)).toBeInTheDocument();
    expect(screen.getByText('Antwortzeit 102 ms')).toBeInTheDocument();
    expect(screen.getByText('Plan-Alter 80 ms')).toBeInTheDocument();
    expect(screen.getByText('GPU 23 ms')).toBeInTheDocument();
    expect(screen.getByText('Endet in 24:59')).toBeInTheDocument();
  });

  it('a fact the topic does not carry renders nothing — unknown is never zero', () => {
    render(<CloudRunFacts state={state({ rtt_ms_p50: null, gpu_ms_p50: null, ends_in_s: null })} robotPhase={TaskPhase.INFERENCING} />);
    expect(screen.queryByText(/Antwortzeit/)).toBeNull();
    expect(screen.queryByText(/GPU/)).toBeNull();
    expect(screen.queryByText(/Endet in/)).toBeNull();
    expect(screen.getByText('Plan-Alter 80 ms')).toBeInTheDocument();
  });

  it('the hold notice while holding, the stale camera, the end sentence', () => {
    const { rerender } = render(<CloudRunFacts state={state({ phase: 'holding', hold_s: 1.6 })} robotPhase={TaskPhase.INFERENCING} />);
    expect(screen.getByText('Arm hält an – warte auf die Cloud (2 s)')).toBeInTheDocument();
    rerender(<CloudRunFacts state={state({ stale_camera: 'scene' })} robotPhase={TaskPhase.INFERENCING} />);
    expect(screen.getByText('Kamera „scene“ liefert seit über 5 Sekunden dasselbe Bild.')).toBeInTheDocument();
    rerender(<CloudRunFacts state={state({ active: false, phase: 'ended', end_text_de: 'Die Verbindung zur Cloud ist abgebrochen.' })} robotPhase={TaskPhase.READY} />);
    expect(screen.getByText('Die Verbindung zur Cloud ist abgebrochen.')).toBeInTheDocument();
    expect(screen.queryByText(/Antwortzeit/)).toBeNull();
  });

  it('formats m:ss', () => {
    expect(formatMmSs(0)).toBe('0:00');
    expect(formatMmSs(61.9)).toBe('1:01');
    expect(formatMmSs(-1)).toBeNull();
    expect(formatMmSs(null)).toBeNull();
  });
});
