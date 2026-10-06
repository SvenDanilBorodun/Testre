// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Episoden ansehen" (the approved mockup's player, spec §F): the dataset's
// summary and episode list, the dark stage with every camera and the 3D twin
// on ONE frame-accurate clock, the transport and timeline, the hints, the
// details, the joint charts and the keys. Marks belong to the version of the
// dataset they were made on (R-9): a summary with another `meta_digest` drops
// them and says so once.
//
// The render invariant (§F2, §J.B): this view re-renders on an episode switch,
// play/pause, speed, marks and its own controls — never per video frame; the
// clock moves the DOM through refs (Transport, Scrubber, JointCharts) and the
// twin reads its pose from `poseSource`.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useDispatch, useSelector, useStore } from 'react-redux';
import Icon from '../icons/Icon';
import SyncBadge from './SyncBadge';
import ToolsBar from './ToolsBar';
import SyncBanners from './SyncBanners';
import EpisodeList from './EpisodeList';
import Stage from './Stage';
import Transport from './Transport';
import HintsPanel from './HintsPanel';
import DetailsPanel from './DetailsPanel';
import JointCharts from './JointCharts';
import KeysLegend from './KeysLegend';
import { LEADER_COLOR } from './theme';
import { toastWarn } from './datenToasts';
import { releasePointerFocus } from '../Record/ActionBar';
import COPY from '../../features/editDataset/datenCopy';
import { clearMarks, selectMarks, toggleMark } from '../../features/editDataset/editDatasetSlice';
import { countRender } from '../../features/editDataset/renderProbe';
import useEpisodePlayer from '../../features/editDataset/hooks/useEpisodePlayer';
import { driverCamera, orderCameras } from '../../features/editDataset/model/labels';
import { frameAt, lastFrame, shiftTarget, stepTarget } from '../../features/editDataset/model/playerClock';
import { fill } from '../../features/editDataset/model/format';
import { hubLink, trainingBlock } from '../../features/editDataset/model/cardModel';

const EMPTY = Object.freeze([]);

/** Should a window keydown reach the player (spec §F4)? */
export function playerKeyAllowed(e, blocked) {
  if (blocked) return false;
  if (e.altKey || e.ctrlKey || e.metaKey) return false;
  const t = e.target;
  if (t && t.closest) {
    if (t.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]')) return false;
    if (t.closest('[role="dialog"], [role="menu"]')) return false;
  }
  return true;
}

