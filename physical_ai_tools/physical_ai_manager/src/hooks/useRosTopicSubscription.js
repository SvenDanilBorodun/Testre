// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// Author: Kiwoong Park

import { useRef, useEffect, useState, useCallback } from 'react';
import toast from 'react-hot-toast';
import { useDispatch, useSelector } from 'react-redux';
import ROSLIB from 'roslib';
import TaskPhase from '../constants/taskPhases';
import {
  setTaskStatus,
  setTaskInfo,
  setHeartbeatStatus,
  setLastHeartbeatTime,
  setUseMultiTaskMode,
  setMultiTaskIndex,
} from '../features/tasks/taskSlice';
import {
  setIsTraining,
  setTopicReceived,
  setTrainingInfo,
  setCurrentStep,
  setLastUpdate,
  setSelectedUser,
  setSelectedDataset,
  setCurrentLoss,
} from '../features/training/trainingSlice';
import {
  setHFStatus,
  setDownloadStatus,
  setHFUserId,
  setHFRepoIdUpload,
  setHFRepoIdDownload,
  setUploadStatus,
} from '../features/editDataset/editDatasetSlice';
import {
  setRunState,
  setWorkflowStatus,
  setDetections,
  setSensorSnapshot,
  setPaused,
  setVariable,
} from '../features/workshop/workshopSlice';
import HFStatus from '../constants/HFStatus';
import store from '../store/store';
import rosConnectionManager from '../utils/rosConnectionManager';
import { registerDataset } from '../services/datasetsApi';

