/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

import { createTeachSounds } from '../teachSounds';

function fakeAudioContextClass() {
  const created = [];
  const oscillators = [];
  class FakeAudioContext {
    constructor() {
      this.state = 'running';
      this.currentTime = 0;
      this.destination = {};
      this.resume = vi.fn(() => Promise.resolve());
      this.close = vi.fn(() => Promise.resolve());
      created.push(this);
    }

    createOscillator() {
      const osc = {
        type: '', frequency: { value: 0 }, connect: vi.fn(), start: vi.fn(), stop: vi.fn(),
      };
      oscillators.push(osc);
      return osc;
    }

    createGain() {
      return {
        gain: { setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn() },
        connect: vi.fn(),
      };
    }
  }
  return { FakeAudioContext, created, oscillators };
}

describe('createTeachSounds', () => {
  let original;
  beforeEach(() => {
    original = window.AudioContext;
    window.localStorage.removeItem('edubotics_audio_muted');
  });
  afterEach(() => {
    window.AudioContext = original;
    window.localStorage.removeItem('edubotics_audio_muted');
  });

  it('creates no AudioContext while the teacher has muted audio', () => {
    const fake = fakeAudioContextClass();
    window.AudioContext = fake.FakeAudioContext;
    window.localStorage.setItem('edubotics_audio_muted', '1');
    const sounds = createTeachSounds();
    sounds.tick();
    sounds.start();
    sounds.stop();
    sounds.capture();
    expect(fake.created).toHaveLength(0);
  });

  it('starts an oscillator at the tick frequency, lazily, on one context', () => {
    const fake = fakeAudioContextClass();
    window.AudioContext = fake.FakeAudioContext;
    const sounds = createTeachSounds();
    expect(fake.created).toHaveLength(0);
    sounds.tick();
    expect(fake.created).toHaveLength(1);
    expect(fake.oscillators).toHaveLength(1);
    expect(fake.oscillators[0].frequency.value).toBe(880);
    expect(fake.oscillators[0].start).toHaveBeenCalled();
    sounds.capture();
    expect(fake.created).toHaveLength(1);
    expect(fake.oscillators.map((o) => o.frequency.value)).toEqual([880, 1046, 1568]);
  });

  it('reads the mute switch at call time', () => {
    const fake = fakeAudioContextClass();
    window.AudioContext = fake.FakeAudioContext;
    const sounds = createTeachSounds();
    window.localStorage.setItem('edubotics_audio_muted', '1');
    sounds.start();
    expect(fake.oscillators).toHaveLength(0);
    window.localStorage.setItem('edubotics_audio_muted', '0');
    sounds.start();
    expect(fake.oscillators.map((o) => o.frequency.value)).toEqual([1320]);
  });

  it('resumes a suspended context', () => {
    const fake = fakeAudioContextClass();
    window.AudioContext = fake.FakeAudioContext;
    const sounds = createTeachSounds();
    sounds.stop();
    fake.created[0].state = 'suspended';
    sounds.stop();
    expect(fake.created[0].resume).toHaveBeenCalledTimes(1);
  });

  it('never throws when Web Audio throws', () => {
    window.AudioContext = function Throwing() { throw new Error('no audio'); };
    const sounds = createTeachSounds();
    expect(() => {
      sounds.tick();
      sounds.start();
      sounds.stop();
      sounds.capture();
      sounds.dispose();
    }).not.toThrow();
  });
});
