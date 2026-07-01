/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useState, useCallback, useRef, Suspense, lazy } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import * as Blockly from 'blockly/core';
import CalibrationWizard from '../components/Workshop/CalibrationWizard';
import LeaderToggle from '../components/Workshop/LeaderToggle';
import BlocklyWorkspace from '../components/Workshop/BlocklyWorkspace';
import RunControls from '../components/Workshop/RunControls';
import CameraFeedOverlay from '../components/Workshop/CameraFeedOverlay';
import SimScene from '../components/Workshop/SimScene';
import TemplatePicker from '../components/Workshop/TemplatePicker';
import ToolbarButtons from '../components/Workshop/ToolbarButtons';
import DebugPanel from '../components/Workshop/DebugPanel';
import GalleryTab from '../components/Workshop/GalleryTab';
import SkillmapPlayer from '../components/Workshop/SkillmapPlayer';
import VersionHistoryDropdown from '../components/Workshop/VersionHistoryDropdown';
import JogPanel from '../components/Workshop/JogPanel';
import RecordPanel from '../components/Workshop/RecordPanel';
import {
  applyPinnedCoordinates,
  setDriveToHandler,
} from '../components/Workshop/blocks/destinations';
import { useAutosave } from '../components/Workshop/useAutosave';
import {
  setUnsavedBlocklyJson,
  setSelectedWorkflowId,
  markWorkflowSaved,
  requestRecalibration,
} from '../features/workshop/workshopSlice';
import { useRosTopicSubscription } from '../hooks/useRosTopicSubscription';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import {
  setObjectCatalogOptions,
  setWorkspaceAccessor,
} from '../components/Workshop/blocks/perception';
import {
  getWorkflow,
  createWorkflow,
  updateWorkflow,
} from '../services/workflowApi';

// Phase-5: live 3D follower twin in the real-mode editor (stacked below the scene
// camera). LAZY-loaded so three.js (~600 KB) + urdf-loader stay out of the entry
// bundle the white-screen CI greps (same idiom as RecordPage + SimScene). Mounts
// only while the „3D-Ansicht" panel is open; full WebGL teardown on collapse.
const UrdfTwin = lazy(() => import('../components/UrdfTwin'));

// Phase-2: read-only „Code"-Vorschau (the Blockly program rendered as real
// Python). LAZY-loaded so the Python generator (`blockly/python`, a separate
// async chunk pulled in by CodeView → pythonCodeGen) stays out of the entry
// bundle; mounts only while the „Code"-panel is open.
const CodeView = lazy(() => import('../components/Workshop/CodeView'));

// The student's „3D-Ansicht" open/closed choice persists across reloads. Distinct
// key from RecordPage's `edubotics_urdf_open` so the two panels are independent.
const WORKSHOP_URDF_OPEN_KEY = 'edubotics_workshop_urdf_open';
// The „Code anzeigen" open/closed choice persists across reloads too.
const WORKSHOP_CODE_OPEN_KEY = 'edubotics_workshop_code_open';

// Mirror the BACKEND validator (_DESTINATION_NAME_RE, handlers/destinations.py):
// letters (incl. ä ö ü ß), digits, space, underscore, hyphen — used by
// „Position merken". Capped at 24 (NOT the backend's 40) to MATCH the
// destination_pin / destination_ref block NAME field (destinations.js
// NAME_MAX_LEN=24): a captured point is typed by name into those blocks, and a
// 25–40-char name would be truncated to 24 there → „Ziel nicht gefunden" at run
// time. 24 is a safe subset of the backend's 1..40 range.
const CAPTURE_NAME_MAX_LEN = 24;
const CAPTURE_NAME_RE = /^[A-Za-zÄÖÜäöüß0-9 _-]{1,24}$/;
function sanitizeDestinationName(raw) {
  if (typeof raw !== 'string') return '';
  const trimmed = raw.trim().slice(0, CAPTURE_NAME_MAX_LEN);
  if (trimmed === '' || trimmed === '—') return '';
  if (!CAPTURE_NAME_RE.test(trimmed)) return '';
  return trimmed;
}

// Phase-3 simulator fallback palette: an uncalibrated rig may have no
// object_catalog.json yet, so seed at least one type so the sim editor is usable.
// The key MUST match the server's seeded catalog type (`beispiel`,
// object_catalog.py) — else the server drops every placed object silently.
const SIM_DEFAULT_CATALOG = [['Beispiel-Objekt', 'beispiel']];
// Phase-4: `zones` is a first-class key alongside `objects` (no-go Sperrzonen).
// hydrate/save round-trip the whole sim_scene unchanged, so persisted zones ride
// along in workflows.sim_scene.zones.
const EMPTY_SIM_SCENE = { version: 1, objects: [], zones: [] };

