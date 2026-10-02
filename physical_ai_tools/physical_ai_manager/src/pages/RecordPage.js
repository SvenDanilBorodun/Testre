// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Aufnahme page (Aufnahme 2.0, spec §3.12): a light page in the Startseite
// language around a dark stage. This file only COMPOSES: every decision (which
// view, which buttons, which problem, what a key or a button sends) is made by
// components/Record/useRecordController and the pure models under
// components/Record/model; every piece drawn is a presentational component
// under components/Record. What stays here is page furniture: the view switch
// („Kameras" | „3D" | „Beides", a MACHINE storage key), the 3D presets (not
// persisted), the toast cap, and the lazy 3D twin.
//
//   <div class="rec-page">                    container: rec (inline-size)
//     <RecordHeader/>
//     <div class="rec-grid">                  minmax(0,1fr) 360px
//       <section class="rec-left">
//         <RecordStage>   Phasenleiste + view switch · tiles · overlay layers
//         <div class="rec-footer">            one group, sticky on a narrow page
//           <ProblemBanner/>                  only while there is a problem
//           <ActionBar/>                      a flow sibling, never absolute
//       <aside class="rec-right"> <TaskCard/> <SessionCard/>
//
// On a narrow window (≤ 1150 px of page, e.g. 1093 px at 125 % scaling)
// record.css turns this into one scrolling column with a sticky footer (banner
// + action bar together, so the banner can never slide under the bar).

