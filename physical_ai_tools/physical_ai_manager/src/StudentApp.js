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

import React, { useEffect, useRef } from 'react';
import clsx from 'clsx';
import { MdHome, MdVideocam, MdMemory, MdWidgets } from 'react-icons/md';
import { GoGraph } from 'react-icons/go';
import toast from 'react-hot-toast';
import './App.css';
import HomePage from './pages/HomePage';
import RecordPage from './pages/RecordPage';
import InferencePage from './pages/InferencePage';
import TrainingPage from './pages/TrainingPage';
import EditDatasetPage from './pages/EditDatasetPage';
import StartupGate from './components/StartupGate';
import { LogoMark } from './components/EbUI';
import packageJson from '../package.json';
import { useRosTopicSubscription } from './hooks/useRosTopicSubscription';
import rosConnectionManager from './utils/rosConnectionManager';
import { useDispatch, useSelector } from 'react-redux';
import { setRosHost } from './features/ros/rosSlice';
import { moveToPage } from './features/ui/uiSlice';
import PageType from './constants/pageType';
import { supabase } from './lib/supabaseClient';
import {
  setSession,
  setIsLoading,
  setProfile,
  clearSession,
} from './features/auth/authSlice';
import { getMe } from './services/meApi';

// Cloud-only mode: app was opened with ?cloud=1. Skip ROS setup entirely so
// the roslibjs client doesn't spam the console with connection-refused errors
// trying to reach a rosbridge container that wasn't started.
function isCloudOnlyMode() {
  if (typeof window === 'undefined') return false;
  try {
    const params = new URLSearchParams(window.location.search);
    return params.get('cloud') === '1';
  } catch {
    return false;
  }
}

