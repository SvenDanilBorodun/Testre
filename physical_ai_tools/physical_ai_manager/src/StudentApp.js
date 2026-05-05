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
import { MdHome, MdVideocam, MdMemory, MdWidgets, MdConstruction } from 'react-icons/md';
import { GoGraph } from 'react-icons/go';
import toast from 'react-hot-toast';
import './App.css';
import HomePage from './pages/HomePage';
import RecordPage from './pages/RecordPage';
import InferencePage from './pages/InferencePage';
import TrainingPage from './pages/TrainingPage';
import EditDatasetPage from './pages/EditDatasetPage';
import WorkshopPage from './pages/WorkshopPage';
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
import { isCloudOnlyMode } from './utils/cloudMode';

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
        if (!alive) return;
        console.error('getMe failed', err);
        // A 401 means the session token is dead (expired, revoked, user
        // deleted server-side). Previously the error was swallowed and the
        // app ended up with a session object but no profile — a blank
        // state with no actionable UI. Sign out so the login form shows.
        const status = err?.status ?? err?.response?.status;
        if (status === 401 || status === 403) {
          toast.error('Sitzung abgelaufen — bitte erneut anmelden.');
          supabase.auth.signOut();
          dispatch(clearSession());
        } else {
          // Network / 5xx — don't sign out (session may still be valid);
          // just surface it so the user knows why nothing is loading.
          toast.error('Server nicht erreichbar — bitte Verbindung prüfen.');
        }
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

  const handleWorkshopPageNavigation = () => requireRobotOrRedirect(PageType.WORKSHOP);

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
    { key: PageType.RECORD, label: 'Aufnahme', Icon: MdVideocam, onClick: handleRecordPageNavigation, hardwareOnly: true },
    { key: PageType.TRAINING, label: 'Training', Icon: GoGraph, onClick: handleTrainingPageNavigation },
    { key: PageType.INFERENCE, label: 'Inferenz', Icon: MdMemory, onClick: handleInferencePageNavigation, hardwareOnly: true },
    { key: PageType.EDIT_DATASET, label: 'Daten', Icon: MdWidgets, onClick: handleEditDatasetPageNavigation, sep: true },
    { key: PageType.WORKSHOP, label: 'Roboter Studio', Icon: MdConstruction, onClick: handleWorkshopPageNavigation, hardwareOnly: true },
  ].filter((n) => !cloudOnly || !n.hardwareOnly);

  const isDarkPage = page === PageType.RECORD || page === PageType.INFERENCE;

  const blockRoleMismatch = profileLoaded && role && role !== 'student';

  return (
    <StartupGate>
      <div
        className={clsx(
          'flex h-screen w-screen overflow-hidden',
          'flex-col sm:flex-row',
          isDarkPage && 'dark-surface'
        )}
        style={isDarkPage ? { background: 'var(--dark-bg)' } : {}}
      >
        {/* Desktop / tablet rail */}
        <aside
          className={clsx(
            'hidden sm:flex shrink-0 flex-col items-center py-4 md:py-5 gap-1',
            'w-[64px] md:w-[88px]',
            isDarkPage
              ? 'border-r border-[color:var(--dark-line)]'
              : 'bg-white border-r border-[var(--line)]'
          )}
        >
          <div className="mb-3 md:mb-4">
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
                  title={n.label}
                  className={clsx(
                    'group w-12 md:w-[68px] py-2.5 md:py-3 rounded-[var(--radius)] flex flex-col items-center gap-1 md:gap-1.5 transition',
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
                      'w-9 h-9 md:w-10 md:h-10 flex items-center justify-center rounded-[10px]',
                      active && (isDarkPage ? 'bg-white/10' : 'bg-white/60')
                    )}
                  >
                    <Icon size={20} />
                  </span>
                  <span className="hidden md:block text-[11px] font-medium">{n.label}</span>
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

        {/* Mobile top bar */}
        <header
          className={clsx(
            'sm:hidden shrink-0 h-12 px-3 flex items-center justify-between border-b',
            isDarkPage
              ? 'border-[color:var(--dark-line)] bg-black/30 backdrop-blur'
              : 'bg-white border-[var(--line)]'
          )}
        >
          <div className="flex items-center gap-2">
            <LogoMark size={20} />
            <span
              className={clsx(
                'text-sm font-semibold tracking-tight',
                isDarkPage ? 'text-white' : 'text-[var(--ink)]'
              )}
            >
              EduBotics
            </span>
          </div>
          <div
            className={clsx(
              'text-[10px] font-mono',
              isDarkPage ? 'text-white/40' : 'text-[var(--ink-4)]'
            )}
          >
            v{packageJson.version}
          </div>
        </header>

        <main className="flex-1 flex flex-col min-h-0 min-w-0 relative overflow-hidden">
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
          ) : page === PageType.WORKSHOP ? (
            <WorkshopPage isActive={page === PageType.WORKSHOP} />
          ) : (
            <HomePage />
          )}
        </main>

        {/* Mobile bottom nav */}
        <nav
          className={clsx(
            'sm:hidden shrink-0 h-14 border-t flex items-stretch',
            isDarkPage
              ? 'bg-black/60 backdrop-blur border-[color:var(--dark-line)]'
              : 'bg-white border-[var(--line)]'
          )}
        >
          {navItems.map((n) => {
            const Icon = n.Icon;
            const active = page === n.key;
            return (
              <button
                key={n.key}
                onClick={n.onClick}
                className={clsx(
                  'flex-1 min-w-0 flex flex-col items-center justify-center gap-0.5 transition',
                  active
                    ? isDarkPage
                      ? 'text-white'
                      : 'text-[var(--accent-ink)]'
                    : isDarkPage
                    ? 'text-white/60'
                    : 'text-[var(--ink-3)]'
                )}
              >
                <Icon size={20} />
                <span className="text-[10px] font-medium truncate px-1">{n.label}</span>
              </button>
            );
          })}
        </nav>
      </div>
    </StartupGate>
  );
}

export default StudentApp;
