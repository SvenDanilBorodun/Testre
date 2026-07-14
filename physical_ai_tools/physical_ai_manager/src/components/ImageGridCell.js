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

import React, { useCallback, useEffect, useRef } from 'react';
import clsx from 'clsx';
import { MdClose } from 'react-icons/md';
import { useSelector } from 'react-redux';
import ROSLIB from 'roslib';
import { STREAM_QUALITY } from '../constants/streamConfig';
import rosConnectionManager from '../utils/rosConnectionManager';
import { usePiMode, videoStreamBase } from '../utils/piMode';

// H1: during a classroom-Jetson inference session the Jetson's
// web_video_server (:8080) is bound to loopback only and there is NO LAN
// route to it — the only LAN surface is the JWT-proxied rosbridge on :9091.
// So in that case we can't use an <img src="http://<host>:8080/stream">;
// instead we subscribe to the CompressedImage topic over the already-
// swapped rosbridge and feed each base64 JPEG frame into the <img> as a
// data URL. Throttled + queue_length 1 to bound the base64-over-rosbridge
// bandwidth (≈ jpeg_bytes × 1.33 × (1000/throttle) per camera). 100 ms ≈
// 10 fps is plenty for a monitor view and keeps two cameras well under a
// classroom-LAN budget.
const JETSON_VIEW_THROTTLE_MS = 100;

const classImageGridCell = (topic) =>
  clsx(
    'relative',
    'bg-gray-100',
    'rounded-3xl',
    'flex',
    'items-center',
    'justify-center',
    'transition-all',
    'duration-300',
    'w-full',
    {
      'border-2 border-dashed border-gray-300 hover:border-gray-400': !topic,
      'bg-white': topic,
    }
  );

const classImageGridCellButton = clsx(
  'absolute',
  'top-2',
  'right-2',
  'w-8',
  'h-8',
  'bg-black',
  'bg-opacity-50',
  'text-white',
  'rounded-full',
  'flex',
  'items-center',
  'justify-center',
  'hover:bg-opacity-70',
  'z-10'
);

