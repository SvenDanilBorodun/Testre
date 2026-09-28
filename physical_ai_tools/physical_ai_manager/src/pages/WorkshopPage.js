/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useState, useCallback, useMemo, useRef, Suspense, lazy } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import toast, { useToasterStore } from 'react-hot-toast';
import * as Blockly from 'blockly/core';
import CalibrationWizard from '../components/Workshop/CalibrationWizard';
import { HomeGlideProvider } from '../components/Workshop/HomeGlidePrompt';
import LeaderToggle from '../components/Workshop/LeaderToggle';
import BlocklyWorkspace from '../components/Workshop/BlocklyWorkspace';
import { DEFAULT_OBJECT_CATALOG } from '../components/Workshop/blocks/objectCatalogDefaults';
import RunControls from '../components/Workshop/RunControls';
import CameraFeedOverlay from '../components/Workshop/CameraFeedOverlay';
import SimStage from '../components/Workshop/SimStage';
import TemplatePicker from '../components/Workshop/TemplatePicker';
import ToolbarButtons from '../components/Workshop/ToolbarButtons';
import DebugPanel from '../components/Workshop/DebugPanel';
import GalleryTab from '../components/Workshop/GalleryTab';
import SkillmapPlayer from '../components/Workshop/SkillmapPlayer';
import VersionHistoryDropdown from '../components/Workshop/VersionHistoryDropdown';
import JogPanel from '../components/Workshop/JogPanel';
import RightDock from '../components/Workshop/RightDock';
import { buildCatalogDims } from '../components/Workshop/simConstants';
import { DE, formatDe } from '../components/Workshop/blocks/messages_de';
import {
  applyPinnedCoordinates,
  setDriveToHandler,
} from '../components/Workshop/blocks/destinations';
import {
  DESTINATIONS_SERIALIZER_NAME,
  MAX_DESTINATION_ENTRIES,
  createDetachedDestinationStore,
  getDestinationStore,
  nextAutoName,
  readDestinationEntries,
  serializeState,
} from '../components/Workshop/sammlung/destinationStore';
import { createBlocklyAssetDocument } from '../components/Workshop/sammlung/assetDocument';
import { createCodeAssetDocument } from '../components/Workshop/code/codeAssetDocument';
import { createSammlungProvider } from '../components/Workshop/sammlung/provider';
import { buildTwinMarkers, variablePointsFromValues } from '../components/Workshop/sammlung/markers';
import { ghostJointsFromEntry } from '../utils/armProfile';
import SammlungDrawer from '../components/Workshop/sammlung/SammlungDrawer';
import TeachHost from '../components/Workshop/teach/TeachHost';
import {
  TEACH_BLOCK_TITLES_DE, TEACH_KINDS, TEACH_KIND_ICON, TEACH_KIND_LABEL_DE, teachEntryBlockReason,
} from '../components/Workshop/teach/teachGates';
import MenuButton from '../components/Workshop/MenuButton';
import Icon from '../components/icons/Icon';
import { toastIcon } from '../components/icons/toast';
import { jumpToBlock } from '../components/Workshop/sammlung/blockUsage';
import { refreshAssetReferenceWarnings } from '../components/Workshop/sammlung/referenceValidators';
import { useAutosave } from '../components/Workshop/useAutosave';
import { useCodeAutosave } from '../components/Workshop/code/useCodeAutosave';
import CodeWorkspace from '../components/Workshop/code/CodeWorkspace';
import NewProgramDialog from '../components/Workshop/code/NewProgramDialog';
import SubmitButton from '../components/Workshop/code/SubmitButton';
import { CODE_DE } from '../components/Workshop/code/codeMessagesDe';
import { STARTER_FILES, isCodeLanguage, validateProject } from '../components/Workshop/code/codeProject';
import { slimSavePayload } from '../utils/blocklyPayload';
import {
  clearCounters,
  clearVariables,
  openWorkflow,
  selectWorkflowRunning,
  setUnsavedBlocklyJson,
  setSelectedWorkflowId,
  markWorkflowSaved,
  requestRecalibration,
  setDebuggerVisible,
} from '../features/workshop/workshopSlice';
import {
  closeDrawer,
  fetchTrajectories,
  openDrawer,
  requestTeach,
  selectDrawer,
  selectHighlight,
  selectLastPreviewResult,
  selectPreviewActive,
  selectTeachOpen,
  selectTrajectoryList,
  setHighlight,
} from '../features/workshop/studioAssetsSlice';
import useRefetchOnFocus from '../hooks/useRefetchOnFocus';
import { useRosTopicSubscription } from '../hooks/useRosTopicSubscription';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import useRsBridgeStatus from '../hooks/useRsBridgeStatus';
import useSimPreview from '../hooks/useSimPreview';
import {
  SIM_ENTRY_BLOCK_TITLES_DE,
  SIM_ENTRY_SETTLE_MS,
  SIM_TOGGLE_DEFAULT_TITLE_DE,
  previewLeaderGate,
  simEntryBlockReason,
} from '../utils/simPreview';
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

// On-screen toast stack cap. Roboter Studio was the ONE student page without
// it: the other five (ControlPanel, TrainingPage, EditDatasetPage, RecordPage,
// InferencePage) all cap at 3 and dismiss the overflow. Measured here: the
// output blocks inside „wiederhole fortlaufend" emit ~17.7 toasts/s, so a 15 s
// loop stacks ~265 toasts — enough to cover the editor AND the run-control
// strip, the Stopp button included, which leaves the student no way to end the
// very run producing them. Capping the STACK is the on-screen half only; the
// emit rate itself is rate-limited server-side.
const TOAST_LIMIT = 3;

// The „Code anzeigen" open/closed choice persists across reloads.
const WORKSHOP_CODE_OPEN_KEY = 'edubotics_workshop_code_open';

// Right-dock persistence. `dock_open` is the JSON array of open tab ids
// (top→bottom, normally ≤2), `dock_collapsed` folds the dock to the rail.
const DOCK_OPEN_KEY = 'edubotics_workshop_dock_open';
const DOCK_COLLAPSED_KEY = 'edubotics_workshop_dock_collapsed';
// `record` is gone (the „Aufnehmen" tab retired into Vormachen); a stored
// layout still naming it heals through the unknown-id filter below.
const KNOWN_TAB_IDS = ['camera', 'control', '3d', 'tutorial', 'debug'];
const DEFAULT_DOCK_OPEN = ['camera'];

function readDockOpen() {
  try {
    const raw = window.localStorage.getItem(DOCK_OPEN_KEY);
    if (!raw) return DEFAULT_DOCK_OPEN;
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return DEFAULT_DOCK_OPEN;
    // Drop unknown ids and dedupe; cap at 3 (2 user + 1 forced, e.g. Lernpfad).
    const cleaned = arr.filter((x, i) => KNOWN_TAB_IDS.includes(x) && arr.indexOf(x) === i).slice(0, 3);
    return cleaned.length ? cleaned : DEFAULT_DOCK_OPEN;
  } catch (e) {
    return DEFAULT_DOCK_OPEN;
  }
}

function readDockCollapsed() {
  try {
    return window.localStorage.getItem(DOCK_COLLAPSED_KEY) === '1';
  } catch (e) {
    return false;
  }
}

// Desktop dock width (px), set by the drag divider between the editor and the
// dock so the student can rebalance how much room the blocks vs. the tools get.
const DOCK_WIDTH_KEY = 'edubotics_workshop_dock_width';
const DOCK_WIDTH_DEFAULT = 380;
const DOCK_WIDTH_MIN = 280;
const DOCK_WIDTH_MAX = 760;

function clampDockWidth(n) {
  if (!Number.isFinite(n)) return DOCK_WIDTH_DEFAULT;
  return Math.min(DOCK_WIDTH_MAX, Math.max(DOCK_WIDTH_MIN, n));
}

function readDockWidth() {
  try {
    const raw = window.localStorage.getItem(DOCK_WIDTH_KEY);
    if (raw == null) return DOCK_WIDTH_DEFAULT;
    return clampDockWidth(Number(raw));
  } catch (e) {
    return DOCK_WIDTH_DEFAULT;
  }
}

// Open `id` into the dock. Below the 2-panel soft cap it just appends; at the cap
// it evicts the OLDEST non-busy panel (so an in-progress recording / hand-guide /
// active tutorial is never torn out from under the student). If every open panel
// is busy it grows to 3 rather than evict a busy one.
function addOpenTab(openIds, id, isBusy) {
  if (openIds.includes(id)) return openIds;
  if (openIds.length < 2) return [...openIds, id];
  const evictIdx = openIds.findIndex((oid) => !isBusy(oid));
  if (evictIdx === -1) return [...openIds, id];
  const next = openIds.slice();
  next.splice(evictIdx, 1);
  next.push(id);
  return next;
}

// Phase-3 simulator fallback palette: if the catalog service momentarily returns
// nothing, seed the base type so the sim editor stays usable. The key MUST be a
// real type in the server's FIXED object set (`wuerfel`,
// object_catalog.py::_FIXED_CATALOG) — else the server drops every placed object
// silently. `wuerfel` is always present in the fixed set.
// One copy, in blocks/objectCatalogDefaults.js — shared with the teacher-web
// template editor, which has no robot to ask either.
const SIM_DEFAULT_CATALOG = DEFAULT_OBJECT_CATALOG;
// Phase-4: `zones` is a first-class key alongside `objects` (no-go Sperrzonen).
// hydrate/save round-trip the whole sim_scene unchanged, so persisted zones ride
// along in workflows.sim_scene.zones.
const EMPTY_SIM_SCENE = { version: 1, objects: [], zones: [] };

