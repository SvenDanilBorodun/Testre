/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Roboter Studio — "Test im Simulator" view.
//
// Owns the whole simulator surface while the student is in simulator mode. Two
// halves, ONE UrdfTwin instance:
//   * a 2D top-down TABLE EDITOR — drag catalog objects onto the arm's reach
//     annulus (x,y base-frame plane), set each object's yaw, auto-assign a unique
//     tag_id; emits the Sim-Szene `{version:1, objects:[{type,tag_id,x,y,yaw}]}`
//     up to WorkshopPage via onChange (persisted in workflows.sim_scene on save,
//     sent in the /workflow/start payload's `sim` sibling by RunControls). Objects
//     size + colour PER TYPE from the `catalogDims` map (fallback amber cube), and
//     placement is CAPPED per type at `max_instances` (each object needs its own
//     tag_id — the sim silently skips extras server-side).
//   * a LAZY 3D preview (UrdfTwin) of the virtual follower replaying the
//     server's /sim/joint_states, with the placed objects on a virtual table.
//
// Layout: `layout="stack"` (default) is the original single-column view (the 3D
// pane in an aspect-video box below the editor) — byte-identical for any caller
// that does not opt in. `layout="split"` is the integrated sim stage: the 3D arm
// pane fills the top, the 2D editor scrolls below. Both render the SAME single
// UrdfTwin element (one WebGL context, one /sim/joint_states sub).
//
// Grasp-attach is computed HERE on the front end (the thin UrdfTwin renderer only
// surfaces the end-effector pose + gripper angle via onEndEffector): when the
// gripper crosses CLOSED and the end-effector is within a capture radius of an
// un-held object → that object is held (its mesh follows the gripper); on OPEN →
// released. No backend signal — the sim is logic+geometry, not physics.
//
// three.js is pulled in ONLY through the React.lazy import below, so this module
// (statically imported by the eagerly-loaded WorkshopPage) keeps three out of the
// entry bundle the white-screen CI greps. simConstants.js (the shared reach/colour
// constants) is dependency-free for the same reason.

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  Suspense,
  lazy,
} from 'react';
import toast from 'react-hot-toast';
import { useSelector } from 'react-redux';
import {
  reachAnnulus,
  REACH_INNER_M,
  REACH_OUTER_M,
  SIM_OBJECT_FALLBACK_SIZE_M,
  SIM_OBJECT_COLOR_HEX,
  SIM_OBJECT_HELD_COLOR_HEX,
} from './simConstants';
import { armGeometry } from '../../utils/armProfile';

const UrdfTwin = lazy(() => import('../UrdfTwin'));

// Sim-only virtual joint stream (never the bare /joint_states — see plan §C).
const SIM_JOINT_TOPIC = '/sim/joint_states';

// Strict-vertical reach annulus SHOWN TO THE STUDENT (the graspable table-top
// ring). PROFILE-DRIVEN (edu6 §4.5) via reachAnnulus(caps): OMX fallback
// 0.10–0.28 m, edu6 0.09–0.21 m. Objects must sit inside it to be graspable.
// The radii come from ./simConstants so the 2D SVG ring and the 3D twin's ring
// never desync — and so this is NOT the solver's wrist-centre span (module note).

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
// Fallback marker-square edge in px for a type WITHOUT catalog dims
// (SIM_OBJECT_FALLBACK_SIZE_M at PX_PER_M = 15 px). A type known to catalogDims
// sizes its square from width_m × PX_PER_M instead, so the 2D + 3D panes agree.
const OBJECT_PX = SIM_OBJECT_FALLBACK_SIZE_M * PX_PER_M;

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

// No-go ("Sperrzone") keep-out boxes (Phase-4). The editor draws an axis-aligned
// table rectangle and assigns a base-frame height band z0..z1 (metres). Rendered
// red (distinct from the amber objects). A drag with < MIN_ZONE_M extent on
// either axis is ignored (no accidental zero-size zone). Zone corners are NOT
// clamped to the reach annulus — a keep-out box need not be reachable.
const ZONE_Z0_DEFAULT = 0;
const ZONE_Z1_DEFAULT = 0.12;
const MIN_ZONE_M = 0.01;
const ZONE_FILL = 'rgba(239,68,68,0.15)';
const ZONE_DRAFT_FILL = 'rgba(239,68,68,0.12)';
const ZONE_STROKE = '#ef4444';

