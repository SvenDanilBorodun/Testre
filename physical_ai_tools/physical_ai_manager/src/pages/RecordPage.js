// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

import React, { useState, useEffect, Suspense, lazy } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import toast, { useToasterStore } from 'react-hot-toast';
import {
  MdKeyboardDoubleArrowLeft,
  MdKeyboardDoubleArrowRight,
  MdTask,
  MdViewInAr,
  MdClose,
} from 'react-icons/md';
import ControlPanel from '../components/ControlPanel';
import HeartbeatStatus from '../components/HeartbeatStatus';
import ImageGrid from '../components/ImageGrid';
import InfoPanel from '../components/InfoPanel';
import { addTag } from '../features/tasks/taskSlice';
import { setIsFirstLoadFalse, moveToPage } from '../features/ui/uiSlice';
import PageType from '../constants/pageType';

// The 3D follower twin pulls in three.js (~600 KB) + urdf-loader. Load it as a
// LAZY chunk so it stays out of the entry bundle the white-screen CI greps, and
// only mounts while the „3D-Ansicht" panel is open (full WebGL teardown on
// collapse — see UrdfTwin's disposal effect).
const UrdfTwin = lazy(() => import('../components/UrdfTwin'));

const URDF_OPEN_STORAGE_KEY = 'edubotics_urdf_open';

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

  // 3D twin panel. Default COLLAPSED (the camera view stays primary); the
  // student's open/closed choice persists across reloads in localStorage.
  const [isUrdfOpen, setIsUrdfOpen] = useState(() => {
    try {
      return typeof window !== 'undefined' &&
        window.localStorage.getItem(URDF_OPEN_STORAGE_KEY) === '1';
    } catch (_) {
      return false;
    }
  });

  const toggleUrdf = () => {
    setIsUrdfOpen((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(URDF_OPEN_STORAGE_KEY, next ? '1' : '0');
      } catch (_) { /* private mode / quota — ignore */ }
      return next;
    });
  };

  useEffect(() => {
    const onResize = () => {
      // Only auto-collapse when crossing to small; keep user's open choice on wide.
      if (window.innerWidth < 900) setIsRightPanelCollapsed(true);
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const isFirstLoad = useSelector((state) => state.ui.isFirstLoad.record);

  // Leave the page when recording is EXPLICITLY not available on this rig
  // (capabilities.recordable === false — e.g. the rig switched to a
  // follower-only profile mid-session). The sidebar capability filter removes
  // the Aufnahme tab, but a page that is already mounted would keep rendering
  // against a rig that can't record. Null/unknown caps eject nothing —
  // symmetric with the nav filter. (InferencePage deliberately has NO such
  // guard: Inferenz is always visible by invariant.)
  const recordable = useSelector((state) => state.tasks.taskStatus.capabilities?.recordable);
  useEffect(() => {
    if (recordable === false) {
      toast.error('Aufnahme ist auf diesem Roboter nicht verfügbar.');
      dispatch(moveToPage(PageType.HOME));
    }
  }, [recordable, dispatch]);

  useEffect(() => {
    toasts
      .filter((t) => t.visible)
      .filter((_, i) => i >= TOAST_LIMIT)
      .forEach((t) => toast.dismiss(t.id));
  }, [toasts]);

  useEffect(() => {
    // Wait for the robot identity before seeding the default tags AND before
    // consuming first-load — the identity arrives a beat after the page mounts
    // (server idle identity tick). Consuming first-load early would permanently
    // skip the robotType + `edubotics` tag seed for the session (the picker's
    // setIsFirstLoadTrue reset path is gone with the picker).
    if (taskStatus.robotType === '') return;
    if (isFirstLoad && taskInfo.tags.length === 0) {
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
        <button
          onClick={toggleUrdf}
          className={clsx(
            'h-8 px-3 rounded-full border backdrop-blur-md flex items-center gap-1.5 text-[11px] font-mono whitespace-nowrap transition-colors',
            isUrdfOpen
              ? 'bg-white/20 border-white/30 text-white'
              : 'bg-white/[0.08] border-white/15 text-white/80 hover:bg-white/15'
          )}
          title={isUrdfOpen ? '3D-Ansicht schließen' : '3D-Ansicht öffnen'}
          aria-pressed={isUrdfOpen}
        >
          <MdViewInAr size={16} />
          3D-Ansicht
        </button>
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
      <div className="flex-1 flex items-start min-h-0 pt-[56px] pb-2 px-2 sm:px-3 lg:px-4 gap-2 sm:gap-3 lg:gap-4">
        <div className="flex-1 self-stretch min-w-0 relative rounded-[var(--radius-lg)] overflow-hidden">
          <ImageGrid isActive={isActive} />

          {/* 3D follower twin — floating card over the camera view. Mounts only
              while open (lazy chunk + full WebGL teardown on close). Read-only:
              mirrors /joint_states, never drives the arm (Rule §2). */}
          {isUrdfOpen && (
            <div className="absolute top-3 right-3 z-20 w-[min(360px,42vw)] h-[min(300px,40vh)] rounded-[var(--radius-lg)] overflow-hidden border border-white/15 shadow-pop bg-[#1a1d23]">
              <button
                onClick={toggleUrdf}
                className="absolute top-2 right-2 z-20 w-7 h-7 bg-black/50 text-white rounded-full flex items-center justify-center hover:bg-black/70"
                title="3D-Ansicht schließen"
              >
                <MdClose size={16} />
              </button>
              <Suspense
                fallback={
                  <div className="w-full h-full flex items-center justify-center text-[12px] text-white/70">
                    3D-Modell wird geladen …
                  </div>
                }
              >
                <UrdfTwin />
              </Suspense>
            </div>
          )}

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
              : 'w-[min(420px,38vw)] min-w-[300px] max-w-[480px] md:min-w-[320px] lg:min-w-[360px] 2xl:max-w-[520px] opacity-100'
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