import React, { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react';
import { shallowEqual, useSelector } from 'react-redux';
import toast, { useToasterStore } from 'react-hot-toast';

import HeartbeatStatus from '../components/HeartbeatStatus';
import ActionBar from '../components/Record/ActionBar';
import CameraTiles from '../components/Record/CameraTiles';
import EpisodeDots from '../components/Record/EpisodeDots';
import FinishCard from '../components/Record/FinishCard';
import PhaseOverlay, { overlayText } from '../components/Record/PhaseOverlay';
import PhaseTrack from '../components/Record/PhaseTrack';
import ProblemBanner from '../components/Record/ProblemBanner';
import RecordHeader from '../components/Record/RecordHeader';
import RecordStage from '../components/Record/RecordStage';
import RecordingFrame from '../components/Record/RecordingFrame';
import SavingChips from '../components/Record/SavingChips';
import SessionCard from '../components/Record/SessionCard';
import StageCard from '../components/Record/StageCard';
import StageViewSwitch from '../components/Record/StageViewSwitch';
import TaskCard from '../components/Record/TaskCard';
import useRecordController from '../components/Record/useRecordController';
import { VIEW } from '../components/Record/model/phaseModel';
import { selectRecordStatus } from '../features/tasks/recordSelectors';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import { usePiMode } from '../utils/piMode';
import { TASK_NAME_MAX } from '../utils/recordTaskInfo';
import { cameraRoleLabel, expectedCameraRoles, robotDisplayName } from '../utils/robotIdentity';
import '../components/Record/record.css';

// The 3D follower twin pulls in three.js (~600 KB) + urdf-loader. It stays a
// LAZY chunk (out of the entry bundle the white-screen CI greps) and mounts
// only while „3D" or „Beides" is chosen and the robot is connected.
const UrdfTwin = lazy(() => import('../components/UrdfTwin'));

// Which tiles the stage shows. A view of the RIG, like the Roboter-Studio dock
// keys: MACHINE-scoped in utils/sessionScope.js.
export const RECORD_VIEW_KEY = 'edubotics_record_view';
const RECORD_VIEWS = ['cams', '3d', 'both'];
const PRESETS = ['persp', 'front', 'side', 'top'];
const TOAST_LIMIT = 3;

function readRecordView() {
  try {
    const v = window.localStorage.getItem(RECORD_VIEW_KEY);
    return RECORD_VIEWS.includes(v) ? v : 'cams';
  } catch {
    return 'cams';
  }
}

function writeRecordView(v) {
  try {
    window.localStorage.setItem(RECORD_VIEW_KEY, v);
  } catch {
    /* private mode / quota: the choice just is not remembered */
  }
}

const OVERLAY_PHASE = { [VIEW.WARMUP]: 'warmup', [VIEW.RESETTING]: 'reset' };
// The views in which the student is looking at the arm, not at the form.
const STAGE_VIEWS = new Set([VIEW.STARTING, VIEW.WARMUP, VIEW.RECORDING, VIEW.COLLISION, VIEW.FINISHING]);

function RecordPage({ isActive = true }) {
  const c = useRecordController({ isActive });
  const copy = c.copy;
  const { view, model } = c;
  const { piMode } = usePiMode();
  const { getImageTopicList } = useRosServiceCaller();
  const status = useSelector(selectRecordStatus, shallowEqual);
  const heartbeat = useSelector((s) => s.tasks.heartbeatStatus);
  const connected = heartbeat === 'connected';

  // Keep at most TOAST_LIMIT toasts on screen (the page's own notices ride the
  // banner, but other surfaces still toast).
  const { toasts } = useToasterStore();
  useEffect(() => {
    toasts
      .filter((t) => t.visible)
      .filter((_, i) => i >= TOAST_LIMIT)
      .forEach((t) => toast.dismiss(t.id));
  }, [toasts]);

  // On a narrow window the page scrolls, and the task card sits BELOW the
  // stage: a student who typed the task name and pressed Start (the action bar
  // sticks to the bottom) would record with the stage scrolled out of sight.
  // Entering a session brings the page back to the top. (On a wide window
  // .rec-main does not scroll, so this does nothing.)
  const mainRef = useRef(null);
  const wasStageViewRef = useRef(STAGE_VIEWS.has(view));
  useEffect(() => {
    const isStage = STAGE_VIEWS.has(view);
    const was = wasStageViewRef.current;
    wasStageViewRef.current = isStage;
    const main = mainRef.current;
    if (!isStage || was || !main || typeof main.scrollTo !== 'function' || main.scrollTop === 0) return;
    main.scrollTo({ top: 0, behavior: c.reducedMotion ? 'auto' : 'smooth' });
  }, [view, c.reducedMotion]);

  const [recordView, setRecordView] = useState(readRecordView);
  const chooseView = (v) => {
    setRecordView(v);
    writeRecordView(v);
  };
  const [preset, setPreset] = useState('persp');

  const caps = status.capabilities;
  const roles = useMemo(() => expectedCameraRoles(caps), [caps]);
  const secondsLeft = c.clock.secondsLeft;
  const recording = view === VIEW.RECORDING;
  const hot = secondsLeft >= 1 && secondsLeft <= 3;

  // --- stage layers -----------------------------------------------------------
  const overlayPhase = OVERLAY_PHASE[view] || null;
  const episodeTime = status.running ? status.episodeTime : c.form.episodeTime;
  const overlay = overlayPhase
    ? overlayText(overlayPhase, {
      saved: model.episode.saved,
      total: model.episode.total,
      episodeTime: Number(episodeTime) || 0,
    }, copy.overlay)
    : null;

  const rawSession = c.session.raw;
  const savedRows = (rawSession?.episodes || []).filter((e) => e.outcome === 'saved');
  const lastSaved = savedRows.length ? savedRows[savedRows.length - 1] : null;
  const savedKey = lastSaved ? `${rawSession.id}:${savedRows.length}` : null;

  let stageCard = null;
  if (view === VIEW.OFFLINE) {
    stageCard = { kind: 'offline', title: copy.card.offlineTitle, body: piMode ? copy.card.offlinePi : copy.card.offlineWindows };
  } else if (view === VIEW.CONNECTING) {
    stageCard = { kind: 'connecting', title: copy.card.connecting };
  } else if (view === VIEW.INFERENCE_BUSY) {
    stageCard = { kind: 'inference', title: copy.card.inference };
  } else if (view === VIEW.COLLISION && rawSession?.active) {
    // Behind the CollisionModal (unchanged, spec §3.16): what happened, not what to do.
    stageCard = { kind: 'collision', title: copy.card.collisionTitle, body: copy.card.collisionBody(model.episode.current) };
  }
  const showFinish = view === VIEW.FINISHING && c.finish.visible;
  const onFinishAction = (id) => {
    if (id === 'toTraining') c.goToTraining();
    else c.dismissFinish();
  };

  const twinShown = (recordView === '3d' || recordView === 'both') && connected;
  const camsShown = recordView !== '3d';

  const tiles = (
    <>
      {camsShown ? (
        <CameraTiles
          connected={connected}
          fetchTopics={getImageTopicList}
          roles={roles}
          labelFor={cameraRoleLabel}
          signal={c.signal}
          fallbackText={copy.tiles.noStream}
          isActive={isActive}
        />
      ) : null}
      {recordView !== 'cams' ? (
        <div className="rec-tile rec-t3d" data-testid="rec-twin-tile">
          {twinShown ? (
            <div className="rec-tile-fill">
              <Suspense fallback={<div className="rec-tile-fallback">{copy.tiles.twinLoading}</div>}>
                <UrdfTwin viewPreset={preset} showChrome={false} />
              </Suspense>
            </div>
          ) : null}
          <div className="rec-tl">
            <span>{copy.tiles.tile3d}</span>
          </div>
          <div className="rec-presets">
            <StageViewSwitch
              groupLabel={copy.preset.group}
              value={preset}
              onChange={setPreset}
              options={PRESETS.map((p) => ({ value: p, label: copy.preset[p] }))}
            />
          </div>
        </div>
      ) : null}
    </>
  );

  const bar = (
    <>
      <PhaseTrack
        ariaLabel={copy.track.aria}
        segments={model.segments}
        idleText={model.idleText}
        remaining={model.trackRemaining}
        subscribeFrame={c.clock.subscribe}
      />
      <StageViewSwitch
        groupLabel={copy.view.group}
        value={recordView}
        onChange={chooseView}
        options={RECORD_VIEWS.map((v) => ({ value: v, label: copy.view[v] }))}
      />
    </>
  );

  const title = String(c.form.taskName || '').trim() || copy.header.fallbackTitle;

  return (
    <div className={c.reducedMotion ? 'rec-page rec-reduced' : 'rec-page'} data-testid="rec-page" data-view={view}>
      <div className="rec-main" ref={mainRef}>
        <RecordHeader
          eyebrow={copy.header.eyebrow}
          title={title}
          connection={<HeartbeatStatus />}
          robotName={robotDisplayName(caps, status.robotProfile, status.robotType)}
        />
        <div className="rec-grid">
          <section className="rec-left" aria-label={copy.header.eyebrow}>
            <RecordStage
              color={model.phaseColor}
              recording={recording}
              hot={recording && hot}
              view={recordView}
              oneCamera={camsShown && Array.isArray(roles) && roles.length === 1}
              bar={bar}
              tiles={tiles}
            >
              <PhaseOverlay
                phase={overlayPhase}
                text={overlay}
                totalS={Number(status.totalTime) || 0}
                secondsLeft={secondsLeft}
                subscribeFrame={c.clock.subscribe}
                recording={recording}
                losText={copy.overlay.los}
                reducedMotion={c.reducedMotion}
              />
              <RecordingFrame
                active={recording}
                hot={hot}
                subscribeFrame={c.clock.subscribe}
                text={{
                  badge: copy.rec.badge(c.clock.elapsed),
                  remaining: `${secondsLeft} s`,
                  until: copy.rec.until,
                }}
              />
              <SavingChips
                saving={view === VIEW.SAVING}
                savingText={copy.chips.saving(model.episode.current)}
                savedKey={savedKey}
                savedText={lastSaved ? copy.chips.saved(lastSaved.n) : ''}
              />
              {stageCard ? <StageCard {...stageCard} /> : null}
              {showFinish ? <FinishCard {...c.finish} onAction={onFinishAction} /> : null}
            </RecordStage>
            <div className="rec-footer" data-testid="rec-footer">
              <ProblemBanner problem={c.problem} homeLabel={copy.problem.homeLabel} onGoHome={c.goToHome} />
              <ActionBar
                ariaLabel={copy.actionbar.aria}
                pill={model.pill}
                dotCount={model.dots.items.length}
                dots={(
                  <EpisodeDots
                    label={model.dots.label}
                    dots={model.dots.items}
                    more={model.dots.more}
                    color={model.dots.color}
                  />
                )}
                buttons={model.buttons}
                question={c.question}
                onAction={c.act}
                onAnswer={c.act}
                mute={{
                  muted: c.muted,
                  onToggle: c.toggleMute,
                  labelOff: copy.mute.labelOff,
                  labelOn: copy.mute.labelOn,
                }}
              />
            </div>
          </section>
          <aside className="rec-right">
            <TaskCard
              labels={copy.task}
              form={c.form}
              onChange={c.setField}
              editable={c.editable}
              lockedReason={c.lockedReason}
              steppers={c.steppers}
              estimate={c.estimate}
              saveName={c.saveName}
              hfUsers={c.hfUsers}
              invalid={c.invalid}
              taskNameMax={TASK_NAME_MAX}
            />
            <SessionCard
              title={c.session.title}
              chip={c.session.chip}
              rows={c.session.rows}
              emptyText={c.session.emptyText}
              note={c.session.note}
              sum={c.session.sum}
            />
          </aside>
        </div>
      </div>
    </div>
  );
}

// Memoised: StudentApp re-renders with every store change it reads; the page
// has its own subscriptions and needs no re-render from its parent (V2-2).
export default React.memo(RecordPage);