function WorkshopPage({ isActive }) {
  const dispatch = useDispatch();
  // WS4 (2026-06-17): scene-cam-only calibration. The gripper camera is no
  // longer calibrated (not modelled in the omx_f URDF; unused at runtime), so
  // the editor-unlock gate reads only scene flags. The gripper flags are
  // intentionally NOT read here.
  // 2026-06-22: the colour-profile step was dropped — the editor unlocks after
  // the 3 geometry steps (intrinsic + extrinsic + table touch-off). The grasp
  // path needs only those three; the colour profile gated nothing else.
  const hasIntrinsicScene = useSelector((s) => s.workshop.hasIntrinsicScene);
  const hasHandeyeScene = useSelector((s) => s.workshop.hasHandeyeScene);
  const hasTableTouch = useSelector((s) => s.workshop.hasTableTouch);
  const recalibrating = useSelector((s) => s.workshop.recalibrating);
  // W5: holds the editor closed only while the student is on the optional
  // „Genauigkeit prüfen" step right after the touch-off (defaults false on a
  // page reload, so a calibrated rig opens straight to the editor).
  const pendingVerify = useSelector((s) => s.workshop.pendingVerify);
  const selectedWorkflowId = useSelector((s) => s.workshop.selectedWorkflowId);
  const unsavedBlocklyJson = useSelector((s) => s.workshop.unsavedBlocklyJson);
  const accessToken = useSelector((s) => s.auth?.session?.access_token);
  // Audit fix: prior path `s.auth?.user?.id` was always null (no
  // top-level `user` field on the auth slice). The Supabase user lives
  // under `session.user.id`. Without this fix the autosave scopeKey
  // was the same string for every student on a shared browser →
  // cross-student autosave bleed.
  const userId = useSelector((s) => s.auth?.session?.user?.id || null);
  const debuggerVisible = useSelector((s) => s.workshop.debuggerVisible);
  const restrictedBlocks = useSelector((s) => s.workshop.restrictedBlocks);
  const activeTutorialId = useSelector((s) => s.workshop.activeTutorialId);

  const [editorJson, setEditorJson] = useState(null);
  const [initialJsonForEditor, setInitialJsonForEditor] = useState(null);
  const [editorKey, setEditorKey] = useState(0);
  const [workspace, setWorkspace] = useState(null);
  const [saving, setSaving] = useState(false);
  const [view, setView] = useState('editor'); // 'editor' | 'gallery'
  // Phase-3 "Test im Simulator". simMode opens the editor without calibration and
  // swaps the live scene camera for the SimScene editor + virtual-arm preview.
  const [simMode, setSimMode] = useState(false);
  const [simScene, setSimScene] = useState(EMPTY_SIM_SCENE);
  // #B2: leaving simMode while a sim run is active would unmount RunControls (and
  // its Stop button) on an uncalibrated rig, stranding the run. Lock the toggle
  // while a sim run is in flight (mirrors LeaderToggle's run-guard).
  const runState = useSelector((s) => s.workshop.runState);
  const simRunActive = simMode && runState === 'running';
  // [label_de, type_name] pairs from GetObjectCatalog, threaded to SimScene's
  // object palette (the same source the Blockly dropdowns use).
  const [objectCatalog, setObjectCatalog] = useState([]);
  // Batch 2b: rosbridge liveness gates the real-arm jog/record panels (the same
  // signal the rest of the app uses for „Roboter verbunden").
  const heartbeatStatus = useSelector((s) => s.tasks?.heartbeatStatus);
  // Batch 2b: RecordPanel reports whether a hand-guide recording is in flight
  // (lifted here) so we can disable JogPanel + the „fahre dorthin" drive-to while
  // the arm is being hand-guided — a driven move would fight the student's hand.
  const [recordPanelRecording, setRecordPanelRecording] = useState(false);
  // Batch 2b: JogPanel reports whether a hand-guide (torque-off) session is open
  // (lifted here, like recordPanelRecording) so we can disable RecordPanel while
  // the arm is limp — one shared truth, so neither panel shows a stale
  // „freigeschaltet"/disabled state after the sibling closes the session.
  const [jogHandGuideOn, setJogHandGuideOn] = useState(false);
  const subscriptions = useRosTopicSubscription();
  const { getObjectCatalog, capturePose, jogArm } = useRosServiceCaller();
  const workspaceRef = useRef(null);
  // Blockly 12 ties getSelected() to the FocusManager, and clicking the
  // camera overlay (a non-focusable div) blurs the block in Chromium →
  // selection is null by the time the camera onClick runs. So we remember
  // the id of the most-recently selected destination_pin block via a
  // SELECTED change-listener and use THAT at mark time, not live selection.
  const lastPinBlockIdRef = useRef(null);

  // Phase-5: real-mode 3D follower twin panel. Default COLLAPSED (the scene
  // camera stays primary); the open/closed choice persists in localStorage.
  const [isUrdfOpen, setIsUrdfOpen] = useState(() => {
    try {
      return typeof window !== 'undefined'
        && window.localStorage.getItem(WORKSHOP_URDF_OPEN_KEY) === '1';
    } catch (_) {
      return false;
    }
  });
  const toggleUrdf = useCallback(() => {
    setIsUrdfOpen((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(WORKSHOP_URDF_OPEN_KEY, next ? '1' : '0');
      } catch (_) { /* private mode / quota — ignore */ }
      return next;
    });
  }, []);

  // Phase-2: read-only Python „Code"-Vorschau panel. Default COLLAPSED; the
  // open/closed choice persists in localStorage (distinct key from 3D-Ansicht).
  const [isCodeOpen, setIsCodeOpen] = useState(() => {
    try {
      return typeof window !== 'undefined'
        && window.localStorage.getItem(WORKSHOP_CODE_OPEN_KEY) === '1';
    } catch (_) {
      return false;
    }
  });
  const toggleCode = useCallback(() => {
    setIsCodeOpen((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(WORKSHOP_CODE_OPEN_KEY, next ? '1' : '0');
      } catch (_) { /* private mode / quota — ignore */ }
      return next;
    });
  }, []);

  // Phase-2: „Position merken" — capture the follower's current pose as a named
  // destination via /workshop/capture_pose. Does NOT drive the arm; the named
  // point is then usable by „Ziel <Name>" / „bewege zu".
  const [capturing, setCapturing] = useState(false);
  const handleCapturePose = useCallback(async () => {
    if (typeof window === 'undefined') return;
    const raw = window.prompt('Name für die gemerkte Position:', '');
    if (raw === null) return; // student cancelled the prompt
    const name = sanitizeDestinationName(raw);
    if (!name) {
      toast.error(
        'Bitte einen gültigen Namen verwenden '
        + '(Buchstaben, Zahlen, Leerzeichen, _ und -).',
      );
      return;
    }
    setCapturing(true);
    try {
      const res = await capturePose(name);
      if (res && res.success) {
        const x = Number(res.world_x || 0).toFixed(3);
        const y = Number(res.world_y || 0).toFixed(3);
        const z = Number(res.world_z || 0).toFixed(3);
        toast.success(
          `Position „${name}" gemerkt (x=${x}, y=${y}, z=${z}). `
          + 'Du kannst sie jetzt mit „Ziel ' + name + '" verwenden.',
        );
      } else {
        toast.error(
          (res && res.message)
            ? res.message
            : 'Position konnte nicht gemerkt werden.',
        );
      }
    } catch (e) {
      toast.error(`Position konnte nicht gemerkt werden: ${e.message || e}`);
    } finally {
      setCapturing(false);
    }
  }, [capturePose]);
  // Twin overlays: the end-effector path trail + base/TCP coordinate triads.
  const [showPath, setShowPath] = useState(false);
  const [showFrames, setShowFrames] = useState(false);
  // Bumped to clear the path trail (manual „Bahn löschen" + auto on run start).
  const [pathClearToken, setPathClearToken] = useState(0);
  // Auto-clear the trail when a run begins, so each run draws a fresh path.
  const prevRunStateRef = useRef(runState);
  useEffect(() => {
    if (runState === 'running' && prevRunStateRef.current !== 'running') {
      setPathClearToken((t) => t + 1);
    }
    prevRunStateRef.current = runState;
  }, [runState]);

  const calibrated =
    hasIntrinsicScene &&
    hasHandeyeScene &&
    hasTableTouch;

  // The editor unlocks only when calibrated AND not mid-recalibration AND not
  // holding for the optional accuracy-verify step. The `recalibrating` override
  // (set by „Kalibrierung neu starten") keeps the wizard open even though the
  // on-disk YAMLs still satisfy `calibrated` — it clears once the student
  // finishes re-running the steps. `pendingVerify` (set right after the
  // touch-off, cleared by „Fertig"/„Überspringen") routes the student through
  // the optional „Genauigkeit prüfen" step before the editor opens; it is NOT
  // part of `calibrated`, so a calibrated rig still opens straight to the
  // editor on a fresh page load (workshopSlice).
  // Phase-3: the simulator needs no calibration (real interpreter + real IK on a
  // VIRTUAL arm — plan §WS-D), so simMode opens the editor regardless.
  const showEditor = simMode || (calibrated && !recalibrating && !pendingVerify);

  // Re-subscribe to /workflow/status whenever this page is active OR
  // the rosbridge connection state flips back to connected. The v1
  // ship subscribed once on mount and lost the feed after any
  // rosbridge reconnect (audit §3.8). The hook's subscribe call is
  // re-entrant: it tears down a stale topic before binding a new one.
  useEffect(() => {
    if (!isActive) return undefined;
    if (subscriptions && typeof subscriptions.subscribeToWorkflowStatus === 'function') {
      subscriptions.subscribeToWorkflowStatus();
    }
    if (subscriptions && typeof subscriptions.subscribeToWorkflowSensors === 'function') {
      subscriptions.subscribeToWorkflowSensors();
    }
    return undefined;
  }, [isActive, subscriptions, subscriptions?.connected]);

  // Hydrate the editor with: (a) selected cloud workflow, then
  // (b) unsavedBlocklyJson from Redux, then (c) blank workspace.
  // Bumping editorKey forces BlocklyWorkspace to remount so initialJson
  // is re-applied — Blockly only consumes initialJson on mount.
  useEffect(() => {
    let cancelled = false;
    if (!isActive) return undefined;
    if (selectedWorkflowId && accessToken) {
      getWorkflow(accessToken, selectedWorkflowId)
        .then((w) => {
          if (cancelled) return;
          setInitialJsonForEditor(w?.blockly_json || null);
          // Hydrate the persisted Sim-Szene (workflows.sim_scene); fall back to
          // an empty scene when absent/empty so a non-sim workflow clears it.
          setSimScene(
            w?.sim_scene && Array.isArray(w.sim_scene.objects)
              ? w.sim_scene
              : EMPTY_SIM_SCENE,
          );
          setEditorKey((k) => k + 1);
        })
        .catch((e) => {
          if (cancelled) return;
          toast.error(`Workflow konnte nicht geladen werden: ${e.message || e}`);
          setInitialJsonForEditor(unsavedBlocklyJson || null);
          setEditorKey((k) => k + 1);
        });
      return () => { cancelled = true; };
    }
    setInitialJsonForEditor(unsavedBlocklyJson || null);
    setEditorKey((k) => k + 1);
    return () => { cancelled = true; };
    // unsavedBlocklyJson intentionally omitted from deps: we only want
    // to seed once per workflow-id change. The change-listener inside
    // BlocklyWorkspace keeps Redux in sync after that.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActive, selectedWorkflowId, accessToken]);

  const handleEditorChange = useCallback(
    (json) => {
      setEditorJson(json);
      dispatch(setUnsavedBlocklyJson(json));
    },
    [dispatch]
  );

  const handleWorkspaceReady = useCallback((ws) => {
    workspaceRef.current = ws;
    setWorkspace(ws);
    // ws is null on workspace teardown — drop the remembered pin so a stale
    // id can't survive a remount/editorKey bump.
    if (!ws) {
      lastPinBlockIdRef.current = null;
      return;
    }
    // Record the last-selected destination_pin so the camera click can find
    // it after the click blurs Blockly's selection. On a deselect
    // (newElementId empty) we intentionally KEEP the remembered pin — that
    // deselect is exactly the camera-click blur we must survive.
    ws.addChangeListener((e) => {
      if (e.type !== Blockly.Events.SELECTED) return;
      const id = e.newElementId;
      if (!id) return; // deselect: keep the last remembered pin
      const block = ws.getBlockById(id);
      if (block && block.type === 'edubotics_destination_pin') {
        lastPinBlockIdRef.current = id;
      }
    });
  }, []);

  // Wire the workspace accessor so a late object-catalog refresh can reach the
  // live OBJECT_TYPE dropdowns (perception.js refreshObjectTypeDropdowns).
  useEffect(() => {
    setWorkspaceAccessor(() => workspaceRef.current);
    return () => setWorkspaceAccessor(null);
  }, []);

  // Batch 2b: the per-block „fahre dorthin" button (destinations.js) bridges to
  // this handler — it drives the REAL follower to the block's pinned point via
  // /workshop/jog (mode 'drive_to'). Refuse in the simulator (no real arm) and
  // while a program runs; an un-pinned point reads as NaN → German hint.
  useEffect(() => {
    setDriveToHandler(async ({ name, x, y, z }) => {
      if (simMode) {
        toast.error('Im Simulator kann der echte Roboter nicht gefahren werden.');
        return;
      }
      // Refuse a driven move on a disconnected robot (matches JogPanel/RecordPanel
      // gating) — without this the jog call silently no-ops "successfully".
      if (heartbeatStatus !== 'connected') {
        toast.error('Roboter nicht verbunden.');
        return;
      }
      if (runState === 'running') {
        toast.error('Während ein Programm läuft, ist das Fahren gesperrt.');
        return;
      }
      if (recordPanelRecording) {
        toast.error('Erst die Aufnahme beenden, dann fahren.');
        return;
      }
      // Refuse a driven move while the arm is hand-guided (limp — the student's
      // hand is on it in JogPanel „Arm freischalten"). Re-torquing + driving it
      // would fight the student's hand while JogPanel still shows „freigeschaltet".
      if (jogHandGuideOn) {
        toast.error(
          'Der Arm ist freigeschaltet — bitte zuerst festsetzen, '
          + 'bevor du ihn zu einem Punkt fährst.',
        );
        return;
      }
      if (![x, y, z].every((v) => Number.isFinite(v))) {
        toast.error(
          'Dieses Ziel wurde noch nicht gesetzt — klicke zuerst in die '
          + 'Szenen-Kamera, um die Koordinaten zu setzen.',
        );
        return;
      }
      const label = name || 'Ziel';
      if (typeof window !== 'undefined'
          && !window.confirm(
            `Roboter zu „${label}" fahren `
            + `(x=${x.toFixed(3)}, y=${y.toFixed(3)}, z=${z.toFixed(3)})?`)) {
        return;
      }
      try {
        const res = await jogArm({ mode: 'drive_to', target_x: x, target_y: y, target_z: z });
        if (res && res.success) {
          toast.success(`Fahre zu „${label}".`);
        } else {
          toast.error((res && res.message) || 'Fahrt nicht möglich.');
        }
      } catch (e) {
        toast.error(`Fahrt fehlgeschlagen: ${e.message || e}`);
      }
    });
    return () => setDriveToHandler(null);
  }, [jogArm, simMode, runState, recordPanelRecording, heartbeatStatus, jogHandGuideOn]);

  // Fetch the named-object catalog for the Blockly dropdowns once the editor is
  // available (calibrated). Re-fetch if the student calibrates in-session. The
  // GetObjectCatalog service reads the volume catalog at call time, so this also
  // picks up a teacher's catalog edit after an environment restart.
  // Phase-3: also fetch the catalog in simMode (even uncalibrated) so the
  // simulator object palette is populated; an empty/failed fetch seeds a default.
  useEffect(() => {
    if (!isActive || !(calibrated || simMode)) return undefined;
    let cancelled = false;
    getObjectCatalog()
      .then((r) => {
        if (cancelled) return;
        if (r && r.success && Array.isArray(r.type_names) && r.type_names.length) {
          const labels = Array.isArray(r.labels_de) ? r.labels_de : [];
          const pairs = r.type_names.map((t, i) => [labels[i] || t, t]);
          setObjectCatalogOptions(pairs);
          setObjectCatalog(pairs);
        } else if (simMode) {
          // Non-fatal in the simulator — keep the palette usable.
          setObjectCatalog(SIM_DEFAULT_CATALOG);
        } else if (r && r.message) {
          toast.error(r.message);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        console.warn('getObjectCatalog failed', e);
        if (simMode) setObjectCatalog(SIM_DEFAULT_CATALOG);
      });
    return () => {
      cancelled = true;
    };
  }, [isActive, calibrated, simMode, getObjectCatalog]);

  // TemplatePicker calls onPicked(workflowObject) — the full row, not
  // just the id. We extract the id and store it in Redux; the editor's
  // load effect picks up the change and hydrates initialJsonForEditor.
  const handlePickWorkflow = useCallback(
    (workflow) => {
      if (!workflow || !workflow.id) return;
      dispatch(setSelectedWorkflowId(workflow.id));
    },
    [dispatch]
  );

  // Click-to-pin handler: when the student clicks the scene camera and
  // a destination_pin block is selected, write the world coordinates
  // returned by /workshop/mark_destination into that block's X/Y/Z
  // fields. Without this, the destination_pin handler at runtime would
  // overwrite the click data with zeros (audit §1.4).
  const handleMarkDestination = useCallback(({ label, world_x, world_y, world_z }) => {
    const ws = workspaceRef.current;
    if (!ws) return;
    // Use the remembered pin id, not live selection — the camera click has
    // already blurred the block in Chromium (see lastPinBlockIdRef note).
    // getBlockById returns null for a deleted/disposed block, so this also
    // covers "selected a pin, then deleted it before clicking".
    const id = lastPinBlockIdRef.current;
    const block = id ? ws.getBlockById(id) : null;
    if (!block || block.type !== 'edubotics_destination_pin') {
      lastPinBlockIdRef.current = null;
      toast(
        'Tipp: Wähle zuerst einen "setze Ziel = Pin"-Block aus, '
        + 'dann klicke in die Szenen-Kamera, damit die Koordinaten '
        + `in den Block geschrieben werden. (Ziel "${label}" wurde `
        + 'serverseitig gespeichert, aber kein Block aktualisiert.)',
        { icon: '💡' },
      );
      return;
    }
    applyPinnedCoordinates(block, world_x, world_y, world_z);
    toast.success(`Koordinaten in Block „${block.getFieldValue('NAME') || label}" geschrieben.`);
  }, []);

  // Autosave hook. Restores the most-recent local state if the parent
  // hasn't already loaded a server workflow. Scoped per user so two
  // students sharing a browser don't see each other's drafts.
  const handleAutosaveRestore = useCallback(
    (state) => {
      if (selectedWorkflowId) return;  // server workflow takes precedence
      if (initialJsonForEditor) return;
      setInitialJsonForEditor(state);
      setEditorKey((k) => k + 1);
      dispatch(setUnsavedBlocklyJson(state));
    },
    [selectedWorkflowId, initialJsonForEditor, dispatch]
  );
  const { lastSavedAt } = useAutosave({
    workspace,
    enabled: isActive && (calibrated || simMode),
    scopeKey: userId,
    onRestore: handleAutosaveRestore,
  });

  const handleSave = useCallback(async () => {
    if (!accessToken) {
      toast.error('Nicht angemeldet — Speichern nicht möglich.');
      return;
    }
    const json = editorJson || unsavedBlocklyJson;
    if (!json) {
      toast.error('Workflow ist leer.');
      return;
    }
    setSaving(true);
    try {
      if (selectedWorkflowId) {
        await updateWorkflow(accessToken, selectedWorkflowId, {
          blockly_json: json,
          sim_scene: simScene,
        });
      } else {
        const created = await createWorkflow(accessToken, {
          name: 'Neuer Workflow',
          description: '',
          blockly_json: json,
          sim_scene: simScene,
        });
        if (created && created.id) {
          dispatch(setSelectedWorkflowId(created.id));
        }
      }
      dispatch(markWorkflowSaved());
      toast.success('Gespeichert.');
    } catch (e) {
      toast.error(`Speichern fehlgeschlagen: ${e.message || e}`);
    } finally {
      setSaving(false);
    }
  }, [accessToken, editorJson, unsavedBlocklyJson, selectedWorkflowId, simScene, dispatch]);

  if (!isActive) return null;

  return (
    <div className="flex flex-col h-full w-full overflow-hidden">
      <header className="px-4 sm:px-6 py-3 sm:py-4 border-b border-[var(--line)] bg-white">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div className="min-w-0">
            <h1 className="text-lg sm:text-xl font-semibold text-[var(--ink)]">
              Roboter Studio
            </h1>
            <p className="text-xs sm:text-sm text-[var(--ink-3)]">
              {showEditor
                ? 'Bausteine ziehen, Aufgabe zusammenstellen und vom Roboter ausführen lassen.'
                : 'Bevor wir loslegen können, muss die Kamera eingerichtet werden.'}
            </p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {/* Phase-3: open the editor on a virtual arm — no calibration, no
                real robot. The interpreter + IK run for real on a simulated
                arm; only logic, order and reachability are validated. */}
            <button
              type="button"
              onClick={() => { if (!simRunActive) setSimMode((v) => !v); }}
              aria-pressed={simMode}
              disabled={simRunActive}
              title={simRunActive
                ? 'Während ein Simulationslauf läuft, kann der Simulator nicht beendet werden — bitte zuerst stoppen.'
                : 'Programm auf einem virtuellen Roboter testen — ohne echten Roboter und ohne Kalibrierung'}
              className={
                'text-xs px-3 py-1.5 rounded-md border disabled:opacity-50 '
                + 'disabled:cursor-not-allowed '
                + (simMode
                  ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                  : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
              }
            >
              {simMode ? 'Simulator beenden' : 'Test im Simulator'}
            </button>
            {/* Follower-only leader toggle (Windows student rig only; self-hides
                on Jetson/cloud where the GUI control bridge is absent). */}
            <LeaderToggle isActive={isActive} />
          </div>
        </div>
      </header>
      <main className="flex-1 overflow-hidden flex flex-col">
        {showEditor ? (
          <>
            <div className="px-3 sm:px-4 pt-2 sm:pt-3 flex items-center gap-2 flex-wrap">
              <div className="flex items-center gap-1 rounded-md border border-[var(--line)] bg-white p-0.5">
                <button
                  type="button"
                  onClick={() => setView('editor')}
                  aria-pressed={view === 'editor'}
                  className={
                    'px-3 py-1 text-xs rounded '
                    + (view === 'editor'
                      ? 'bg-[var(--accent)] text-white'
                      : 'text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]')
                  }
                >
                  Editor
                </button>
                <button
                  type="button"
                  onClick={() => setView('gallery')}
                  aria-pressed={view === 'gallery'}
                  className={
                    'px-3 py-1 text-xs rounded '
                    + (view === 'gallery'
                      ? 'bg-[var(--accent)] text-white'
                      : 'text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]')
                  }
                >
                  Galerie
                </button>
              </div>
              <div className="flex-1 min-w-0">
                <TemplatePicker onPicked={handlePickWorkflow} />
              </div>
              {/* Audit U3: a calibrated student can re-enter the wizard
                  (e.g. cameras moved between sessions) without having
                  to wipe the named volume from a terminal. Resets
                  per-step badges; on-disk YAMLs survive and are
                  overwritten as the student re-runs each step. */}
              <button
                type="button"
                onClick={() => {
                  if (typeof window !== 'undefined'
                      && window.confirm('Kalibrierung neu starten? Die bisherigen Werte werden beim nächsten Schritt überschrieben.')) {
                    dispatch(requestRecalibration());
                  }
                }}
                className="text-xs px-2 py-1 rounded-md border border-[var(--line)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)]"
                title="Kalibrierung erneut durchlaufen — Schritt-Marker werden zurückgesetzt"
              >
                Kalibrierung neu starten
              </button>
            </div>
            {view === 'gallery' ? (
              <div className="flex-1 p-3 sm:p-4 overflow-auto">
                <GalleryTab
                  onPicked={(wf) => {
                    handlePickWorkflow(wf);
                    setView('editor');
                  }}
                />
              </div>
            ) : (
              <>
                <ToolbarButtons
                  workspace={workspace}
                  lastSavedAt={lastSavedAt}
                  onSave={handleSave}
                  saving={saving}
                  extra={
                    <VersionHistoryDropdown
                      workflowId={selectedWorkflowId}
                      onRestore={(updated) => {
                        if (updated && updated.blockly_json) {
                          setInitialJsonForEditor(updated.blockly_json);
                          setEditorKey((k) => k + 1);
                          dispatch(setUnsavedBlocklyJson(updated.blockly_json));
                        }
                      }}
                    />
                  }
                />
                <div
                  className={
                    'flex-1 flex flex-col gap-3 p-3 sm:p-4 overflow-auto '
                    + 'md:grid md:gap-4 md:overflow-hidden '
                    + (debuggerVisible || activeTutorialId
                      ? 'md:grid-cols-12'
                      : 'md:grid-cols-3')
                  }
                >
                  <div
                    className={
                      'bg-white rounded-lg border border-[var(--line)] '
                      + 'overflow-hidden min-h-[420px] '
                      + (debuggerVisible
                        ? 'md:col-span-6'
                        : activeTutorialId
                        ? 'md:col-span-7'
                        : 'md:col-span-2')
                    }
                  >
                    <BlocklyWorkspace
                      key={editorKey}
                      initialJson={initialJsonForEditor}
                      onChange={handleEditorChange}
                      onWorkspaceReady={handleWorkspaceReady}
                      restrictedBlocks={restrictedBlocks}
                    />
                  </div>
                  <div
                    className={
                      // md+: the column is a fixed-height grid cell. Opening the
                      // „3D-Ansicht" panel inserts an aspect-video block that can
                      // push SkillmapPlayer past the cell height; clip horizontally
                      // (as before) but let the column scroll vertically so nothing
                      // is hidden without a scrollbar. Mobile keeps overflow-auto.
                      'flex flex-col gap-3 overflow-auto '
                      + 'md:overflow-x-hidden md:overflow-y-auto '
                      + 'min-w-0 '
                      + (debuggerVisible
                        ? 'md:col-span-3'
                        : activeTutorialId
                        ? 'md:col-span-2'
                        : 'md:col-span-1')
                    }
                  >
                    {simMode ? (
                      <SimScene
                        scene={simScene}
                        onChange={setSimScene}
                        catalog={objectCatalog}
                      />
                    ) : (
                      <>
                        <CameraFeedOverlay
                          camera="scene"
                          clickable={true}
                          onMark={handleMarkDestination}
                        />
                        {/* Phase-2: capture the arm's CURRENT pose as a named
                            destination (does NOT drive the arm). The point is
                            then usable by „Ziel <Name>" / „bewege zu". */}
                        <div className="rounded-lg border border-[var(--line)] bg-white px-2 py-1.5 flex items-center gap-2 flex-wrap">
                          <button
                            type="button"
                            onClick={handleCapturePose}
                            disabled={capturing}
                            title="Aktuelle Roboterposition als benanntes Ziel speichern"
                            className={
                              'text-xs px-2.5 py-1 rounded-md border disabled:opacity-50 '
                              + 'disabled:cursor-not-allowed bg-[var(--accent)] text-white '
                              + 'border-[var(--accent)] hover:opacity-90'
                            }
                          >
                            {capturing ? 'Wird gemerkt …' : 'Position merken'}
                          </button>
                          <span className="text-[11px] text-[var(--ink-3)]">
                            Speichert die aktuelle Armposition als Ziel.
                          </span>
                        </div>
                        {/* Batch 2b: manual jog (Tippbetrieb) + hand-guide, then
                            record/replay a hand-guided motion. Disabled unless a
                            real follower is connected and no program is running. */}
                        <JogPanel
                          disabled={heartbeatStatus !== 'connected'
                            || runState === 'running'
                            || recordPanelRecording}
                          onHandGuideChange={setJogHandGuideOn}
                        />
                        <RecordPanel
                          accessToken={accessToken}
                          workflowId={selectedWorkflowId}
                          disabled={heartbeatStatus !== 'connected'
                            || runState === 'running'
                            || jogHandGuideOn}
                          onRecordingChange={setRecordPanelRecording}
                        />
                        {/* Phase-5: live 3D follower twin, stacked BELOW the scene
                            camera (the camera click-to-pin flow is untouched).
                            Collapsible „3D-Ansicht" panel, default collapsed. */}
                        <div className="rounded-lg border border-[var(--line)] bg-white overflow-hidden">
                          <div className="flex items-center gap-1.5 flex-wrap px-2 py-1.5 border-b border-[var(--line)]">
                            <button
                              type="button"
                              onClick={toggleUrdf}
                              aria-pressed={isUrdfOpen}
                              title={isUrdfOpen ? '3D-Ansicht schließen' : '3D-Ansicht öffnen'}
                              className={
                                'text-xs px-2.5 py-1 rounded-md border '
                                + (isUrdfOpen
                                  ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                                  : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
                              }
                            >
                              3D-Ansicht
                            </button>
                            {isUrdfOpen && (
                              <>
                                <button
                                  type="button"
                                  onClick={() => setShowPath((v) => !v)}
                                  aria-pressed={showPath}
                                  title="Bewegungsbahn des Greifers anzeigen"
                                  className={
                                    'text-xs px-2.5 py-1 rounded-md border '
                                    + (showPath
                                      ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                                      : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
                                  }
                                >
                                  {showPath ? 'Bahn verbergen' : 'Bahn anzeigen'}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => setPathClearToken((t) => t + 1)}
                                  title="Bewegungsbahn löschen"
                                  className="text-xs px-2.5 py-1 rounded-md border bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]"
                                >
                                  Bahn löschen
                                </button>
                                <button
                                  type="button"
                                  onClick={() => setShowFrames((v) => !v)}
                                  aria-pressed={showFrames}
                                  title="Koordinatenachsen an Basis und Greifer anzeigen"
                                  className={
                                    'text-xs px-2.5 py-1 rounded-md border '
                                    + (showFrames
                                      ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                                      : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
                                  }
                                >
                                  {showFrames ? 'Achsen verbergen' : 'Achsen anzeigen'}
                                </button>
                              </>
                            )}
                          </div>
                          {isUrdfOpen && (
                            <div className="relative w-full aspect-video bg-[#1a1d23]">
                              <Suspense
                                fallback={
                                  <div className="w-full h-full flex items-center justify-center text-[12px] text-white/70">
                                    3D-Vorschau wird geladen …
                                  </div>
                                }
                              >
                                <UrdfTwin
                                  showPath={showPath}
                                  pathClearToken={pathClearToken}
                                  showFrames={showFrames}
                                />
                              </Suspense>
                              {showFrames && (
                                <div className="absolute bottom-1.5 left-1.5 z-10 px-2 py-0.5 rounded bg-black/55 text-[10px] text-white/80 font-mono">
                                  X rot · Y grün · Z blau
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      </>
                    )}
                    <SkillmapPlayer />
                  </div>
                  {debuggerVisible && (
                    <div className="md:col-span-3 overflow-auto md:overflow-hidden min-w-0">
                      <DebugPanel workspace={workspace} />
                    </div>
                  )}
                </div>
                {/* Phase-2: read-only Python „Code"-Vorschau. Full-width
                    collapsible panel (own toggle, persisted) between the editor
                    grid and the run controls — regenerated from the live
                    workspace on every block change. Never executes. */}
                <div className="px-3 sm:px-4 pb-1">
                  <div className="rounded-lg border border-[var(--line)] bg-white overflow-hidden">
                    <div className="flex items-center gap-2 px-3 py-1.5 border-b border-[var(--line)]">
                      <button
                        type="button"
                        onClick={toggleCode}
                        aria-pressed={isCodeOpen}
                        title={isCodeOpen ? 'Code-Vorschau schließen' : 'Code-Vorschau öffnen'}
                        className={
                          'text-xs px-2.5 py-1 rounded-md border '
                          + (isCodeOpen
                            ? 'bg-[var(--accent)] text-white border-[var(--accent)]'
                            : 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]')
                        }
                      >
                        Code anzeigen
                      </button>
                      <span className="text-[11px] text-[var(--ink-3)]">
                        Python-Vorschau deines Programms
                      </span>
                    </div>
                    {isCodeOpen && (
                      <Suspense
                        fallback={
                          <p className="px-3 py-4 text-xs text-[var(--ink-3)]">
                            Code-Vorschau wird geladen …
                          </p>
                        }
                      >
                        <CodeView
                          workspace={workspace}
                          changeToken={editorJson || unsavedBlocklyJson}
                        />
                      </Suspense>
                    )}
                  </div>
                </div>
                <RunControls
                  workflowId={selectedWorkflowId}
                  blocklyJson={editorJson || unsavedBlocklyJson}
                  workspace={workspace}
                  simMode={simMode}
                  simScene={simScene}
                />
              </>
            )}
          </>
        ) : (
          <CalibrationWizard />
        )}
      </main>
    </div>
  );
}

export default WorkshopPage;
