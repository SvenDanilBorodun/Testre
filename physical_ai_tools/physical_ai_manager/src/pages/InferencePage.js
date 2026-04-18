// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import React, { useState, useEffect } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import toast, { useToasterStore } from 'react-hot-toast';
import { MdKeyboardDoubleArrowLeft, MdKeyboardDoubleArrowRight } from 'react-icons/md';
import ControlPanel from '../components/ControlPanel';
import HeartbeatStatus from '../components/HeartbeatStatus';
import ImageGrid from '../components/ImageGrid';
import InferencePanel from '../components/InferencePanel';
import { addTag } from '../features/tasks/taskSlice';
import { setIsFirstLoadFalse } from '../features/ui/uiSlice';

export default function InferencePage({ isActive = true }) {
  const dispatch = useDispatch();

  const { toasts } = useToasterStore();
  const TOAST_LIMIT = 3;

  const taskStatus = useSelector((state) => state.tasks.taskStatus);
  const taskInfo = useSelector((state) => state.tasks.taskInfo);
  const imageTopicList = useSelector((state) => state.ros.imageTopicList);

  const getInitialCollapsed = () =>
    typeof window !== 'undefined' && window.innerWidth < 900;
  const [isRightPanelCollapsed, setIsRightPanelCollapsed] = useState(getInitialCollapsed);

  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth < 900) setIsRightPanelCollapsed(true);
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const isFirstLoad = useSelector((state) => state.ui.isFirstLoad.inference);

  useEffect(() => {
    toasts
      .filter((t) => t.visible)
      .filter((_, i) => i >= TOAST_LIMIT)
      .forEach((t) => toast.dismiss(t.id));
  }, [toasts]);

  useEffect(() => {
    if (isFirstLoad && taskStatus.robotType !== '' && taskInfo.tags.length === 0) {
      dispatch(addTag(taskStatus.robotType));
      dispatch(addTag('edubotics'));
    }
    dispatch(setIsFirstLoadFalse('inference'));
  }, [taskInfo.tags, taskStatus.robotType, dispatch, isFirstLoad]);

  const camCount = imageTopicList?.length || 0;

  return (
    <div
      className="relative h-full w-full flex flex-col overflow-hidden"
      style={{ background: 'var(--dark-bg)', color: 'var(--dark-ink)' }}
    >
      <div className="absolute top-3 left-3 right-3 z-30 flex items-center gap-2 flex-wrap">
        <div className="h-8 px-3 rounded-full bg-white/[0.08] border border-white/15 backdrop-blur-md flex items-center gap-2 text-[11px] text-white/80">
          <span className="font-mono uppercase tracking-wider opacity-70">Roboter</span>
          <span className="font-mono px-1.5 py-0.5 rounded bg-white/10 max-w-[160px] truncate">
            {taskStatus?.robotType || '—'}
          </span>
        </div>
        <HeartbeatStatus dark />
        {camCount > 0 && (
          <div className="h-8 px-3 rounded-full bg-white/[0.08] border border-white/15 backdrop-blur-md flex items-center gap-2 text-[11px] text-white/80 font-mono whitespace-nowrap">
            <span
              className="w-1.5 h-1.5 rounded-full"
              style={{ background: 'var(--accent)' }}
            />
            {camCount} {camCount === 1 ? 'Kamera' : 'Kameras'} aktiv
          </div>
        )}
        <div className="flex-1" />
        {isRightPanelCollapsed && (
          <button
            onClick={() => setIsRightPanelCollapsed(false)}
            className="w-10 h-10 bg-white/[0.08] border border-white/15 rounded-full flex items-center justify-center text-white/80 backdrop-blur-md hover:bg-white/15"
            title="Panel öffnen"
          >
            <MdKeyboardDoubleArrowLeft size={22} />
          </button>
        )}
      </div>

      <div className="flex-1 flex items-start min-h-0 pt-[56px] pb-2 px-3 gap-3">
        <div className="flex-1 self-stretch min-w-0 relative rounded-[var(--radius-lg)] overflow-hidden">
          <ImageGrid isActive={isActive} />
        </div>
        <div
          className={clsx(
            'relative transition-all duration-300 ease-in-out',
            isRightPanelCollapsed
              ? 'w-0 opacity-0 pointer-events-none overflow-hidden'
              : 'w-[min(400px,40vw)] min-w-[300px] max-w-[400px] md:min-w-[320px] lg:min-w-[360px] opacity-100'
          )}
          style={{ maxHeight: 'calc(100vh - 220px)' }}
        >
          <button
            onClick={() => setIsRightPanelCollapsed(!isRightPanelCollapsed)}
            className="absolute -left-4 top-2 w-9 h-9 bg-white/95 border border-[var(--line)] rounded-full flex items-center justify-center shadow-pop text-[var(--ink-2)] hover:text-[var(--ink)] z-30 backdrop-blur"
            title="Einklappen"
          >
            <MdKeyboardDoubleArrowRight size={20} />
          </button>
          <InferencePanel />
        </div>
      </div>

      <div className="shrink-0">
        <ControlPanel />
      </div>
    </div>
  );
}
