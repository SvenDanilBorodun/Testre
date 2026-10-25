// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The Start-page hero: the student's ACTUAL robot.
//
// What this replaces: a hand-drawn SVG of a two-finger gantry (matching none
// of the shipped arms) on a `camera-noise` background, under a hardcoded
// „LIVE" pill that rendered identically while the bridge was down. Every part
// of that claimed something the page could not back up.
//
// Two views, both real:
//   * 3D-Modell — the existing `UrdfTwin`, mirroring /joint_states onto the
//     URDF of the detected arm family (omx_f / v1).
//   * Kamera    — the scene camera's MJPEG stream, the same web_video_server
//     URL shape `CameraFeedOverlay` uses.
//
// THREE THINGS HERE ARE LOAD-BEARING:
//
//  1. `UrdfTwin` is imported with React.lazy and mounted ONLY while the bridge
//     is connected. It is a deliberate lazy chunk so three.js (~600 KB) and
//     urdf-loader stay out of the entry bundle that the white-screen CI greps
//     watch across five sites — and Start is the DEFAULT page, so a plain
//     import would pull three.js in for every student on every load.
//
//  2. The camera never starts by itself. Each MJPEG stream costs 5–8 Mbps
//     (`ImageGridCell` carries the explicit warning), and thirty students
//     landing on a page that auto-opens a stream is a different product from
//     thirty students landing on a 3D model. The twin is the default; the
//     camera is one deliberate click.
//
//  3. The liveness dot is bound to the twin's own `hasJointData`, which is
//     watchdogged (3 s) and NOT a latch. A dead feed turns the dot grey and
//     says so, instead of leaving a green dot over a frozen pose.

import React, { Suspense, useCallback, useMemo, useState } from 'react';
import { useSelector } from 'react-redux';

import { Led, RobotIcon, Screen } from '../sucher';
import { releasePointerFocus } from '../sucher/pointerFocus';
import { STREAM_QUALITY } from '../../constants/streamConfig';
import { usePiMode, videoStreamBase } from '../../utils/piMode';
import { robotDisplayName, robotHelpText } from '../../utils/robotIdentity';

const UrdfTwin = React.lazy(() => import('../UrdfTwin'));

// Student-scoped: a personal preference about their own workspace, so it is
// registered in utils/sessionScope.js::STUDENT_SCOPED_KEYS and scrubbed at
// handover. `sessionScope.test.js` fails on any edubotics* key in none of the
// three lists, so this constant and that entry ship together.
export const HOME_VIEW_KEY = 'edubotics_home_view';
const VIEW_TWIN = 'twin';
const VIEW_CAMERA = 'camera';

function readSavedView() {
  try {
    const v = localStorage.getItem(HOME_VIEW_KEY);
    return v === VIEW_CAMERA ? VIEW_CAMERA : VIEW_TWIN;
  } catch {
    return VIEW_TWIN;
  }
}

/**
 * Pick the topic to show. Prefer the scene camera — on a follower-only kit it
 * is the ONLY camera, and on a full OMX it is the one showing the workspace
 * rather than the inside of the gripper. Falls back to whatever is first.
 */
export function pickSceneTopic(topics) {
  const list = Array.isArray(topics) ? topics.filter((t) => typeof t === 'string' && t) : [];
  return list.find((t) => t.includes('scene')) || list[0] || '';
}

/**
 * web_video_server's `compressed` transport appends `/compressed` itself, so
 * it must receive the BARE topic — passing the suffixed name makes it
 * subscribe to `<topic>/compressed/compressed`, which has no publisher and
 * yields a black feed. Same rule (and the same bug) as CameraFeedOverlay.
 */
export function streamUrlFor(topic, base) {
  if (!topic) return '';
  const bare = topic.endsWith('/compressed') ? topic.slice(0, -'/compressed'.length) : topic;
  return `${base}/stream?quality=${STREAM_QUALITY}&type=ros_compressed`
    + `&default_transport=compressed&topic=${encodeURIComponent(bare)}`;
}

/**
 * @param {boolean|null} [jointsLive] — from hooks/useJointLiveness, owned by
 *   HomePage and shared with the Health-Check card. One subscription, two
 *   surfaces: two liveness indicators that can disagree is worse than none,
 *   and it must NOT come from the twin, which is unmounted in camera view.
 */
