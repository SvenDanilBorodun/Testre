// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Selectors of the Aufnahme page (spec §3.2).
//
// `selectRecordStatus` and `selectRecordForm` build a NEW object per call and
// must be read with `shallowEqual` (react-redux): every field is a primitive or
// an identity-stable reference from the store, so the page re-renders when a
// field changes — about once a second while recording — and not on every
// 30 Hz tick. That is why `receivedAt` / `receivedWallMs` are left out: they
// change on every tick. The phase clock reads the arrival time through the
// store directly (useSmoothPhaseClock), never through a render.

export const selectRecordStatus = (state) => {
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
};

export const selectPhaseAnchor = (state) => state.tasks.phaseAnchor;
export const selectRecordSession = (state) => state.tasks.recordSession;
export const selectRecordNotice = (state) => state.tasks.recordNotice;

/** The recording form (taskInfo) fields the Aufnahme page edits and sends. */
export const selectRecordForm = (state) => {
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
};
