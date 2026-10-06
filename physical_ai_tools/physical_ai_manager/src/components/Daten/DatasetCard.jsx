// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// One library card (the approved mockup's `.card`): draws what
// features/editDataset/model/cardModel.js decided — the thumbnail, the badge,
// the title and owner chip, the stats, the hint line, the actions and the ⋮
// menu. A click on a button releases its focus (the Aufnahme invariant), so
// Space never re-presses it.

import React, { useEffect, useRef, useState } from 'react';
import Icon from '../icons/Icon';
import SyncBadge from './SyncBadge';
import CardMenu from './CardMenu';
import Fill from './Fill';
import { releasePointerFocus } from '../Record/ActionBar';
import COPY from '../../features/editDataset/datenCopy';
import { THUMB_RETRY_DELAYS_MS, classifyMedia, isTokenError, thumbUrl } from '../../features/editDataset/api/datenHttp';

const VARIANT = {
  primary: 'dat-btn dat-btn-sm dat-btn-primary',
  danger: 'dat-btn dat-btn-sm dat-btn-danger',
  ghost: 'dat-btn dat-btn-sm dat-btn-ghost',
};

/** The card's poster: the sidecar's thumb.jpg, retried on 503 (2/4/8 s) and re-minted once on a 403. */
function CardThumb({ id, peekToken, getToken }) {
  const [token, setToken] = useState(() => peekToken(id));
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const remintedRef = useRef(false);
  const timerRef = useRef(null);

  useEffect(() => {
    if (!token) {
      const t = peekToken(id);
      if (t) setToken(t);
    }
  }, [id, token, peekToken]);
  useEffect(() => () => clearTimeout(timerRef.current), []);

  if (!token || failed) return null;
  const src = thumbUrl(token);
  const onError = async () => {
    const { code } = await classifyMedia(src);
    if (isTokenError(code) && !remintedRef.current) {
      remintedRef.current = true;
      const fresh = await getToken(id, true);
      if (fresh) { setToken(fresh); setAttempt((a) => a + 1); } else setFailed(true);
      return;
    }
    if ((code === 'overloaded' || code === 'sidecar_down') && attempt < THUMB_RETRY_DELAYS_MS.length) {
      timerRef.current = setTimeout(() => setAttempt((a) => a + 1), THUMB_RETRY_DELAYS_MS[attempt]);
      return;
    }
    setFailed(true);
  };
  return <img key={`${token}-${attempt}`} src={src} alt="" loading="lazy" onError={onError} />;
}

function ActionButton({ a, onAction }) {
  return (
    <button
      type="button"
      className={VARIANT[a.variant] || 'dat-btn dat-btn-sm'}
      disabled={!!a.disabled}
      aria-disabled={a.ariaDisabled ? 'true' : undefined}
      title={a.title || undefined}
      data-action={a.id}
      onClick={(e) => {
        releasePointerFocus(e);
        onAction(a.id);
      }}
    >
      {a.icon ? <Icon name={a.icon} size={16} /> : null}
      {a.label}
    </button>
  );
}

export default function DatasetCard({
  model, onAction, onMenu, onMergePick, onRefreshSync, peekToken, getToken, onMenuOpenChange,
}) {
  const m = model;
  return (
    <article className={`dat-card${m.mergePick && m.mergePick.selected ? ' dat-is-sel' : ''}`} data-id={m.id} data-kind={m.kind}>
      <div className="dat-thumb">
        {m.thumb === 'image' ? <CardThumb id={m.id} peekToken={peekToken} getToken={getToken} /> : null}
        {m.thumb === 'online' ? (
          <div className="dat-veil"><span><Icon name="cloud" size={14} />{COPY.card.previewAfterLoad}</span></div>
        ) : null}
        {m.badge ? (
          <div className="dat-badge-wrap">
            <SyncBadge
              state={m.badge.state}
              reason={m.badge.reason}
              overlay={m.badge.overlay}
              ownerName={m.badge.ownerName}
              onRefresh={onRefreshSync}
            />
          </div>
        ) : null}
        {m.mergePick ? (
          <button
            type="button"
            className="dat-selbox"
            aria-pressed={m.mergePick.selected ? 'true' : 'false'}
            aria-label={COPY.card.mergePick}
            disabled={m.mergePick.disabled}
            title={m.mergePick.title || undefined}
            onClick={(e) => { releasePointerFocus(e); onMergePick(m.id); }}
          >
            <Icon name={m.mergePick.selected ? 'check' : 'plus'} size={16} />
          </button>
        ) : null}
      </div>
      <div className="dat-card-body">
        <div className="dat-card-title-row">
          <h3 title={m.title}>{m.title}</h3>
          {m.ownerName ? <span className="dat-owner"><Icon name="users" size={13} />{m.ownerName}</span> : null}
        </div>
        <div className="dat-repo dat-mono">{m.repo}</div>
        {m.source ? (
          <div className="dat-src">
            <Icon name="copy" size={13} />
            <Fill template={COPY.card.copyOf} values={{ repo: <span className="dat-mono">{m.source}</span> }} />
          </div>
        ) : null}
        <dl className="dat-stats">
          <div><dt>{COPY.card.episodes}</dt><dd>{m.stats.episodes}</dd></div>
          <div><dt>{COPY.card.duration}</dt><dd>{m.stats.duration}</dd></div>
          <div><dt>{COPY.card.size}</dt><dd>{m.stats.size}</dd></div>
          <div><dt>{COPY.card.fps}</dt><dd>{m.stats.fps}</dd></div>
        </dl>
        <div className={`dat-hintline${m.hint.kind === 'warn' ? ' dat-warn' : ''}${m.hint.kind === 'ok' ? ' dat-ok' : ''}${m.hint.kind === 'bad' ? ' dat-bad' : ''}`}>
          <Icon name={m.hint.icon} size={14} className={m.hint.spin ? 'animate-spin' : undefined} />
          <span>{m.hint.text}</span>
        </div>
      </div>
      <div className="dat-card-actions">
        {m.progress ? (
          <>
            <div className="dat-prog">
              <div className="dat-row"><span>{m.progress.label}</span><span className="dat-mono">{m.progress.right}</span></div>
              <div className="dat-bar"><i style={{ width: `${m.progress.pct}%` }} /></div>
            </div>
            {m.progress.cancel ? (
              <button
                type="button"
                className="dat-btn dat-btn-sm dat-btn-ghost"
                data-action={`cancel_${m.progress.cancel}`}
                onClick={(e) => { releasePointerFocus(e); onAction(`cancel_${m.progress.cancel}`); }}
              >
                {COPY.card.cancel}
              </button>
            ) : null}
          </>
        ) : (
          <>
            {m.actions.map((a) => <ActionButton key={a.id} a={a} onAction={onAction} />)}
            {m.inlineNote ? <span className="dat-small dat-grow">{m.inlineNote}</span> : <span className="dat-grow" />}
            {m.menu ? <CardMenu items={m.menu} onSelect={onMenu} onOpenChange={onMenuOpenChange} /> : null}
            {m.actions2.length ? (
              <div className="dat-card-actions2">
                {m.actions2.map((a) => <ActionButton key={a.id} a={a} onAction={onAction} />)}
              </div>
            ) : null}
            {m.note ? <span className="dat-card-note">{m.note}</span> : null}
          </>
        )}
      </div>
    </article>
  );
}