export default function RobotHero({ jointsLive = null }) {
  const { piMode } = usePiMode();
  const rosHost = useSelector((s) => s.ros.rosHost);
  const imageTopicList = useSelector((s) => s.ros.imageTopicList);
  const heartbeatStatus = useSelector((s) => s.tasks.heartbeatStatus);
  const robotType = useSelector((s) => s.tasks.taskStatus.robotType);
  const robotProfile = useSelector((s) => s.tasks.taskStatus.robotProfile);
  const caps = useSelector((s) => s.tasks.taskStatus.capabilities);

  const [view, setView] = useState(readSavedView);

  const connected = heartbeatStatus === 'connected';
  const name = robotDisplayName(caps, robotProfile, robotType);
  const help = robotHelpText(caps);
  const sceneTopic = useMemo(() => pickSceneTopic(imageTopicList), [imageTopicList]);
  const cameraAvailable = connected && !!sceneTopic;
  const streamUrl = useMemo(
    () => (cameraAvailable ? streamUrlFor(sceneTopic, videoStreamBase(rosHost, piMode)) : ''),
    [cameraAvailable, sceneTopic, rosHost, piMode],
  );

  const chooseView = useCallback((next) => {
    setView(next);
    try {
      localStorage.setItem(HOME_VIEW_KEY, next);
    } catch {
      /* storage disabled — the choice just does not survive the session */
    }
  }, []);

  // A remembered „Kamera" must not strand the student on a blank panel when
  // this rig has no camera topic. Fall back to the twin for the session
  // WITHOUT rewriting their choice, so the camera comes back when it does.
  const effectiveView = view === VIEW_CAMERA && !cameraAvailable ? VIEW_TWIN : view;

  // A real view switch (the two buttons below) releases the pointer focus on a
  // mouse click, like every Sucher key (D11).
  const pick = (next) => (event) => {
    releasePointerFocus(event);
    chooseView(next);
  };

  return (
    <Screen
      power
      className="su-hero h-[280px] sm:h-[340px] md:h-[380px] lg:h-full lg:min-h-[540px]"
    >
      {connected && effectiveView === VIEW_TWIN && (
        <Suspense fallback={<div className="absolute inset-0 eb-sweep" />}>
          <UrdfTwin showChrome={false} chronoTrail />
        </Suspense>
      )}

      {connected && effectiveView === VIEW_CAMERA && streamUrl && (
        <img
          src={streamUrl}
          alt={`Livebild der Kamera ${sceneTopic}`}
          className="absolute inset-0 w-full h-full object-contain"
        />
      )}

      {/* Flat, unmistakably inert stand-in while there is no robot to render:
          the robot's own sprite, dimmed and still. */}
      {!connected && (
        <div className="su-hero__offline" aria-hidden="true">
          <RobotIcon profile={robotProfile || robotType} light dim size={200} />
        </div>
      )}

      {/* Liveness — the real one. Grey whenever we are not receiving. */}
      <div className="absolute top-4 left-4 z-10 flex items-center gap-2 flex-wrap">
        {connected ? (
          effectiveView === VIEW_TWIN ? (
            <span className="su-hero__chip">
              <Led tone={jointsLive === true ? 'ok' : 'off'} pulse={jointsLive === true ? 'breathe' : 'none'} size={8} />
              <span>{jointsLive === true ? 'Gelenke live' : 'Wartet auf Gelenkdaten …'}</span>
            </span>
          ) : (
            <span className="su-hero__chip">
              <Led tone="ok" pulse="breathe" size={8} />
              <span>Kamerabild</span>
            </span>
          )
        ) : (
          <span className="su-hero__chip">
            <Led tone="off" size={8} />
            <span>Nicht verbunden</span>
          </span>
        )}
      </div>

      {/* View switch. Disabled rather than hidden when there is no camera, so
          the student can see the option exists and why it is unavailable. */}
      <div className="su-seg su-seg--glass su-seg--md absolute top-4 right-4 z-10">
        <button
          type="button"
          onClick={pick(VIEW_TWIN)}
          aria-pressed={effectiveView === VIEW_TWIN}
        >
          3D-Modell
        </button>
        <button
          type="button"
          onClick={pick(VIEW_CAMERA)}
          disabled={!cameraAvailable}
          aria-pressed={effectiveView === VIEW_CAMERA}
          title={cameraAvailable ? 'Livebild der Kamera zeigen' : 'Keine Kamera erkannt'}
        >
          Kamera
        </button>
      </div>

      {/* Identity. The German name from the wire, never the profile id. */}
      <div className="su-hero__id absolute bottom-0 left-0 right-0 z-10">
        <div className="su-lab">Dein Roboter</div>
        <div className="su-hero__name su-serif">
          {name || 'Roboter wird erkannt …'}
        </div>
        {help && <p className="su-hero__help">{help}</p>}
      </div>
    </Screen>
  );
}
