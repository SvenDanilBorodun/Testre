// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Aufnahme countdown tick (spec §3.7): 700 Hz sine, 80 ms, gain 0.15 with
// a 10 ms release; muted by the existing „Ton" key, read on every call.

import { MUTE_KEY, TICK, createRecordSounds } from '../recordSounds';

class FakeParam {
  constructor() { this.events = []; this.value = 0; }
  setValueAtTime(v, t) { this.events.push(['set', v, t]); }
  linearRampToValueAtTime(v, t) { this.events.push(['ramp', v, t]); }
}

let contexts;
class FakeAudioContext {
  constructor() {
    this.state = 'suspended';
    this.currentTime = 10;
    this.destination = {};
    this.oscillators = [];
    this.gains = [];
    this.closed = false;
    contexts.push(this);
  }
  resume() { this.state = 'running'; return Promise.resolve(); }
  close() { this.closed = true; return Promise.resolve(); }
  createOscillator() {
    const o = {
      type: '', frequency: new FakeParam(), connect: vi.fn(), start: vi.fn(), stop: vi.fn(),
    };
    this.oscillators.push(o);
    return o;
  }
  createGain() {
    const g = { gain: new FakeParam(), connect: vi.fn() };
    this.gains.push(g);
    return g;
  }
}

beforeEach(() => {
  contexts = [];
  window.AudioContext = FakeAudioContext;
  localStorage.removeItem(MUTE_KEY);
});

afterEach(() => {
  delete window.AudioContext;
  localStorage.removeItem(MUTE_KEY);
});

describe('createRecordSounds', () => {
  it('uses the existing mute key', () => {
    expect(MUTE_KEY).toBe('edubotics_audio_muted');
    expect(TICK).toEqual({ frequencyHz: 700, durationMs: 80, gain: 0.15, releaseMs: 10 });
  });

  it('prime() opens and resumes one context; tick() plays one 700 Hz sine', () => {
    const sounds = createRecordSounds();
    sounds.prime();
    expect(contexts).toHaveLength(1);
    expect(contexts[0].state).toBe('running');
    sounds.tick();
    const ctx = contexts[0];
    expect(ctx.oscillators).toHaveLength(1);
    const osc = ctx.oscillators[0];
    expect(osc.type).toBe('sine');
    expect(osc.frequency.events).toEqual([['set', 700, 10]]);
    expect(osc.start).toHaveBeenCalledWith(10);
    expect(osc.stop).toHaveBeenCalledWith(10 + 0.08);
    expect(ctx.gains[0].gain.events).toEqual([
      ['set', 0.15, 10],
      ['set', 0.15, 10 + 0.07],
      ['ramp', 0.0001, 10 + 0.08],
    ]);
    sounds.tick();
    expect(contexts).toHaveLength(1);
    expect(ctx.oscillators).toHaveLength(2);
  });

  it('muted: silent, read on every call', () => {
    const sounds = createRecordSounds();
    sounds.prime();
    localStorage.setItem(MUTE_KEY, '1');
    sounds.tick();
    expect(contexts[0].oscillators).toHaveLength(0);
    localStorage.setItem(MUTE_KEY, '0');
    sounds.tick();
    expect(contexts[0].oscillators).toHaveLength(1);
  });

  it('never throws without Web Audio or storage', () => {
    delete window.AudioContext;
    const sounds = createRecordSounds();
    expect(() => { sounds.prime(); sounds.tick(); sounds.dispose(); }).not.toThrow();
    window.AudioContext = FakeAudioContext;
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    const s2 = createRecordSounds();
    expect(() => s2.tick()).not.toThrow();
    spy.mockRestore();
  });

  it('dispose() closes the context', () => {
    const sounds = createRecordSounds();
    sounds.prime();
    sounds.dispose();
    expect(contexts[0].closed).toBe(true);
    sounds.tick(); // a new context on demand after dispose
    expect(contexts).toHaveLength(2);
  });
});
