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
import { useRosServiceCaller } from '../../hooks/useRosServiceCaller';

const CAMERA_TOPICS = {
  scene: '/scene/image_raw/compressed',
  gripper: '/gripper/image_raw/compressed',
};

function CameraFeedOverlay({ camera = 'scene', clickable = false, onMark, ...rest }) {
  const rosHost = useSelector((s) => s.ros.rosHost) || window.location.hostname;
  const containerRef = useRef(null);
  const imgRef = useRef(null);
  const detections = useSelector((s) => s.workshop.detections);
  const detectionLabels = useSelector((s) => s.workshop.detectionLabels);
  const { callService } = useRosServiceCaller();
  const [naturalSize, setNaturalSize] = useState({ w: 0, h: 0 });

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;

    const img = document.createElement('img');
    const topic = CAMERA_TOPICS[camera];
    if (!topic) return undefined;

    const timestamp = Date.now();
    img.src = `http://${rosHost}:8080/stream?quality=70&type=ros_compressed&default_transport=compressed&topic=${topic}&t=${timestamp}`;
    img.alt = topic;
    img.className = 'block w-full h-full object-contain rounded-lg bg-black';
    img.onload = () => {
      setNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
    };
    img.onerror = () => {
      console.error(`CameraFeedOverlay: stream error for ${topic}`);
    };

    container.appendChild(img);
    imgRef.current = img;

    return () => {
      img.src = '';
      if (img.parentNode) img.parentNode.removeChild(img);
      imgRef.current = null;
    };
  }, [camera, rosHost]);

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
        labels={detectionLabels}
        naturalSize={naturalSize}
      />
    </div>
  );
}

function DetectionOverlay({ detections, labels, naturalSize }) {
  if (!detections || detections.length === 0 || naturalSize.w === 0) return null;
  return (
    <svg className="absolute inset-0 pointer-events-none w-full h-full" viewBox={`0 0 ${naturalSize.w} ${naturalSize.h}`} preserveAspectRatio="xMidYMid meet">
      {detections.map((d, idx) => {
        if (!d || d.x === undefined) return null;
        return (
          <g key={idx}>
            <rect
              x={d.x}
              y={d.y}
              width={d.z}   /* width re-used into geometry_msgs/Point.z slot */
              height={1}
              fill="none"
              stroke="#22c55e"
              strokeWidth="3"
            />
            {labels && labels[idx] && (
              <text x={d.x} y={d.y - 4} fill="#22c55e" fontSize="14" fontWeight="bold">
                {labels[idx]}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

export default CameraFeedOverlay;
