// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The camera tiles of the Aufnahme stage (spec §3.12). Replaces ImageGrid on
// this page: no picker, no close buttons, no toasts. The page shows the cameras
// the robot HAS, in the order its profile names them (expectedCameraRoles), each
// under its German role name („Greifer-Kamera", „Szenen-Kamera") with a live
// rate badge from /edubotics/signal_status when that topic exists.
//
// The topic list is asked for silently: on mount, again whenever the robot
// link comes back, and again every RETRY_MS while the last answer failed,
// named no camera or lacked one the profile expects (the camera container may
// still be starting). Once the robot HAS answered, a role it does not deliver,
// or a stream that fails, shows „Kamerabild nicht verfügbar" in its tile —
// never before the first answer, which would flash a failure on every visit.
//
// Presentational in the sense that matters: the service call is injected
// (`fetchTopics`), so nothing here touches Redux or rosbridge directly;
// ImageGridCell (bare) draws the stream.

import React, { useEffect, useMemo, useRef, useState } from 'react';
import ImageGridCell from '../ImageGridCell';

export const RETRY_MS = 5000;

/** '/gripper/image_raw' → 'gripper' (the role is the first path segment). */
export function topicRole(topic) {
  const seg = String(topic || '').split('/').filter(Boolean)[0];
  return seg || '';
}

/**
 * The tiles to draw: one per expected role (a missing one with topic null), or
 * — when the profile names no roles — one per camera the robot reported.
 */
export function cameraTiles(topics, roles) {
  const list = Array.isArray(topics) ? topics.filter((t) => typeof t === 'string' && t.trim()) : [];
  const byRole = new Map();
  list.forEach((t) => { if (!byRole.has(topicRole(t))) byRole.set(topicRole(t), t); });
  if (Array.isArray(roles) && roles.length) {
    return roles.map((role) => ({ role, topic: byRole.get(role) || null }));
  }
  return [...byRole.entries()].map(([role, topic]) => ({ role, topic }));
}

const DOT = { ok: 'ok', slow: 'warn', stalled: 'bad' };

function formatHz(hz) {
  if (typeof hz === 'string') return hz;
  if (typeof hz === 'number' && Number.isFinite(hz)) return `${Math.round(hz)} Hz`;
  return '—';
}

function Tile({ tile, index, label, badge, fallbackText, isActive, answered }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => { setFailed(false); }, [tile.topic]);
  const unavailable = failed || (answered && !tile.topic);
  return (
    <div className="rec-tile" data-testid="rec-camera-tile" data-role={tile.role}>
      {unavailable ? <div className="rec-tile-fallback">{fallbackText}</div> : null}
      {tile.topic ? (
        <div className="rec-tile-fill" style={failed ? { visibility: 'hidden' } : undefined}>
          <ImageGridCell
            bare
            topic={tile.topic}
            idx={index}
            isActive={isActive}
            onStreamError={() => setFailed(true)}
          />
        </div>
      ) : null}
      <div className="rec-tl">
        {badge ? <span className={`rec-dot ${DOT[badge.verdict] || ''}`.trim()} data-verdict={badge.verdict} /> : null}
        <span>{label}</span>
        {badge ? <span className="rec-hz">{typeof badge.hzText === 'string' ? badge.hzText : formatHz(badge.hz)}</span> : null}
      </div>
    </div>
  );
}

export default function CameraTiles({
  connected = false,
  fetchTopics,
  roles = null,
  labelFor = (role) => role,
  signal = null,
  fallbackText = '',
  isActive = true,
  retryMs = RETRY_MS,
}) {
  const [topics, setTopics] = useState(null);
  const [needsRetry, setNeedsRetry] = useState(false);
  const fetchRef = useRef(fetchTopics);
  useEffect(() => { fetchRef.current = fetchTopics; }, [fetchTopics]);
  const rolesRef = useRef(roles);
  useEffect(() => { rolesRef.current = roles; }, [roles]);
  const [attempt, setAttempt] = useState(0);

  // Ask on mount and whenever the link comes back; a failed or empty answer
  // re-asks after retryMs while still connected. Silent: no toast, no log.
  useEffect(() => {
    if (!connected || typeof fetchRef.current !== 'function') return undefined;
    let cancelled = false;
    (async () => {
      let list = null;
      try {
        const res = await fetchRef.current();
        if (res && res.success) list = Array.isArray(res.image_topic_list) ? res.image_topic_list : [];
      } catch {
        list = null;
      }
      if (cancelled) return;
      if (list !== null) setTopics(list);
      const have = new Set((list || []).map(topicRole));
      const missing = Array.isArray(rolesRef.current) && rolesRef.current.some((r) => !have.has(r));
      setNeedsRetry(list === null || list.length === 0 || missing);
    })();
    return () => { cancelled = true; };
  }, [connected, attempt]);

  useEffect(() => {
    if (!connected || !needsRetry) return undefined;
    const t = setTimeout(() => setAttempt((a) => a + 1), retryMs);
    return () => clearTimeout(t);
  }, [connected, needsRetry, retryMs, attempt]);

  const tiles = useMemo(() => cameraTiles(topics, roles), [topics, roles]);
  const badges = useMemo(() => {
    const m = new Map();
    if (Array.isArray(signal)) signal.filter((s) => s && s.kind === 'camera').forEach((s) => m.set(s.name, s));
    return m;
  }, [signal]);

  return (
    <>
      {tiles.map((tile, i) => (
        <Tile
          key={tile.role}
          tile={tile}
          index={i}
          label={labelFor(tile.role)}
          badge={Array.isArray(signal) ? badges.get(tile.role) || null : null}
          fallbackText={fallbackText}
          isActive={isActive}
          answered={topics !== null}
        />
      ))}
    </>
  );
}
