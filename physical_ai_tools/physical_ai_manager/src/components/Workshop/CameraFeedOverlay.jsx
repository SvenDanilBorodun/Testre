/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useRef, useState, useCallback } from 'react';
import { useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import ROSLIB from 'roslib';
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';
import { isCloudOnlyMode } from '../../utils/cloudMode';
import rosConnectionManager from '../../utils/rosConnectionManager';
import { STREAM_QUALITY } from '../../constants/streamConfig';

const CAMERA_TOPICS = {
  scene: '/scene/image_raw/compressed',
  gripper: '/gripper/image_raw/compressed',
};

// Audit F24: a frozen MJPEG keeps the TCP socket open with
// multipart/x-mixed-replace, so `img.onerror` never fires — the
// browser shows a "loaded" image with stale content. Use a rosbridge
// throttled subscription (~1 Hz) as a side-channel liveness ping;
// when no message arrives within FROZEN_THRESHOLD_MS, overlay a
// "Kamera eingefroren" badge so the student doesn't record / calibrate
// against a dead frame.
const FROZEN_THRESHOLD_MS = 2000;

function CameraFeedOverlay({ camera = 'scene', clickable = false, onMark, ...rest }) {
  // Hooks first, no conditional returns above them — react-hooks/rules
  // requires the same call order on every render. The cloud-mode
  // placeholder branches below the hooks (audit §3.7).
  const cloudOnly = isCloudOnlyMode();
  const rosHost = useSelector((s) => s.ros.rosHost) || window.location.hostname;
  const containerRef = useRef(null);
  const imgRef = useRef(null);
  const detections = useSelector((s) => s.workshop.detections);
  const { callService } = useRosServiceCaller();
  const [naturalSize, setNaturalSize] = useState({ w: 0, h: 0 });
  const [streamError, setStreamError] = useState(false);
  const [isFrozen, setIsFrozen] = useState(false);
  // Bumped to force a stream re-mount when we detect a stale stream
  // (audit F25 — naturalSize sticking after a mid-session resolution
  // change is fixed by re-creating the <img>).
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (cloudOnly) return undefined;
    const container = containerRef.current;
    if (!container) return undefined;

    const img = document.createElement('img');
    const topic = CAMERA_TOPICS[camera];
    if (!topic) return undefined;

    let cancelled = false;
    const timestamp = Date.now();
    img.src = `http://${rosHost}:8080/stream?quality=${STREAM_QUALITY}&type=ros_compressed&default_transport=compressed&topic=${topic}&t=${timestamp}`;
    img.alt = topic;
    img.className = 'block w-full h-full object-contain rounded-lg bg-black';
    img.onload = () => {
      if (cancelled) return;
      setNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
      setStreamError(false);
    };
    img.onerror = () => {
      if (cancelled) return;
      console.error(`CameraFeedOverlay: stream error for ${topic}`);
      setStreamError(true);
    };

    container.appendChild(img);
    imgRef.current = img;

    return () => {
      cancelled = true;
      img.src = '';
      if (img.parentNode) img.parentNode.removeChild(img);
      imgRef.current = null;
    };
  }, [camera, rosHost, cloudOnly, reloadKey]);

  // Audit F24: ROS-side liveness ping. Subscribe to the same compressed
  // image topic at 1 Hz throttle so a frozen MJPEG doesn't fool us. If
  // no message arrives for FROZEN_THRESHOLD_MS, flip the badge.
  useEffect(() => {
    if (cloudOnly) return undefined;
    const topic = CAMERA_TOPICS[camera];
    if (!topic) return undefined;
    const ros = rosConnectionManager?.ros;
    if (!ros || !ros.isConnected) return undefined;
    let lastSeenMs = Date.now();
    const subscription = new ROSLIB.Topic({
      ros,
      name: topic,
      messageType: 'sensor_msgs/CompressedImage',
      throttle_rate: 1000,
      queue_size: 1,
    });
    const onMsg = () => {
      lastSeenMs = Date.now();
      setIsFrozen(false);
    };
    subscription.subscribe(onMsg);
    const intervalId = setInterval(() => {
      const ageMs = Date.now() - lastSeenMs;
      setIsFrozen(ageMs > FROZEN_THRESHOLD_MS);
    }, 1000);
    return () => {
      clearInterval(intervalId);
      try {
        subscription.unsubscribe();
      } catch (e) {
        /* roslib throws if already torn down */
      }
    };
  }, [camera, cloudOnly]);

  // Audit F25: when a sustained freeze is detected, force a re-mount
  // of the <img> so the next reconnect grabs a fresh naturalSize.
  // Without this, MJPEG `onload` only fires on the first frame — a
  // mid-session resolution change leaves click-to-mark scaled by the
  // stale natural size and the arm misses.
  useEffect(() => {
    if (!isFrozen) return undefined;
    const t = setTimeout(() => setReloadKey((k) => k + 1), 5000);
    return () => clearTimeout(t);
  }, [isFrozen]);

  const handleClick = useCallback(
    async (e) => {
      if (!clickable || !imgRef.current || naturalSize.w === 0) return;
      const img = imgRef.current;
      const rect = img.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / rect.width) * naturalSize.w;
      const y = ((e.clientY - rect.top) / rect.height) * naturalSize.h;
      const label = window.prompt('Wie soll dieses Ziel heißen?', 'Ziel') || 'Ziel';
      try {
        const r = await callService(
          '/workshop/mark_destination',
          'physical_ai_interfaces/srv/MarkDestination',
          { camera, pixel_x: Math.round(x), pixel_y: Math.round(y), label }
        );
        if (!r.success) {
          toast.error(r.message || 'Ziel konnte nicht erstellt werden.');
          return;
        }
        toast.success(r.message);
        if (onMark) {
          onMark({
            label,
            world_x: r.world_x,
            world_y: r.world_y,
            world_z: r.world_z,
          });
        }
      } catch (err) {
        toast.error(`Service-Aufruf fehlgeschlagen: ${err.message || err}`);
      }
    },
    [callService, camera, clickable, naturalSize, onMark]
  );

  if (cloudOnly) {
    return (
      <div className="relative w-full aspect-video rounded-lg overflow-hidden bg-[var(--ink-5)] flex items-center justify-center text-center px-4">
        <p className="text-sm text-[var(--ink-4)]">
          Kamera-Stream nur in der Desktop-App verfügbar.
        </p>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      onClick={handleClick}
      className={
        'relative w-full aspect-video rounded-lg overflow-hidden bg-black ' +
        (clickable ? 'cursor-crosshair' : '')
      }
      {...rest}
    >
      <DetectionOverlay
        detections={detections}
        naturalSize={naturalSize}
      />
      {streamError && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/60 text-sm text-white px-4 text-center pointer-events-none">
          Kamera-Stream nicht erreichbar. Bitte Verbindung prüfen
          und neu laden.
        </div>
      )}
      {!streamError && isFrozen && (
        <div className="absolute top-2 right-2 px-2 py-1 rounded-md bg-amber-500/90 text-xs font-semibold text-white shadow pointer-events-none">
          Kamera eingefroren
        </div>
      )}
    </div>
  );
}