// Fallback palette when the catalog service momentarily returns nothing — keeps
// the simulator usable. The key MUST be a real type in the server's FIXED object
// set (`wuerfel`, object_catalog.py::_FIXED_CATALOG), else the server's
// recipe_for_type() raises and silently drops every placed object (the run then
// "finds nothing" with no explanation). `wuerfel` is always present.
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
// The annulus is PROFILE-DRIVEN (edu6 §4.5): callers pass reachAnnulus(caps);
// the default keeps the OMX constants for annulus-less call sites.
function clampToAnnulus(x, y, annulus) {
  const inner = annulus && annulus.inner ? annulus.inner : REACH_INNER_M;
  const outer = annulus && annulus.outer ? annulus.outer : REACH_OUTER_M;
  const r = Math.hypot(x, y);
  if (r === 0) return { x: inner, y: 0 };
  let rr = r;
  if (r < inner) rr = inner;
  else if (r > outer) rr = outer;
  if (rr === r) return { x, y };
  const k = rr / r;
  return { x: x * k, y: y * k };
}
// Clamp a scalar into [lo, hi]. Zone corners are clamped into the VIEW window
// (not the reach annulus) so a drawn rectangle stays on the visible table.
function clampRange(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}
function round3(v) {
  return Math.round(v * 1000) / 1000;
}