export default function ImageGridCell({
  topic,
  aspect,
  idx,
  onClose,
  onPlusClick,
  isActive = true,
  style = {},
}) {
  const rosHost = useSelector((state) => state.ros.rosHost);
  // H1: when connected to the classroom Jetson the camera feed must ride
  // the JWT-proxied rosbridge (web_video_server :8080 is loopback-only on
  // the Jetson, and rosHost still points at the student's own PC).
  const jetsonConnected = useSelector((state) => state.jetson.status === 'connected');
  const rosbridgeUrl = useSelector((state) => state.ros.rosbridgeUrl);
  // Orange Pi: MJPEG rides the same-origin /video proxy instead of :8080.
  const { piMode } = usePiMode();
  const containerRef = useRef(null);
  const currentImgRef = useRef(null);

  // Completely remove img element from DOM
  const destroyImage = useCallback(() => {
    if (currentImgRef.current) {
      console.log(`Destroying image stream for idx ${idx}`);
      // First set src to empty
      currentImgRef.current.src = '';
      // Remove from DOM completely
      if (currentImgRef.current.parentNode) {
        currentImgRef.current.parentNode.removeChild(currentImgRef.current);
      }
      currentImgRef.current = null;
    }
  }, [idx]);

  // Audit F26: the prior implementation used a non-atomic
  // `isCreatingRef` boolean combined with a 300 ms `await` — two
  // effect re-runs could both pass the guard, append two <img>
  // tags, and only one ref tracked the second one → cleanup leaked
  // the first stream (5-8 Mbps each). Replace with an
  // effect-scoped cancel token: each effect run owns its own
  // `cancelled` flag and its cleanup function flips it before
  // tearing down.
  useEffect(() => {
    if (!topic || !topic.trim() || !isActive) {
      destroyImage();
      return undefined;
    }
    let cancelled = false;
    let subscription = null;
    const run = async () => {
      // Tear down any leftover <img> from a previous run before
      // committing to this effect's stream.
      destroyImage();
      let staggeredDelay = 0;
      if (idx === 0 || idx === 2) {
        // Left and right cells connect after 300ms (center first).
        staggeredDelay = 300;
      }
      if (staggeredDelay > 0) {
        await new Promise((resolve) => setTimeout(resolve, staggeredDelay));
      }
      if (cancelled || !containerRef.current) return;

      const img = document.createElement('img');
      img.alt = topic;
      img.className = 'w-full h-full object-cover rounded-3xl bg-gray-100';
      img.onclick = (e) => e.stopPropagation();

      if (jetsonConnected) {
        // H1 — rosbridge CompressedImage path (see the module comment).
        // The /image/get_available_list service strips the trailing
        // `/compressed`; add it back to subscribe to the actual transport.
        const fullTopic = topic.endsWith('/compressed') ? topic : `${topic}/compressed`;
        let ros;
        try {
          ros = await rosConnectionManager.getConnection(rosbridgeUrl);
        } catch (err) {
          if (!cancelled) {
            console.error(
              `ImageGridCell idx ${idx}: rosbridge not connectable for ${fullTopic}:`,
              err.message
            );
          }
          return;
        }
        if (cancelled || !containerRef.current) return;
        containerRef.current.appendChild(img);
        currentImgRef.current = img;
        subscription = new ROSLIB.Topic({
          ros,
          name: fullTopic,
          messageType: 'sensor_msgs/msg/CompressedImage',
          throttle_rate: JETSON_VIEW_THROTTLE_MS,
          queue_length: 1,
        });
        subscription.subscribe((msg) => {
          if (cancelled || !currentImgRef.current) return;
          // rosbridge encodes CompressedImage.data (uint8[]) as base64.
          currentImgRef.current.src = `data:image/jpeg;base64,${msg.data}`;
        });
      } else {
        const timestamp = Date.now();
        // Audit F35: STREAM_QUALITY constant shared with CameraFeedOverlay.
        // Pi mode rides the same-origin /video nginx proxy (port 80 —
        // school firewalls filter :8080); otherwise direct web_video_server.
        img.src = `${videoStreamBase(rosHost, piMode)}/stream?quality=${STREAM_QUALITY}&type=ros_compressed&default_transport=compressed&topic=${topic}&t=${timestamp}`;
        img.onerror = () => {
          if (cancelled) return;
          console.error(`Image stream error for idx ${idx}, topic: ${topic}`);
        };
        if (cancelled || !containerRef.current) return;
        containerRef.current.appendChild(img);
        currentImgRef.current = img;
      }
    };
    run().catch((error) => {
      console.error(`Error creating image stream for idx ${idx}:`, error);
    });
    return () => {
      cancelled = true;
      if (subscription) {
        try { subscription.unsubscribe(); } catch (_) { /* swallow */ }
        subscription = null;
      }
      destroyImage();
    };
  }, [topic, isActive, rosHost, idx, destroyImage, jetsonConnected, rosbridgeUrl, piMode]);

  // Force cleanup on unmount
  useEffect(() => {
    return () => {
      destroyImage();
    };
  }, [idx, destroyImage]);

  const handleClose = (e) => {
    e.stopPropagation();
    destroyImage();
    onClose(idx);
  };

  return (
    <div
      className={classImageGridCell(topic)}
      onClick={!topic ? () => onPlusClick(idx) : undefined}
      style={{ cursor: !topic ? 'pointer' : 'default', aspectRatio: aspect, ...style }}
    >
      {topic && topic.trim() !== '' && (
        <button className={classImageGridCellButton} onClick={handleClose}>
          <MdClose size={20} />
        </button>
      )}
      <div ref={containerRef} className="w-full h-full flex items-center justify-center">
        {(!topic || !isActive) && <div className="text-6xl text-gray-400 font-light">+</div>}
      </div>
    </div>
  );
}
