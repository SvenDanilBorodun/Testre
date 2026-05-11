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
import { STREAM_QUALITY } from '../constants/streamConfig';

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
      const timestamp = Date.now();
      // Audit F35: STREAM_QUALITY constant shared with CameraFeedOverlay.
      img.src = `http://${rosHost}:8080/stream?quality=${STREAM_QUALITY}&type=ros_compressed&default_transport=compressed&topic=${topic}&t=${timestamp}`;
      img.alt = topic;
      img.className = 'w-full h-full object-cover rounded-3xl bg-gray-100';
      img.onclick = (e) => e.stopPropagation();
      img.onerror = () => {
        if (cancelled) return;
        console.error(`Image stream error for idx ${idx}, topic: ${topic}`);
      };
      if (cancelled || !containerRef.current) return;
      containerRef.current.appendChild(img);
      currentImgRef.current = img;
    };
    run().catch((error) => {
      console.error(`Error creating image stream for idx ${idx}:`, error);
    });
    return () => {
      cancelled = true;
      destroyImage();
    };
  }, [topic, isActive, rosHost, idx, destroyImage]);

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
