// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The dark stage (spec §F1, §F7): one muted <video> per camera in the
// mockup's visual order (Greifer, Szene, others) — the scene camera drives the
// clock — and the 3D twin (the existing lazy UrdfTwin, posed from the
// recording through `poseSource`, the leader's command as a violet ghost),
// shown as „Kameras | 3D | Beides". A clip the cutter refuses (409
// `unplayable`) shows its sentence in that tile while the other camera and the
// charts go on; a 503 is retried once after 2 s, then „Erneut versuchen".

import React, { Suspense, useEffect, useRef, useState } from 'react';
import Icon from '../icons/Icon';
import { releasePointerFocus } from '../Record/ActionBar';
import { countRender } from '../../features/editDataset/renderProbe';
import COPY from '../../features/editDataset/datenCopy';
import { CLIP_RETRY_DELAY_MS, classifyMedia, clipUrl, isTokenError } from '../../features/editDataset/api/datenHttp';
import { cameraLabel } from '../../features/editDataset/model/labels';

const UrdfTwin = React.lazy(() => import('../UrdfTwin'));

export const PRESETS = Object.freeze([
  ['persp', 'presetPersp'],
  ['front', 'presetFront'],
  ['side', 'presetSide'],
  ['top', 'presetTop'],
]);

function VideoTile({
  cam, isDriver, token, episode, engine, videoRef, onTokenError,
}) {
  const [status, setStatus] = useState('loading'); // loading | ok | unplayable | failed
  const [attempt, setAttempt] = useState(0);
  const retriedRef = useRef(false);
  const remintedRef = useRef(false);
  const timerRef = useRef(null);
  const src = token ? clipUrl(token, episode, cam.index) : null;

  // A new clip (episode, token): start over.
  useEffect(() => {
    setStatus('loading');
    retriedRef.current = false;
    remintedRef.current = false;
    engine.setFailed(cam.index, false);
  }, [src, engine, cam.index]);
  useEffect(() => () => clearTimeout(timerRef.current), []);

  const fail = (kind) => {
    setStatus(kind);
    engine.setFailed(cam.index, true);
  };

  const onError = async () => {
    if (!src) return;
    const { code } = await classifyMedia(src);
    if (code === 'unplayable') { fail('unplayable'); return; }
    if (isTokenError(code) && !remintedRef.current) {
      remintedRef.current = true;
      onTokenError();
      return;
    }
    if ((code === 'overloaded' || code === 'sidecar_down') && !retriedRef.current) {
      retriedRef.current = true;
      timerRef.current = setTimeout(() => setAttempt((a) => a + 1), CLIP_RETRY_DELAY_MS);
      return;
    }
    fail('failed');
  };

  return (
    <div className="dat-tile" data-camera={cam.name || cam.key} data-status={status}>
      {src && status !== 'unplayable' && status !== 'failed' ? (
        <video
          key={`${src}#${attempt}`}
          ref={videoRef(cam.index, isDriver)}
          src={src}
          muted
          playsInline
          preload="auto"
          onLoadedData={() => setStatus('ok')}
          onError={onError}
        />
      ) : null}
      <span className="dat-lbl"><Icon name="camera" size={13} />{cameraLabel(cam)}</span>
      {status === 'unplayable' ? <div className="dat-tile-note">{COPY.player.unplayable}</div> : null}
      {status === 'failed' ? (
        <div className="dat-tile-note">
          <span>{COPY.player.loadFailed}</span>
          <button
            type="button"
            className="dat-btn dat-btn-sm"
            onClick={(e) => {
              releasePointerFocus(e);
              retriedRef.current = false;
              remintedRef.current = false;
              engine.setFailed(cam.index, false);
              setStatus('loading');
              setAttempt((a) => a + 1);
            }}
          >
            <Icon name="refresh" size={14} />
            {COPY.player.retry}
          </button>
        </div>
      ) : null}
    </div>
  );
}

function Stage({
  engine, videoRef, cameras, driverIndex, token, episode, onTokenError,
  view, onView, preset, onPreset, connected, poseSource, children,
}) {
  countRender('Stage');
  const show3d = view !== 'cams';
  const showCams = view !== '3d';
  const click = (fn) => (e) => { releasePointerFocus(e); fn(); };
  return (
    <div className="dat-stage">
      <div className="dat-stage-bar">
        <div className="dat-seg dat-dark" role="group" aria-label={COPY.player.viewLabel}>
          {[['cams', COPY.player.viewCams], ['3d', COPY.player.view3d], ['both', COPY.player.viewBoth]].map(([k, l]) => (
            <button key={k} type="button" aria-pressed={view === k ? 'true' : 'false'} onClick={click(() => onView(k))}>{l}</button>
          ))}
        </div>
        <span className="dat-grow" />
        <span className="dat-legend">
          <span><i className="dat-sw" />{COPY.player.legendFollower}</span>
          <span><i className="dat-sw dat-l" />{COPY.player.legendLeader}</span>
        </span>
      </div>
      <div className={`dat-tiles dat-v-${view}${cameras.length === 1 ? ' dat-one-cam' : ''}`}>
        {showCams ? cameras.map((cam) => (
          <VideoTile
            key={cam.index}
            cam={cam}
            isDriver={cam.index === driverIndex}
            token={token}
            episode={episode}
            engine={engine}
            videoRef={videoRef}
            onTokenError={onTokenError}
          />
        )) : null}
        {show3d ? (
          <div className="dat-tile dat-t3d">
            {connected ? (
              <div className="dat-twin">
                <Suspense fallback={null}>
                  <UrdfTwin showChrome={false} viewPreset={preset} poseSource={poseSource} />
                </Suspense>
              </div>
            ) : <div className="dat-tile-note">{COPY.player.offline3d}</div>}
            <span className="dat-lbl"><Icon name="rotate3d" size={13} />{COPY.player.tile3d}</span>
            <div className="dat-presets">
              {PRESETS.map(([k, labelKey]) => (
                <button key={k} type="button" aria-pressed={preset === k ? 'true' : 'false'} onClick={click(() => onPreset(k))}>
                  {COPY.player[labelKey]}
                </button>
              ))}
            </div>
            <span className="dat-hint3d">{COPY.player.drag3d}</span>
          </div>
        ) : null}
      </div>
      {children}
    </div>
  );
}

export default React.memo(Stage);
