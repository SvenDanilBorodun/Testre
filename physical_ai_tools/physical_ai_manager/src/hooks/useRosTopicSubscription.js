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

import { useRef, useEffect, useState, useCallback, useMemo } from 'react';
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
  setCollision,
  isValidCapabilities,
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
import { recordInferenceRun } from '../services/jetsonClient';

// Parse `capabilities_json` at most ONCE per distinct string (D10). Every
// /task/status tick spreads a new taskStatus reference in the reducer, so a
// per-tick `JSON.parse` would hand `useSelector` a NEW capabilities object
// identity on every message → the app shell + whole nav re-render at status
// rate (during recording: every record tick). Caching the last string→object
// pair keeps the identity stable across identical ticks. MODULE-scope, NOT a
// `useRef`: this hook is instantiated TWICE (StudentApp + WorkshopPage), both
// subscribing + dispatching — per-instance caches would alternate two object
// identities per tick on the Workshop page. One cache serves both instances.
let _capsCache = { str: '', obj: null };

export function useRosTopicSubscription() {
  const taskStatusTopicRef = useRef(null);
  const heartbeatTopicRef = useRef(null);
  const trainingStatusTopicRef = useRef(null);
  const workflowStatusTopicRef = useRef(null);
  const workflowSensorsTopicRef = useRef(null);
  const previousPhaseRef = useRef(null);
  const audioContextRef = useRef(null);
  // Episode number that already received its 3-seconds-left warning beeps.
  const lastWarnEpisodeRef = useRef(null);
  // In-flight Jetson inference run (leLab-comparison PR-5b): start time +
  // policy captured on entering the inference phases; a compact record is
  // POSTed when the run ends (Jetson sessions are ephemeral — volume +
  // container logs wiped — so this is the only after-the-fact forensics).
  const inferenceRunRef = useRef(null);
  const hfStatusTopicRef = useRef(null);
  const lastTrainingUpdateRef = useRef(0);
  // Track the per-ros-instance reconnect listener so we don't double-bind
  // when the caller re-invokes the subscribe function. The refs are hoisted
  // here so `cleanup` (defined below) can detach the listeners on unmount.
  const workflowSensorsRebindRef = useRef(null);
  const workflowStatusRebindRef = useRef(null);

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
    // Detach the on('connection', rebind) listeners we wired in the
    // subscribe paths so they don't double-bind on the next mount.
    // Best-effort — `rosConnectionManager.ros` may be torn down already.
    try {
      const ros = rosConnectionManager.ros;
      if (ros && typeof ros.off === 'function') {
        if (workflowStatusRebindRef.current) {
          ros.off('connection', workflowStatusRebindRef.current);
        }
        if (workflowSensorsRebindRef.current) {
          ros.off('connection', workflowSensorsRebindRef.current);
        }
      }
    } catch (_) { /* ignored */ }
    workflowStatusRebindRef.current = null;
    workflowSensorsRebindRef.current = null;

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

  // Finalize + POST the in-flight Jetson inference run (best-effort; only
  // when a Jetson is connected and the student is signed in).
  const finalizeInferenceRun = useCallback((exitReason, errorMessage = '') => {
    const run = inferenceRunRef.current;
    inferenceRunRef.current = null;
    if (!run) return;
    const state = store.getState();
    if (state.jetson?.status !== 'connected') return;
    const token = state.auth?.session?.access_token;
    if (!token) return;
    recordInferenceRun(token, {
      policy_repo: run.policyRepo || 'unbekannt',
      jetson_id: state.jetson?.jetsonId || null,
      started_at: run.startedAtIso,
      duration_s: Math.max(0, (Date.now() - run.startedAtMs) / 1000),
      exit_reason: exitReason,
      error_message_de: (errorMessage || '').slice(0, 2000),
    }).catch((err) => {
      // Telemetry only — never surface a failure to the student.
      console.warn('Inferenz-Protokoll konnte nicht gespeichert werden:', err);
    });
  }, []);

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

        // Teleop force/collision e-stop. The server publishes (error kept empty)
        // phase=COLLISION when the follower was forced against an object and halted in
        // place, phase=COLLISION_HOMING while the student-triggered safe-home glide runs,
        // and phase=COLLISION_HOMED once the follower verifiably reached home. Drive the
        // blocking two-step CollisionModal off these, and clear it when a non-collision
        // status arrives (resume publishes phase=READY). Handled BEFORE the error
        // early-return below so it can never be masked by an error toast.
        const collisionStage =
          msg.phase === TaskPhase.COLLISION
            ? 'stopped'
            : msg.phase === TaskPhase.COLLISION_HOMING
              ? 'homing'
              : msg.phase === TaskPhase.COLLISION_HOMED
                ? 'homed'
                : null;
        if (collisionStage !== null) {
          dispatch(
            setCollision({
              active: true,
              stage: collisionStage,
              message: msg.current_task_instruction || '',
              // Per-joint |pos - home| (rad, joint1..joint5) for the homing
              // progress strip. Pre-rebuild server images omit the field —
              // default to [] so the modal simply hides the strip.
              jointDistToHome: Array.from(msg.joint_dist_to_home ?? []),
            })
          );
          previousPhaseRef.current = msg.phase;
          return;
        }
        if (
          previousPhaseRef.current === TaskPhase.COLLISION ||
          previousPhaseRef.current === TaskPhase.COLLISION_HOMING ||
          previousPhaseRef.current === TaskPhase.COLLISION_HOMED
        ) {
          dispatch(
            setCollision({ active: false, stage: 'stopped', message: '', jointDistToHome: [] })
          );
        }

        if (msg.error !== '') {
          console.log('error:', msg.error);
          // An error while a Jetson inference run is in flight is that
          // run's exit reason (PR-5b).
          if (inferenceRunRef.current) {
            finalizeInferenceRun('error', msg.error);
          }
          toast.error(msg.error);
          return;
        }

        const currentPhase = msg.phase;
        const previousPhase = previousPhaseRef.current;

        // Audio cues (leLab-comparison PR-3). A student demonstrating a
        // manipulation task watches the ARM, not the screen — sound is the
        // right channel for phase changes. Muteable via the ControlPanel
        // „Ton"-toggle (localStorage, read fresh each event so the toggle
        // applies without re-subscribing).
        const audioMuted = localStorage.getItem('edubotics_audio_muted') === '1';

        if (currentPhase === TaskPhase.RECORDING && previousPhase !== TaskPhase.RECORDING) {
          console.log('🔊 Recording started - playing beep sound');
          // New episode/session entering RECORDING — re-arm the 3-seconds
          // warning (the episode counter restarts per session, and a stale
          // ref from the previous session could suppress one warning).
          lastWarnEpisodeRef.current = null;

          if (!audioMuted) {
            setTimeout(() => {
              playBeep(RECORDING_BEEP_FREQUENCY, RECORDING_BEEP_DURATION);
            }, BEEP_DELAY);
          }

          toast.success('Aufnahme gestartet! 🎬');
        }

        // Falling two-tone when the episode leaves RECORDING (auto-save or
        // reset) — the eyes-on-arm signal that this take is in the can.
        if (
          previousPhase === TaskPhase.RECORDING &&
          (currentPhase === TaskPhase.SAVING || currentPhase === TaskPhase.RESETTING) &&
          !audioMuted
        ) {
          playBeep(660, 180);
          setTimeout(() => playBeep(440, 220), 200);
        }

        // Triple warning beep in the final 3 s before the episode
        // auto-saves, once per episode (guarded by episode number).
        if (
          currentPhase === TaskPhase.RECORDING &&
          msg.total_time > 0 &&
          msg.total_time - msg.proceed_time <= 3 &&
          lastWarnEpisodeRef.current !== msg.current_episode_number &&
          !audioMuted
        ) {
          lastWarnEpisodeRef.current = msg.current_episode_number;
          [0, 250, 500].forEach((delay) => {
            setTimeout(() => playBeep(880, 120), delay);
          });
        }

        // Jetson inference run lifecycle (PR-5b). Entering either
        // inference phase from outside starts the record; leaving to a
        // non-inference phase finalizes it as 'stopped' (inference has no
        // natural completion — the student presses Stopp; errors are
        // finalized in the error branch above).
        const inferencePhases = [TaskPhase.INFERENCING, TaskPhase.INFERENCE_LOADING];
        const inInference = inferencePhases.includes(currentPhase);
        const wasInference = inferencePhases.includes(previousPhase);
        if (inInference && !wasInference) {
          const jetsonState = store.getState();
          if (jetsonState.jetson?.status === 'connected') {
            inferenceRunRef.current = {
              startedAtMs: Date.now(),
              startedAtIso: new Date().toISOString(),
              policyRepo: jetsonState.tasks?.taskInfo?.policyPath || '',
            };
          }
        } else if (!inInference && wasInference && inferenceRunRef.current) {
          finalizeInferenceRun('stopped');
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
          msg.phase === TaskPhase.INFERENCING ||
          msg.phase === TaskPhase.INFERENCE_LOADING;

        // May the ROBOT tell this browser who the student is?
        //
        // `task_info.user_id` is the Hugging Face account a recording uploads
        // under — the one genuinely person-scoped field on this wire. The ROS
        // node keeps its own copy for the life of a task, so it survives BOTH a
        // sign-out and a change of student: nothing unsubscribes and nothing
        // can, because the browser still has to show the running task. Two
        // executed leaks, one adopt:
        //   * after `signOutStudent`, ONE tick put the previous student's id
        //     back into Redux AND into storage (`setTaskInfo` re-persists a
        //     truthy userId), with no user action at all;
        //   * student B signing in while the node still holds A's `user_id`
        //     inherited it the same way — for which "is anybody signed in?" is
        //     no answer at all, since somebody is.
        //
        // So the gate is an IDENTITY comparison, not a session check. The one
        // student-scoped identity in the store is `auth.hfUsername`, and it is
        // the SAME id space as `task_info.user_id`: `useMeProfile`'s auto-link
        // PATCHes `/me` with the selected Benutzer-ID, i.e. with the very value
        // the recorder sends. Fail-safe by construction — `hfUsername` is null
        // until `/me` resolves and is cleared by `signedOut`, and a null never
        // equals a non-empty id, so an unknown identity adopts nothing.
        //
        // Accepted cost: a student on the „Ohne Anmeldung fortfahren" offline
        // escape — since the student login gate (utils/authGate) the only way
        // to reach these pages signed out — no longer inherits the id of a task
        // started elsewhere; they keep their own persisted Benutzer-ID in a
        // field that is read-only while a task runs anyway. Nothing else reads
        // `taskStatus.userId`, so blanking that half is free.
        const myHfUsername = store.getState().auth?.hfUsername || '';
        const robotUserId = msg.task_info?.user_id || '';
        const robotNamesMe = Boolean(myHfUsername) && robotUserId === myHfUsername;

        // ROS message to React state
        const statusPayload = {
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
          userId: robotNamesMe ? robotUserId : '',
          usedStorageSize: msg.used_storage_size || 0,
          totalStorageSize: msg.total_storage_size || 0,
          usedCpu: msg.used_cpu || 0,
          usedRamSize: msg.used_ram_size || 0,
          totalRamSize: msg.total_ram_size || 0,
          error: msg.error || '',
          topicReceived: true,
        };

        // Robot profile id + capability manifest ride the same wire (D2). Add
        // them ONLY when the wire fields are non-empty. Two reasons a tick
        // carries no usable identity:
        //  - PRE-CAPABILITY / old server image (sends ''). The collision monitor
        //    now self-stamps identity (_stamp_identity), so it no longer emits
        //    empty identity — but the degraded-boot path (communicator=None)
        //    still can, and both must never wipe a settled profile/caps.
        //  - JETSON mode: while the student holds a classroom-Jetson lock the
        //    rosbridge points at the Jetson's omx_follower server, whose caps
        //    describe the JETSON, not the LOCAL rig. Adopting them would leave a
        //    stale omx_follower manifest in Redux that wrongly hides
        //    Aufnahme/Daten/Training the instant the lock is released (the nav
        //    capability filter is skipped only WHILE connected). So skip adoption
        //    entirely here; clearCapabilities on release resets caps to null.
        // `capabilities_json` is parsed at most once per distinct string via the
        // module cache (D10); a parse error OR an incomplete manifest ('{}',
        // partial) keeps the previous cached object and OMITs the key so a
        // malformed/empty tick can't null it out or fail open.
        const jetsonConnected = store.getState().jetson?.status === 'connected';
        if (!jetsonConnected) {
          if (msg.robot_profile) {
            statusPayload.robotProfile = msg.robot_profile;
          }
          const capsStr = msg.capabilities_json;
          if (capsStr) {
            if (capsStr !== _capsCache.str) {
              try {
                const parsed = JSON.parse(capsStr);
                if (isValidCapabilities(parsed)) {
                  _capsCache = { str: capsStr, obj: parsed };
                  statusPayload.capabilities = parsed;
                } else {
                  // Valid JSON but not a COMPLETE manifest ('{}', partial,
                  // non-boolean values). Cache the string so we don't re-validate
                  // it every tick, keep the last good object, and OMIT the key so
                  // the reducer keeps the settled manifest (no fail-open).
                  _capsCache = { str: capsStr, obj: _capsCache.obj };
                }
              } catch (e) {
                // Malformed caps JSON — keep the previous good object (string-key
                // it so we don't re-parse the same bad string every tick) and
                // OMIT the key so the reducer keeps the last good value.
                _capsCache = { str: capsStr, obj: _capsCache.obj };
              }
            } else if (_capsCache.obj) {
              // Same string as last time → reuse the cached object (stable identity).
              statusPayload.capabilities = _capsCache.obj;
            }
          }
        }

        dispatch(setTaskStatus(statusPayload));

        // Extract TaskInfo from TaskStatus message. GATED on a real task
        // (D8 companion #1): the idle identity tick + collision-monitor bare
        // ticks carry a default-constructed (all-empty) task_info, and
        // dispatching it would wipe the student's Record/Inference FORM
        // (taskName, fps, episodeTime, …) every ~3 s while they type. A real
        // task always has a non-empty task_name; a bare tick never does.
        if (msg.task_info && (isRunning || msg.task_info.task_name)) {
          const infoUpdate = {
            taskName: msg.task_info.task_name || '',
            taskType: msg.task_info.task_type || '',
            taskInstruction: msg.task_info.task_instruction || [],
            policyPath: msg.task_info.policy_path || '',
            recordInferenceMode: msg.task_info.record_inference_mode || false,
            // userId intentionally NOT defaulted to '' here — that would wipe
            // the student's saved Benutzer-ID on every idle status tick. It is
            // adopted from the server only when non-empty, below.
            fps: msg.task_info.fps || 0,
            episodeTime: msg.task_info.episode_time_s || 0,
            resetTime: msg.task_info.reset_time_s || 0,
            numEpisodes: msg.task_info.num_episodes || 0,
            pushToHub: msg.task_info.push_to_hub || false,
            // privateMode intentionally NOT set here — see the robotNamesMe
            // gate below. Adopting it unconditionally let a task left behind
            // by the PREVIOUS student silently un-tick the next student's
            // private-by-default box.
            useOptimizedSave: msg.task_info.use_optimized_save_mode || false,
            recordRosBag2: msg.task_info.record_rosbag2 || false,
          };

          // Only overwrite user-editable fields (tags, warmupTime) when a task is actively running,
          // so the server's values don't erase what the student typed in the UI.
          if (isRunning) {
            infoUpdate.tags = msg.task_info.tags || [];
            infoUpdate.warmupTime = msg.task_info.warmup_time_s || 0;
          }

          // Adopt the server's user_id only when non-empty (e.g. resuming a
          // task started elsewhere); never overwrite a saved selection with ''.
          // And only when it NAMES the signed-in student — see robotNamesMe.
          if (robotNamesMe) {
            infoUpdate.userId = robotUserId;
            // `private_mode` is gated on the SAME identity comparison, and for
            // a stronger reason than user_id: the visibility of a recording is
            // a data-protection decision about OTHER people (classmates' faces
            // and voices), taken once, irreversibly, at upload time.
            //
            // The ROS node keeps `task_info` for the life of a task, so it
            // survives a handover exactly like user_id does (see the block
            // above). With this ungated, one tick carrying the previous
            // student's finished PUBLIC task overwrote the new student's
            // private-by-default value — in a form field that is read-only
            // while a task runs, so they could not even see it change before
            // pressing record.
            //
            // `!== false`, not `|| false`: an absent or garbled flag keeps the
            // private default instead of failing open to public. That matches
            // TaskInfo.msg's own `bool private_mode true`.
            infoUpdate.privateMode = msg.task_info.private_mode !== false;
          }

          dispatch(setTaskInfo(infoUpdate));

          // Set multi-task index safely with null checks and optimized search
          if (msg.task_info.task_instruction && msg.current_task_instruction) {
            const taskIndex = msg.task_info.task_instruction.indexOf(msg.current_task_instruction);
            if (taskIndex !== -1) {
              dispatch(setMultiTaskIndex(taskIndex));
            } else {
              dispatch(setMultiTaskIndex(undefined));
            }
          }

          if (msg.task_info.task_instruction.length > 1) {
            dispatch(setUseMultiTaskMode(true));
          } else {
            dispatch(setUseMultiTaskMode(false));
          }
        }
      });
    } catch (error) {
      console.error('Failed to subscribe to task status topic:', error);
    }
  }, [dispatch, rosbridgeUrl, playBeep, finalizeInferenceRun]);

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

  // Register a freshly-uploaded HF dataset in the cloud registry, with one
  // retry on a transient failure and a German warning toast on final
  // failure. Reads the access token + task context from the store at call
  // time (this runs inside a ROS topic callback, not a render), so it has no
  // reactive deps. Best-effort by contract — never throws into the caller.
  const registerUploadedDataset = useCallback((repoId) => {
    const accessToken = store.getState().auth.session?.access_token;
    if (!accessToken) {
      // No cloud session. Since the student login gate (utils/authGate) this
      // branch is reachable ONLY through the „Ohne Anmeldung fortfahren"
      // offline escape — which is also the behaviour change worth stating: with
      // a login enforced, EVERY ordinary recording now auto-registers in the
      // cloud dataset registry, where before this skip was the common case.
      // (That registry is what makes GDPR erasure enumerable, so the change is
      // a good one.) The student can still sync later from the Training tab;
      // nothing to warn about here.
      console.warn('[datasets] skip auto-register: not signed in');
      return;
    }
    const taskInfo = store.getState().tasks?.taskInfo || {};
    const repoLeaf = repoId.split('/').slice(1).join('/') || repoId;
    const payload = {
      hf_repo_id: repoId,
      name: taskInfo.taskName || repoLeaf,
      description: '',
      fps: taskInfo.fps || undefined,
      robot_type: store.getState().tasks?.taskStatus?.robotType || undefined,
    };

    const RETRY_DELAY_MS = 1500;
    const attempt = (retriesLeft) => {
      registerDataset(accessToken, payload).catch((err) => {
        const status = err?.status;
        // A 4xx (already-registered 409, auth 401/403, validation 422) won't
        // be fixed by retrying — only retry transient (5xx / network) once.
        const transient = !status || status >= 500;
        if (retriesLeft > 0 && transient) {
          console.warn(
            '[datasets] register failed, retrying once:',
            err?.message || err
          );
          setTimeout(() => attempt(retriesLeft - 1), RETRY_DELAY_MS);
          return;
        }
        // A 409 (already known) is a success from the student's POV — the
        // dataset IS in the registry. Don't alarm them.
        if (status === 409) {
          console.warn('[datasets] already registered (409) — treating as ok');
          return;
        }
        console.warn(
          '[datasets] register failed (final):',
          err?.message || err
        );
        toast.error(
          'Datensatz konnte nicht automatisch registriert werden – im ' +
            'Training-Tab „Datensätze synchronisieren" klicken.',
          { duration: 7000 }
        );
      });
    };
    attempt(1);
  }, []);

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
          // failure here doesn't break recording. Hardened (vs the old
          // silent single-shot `.catch`): require the access token,
          // retry once on a transient failure, and on FINAL failure show
          // a German warning toast pointing at the manual sync button —
          // so a dropped registration is visible, not silently lost.
          if (operation === 'upload' && repoId && repoId.includes('/')) {
            registerUploadedDataset(repoId);
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
  }, [dispatch, rosbridgeUrl, registerUploadedDataset]);

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
  //   "[SOUND]"             — play the default 880 Hz beep
  //   "[TONE:F:S]"          — play a tone of F Hz for S seconds
  //   "[SPEAK:text]"        — speak `text` via window.speechSynthesis (de-DE)
  //   "[TOAST:level:sec:t]" — on-screen react-hot-toast popup (level →
  //                           info/success/warning/error, sec = duration)
  //   "[VAR:name=json]"     — variable inspector update
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
    // [TOAST:level:sec:text] — on-screen popup via the existing react-hot-toast
    // instance (already imported above). The level alternation in the regex
    // restricts it to the 4 known severities, so a malformed token is NOT
    // intercepted (it falls through and stays visible in the log strip). The
    // text is the last capture, so it may itself contain ':' (the backend
    // strips ']' so it can't break the closing bracket).
    m = /^\[TOAST:(info|success|warning|error):([\d.]+):(.*)\]$/s.exec(message);
    if (m) {
      const level = m[1];
      const seconds = Math.max(1, Math.min(15, Number(m[2]) || 3));
      const text = String(m[3] ?? '').slice(0, 240);
      try {
        const opts = { duration: seconds * 1000 };
        if (level === 'success') {
          toast.success(text, opts);
        } else if (level === 'error') {
          toast.error(text, opts);
        } else if (level === 'warning') {
          toast(text, { ...opts, icon: '⚠️' });
        } else {
          toast(text, opts);
        }
      } catch (e) {
        // toast unavailable in a non-DOM test env — never break the feed.
        console.warn('TOAST token failed', e);
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
      // Audit §A.r3: flip `connected` truthiness here too so callers that
      // only mount /workflow/sensors (e.g. WorkshopPage entering the
      // editor view before /task/status fires) get an accurate flag.
      setConnected(true);
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
        }));
      });
      workflowSensorsTopicRef.current = topic;
      // Auto-rebind on rosbridge reconnect. ROSLIB.Ros emits 'connection'
      // every time the socket reattaches; without a rebind, a Wi-Fi blip
      // silently kills the SensorPanel forever. Drop the old listener
      // first so multiple re-subscribes don't pile up.
      if (workflowSensorsRebindRef.current && typeof ros.off === 'function') {
        try {
          ros.off('connection', workflowSensorsRebindRef.current);
        } catch (_) { /* listener already gone */ }
      }
      const rebind = () => {
        // Clear the topic ref so the re-entrant subscribe path doesn't
        // short-circuit on a stale Topic bound to the dead socket.
        if (workflowSensorsTopicRef.current) {
          try { workflowSensorsTopicRef.current.unsubscribe(); } catch (_) { /* ignored */ }
          workflowSensorsTopicRef.current = null;
        }
        subscribeToWorkflowSensors();
      };
      workflowSensorsRebindRef.current = rebind;
      if (typeof ros.on === 'function') ros.on('connection', rebind);
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
      // Audit §A.r3: flip `connected` truthiness on every subscribe path
      // (was only set in /task/status before).
      setConnected(true);
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
              // Grasp-orientation overlay (named-object grasping): a pinch-axis
              // angle (image space, rad) + a flag for whether it's meaningful.
              // This mapper cherry-picks Detection fields, so an unmapped field
              // is silently dropped — keep these two in sync with Detection.msg.
              graspAngleRad: d.grasp_angle_rad,
              hasGraspAngle: d.has_grasp_angle,
            })),
          }));
        }
      });
      workflowStatusTopicRef.current = topic;
      // Auto-rebind on rosbridge reconnect (audit §A.r3) — the v1 ship
      // returned early on the first subscribe attempt's `if existingRos
      // && existingRos.isConnected` guard, but never re-subscribed after
      // a brief socket drop, so the WorkflowStatus feed silently went
      // dark. Drop any previous listener first to avoid double-binding.
      if (workflowStatusRebindRef.current && typeof ros.off === 'function') {
        try {
          ros.off('connection', workflowStatusRebindRef.current);
        } catch (_) { /* listener already gone */ }
      }
      const rebind = () => {
        if (workflowStatusTopicRef.current) {
          try { workflowStatusTopicRef.current.unsubscribe(); } catch (_) { /* ignored */ }
          workflowStatusTopicRef.current = null;
        }
        subscribeToWorkflowStatus();
      };
      workflowStatusRebindRef.current = rebind;
      if (typeof ros.on === 'function') ros.on('connection', rebind);
    } catch (e) {
      console.error('subscribeToWorkflowStatus failed:', e);
    }
  }, [dispatch, rosbridgeUrl, interceptToken]);

  // Memoize the returned object so callers' useEffect(..., [subscriptions])
  // doesn't fire every render. Every callback above is already wrapped in
  // useCallback with stable deps; this wrapper is the final piece — without
  // it, the new object reference each render would invalidate downstream
  // effects (e.g. WorkshopPage.js's re-subscribe useEffect at line ~80).
  return useMemo(
    () => ({
      connected,
      subscribeToTaskStatus,
      cleanup,
      getPhaseName,
      subscribeToTrainingStatus,
      subscribeHFStatus,
      subscribeToWorkflowStatus,
      subscribeToWorkflowSensors,
      initializeSubscriptions, // Manual initialization function
    }),
    [
      connected,
      subscribeToTaskStatus,
      cleanup,
      getPhaseName,
      subscribeToTrainingStatus,
      subscribeHFStatus,
      subscribeToWorkflowStatus,
      subscribeToWorkflowSensors,
      initializeSubscriptions,
    ]
  );
}
