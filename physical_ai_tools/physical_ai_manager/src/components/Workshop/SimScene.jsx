/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Roboter Studio Phase-3 — "Test im Simulator" view.
//
// Replaces the live scene camera in Workshop's right column while the student is
// in simulator mode. Two halves:
//   * a 2D top-down TABLE EDITOR — drag catalog objects onto the arm's reach
//     annulus (x,y base-frame plane), set each object's yaw, auto-assign a unique
//     tag_id; emits the Sim-Szene `{version:1, objects:[{type,tag_id,x,y,yaw}]}`
//     up to WorkshopPage via onChange (persisted in workflows.sim_scene on save,
//     sent in the /workflow/start payload's `sim` sibling by RunControls).
//   * a LAZY 3D preview (UrdfTwin) of the virtual follower replaying the
//     server's /sim/joint_states, with the placed objects on a virtual table.
//
// Grasp-attach is computed HERE on the front end (the thin UrdfTwin renderer only
// surfaces the end-effector pose + gripper angle via onEndEffector): when the
// gripper crosses CLOSED and the end-effector is within a capture radius of an
// un-held object → that object is held (its mesh follows the gripper); on OPEN →
// released. No backend signal — the sim is logic+geometry, not physics.
//
// three.js is pulled in ONLY through the React.lazy import below, so this module
// (statically imported by the eagerly-loaded WorkshopPage) keeps three out of the
// entry bundle the white-screen CI greps.

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  Suspense,
  lazy,
} from 'react';

const UrdfTwin = lazy(() => import('../UrdfTwin'));

// Sim-only virtual joint stream (never the bare /joint_states — see plan §C).
const SIM_JOINT_TOPIC = '/sim/joint_states';

// Strict-vertical reach annulus of the OMX-F closed-form IK (ik_solver.py:
// ~0.10–0.28 m table-top radius). Objects must sit inside it to be graspable.
const REACH_INNER_M = 0.10;
const REACH_OUTER_M = 0.28;

// Base-frame view window (metres): x = forward (away from base), y = left.
const VIEW_MAX_X = 0.32;
const VIEW_MIN_X = -0.05;
const VIEW_MAX_Y = 0.30;
const VIEW_MIN_Y = -0.30;
// Uniform metres→pixels so the reach annulus draws as a true circle. The SVG
// box is sized to the metre ranges at this scale.
const PX_PER_M = 500;
const SVG_W = PX_PER_M * (VIEW_MAX_Y - VIEW_MIN_Y); // 300
const SVG_H = PX_PER_M * (VIEW_MAX_X - VIEW_MIN_X); // 185
// Base origin (robot base, x=0 y=0) in SVG pixels.
const ORIGIN_PX = PX_PER_M * VIEW_MAX_Y;
const ORIGIN_PY = PX_PER_M * VIEW_MAX_X;
const OBJECT_PX = 15; // marker square edge (≈ SIM_OBJECT_SIZE_M at PX_PER_M)

// Grasp-attach geometry (front-end, idealized). The OMX-F gripper joint rests
// open ≈ +0.8 rad and any CLOSE drives it negative-ish (per-object close angles
// run ≈ -0.1 … -0.5, with no fixed floor). The published /sim/joint_states stream
// is the COMMANDED gripper, so classify with a wide hysteresis band well below the
// open rest: "closing" when it crosses BELOW +0.2 (catches even a shallow -0.1
// close on a wide object — M1 fix; the old -0.20 threshold missed those), "open"
// again above +0.5. The 0.2…0.5 band prevents chatter; the descend (gripper held
// at +0.8) never trips "closing".
const GRIPPER_CLOSED_RAD = 0.2;
const GRIPPER_OPEN_RAD = 0.5;
const CAPTURE_RADIUS_M = 0.06;

// Fallback palette when the catalog service returns nothing (an uncalibrated rig
// may have no object_catalog.json yet) — keeps the simulator usable.
const DEFAULT_CATALOG = [['Würfel', 'wuerfel']];

