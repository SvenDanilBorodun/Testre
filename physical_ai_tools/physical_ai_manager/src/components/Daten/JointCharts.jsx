// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Gelenke" (spec §F5, the mockup's charts): one recharts LineChart per
// joint, in DEGREES — the follower's measured state solid in the accent, the
// leader's command dashed in the leader violet — down-sampled to ≤ 600 points
// per series (min/max per bucket), the hint bands, a zero line when the range
// spans 0, a y-range of at least 10°. The CURSOR is a 1-px element over the
// plot area moved by REF on every frame (fixed chart margins, so the plot area
// is known), and so are the „f° / l°" values; a click seeks to the frame under
// the pointer. Nothing here re-renders per frame.

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Line, LineChart, ReferenceArea, ReferenceLine, XAxis, YAxis,
} from 'recharts';
import Icon from '../icons/Icon';
import { countRender } from '../../features/editDataset/renderProbe';
import COPY from '../../features/editDataset/datenCopy';
import { jointChart } from '../../features/editDataset/model/chartData';
import { fill } from '../../features/editDataset/model/format';
import { jointLabel } from '../../features/editDataset/model/labels';
import { hintBand } from '../../features/editDataset/model/hintText';
import { frameAt, frameFraction, scrubTarget } from '../../features/editDataset/model/playerClock';

export const CHART_MARGIN = Object.freeze({ top: 6, right: 4, bottom: 6, left: 4 });
const CHART_HEIGHT = 84;
const FALLBACK_WIDTH = 300;

function useWidth(ref) {
  const [width, setWidth] = useState(FALLBACK_WIDTH);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const measure = () => {
      const w = Math.round(el.getBoundingClientRect().width);
      if (w > 0) setWidth(w);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

const Chart = React.memo(function Chart({
  name, k, data, length, fps, durationS, hints, onSeek, registerCursor,
}) {
  const plotRef = useRef(null);
  const width = useWidth(plotRef);
  const chart = useMemo(() => jointChart(data, k), [data, k]);
  const total = Math.max(0, Number(durationS) || length / fps);
  const bands = (hints || []).map((h) => hintBand(h, total)).filter(Boolean);

  const cursorRef = useRef(null);
  const fRef = useRef(null);
  const lRef = useRef(null);
  useEffect(() => registerCursor(k, { cursor: cursorRef, f: fRef, l: lRef, chart }), [k, chart, registerCursor]);

  const label = jointLabel(name);
  return (
    <figure className="dat-chart" data-joint={name}>
      <figcaption>
        <span>{label}</span>
        <span className="dat-vals dat-mono">
          <b className="dat-f" ref={fRef}>–</b>
          <b className="dat-l" ref={lRef}>–</b>
        </span>
      </figcaption>
      {/* A click seeks; the keyboard has the player's own keys (§F4). */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <div
        ref={plotRef}
        className="dat-chart-plot"
        role="img"
        aria-label={fill(COPY.player.chartAria, { name: label })}
        onClick={(e) => {
          const r = plotRef.current.getBoundingClientRect();
          const inner = r.width - CHART_MARGIN.left - CHART_MARGIN.right;
          const frac = inner > 0 ? (e.clientX - r.left - CHART_MARGIN.left) / inner : 0;
          onSeek(scrubTarget(frac, length));
        }}
      >
        <LineChart width={width} height={CHART_HEIGHT} margin={CHART_MARGIN}>
          <XAxis type="number" dataKey="i" domain={[0, Math.max(1, length - 1)]} hide allowDataOverflow />
          <YAxis type="number" domain={chart.domain} hide allowDataOverflow />
          {bands.map((b) => (
            <ReferenceArea
              key={`${b.from}-${b.to}`}
              x1={frameAt(b.from, fps, length)}
              x2={frameAt(b.to, fps, length)}
              fill="rgba(208,138,30,.12)"
              strokeOpacity={0}
              ifOverflow="hidden"
            />
          ))}
          {chart.zero ? <ReferenceLine y={0} stroke="var(--line-2)" strokeWidth={1} /> : null}
          <Line
            data={chart.leader}
            dataKey="v"
            type="linear"
            stroke="var(--leader)"
            strokeWidth={1.6}
            strokeDasharray="5 4"
            dot={false}
            isAnimationActive={false}
          />
          <Line
            data={chart.follower}
            dataKey="v"
            type="linear"
            stroke="var(--accent)"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
        <div
          ref={cursorRef}
          className="dat-chart-cursor"
          style={{ left: CHART_MARGIN.left }}
        />
      </div>
    </figure>
  );
});

function JointCharts({
  engine, data, names, length, fps, durationS, hints,
}) {
  countRender('JointCharts');
  const cursors = useRef(new Map());
  const registerCursor = React.useCallback((k, entry) => {
    cursors.current.set(k, entry);
    return () => { if (cursors.current.get(k) === entry) cursors.current.delete(k); };
  }, []);

  useEffect(() => {
    const paint = (idx) => {
      const frac = frameFraction(idx, length);
      cursors.current.forEach((c) => {
        const el = c.cursor.current;
        if (el) {
          el.style.left = `calc(${CHART_MARGIN.left}px + (100% - ${CHART_MARGIN.left + CHART_MARGIN.right}px) * ${frac})`;
        }
        const fv = c.chart.followerAll[idx];
        const lv = c.chart.leaderAll[idx];
        if (c.f.current) c.f.current.textContent = Number.isFinite(fv) ? `${Math.round(fv)}°` : '–';
        if (c.l.current) c.l.current.textContent = Number.isFinite(lv) ? `${Math.round(lv)}°` : '–';
      });
    };
    paint(engine.getIdx());
    return engine.subscribe(paint);
  }, [engine, length, data]);
  const onSeek = React.useCallback((i) => engine.seekFrame(i), [engine]);

  return (
    <div className="dat-panel dat-charts">
      <div className="dat-charts-head">
        <h2><Icon name="jointCurves" size={16} />{COPY.player.chartsTitle}</h2>
        <span className="dat-grow" />
        <span className="dat-lg"><i />{COPY.player.legendFollower}</span>
        <span className="dat-lg"><i className="dat-l" />{COPY.player.legendLeader}</span>
        <span className="dat-small">{COPY.player.chartsNote}</span>
      </div>
      <div className="dat-chart-grid">
        {data ? (names || []).map((name, k) => (
          <Chart
            key={name}
            name={name}
            k={k}
            data={data}
            length={length}
            fps={fps}
            durationS={durationS}
            hints={hints}
            onSeek={onSeek}
            registerCursor={registerCursor}
          />
        )) : null}
      </div>
    </div>
  );
}

export default React.memo(JointCharts);
