/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Vormachen audio cues: countdown tick, recording start/stop, capture. The
// student's eyes are on the arm and both hands are on it, so a sound is the
// only feedback that reaches them. A sound must NEVER break a capture: every
// failure (no Web Audio, a blocked context, a throwing node) is swallowed.
//
// Muting reuses the teacher's existing switch (`edubotics_audio_muted`,
// ControlPanel), read at CALL time so a toggle applies at once. No new key.

const MUTE_KEY = 'edubotics_audio_muted';
const GAIN = 0.15;
const RELEASE_S = 0.01;

function isMuted() {
  try {
    return window.localStorage.getItem(MUTE_KEY) === '1';
  } catch (_) {
    return false;
  }
}

export function createTeachSounds() {
  let ctx = null;

  const context = () => {
    if (ctx) return ctx;
    const Ctor = typeof window !== 'undefined'
      ? (window.AudioContext || window.webkitAudioContext)
      : undefined;
    if (!Ctor) return null;
    ctx = new Ctor();
    return ctx;
  };

  // One oscillator tone of `ms`, starting `delayMs` from now.
  const tone = (ac, frequency, ms, delayMs = 0) => {
    const osc = ac.createOscillator();
    const gain = ac.createGain();
    const t0 = ac.currentTime + delayMs / 1000;
    const t1 = t0 + ms / 1000;
    osc.type = 'sine';
    osc.frequency.value = frequency;
    gain.gain.setValueAtTime(GAIN, t0);
    gain.gain.setValueAtTime(GAIN, Math.max(t0, t1 - RELEASE_S));
    gain.gain.linearRampToValueAtTime(0, t1);
    osc.connect(gain);
    gain.connect(ac.destination);
    osc.start(t0);
    osc.stop(t1);
  };

  const play = (tones) => {
    if (isMuted()) return;
    try {
      const ac = context();
      if (!ac) return;
      if (ac.state === 'suspended' && typeof ac.resume === 'function') {
        Promise.resolve(ac.resume()).catch(() => {});
      }
      tones.forEach(([frequency, ms, delayMs]) => tone(ac, frequency, ms, delayMs));
    } catch (_) {
      // A sound never breaks a capture.
    }
  };

  return {
    tick: () => play([[880, 60, 0]]),
    start: () => play([[1320, 150, 0]]),
    stop: () => play([[440, 200, 0]]),
    capture: () => play([[1046, 70, 0], [1568, 70, 80]]),
    dispose: () => {
      const ac = ctx;
      ctx = null;
      if (!ac) return;
      try {
        Promise.resolve(ac.close && ac.close()).catch(() => {});
      } catch (_) {
        // ignore
      }
    },
  };
}

export default createTeachSounds;