// base (x,y) → SVG pixels. +x (forward) points UP, +y (left) points LEFT.
function baseToSvg(x, y) {
  return {
    px: PX_PER_M * (VIEW_MAX_Y - y),
    py: PX_PER_M * (VIEW_MAX_X - x),
  };
}
// SVG pixels → base (x,y).
function svgToBase(px, py) {
  return {
    x: VIEW_MAX_X - py / PX_PER_M,
    y: VIEW_MAX_Y - px / PX_PER_M,
  };
}
// Clamp a base point into the reach annulus so every placement is reachable.
function clampToAnnulus(x, y) {
  const r = Math.hypot(x, y);
  if (r === 0) return { x: REACH_INNER_M, y: 0 };
  let rr = r;
  if (r < REACH_INNER_M) rr = REACH_INNER_M;
  else if (r > REACH_OUTER_M) rr = REACH_OUTER_M;
  if (rr === r) return { x, y };
  const k = rr / r;
  return { x: x * k, y: y * k };
}
function round3(v) {
  return Math.round(v * 1000) / 1000;
}

function SimScene({ scene, onChange, catalog }) {
  const objects = useMemo(
    () => (scene && Array.isArray(scene.objects) ? scene.objects : []),
    [scene],
  );
  const cat = Array.isArray(catalog) && catalog.length ? catalog : DEFAULT_CATALOG;

  const [selectedType, setSelectedType] = useState(cat[0][1]);
  const [selectedId, setSelectedId] = useState(null);
  const [heldObjectId, setHeldObjectId] = useState(null);

  const svgRef = useRef(null);
  const draggingIdRef = useRef(null);
  // Grasp state machine + latest pose, read by the (stable) onEndEffector cb.
  const graspRef = useRef({ closed: false });
  const heldRef = useRef(null);
  const objectsRef = useRef(objects);

  // Keep the selected type valid as the catalog arrives/changes.
  useEffect(() => {
    if (!cat.some(([, value]) => value === selectedType)) {
      setSelectedType(cat[0][1]);
    }
  }, [cat, selectedType]);

  // Mirror objects into a ref for the grasp callback, and drop a held id whose
  // object was deleted.
  useEffect(() => {
    objectsRef.current = objects;
    if (heldObjectId !== null && !objects.some((o) => o.tag_id === heldObjectId)) {
      heldRef.current = null;
      setHeldObjectId(null);
    }
  }, [objects, heldObjectId]);

  const emit = useCallback(
    (nextObjects) => {
      if (typeof onChange === 'function') {
        onChange({ version: 1, objects: nextObjects });
      }
    },
    [onChange],
  );

  // Pointer → base coords inside the SVG (handles CSS scaling).
  const eventToBase = useCallback((e) => {
    const svg = svgRef.current;
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const px = ((e.clientX - rect.left) / rect.width) * SVG_W;
    const py = ((e.clientY - rect.top) / rect.height) * SVG_H;
    return svgToBase(px, py);
  }, []);

  // Click empty table area → place the selected type (clamped into the annulus).
  const handlePlace = useCallback(
    (e) => {
      if (draggingIdRef.current !== null) return;
      const base = eventToBase(e);
      if (!base) return;
      const { x, y } = clampToAnnulus(base.x, base.y);
      const nextTag = objects.length
        ? Math.max(...objects.map((o) => (typeof o.tag_id === 'number' ? o.tag_id : -1))) + 1
        : 0;
      const next = [
        ...objects,
        { type: selectedType, tag_id: nextTag, x: round3(x), y: round3(y), yaw: 0 },
      ];
      emit(next);
      setSelectedId(nextTag);
    },
    [eventToBase, objects, selectedType, emit],
  );

  const startDrag = useCallback((e, id) => {
    e.stopPropagation();
    draggingIdRef.current = id;
    setSelectedId(id);
    if (svgRef.current && typeof svgRef.current.setPointerCapture === 'function') {
      try { svgRef.current.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    }
  }, []);

  const handlePointerMove = useCallback(
    (e) => {
      const id = draggingIdRef.current;
      if (id === null) return;
      const base = eventToBase(e);
      if (!base) return;
      const { x, y } = clampToAnnulus(base.x, base.y);
      emit(
        objects.map((o) =>
          o.tag_id === id ? { ...o, x: round3(x), y: round3(y) } : o,
        ),
      );
    },
    [eventToBase, objects, emit],
  );

  const handlePointerUp = useCallback(() => {
    draggingIdRef.current = null;
  }, []);

  const handleYaw = useCallback(
    (deg) => {
      if (selectedId === null) return;
      const yaw = (deg * Math.PI) / 180;
      emit(
        objects.map((o) =>
          o.tag_id === selectedId ? { ...o, yaw: round3(yaw) } : o,
        ),
      );
    },
    [selectedId, objects, emit],
  );

  const handleDelete = useCallback(() => {
    if (selectedId === null) return;
    emit(objects.filter((o) => o.tag_id !== selectedId));
    setSelectedId(null);
  }, [selectedId, objects, emit]);

  const handleClear = useCallback(() => {
    if (!objects.length) return;
    emit([]);
    setSelectedId(null);
  }, [objects, emit]);

  // Grasp-attach geometry. Stable callback (reads refs) so UrdfTwin's
  // onEndEffector prop identity never churns.
  const handleEndEffector = useCallback(({ x, y, gripper }) => {
    if (typeof gripper !== 'number' || !Number.isFinite(gripper)) return;
    const g = graspRef.current;
    if (gripper < GRIPPER_CLOSED_RAD && !g.closed) {
      g.closed = true;
      if (heldRef.current === null) {
        let best = null;
        let bestD = CAPTURE_RADIUS_M;
        objectsRef.current.forEach((o) => {
          if (o.tag_id === undefined || o.tag_id === null) return;
          const d = Math.hypot((o.x || 0) - x, (o.y || 0) - y);
          if (d <= bestD) {
            bestD = d;
            best = o.tag_id;
          }
        });
        if (best !== null) {
          heldRef.current = best;
          setHeldObjectId(best);
        }
      }
    } else if (gripper > GRIPPER_OPEN_RAD && g.closed) {
      g.closed = false;
      if (heldRef.current !== null) {
        heldRef.current = null;
        setHeldObjectId(null);
      }
    }
  }, []);

  const typeLabel = useMemo(() => {
    const m = new Map(cat.map(([label, value]) => [value, label]));
    return (value) => m.get(value) || value;
  }, [cat]);

  const selected = objects.find((o) => o.tag_id === selectedId) || null;
  const selectedYawDeg = selected
    ? Math.round(((selected.yaw || 0) * 180) / Math.PI)
    : 0;

  return (
    <div className="flex flex-col gap-3">
      {/* German notice: the simulator validates logic, not physics. */}
      <div className="rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-800">
        <strong>Test im Simulator.</strong> Hier prüfst du die Logik und
        Reihenfolge deines Programms — nicht die echte Physik des Greifens.
        Platziere Objekte auf dem Tisch und lass das Programm den virtuellen
        Roboter steuern.
      </div>

      {/* Object palette */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-[var(--ink-3)]">Objekt:</span>
        {cat.map(([label, value]) => (
          <button
            key={value}
            type="button"
            onClick={() => setSelectedType(value)}
            aria-pressed={selectedType === value}
            className={
              'px-2.5 py-1 text-xs rounded-md border '
              + (selectedType === value
                ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
            }
          >
            {label}
          </button>
        ))}
      </div>

      {/* 2D top-down table editor */}
      <div className="rounded-lg border border-[var(--line)] bg-white p-2">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${SVG_W} ${SVG_H}`}
          className="w-full h-auto rounded-md bg-[var(--bg-sunk)] touch-none cursor-crosshair"
          onClick={handlePlace}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerLeave={handlePointerUp}
          role="application"
          aria-label="Simulator-Tisch — Objekte platzieren"
        >
          {/* Reach annulus (graspable ring) */}
          <circle
            cx={ORIGIN_PX}
            cy={ORIGIN_PY}
            r={REACH_OUTER_M * PX_PER_M}
            fill="rgba(34,197,94,0.08)"
            stroke="#22c55e"
            strokeWidth="1"
            strokeDasharray="4 3"
          />
          <circle
            cx={ORIGIN_PX}
            cy={ORIGIN_PY}
            r={REACH_INNER_M * PX_PER_M}
            fill="var(--bg-sunk)"
            stroke="#22c55e"
            strokeWidth="1"
            strokeDasharray="4 3"
          />
          {/* Robot base marker */}
          <circle cx={ORIGIN_PX} cy={ORIGIN_PY} r="4" fill="#64748b" />
          <text
            x={ORIGIN_PX + 7}
            y={ORIGIN_PY + 4}
            fontSize="9"
            fill="#64748b"
          >
            Roboter
          </text>

          {/* Placed objects */}
          {objects.map((o) => {
            const { px, py } = baseToSvg(o.x || 0, o.y || 0);
            const isSel = o.tag_id === selectedId;
            const isHeld = o.tag_id === heldObjectId;
            // yaw direction in SVG: base dir (cosθ,sinθ) → svg (-sinθ,-cosθ).
            const yaw = o.yaw || 0;
            const lineLen = 14;
            const dx = -Math.sin(yaw) * lineLen;
            const dy = -Math.cos(yaw) * lineLen;
            const fill = isHeld ? '#22c55e' : '#f59e0b';
            return (
              <g
                key={o.tag_id}
                onPointerDown={(e) => startDrag(e, o.tag_id)}
                className="cursor-grab"
              >
                <rect
                  x={px - OBJECT_PX / 2}
                  y={py - OBJECT_PX / 2}
                  width={OBJECT_PX}
                  height={OBJECT_PX}
                  transform={`rotate(${(-yaw * 180) / Math.PI} ${px} ${py})`}
                  fill={fill}
                  stroke={isSel ? '#1d4ed8' : '#92400e'}
                  strokeWidth={isSel ? 2 : 1}
                  rx="2"
                />
                <line
                  x1={px}
                  y1={py}
                  x2={px + dx}
                  y2={py + dy}
                  stroke="#1f2937"
                  strokeWidth="1.5"
                />
                <text x={px + 9} y={py - 9} fontSize="9" fill="#475569">
                  {typeLabel(o.type)} #{o.tag_id}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      {/* Selected-object controls */}
      {selected ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--line)] bg-white px-3 py-2 text-xs">
          <span className="text-[var(--ink-3)]">
            Ausgewählt: <strong>{typeLabel(selected.type)} #{selected.tag_id}</strong>
          </span>
          <label className="flex items-center gap-1.5">
            Drehung:
            <input
              type="range"
              min={-180}
              max={180}
              step={5}
              value={selectedYawDeg}
              onChange={(e) => handleYaw(Number(e.target.value))}
              aria-label="Drehung des Objekts"
            />
            <span className="w-10 text-right font-mono">{selectedYawDeg}°</span>
          </label>
          <button
            type="button"
            onClick={handleDelete}
            className="ml-auto px-2 py-1 rounded-md border border-red-200 text-red-700 hover:bg-red-50"
          >
            Löschen
          </button>
        </div>
      ) : (
        <p className="text-xs text-[var(--ink-4)] px-1">
          Objekt-Typ wählen, dann in die grüne Reichweite tippen, um es zu
          platzieren. Tippe ein Objekt an, um es zu drehen oder zu verschieben.
        </p>
      )}

      {objects.length > 0 && (
        <button
          type="button"
          onClick={handleClear}
          className="self-start text-xs px-2 py-1 rounded-md border border-[var(--line)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]"
        >
          Tisch leeren
        </button>
      )}

      {/* 3D virtual-arm preview */}
      <div className="relative w-full aspect-video rounded-lg overflow-hidden border border-white/10 bg-[#1a1d23]">
        <Suspense
          fallback={
            <div className="w-full h-full flex items-center justify-center text-[12px] text-white/70">
              3D-Vorschau wird geladen …
            </div>
          }
        >
          <UrdfTwin
            jointTopic={SIM_JOINT_TOPIC}
            objects={objects}
            showTable
            heldObjectId={heldObjectId}
            onEndEffector={handleEndEffector}
          />
        </Suspense>
      </div>
    </div>
  );
}

export default SimScene;
