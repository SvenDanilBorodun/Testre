// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Redux form (`tasks.taskInfo`) -> the TaskInfo wire object of a cloud start
// (spec §6.5, D28, R-5). Pure.
//
// Exactly useRosServiceCaller.sendRecordCommand's mapping, then the overrides.
// The robot echoes this object on /task/status while the run lasts and
// useRosTopicSubscription adopts it into `tasks.taskInfo`: built from the form
// itself, the echo changes nothing but the overridden fields (policyPath, fps,
// recordInferenceMode). The status echo of the PREVIOUS task would otherwise
// overwrite the student's Aufnahme form.

/**
 * @param {object} form       state.tasks.taskInfo
 * @param {object} overrides  wire keys, e.g. {task_type, policy_path, fps, record_inference_mode}
 * @returns {object} the TaskInfo wire object
 */
export function taskInfoWire(form, overrides = {}) {
  const info = form || {};
  const instructions = Array.isArray(info.taskInstruction) ? info.taskInstruction : [];
  const base = {
    task_name: String(info.taskName || ''),
    task_type: String(info.taskType || ''),
    user_id: String(info.userId || ''),
    task_instruction: instructions.filter((instruction) => instruction.trim() !== ''),
    policy_path: String(info.policyPath || ''),
    record_inference_mode: Boolean(info.recordInferenceMode),
    fps: Number(info.fps) || 0,
    tags: info.tags || [],
    warmup_time_s: Number(info.warmupTime) || 0,
    episode_time_s: Number(info.episodeTime) || 0,
    reset_time_s: Number(info.resetTime) || 0,
    num_episodes: Number(info.numEpisodes) || 0,
    push_to_hub: Boolean(info.pushToHub),
    private_mode: Boolean(info.privateMode),
    use_optimized_save_mode: Boolean(info.useOptimizedSave),
    record_rosbag2: Boolean(info.recordRosBag2),
  };
  return { ...base, ...overrides };
}

export default taskInfoWire;
