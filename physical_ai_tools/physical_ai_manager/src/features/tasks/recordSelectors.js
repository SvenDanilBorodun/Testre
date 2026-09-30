// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Selectors of the Aufnahme page (spec §3.2).
//
// `selectRecordStatus` and `selectRecordForm` hand back the SAME object for as
// long as nothing in it changed — arrays compared by content, because the
// /task/status adopt path rebuilds `taskInstruction` / `tags` on every running
// tick (V2-2) — so they are safe with or without `shallowEqual`, and the page
// re-renders when a field changes (about once a second while recording), not
// on every 30 Hz tick. That is why `receivedAt` / `receivedWallMs` are left
// out: they change on every tick. The phase clock reads the arrival time
// through the store directly (useSmoothPhaseClock), never through a render.

function sameValue(a, b) {
  if (Object.is(a, b)) return true;
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  return a.every((item, i) => Object.is(item, b[i]));
}

// Return `prev` when every field of `next` equals it (arrays by content).
function keepIfEqual(prev, next) {
  if (!prev) return next;
  const keys = Object.keys(next);
  if (keys.length !== Object.keys(prev).length) return next;
  return keys.every((k) => sameValue(prev[k], next[k])) ? prev : next;
}

// One cache per selector: the last result. A second store (tests) only costs a
// fresh object, never a wrong one — the check is by content.
function stable(build) {
  let last = null;
  return (state) => {
    last = keepIfEqual(last, build(state));
    return last;
  };
}

export const selectRecordStatus = stable((state) => {
  const st = state.tasks.taskStatus;
  return {
    phase: st.phase,
    running: st.running,
    taskType: st.taskType,
    totalTime: st.totalTime,
    proceedTime: st.proceedTime,
    currentEpisodeNumber: st.currentEpisodeNumber,
    numEpisodes: st.numEpisodes,
    episodeTime: st.episodeTime,
    warmupTime: st.warmupTime,
    resetTime: st.resetTime,
    fps: st.fps,
    pushToHub: st.pushToHub,
    topicReceived: st.topicReceived,
    capabilities: st.capabilities,
    robotProfile: st.robotProfile,
    robotType: st.robotType,
  };
});

export const selectPhaseAnchor = (state) => state.tasks.phaseAnchor;
export const selectRecordSession = (state) => state.tasks.recordSession;
export const selectRecordNotice = (state) => state.tasks.recordNotice;

/** The recording form (taskInfo) fields the Aufnahme page edits and sends. */
export const selectRecordForm = stable((state) => {
  const info = state.tasks.taskInfo;
  return {
    taskName: info.taskName,
    taskInstruction: info.taskInstruction,
    userId: info.userId,
    fps: info.fps,
    tags: info.tags,
    warmupTime: info.warmupTime,
    episodeTime: info.episodeTime,
    resetTime: info.resetTime,
    numEpisodes: info.numEpisodes,
    pushToHub: info.pushToHub,
    privateMode: info.privateMode,
  };
});