// Compact „Öffnen" control for the toolbar: the TemplatePicker (a full card list
// of templates + own workflows) used to sit expanded in the top band, eating
// vertical space in every view. It now opens in a popover so the band stays a
// single slim row (density pass). Closes on pick or outside click.
// `lockedReason` (a program runs, R2-O3): the button is disabled and says why;
// a popover already open keeps its list but every choice in it is disabled.
function OpenWorkflowPopover({ onPicked, lockedReason = null }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        disabled={!!lockedReason}
        title={lockedReason || 'Vorlage oder gespeicherten Workflow öffnen'}
        className={
          'inline-flex items-center gap-1 min-h-[28px] px-3 py-1.5 rounded-md '
          + 'text-sm font-medium border border-[var(--line)] bg-white text-[var(--ink)] '
          + 'hover:bg-[var(--bg-sunk)] focus:outline-none focus-visible:ring-2 '
          + 'focus-visible:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed'
        }
      >
        <Icon name="folderOpen" />
        {DE.DOCK_OPEN_WORKFLOW}
        <Icon name="chevronDown" size="0.85em" />
      </button>
      {open && (
        <div className="absolute z-30 mt-1 left-0 w-80 max-h-[60vh] overflow-auto rounded-md border border-[var(--line)] bg-white shadow-lg p-3">
          <TemplatePicker
            lockedReason={lockedReason}
            onPicked={(wf) => {
              setOpen(false);
              if (onPicked) onPicked(wf);
            }}
          />
        </div>
      )}
    </div>
  );
}

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
  // Effects key on whether a token EXISTS, never on its value: Supabase
  // rotates the string on every TOKEN_REFRESHED (~hourly), and a hydrate keyed
  // on it re-fetched the workflow and REMOUNTED the editor under the student.
  // Callers read the current token through the ref.
  const hasAccessToken = !!accessToken;
  const accessTokenRef = useRef(accessToken);
  useEffect(() => { accessTokenRef.current = accessToken; }, [accessToken]);
  // The id this page itself just CREATED (first save of an unsaved workflow):
  // the live editor already IS that document, so its hydrate is skipped once.
  const skipHydrateForIdRef = useRef(null);
  const robotType = useSelector((s) => (s.tasks && s.tasks.taskStatus ? s.tasks.taskStatus.robotType : ''));
  // Audit fix: prior path `s.auth?.user?.id` was always null (no
  // top-level `user` field on the auth slice). The Supabase user lives
  // under `session.user.id`. Without this fix the autosave scopeKey
  // was the same string for every student on a shared browser →
  // cross-student autosave bleed.
  const userId = useSelector((s) => s.auth?.session?.user?.id || null);
  const restrictedBlocks = useSelector((s) => s.workshop.restrictedBlocks);
  // Sammlung toolbox groups: what the flyouts and the reference warnings read.
  const trajectoryList = useSelector(selectTrajectoryList);
  const lastPreviewResult = useSelector(selectLastPreviewResult);
  const drawer = useSelector(selectDrawer);
  const highlight = useSelector(selectHighlight);
  const variableValues = useSelector((s) => (s.workshop && s.workshop.variables) || null);
  const debuggerWarnings = useSelector((s) => (s.workshop ? s.workshop.debuggerWarnings : null));
  const activeTutorialId = useSelector((s) => s.workshop.activeTutorialId);

  // A Blockly document's content: the JSON it OPENED with, and the canvas's
  // last reported edit since then (reset whenever another document opens —
  // review round 5, MD5: the previous program's last edit outlived its
  // document, so Start ran it under the next program's id and a save wrote
  // it there). Refs beside the state, written in the same call, for the save.
  const [editorJson, setEditorJsonState] = useState(null);
  const editorJsonRef = useRef(null);
  const setEditorJson = useCallback((json) => {
    editorJsonRef.current = json || null;
    setEditorJsonState(json || null);
  }, []);
  const [initialJsonForEditor, setInitialJsonState] = useState(null);
  const initialJsonRef = useRef(null);
  const setInitialJsonForEditor = useCallback((json) => {
    initialJsonRef.current = json || null;
    setInitialJsonState(json || null);
  }, []);
  // The store's copy of the last edit (it outlives a remount, and the crash-
  // recovery autosave reads it). Mirrored into a ref for the save; every
  // page path that REPLACES the document writes both at once through
  // setUnsavedJson — an effect would update the ref only after React
  // re-rendered, and a save in between wrote the previous program's last
  // edit into the next one's row (review round 5).
  const unsavedJsonRef = useRef(unsavedBlocklyJson);
  useEffect(() => { unsavedJsonRef.current = unsavedBlocklyJson; }, [unsavedBlocklyJson]);
  const setUnsavedJson = useCallback((json) => {
    unsavedJsonRef.current = json || null;
    dispatch(setUnsavedBlocklyJson(json || null));
  }, [dispatch]);
  const [editorKey, setEditorKey] = useState(0);
  // True while a selected workflow's row is being fetched: NEITHER editor
  // mounts until the language is known, so a code workflow never flashes (and
  // never injects) the Blockly canvas, and a Blockly workflow never mounts a
  // blank canvas it will remount a moment later. Seeded from the props, not
  // `false`: the first render happens BEFORE the hydrate effect can say so.
  const [hydrating, setHydratingState] = useState(() => !!(selectedWorkflowId && accessToken));
  // Read by every save (review round 5, MD2): set in the same call as the
  // state, so a save a moment after the pick is refused, not raced.
  const documentLoadingRef = useRef(hydrating);
  const setHydrating = useCallback((v) => {
    documentLoadingRef.current = !!v;
    setHydratingState(!!v);
  }, []);
  const [workspace, setWorkspace] = useState(null);
  const [saving, setSaving] = useState(false);
  const [view, setView] = useState('editor'); // 'editor' | 'gallery'
  // Phase-3 "Test im Simulator". simMode opens the editor without calibration and
  // swaps the live scene camera for the SimScene editor + virtual-arm preview.
  const [simMode, setSimMode] = useState(false);
  const [simScene, setSimScene] = useState(EMPTY_SIM_SCENE);
  // A CODE workflow (Roboter Studio Python/Java): one workflow is one language,
  // chosen at creation (NewProgramDialog) and immutable after. '' is a Blockly
  // document; a code document never mounts the Blockly canvas and saves
  // `code_language` + `code_files` as two fields on the same PATCH/POST —
  // never inside `blockly_json`, whose SAVE allowlist would delete them.
  const [codeLanguage, setCodeLanguage] = useState('');
  const [codeFiles, setCodeFiles] = useState(null);
  const isCodeWorkflow = isCodeLanguage(codeLanguage);
  // Migration 041: a code document's Ziele/Positionen live in ONE detached
  // DestinationStore per open code document (a Blockly document keeps them in
  // its workspace's store). Created by openCodeDocument — never
  // `getDestinationStore(null)`, which hands out a fresh store on every call.
  const [codeDestinationStore, setCodeDestinationStore] = useState(null);
  // The ref every save reads. applyCodeFiles sets it SYNCHRONOUSLY: a rename
  // rewrites the code and then awaits saveWorkflowNow in the same tick, and a
  // ref refreshed in an effect would still hold the old text when runSave reads
  // it. Every programmatic code edit (a rename, an insertion) and the editor's
  // own typing go through applyCodeFiles.
  const codeFilesRef = useRef(codeFiles);
  // The language and the Ziele store of the open code document, read by every
  // save beside codeFilesRef — so openCodeDocument moves all three together,
  // synchronously: a save between two renders must never pair one document's
  // files with another's language or Ziele.
  const codeLanguageRef = useRef(codeLanguage);
  const codeStoreRef = useRef(null);
  // `next` is the files, or an updater `(latest) => files` built from the
  // ref — what CodeWorkspace's keystrokes use, so a keystroke landing before
  // React re-rendered an edit applied here (a drawer rename) cannot rebuild
  // the project from the stale prop and undo it (review n8).
  const applyCodeFiles = useCallback((next) => {
    const files = typeof next === 'function' ? next(codeFilesRef.current) : next;
    codeFilesRef.current = files;
    setCodeFiles(files);
  }, []);
  // Whether a code PATCH carries the Ziele (review m1): only when the
  // document opened WITH Ziele or its store changed since opening (clearing
  // them all then sends `{}`). A Ziele-free program sends no blockly_json at
  // all, so it still saves against an API from before migration 041.
  const codeZieleSendRef = useRef(false);
  // The previous code document's store subscription, ended when the next
  // document opens (review round 3, nb5): the old store stays reachable (a
  // TeachOverlay still open, an insertion in flight) and a change to it must
  // not mark the NEXT document's Ziele as changed.
  const codeStoreUnsubscribeRef = useRef(null);
  // Where the student's cursor last was ({file, line}), reported by
  // CodeWorkspace — null until they click into the editor; an insertion then
  // writes nothing and asks them to click first (owner decision R3-O4). A
  // request to show a line (a „Benutzt in" row, an insertion) travels as a prop.
  const codeCursorRef = useRef(null);
  // THE OPEN DOCUMENT'S IDENTITY (review round 5, MD2/MD3/MD5, owner decision
  // R5-O1). `selectedWorkflowId` is the program the student ASKED for and moves
  // the moment they pick one; the editor holds it only once its row arrived.
  // In between — and after a load that failed — the page still holds another
  // document, so everything that writes to the cloud or runs a program reads
  // the identity of the document the editor HOLDS:
  //   * `openDocId` — its cloud row (null: not saved yet, or a load that
  //     failed), set when a row arrived, a save created one, or „Neu" opened;
  //   * the document token — bumped the moment the page starts replacing the
  //     document (a load, „Neu", a draft, a version restore), so a save, a
  //     restore result, a run start or an insertion that began before can
  //     tell it now belongs to something else.
  // Both are written through the refs synchronously and mirrored into state
  // for the render.
  const [openDocId, setOpenDocIdState] = useState(null);
  const openDocIdRef = useRef(null);
  const setOpenDocId = useCallback((id) => {
    openDocIdRef.current = id || null;
    setOpenDocIdState(id || null);
  }, []);
  const docTokenRef = useRef(0);
  const [docToken, setDocToken] = useState(0);
  const bumpDocToken = useCallback(() => {
    docTokenRef.current += 1;
    setDocToken(docTokenRef.current);
  }, []);
  const getDocumentToken = useCallback(() => docTokenRef.current, []);
  const revealSeqRef = useRef(0);
  const [codeRevealRequest, setCodeRevealRequest] = useState(null);
  // The ONLY way a code document opens (hydrate, its failure, „Neu", a draft,
  // a version restore): language + files + its Ziele from the saved
  // `blockly_json` (`edubotics-destinations`). A non-code `language` closes
  // any open code document. The caller bumps editorKey, as before.
  const openCodeDocument = useCallback(({ language, files, blocklyJson } = {}) => {
    bumpDocToken();
    codeCursorRef.current = null;
    setCodeRevealRequest(null);
    if (codeStoreUnsubscribeRef.current) {
      codeStoreUnsubscribeRef.current();
      codeStoreUnsubscribeRef.current = null;
    }
    if (isCodeLanguage(language)) {
      const store = createDetachedDestinationStore(readDestinationEntries(blocklyJson));
      codeZieleSendRef.current = store.getEntries().length > 0;
      // The store lives exactly as long as this document; so does this.
      codeStoreUnsubscribeRef.current = store.subscribe(() => { codeZieleSendRef.current = true; });
      codeLanguageRef.current = language;
      codeStoreRef.current = store;
      setCodeLanguage(language);
      applyCodeFiles(files && typeof files === 'object' && !Array.isArray(files) ? files : {});
      setCodeDestinationStore(store);
      return;
    }
    codeLanguageRef.current = '';
    codeStoreRef.current = null;
    codeZieleSendRef.current = false;
    setCodeLanguage('');
    applyCodeFiles(null);
    setCodeDestinationStore(null);
  }, [applyCodeFiles, bumpDocToken]);
  // „Neu ▾" has been used this mount: the student has CHOSEN what the editor
  // holds, so a crash-recovery draft arriving a moment later must not overrule
  // them (the read is async; the click is not).
  const documentChosenRef = useRef(false);
  // #B2: leaving simMode while a sim run is active would unmount RunControls (and
  // its Stop button) on an uncalibrated rig, stranding the run. Lock the toggle
  // while a sim run is in flight (mirrors LeaderToggle's run-guard).
  const runState = useSelector((s) => s.workshop.runState);
  const simRunActive = simMode && runState === 'running';
  // No document switch while a program runs or stands at a breakpoint
  // (owner decision R2-O3): every action that replaces the open document is
  // disabled with this German reason, and the handlers refuse it too (a clone
  // that finishes after the run started, a popover opened before). A run
  // keeps reporting on the program that produced it.
  // Only while the robot link is alive (review round 3, mb1) — exactly like
  // utils/signOut::logoutBlockReason: a run state the dead link can no longer
  // retire must not lock the student out of every other program.
  const workflowRunning = useSelector(selectWorkflowRunning);
  const switchHeartbeat = useSelector((s) => s.tasks?.heartbeatStatus);
  const runLockReason = workflowRunning && switchHeartbeat === 'connected' ? DE.STOP_PROGRAM_FIRST : null;
  const runLockRef = useRef(runLockReason);
  runLockRef.current = runLockReason;
  // A version restore on its way (review round 3, nb2; round 5, MD3): no run,
  // preview, save or document switch meanwhile — the cloud row and the editor
  // would part. The ref is set in the same call as the state, so a click that
  // arrives before React re-rendered is refused too.
  const [versionRestoring, setVersionRestoringState] = useState(false);
  const versionRestoringRef = useRef(false);
  const handleRestoringChange = useCallback((v) => {
    versionRestoringRef.current = !!v;
    setVersionRestoringState(!!v);
  }, []);
  // From „Start" until the run is marked running or has failed (review round
  // 5, MD5): RunControls reports it; nothing replaces the document meanwhile.
  const [runStarting, setRunStartingState] = useState(false);
  const runStartingRef = useRef(false);
  const handleRunStarting = useCallback((v) => {
    runStartingRef.current = !!v;
    setRunStartingState(!!v);
  }, []);
  // Why no action may replace the open document right now, or null.
  const switchLockReason = runLockReason
    || (runStarting ? DE.PROGRAM_STARTING : null)
    || (versionRestoring ? DE.VERSION_RESTORE_IN_FLIGHT : null);
  const refuseSwitch = useCallback(() => {
    const reason = runLockRef.current
      || (runStartingRef.current ? DE.PROGRAM_STARTING : null)
      || (versionRestoringRef.current ? DE.VERSION_RESTORE_IN_FLIGHT : null);
    if (!reason) return false;
    toast.error(reason);
    return true;
  }, []);
  // [label_de, type_name] pairs from GetObjectCatalog, threaded to SimScene's
  // object palette (the same source the Blockly dropdowns use).
  const [objectCatalog, setObjectCatalog] = useState([]);
  // The catalog's type names, for the code editor's object-name completion.
  const codeObjectTypes = useMemo(
    () => (Array.isArray(objectCatalog)
      ? objectCatalog.map((p) => (Array.isArray(p) ? p[1] : null)).filter((t) => typeof t === 'string' && t)
      : []),
    [objectCatalog],
  );
  // Sim-stage render seam: { [type]: {height_m, width_m, color, max_instances} },
  // built from the parallel GetObjectCatalog srv arrays in the same fetch effect
  // and threaded to SimStage → SimScene → UrdfTwin so objects render at their real
  // catalog size/colour and placement is capped per type. A STABLE reference (set
  // once per fetch) because it is a dep of UrdfTwin's object-diff effect. `{}` on
  // an old server / failed fetch → both renderers fall back to the fixed defaults.
  const [catalogDims, setCatalogDims] = useState({});
  // Debug strip open-state WHILE in the simulator. In sim the RightDock (and its
  // „Debug" tab) is replaced by SimStage, so RunControls' Debug button targets
  // this flag instead of the dock tab (see onToggleDebug wiring below).
  const [simDebugOpen, setSimDebugOpen] = useState(false);
  // Why Start may not start a run right now, or null: while the program the
  // student opened is still being fetched (review round 4, mc10 — Start would
  // run the OLD program's files and Ziele under the NEW program's id), and
  // while a version restore is on its way (nb2). Previews read the same two.
  const startBlockedReason = hydrating
    ? DE.DOCUMENT_LOADING
    : (versionRestoring ? DE.VERSION_RESTORE_IN_FLIGHT : null);
  // Batch 2b: rosbridge liveness gates the real-arm jog panel and Vormachen (the same
  // signal the rest of the app uses for „Roboter verbunden").
  const heartbeatStatus = useSelector((s) => s.tasks?.heartbeatStatus);
  const caps = useSelector((s) => (s.tasks && s.tasks.taskStatus ? s.tasks.taskStatus.capabilities : null) || null);
  // Vormachen (teach/TeachHost) owns the arm while open: JogPanel, the
  // LeaderToggle, the „fahre dorthin" drive-to and the sim entry are locked, since
  // a driven move or a container flip would fight the student's hand.
  const teachOpen = useSelector(selectTeachOpen);
  const paused = useSelector((s) => !!(s.workshop && s.workshop.paused));
  const previewActive = useSelector(selectPreviewActive);
  // Batch 2b: JogPanel reports whether a hand-guide (torque-off) session is open
  // (lifted here) so Vormachen refuses to open over a limp arm — one shared truth,
  // so no surface shows a stale „freigeschaltet" state after JogPanel closes it.
  const [jogHandGuideOn, setJogHandGuideOn] = useState(false);
  // The Roboter-Studio bridge (:8769): Vormachen reads `leaderOn` (leader vs
  // hand mode, and a leader switched on mid-session) and `followerOnly` (the
  // leader gone in leader mode). Polled only while the page is shown.
  const rsBridge = useRsBridgeStatus({ enabled: isActive });
  const teachReason = teachEntryBlockReason({
    heartbeatStatus,
    runState,
    paused,
    simMode,
    jogHandGuideOn,
    previewActive,
  });
  const subscriptions = useRosTopicSubscription();
  const { getObjectCatalog, jogArm } = useRosServiceCaller();
  const workspaceRef = useRef(null);
  const workspaceDocTokenRef = useRef(null);
  // Blockly 12 ties getSelected() to the FocusManager, and clicking the
  // camera overlay (a non-focusable div) blurs the block in Chromium →
  // selection is null by the time the camera onClick runs. So we remember
  // the id of the most-recently selected destination_pin block via a
  // SELECTED change-listener and use THAT at mark time, not live selection.
  const lastPinBlockIdRef = useRef(null);

  // Redesign: the right-side tools (camera / jog / 3D / Lernpfad / Debug) live in a tabbed, collapsible RightDock instead of a tall stacked
  // column. `dockOpen` is the ordered list of open panels (≤2 by default),
  // `dockCollapsed` folds the dock to its rail. Both persist across reloads.
  const [dockOpen, setDockOpen] = useState(readDockOpen);
  const [dockCollapsed, setDockCollapsed] = useState(readDockCollapsed);
  const [dockWidth, setDockWidth] = useState(readDockWidth);
  // Toast cap — same dismissal loop as the other five student pages. `toasts` is
  // newest-first, so index >= TOAST_LIMIT is the overflow; dismiss (not remove)
  // keeps the exit animation.
  const { toasts } = useToasterStore();
  useEffect(() => {
    toasts
      .filter((t) => t.visible)
      .filter((_, i) => i >= TOAST_LIMIT)
      .forEach((t) => toast.dismiss(t.id));
  }, [toasts]);

  useEffect(() => {
    try {
      window.localStorage.setItem(DOCK_OPEN_KEY, JSON.stringify(dockOpen));
    } catch (_) { /* quota / private mode — keep the in-memory value */ }
  }, [dockOpen]);
  useEffect(() => {
    try {
      window.localStorage.setItem(DOCK_COLLAPSED_KEY, dockCollapsed ? '1' : '0');
    } catch (_) { /* quota / private mode — keep the in-memory value */ }
  }, [dockCollapsed]);

  // Drag divider between the editor and the dock. The handle captures the pointer
  // (so a drag that leaves the 8px strip still tracks); the new width is the
  // distance from the pointer to the row's right edge, clamped so the editor
  // keeps a usable minimum. Persisted on release (not per-move — avoids a
  // localStorage write every animation frame).
  const dockRowRef = useRef(null);
  const dockDragRef = useRef(false);
  const onDockResizeDown = useCallback((e) => {
    dockDragRef.current = true;
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch (_) { /* jsdom */ }
  }, []);
  const onDockResizeMove = useCallback((e) => {
    if (!dockDragRef.current || !dockRowRef.current) return;
    const rect = dockRowRef.current.getBoundingClientRect();
    if (!rect.width) return;
    const raw = rect.right - e.clientX;
    // Cap so the editor never drops below ~360 px.
    const maxW = Math.min(DOCK_WIDTH_MAX, rect.width - 360);
    setDockWidth(Math.max(DOCK_WIDTH_MIN, Math.min(maxW, raw)));
  }, []);
  const onDockResizeUp = useCallback((e) => {
    if (!dockDragRef.current) return;
    dockDragRef.current = false;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch (_) { /* jsdom */ }
    setDockWidth((w) => {
      try { window.localStorage.setItem(DOCK_WIDTH_KEY, String(w)); } catch (_) { /* ignore */ }
      return w;
    });
  }, []);

  // No svgResize here: BlocklyWorkspace observes its own host box, which covers
  // the dock width/collapse and the simulator swap as well as every HEIGHT
  // change (Code-Vorschau, Protokoll, banners) the old width-only effect missed.

  // A panel is „busy" while it holds a live session that must NOT be torn out
  // (recording, hand-guide, or an active tutorial that owns the toolbox
  // restriction). Busy panels can't be closed or evicted (see RightDock).
  const isTabBusy = useCallback(
    (id) =>
      (id === 'control' && jogHandGuideOn)
      || (id === 'tutorial' && !!activeTutorialId),
    [jogHandGuideOn, activeTutorialId],
  );

  const handleToggleTab = useCallback(
    (id) => {
      if (dockCollapsed) {
        // A rail click while collapsed expands + ensures the tab is open
        // (never closes on the reveal click).
        setDockCollapsed(false);
        setDockOpen((prev) => (prev.includes(id) ? prev : addOpenTab(prev, id, isTabBusy)));
        return;
      }
      setDockOpen((prev) => {
        if (prev.includes(id)) return isTabBusy(id) ? prev : prev.filter((x) => x !== id);
        return addOpenTab(prev, id, isTabBusy);
      });
    },
    [dockCollapsed, isTabBusy],
  );

  const handleCloseTab = useCallback(
    (id) => {
      setDockOpen((prev) => (isTabBusy(id) ? prev : prev.filter((x) => x !== id)));
    },
    [isTabBusy],
  );

  const handleToggleCollapsed = useCallback(() => setDockCollapsed((c) => !c), []);

  // Keep the Redux `debuggerVisible` flag in sync with whichever Debug surface is
  // live: the dock's „Debug" tab normally, or SimStage's debug strip while in the
  // simulator (where the dock — and its tab — is replaced by SimStage).
  useEffect(() => {
    dispatch(setDebuggerVisible(simMode ? simDebugOpen : dockOpen.includes('debug')));
  }, [dockOpen, simMode, simDebugOpen, dispatch]);

  // Force the „Lernpfad" tab open whenever a tutorial is active (it owns the
  // toolbox restriction via SkillmapPlayer — closing it would lift the
  // restriction mid-tutorial). Expand the dock the moment a tutorial STARTS.
  // Skipped in simMode: the dock isn't rendered (SimStage replaces it), and sim
  // entry is blocked while a tutorial is active anyway (the toggle is disabled).
  const prevTutorialRef = useRef(null);
  useEffect(() => {
    if (simMode) return;
    const started = !!activeTutorialId && prevTutorialRef.current !== activeTutorialId;
    prevTutorialRef.current = activeTutorialId;
    if (!activeTutorialId) return;
    setDockOpen((prev) => (prev.includes('tutorial') ? prev : addOpenTab(prev, 'tutorial', isTabBusy)));
    if (started) setDockCollapsed(false);
  }, [activeTutorialId, simMode, isTabBusy]);

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

  // Twin overlays: the end-effector path trail + base/TCP coordinate triads.
  const [showPath, setShowPath] = useState(false);
  const [showFrames, setShowFrames] = useState(false);
  // Bumped to clear the path trail (manual „Bahn löschen" + auto on run start).
  const [pathClearToken, setPathClearToken] = useState(0);
  // „Ziel auf den Sim-Tisch setzen" (Sammlung flyout): {mode: 'ziel', token}; each
  // new token switches SimScene into its „Ziel setzen" mode.
  const [simZielRequest, setSimZielRequest] = useState(null);
  // A request belongs to the simulator visit it was made in: SimScene remounts
  // on the next entry and would otherwise re-read a stale token as a new ask.
  useEffect(() => {
    if (!simMode) setSimZielRequest(null);
  }, [simMode]);
  // Auto-clear the trail when a run begins, so each run draws a fresh path.
  const prevRunStateRef = useRef(runState);
  useEffect(() => {
    if (runState === 'running' && prevRunStateRef.current !== 'running') {
      setPathClearToken((t) => t + 1);
    }
    prevRunStateRef.current = runState;
  }, [runState]);

  // One ladder for the header sim toggle AND a preview's sim entry
  // (utils/simPreview.js::simEntryBlockReason).
  const simEntryReason = simEntryBlockReason({
    simRunActive, simMode, activeTutorialId, teachOpen, jogHandGuideOn,
  });
  const simEntryReasonRef = useRef(simEntryReason);
  simEntryReasonRef.current = simEntryReason;
  const simModeRef = useRef(simMode);
  simModeRef.current = simMode;
  // Enter the simulator on behalf of a preview (and, later, „Ziel setzen" / a
  // point marker with `showPath: false`, which never turns the trail on).
  // Resolves true once the simulator is (or already was) open.
  const ensureSimMode = useCallback(async ({ showPath: withPath = true } = {}) => {
    if (simModeRef.current) {
      if (withPath) setShowPath(true);
      return true;
    }
    const reason = simEntryReasonRef.current;
    if (reason) {
      toast.error(SIM_ENTRY_BLOCK_TITLES_DE[reason]);
      return false;
    }
    setSimMode(true);
    if (withPath) setShowPath(true);
    await new Promise((resolve) => { setTimeout(resolve, SIM_ENTRY_SETTLE_MS); });
    return true;
  }, []);

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
    if (!isActive) {
      // The inactive page renders nothing, so BlocklyWorkspace is unmounted and
      // its next activation must hydrate: a skip left armed here (a create that
      // resolved after a tab switch) would remount the editor from a stale
      // document under the new id.
      skipHydrateForIdRef.current = null;
      return undefined;
    }
    // The live editor IS this document — the page just created it; a re-fetch
    // would remount the workspace and drop anything captured during the save
    // round-trip. Consumed once, so a later re-open hydrates normally.
    if (selectedWorkflowId && skipHydrateForIdRef.current === selectedWorkflowId) {
      skipHydrateForIdRef.current = null;
      return undefined;
    }
    const token = accessTokenRef.current;
    if (selectedWorkflowId && token) {
      // Another document is on its way: an insertion still loading its
      // module must not land in it (mb9), and a save, a restore result or a
      // run start that began before must not act on it (round 5).
      bumpDocToken();
      setHydrating(true);
      getWorkflow(token, selectedWorkflowId)
        .then((w) => {
          if (cancelled) return;
          // The editor holds THIS row from now on, and neither the page's nor
          // the store's copy of the previous program's last edit may stand
          // in for it (review round 5: a save before the canvas was ready,
          // and Start, used the previous program's edit under this id).
          setOpenDocId(selectedWorkflowId);
          setEditorJson(null);
          setUnsavedJson(null);
          setInitialJsonForEditor(w?.blockly_json || null);
          // Hydrate the persisted Sim-Szene (workflows.sim_scene); fall back to
          // an empty scene when absent/empty so a non-sim workflow clears it.
          setSimScene(
            w?.sim_scene && Array.isArray(w.sim_scene.objects)
              ? w.sim_scene
              : EMPTY_SIM_SCENE,
          );
          // A code workflow (migration 040): the language routes the editor,
          // the files are the document, and (041) blockly_json carries its
          // Ziele. A pre-040 row or a Blockly workflow carries '' / {} and
          // closes any code document that was open.
          openCodeDocument({
            language: w?.code_language,
            files: w?.code_files,
            blocklyJson: w?.blockly_json,
          });
          setEditorKey((k) => k + 1);
          setHydrating(false);
        })
        .catch((e) => {
          if (cancelled) return;
          toast.error(`Workflow konnte nicht geladen werden: ${e.message || e}`);
          // What the editor shows now is NOT that row: it holds no cloud row,
          // so a save creates one instead of overwriting the row that failed
          // to load (review round 5, MD2).
          setOpenDocId(null);
          setEditorJson(null);
          setInitialJsonForEditor(unsavedBlocklyJson || null);
          openCodeDocument({ language: '' });
          setEditorKey((k) => k + 1);
          setHydrating(false);
        });
      return () => { cancelled = true; };
    }
    if (!selectedWorkflowId) setOpenDocId(null);
    setEditorJson(null);
    setInitialJsonForEditor(unsavedBlocklyJson || null);
    setEditorKey((k) => k + 1);
    setHydrating(false);
    return () => { cancelled = true; };
    // unsavedBlocklyJson intentionally omitted from deps: we only want
    // to seed once per workflow-id change. The change-listener inside
    // BlocklyWorkspace keeps Redux in sync after that. The token is read
    // through its ref: only its PRESENCE is a dependency (see hasAccessToken).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActive, selectedWorkflowId, hasAccessToken]);

  const handleEditorChange = useCallback(
    (json) => {
      setEditorJson(json);
      setUnsavedJson(json);
    },
    [setEditorJson, setUnsavedJson]
  );

  const handleWorkspaceReady = useCallback((ws) => {
    workspaceRef.current = ws;
    // The document this canvas was mounted for: every document replacement
    // bumps the token and then remounts the canvas, so a canvas whose token
    // is behind still shows the PREVIOUS document until React unmounts it,
    // and a save must not serialise it (review round 5).
    workspaceDocTokenRef.current = ws ? docTokenRef.current : null;
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

  // The open document's Ziele store and its asset document — ONE for either
  // notation: the Blockly workspace's own store, or the code document's
  // detached one. Every Ziele path of the page (a camera click, the sim
  // table, the markers, a preview, the run, Vormachen, the drawer) goes
  // through these, so a code program gets all of them (owner decision O4).
  const activeStore = isCodeWorkflow
    ? codeDestinationStore
    : (workspace ? getDestinationStore(workspace) : null);
  const activeStoreRef = useRef(activeStore);
  activeStoreRef.current = activeStore;
  const assetDoc = useMemo(() => {
    if (isCodeWorkflow) {
      if (!codeDestinationStore) return null;
      return createCodeAssetDocument({
        language: codeLanguage,
        store: codeDestinationStore,
        getFiles: () => codeFilesRef.current,
        applyFiles: applyCodeFiles,
        requestReveal: (at) => {
          revealSeqRef.current += 1;
          setCodeRevealRequest({ ...at, nonce: revealSeqRef.current });
        },
        getCursor: () => codeCursorRef.current,
        setCursor: (at) => { codeCursorRef.current = at; },
        getDocumentToken,
      });
    }
    return workspace ? createBlocklyAssetDocument(workspace) : null;
  }, [isCodeWorkflow, codeLanguage, codeDestinationStore, workspace, applyCodeFiles, getDocumentToken]);
  const assetDocRef = useRef(assetDoc);
  assetDocRef.current = assetDoc;
  // Vormachen teaches into the document only while its editor is on screen —
  // a Blockly workspace exists only then; a code document outlives its
  // editor (gallery, calibration), so it is handed over only while
  // CodeWorkspace is mounted.
  const teachAssetDoc = assetDoc && (!isCodeWorkflow || (showEditor && view !== 'gallery' && !hydrating))
    ? assetDoc : null;
  const handleCodeCursorChange = useCallback((at) => { codeCursorRef.current = at; }, []);

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
      // Refuse a driven move on a disconnected robot (matches JogPanel/Vormachen
      // gating) — without this the jog call silently no-ops "successfully".
      if (heartbeatStatus !== 'connected') {
        toast.error('Roboter nicht verbunden.');
        return;
      }
      if (runState === 'running') {
        toast.error('Während ein Programm läuft, ist das Fahren gesperrt.');
        return;
      }
      if (teachOpen) {
        toast.error(DE.TEACH_DRIVE_BLOCKED);
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
  }, [jogArm, simMode, runState, teachOpen, heartbeatStatus, jogHandGuideOn]);

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
          // Sim-stage render seam: per-type size/colour/cap from the parallel srv
          // arrays. `{}` on an old server (arrays absent) → renderers fall back to
          // the fixed amber-cube defaults. Set once per fetch (stable reference).
          setCatalogDims(buildCatalogDims(r));
        } else if (simMode) {
          // Non-fatal in the simulator — keep the palette usable.
          setObjectCatalog(SIM_DEFAULT_CATALOG);
          setCatalogDims({});
        } else if (r && r.message) {
          toast.error(r.message);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        console.warn('getObjectCatalog failed', e);
        if (simMode) {
          setObjectCatalog(SIM_DEFAULT_CATALOG);
          setCatalogDims({});
        }
      });
    return () => {
      cancelled = true;
    };
  }, [isActive, calibrated, simMode, getObjectCatalog]);

  // TemplatePicker calls onPicked(workflowObject) — the full row, not
  // just the id. We extract the id and store it in Redux; the editor's
  // load effect picks up the change and hydrates initialJsonForEditor.
  // `openWorkflow`, not setSelectedWorkflowId: opening another program from
  // an UNSAVED one must retire the old values too (review round 2, mi7).
  const handlePickWorkflow = useCallback(
    (workflow) => {
      if (!workflow || !workflow.id) return;
      if (refuseSwitch()) return;
      dispatch(openWorkflow(workflow.id));
    },
    [dispatch, refuseSwitch]
  );

  // „Neu → Blöcke / Python / Java" (NewProgramDialog): the ONE moment a
  // workflow's language is chosen. The editor becomes an UNSAVED document of
  // that kind — a blank canvas, or the language's starter project — and the
  // first save creates the row with `code_language` + `code_files` (runSave).
  // Nothing is created here: a student who changes their mind before saving
  // leaves no row behind.
  const handleNewProgram = useCallback((choice) => {
    if (refuseSwitch()) return;
    documentChosenRef.current = true;
    dispatch(setSelectedWorkflowId(null));
    // A new document shows no values of the last one — also from one
    // unsaved document to the next, which setSelectedWorkflowId cannot see
    // (review m4).
    dispatch(clearVariables());
    dispatch(clearCounters());
    setUnsavedJson(null);
    setOpenDocId(null);
    setEditorJson(null);
    setInitialJsonForEditor(null);
    setSimScene(EMPTY_SIM_SCENE);
    openCodeDocument(isCodeLanguage(choice)
      ? { language: choice, files: { ...STARTER_FILES[choice] }, blocklyJson: null }
      : { language: '' });
    setEditorKey((k) => k + 1);
  }, [dispatch, openCodeDocument, refuseSwitch, setOpenDocId, setEditorJson, setInitialJsonForEditor,
    setUnsavedJson]);

  // Camera click → Ziel, without a prompt. CameraFeedOverlay asks for the
  // label BEFORE it calls /workshop/mark_destination, so one point has one name
  // on the server and in the editor:
  //   * a „setze Ziel = Pin" block was selected last → that block's own NAME,
  //     and its X/Y/Z fields receive the coordinates (the remembered id, not the
  //     live selection — the camera click has already blurred the block in
  //     Chromium, see lastPinBlockIdRef; getBlockById returns null for a
  //     deleted block, which drops the stale id);
  //   * otherwise → a new „Ziel n" in the document's destination store, which
  //     the overlay then offers to rename inline.
  //   * a CODE program has no „setze Ziel" block to select: the click always
  //     makes a new Ziel in its store, named past its store names AND the
  //     names the program pins itself.
  const resolveMarkLabel = useCallback(() => {
    const store = activeStoreRef.current;
    const doc = assetDocRef.current;
    if (!store || !doc) return null;
    const ws = workspaceRef.current;
    if (ws) {
      const id = lastPinBlockIdRef.current;
      const block = id ? ws.getBlockById(id) : null;
      if (block && block.type === 'edubotics_destination_pin') {
        return { label: (block.getFieldValue('NAME') || '').trim() || 'A', target: 'block', blockId: id };
      }
    }
    lastPinBlockIdRef.current = null;
    if (store.getEntries().length >= MAX_DESTINATION_ENTRIES) { toast.error(DE.ERR_STORE_FULL); return null; }
    return { label: nextAutoName(DE.TEACH_AUTO_NAME_ZIEL, doc.takenPlaceNames()), target: 'store' };
  }, []);

  const handleMarkDestination = useCallback(({ label, target, blockId, world_x, world_y, world_z }) => {
    const store = activeStoreRef.current;
    if (!store) return null;
    if (target === 'block') {
      const ws = workspaceRef.current;
      const block = ws && blockId ? ws.getBlockById(blockId) : null;
      if (!block || block.type !== 'edubotics_destination_pin') return null;
      applyPinnedCoordinates(block, world_x, world_y, world_z);
      toast.success(formatDe(DE.CAMERA_PIN_WRITTEN, block.getFieldValue('NAME') || label));
      return null;
    }
    const res = store.add({
      name: label,
      kind: 'pin',
      source: 'camera',
      x: world_x,
      y: world_y,
      z: world_z,
      robot_type: robotType || undefined,
    });
    if (!res.ok) { toast.error(res.error); return null; }
    toast.success(formatDe(DE.CAMERA_ZIEL_CREATED, res.entry.name));
    return { entryId: res.entry.id, name: res.entry.name };
  }, [robotType]);

  // The overlay's inline rename field. The result is returned so the field
  // stays open (with the refusal toasted) until a name is accepted.
  const handleRenameMarked = useCallback((entryId, rawName) => {
    const store = activeStoreRef.current;
    if (!store) return { ok: false };
    const res = store.rename(entryId, rawName);
    if (!res.ok) toast.error(res.error);
    return res;
  }, []);

  // The document's Ziele/Positionen as markers on the twin and the sim table.
  // The store notifies synchronously on every change (add, rename, delete, undo,
  // load), so the markers follow the document without polling.
  const [storeEntries, setStoreEntries] = useState([]);
  useEffect(() => {
    if (!activeStore) {
      setStoreEntries([]);
      return undefined;
    }
    setStoreEntries(activeStore.getEntries());
    return activeStore.subscribe((entries) => setStoreEntries(entries));
  }, [activeStore]);
  // Point-shaped variable values ({x, y, z} in metres) get a violet marker
  // (the most recently set ones — sammlung/markers.js::variablePointsFromValues).
  const variablePoints = useMemo(() => variablePointsFromValues(variableValues), [variableValues]);
  const markers = useMemo(
    () => buildTwinMarkers({
      entries: storeEntries, variablePoints, simMode, highlight,
    }),
    [storeEntries, variablePoints, simMode, highlight],
  );
  // The ghost arm: the highlighted Position's captured joints, when they fit
  // this arm (utils/armProfile.js::ghostJointsFromEntry). Anything else → null.
  const ghostJoints = useMemo(() => {
    if (!highlight || highlight.kind !== 'pose') return null;
    const entry = storeEntries.find((e) => e && e.id === highlight.id);
    return ghostJointsFromEntry(entry, caps, robotType);
  }, [storeEntries, highlight, caps, robotType]);

  // A tap in SimScene's „Ziel setzen" mode: a pin ON the virtual table (z 0).
  // On a calibrated real rig the same entry later re-asks the measured plane
  // (plane-tracked, motion.resolve_destination_z); an uncalibrated rig cannot
  // START it outside the simulator because showEditor hides RunControls.
  const handleCreateSimDestination = useCallback(({ x, y }) => {
    const store = activeStoreRef.current;
    const doc = assetDocRef.current;
    if (!store || !doc) return;
    if (store.getEntries().length >= MAX_DESTINATION_ENTRIES) {
      toast.error(DE.ERR_STORE_FULL);
      return;
    }
    const res = store.add({
      name: nextAutoName(DE.TEACH_AUTO_NAME_ZIEL, doc.takenPlaceNames()),
      kind: 'pin',
      source: 'sim',
      x,
      y,
      z: 0,
      robot_type: robotType || undefined,
    });
    if (!res.ok) {
      toast.error(res.error);
      return;
    }
    toast.success(formatDe(DE.SIM_ZIEL_CREATED, res.entry.name));
  }, [robotType]);

  // Autosave hook. Restores the most-recent local state if the parent
  // hasn't already loaded a server workflow. Scoped per user so two
  // students sharing a browser don't see each other's drafts.
  const handleAutosaveRestore = useCallback(
    (state) => {
      if (selectedWorkflowId) return;  // server workflow takes precedence
      if (initialJsonForEditor) return;
      // The draft replaces the (empty) unsaved document.
      bumpDocToken();
      setEditorJson(null);
      setInitialJsonForEditor(state);
      setEditorKey((k) => k + 1);
      setUnsavedJson(state);
    },
    [selectedWorkflowId, initialJsonForEditor, bumpDocToken, setEditorJson, setInitialJsonForEditor,
      setUnsavedJson]
  );
  const { lastSavedAt } = useAutosave({
    workspace,
    enabled: isActive && (calibrated || simMode),
    scopeKey: userId,
    onRestore: handleAutosaveRestore,
  });

  // The same crash recovery for a CODE document, which `useAutosave` cannot
  // give it: that hook is keyed on the Blockly workspace, and a code workflow
  // renders CodeWorkspace instead (see code/useCodeAutosave.js). Gated exactly
  // as the Blockly restore is — a selected cloud workflow takes precedence —
  // plus the two cases the Blockly one cannot meet: the student has already
  // chosen a document with „Neu ▾", or one is already open.
  const handleCodeAutosaveRestore = useCallback(
    (draft) => {
      if (selectedWorkflowId) return;
      if (documentChosenRef.current) return;
      if (isCodeLanguage(codeLanguage)) return;
      openCodeDocument({
        language: draft.language,
        files: draft.files,
        blocklyJson: draft.destinations ? { [DESTINATIONS_SERIALIZER_NAME]: draft.destinations } : null,
      });
      setEditorKey((k) => k + 1);
    },
    [selectedWorkflowId, codeLanguage, openCodeDocument]
  );
  useCodeAutosave({
    language: codeLanguage,
    files: codeFiles,
    // The draft carries the document's Ziele beside its files (041).
    destinations: isCodeWorkflow ? storeEntries : null,
    enabled: isActive && (calibrated || simMode),
    scopeKey: userId,
    onRestore: handleCodeAutosaveRestore,
  });

  // ONE save path. The Speichern button, Strg+S, and every caller that needs a
  // saved workflow id (Vormachen, the Sammlung drawer, „Abgeben") go through
  // saveWorkflowNow. The document is serialised when a save STARTS (the live
  // workspace first, the document's last reported edit, else what it opened
  // with), so nothing captured during an earlier round-trip is lost — and it
  // goes to the row of the document the editor HOLDS (openDocIdRef), never to
  // the one merely selected (review round 5, MD2).
  const simSceneRef = useRef(simScene);
  useEffect(() => { simSceneRef.current = simScene; }, [simScene]);
  // codeLanguageRef / codeFilesRef / codeStoreRef are written synchronously by
  // openCodeDocument and applyCodeFiles — their only writers — never by an
  // effect that could run late and put an older value back.
  // followUpToken: the document a queued follow-up belongs to, snapshotted
  // when it is QUEUED (a create in flight keeps its document's token when it
  // stamps the new id, so its follow-up becomes the update).
  const saveStateRef = useRef({
    inflight: null, followUp: null, followUpToast: false, followUpErrorToast: false, followUpToken: null,
  });

  // Every failure returns its German reason as `error` (a caller that shows
  // the failure itself — the drawer's rename — passes toastOnError: false, so
  // the student is not told twice).
  const runSave = useCallback(async ({ toastOnSuccess, toastOnError = true }) => {
    const fail = (toastText, error) => {
      if (toastOnError) toast.error(toastText);
      return { ok: false, error };
    };
    const token = accessTokenRef.current;
    if (!token) {
      return fail('Nicht angemeldet — Speichern nicht möglich.',
        new Error('Nicht angemeldet — Speichern nicht möglich.'));
    }
    // While the program the student opened is still loading, the editor holds
    // the PREVIOUS one while the selected id already names the next; a save
    // then wrote the old program into the new one's row (review round 5, MD2).
    // Every caller comes through here — the button, Strg+S, a rename, a keep,
    // „Abgeben" — so every one of them is refused, in German.
    if (documentLoadingRef.current) return fail(DE.DOCUMENT_LOADING, new Error(DE.DOCUMENT_LOADING));
    // A version restore on its way would land after this save and part the
    // cloud row from the editor (review round 5, MD3).
    if (versionRestoringRef.current) {
      return fail(DE.VERSION_RESTORE_IN_FLIGHT, new Error(DE.VERSION_RESTORE_IN_FLIGHT));
    }
    // A CODE document: the project is the thing saved, and its blockly_json
    // carries its Ziele/Positionen and nothing else (migration 041) — the
    // serializer's state under `edubotics-destinations`, or `{}` when it has
    // none, the same shape a Blockly document's serializer produces. It rides
    // the SAME PATCH as the files: the cloud writes both in one
    // update_workflow_code call (one version snapshot). The caps of §3.7 are
    // judged here with the server's own German sentences.
    const saveLanguage = codeLanguageRef.current;
    const saveFiles = codeFilesRef.current;
    const savingCode = isCodeLanguage(saveLanguage);
    if (savingCode) {
      const projectError = validateProject(saveFiles, saveLanguage);
      if (projectError) return fail(projectError, new Error(projectError));
    }
    let json = null;
    // The live canvas only while it still shows the open document.
    const ws = workspaceDocTokenRef.current === docTokenRef.current ? workspaceRef.current : null;
    if (savingCode) {
      const entries = codeStoreRef.current ? codeStoreRef.current.getEntries() : [];
      json = entries.length > 0 ? { [DESTINATIONS_SERIALIZER_NAME]: serializeState(entries) } : {};
    } else if (ws) {
      try {
        json = Blockly.serialization.workspaces.save(ws);
      } catch (_) {
        json = null;
      }
    }
    // No live canvas (it is being mounted): the document's last reported edit
    // (this page's, else the store's copy of it — cleared when a saved
    // program opens), else what it opened with — never another document's
    // leftover (review round 5).
    if (!json) json = editorJsonRef.current || unsavedJsonRef.current || initialJsonRef.current;
    if (!json) return fail('Workflow ist leer.', new Error('Workflow ist leer.'));
    // The DOCUMENT — `blocks`, `variables`, the student's canvas notes
    // (`workspaceComments`) and the Ziele/Positionen (`edubotics-destinations`)
    // — and deliberately NOT the two editor-plugin keys. `suggested-blocks`
    // grows ~16 bytes per drag and is never trimmed, so a long-lived workflow
    // eventually crosses MAX_BLOCKLY_JSON_BYTES (256 KiB) and becomes unsaveable
    // behind a German 413 the student cannot act on; `backpack` is one
    // student's private clipboard, and this row is read by group siblings,
    // cloned by `clone_workflow` and published as a classroom template. See
    // `utils/blocklyPayload.js` for the measurements and for the disclosed cost
    // (the stash no longer survives a reload). AUTOSAVE keeps the full output —
    // it is local, per-student and never shared.
    //
    // Both `blockly_json` writes below stay one key per line: the call-site
    // fence in utils/__tests__/blocklyPayload.test.js reads them line by line,
    // and a one-line object literal reads as an unslimmed writer.
    const documentJson = slimSavePayload(json);
    setSaving(true);
    // The id is taken with the document, at the same instant, and it is the
    // id of the document the editor HOLDS (review round 5, MD2): a workflow
    // picked while this save is in flight must not receive this document, and
    // one merely selected — still loading, or a load that failed — is not
    // what the editor holds. The token says whether that document is still
    // the open one when the answer comes back.
    const targetId = openDocIdRef.current;
    const targetToken = docTokenRef.current;
    try {
      if (targetId) {
        if (savingCode) {
          // The PATCH echoes the document's OWN language (the cloud answers
          // 409 on any other value; it is immutable after create) and carries
          // the Ziele beside the files — never a Blockly key (041) — but only
          // when there is something to say about them (codeZieleSendRef).
          await updateWorkflow(token, targetId, codeZieleSendRef.current
            ? {
              code_language: saveLanguage,
              code_files: saveFiles,
              blockly_json: documentJson,
              sim_scene: simSceneRef.current,
            }
            : {
              code_language: saveLanguage,
              code_files: saveFiles,
              sim_scene: simSceneRef.current,
            });
        } else {
          await updateWorkflow(token, targetId, {
            blockly_json: documentJson,
            sim_scene: simSceneRef.current,
          });
        }
        if (docTokenRef.current === targetToken) dispatch(markWorkflowSaved());
        if (toastOnSuccess) toast.success('Gespeichert.');
        return { ok: true, workflowId: targetId, created: false };
      }
      const created = await createWorkflow(token, savingCode
        ? {
          name: CODE_DE.NEW_PROGRAM_NAME,
          description: '',
          blockly_json: documentJson,
          sim_scene: simSceneRef.current,
          code_language: saveLanguage,
          code_files: saveFiles,
        }
        : {
          name: 'Neuer Workflow',
          description: '',
          blockly_json: documentJson,
          sim_scene: simSceneRef.current,
        });
      if (!created || !created.id) {
        return fail('Speichern fehlgeschlagen: keine Workflow-ID erhalten.',
          new Error('keine Workflow-ID erhalten.'));
      }
      // Stamp the new id only while the editor still shows the unsaved document
      // it was created from — the same document (its token), not merely „an
      // unsaved one": a workflow picked meanwhile keeps the editor, and so does
      // a „Neu" document opened meanwhile (both unsaved, and forcing the id
      // over it put THAT document under the new id at the next save).
      if (docTokenRef.current === targetToken && openDocIdRef.current === null) {
        skipHydrateForIdRef.current = created.id;
        setOpenDocId(created.id);
        dispatch(setSelectedWorkflowId(created.id));
        dispatch(markWorkflowSaved());
      }
      if (toastOnSuccess) toast.success('Gespeichert.');
      return { ok: true, workflowId: created.id, created: true };
    } catch (e) {
      return fail(`Speichern fehlgeschlagen: ${e.message || e}`, e);
    } finally {
      setSaving(false);
    }
  }, [dispatch, setOpenDocId]);

  // A save never JOINS a save in flight — a join reported `ok` for a document
  // it never sent. A caller arriving mid-save gets ONE coalesced follow-up that
  // serialises when IT starts (so the newest document is the last one sent);
  // the follow-up of a create is an update, never a second create.
  const saveWorkflowNow = useCallback(({ toastOnSuccess = true, toastOnError = true } = {}) => {
    const st = saveStateRef.current;
    const start = (toastFlag, errorToastFlag) => {
      const p = runSave({ toastOnSuccess: toastFlag, toastOnError: errorToastFlag });
      st.inflight = p;
      p.finally(() => { if (st.inflight === p) st.inflight = null; });
      return p;
    };
    if (!st.inflight) return start(toastOnSuccess, toastOnError);
    st.followUpToast = st.followUpToast || toastOnSuccess;
    st.followUpErrorToast = st.followUpErrorToast || toastOnError;
    if (!st.followUp) {
      st.followUpToken = docTokenRef.current;
      st.followUp = st.inflight.then(() => {}, () => {}).then(() => {
        const toastFlag = st.followUpToast;
        const errorToastFlag = st.followUpErrorToast;
        const queuedFor = st.followUpToken;
        st.followUp = null;
        st.followUpToast = false;
        st.followUpErrorToast = false;
        st.followUpToken = null;
        // Another document was opened while the follow-up waited: the one it
        // would serialise now is not the one this save was asked for.
        if (docTokenRef.current !== queuedFor) {
          return { ok: false, error: new Error('Inzwischen wurde ein anderer Workflow geöffnet.') };
        }
        return start(toastFlag, errorToastFlag);
      });
    }
    return st.followUp;
  }, [runSave]);
  const handleSave = useCallback(() => { saveWorkflowNow(); }, [saveWorkflowNow]);

  // A version the cloud restored, applied to the editor — ONLY when it still
  // belongs to the document that was open when the restore was asked for
  // (review round 5, MD3: a restore of A landing after B was opened replaced
  // B's editor, and the next save wrote A into B). This handler carries that
  // identity: VersionHistoryDropdown calls the one it held at the click.
  const restoreForId = openDocId;
  const restoreForToken = docToken;
  const handleVersionRestored = useCallback((updated) => {
    if (!updated) return;
    if (openDocIdRef.current !== restoreForId || docTokenRef.current !== restoreForToken
        || (updated.id && updated.id !== restoreForId)) {
      return;
    }
    // A run that started meanwhile keeps its program (R2-O3). The restore's own
    // in-flight flag is no reason: it is still set while its result lands.
    const runLock = runLockRef.current || (runStartingRef.current ? DE.PROGRAM_STARTING : null);
    if (runLock) {
      toast.error(runLock);
      return;
    }
    // A code version carries its files (migration 040) and its Ziele (041); a
    // legacy version restores blocks only.
    if (isCodeLanguage(updated.code_language) && updated.code_files
        && typeof updated.code_files === 'object') {
      openCodeDocument({
        language: updated.code_language,
        files: updated.code_files,
        blocklyJson: updated.blockly_json,
      });
      setEditorKey((k) => k + 1);
      return;
    }
    if (updated.blockly_json) {
      bumpDocToken();
      setEditorJson(null);
      setInitialJsonForEditor(updated.blockly_json);
      setEditorKey((k) => k + 1);
      setUnsavedJson(updated.blockly_json);
    }
  }, [restoreForId, restoreForToken, openCodeDocument, bumpDocToken, setEditorJson,
    setInitialJsonForEditor, setUnsavedJson]);

  // Why the history takes no restore right now, or null: a program runs or
  // starts, the opened program is still loading (it would restore the ROW
  // while the editor waits for another), or a save is on its way (a restore
  // landing under it would part the cloud row from the editor). Its own
  // restore in flight it handles itself.
  const historyLockReason = runLockReason
    || (runStarting ? DE.PROGRAM_STARTING : null)
    || (hydrating ? DE.DOCUMENT_LOADING : null)
    || (saving ? DE.SAVE_IN_FLIGHT : null);

  // The open workflow's recordings (studioAssets.trajectories): on open, when a
  // token first appears, and when the tab regains focus.
  // Keyed on the document the editor HOLDS (review round 5): while the next
  // program loads, the drawer still shows — and acts on — the open one's.
  const refetchTrajectories = useCallback(() => {
    if (!isActive || !openDocId || !accessTokenRef.current) return;
    dispatch(fetchTrajectories({ accessToken: accessTokenRef.current, workflowId: openDocId }));
  }, [isActive, openDocId, dispatch]);
  useEffect(() => { refetchTrajectories(); }, [refetchTrajectories, hasAccessToken]);
  useRefetchOnFocus(isActive ? refetchTrajectories : null);

  // The Sammlung provider: ONE object for the page's lifetime (BlocklyWorkspace
  // reads it through a ref), fed a snapshot of the rig and the recording list.
  const sammlungProvider = useMemo(() => createSammlungProvider(), []);
  // Fails CLOSED on an unanswered bridge probe (see previewLeaderGate); before the
  // FIRST answer every preview ▶ is also disabled (`previewPending` below).
  const previewGate = previewLeaderGate(rsBridge, caps);
  const previewPending = previewGate.rsLeaderPending;
  // ▶ on a card or in the drawer: a generated SIM run (hooks/useSimPreview.js).
  // It never addresses the real arm (no /workshop/replay, no /workshop/jog).
  const { startPreview } = useSimPreview({
    workspace,
    destinationStore: activeStore,
    simScene,
    workflowId: openDocId,
    accessToken,
    robotType,
    gates: {
      heartbeatStatus,
      runState,
      paused,
      documentLoading: hydrating,
      versionRestoring,
      teachOpen,
      jogHandGuideOn,
      simMode,
      activeTutorialId,
      ...previewGate,
    },
    ensureSimMode,
  });
  // The provider's action handler is installed once; it reaches the latest
  // startPreview through this ref.
  const startPreviewRef = useRef(startPreview);
  startPreviewRef.current = startPreview;
  // ▶ / „Im Simulator zeigen" on a point-shaped VARIABLE is not a run: open the
  // simulator (never the trail) and highlight its marker — no service call.
  // Every other kind goes to the generated sim run. Shared by the flyout card
  // action and the drawer, so both routes behave identically.
  // The highlight the student ASKED for (a drawer focus, a variable's
  // „Im Simulator zeigen"), as { kind, id }. A card hover is transient: when it
  // ends the highlight falls back to this one instead of to nothing, and the
  // drawer retires it when its focus or tab changes or it closes.
  const drawerHighlightRef = useRef(null);
  const previewAsset = useCallback(async (asset, options) => {
    if (asset && asset.kind === 'variable') {
      // A refused sim entry (a running program, a tutorial) has already toasted;
      // highlighting a marker on a stage that never opened would be a lie.
      if (!(await ensureSimMode({ showPath: false }))) return;
      const highlight = { kind: 'variable', id: `var:${asset.name}` };
      drawerHighlightRef.current = highlight;
      dispatch(setHighlight(highlight));
      return;
    }
    await startPreviewRef.current(asset, options);
  }, [ensureSimMode, dispatch]);
  useEffect(() => {
    sammlungProvider.setSnapshot({
      capabilities: {
        hardware: true,
        simMode,
        teach: true,
        drawer: true,
        preview: true,
        // A sim-run ▶ (recording, Ziel, Position — never a variable's marker)
        // is drawn disabled until the control bridge has answered once.
        previewPending,
        previewVariables: true,
        pinCamera: !!calibrated && !simMode,
        pinSim: true,
      },
      robotType: robotType || '',
      trajectories: {
        status: (trajectoryList && trajectoryList.status) || 'none',
        items: (trajectoryList && Array.isArray(trajectoryList.items)) ? trajectoryList.items : [],
      },
      lastPreviewResult: lastPreviewResult || {},
      variableValues: variableValues || {},
      restrictedBlocks: Array.isArray(restrictedBlocks) ? restrictedBlocks : null,
    });
  }, [sammlungProvider, simMode, calibrated, previewPending, robotType, trajectoryList,
    lastPreviewResult, variableValues, restrictedBlocks]);
  useEffect(() => {
    sammlungProvider.setActionHandler((action) => {
      if (!action || typeof action !== 'object') return;
      if (action.type === 'pinCamera') {
        setDockCollapsed(false);
        setDockOpen((prev) => (prev.includes('camera') ? prev : addOpenTab(prev, 'camera', isTabBusy)));
        toast(DE.FLY_PIN_CAMERA_HINT, { icon: toastIcon('camera') });
      } else if (action.type === 'jumpToBlock') {
        jumpToBlock(workspaceRef.current, action.blockId);
      } else if (action.type === 'manage') {
        // The card or „Alle verwalten …" already names the tab (recording →
        // aufnahmen, pin → ziele, pose → positionen, variable → variablen).
        dispatch(openDrawer({ tab: action.tab, focusId: action.focusId ?? null }));
      } else if (action.type === 'teach') {
        // TeachHost judges the gates (and a glide) when it processes the
        // request; the slice drops one that names no window kind.
        dispatch(requestTeach({ kind: action.kind }));
      } else if (action.type === 'preview') {
        // The flyout ▶ always plays at tempo 1.0 (a variable: its marker).
        previewAsset(action.asset);
      } else if (action.type === 'pinSim') {
        // Never turns the trail on (ensureSimMode showPath: false).
        ensureSimMode({ showPath: false }).then((entered) => {
          if (entered) setSimZielRequest({ mode: 'ziel', token: Date.now() });
        });
      } else if (action.type === 'highlight') {
        dispatch(setHighlight(action.asset ?? drawerHighlightRef.current));
      }
    });
  }, [sammlungProvider, isTabBusy, dispatch, ensureSimMode, previewAsset]);
  // A Ziel/Position focused in the drawer highlights its marker; clearing that
  // focus (or leaving those tabs) drops only the highlight the drawer set.
  // drawerHighlightRef is declared with previewAsset above.
  const drawerOpen = !!(drawer && drawer.open);
  const drawerTab = drawer ? drawer.tab : null;
  const drawerFocusId = drawer ? drawer.focusId : null;
  useEffect(() => {
    let kind = null;
    if (drawerTab === 'ziele') kind = 'pin';
    else if (drawerTab === 'positionen') kind = 'pose';
    if (drawerOpen && kind && drawerFocusId) {
      drawerHighlightRef.current = { kind, id: drawerFocusId };
      dispatch(setHighlight({ kind, id: drawerFocusId }));
    } else if (drawerHighlightRef.current) {
      drawerHighlightRef.current = null;
      dispatch(setHighlight(null));
    }
  }, [drawerOpen, drawerTab, drawerFocusId, dispatch]);
  // The drawer belongs to the editor it was opened over. Redux keeps
  // `drawer.open` across the Galerie switch and a tab change, so without this
  // it reappeared over a freshly mounted editor.
  useEffect(() => {
    if (!isActive || view === 'gallery') dispatch(closeDrawer());
  }, [isActive, view, dispatch]);
  // RunControls writes its IK pre-check warnings UNKEYED; re-apply the keyed
  // missing-name warnings after each change (forced — the validator caches).
  useEffect(() => {
    if (workspace) refreshAssetReferenceWarnings(workspace, { force: true });
  }, [debuggerWarnings, workspace]);

  if (!isActive) return null;

  // Per-panel gating (same rules as before — the panels just moved into the dock).
  // Vormachen owns the arm while it is open, so JogPanel's torque-off and nudges
  // are locked (its „Arm festsetzen" stays usable — JogPanel never disables it).
  const jogDisabled =
    heartbeatStatus !== 'connected' || runState === 'running' || teachOpen;
  const teachReasonText = teachReason ? TEACH_BLOCK_TITLES_DE[teachReason] : null;
  const teachChooserItems = TEACH_KINDS.map((kind) => ({
    id: kind,
    label: TEACH_KIND_LABEL_DE[kind],
    icon: TEACH_KIND_ICON[kind],
    onSelect: () => dispatch(requestTeach({ kind })),
  }));

  // Editor/Galerie switch — shared by both views (in the editor toolbar, and as a
  // standalone strip in the gallery view where the toolbar is absent).
  const viewTabs = (
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
  );

  // „Kalibrierung neu starten" — a calibrated student can re-enter the wizard
  // (e.g. the cameras moved between sessions). Resets per-step badges; the on-disk
  // YAMLs survive and are overwritten as each step is re-run.
  const recalibrateButton = (
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
  );

  // Dock tab registry. `render` is INVOKED by RightDock (never used as a
  // `<tab.render/>` element type), so each panel INSTANCE stays mounted across
  // dock re-renders / resize — critical for JogPanel, whose unmount teardown
  // re-torques the arm. The whole dock is REPLACED
  // by SimStage while in the simulator (see the render tree), so these tabs never
  // render during sim — no `hidden: simMode` / SimScene special-casing is needed.
  const dockTabs = [
    {
      id: 'camera',
      label: DE.DOCK_TAB_CAMERA,
      icon: 'camera',
      // Only the feed: capturing the arm's pose moved into Vormachen („P").
      render: () => (
        <div className="flex flex-col gap-2 h-full">
          {/* The feed fills the panel height (fill), so a taller panel shows a
              bigger camera instead of a small strip with empty space below. */}
          <div className="flex-1 min-h-0">
            <CameraFeedOverlay
              camera="scene"
              clickable={true}
              fill
              resolveMarkLabel={resolveMarkLabel}
              onMark={handleMarkDestination}
              onRenameMark={handleRenameMarked}
            />
          </div>
        </div>
      ),
    },
    {
      id: 'control',
      label: DE.DOCK_TAB_CONTROL,
      icon: 'gamepad',
      busy: jogHandGuideOn,
      render: () => (
        <JogPanel disabled={jogDisabled} onHandGuideChange={setJogHandGuideOn} />
      ),
    },
    {
      id: '3d',
      label: DE.DOCK_TAB_3D,
      icon: 'box',
      render: () => (
        <div className="flex flex-col gap-2 h-full">
          <div className="flex items-center gap-1.5 flex-wrap shrink-0">
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
          </div>
          <div className="relative flex-1 min-h-0 w-full bg-[#1a1d23] rounded-md overflow-hidden">
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
                markers={markers}
                ghostJoints={ghostJoints}
              />
            </Suspense>
            {showFrames && (
              <div className="absolute bottom-1.5 left-1.5 z-10 px-2 py-0.5 rounded bg-black/55 text-[10px] text-white/80 font-mono">
                X rot · Y grün · Z blau
              </div>
            )}
          </div>
        </div>
      ),
    },
    {
      id: 'tutorial',
      label: DE.DOCK_TAB_TUTORIAL,
      icon: 'graduationCap',
      busy: !!activeTutorialId,
      render: () => <SkillmapPlayer />,
    },
    {
      id: 'debug',
      label: DE.DOCK_TAB_DEBUG,
      icon: 'debug',
      // The open document's language decides the „Haltepunkte" tab: a code
      // program has no blocks to Alt-click, and Java has no breakpoints at all
      // this round (A8). It is a page-level fact — the dock's panels are
      // rendered here — and the panel has no other route to it.
      render: () => <DebugPanel workspace={workspace} codeLanguage={codeLanguage} />,
    },
  ];

  return (
    // ONE warned glide-to-Grundstellung prompt for the whole page: its three
    // callers (TableTouchStep, JogPanel, Vormachen) can each unmount while a
    // countdown is still running — see HomeGlidePrompt.
    <HomeGlideProvider>
    <div className="flex flex-col h-full w-full overflow-hidden">
      <header className="px-3 sm:px-4 py-2 border-b border-[var(--line)] bg-white shrink-0">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="min-w-0 flex items-baseline gap-2 flex-wrap">
            <h1 className="text-base sm:text-lg font-semibold text-[var(--ink)]">
              Roboter Studio
            </h1>
            <p className="text-xs text-[var(--ink-3)] hidden lg:block truncate">
              {!showEditor
                ? 'Bevor wir loslegen können, muss die Kamera eingerichtet werden.'
                : isCodeWorkflow
                  ? CODE_DE.HEADER_HINT_CODE
                  : 'Bausteine ziehen, Aufgabe zusammenstellen und vom Roboter ausführen lassen.'}
            </p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {/* „Abgeben": hand the teacher this stand (saves first, then
                POST /workflows/{id}/submit). Blocks and code alike. */}
            {showEditor && (
              <SubmitButton
                accessToken={accessToken}
                saveWorkflowNow={saveWorkflowNow}
                running={runState === 'running'}
              />
            )}
            {/* Phase-3: open the editor on a virtual arm — no calibration, no
                real robot. The interpreter + IK run for real on a simulated
                arm; only logic, order and reachability are validated. */}
            <button
              type="button"
              onClick={() => {
                // Belt-and-suspenders (the button is already disabled for all
                // four): never toggle while a sim run is in flight, and never
                // ENTER sim during an active tutorial (replacing the dock would
                // unmount SkillmapPlayer + lift the toolbox restriction
                // mid-tutorial), while Vormachen is open (it owns the REAL arm,
                // which the simulator would hide under the student's hand), or
                // while the arm is hand-guided (unmounting JogPanel re-torques /
                // resets the live hand-guide session under the student's hand).
                if (simEntryReason) return;
                setSimMode((v) => {
                  const next = !v;
                  // The avoidance trail is a headline sim feature — turn it on
                  // once on ENTRY (the student can still toggle it off; real-mode
                  // shares this same showPath state, so leaving sim keeps it).
                  if (next) setShowPath(true);
                  return next;
                });
              }}
              aria-pressed={simMode}
              disabled={!!simEntryReason}
              title={simEntryReason
                ? SIM_ENTRY_BLOCK_TITLES_DE[simEntryReason]
                : SIM_TOGGLE_DEFAULT_TITLE_DE}
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
            <LeaderToggle
              isActive={isActive}
              lockedReason={teachOpen ? DE.TEACH_BLOCK_UI_LOCKED : null}
            />
          </div>
        </div>
      </header>
      {/* Vormachen: the full-screen teaching overlay, opened from the toolbar
          button or a Sammlung flyout („✋ … vormachen"). */}
      <TeachHost
        isActive={isActive}
        workspace={workspace}
        assetDoc={teachAssetDoc}
        accessToken={accessToken}
        workflowId={openDocId}
        robotType={robotType}
        caps={caps}
        heartbeatStatus={heartbeatStatus}
        runState={runState}
        paused={paused}
        simMode={simMode}
        jogHandGuideOn={jogHandGuideOn}
        previewActive={previewActive}
        rsBridge={rsBridge}
        saveWorkflowNow={saveWorkflowNow}
        refetchTrajectories={refetchTrajectories}
      />
      <main className="flex-1 overflow-hidden flex flex-col min-h-0">
        {showEditor ? (
          <>
            {view === 'gallery' ? (
              <>
                {/* Gallery view has no editor toolbar — a slim strip carries the
                    view switch + „Öffnen". */}
                <div className="px-3 sm:px-4 py-1.5 flex items-center gap-2 flex-wrap border-b border-[var(--line)] bg-white shrink-0">
                  {viewTabs}
                  <OpenWorkflowPopover onPicked={handlePickWorkflow} lockedReason={switchLockReason} />
                </div>
                <div className="flex-1 min-h-0 p-3 sm:p-4 overflow-auto">
                  <GalleryTab
                    lockedReason={switchLockReason}
                    onPicked={(wf) => {
                      handlePickWorkflow(wf);
                      setView('editor');
                    }}
                  />
                </div>
              </>
            ) : (
              <>
                {/* One compact toolbar: view switch + „Öffnen" (leading) · editor
                    tools · Verlauf + Kalibrieren (extra) · autosave. */}
                <ToolbarButtons
                  workspace={workspace}
                  lastSavedAt={lastSavedAt}
                  onSave={handleSave}
                  saving={saving}
                  leading={
                    <>
                      {viewTabs}
                      <NewProgramDialog
                        onCreate={handleNewProgram}
                        disabled={!!switchLockReason}
                        disabledReason={switchLockReason}
                      />
                      <OpenWorkflowPopover onPicked={handlePickWorkflow} lockedReason={switchLockReason} />
                    </>
                  }
                  extra={
                    <>
                      {/* One focused window per kind (owner decisions D1, D4):
                          the chooser names the three. */}
                      <MenuButton
                        label={DE.TOOLBAR_TEACH}
                        icon="hand"
                        menuLabel={DE.TEACH_CHOOSER_MENU}
                        items={teachChooserItems}
                        disabled={!!teachReason || teachOpen}
                        title={teachReasonText || DE.TOOLBAR_TEACH_TITLE}
                        buttonClassName={
                          'text-xs px-2.5 py-1 rounded-md border disabled:opacity-50 '
                          + 'disabled:cursor-not-allowed bg-[var(--accent)] text-white '
                          + 'border-[var(--accent)] hover:opacity-90'
                        }
                      />
                      <VersionHistoryDropdown
                        workflowId={openDocId}
                        lockedReason={historyLockReason}
                        onRestoringChange={handleRestoringChange}
                        onRestore={handleVersionRestored}
                      />
                      {recalibrateButton}
                    </>
                  }
                />
                {/* Editor + right region. The Blockly editor grows to fill the row
                    on the LEFT; the right region is normally the tabbed RightDock
                    and — while in the simulator — the integrated SimStage instead.
                    The LEFT editor column is the SAME first child in both branches,
                    so swapping the right sibling NEVER remounts the workspace (it
                    is keyed only by editorKey). On mobile the two stack. */}
                <div
                  ref={dockRowRef}
                  className="flex-1 min-h-0 flex flex-col md:flex-row overflow-hidden"
                >
                  <div className="flex-1 min-w-0 min-h-0 p-2 md:p-3 md:pr-0">
                    {/* No min-height: Blockly must fill exactly the box that is
                        visible (see BlocklyWorkspace). On narrow screens the dock
                        is height-capped instead, so the editor keeps ≥ half. */}
                    {/* `relative`: the Sammlung drawer is positioned inside this
                        box beside the toolbox and never changes its size. */}
                    <div className="relative h-full bg-white rounded-lg border border-[var(--line)] overflow-hidden">
                      {/* One workflow is one language: a code document mounts the
                          file tree + CodeMirror host and NEVER the Blockly canvas
                          (the Sammlung drawer belongs to that canvas). While the
                          row is being fetched, neither — the language is not
                          known yet. */}
                      {hydrating ? (
                        <p className="px-3 py-4 text-xs text-[var(--ink-3)]">
                          {CODE_DE.WORKFLOW_LOADING}
                        </p>
                      ) : isCodeWorkflow ? (
                        <CodeWorkspace
                          key={editorKey}
                          language={codeLanguage}
                          files={codeFiles}
                          onFilesChange={applyCodeFiles}
                          assetDoc={assetDoc}
                          provider={sammlungProvider}
                          onCursorChange={handleCodeCursorChange}
                          revealRequest={codeRevealRequest}
                          objectTypes={codeObjectTypes}
                        />
                      ) : (
                        <BlocklyWorkspace
                          key={editorKey}
                          initialJson={initialJsonForEditor}
                          onChange={handleEditorChange}
                          onWorkspaceReady={handleWorkspaceReady}
                          restrictedBlocks={restrictedBlocks}
                          sammlungProvider={sammlungProvider}
                        />
                      )}
                      {drawer && drawer.open && (
                        <SammlungDrawer
                          assetDoc={assetDoc}
                          workspace={workspace}
                          provider={sammlungProvider}
                          accessToken={accessToken}
                          workflowId={openDocId}
                          robotType={robotType}
                          onPreview={previewAsset}
                          saveWorkflowNow={saveWorkflowNow}
                          refetchTrajectories={refetchTrajectories}
                        />
                      )}
                    </div>
                  </div>
                  {simMode ? (
                    // Integrated sim stage: replaces BOTH the drag divider and the
                    // dock. It owns its OWN width (§6.6) — a collapsed dock must
                    // never yield a blank right region — and hosts the single
                    // UrdfTwin via SimScene(layout="split").
                    <SimStage
                      scene={simScene}
                      onChange={setSimScene}
                      catalog={objectCatalog}
                      catalogDims={catalogDims}
                      workspace={workspace}
                      codeLanguage={codeLanguage}
                      debugOpen={simDebugOpen}
                      onToggleDebug={() => setSimDebugOpen((v) => !v)}
                      showPath={showPath}
                      onToggleShowPath={() => setShowPath((v) => !v)}
                      onClearPath={() => setPathClearToken((t) => t + 1)}
                      pathClearToken={pathClearToken}
                      markers={markers}
                      ghostJoints={ghostJoints}
                      requestedMode={simZielRequest}
                      onCreateDestination={handleCreateSimDestination}
                    />
                  ) : (
                    <>
                      {/* Drag divider — students rebalance the blocks area vs. the
                          dock. Desktop only (mobile stacks); hidden when the dock is
                          collapsed to its rail. */}
                      {!dockCollapsed && (
                        <div
                          role="separator"
                          aria-orientation="vertical"
                          onPointerDown={onDockResizeDown}
                          onPointerMove={onDockResizeMove}
                          onPointerUp={onDockResizeUp}
                          title="Breite ziehen, um die Größe zu ändern"
                          className="hidden md:flex shrink-0 w-2 cursor-col-resize items-center justify-center group touch-none"
                        >
                          <span className="h-10 w-1 rounded-full bg-[var(--line-2)] group-hover:bg-[var(--accent)]" />
                        </div>
                      )}
                      <RightDock
                        tabs={dockTabs}
                        openIds={dockOpen}
                        collapsed={dockCollapsed}
                        width={dockWidth}
                        onToggleTab={handleToggleTab}
                        onCloseTab={handleCloseTab}
                        onToggleCollapsed={handleToggleCollapsed}
                      />
                    </>
                  )}
                </div>
                {/* Read-only Python „Code"-Vorschau — compact, collapsible,
                    full-width strip between the editor row and the run controls.
                    A Blockly affordance: a code program IS its code. */}
                {!isCodeWorkflow && (
                <div className="px-2 sm:px-3 pb-1 shrink-0">
                  <div className="rounded-lg border border-[var(--line)] bg-white overflow-hidden">
                    <div className="flex items-center gap-2 px-3 py-1 border-b border-[var(--line)]">
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
                        {DE.DOCK_CODE_LABEL} anzeigen
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
                          changeToken={editorJson || unsavedBlocklyJson || initialJsonForEditor}
                        />
                      </Suspense>
                    )}
                  </div>
                </div>
                )}
                <RunControls
                  workflowId={openDocId}
                  blocklyJson={editorJson || unsavedBlocklyJson || initialJsonForEditor}
                  workspace={workspace}
                  simMode={simMode}
                  simScene={simScene}
                  codeLanguage={codeLanguage}
                  codeFiles={codeFiles}
                  destinationStore={activeStore}
                  startBlockedReason={startBlockedReason}
                  onStartingChange={handleRunStarting}
                  getDocumentToken={getDocumentToken}
                  debugOpen={simMode ? simDebugOpen : dockOpen.includes('debug')}
                  onToggleDebug={() => {
                    if (simMode) setSimDebugOpen((v) => !v);
                    else handleToggleTab('debug');
                  }}
                />
              </>
            )}
          </>
        ) : (
          <CalibrationWizard />
        )}
      </main>
    </div>
    </HomeGlideProvider>
  );
}

export default WorkshopPage;