function PlayerView({
  card, hubFacts = null, model, api, connected, hfOff, keysBlocked, newerAcked, actions,
}) {
  countRender('PlayerView');
  const dispatch = useDispatch();
  const store = useStore();
  const id = card.id;
  const entry = card.local;
  const digest = entry ? entry.meta_digest : '';

  // ---- the summary (reloaded whenever the dataset's version changes) -------
  const [summary, setSummary] = useState({ status: 'loading', data: null, digest: null });
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setSummary((s) => (s.digest === digest && s.status === 'ready' ? s : { status: 'loading', data: null, digest }));
    api.fetchSummary(id).then((data) => {
      if (!cancelled) setSummary({ status: 'ready', data, digest });
    }).catch(() => {
      if (!cancelled) setSummary({ status: 'error', data: null, digest });
    });
    return () => { cancelled = true; };
  }, [api, id, digest, reload]);
  const sum = summary.data;
  const sumDigest = sum ? sum.meta_digest : null;

  // R-9: marks made on another version of the dataset are dropped, once.
  useEffect(() => {
    if (!sumDigest) return;
    const stored = store.getState().editDataset?.marks?.[id];
    if (stored && stored.digest !== sumDigest && stored.indices.length) {
      dispatch(clearMarks(id));
      toastWarn(COPY.marks.cleared);
    }
  }, [sumDigest, id, store, dispatch]);
  const marks = useSelector(selectMarks(id, sumDigest)) || EMPTY;

  // ---- the open episode ----------------------------------------------------
  const episodes = (sum && sum.episodes) || EMPTY;
  const [ep, setEp] = useState(0);
  useEffect(() => { setEp((e) => Math.min(e, Math.max(0, episodes.length - 1))); }, [episodes.length]);
  const epInfo = episodes[ep] || null;
  const fps = Number((sum && sum.fps) || (entry && entry.fps) || 30);
  const length = epInfo ? Math.max(1, Number(epInfo.length) || 1) : 1;
  const durationS = epInfo ? Number(epInfo.duration_s) || length / fps : 0;
  const episodeKey = `${sumDigest}:${ep}`;
  const { engine, playing, speed, setSpeed, videoRef } = useEpisodePlayer({ fps, length, episodeKey });

  const [view, setView] = useState('both');
  const [preset, setPreset] = useState('persp');
  const [onlyHints, setOnlyHints] = useState(false);

  // ---- the clip token ------------------------------------------------------
  const [token, setToken] = useState(() => api.peekDsToken(id));
  useEffect(() => {
    let cancelled = false;
    api.dsToken(id).then((t) => { if (!cancelled && t) setToken((cur) => cur || t); });
    return () => { cancelled = true; };
  }, [api, id]);
  const onTokenError = useCallback(() => {
    api.dsToken(id, true).then((t) => { if (t) setToken(t); });
  }, [api, id]);

  // ---- the episode's state/action (charts and the 3D twin) ----------------
  const [epData, setEpData] = useState(null);
  const dataRef = useRef({ data: null, key: '' });
  useEffect(() => {
    if (!sumDigest || !epInfo) return undefined;
    let cancelled = false;
    setEpData(null);
    dataRef.current = { data: null, key: '' };
    api.fetchEpisodeData(id, ep).then((data) => {
      if (cancelled) return;
      dataRef.current = { data, key: episodeKey };
      setEpData(data);
    }).catch(() => { /* the charts stay empty; the videos still play */ });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, id, ep, sumDigest]);

  const poseSource = useCallback(() => {
    const { data, key } = dataRef.current;
    if (!data || !Array.isArray(data.state) || !data.state.length) return { version: 'none', pose: null, ghost: null };
    const i = Math.min(engine.getIdx(), data.state.length - 1);
    const names = data.names || {};
    return {
      version: `${key}:${i}`,
      pose: { names: names.state || [], positions: data.state[i] || [] },
      ghost: Array.isArray(data.action) && data.action[i]
        ? { names: names.action || [], positions: data.action[i], color: LEADER_COLOR }
        : null,
    };
  }, [engine]);

  // ---- cameras ---------------------------------------------------------------
  const cameras = useMemo(() => orderCameras(sum && sum.cameras), [sum]);
  const driver = useMemo(() => driverCamera(sum && sum.cameras), [sum]);
  const jointNames = (sum && sum.joints && sum.joints.state) || EMPTY;

  // ---- actions -----------------------------------------------------------------
  const selectEp = useCallback((i) => {
    setEp(() => Math.max(0, Math.min(episodes.length - 1, i)));
  }, [episodes.length]);
  const markToggle = useCallback((i) => {
    if (!sumDigest) return;
    actions.guardEdit(card, () => dispatch(toggleMark({ id, digest: sumDigest, index: i })));
  }, [actions, card, dispatch, id, sumDigest]);
  const hintIndices = useMemo(
    () => episodes.map((e, i) => ((e.hints || []).length ? i : -1)).filter((i) => i >= 0),
    [episodes],
  );
  const onTool = useCallback((what) => {
    switch (what) {
      case 'delete_tool':
      case 'split_tool':
        if (!sum) return;
        actions.guardEdit(card, () => actions.openTool(what === 'split_tool' ? 'split' : 'delete', {
          card, summary: sum, marks, hintIndices,
        }));
        return;
      case 'merge_with':
        actions.startMerge(id);
        return;
      default:
        actions.cardAction(what, card);
    }
  }, [actions, card, sum, marks, hintIndices, id]);
  const onBanner = useCallback((what) => actions.cardAction(what, card), [actions, card]);
  const onDeleteMarked = useCallback(() => {
    if (!sum || !marks.length) return;
    actions.guardEdit(card, () => actions.confirmDeleteMarked({ card, summary: sum, marks }));
  }, [actions, card, sum, marks]);

  const onToggleCurrentMark = useCallback(() => markToggle(ep), [markToggle, ep]);
  const onPrevEpisode = useCallback(() => selectEp(ep - 1), [selectEp, ep]);
  const onNextEpisode = useCallback(() => selectEp(ep + 1), [selectEp, ep]);
  const onToggleOnlyHints = useCallback(() => setOnlyHints((v) => !v), []);
  const onClearMarks = useCallback(() => dispatch(clearMarks(id)), [dispatch, id]);
  const onSeekSeconds = useCallback((t) => engine.seekFrame(frameAt(t, fps, length)), [engine, fps, length]);

  // ---- keys (spec §F4) -------------------------------------------------------
  const keyState = useRef({});
  keyState.current = {
    keysBlocked, ep, episodes: episodes.length, fps, length, markToggle, selectEp,
  };
  useEffect(() => {
    const onKey = (e) => {
      const k = keyState.current;
      if (!playerKeyAllowed(e, k.keysBlocked)) return;
      let handled = true;
      switch (e.code) {
        case 'Space': engine.toggle(); break;
        case 'ArrowLeft':
          engine.seekFrame(e.shiftKey ? shiftTarget(engine.getIdx(), -5, k.fps, k.length) : stepTarget(engine.getIdx(), -1, k.length));
          break;
        case 'ArrowRight':
          engine.seekFrame(e.shiftKey ? shiftTarget(engine.getIdx(), 5, k.fps, k.length) : stepTarget(engine.getIdx(), 1, k.length));
          break;
        case 'ArrowUp': k.selectEp(k.ep - 1); break;
        case 'ArrowDown': k.selectEp(k.ep + 1); break;
        case 'Home': engine.seekFrame(0); break;
        case 'End': engine.seekFrame(lastFrame(k.length)); break;
        case 'Delete':
        case 'Backspace': k.markToggle(k.ep); break;
        default: handled = false;
      }
      if (handled) e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [engine]);

  const syncState = card.sync ? card.sync.state : 'unknown';
  const link = hubLink(card);
  const ownerName = model.ownerName;

  return (
    <div className="dat-main" data-view="player">
      <div className="dat-pl-head">
        <button type="button" className="dat-btn dat-btn-ghost" onClick={(e) => { releasePointerFocus(e); actions.back(); }}>
          <Icon name="back" size={16} />
          {COPY.player.back}
        </button>
        <div className="dat-pl-title">
          <div className="dat-eyebrow">{COPY.player.eyebrow}</div>
          <h1 className="dat-title">{model.title}</h1>
          <div className="dat-pl-meta">
            <span className="dat-mono">{id}</span>
            {model.badge ? (
              <SyncBadge state={model.badge.state} reason={model.badge.reason} overlay={model.badge.overlay} ownerName={model.badge.ownerName} />
            ) : null}
            {ownerName ? <span className="dat-owner"><Icon name="users" size={13} />{fill(COPY.player.groupOwner, { name: ownerName })}</span> : null}
          </div>
        </div>
      </div>

      <ToolsBar
        markCount={marks.length}
        own={model.own}
        ownerName={ownerName}
        syncState={syncState}
        trainingBlock={trainingBlock(card)}
        hfOff={hfOff}
        onAction={onTool}
      />
      <SyncBanners
        syncState={syncState}
        hub={hubFacts}
        newerAcked={newerAcked}
        own={model.own}
        partnerNote={model.own ? null : fill(COPY.card.partnerNote, { name: ownerName })}
        hfOff={hfOff}
        onAction={onBanner}
      />

      {summary.status === 'error' ? (
        <div className="dat-state">
          <span>{COPY.http.generic}</span>
          <button type="button" className="dat-btn dat-btn-sm" onClick={(e) => { releasePointerFocus(e); setReload((r) => r + 1); }}>
            <Icon name="refresh" size={14} />
            {COPY.player.retry}
          </button>
        </div>
      ) : null}
      {summary.status === 'loading' && !sum ? (
        <div className="dat-state"><Icon name="loading" className="animate-spin" />{COPY.player.loading}</div>
      ) : null}

      {sum ? (
        <div className="dat-pl-grid">
          <EpisodeList
            summary={sum}
            entry={entry}
            current={ep}
            marks={marks}
            onlyHints={onlyHints}
            onToggleOnlyHints={onToggleOnlyHints}
            onSelect={selectEp}
            onToggleMark={markToggle}
            onClearMarks={onClearMarks}
            onDeleteMarked={onDeleteMarked}
            jointNames={jointNames}
          />
          <section className="dat-pl-main">
            <Stage
              engine={engine}
              videoRef={videoRef}
              cameras={cameras}
              driverIndex={driver ? driver.index : -1}
              token={token}
              episode={ep}
              onTokenError={onTokenError}
              view={view}
              onView={setView}
              preset={preset}
              onPreset={setPreset}
              connected={connected}
              poseSource={poseSource}
            >
              <Transport
                engine={engine}
                playing={playing}
                speed={speed}
                onSpeed={setSpeed}
                fps={fps}
                length={length}
                durationS={durationS}
                hints={epInfo ? epInfo.hints : EMPTY}
                jointNames={jointNames}
                marked={marks.includes(ep)}
                onToggleMark={onToggleCurrentMark}
                onPrevEpisode={onPrevEpisode}
                onNextEpisode={onNextEpisode}
              />
            </Stage>
            <div className="dat-info-row">
              <HintsPanel
                episode={ep}
                hints={epInfo ? epInfo.hints : EMPTY}
                jointNames={jointNames}
                durationS={durationS}
                onSeekSeconds={onSeekSeconds}
              />
              <DetailsPanel summary={sum} episode={ep} link={link} />
            </div>
            <JointCharts
              engine={engine}
              data={epData}
              names={jointNames}
              length={length}
              fps={fps}
              durationS={durationS}
              hints={epInfo ? epInfo.hints : EMPTY}
            />
            <KeysLegend />
          </section>
        </div>
      ) : null}
    </div>
  );
}

export default React.memo(PlayerView);
