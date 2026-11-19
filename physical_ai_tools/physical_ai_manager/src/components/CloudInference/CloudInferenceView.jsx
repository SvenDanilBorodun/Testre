// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The Inferenz page's cloud target (spec §6.3): the robot's own cameras on the
// stage, the model running on a cloud GPU. Chips: the robot, the rosbridge
// heartbeat, the LOCAL cameras' liveness, and the cloud's answer time from the
// robot's state topic (facts only; one HTTPS path to the cloud, O7').
//
// Above the panel with Start: a warning when the reserved model was trained with
// another scene camera than the one the robot states (D58). Both must be known;
// it never disables Start.

import React from 'react';
import { useSelector } from 'react-redux';

import TaskPhase from '../../constants/taskPhases';
import HeartbeatStatus from '../HeartbeatStatus';
import ImageGrid from '../ImageGrid';
import Icon from '../icons/Icon';
import { Led, Plate, Screen } from '../sucher';
import '../sucher-pages/inference.css';
import { useCameraLiveness } from '../../hooks/useCameraLiveness';
import useCloudInferenceSession from '../../hooks/useCloudInferenceSession';
import useCloudInferenceState from '../../hooks/useCloudInferenceState';
import useSceneCameraProfile, { sceneCameraProfileOf } from '../../hooks/useSceneCameraProfile';
import CloudInferencePanel from './CloudInferencePanel';
import CloudWait from './CloudWait';
import { cloudCopy, fill } from './cloudCopy';

// A glass chip on the camera screen (Sucher: a small readout).
const CHIP = 'su-inf-chip';

/**
 * @param {object} p
 * @param {boolean} p.isActive
 * @param {{offered: boolean, enabled: boolean, reason: ?string}} p.cloud
 */
export default function CloudInferenceView({ isActive = true, cloud }) {
  const robotType = useSelector((s) => s.tasks.taskStatus.robotType);
  const robotProfile = useSelector((s) => s.tasks.taskStatus.robotProfile);
  const phase = useSelector((s) => s.tasks.taskStatus.phase);
  const imageTopicList = useSelector((s) => s.ros.imageTopicList);
  const rosbridgeUrl = useSelector((s) => s.ros.rosbridgeUrl);
  const { liveCount } = useCameraLiveness({ topics: imageTopicList, rosbridgeUrl, enabled: isActive });
  const cloudState = useCloudInferenceState({ enabled: isActive });
  const modelScene = sceneCameraProfileOf(useCloudInferenceSession().sceneCameraProfile);
  const robotScene = useSceneCameraProfile({ enabled: isActive });
  const sceneMismatch = modelScene !== null && robotScene !== null && modelScene !== robotScene;
  const configured = imageTopicList?.length || 0;
  const rtt = cloudState && cloudState.active && typeof cloudState.rtt_ms_p50 === 'number'
    ? Math.round(cloudState.rtt_ms_p50)
    : null;

  return (
    <div className="su-inf relative h-full w-full flex flex-col overflow-hidden">
      <div className="su-inf__body">
        <Screen power className="su-inf__screen">
          <div className="su-inf__chips">
            <div className={CHIP}>
              <span className="su-inf-chip__key">Roboter</span>
              <span className="max-w-[160px] truncate">{robotType || '—'}</span>
            </div>
            <HeartbeatStatus dark size="sm" />
            <div className={`${CHIP} whitespace-nowrap`} data-testid="cloud-camera-chip">
              <Icon name="camera" size={13} />
              {configured === 0 ? '—' : `${liveCount} / ${configured}`}
            </div>
            <div className={`${CHIP} su-inf-chip--text whitespace-nowrap`} data-testid="cloud-chip">
              <Led tone={cloudState && cloudState.active ? 'sig' : 'off'} pulse={cloudState && cloudState.active ? 'breathe' : 'none'} size={7} />
              <Icon name="cloud" size={13} />
              <span>{cloudCopy.target.cloud}</span>
              {rtt !== null && <span className="su-inf-chip__num">{fill(cloudCopy.status.rtt, { ms: rtt })}</span>}
            </div>
          </div>
          <div className="su-inf__grid">
            <ImageGrid isActive={isActive} />
            {phase === TaskPhase.INFERENCE_LOADING && (
              <div
                className="absolute inset-0 z-20 flex items-center justify-center p-4 bg-black/55 backdrop-blur-sm"
                data-testid="cloud-loading-overlay"
              >
                <CloudWait kind="model" tone="dark" robotProfile={robotProfile || robotType} />
              </div>
            )}
          </div>
        </Screen>
        <Plate as="aside" className="su-inf__panel">
          {sceneMismatch && (
            <div role="status" className="shrink-0 px-5 pt-5 flex items-start gap-2 text-sm text-[color:var(--amber)]"
              data-testid="cloud-scene-mismatch">
              <Icon name="warning" size={16} className="mt-0.5 shrink-0" />
              <span>{fill(cloudCopy.sceneMismatch, {
                then: cloudCopy.sceneCameraNames[modelScene],
                now: cloudCopy.sceneCameraNames[robotScene],
              })}</span>
            </div>
          )}
          <CloudInferencePanel isActive={isActive} cloud={cloud} cloudState={cloudState} />
        </Plate>
      </div>
    </div>
  );
}