export function useRosTopicSubscription() {
  const taskStatusTopicRef = useRef(null);
  const heartbeatTopicRef = useRef(null);
  const trainingStatusTopicRef = useRef(null);
  const workflowStatusTopicRef = useRef(null);
  const workflowSensorsTopicRef = useRef(null);
  const previousPhaseRef = useRef(null);
  const audioContextRef = useRef(null);
  const hfStatusTopicRef = useRef(null);
  const lastTrainingUpdateRef = useRef(0);

  const dispatch = useDispatch();
  const rosbridgeUrl = useSelector((state) => state.ros.rosbridgeUrl);
  const [connected, setConnected] = useState(false);

  const initializeAudioContext = useCallback(() => {
    if (!audioContextRef.current) {
      audioContextRef.current = new (window.AudioContext || window.webkitAudioContext)();
    }
    return audioContextRef.current;
  }, []);

  const playBeep = useCallback(
    async (frequency = 1000, duration = 400) => {
      const INITIAL_GAIN = 1.0;
      const FINAL_GAIN = 0.01;
      const FALLBACK_VIBRATION_PATTERN = [200, 100, 200];

      try {
        const audioContext = initializeAudioContext();

        if (audioContext.state === 'suspended') {
          await audioContext.resume();
        }

        const oscillator = audioContext.createOscillator();
        const gainNode = audioContext.createGain();

        oscillator.connect(gainNode);
        gainNode.connect(audioContext.destination);

        oscillator.frequency.value = frequency;
        oscillator.type = 'sine';

        gainNode.gain.setValueAtTime(INITIAL_GAIN, audioContext.currentTime);
        gainNode.gain.exponentialRampToValueAtTime(
          FINAL_GAIN,
          audioContext.currentTime + duration / 1000
        );

        oscillator.start(audioContext.currentTime);
        oscillator.stop(audioContext.currentTime + duration / 1000);

        console.log('🔊 Beep played successfully');
      } catch (error) {
        console.warn('Audio playback failed:', error);
        try {
          if (window.navigator && window.navigator.vibrate) {
            window.navigator.vibrate(FALLBACK_VIBRATION_PATTERN);
            console.log('📳 Fallback to vibration');
          }
        } catch (vibrationError) {
          console.warn('Vibration fallback also failed:', vibrationError);
        }
      }
    },
    [initializeAudioContext]
  );

  // Helper function to unsubscribe from a topic
  const unsubscribeFromTopic = useCallback((topicRef, topicName) => {
    if (topicRef.current) {
      topicRef.current.unsubscribe();
      topicRef.current = null;
      console.log(`${topicName} topic unsubscribed`);
    }
  }, []);

  const cleanup = useCallback(() => {
    console.log('Starting ROS subscriptions cleanup...');

    // Unsubscribe from all topics
    unsubscribeFromTopic(taskStatusTopicRef, 'Task status');
    unsubscribeFromTopic(heartbeatTopicRef, 'Heartbeat');
    unsubscribeFromTopic(trainingStatusTopicRef, 'Training status');
    unsubscribeFromTopic(hfStatusTopicRef, 'HF status');
    // Workshop subscribers added in Phase-2/3 — without these the
    // /workflow/status and /workflow/sensors subscriptions leak onto
    // the dying ros connection and the next reconnect runs with two
    // parallel listeners. Audit round-3 §A / §NF-1.
    unsubscribeFromTopic(workflowStatusTopicRef, 'Workflow status');
    unsubscribeFromTopic(workflowSensorsTopicRef, 'Workflow sensors');

    // Reset previous phase tracking
    previousPhaseRef.current = null;

    if (audioContextRef.current && audioContextRef.current.state !== 'closed') {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }

    setConnected(false);
    dispatch(setHeartbeatStatus('disconnected'));
    console.log('ROS task status cleanup completed');
  }, [dispatch, unsubscribeFromTopic]);

  useEffect(() => {
    const enableAudioOnUserGesture = () => {
      const audioContext = initializeAudioContext();
      if (audioContext.state === 'suspended') {
        audioContext
          .resume()
          .then(() => {
            console.log('🎵 Audio enabled by user gesture');
          })
          .catch((error) => {
            console.warn('Failed to resume AudioContext on user gesture:', error);
          });
      }
    };

    const events = ['touchstart', 'touchend', 'mousedown', 'keydown', 'click'];
    events.forEach((event) => {
      document.addEventListener(event, enableAudioOnUserGesture, { once: true, passive: true });
    });

    return () => {
      events.forEach((event) => {
        document.removeEventListener(event, enableAudioOnUserGesture);
      });
    };
  }, [initializeAudioContext]);

  const subscribeToTaskStatus = useCallback(async () => {
    try {
      const RECORDING_BEEP_FREQUENCY = 1000;
      const RECORDING_BEEP_DURATION = 400;
      const BEEP_DELAY = 100;

      const ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      if (!ros) return;

      // Skip if already subscribed
      if (taskStatusTopicRef.current) {
        console.log('Task status already subscribed, skipping...');
        return;
      }

      setConnected(true);
      taskStatusTopicRef.current = new ROSLIB.Topic({
        ros,
        name: '/task/status',
        messageType: 'physical_ai_interfaces/msg/TaskStatus',
      });

      taskStatusTopicRef.current.subscribe((msg) => {
        console.log('Received task status:', msg);

        let progress = 0;

        if (msg.error !== '') {
          console.log('error:', msg.error);
          toast.error(msg.error);
          return;
        }

        const currentPhase = msg.phase;
        const previousPhase = previousPhaseRef.current;

        if (currentPhase === TaskPhase.RECORDING && previousPhase !== TaskPhase.RECORDING) {
          console.log('🔊 Recording started - playing beep sound');

          setTimeout(() => {
            playBeep(RECORDING_BEEP_FREQUENCY, RECORDING_BEEP_DURATION);
          }, BEEP_DELAY);

          toast.success('Recording started! 🎬');
        }

        previousPhaseRef.current = currentPhase;

        // Calculate progress percentage
        if (msg.phase === TaskPhase.SAVING) {
          // Saving data phase
          progress = msg.encoding_progress || 0;
        } else {
          // all other phases
          progress = msg.total_time > 0 ? (msg.proceed_time / msg.total_time) * 100 : 0;
        }

        const isRunning =
          msg.phase === TaskPhase.WARMING_UP ||
          msg.phase === TaskPhase.RESETTING ||
          msg.phase === TaskPhase.RECORDING ||
          msg.phase === TaskPhase.SAVING ||
          msg.phase === TaskPhase.INFERENCING;

        // ROS message to React state
        dispatch(
          setTaskStatus({
            robotType: msg.robot_type || '',
            taskName: msg.task_info?.task_name || 'idle',
            running: isRunning,
            phase: msg.phase || 0,
            progress: Math.round(progress),
            totalTime: msg.total_time || 0,
            proceedTime: msg.proceed_time || 0,
            currentEpisodeNumber: msg.current_episode_number || 0,
            currentScenarioNumber: msg.current_scenario_number || 0,
            currentTaskInstruction: msg.current_task_instruction || '',
            userId: msg.task_info?.user_id || '',
            usedStorageSize: msg.used_storage_size || 0,
            totalStorageSize: msg.total_storage_size || 0,
            usedCpu: msg.used_cpu || 0,
            usedRamSize: msg.used_ram_size || 0,
            totalRamSize: msg.total_ram_size || 0,
            error: msg.error || '',
            topicReceived: true,
          })
        );

        // Extract TaskInfo from TaskStatus message
        if (msg.task_info) {
          const infoUpdate = {
            taskName: msg.task_info.task_name || '',
            taskType: msg.task_info.task_type || '',
            taskInstruction: msg.task_info.task_instruction || [],
            policyPath: msg.task_info.policy_path || '',
            recordInferenceMode: msg.task_info.record_inference_mode || false,
            userId: msg.task_info.user_id || '',
            fps: msg.task_info.fps || 0,
            episodeTime: msg.task_info.episode_time_s || 0,
            resetTime: msg.task_info.reset_time_s || 0,
            numEpisodes: msg.task_info.num_episodes || 0,
            pushToHub: msg.task_info.push_to_hub || false,
            privateMode: msg.task_info.private_mode || false,
            useOptimizedSave: msg.task_info.use_optimized_save_mode || false,
            recordRosBag2: msg.task_info.record_rosbag2 || false,
          };

          // Only overwrite user-editable fields (tags, warmupTime) when a task is actively running,
          // so the server's values don't erase what the student typed in the UI.
          if (isRunning) {
            infoUpdate.tags = msg.task_info.tags || [];
            infoUpdate.warmupTime = msg.task_info.warmup_time_s || 0;
          }

          dispatch(setTaskInfo(infoUpdate));
        }

        // Set multi-task index safely with null checks and optimized search
        if (msg.task_info?.task_instruction && msg.current_task_instruction) {
          const taskIndex = msg.task_info.task_instruction.indexOf(msg.current_task_instruction);
          if (taskIndex !== -1) {
            dispatch(setMultiTaskIndex(taskIndex));
          } else {
            dispatch(setMultiTaskIndex(undefined));
          }
        }

        if (msg.task_info?.task_instruction.length > 1) {
          dispatch(setUseMultiTaskMode(true));
        } else {
          dispatch(setUseMultiTaskMode(false));
        }
      });
    } catch (error) {
      console.error('Failed to subscribe to task status topic:', error);
    }
  }, [dispatch, rosbridgeUrl, playBeep]);

  const subscribeToHeartbeat = useCallback(async () => {
    try {
      const ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      if (!ros) return;

      // Skip if already subscribed
      if (heartbeatTopicRef.current) {
        console.log('Heartbeat already subscribed, skipping...');
        return;
      }

      heartbeatTopicRef.current = new ROSLIB.Topic({
        ros,
        name: '/heartbeat',
        messageType: 'std_msgs/msg/Empty',
      });

      heartbeatTopicRef.current.subscribe(() => {
        dispatch(setHeartbeatStatus('connected'));
        dispatch(setLastHeartbeatTime(Date.now()));
      });

      console.log('Heartbeat subscription established');
    } catch (error) {
      console.error('Failed to subscribe to heartbeat topic:', error);
    }
  }, [dispatch, rosbridgeUrl]);

  // Start connection and subscription
  useEffect(() => {
    if (!rosbridgeUrl) return;

    const initializeSubscriptions = async () => {
      // Cleanup previous subscriptions before creating new ones
      cleanup();

      try {
        await subscribeToTaskStatus();
        await subscribeToHeartbeat();
        await subscribeToTrainingStatus();
        await subscribeHFStatus();
      } catch (error) {
        console.error('Failed to initialize ROS subscriptions:', error);
      }
    };

    initializeSubscriptions();

    return cleanup;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rosbridgeUrl]); // Only rosbridgeUrl as dependency to prevent unnecessary re-subscriptions

  // Helper function to get phase name
  const getPhaseName = useCallback((phase) => {
    const phaseNames = {
      [TaskPhase.READY]: 'NONE',
      [TaskPhase.WARMING_UP]: 'WARMING_UP',
      [TaskPhase.RESETTING]: 'RESETTING',
      [TaskPhase.RECORDING]: 'RECORDING',
      [TaskPhase.SAVING]: 'SAVING',
      [TaskPhase.STOPPED]: 'STOPPED',
      [TaskPhase.INFERENCING]: 'INFERENCING',
    };
    return phaseNames[phase] || 'UNKNOWN';
  }, []);

  // Function to reset task to initial state
  const resetTaskToIdle = useCallback(() => {
    setTaskStatus((prevStatus) => ({
      ...prevStatus,
      running: false,
      phase: 0,
    }));
  }, []);

  const subscribeToTrainingStatus = useCallback(async () => {
    try {
      const ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      if (!ros) return;

      // Skip if already subscribed
      if (trainingStatusTopicRef.current) {
        console.log('Training status already subscribed, skipping...');
        return;
      }

      setConnected(true);
      trainingStatusTopicRef.current = new ROSLIB.Topic({
        ros,
        name: '/training/status',
        messageType: 'physical_ai_interfaces/msg/TrainingStatus',
      });

      trainingStatusTopicRef.current.subscribe((msg) => {
        // Errors always pass through immediately
        if (msg.error !== '') {
          console.log('error:', msg.error);
          toast.error(msg.error);
          return;
        }

        // Throttle progress updates to max 1/sec to avoid excessive re-renders
        const now = Date.now();
        if (now - lastTrainingUpdateRef.current < 1000) return;
        lastTrainingUpdateRef.current = now;

        console.log('Received training status:', msg);

        // ROS message to React state
        dispatch(
          setTrainingInfo({
            datasetRepoId: msg.training_info.dataset || '',
            policyType: msg.training_info.policy_type || '',
            outputFolderName: msg.training_info.output_folder_name || '',
            seed: msg.training_info.seed || 0,
            numWorkers: msg.training_info.num_workers || 0,
            batchSize: msg.training_info.batch_size || 0,
            steps: msg.training_info.steps || 0,
            evalFreq: msg.training_info.eval_freq || 0,
            logFreq: msg.training_info.log_freq || 0,
            saveFreq: msg.training_info.save_freq || 0,
          })
        );

        const datasetParts = msg.training_info.dataset.split('/');
        dispatch(setSelectedUser(datasetParts[0] || ''));
        dispatch(setSelectedDataset(datasetParts[1] || ''));
        dispatch(setIsTraining(msg.is_training));
        dispatch(setCurrentStep(msg.current_step || 0));
        dispatch(setCurrentLoss(msg.current_loss));
        dispatch(setTopicReceived(true));
        dispatch(setLastUpdate(now));
      });
    } catch (error) {
      console.error('Failed to subscribe to training status topic:', error);
    }
  }, [dispatch, rosbridgeUrl]);

  const subscribeHFStatus = useCallback(async () => {
    try {
      const ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      if (!ros) return;

      // Skip if already subscribed
      if (hfStatusTopicRef.current) {
        console.log('HF status already subscribed, skipping...');
        return;
      }

      hfStatusTopicRef.current = new ROSLIB.Topic({
        ros,
        name: '/huggingface/status',
        messageType: 'physical_ai_interfaces/msg/HFOperationStatus',
      });

      hfStatusTopicRef.current.subscribe((msg) => {
        console.log('Received HF status:', msg);

        const status = msg.status;
        const operation = msg.operation;
        const repoId = msg.repo_id;
        // const localPath = msg.local_path;
        const message = msg.message;
        const progressCurrent = msg.progress_current;
        const progressTotal = msg.progress_total;
        const progressPercentage = msg.progress_percentage;

        if (status === 'Failed') {
          toast.error(message);
        } else if (status === 'Success') {
          toast.success(message);
          // Register the freshly-uploaded HF dataset in the cloud
          // registry so group siblings can discover it. Best-effort:
          // failure here doesn't break recording — the student can
          // still pass the repo_id manually.
          if (operation === 'upload' && repoId && repoId.includes('/')) {
            try {
              const accessToken =
                store.getState().auth.session?.access_token;
              if (accessToken) {
                const taskInfo = store.getState().tasks?.taskInfo || {};
                const repoLeaf = repoId.split('/').slice(1).join('/') || repoId;
                registerDataset(accessToken, {
                  hf_repo_id: repoId,
                  name: taskInfo.taskName || repoLeaf,
                  description: '',
                  fps: taskInfo.fps || undefined,
                  robot_type:
                    store.getState().tasks?.taskStatus?.robotType ||
                    undefined,
                }).catch((err) => {
                  console.warn(
                    '[datasets] register failed (non-fatal):',
                    err?.message || err
                  );
                });
              }
            } catch (err) {
              console.warn(
                '[datasets] register pre-call error (non-fatal):',
                err
              );
            }
          }
        }

        console.log('status:', status);

        // Check the current status from the store
        const currentStatus = store.getState().editDataset.hfStatus;

        if (
          (currentStatus === HFStatus.SUCCESS || currentStatus === HFStatus.FAILED) &&
          status === HFStatus.IDLE
        ) {
          console.log('Maintaining SUCCESS status, skipping IDLE update');
          // Skip updating the status
        } else {
          console.log('Updating HF status to:', status);
          dispatch(setHFStatus(status));
        }

        if (operation === 'upload') {
          dispatch(
            setUploadStatus({
              current: progressCurrent,
              total: progressTotal,
              percentage: progressPercentage.toFixed(2),
            })
          );
        } else if (operation === 'download') {
          dispatch(
            setDownloadStatus({
              current: progressCurrent,
              total: progressTotal,
              percentage: progressPercentage.toFixed(2),
            })
          );
        }
        const userId = repoId.split('/')[0];
        const repoName = repoId.split('/')[1];

        if (userId?.trim() && repoName?.trim()) {
          dispatch(setHFUserId(userId));

          if (operation === 'upload') {
            dispatch(setHFRepoIdUpload(repoName));
          } else if (operation === 'download') {
            dispatch(setHFRepoIdDownload(repoName));
          }
        }
      });

      console.log('HF status subscription established');
    } catch (error) {
      console.error('Failed to subscribe to HF status topic:', error);
    }
  }, [dispatch, rosbridgeUrl]);

  // Manual initialization function
  const initializeSubscriptions = useCallback(async () => {
    if (!rosbridgeUrl) {
      console.warn('Cannot initialize subscriptions: rosbridgeUrl is not set');
      return;
    }

    console.log('Manually initializing ROS subscriptions...');

    // Cleanup previous subscriptions before creating new ones
    cleanup();

    try {
      await subscribeToTaskStatus();
      await subscribeToHeartbeat();
      await subscribeToTrainingStatus();
      await subscribeHFStatus();
      console.log('ROS subscriptions initialized successfully');
    } catch (error) {
      console.error('Failed to initialize ROS subscriptions:', error);
    }
  }, [
    rosbridgeUrl,
    cleanup,
    subscribeToTaskStatus,
    subscribeToHeartbeat,
    subscribeToTrainingStatus,
    subscribeHFStatus,
  ]);

  // Intercept inline output tokens emitted by the workflow runtime via
  // ctx.log. We use a single text channel for all auxiliary outputs:
  //   "[SOUND]"         — play the default 880 Hz beep
  //   "[TONE:F:S]"      — play a tone of F Hz for S seconds
  //   "[SPEAK:text]"    — speak `text` via window.speechSynthesis (de-DE)
  //   "[VAR:name=json]" — variable inspector update
  // None of the tokens leak into the user-visible log strip.
  const interceptToken = useCallback((message) => {
    if (typeof message !== 'string') return { intercepted: false };
    if (message === '[SOUND]') {
      playBeep(880, 250);
      return { intercepted: true };
    }
    let m = /^\[TONE:([\d.]+):([\d.]+)\]$/.exec(message);
    if (m) {
      const freq = Math.max(50, Math.min(8000, Number(m[1])));
      const seconds = Math.max(0.05, Math.min(5, Number(m[2])));
      playBeep(freq, Math.round(seconds * 1000));
      return { intercepted: true };
    }
    m = /^\[SPEAK:(.*)\]$/s.exec(message);
    if (m) {
      try {
        if (window.speechSynthesis && window.SpeechSynthesisUtterance) {
          // Cap the spoken text — a workflow that logs a 50 kB
          // sentinel shouldn't queue an audio book.
          const spokenText = String(m[1] ?? '').slice(0, 500);
          const u = new window.SpeechSynthesisUtterance(spokenText);
          u.lang = 'de-DE';
          // Cancel any in-flight utterance so a tight loop of speak
          // blocks doesn't queue dozens of seconds of audio.
          window.speechSynthesis.cancel();
          window.speechSynthesis.speak(u);
        }
      } catch (e) {
        console.warn('SpeechSynthesis failed', e);
      }
      return { intercepted: true };
    }
    m = /^\[VAR:([^=]+)=(.*)\]$/s.exec(message);
    if (m) {
      try {
        // Harden against prototype-pollution and unbounded growth.
        // Audit round-3 §B / §38 — variable name must match the
        // Blockly identifier shape and the JSON payload is capped so
        // a runaway workflow can't balloon Redux state.
        const rawName = String(m[1] ?? '');
        const rawValue = String(m[2] ?? '');
        if (rawName.length > 64 || rawValue.length > 4096) {
          return { intercepted: true };
        }
        if (!/^[A-Za-zÄÖÜäöüß_][A-Za-zÄÖÜäöüß0-9_]*$/.test(rawName)) {
          return { intercepted: true };
        }
        if (rawName === '__proto__' || rawName === 'constructor' || rawName === 'prototype') {
          return { intercepted: true };
        }
        let value = null;
        try {
          value = JSON.parse(rawValue);
        } catch (e) {
          value = rawValue;
        }
        store.dispatch(setVariable({ name: rawName, value }));
      } catch (e) {
        console.warn('VAR token parse failed', e);
      }
      return { intercepted: true };
    }
    return { intercepted: false };
  }, [playBeep]);

  const subscribeToWorkflowSensors = useCallback(async () => {
    if (!rosbridgeUrl) return;
    if (workflowSensorsTopicRef.current) {
      const existingRos = workflowSensorsTopicRef.current.ros;
      if (existingRos && existingRos.isConnected) return;
      try {
        workflowSensorsTopicRef.current.unsubscribe();
      } catch (_) { /* topic already torn down */ }
      workflowSensorsTopicRef.current = null;
    }
    try {
      const ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      if (!ros || !ros.isConnected) return;
      const topic = new ROSLIB.Topic({
        ros,
        name: '/workflow/sensors',
        messageType: 'physical_ai_interfaces/msg/SensorSnapshot',
      });
      topic.subscribe((msg) => {
        dispatch(setSensorSnapshot({
          follower_joints: Array.from(msg.follower_joints || []),
          gripper_opening: Number(msg.gripper_opening || 0),
          visible_apriltag_ids: Array.from(msg.visible_apriltag_ids || []),
          color_counts: Array.from(msg.color_counts || [0, 0, 0, 0]),
          visible_object_classes: Array.from(msg.visible_object_classes || []),
        }));
      });
      workflowSensorsTopicRef.current = topic;
    } catch (e) {
      console.error('subscribeToWorkflowSensors failed:', e);
    }
  }, [dispatch, rosbridgeUrl]);

  const subscribeToWorkflowStatus = useCallback(async () => {
    if (!rosbridgeUrl) return;
    // Re-entrant: if a previous topic is bound to a now-dead ros
    // connection (after a rosbridge reconnect), drop it and re-subscribe.
    // Audit §3.8 — the v1 ship returned early on the first subscribe
    // attempt and never re-subscribed after a reconnect, so the
    // workflow status feed silently went dark.
    if (workflowStatusTopicRef.current) {
      const existingRos = workflowStatusTopicRef.current.ros;
      if (existingRos && existingRos.isConnected) return;
      try {
        workflowStatusTopicRef.current.unsubscribe();
      } catch (_) { /* topic already torn down */ }
      workflowStatusTopicRef.current = null;
    }
    try {
      const ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      if (!ros || !ros.isConnected) return;
      const topic = new ROSLIB.Topic({
        ros,
        name: '/workflow/status',
        messageType: 'physical_ai_interfaces/msg/WorkflowStatus',
      });
      topic.subscribe((msg) => {
        // Inline token interception for [SOUND], [TONE:..], [SPEAK:..],
        // [VAR:..]. None of these should appear in the user-visible
        // log strip — they are control channels for browser-side audio
        // and the variable inspector.
        const tokenResult = interceptToken(msg.log_message);
        dispatch(setWorkflowStatus({
          current_block_id: msg.current_block_id,
          phase: msg.phase,
          progress: msg.progress,
          error: msg.error || '',
          log_message: tokenResult.intercepted ? '' : msg.log_message,
        }));
        if (msg.phase === 'finished' || msg.phase === 'stopped' || msg.phase === 'error') {
          dispatch(setRunState(msg.phase));
          dispatch(setPaused(false));
          // Audit F34: surface workflow errors as a toast in addition
          // to the inline WorkflowStatus banner. Previously a cloud-
          // burst error wrote `error` into Redux but only appeared in
          // the run-controls strip, easy to miss.
          if (msg.phase === 'error' && msg.error) {
            try {
              toast.error(msg.error);
            } catch (_) {
              /* toast unavailable in non-DOM test env */
            }
          }
        } else if (msg.phase === 'running') {
          dispatch(setRunState('running'));
          dispatch(setPaused(false));
        } else if (msg.phase === 'paused') {
          dispatch(setPaused(true));
        }
        // Always dispatch detections — including an empty list — so
        // the editor clears stale bbox overlays once the workflow
        // moves past a perception block. Detection[] now carries
        // (cx, cy, w, h, label, confidence) directly; the v1 parallel
        // arrays (Point[] + string[]) were replaced after audit §1.6.
        if (Array.isArray(msg.active_detections)) {
          dispatch(setDetections({
            detections: msg.active_detections.map((d) => ({
              cx: d.cx,
              cy: d.cy,
              w: d.w,
              h: d.h,
              label: d.label,
              confidence: d.confidence,
            })),
          }));
        }
      });
      workflowStatusTopicRef.current = topic;
    } catch (e) {
      console.error('subscribeToWorkflowStatus failed:', e);
    }
  }, [dispatch, rosbridgeUrl, interceptToken]);

  return {
    connected,
    subscribeToTaskStatus,
    cleanup,
    getPhaseName,
    resetTaskToIdle,
    subscribeToTrainingStatus,
    subscribeHFStatus,
    subscribeToWorkflowStatus,
    subscribeToWorkflowSensors,
    initializeSubscriptions, // Manual initialization function
  };
}
