// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Inferenz page's cloud target (spec §6.3): the robot's own cameras on the
// stage, the model running on a cloud GPU. Chips: the robot, the rosbridge
// heartbeat, the LOCAL cameras' liveness, and the cloud's answer time from the
// robot's state topic (facts only; one HTTPS path to the cloud, O7').

import React from 'react';
import { useSelector } from 'react-redux';

import TaskPhase from '../../constants/taskPhases';
import HeartbeatStatus from '../HeartbeatStatus';
import ImageGrid from '../ImageGrid';
import Icon from '../icons/Icon';
import { useCameraLiveness } from '../../hooks/useCameraLiveness';
import useCloudInferenceState from '../../hooks/useCloudInferenceState';
import CloudInferencePanel from './CloudInferencePanel';
import CloudWait from './CloudWait';
import { cloudCopy, fill } from './cloudCopy';

const CHIP = 'h-8 px-3 rounded-full bg-white/[0.08] border border-white/15 backdrop-blur-md flex items-center gap-2 text-[11px] text-white/80';

/**
 * @param {object} p
 * @param {boolean} p.isActive
 * @param {{offered: boolean, enabled: boolean, reason: ?string}} p.cloud
 */
export default function CloudInferenceView({ isActive = true, cloud }) {
  const robotType = useSelector((s) => s.tasks.taskStatus.robotType);
  const phase = useSelector((s) => s.tasks.taskStatus.phase);
  const imageTopicList = useSelector((s) => s.ros.imageTopicList);
  const rosbridgeUrl = useSelector((s) => s.ros.rosbridgeUrl);
  const { liveCount } = useCameraLiveness({ topics: imageTopicList, rosbridgeUrl, enabled: isActive });
  const cloudState = useCloudInferenceState({ enabled: isActive });
  const configured = imageTopicList?.length || 0;
  const rtt = cloudState && cloudState.active && typeof cloudState.rtt_ms_p50 === 'number'
    ? Math.round(cloudState.rtt_ms_p50)
    : null;

  return (
    <div
      className="relative h-full w-full flex flex-col overflow-hidden"
      style={{ background: 'var(--dark-bg)', color: 'var(--dark-ink)' }}
    >
      <div className="absolute top-3 left-3 right-3 z-30 flex items-center gap-2 flex-wrap">
        <div className={CHIP}>
          <span className="font-mono uppercase tracking-wider opacity-70">Roboter</span>
          <span className="font-mono px-1.5 py-0.5 rounded bg-white/10 max-w-[160px] truncate">{robotType || '—'}</span>
        </div>
        <HeartbeatStatus dark />
        <div className={`${CHIP} font-mono whitespace-nowrap`} data-testid="cloud-camera-chip">
          <Icon name="camera" size={12} />
          {configured === 0 ? '—' : `${liveCount} / ${configured}`}
        </div>
        <div className={`${CHIP} whitespace-nowrap`} data-testid="cloud-chip">
          <Icon name="cloud" size={12} />
          <span>{cloudCopy.target.cloud}</span>
          {rtt !== null && <span className="font-mono">{fill(cloudCopy.status.rtt, { ms: rtt })}</span>}
        </div>
      </div>

      <div className="flex-1 flex items-start min-h-0 pt-[56px] pb-2 px-2 sm:px-3 lg:px-4 gap-2 sm:gap-3 lg:gap-4">
        <div className="flex-1 self-stretch min-w-0 relative rounded-[var(--radius-lg)] overflow-hidden">
          <ImageGrid isActive={isActive} />
          {phase === TaskPhase.INFERENCE_LOADING && (
            // The waiting card carries the live region; this overlay is not one,
            // so its clock is never read out every second. The card mounts with
            // the loading phase, so its clock counts from the start of the load.
            <div
              className="absolute inset-0 z-20 flex items-center justify-center p-4 bg-black/55 backdrop-blur-sm"
              data-testid="cloud-loading-overlay"
            >
              <CloudWait kind="model" tone="dark" />
            </div>
          )}
        </div>
        <div className="self-stretch w-[min(420px,38vw)] min-w-[300px] max-w-[480px]">
          <CloudInferencePanel isActive={isActive} cloud={cloud} cloudState={cloudState} />
        </div>
      </div>
    </div>
  );
}
