// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Aufnahme countdown tick (spec §3.7): a short 700 Hz sine on each of the
// last three seconds of a warm-up or reset, so a student with both hands on
// the leader arm hears the recording coming. The other cues (1000 Hz „Los!",
// the 660→440 Hz fall, the triple 880 Hz) stay in useRosTopicSubscription.
//
// Browsers only let a page make sound after a user gesture: `prime()` is
// called inside the „Aufnahme starten" click. Muted by the page's existing
// „Ton" key, read on every call (a switch applies at once), and never throws —
// a missing Web Audio or blocked storage just means no tick.

export const MUTE_KEY = 'edubotics_audio_muted';
export const TICK = Object.freeze({ frequencyHz: 700, durationMs: 80, gain: 0.15, releaseMs: 10 });

/** The „Ton" switch, read fresh (the Aufnahme page and the hook's beeps share it). */
export function isMuted() {
  try {
    return window.localStorage.getItem(MUTE_KEY) === '1';
  } catch {
    return false;
  }
}

/** Flip the „Ton" switch; storage that refuses (private mode) is not an error. */
export function setMuted(muted) {
  try {
    window.localStorage.setItem(MUTE_KEY, muted ? '1' : '0');
  } catch { /* private mode / quota */ }
}

export function createRecordSounds() {
  let ctx = null;

  const context = () => {
    if (ctx) return ctx;
    const Ctor = typeof window !== 'undefined' ? (window.AudioContext || window.webkitAudioContext) : null;
    if (!Ctor) return null;
    try {
      ctx = new Ctor();
    } catch {
      ctx = null;
    }
    return ctx;
  };

  const resume = (c) => {
    try {
      if (c && c.state === 'suspended' && typeof c.resume === 'function') {
        const p = c.resume();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      }
    } catch { /* ignore */ }
  };

  return {
    prime() {
      resume(context());
    },
    tick() {
      if (isMuted()) return;
      const c = context();
      if (!c) return;
      resume(c);
      try {
        const t = c.currentTime;
        const end = t + TICK.durationMs / 1000;
        const osc = c.createOscillator();
        const gain = c.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(TICK.frequencyHz, t);
        gain.gain.setValueAtTime(TICK.gain, t);
        gain.gain.setValueAtTime(TICK.gain, end - TICK.releaseMs / 1000);
        gain.gain.linearRampToValueAtTime(0.0001, end);
        osc.connect(gain);
        gain.connect(c.destination);
        osc.start(t);
        osc.stop(end);
      } catch { /* no sound is not an error */ }
    },
    dispose() {
      const c = ctx;
      ctx = null;
      try {
        if (c && typeof c.close === 'function') {
          const p = c.close();
          if (p && typeof p.catch === 'function') p.catch(() => {});
        }
      } catch { /* ignore */ }
    },
  };
}
