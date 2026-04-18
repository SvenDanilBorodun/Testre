// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import React, { useState, useEffect } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import toast, { useToasterStore } from 'react-hot-toast';
import { MdKeyboardDoubleArrowLeft, MdKeyboardDoubleArrowRight, MdTask } from 'react-icons/md';
import ControlPanel from '../components/ControlPanel';
import HeartbeatStatus from '../components/HeartbeatStatus';
import ImageGrid from '../components/ImageGrid';
import InfoPanel from '../components/InfoPanel';
import { addTag } from '../features/tasks/taskSlice';
import { setIsFirstLoadFalse } from '../features/ui/uiSlice';

export default function RecordPage({ isActive = true }) {
  const dispatch = useDispatch();

  const taskInfo = useSelector((state) => state.tasks.taskInfo);
  const taskStatus = useSelector((state) => state.tasks.taskStatus);
  const useMultiTaskMode = useSelector((state) => state.tasks.useMultiTaskMode);
  const multiTaskIndex = useSelector((state) => state.tasks.multiTaskIndex);
  const imageTopicList = useSelector((state) => state.ros.imageTopicList);

  const { toasts } = useToasterStore();
  const TOAST_LIMIT = 3;

  // Auto-collapse the side panel on narrow viewports.
  const getInitialCollapsed = () =>
    typeof window !== 'undefined' && window.innerWidth < 900;
  const [isRightPanelCollapsed, setIsRightPanelCollapsed] = useState(getInitialCollapsed);

  useEffect(() => {
    const onResize = () => {
      // Only auto-collapse when crossing to small; keep user's open choice on wide.
      if (window.innerWidth < 900) setIsRightPanelCollapsed(true);
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const isFirstLoad = useSelector((state) => state.ui.isFirstLoad.record);

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
    dispatch(setIsFirstLoadFalse('record'));
  }, [taskInfo.tags, taskStatus.robotType, dispatch, isFirstLoad]);

  const camCount = imageTopicList?.length || 0;

  return (
    <div
      className="relative h-full w-full flex flex-col overflow-hidden"
      style={{ background: 'var(--dark-bg)', color: 'var(--dark-ink)' }}
    >
      {/* Top glass chrome */}
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

      {/* Content area */}
      <div className="flex-1 flex items-start min-h-0 pt-[56px] pb-2 px-3 gap-3">
        <div className="flex-1 self-stretch min-w-0 relative rounded-[var(--radius-lg)] overflow-hidden">
          <ImageGrid isActive={isActive} />

          {useMultiTaskMode && taskStatus?.currentTaskInstruction && (
            <div className="absolute bottom-3 left-3 right-3 max-w-[560px] pointer-events-none z-20">
              <div className="bg-black/60 backdrop-blur-md border border-white/15 rounded-[var(--radius-lg)] px-4 py-2.5 text-white shadow-pop">
                <div
                  className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-wider mb-0.5"
                  style={{ color: 'var(--accent)' }}
                >
                  <MdTask />
                  Aktuelle Aufgabe
                  {multiTaskIndex !== undefined && (
                    <span className="opacity-80">
                      · {multiTaskIndex + 1} / {taskInfo.taskInstruction.length}
                    </span>
                  )}
                </div>
                <div className="text-[14px] font-semibold leading-snug">
                  {taskStatus.currentTaskInstruction}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Responsive side panel. Sizes to its content (no forced h-full) with
            a viewport-bounded max-height that enables internal scroll. */}
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
          <InfoPanel />
        </div>
      </div>

      {/* Bottom control dock */}
      <div className="shrink-0">
        <ControlPanel />
      </div>
    </div>
  );
}