function DetectionOverlay({ detections, naturalSize }) {
  if (!detections || detections.length === 0 || naturalSize.w === 0) return null;
  // Detection now carries (cx, cy, w, h, label, confidence) directly.
  // The audit §1.6 fix replaced the parallel Point[] + string[] arrays
  // with a typed Detection.msg.
  return (
    <svg
      className="absolute inset-0 pointer-events-none w-full h-full"
      viewBox={`0 0 ${naturalSize.w} ${naturalSize.h}`}
      preserveAspectRatio="xMidYMid meet"
    >
      {detections.map((d, idx) => {
        if (!d || d.cx === undefined || d.w === undefined) return null;
        const x = d.cx - d.w / 2;
        const y = d.cy - d.h / 2;
        // Audit F31: render confidence next to the label so OWLv2's
        // low-score boxes (default threshold 0.10) are visibly
        // distinguishable from high-confidence locks. Confidence is a
        // 0..1 fraction on the Detection msg.
        const conf =
          typeof d.confidence === 'number'
            ? ` ${Math.round(d.confidence * 100)}%`
            : '';
        return (
          <g key={`${d.label || ''}-${idx}`}>
            <rect
              x={x}
              y={y}
              width={d.w}
              height={d.h}
              fill="none"
              stroke="#22c55e"
              strokeWidth="3"
            />
            {d.label && (
              <text
                x={x}
                y={y - 4}
                fill="#22c55e"
                fontSize="14"
                fontWeight="bold"
              >
                {d.label}{conf}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

export default CameraFeedOverlay;
