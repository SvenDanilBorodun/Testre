// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// „Auf Hugging Face ansehen" (spec §G14, P33): a public hub copy opens the
// LeRobot viewer with the encoded `/<ns>/<name>/episode_0` path; a private one,
// or one whose visibility is unknown, the dataset page; never a token or any
// other query parameter.

import { describe, expect, it } from 'vitest';
import { hubDatasetUrl } from '../model/hubLinks';

describe('hubDatasetUrl', () => {
  it('public → the viewer URL with the encoded path, nothing else', () => {
    const url = hubDatasetUrl('lena-schmidt/omx_f_wuerfel', false);
    expect(url).toBe('https://huggingface.co/spaces/lerobot/visualize_dataset?path=%2Flena-schmidt%2Fomx_f_wuerfel%2Fepisode_0');
    const parsed = new URL(url);
    expect([...parsed.searchParams.keys()]).toEqual(['path']);
    expect(parsed.searchParams.get('path')).toBe('/lena-schmidt/omx_f_wuerfel/episode_0');
  });

  it('private → the dataset page', () => {
    expect(hubDatasetUrl('lena-schmidt/omx_f_stift', true)).toBe('https://huggingface.co/datasets/lena-schmidt/omx_f_stift');
  });

  it('unknown visibility → the dataset page', () => {
    expect(hubDatasetUrl('lena-schmidt/omx_f_stift', null)).toBe('https://huggingface.co/datasets/lena-schmidt/omx_f_stift');
    expect(hubDatasetUrl('lena-schmidt/omx_f_stift', undefined)).toBe('https://huggingface.co/datasets/lena-schmidt/omx_f_stift');
  });

  it('never a token or a query parameter on the dataset page, nothing for a bad id', () => {
    const url = hubDatasetUrl('lena-schmidt/omx_f_stift', true);
    expect(new URL(url).search).toBe('');
    expect(url).not.toMatch(/token|hf_/i);
    expect(hubDatasetUrl('', false)).toBeNull();
    expect(hubDatasetUrl('a/b/c', false)).toBeNull();
    expect(hubDatasetUrl('../x', false)).toBeNull();
  });
});