function StudentApp() {
  const dispatch = useDispatch();
  const taskStatus = useSelector((state) => state.tasks.taskStatus);
  const taskInfo = useSelector((state) => state.tasks.taskInfo);
  const trainingTopicReceived = useSelector((state) => state.training.topicReceived);
  const session = useSelector((state) => state.auth.session);
  const role = useSelector((state) => state.auth.role);
  const profileLoaded = useSelector((state) => state.auth.profileLoaded);
  const cloudOnly = isCloudOnlyMode();

  const defaultRosHost = window.location.hostname;
  if (!cloudOnly) {
    dispatch(setRosHost(defaultRosHost));
  }

  const page = useSelector((state) => state.ui.currentPage);
  const robotType = useSelector((state) => state.tasks.taskStatus.robotType);

  const isFirstLoad = useRef(true);

  const rosSubscriptionControls = useRosTopicSubscription();
  if (!cloudOnly) {
    rosConnectionManager.setOnConnected(rosSubscriptionControls.initializeSubscriptions);
  }

  useEffect(() => {
    return () => {
      if (!cloudOnly) {
        console.log('App unmounting, cleaning up global ROS connection');
        rosConnectionManager.disconnect();
      }
    };
  }, [cloudOnly]);

  useEffect(() => {
    const handleBeforeUnload = (e) => {
      if (taskStatus.running) {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [taskStatus.running]);

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      dispatch(setSession(session));
      dispatch(setIsLoading(false));
    });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      dispatch(setSession(session));
    });

    return () => subscription.unsubscribe();
  }, [dispatch]);

  useEffect(() => {
    if (!session?.access_token) return;
    let alive = true;
    getMe(session.access_token)
      .then((me) => {
        if (!alive) return;
        dispatch(setProfile(me));
        if (me.role !== 'student') {
          toast.error(
            'Dieses Konto ist für die Web-App. Bitte nutze die Lehrer-URL.',
            { duration: 6000 }
          );
          supabase.auth.signOut();
          dispatch(clearSession());
        }
      })
      .catch((err) => {
        console.error('getMe failed', err);
      });
    return () => {
      alive = false;
    };
  }, [session?.access_token, dispatch]);

  useEffect(() => {
    if (isFirstLoad.current && page === PageType.HOME && taskStatus.topicReceived) {
      if (taskInfo?.taskType === PageType.RECORD) {
        dispatch(moveToPage(PageType.RECORD));
      } else if (taskInfo?.taskType === PageType.INFERENCE) {
        dispatch(moveToPage(PageType.INFERENCE));
      }
      isFirstLoad.current = false;
    } else if (isFirstLoad.current && page === PageType.HOME && trainingTopicReceived) {
      dispatch(moveToPage(PageType.TRAINING));
      isFirstLoad.current = false;
    }
  }, [page, taskInfo?.taskType, taskStatus.topicReceived, trainingTopicReceived, dispatch]);

  const requireRobotOrRedirect = (targetPage) => {
    if (process.env.REACT_APP_DEBUG === 'true') {
      isFirstLoad.current = false;
      dispatch(moveToPage(targetPage));
      return;
    }
    if (taskStatus && taskStatus.robotType !== '') {
      isFirstLoad.current = false;
      dispatch(moveToPage(targetPage));
      return;
    }
    if (!robotType || robotType.trim() === '') {
      toast.error('Bitte wähle zuerst einen Robotertyp auf der Startseite', {
        duration: 4000,
      });
      return;
    }
    dispatch(moveToPage(targetPage));
  };

  const handleHomePageNavigation = () => {
    isFirstLoad.current = false;
    dispatch(moveToPage(PageType.HOME));
  };

  const handleRecordPageNavigation = () => requireRobotOrRedirect(PageType.RECORD);
  const handleInferencePageNavigation = () => requireRobotOrRedirect(PageType.INFERENCE);
  const handleEditDatasetPageNavigation = () => requireRobotOrRedirect(PageType.EDIT_DATASET);

  const handleTrainingPageNavigation = () => {
    isFirstLoad.current = false;
    dispatch(moveToPage(PageType.TRAINING));
  };

  useEffect(() => {
    return () => {
      const allStreamImgs = document.querySelectorAll('img[src*="/stream"]');
      allStreamImgs.forEach((img) => {
        img.src = '';
        if (img.parentNode) {
          img.parentNode.removeChild(img);
        }
      });
    };
  }, [page]);

  const navItems = [
    { key: PageType.HOME, label: 'Start', Icon: MdHome, onClick: handleHomePageNavigation },
    { key: PageType.RECORD, label: 'Aufnahme', Icon: MdVideocam, onClick: handleRecordPageNavigation },
    { key: PageType.TRAINING, label: 'Training', Icon: GoGraph, onClick: handleTrainingPageNavigation },
    { key: PageType.INFERENCE, label: 'Inferenz', Icon: MdMemory, onClick: handleInferencePageNavigation },
    { key: PageType.EDIT_DATASET, label: 'Daten', Icon: MdWidgets, onClick: handleEditDatasetPageNavigation, sep: true },
  ];

  const isDarkPage = page === PageType.RECORD || page === PageType.INFERENCE;

  const blockRoleMismatch = profileLoaded && role && role !== 'student';

  return (
    <StartupGate>
      <div
        className={clsx('flex min-h-screen w-screen', isDarkPage && 'dark-surface')}
        style={isDarkPage ? { background: 'var(--dark-bg)' } : {}}
      >
        <aside
          className={clsx(
            'w-[88px] shrink-0 flex flex-col items-center py-5 gap-1',
            isDarkPage
              ? 'border-r border-[color:var(--dark-line)]'
              : 'bg-white border-r border-[var(--line)]'
          )}
        >
          <div className="mb-4">
            <LogoMark size={22} />
          </div>
          {navItems.map((n) => {
            const Icon = n.Icon;
            const active = page === n.key;
            return (
              <React.Fragment key={n.key}>
                {n.sep && (
                  <div
                    className={clsx(
                      'w-8 h-px my-2',
                      isDarkPage ? 'bg-[color:var(--dark-line)]' : 'bg-[var(--line)]'
                    )}
                  />
                )}
                <button
                  onClick={n.onClick}
                  className={clsx(
                    'group w-[68px] py-3 rounded-[var(--radius)] flex flex-col items-center gap-1.5 transition',
                    active
                      ? isDarkPage
                        ? 'bg-white/[0.08] text-white'
                        : 'bg-[var(--accent-wash)] text-[var(--accent-ink)]'
                      : isDarkPage
                      ? 'text-white/60 hover:bg-white/[0.05]'
                      : 'text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]'
                  )}
                >
                  <span
                    className={clsx(
                      'w-10 h-10 flex items-center justify-center rounded-[10px]',
                      active && (isDarkPage ? 'bg-white/10' : 'bg-white/60')
                    )}
                  >
                    <Icon size={22} />
                  </span>
                  <span className="text-[11px] font-medium">{n.label}</span>
                </button>
              </React.Fragment>
            );
          })}
          <div className="flex-1" />
          <div
            className={clsx(
              'text-[10px] font-mono',
              isDarkPage ? 'text-white/40' : 'text-[var(--ink-4)]'
            )}
          >
            v{packageJson.version}
          </div>
        </aside>
        <main className="flex-1 flex flex-col h-screen min-w-0 relative overflow-hidden">
          {blockRoleMismatch ? (
            <div className="flex flex-col items-center justify-center h-full p-10 text-center">
              <h2 className="text-xl font-bold text-[var(--ink)] mb-2">Falsches Konto</h2>
              <p className="text-[var(--ink-3)] max-w-md">
                Dieses Konto ist für die Web-App gedacht. Bitte melde dich mit einem
                Schüler-Konto auf diesem Gerät an.
              </p>
            </div>
          ) : page === PageType.HOME ? (
            <HomePage />
          ) : page === PageType.RECORD ? (
            <RecordPage isActive={page === PageType.RECORD} />
          ) : page === PageType.INFERENCE ? (
            <InferencePage isActive={page === PageType.INFERENCE} />
          ) : page === PageType.TRAINING ? (
            <TrainingPage isActive={page === PageType.TRAINING} />
          ) : page === PageType.EDIT_DATASET ? (
            <EditDatasetPage isActive={page === PageType.EDIT_DATASET} />
          ) : (
            <HomePage />
          )}
        </main>
      </div>
    </StartupGate>
  );
}

export default StudentApp;