function SimScene({
  scene,
  onChange,
  catalog,
  catalogDims = {},
  layout = 'stack',
  showPath = false,
  pathClearToken = 0,
  showShadows = false,
  showReach = false,
}) {
  const objects = useMemo(
    () => (scene && Array.isArray(scene.objects) ? scene.objects : []),
    [scene],
  );
  // Profile geometry (edu6 §4.5): annulus radii + the grasp-classifier band.
  // caps==null / an OMX manifest resolve to the pre-edu6 literals exactly.
  const caps = useSelector((st) => (st.tasks && st.tasks.taskStatus ? st.tasks.taskStatus.capabilities : null));
  const annulus = useMemo(() => reachAnnulus(caps), [caps]);
  const graspBand = useMemo(() => {
    const geo = armGeometry(caps);
    if (geo.simCloseThresholdRad === null) {
      return { close: GRIPPER_CLOSED_RAD, open: GRIPPER_OPEN_RAD };
    }
    // Profile-supplied close threshold; re-open hysteresis sits halfway
    // between it and the profile's full-open command (edu6: 1.5 / 1.625).
    return {
      close: geo.simCloseThresholdRad,
      open: (geo.simCloseThresholdRad + geo.gripperOpenRad) / 2,
    };
  }, [caps]);
  const graspBandRef = useRef(graspBand);
  useEffect(() => { graspBandRef.current = graspBand; }, [graspBand]);
  const annulusRef = useRef(annulus);
  useEffect(() => { annulusRef.current = annulus; }, [annulus]);
  const zones = useMemo(
    () => (scene && Array.isArray(scene.zones) ? scene.zones : []),
    [scene],
  );
  const cat = Array.isArray(catalog) && catalog.length ? catalog : DEFAULT_CATALOG;

  // German label for a type key (declared BEFORE handlePlace so its placement-cap
  // toast can reference it — a useCallback deps entry is read at render time, so a
  // later `const` would be in the TDZ).
  const typeLabel = useMemo(() => {
    const m = new Map(cat.map(([label, value]) => [value, label]));
    return (value) => m.get(value) || value;
  }, [cat]);

  const [selectedType, setSelectedType] = useState(cat[0][1]);
  const [selectedId, setSelectedId] = useState(null);
  const [heldObjectId, setHeldObjectId] = useState(null);
  // Editor mode: place objects, or draw a no-go Sperrzone rectangle.
  const [mode, setMode] = useState('object'); // 'object' | 'zone'
  // Live drag-rectangle while drawing a zone, in base coords {x0,y0,x1,y1}.
  const [zoneDraft, setZoneDraft] = useState(null);

  const svgRef = useRef(null);
  const draggingIdRef = useRef(null);
  // Start corner of the in-progress zone draw (base coords) — a ref so the move
  // handler doesn't need to re-bind on every pointermove.
  const zoneStartRef = useRef(null);
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

  // Emit a MERGED scene: keep the current objects + zones and apply only the
  // given patch (`{objects}` or `{zones}`). Merging is required so an object edit
  // never clobbers the zones and vice-versa (Phase-4).
  const emit = useCallback(
    (patch) => {
      if (typeof onChange === 'function') {
        onChange({ version: 1, objects, zones, ...patch });
      }
    },
    [onChange, objects, zones],
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

  // Pointer-DOWN on the empty table → place the selected type (clamped into the
  // annulus). Bound to pointerdown, NOT click: an object's <g> stops its own
  // pointerdown from bubbling here, so pressing an existing object selects/drags
  // it and never reaches this handler — fixes the duplicate-on-select bug where
  // pointerup cleared draggingIdRef before the synthesized click ran (#B1).
  const handlePlace = useCallback(
    (e) => {
      // Object-placement is gated to object mode (zone mode draws a rectangle).
      if (mode !== 'object') return;
      if (draggingIdRef.current !== null) return;
      const base = eventToBase(e);
      if (!base) return;
      // Placement cap: the sim can only detect as many objects of a type as it
      // has tag_ids (catalogDims[type].max_instances); placing more would render
      // objects the program silently never finds (the server skips extras with a
      // container-log-only warning). Refuse in German instead. No cap when the
      // map/type/max is absent (today's behaviour).
      const dims = catalogDims && catalogDims[selectedType];
      const max = dims && Number.isFinite(dims.max_instances) ? dims.max_instances : null;
      if (max !== null) {
        const count = objects.filter((o) => o.type === selectedType).length;
        if (count >= max) {
          toast.error(
            `Höchstens ${max} × „${typeLabel(selectedType)}" im Simulator — `
            + 'jedes Objekt braucht eine eigene Tag-ID.',
          );
          return;
        }
      }
      const { x, y } = clampToAnnulus(base.x, base.y, annulusRef.current);
      const nextTag = objects.length
        ? Math.max(...objects.map((o) => (typeof o.tag_id === 'number' ? o.tag_id : -1))) + 1
        : 0;
      const next = [
        ...objects,
        { type: selectedType, tag_id: nextTag, x: round3(x), y: round3(y), yaw: 0 },
      ];
      emit({ objects: next });
      setSelectedId(nextTag);
    },
    [mode, eventToBase, objects, selectedType, emit, catalogDims, typeLabel],
  );

  const startDrag = useCallback((e, id) => {
    // In zone mode let the pointerdown bubble to the SVG so a zone can be drawn
    // starting from on top of an object (don't grab/move the object).
    if (mode !== 'object') return;
    e.stopPropagation();
    draggingIdRef.current = id;
    setSelectedId(id);
    if (svgRef.current && typeof svgRef.current.setPointerCapture === 'function') {
      try { svgRef.current.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    }
  }, [mode]);

  const handlePointerMove = useCallback(
    (e) => {
      const id = draggingIdRef.current;
      if (id === null) return;
      const base = eventToBase(e);
      if (!base) return;
      const { x, y } = clampToAnnulus(base.x, base.y, annulusRef.current);
      emit({
        objects: objects.map((o) =>
          o.tag_id === id ? { ...o, x: round3(x), y: round3(y) } : o,
        ),
      });
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
      emit({
        objects: objects.map((o) =>
          o.tag_id === selectedId ? { ...o, yaw: round3(yaw) } : o,
        ),
      });
    },
    [selectedId, objects, emit],
  );

  const handleDelete = useCallback(() => {
    if (selectedId === null) return;
    emit({ objects: objects.filter((o) => o.tag_id !== selectedId) });
    setSelectedId(null);
  }, [selectedId, objects, emit]);

  const handleClear = useCallback(() => {
    if (!objects.length) return;
    emit({ objects: [] });
    setSelectedId(null);
  }, [objects, emit]);

  // ---- No-go zone editing (Phase-4) ----------------------------------------
  // Leaving zone mode (or losing the start corner) drops any in-progress draft.
  useEffect(() => {
    if (mode !== 'zone') {
      zoneStartRef.current = null;
      setZoneDraft(null);
    }
  }, [mode]);

  // Per-zone height-band edit (z0 = floor, z1 = ceiling), keyed by list index.
  const handleZoneHeight = useCallback(
    (index, which, value) => {
      if (!Number.isFinite(value)) return;
      emit({
        zones: zones.map((z, i) => {
          if (i !== index) return z;
          const min = Array.isArray(z.min) ? [...z.min] : [0, 0, 0];
          const max = Array.isArray(z.max) ? [...z.max] : [0, 0, 0];
          // Preserve min[2] <= max[2] so a student typing „von" > „bis" doesn't
          // produce min[2] > max[2] — the server normalizes it but the cloud
          // validator REJECTS it on save (400), failing the whole sim_scene save.
          if (which === 'z0') min[2] = round3(Math.min(value, max[2]));
          else max[2] = round3(Math.max(value, min[2]));
          return { ...z, min, max };
        }),
      });
    },
    [zones, emit],
  );

  const handleZoneDelete = useCallback(
    (index) => {
      emit({ zones: zones.filter((_, i) => i !== index) });
    },
    [zones, emit],
  );

  // Combined SVG pointer handlers. In object mode they delegate to the existing
  // place/drag handlers; in zone mode they draw a drag-rectangle.
  const handleSvgPointerDown = useCallback(
    (e) => {
      if (mode === 'zone') {
        const base = eventToBase(e);
        if (!base) return;
        const x = clampRange(base.x, VIEW_MIN_X, VIEW_MAX_X);
        const y = clampRange(base.y, VIEW_MIN_Y, VIEW_MAX_Y);
        zoneStartRef.current = { x, y };
        setZoneDraft({ x0: x, y0: y, x1: x, y1: y });
        if (svgRef.current && typeof svgRef.current.setPointerCapture === 'function') {
          try { svgRef.current.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
        }
        return;
      }
      handlePlace(e);
    },
    [mode, eventToBase, handlePlace],
  );

  const handleSvgPointerMove = useCallback(
    (e) => {
      if (mode === 'zone') {
        if (!zoneStartRef.current) return;
        const base = eventToBase(e);
        if (!base) return;
        const x = clampRange(base.x, VIEW_MIN_X, VIEW_MAX_X);
        const y = clampRange(base.y, VIEW_MIN_Y, VIEW_MAX_Y);
        setZoneDraft((d) => (d ? { ...d, x1: x, y1: y } : d));
        return;
      }
      handlePointerMove(e);
    },
    [mode, eventToBase, handlePointerMove],
  );

  const handleSvgPointerUp = useCallback(() => {
    if (mode === 'zone') {
      const draft = zoneDraft;
      zoneStartRef.current = null;
      setZoneDraft(null);
      if (!draft) return;
      const x0 = Math.min(draft.x0, draft.x1);
      const x1 = Math.max(draft.x0, draft.x1);
      const y0 = Math.min(draft.y0, draft.y1);
      const y1 = Math.max(draft.y0, draft.y1);
      // Ignore a click / tiny drag — no accidental zero-size zone.
      if (x1 - x0 < MIN_ZONE_M || y1 - y0 < MIN_ZONE_M) return;
      const zone = {
        min: [round3(x0), round3(y0), ZONE_Z0_DEFAULT],
        max: [round3(x1), round3(y1), round3(ZONE_Z1_DEFAULT)],
      };
      emit({ zones: [...zones, zone] });
      return;
    }
    handlePointerUp();
  }, [mode, zoneDraft, zones, emit, handlePointerUp]);

  // Grasp-attach geometry. Stable callback (reads refs) so UrdfTwin's
  // onEndEffector prop identity never churns.
  const handleEndEffector = useCallback(({ x, y, gripper }) => {
    if (typeof gripper !== 'number' || !Number.isFinite(gripper)) return;
    const g = graspRef.current;
    if (gripper < graspBandRef.current.close && !g.closed) {
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
    } else if (gripper > graspBandRef.current.open && g.closed) {
      g.closed = false;
      if (heldRef.current !== null) {
        heldRef.current = null;
        setHeldObjectId(null);
      }
    }
  }, []);

  const selected = objects.find((o) => o.tag_id === selectedId) || null;
  const selectedYawDeg = selected
    ? Math.round(((selected.yaw || 0) * 180) / Math.PI)
    : 0;

  // Live zone-draw preview rectangle in SVG pixels. baseToSvg flips both axes, so
  // take the min corner + |Δ| for the SVG <rect> origin/size.
  const draftRect = zoneDraft
    ? (() => {
        const a = baseToSvg(zoneDraft.x0, zoneDraft.y0);
        const b = baseToSvg(zoneDraft.x1, zoneDraft.y1);
        return {
          x: Math.min(a.px, b.px),
          y: Math.min(a.py, b.py),
          w: Math.abs(a.px - b.px),
          h: Math.abs(a.py - b.py),
        };
      })()
    : null;

  // The 2D editor body (notice + palette + mode + table + selection + zones),
  // shared by BOTH layouts so they never drift. A fragment adds no DOM node, so
  // the default STACK layout below stays byte-identical to the previous markup.
  const editorBody = (
    <>
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

      {/* Editor mode toggle: place objects vs. draw a no-go Sperrzone. */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-[var(--ink-3)]">Modus:</span>
        <button
          type="button"
          onClick={() => setMode('object')}
          aria-pressed={mode === 'object'}
          className={
            'px-2.5 py-1 text-xs rounded-md border '
            + (mode === 'object'
              ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
              : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
          }
        >
          Objekte platzieren
        </button>
        <button
          type="button"
          onClick={() => setMode('zone')}
          aria-pressed={mode === 'zone'}
          className={
            'px-2.5 py-1 text-xs rounded-md border '
            + (mode === 'zone'
              ? 'bg-red-500 text-white border-red-500'
              : 'bg-white text-red-700 border-red-200 hover:bg-red-50')
          }
        >
          Sperrzone zeichnen
        </button>
        {mode === 'zone' && (
          <span className="text-xs text-[var(--ink-4)]">
            Ziehe ein Rechteck auf — der Roboter fährt um Sperrzonen herum.
          </span>
        )}
      </div>

      {/* 2D top-down table editor */}
      <div className="rounded-lg border border-[var(--line)] bg-white p-2">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${SVG_W} ${SVG_H}`}
          className="w-full h-auto rounded-md bg-[var(--bg-sunk)] touch-none cursor-crosshair"
          onPointerDown={handleSvgPointerDown}
          onPointerMove={handleSvgPointerMove}
          onPointerUp={handleSvgPointerUp}
          onPointerLeave={handleSvgPointerUp}
          role="application"
          aria-label="Simulator-Tisch — Objekte und Sperrzonen platzieren"
        >
          {/* Reach annulus (graspable ring) */}
          <circle
            cx={ORIGIN_PX}
            cy={ORIGIN_PY}
            r={annulus.outer * PX_PER_M}
            fill="rgba(34,197,94,0.08)"
            stroke="#22c55e"
            strokeWidth="1"
            strokeDasharray="4 3"
          />
          <circle
            cx={ORIGIN_PX}
            cy={ORIGIN_PY}
            r={annulus.inner * PX_PER_M}
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

          {/* No-go zones (top-down footprint, drawn under the objects) */}
          {zones.map((z, i) => {
            if (!Array.isArray(z.min) || !Array.isArray(z.max)) return null;
            const a = baseToSvg(z.min[0], z.min[1]);
            const b = baseToSvg(z.max[0], z.max[1]);
            const x = Math.min(a.px, b.px);
            const y = Math.min(a.py, b.py);
            const w = Math.abs(a.px - b.px);
            const h = Math.abs(a.py - b.py);
            return (
              <g key={`zone-${i}`}>
                <rect
                  x={x}
                  y={y}
                  width={w}
                  height={h}
                  fill={ZONE_FILL}
                  stroke={ZONE_STROKE}
                  strokeWidth="1.5"
                  strokeDasharray="5 3"
                />
                <text x={x + 3} y={y + 11} fontSize="9" fill="#b91c1c">
                  Sperrzone {i + 1}
                </text>
              </g>
            );
          })}

          {/* Live zone-draw preview */}
          {draftRect && (
            <rect
              x={draftRect.x}
              y={draftRect.y}
              width={draftRect.w}
              height={draftRect.h}
              fill={ZONE_DRAFT_FILL}
              stroke={ZONE_STROKE}
              strokeWidth="1.5"
              strokeDasharray="3 2"
              pointerEvents="none"
            />
          )}

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
            // Per-type size + colour from the catalog (fallback amber cube), so the
            // 2D square matches the 3D box the same object renders as.
            const dims = catalogDims && catalogDims[o.type];
            const sizePx = dims && Number.isFinite(dims.width_m) && dims.width_m > 0
              ? dims.width_m * PX_PER_M
              : OBJECT_PX;
            const restFill = dims && typeof dims.color === 'string' && dims.color
              ? dims.color
              : SIM_OBJECT_COLOR_HEX;
            const fill = isHeld ? SIM_OBJECT_HELD_COLOR_HEX : restFill;
            return (
              <g
                key={o.tag_id}
                onPointerDown={(e) => startDrag(e, o.tag_id)}
                className="cursor-grab"
              >
                <rect
                  x={px - sizePx / 2}
                  y={py - sizePx / 2}
                  width={sizePx}
                  height={sizePx}
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

      {/* No-go zone list — per-zone height band + delete. */}
      {zones.length > 0 && (
        <div className="flex flex-col gap-1.5 rounded-md border border-red-200 bg-white px-3 py-2 text-xs">
          <span className="font-medium text-red-700">Sperrzonen</span>
          {zones.map((z, i) => {
            const z0 = Array.isArray(z.min) && typeof z.min[2] === 'number' ? z.min[2] : 0;
            const z1 = Array.isArray(z.max) && typeof z.max[2] === 'number' ? z.max[2] : 0;
            return (
              <div key={`zone-row-${i}`} className="flex flex-wrap items-center gap-2">
                <span className="text-[var(--ink-3)]">Zone {i + 1}</span>
                <label className="flex items-center gap-1">
                  Höhe von:
                  <input
                    type="number"
                    step={0.01}
                    value={z0}
                    onChange={(e) => handleZoneHeight(i, 'z0', Number(e.target.value))}
                    className="w-16 rounded border border-[var(--line)] px-1 py-0.5 font-mono"
                    aria-label={`Untere Höhe der Sperrzone ${i + 1}`}
                  />
                  m
                </label>
                <label className="flex items-center gap-1">
                  bis:
                  <input
                    type="number"
                    step={0.01}
                    value={z1}
                    onChange={(e) => handleZoneHeight(i, 'z1', Number(e.target.value))}
                    className="w-16 rounded border border-[var(--line)] px-1 py-0.5 font-mono"
                    aria-label={`Obere Höhe der Sperrzone ${i + 1}`}
                  />
                  m
                </label>
                <button
                  type="button"
                  onClick={() => handleZoneDelete(i)}
                  className="ml-auto px-2 py-1 rounded-md border border-red-200 text-red-700 hover:bg-red-50"
                >
                  Löschen
                </button>
              </div>
            );
          })}
        </div>
      )}

    </>
  );

  // The 3D virtual-arm pane. In split mode it fills the available height (no
  // aspect-video box); the stack className is byte-identical to the previous
  // single-layout markup. ONE UrdfTwin instance in both layouts → one WebGL
  // context + one /sim/joint_states subscription.
  const previewClass = layout === 'split'
    ? 'relative w-full flex-1 min-h-[220px] rounded-lg overflow-hidden border border-white/10 bg-[#1a1d23]'
    : 'relative w-full aspect-video rounded-lg overflow-hidden border border-white/10 bg-[#1a1d23]';
  const preview = (
    <div className={previewClass}>
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
          zones={zones}
          showTable
          heldObjectId={heldObjectId}
          onEndEffector={handleEndEffector}
          catalogDims={catalogDims}
          showPath={showPath}
          pathClearToken={pathClearToken}
          showShadows={showShadows}
          showReach={showReach}
        />
      </Suspense>
    </div>
  );

  // SPLIT: 3D arm pane on top (fills), the 2D editor below (scrolls if needed).
  // STACK (default): the previous single-column DOM, byte-identical.
  if (layout === 'split') {
    return (
      <div className="flex h-full flex-col gap-3">
        {preview}
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto">
          {editorBody}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {editorBody}
      {preview}
    </div>
  );
}

export default SimScene;
