// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Daten tab (Daten 2.0, the owner-approved mockup; spec §F, §G): the
// library and the player, every dialog, the jobs this page starts and the
// states the tab can be in — cloud mode, no identity, no Hugging Face account,
// the robot offline, an old image (only `copy.old.image`), the sidecar down.
//
// Every mutating action goes through `/daten/command` (§J.3) and shows the
// robot's German sentence when it refuses; edits, deletes, downloads and
// „Beide behalten" are jobs the page follows on /edubotics/daten_state (§D4).
// Nothing here is persisted (§G4).

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';

import Icon from '../icons/Icon';
import LibraryView from './LibraryView';
import PlayerView from './PlayerView';
import EpisodeToolDialog from './dialogs/EpisodeToolDialog';
import ConfirmDeleteMarked from './dialogs/ConfirmDeleteMarked';
import ConfirmDeleteDataset from './dialogs/ConfirmDeleteDataset';
import UploadCompareDialog from './dialogs/UploadCompareDialog';
import PullNewerDialog from './dialogs/PullNewerDialog';
import NewerWarnDialog from './dialogs/NewerWarnDialog';
import FetchDialog from './dialogs/FetchDialog';
import ProgressDialog from './dialogs/ProgressDialog';
import { LEADER_COLOR } from './theme';
import {
  toastBusy, toastError, toastOk, toastWarn,
} from './datenToasts';
import './daten.css';

import COPY from '../../features/editDataset/datenCopy';
import {
  addTransfer, clearMarks, openPlayer, removeTransfer, setLibraryFilter, showLibrary,
} from '../../features/editDataset/editDatasetSlice';
import useDatenCommand from '../../features/editDataset/hooks/useDatenCommand';
import useDatenSession from '../../features/editDataset/hooks/useDatenSession';
import useDatenState, { busyKindsById } from '../../features/editDataset/hooks/useDatenState';
import useDatenJobs, { stepOfStage } from '../../features/editDataset/hooks/useDatenJobs';
import useGroupNamespaces from '../../features/editDataset/hooks/useGroupNamespaces';
import { libraryCards, syncAfterLocalEdit } from '../../features/editDataset/model/libraryState';
import { cardModel } from '../../features/editDataset/model/cardModel';
import { taskNameOf } from '../../features/editDataset/model/labels';
import { fill, fmtBytes, plural } from '../../features/editDataset/model/format';
import { selectHfAccount, selectHfInSync } from '../../features/hfToken/hfTokenSelectors';
import { setDatasetRepoId, setSelectedDataset, setSelectedUser } from '../../features/training/trainingSlice';
import { moveToPage } from '../../features/ui/uiSlice';
import PageType from '../../constants/pageType';
import useSignalStatus from '../../hooks/useSignalStatus';
import { isCloudOnlyMode } from '../../utils/cloudMode';

const EMPTY_LIST = Object.freeze([]);

/** The HF worker's plain completion line (anything else on Success is a sentence worth showing, e.g. „noch nicht bestätigt"). */
const PLAIN_UPLOAD_SUCCESS = /^Hugging Face-Upload abgeschlossen/;

/** `value`, or the previous one when it has the same JSON — a stable prop for a memoised child. */
function useStableValue(value) {
  const ref = useRef({ json: null, value });
  const json = JSON.stringify(value);
  if (ref.current.json !== json) ref.current = { json, value };
  return ref.current.value;
}

function StateBox({ icon, text, children }) {
  return (
    <div className="dat-main">
      <div className="dat-state" role="status">
        <Icon name={icon} size={22} className="dat-state-icon" />
        <span>{text}</span>
        {children}
      </div>
    </div>
  );
}

/** What a card is called: its display name, else its folder name. */
const nameOf = (card) => (card.local && card.local.display_name) || card.name;

/** The running job that touches dataset `id` (as source or output), if any. */
function jobFor(payload, id) {
  if (!payload || !Array.isArray(payload.jobs)) return null;
  return payload.jobs.find((j) => j.state === 'running'
    && ((j.outputs || []).includes(id) || (j.datasets || []).includes(id))) || null;
}

export default function DatenPage() {
  const dispatch = useDispatch();
  const cloud = isCloudOnlyMode();
  const isAuthenticated = useSelector((s) => s.auth.isAuthenticated);
  const authLoading = useSelector((s) => s.auth.isLoading);
  const profileLoaded = useSelector((s) => s.auth.profileLoaded);
  const hfUsername = useSelector((s) => s.auth.hfUsername);
  const heartbeat = useSelector((s) => s.tasks.heartbeatStatus);
  const statusRobotType = useSelector((s) => s.tasks.taskStatus.robotType);
  const inSync = useSelector(selectHfInSync);
  const accountFp = useSelector((s) => selectHfAccount(s).fp);
  const view = useSelector((s) => s.editDataset.view);
  const openId = useSelector((s) => s.editDataset.openId);
  const filter = useSelector((s) => s.editDataset.libraryFilter);
  const uploadPctRaw = useSelector((s) => s.editDataset.uploadStatus.percentage);
  const transfers = useSelector((s) => s.editDataset.transfers);

  const connected = heartbeat === 'connected';
  const identity = !cloud && isAuthenticated && !!hfUsername;
  const command = useDatenCommand();
  const group = useGroupNamespaces();
  const groupReady = group.status === 'ready' || group.status === 'error';
  const enabled = identity && connected && groupReady;
  const datenState = useDatenState({ enabled: identity && connected });
  const session = useDatenSession({
    enabled, command, namespaces: group.namespaces, inSync, accountFp, datenState,
  });
  const signal = useSignalStatus({ enabled: connected });
  const diskFree = signal.payload && signal.payload.disk ? signal.payload.disk.free_bytes : null;
  const jobs = useDatenJobs({ datenState, command });

  const [dialog, setDialog] = useState(null);
  const [menusOpen, setMenusOpen] = useState(0);
  const [merge, setMerge] = useState({ active: false, ids: [] });
  const [query, setQuery] = useState('');
  const [newerAck, setNewerAck] = useState({});
  const cancelledRef = useRef(new Set());
  // The library as of the last reply — after an awaited `loadLibrary` the
  // reply it waited for, never the state of the last render (V2-5).
  const getLib = session.getLib;

  const own = group.own;
  const robotType = session.lib.robotType || statusRobotType || 'omx_f';
  const payload = jobs.payload;
  const busy = useMemo(() => busyKindsById(payload), [payload]);
  const transferRepo = payload && payload.transfer && payload.transfer.kind === 'upload' ? payload.transfer.repo_id : null;
  const uploadPct = Number(uploadPctRaw) || 0;

  // ---- the cards -------------------------------------------------------------
  const baseCards = useMemo(() => libraryCards(session.lib, group.namespaces), [session.lib, group.namespaces]);
  const cards = useMemo(() => {
    // A download into a dataset the list does not show yet (a fetched copy):
    // its card appears at once with the download's progress.
    const known = new Set(baseCards.map((c) => c.id));
    const extra = [];
    ((payload && payload.jobs) || []).forEach((j) => {
      if (j.op !== 'download' || j.state !== 'running') return;
      const target = (j.outputs || [])[0];
      if (!target || known.has(target) || !group.namespaces.includes(target.split('/')[0])) return;
      known.add(target);
      extra.push({
        id: target, ns: target.split('/')[0], name: target.split('/')[1], local: null, hub: null,
        sync: { state: 'unknown', reason: 'not_asked', head: null }, date: Date.now(),
      });
    });
    return extra.length ? [...extra, ...baseCards] : baseCards;
  }, [baseCards, payload, group.namespaces]);

  const models = useMemo(() => {
    const out = {};
    cards.forEach((c) => {
      out[c.id] = cardModel(c, {
        own,
        ownerNames: group.names,
        busyKinds: busy[c.id] || EMPTY_LIST,
        job: jobFor(payload, c.id),
        uploadPct: transferRepo === c.id ? uploadPct : null,
        stateSeen: session.lib.stateSeen,
        lib: session.lib,
        inSync,
        hubLoading: session.hubLoading,
        mergeMode: merge.active,
        mergeSelected: merge.ids.includes(c.id),
      });
    });
    return out;
  }, [cards, own, group.names, busy, payload, transferRepo, uploadPct, session.lib, session.hubLoading, inSync, merge]);

  const counts = useMemo(() => ({
    all: cards.length,
    changed: cards.filter((c) => c.sync && c.sync.state === 'changed').length,
    local: cards.filter((c) => c.sync && c.sync.state === 'local').length,
  }), [cards]);
  const localIds = useMemo(() => new Set(Object.keys(session.lib.local)), [session.lib.local]);
  const hubIds = useMemo(() => new Set(Object.keys((session.lib.hub && session.lib.hub.entries) || {})), [session.lib.hub]);

  // ---- helpers -----------------------------------------------------------------
  const refetch = session.refetchIds;
  const trackJob = jobs.track;

  const toTraining = useCallback((card) => {
    dispatch(setSelectedUser(card.ns));
    dispatch(setSelectedDataset(card.name));
    dispatch(setDatasetRepoId(card.id));
    dispatch(moveToPage(PageType.TRAINING));
  }, [dispatch]);

  /** Run one command; on a refusal show the robot's sentence (and re-read a stale dataset). */
  const run = useCallback(async (action, args, { staleIds = [] } = {}) => {
    const r = await command(action, args);
    if (!r.ok) {
      toastError(r.message || COPY.http.generic);
      if (r.code === 'stale' && staleIds.length) refetch(staleIds);
    }
    return r;
  }, [command, refetch]);

  const startMerge = useCallback((id) => {
    dispatch(showLibrary());
    setMerge({ active: true, ids: id ? [id] : [] });
  }, [dispatch]);

  // ---- jobs --------------------------------------------------------------------
  const runDownload = useCallback(async (card, { mode, revision, target, displayName = null, repo = null, open = false }) => {
    const r = await run('download', {
      repo_id: repo || card.id,
      revision,
      target: target || card.id,
      mode,
      display_name: displayName,
      meta_digest: mode === 'replace' && card.local ? card.local.meta_digest : '',
    }, { staleIds: [card.id] });
    if (!r.ok) return;
    const tgt = r.result.target || target || card.id;
    const label = displayName || nameOf(card);
    trackJob(r.result.job_id, {
      op: 'download',
      onDone: () => {
        toastOk(fill(COPY.toast.downloadDone, { name: label }));
        setNewerAck((a) => ({ ...a, [tgt]: false }));
        session.loadLibrary({ hub: true, ids: [tgt] }).then(() => {
          if (open) dispatch(openPlayer(tgt));
        });
      },
      onFailed: (row) => {
        if (cancelledRef.current.has(`download:${row.job_id}`) || row.code === 'cancelled') return;
        toastError(row.message || COPY.http.generic);
        refetch([tgt]);
      },
    });
  }, [run, trackJob, session, dispatch, refetch]);

  const runKeepBoth = useCallback(async (card, head) => {
    const r = await run('keep_both', {
      dataset: card.id, expected_hub_sha: head, meta_digest: card.local ? card.local.meta_digest : '',
    }, { staleIds: [card.id] });
    if (!r.ok) return;
    dispatch(addTransfer({ repoId: card.id, kind: 'upload', id: card.id, via: 'keep_both' }));
    trackJob(r.result.job_id, {
      op: 'keep_both',
      dialog: true,
      title: COPY.keepBoth.progressTitle,
      steps: [COPY.keepBoth.stepDownload, COPY.keepBoth.stepMerge, COPY.keepBoth.stepUpload],
      onDone: () => {
        dispatch(removeTransfer(card.id));
        session.loadLibrary({ hub: true, ids: [card.id] }).then((landed) => {
          const e = landed ? getLib().local[card.id] : null;
          toastOk(e && Number.isFinite(Number(e.total_episodes))
            ? fill(COPY.keepBoth.done, { n: e.total_episodes })
            : COPY.keepBoth.doneNoCount);
        });
      },
      onFailed: (row) => {
        dispatch(removeTransfer(card.id));
        toastError(row.message || COPY.http.generic);
        refetch([card.id]);
      },
    });
  }, [run, trackJob, dispatch, session, getLib, refetch]);

  const runUpload = useCallback(async (card, args) => {
    const r = await run('upload', { dataset: card.id, ...args }, { staleIds: [card.id] });
    if (!r.ok) return;
    const repoId = r.result.repo_id || card.id;
    cancelledRef.current.delete(repoId);
    dispatch(addTransfer({ repoId, kind: 'upload', id: card.id }));
    if (view === 'player') toastBusy(fill(COPY.toast.uploadStarted, { name: nameOf(card) }));
  }, [run, dispatch, view]);

  const runDeleteDataset = useCallback(async (card) => {
    const r = await run('delete_dataset', {
      dataset: card.id, meta_digest: card.local ? card.local.meta_digest : '',
    }, { staleIds: [card.id] });
    if (!r.ok) return;
    const size = card.local && card.local.size_bytes !== undefined ? fmtBytes(card.local.size_bytes) : null;
    trackJob(r.result.job_id, {
      op: 'delete_dataset',
      dialog: true,
      title: COPY.progress.deleteDatasetTitle,
      sub: COPY.progress.wait,
      steps: [],
      onDone: () => {
        toastOk(size ? fill(COPY.toast.datasetDeleted, { size }) : COPY.toast.datasetDeletedNoSize);
        dispatch(clearMarks(card.id));
        dispatch(showLibrary());
        session.loadLibrary({ hub: true, ids: [card.id] });
      },
      onFailed: (row) => {
        toastError(row.message || COPY.http.generic);
        refetch([card.id]);
      },
    });
  }, [run, trackJob, dispatch, session, refetch]);

  const runEdit = useCallback(async (card, payloadArgs, ui) => {
    const r = await run('edit', { ...payloadArgs }, { staleIds: [card.id] });
    if (!r.ok) return;
    trackJob(r.result.job_id, {
      op: payloadArgs.op,
      dialog: true,
      title: ui.title,
      sub: ui.sub,
      steps: ui.steps,
      onDone: (row) => {
        dispatch(clearMarks(card.id));
        const outputs = (row.outputs && row.outputs.length ? row.outputs : r.result.outputs) || [];
        session.loadLibrary({ hub: true, ids: [...new Set([card.id, ...outputs])] })
          .then((landed) => ui.onDone(outputs, landed));
      },
      onFailed: (row) => {
        toastError(row.message || COPY.http.generic);
        refetch([card.id]);
      },
    });
  }, [run, trackJob, dispatch, session, refetch]);

  // ---- a Daten upload's end (its result arrives on /huggingface/status) ------
  useEffect(() => {
    Object.entries(transfers || {}).forEach(([repoId, t]) => {
      if (!t.result) return;
      dispatch(removeTransfer(repoId));
      if (t.via === 'keep_both') return; // the job says it
      if (cancelledRef.current.has(repoId)) { cancelledRef.current.delete(repoId); return; }
      const card = getLib().local[t.id || repoId];
      const name = card ? (card.display_name || card.name) : repoId.split('/')[1];
      if (t.result.status === 'Success') {
        toastOk(PLAIN_UPLOAD_SUCCESS.test(t.result.message) || !t.result.message
          ? fill(COPY.toast.uploadDone, { name })
          : t.result.message);
      } else {
        toastError(t.result.message || COPY.http.generic);
      }
      refetch([t.id || repoId]);
    });
  }, [transfers, dispatch, refetch, getLib]);

  // ---- the open dataset left the list (deleted elsewhere): back to the library
  const openCard = openId ? cards.find((c) => c.id === openId) || null : null;

  // ---- the hub copy's numbers for the open dataset (V2-15) -------------------
  // The newer/changed banners and the newerWarn dialog name the hub copy's
  // episodes and date, as the mockup does. The library lists a LOCAL dataset's
  // hub entry without numbers, so the page asks `hubstate` once per dataset,
  // hub head and local version; until it answers (or when it cannot) the
  // sentences go without numbers.
  const fetchHubState = session.fetchHubState;
  const openSync = openCard && openCard.sync ? openCard.sync.state : null;
  const factsKey = view === 'player' && openCard && openCard.local && (openSync === 'newer' || openSync === 'changed')
    ? `${openCard.id}|${openCard.sync.head || ''}|${openCard.local.meta_digest || ''}` : null;
  const [hubFacts, setHubFacts] = useState({ key: null, hub: null });
  useEffect(() => {
    if (!factsKey || !openId) return undefined;
    let cancelled = false;
    fetchHubState(openId).then((data) => {
      const h = data && data.hub && data.hub.exists ? data.hub : null;
      if (!cancelled) {
        setHubFacts({ key: factsKey, hub: h ? { total_episodes: h.total_episodes, last_modified: h.last_modified } : null });
      }
    }).catch(() => {
      if (!cancelled) setHubFacts({ key: factsKey, hub: null });
    });
    return () => { cancelled = true; };
  }, [factsKey, openId, fetchHubState]);
  const openHubFacts = useStableValue(hubFacts.key && hubFacts.key === factsKey ? hubFacts.hub : null);
  useEffect(() => {
    if (view === 'player' && session.lib.loaded && openId && !(openCard && openCard.local)) dispatch(showLibrary());
  }, [view, openId, openCard, session.lib.loaded, dispatch]);

  // ---- actions -----------------------------------------------------------------
  const guardEdit = useCallback((card, then) => {
    if (card.sync && card.sync.state === 'newer' && !newerAck[card.id]) {
      setDialog({ type: 'newer', card, then });
      return;
    }
    then();
  }, [newerAck]);

  const cardAction = useCallback((action, card) => {
    const m = models[card.id];
    switch (action) {
      case 'view': dispatch(openPlayer(card.id)); break;
      case 'upload':
      case 'upload_here':
        if (card.ns !== own) { toastWarn(fill(COPY.toast.partnerUpload, { name: (m && m.ownerName) || card.ns })); break; }
        setDialog({ type: 'upload', card });
        break;
      case 'upload_partner':
        toastWarn(fill(COPY.toast.partnerUpload, { name: (m && m.ownerName) || card.ns }));
        break;
      case 'pull': setDialog({ type: 'pull', card }); break;
      case 'load_online': setDialog({
        type: 'pull', card, crashed: !!(m && m.kind === 'crashed'), online: true,
      }); break;
      case 'keep_both': runKeepBoth(card, card.sync && card.sync.head ? card.sync.head : (card.hub && card.hub.head)); break;
      case 'load_view':
        runDownload(card, { mode: 'new', revision: (card.sync && card.sync.head) || (card.hub && card.hub.head), open: true });
        break;
      case 'delete_whole':
      case 'delete_dataset': setDialog({ type: 'delete_dataset', card }); break;
      case 'training': toTraining(card); break;
      case 'merge_with': startMerge(card.id); break;
      case 'cancel_upload':
        command('cancel', { what: 'upload' }).then((r) => {
          if (r.ok) {
            if (transferRepo) cancelledRef.current.add(transferRepo);
            toastWarn(COPY.toast.uploadCancelled);
          } else toastError(r.message || COPY.http.generic);
        });
        break;
      case 'cancel_download': {
        const j = jobFor(payload, card.id);
        command('cancel', { what: 'download' }).then((r) => {
          if (r.ok) {
            if (j) cancelledRef.current.add(`download:${j.job_id}`);
            toastWarn(COPY.toast.downloadCancelled);
          } else toastError(r.message || COPY.http.generic);
        });
        break;
      }
      default: break;
    }
  }, [models, dispatch, own, runKeepBoth, runDownload, toTraining, startMerge, command, transferRepo, payload]);

  const menuAction = useCallback((item, card) => {
    if (item === 'training') toTraining(card);
    else if (item === 'merge_pick') startMerge(card.id);
    else if (item === 'delete') setDialog({ type: 'delete_dataset', card });
  }, [toTraining, startMerge]);

  const onMergePick = useCallback((id) => {
    setMerge((m) => ({ active: true, ids: m.ids.includes(id) ? m.ids.filter((x) => x !== id) : [...m.ids, id] }));
  }, []);
  const onToggleMerge = useCallback(() => setMerge((m) => (m.active ? { active: false, ids: [] } : { active: true, ids: [] })), []);

  const onMergeGo = useCallback(async ({ name, target }) => {
    const sel = merge.ids.map((id) => session.lib.local[id]).filter(Boolean);
    const eps = sel.reduce((a, e) => a + (Number(e.total_episodes) || 0), 0);
    await runEdit({ id: target }, {
      op: 'merge',
      datasets: sel.map((e) => ({ id: e.id, meta_digest: e.meta_digest })),
      new_name: name,
      owner_ns: own,
    }, {
      title: COPY.progress.mergeTitle,
      sub: fill(COPY.progress.mergeSub, {
        datasets: plural(sel.length, COPY.count.datasetOne, COPY.count.datasetMany),
        episodes: plural(eps, COPY.count.episodeOne, COPY.count.episodeMany),
      }),
      steps: COPY.progress.mergeSteps,
      onDone: (outputs) => {
        setMerge({ active: false, ids: [] });
        const out = outputs[0] || target;
        toastOk(
          fill(COPY.toast.merged, { name, count: plural(eps, COPY.count.episodeOne, COPY.count.episodeMany) }),
          { label: COPY.toast.mergedAction, onClick: () => dispatch(openPlayer(out)) },
        );
      },
    });
  }, [merge.ids, session.lib.local, runEdit, own, dispatch]);

  const openTool = useCallback((kind, data) => setDialog({ type: 'tool', kind, ...data }), []);
  const confirmDeleteMarked = useCallback((data) => setDialog({ type: 'delete_marked', ...data }), []);

  const actionsRef = useRef({});
  actionsRef.current = {
    back: () => dispatch(showLibrary()), cardAction, guardEdit, openTool, confirmDeleteMarked, startMerge,
  };
  // One stable object whose functions call the latest handlers, so the player
  // (memoised) never re-renders because a handler was re-created.
  const actions = useMemo(() => ({
    back: () => actionsRef.current.back(),
    cardAction: (a, c) => actionsRef.current.cardAction(a, c),
    guardEdit: (c, t) => actionsRef.current.guardEdit(c, t),
    openTool: (k, d) => actionsRef.current.openTool(k, d),
    confirmDeleteMarked: (d) => actionsRef.current.confirmDeleteMarked(d),
    startMerge: (id) => actionsRef.current.startMerge(id),
  }), []);
  const peekRef = useRef(session.peekDsToken);
  peekRef.current = session.peekDsToken;
  const api = useMemo(() => ({
    fetchSummary: session.fetchSummary,
    fetchEpisodeData: session.fetchEpisodeData,
    dsToken: session.dsToken,
    peekDsToken: (id) => peekRef.current(id),
  }), [session.fetchSummary, session.fetchEpisodeData, session.dsToken]);

  const runDeleteEpisodes = useCallback((card, summary, indices) => {
    runEdit(card, {
      op: 'delete', dataset: card.id, meta_digest: summary.meta_digest, episodes: indices,
    }, {
      title: COPY.progress.deleteTitle,
      sub: COPY.progress.wait,
      steps: COPY.progress.deleteSteps,
      onDone: (_outputs, landed) => {
        // The state AFTER the edit (V2-5): the robot's re-read when it landed,
        // else what a local edit always makes of the state before it.
        const lib = getLib();
        const e = lib.local[card.id] || null;
        const reRead = landed ? lib.sync[card.id] : null;
        const sync = reRead || { ...(card.sync || {}), state: syncAfterLocalEdit(card.sync && card.sync.state), reason: null };
        const count = plural(indices.length, COPY.count.episodeOne, COPY.count.episodeMany);
        if (sync.state === 'changed' && card.ns === own) {
          toastOk(fill(COPY.toast.episodesDeletedUpload, { count }), {
            label: COPY.toast.uploadAction,
            onClick: () => setDialog({ type: 'upload', card: { ...card, local: e || card.local, sync } }),
          });
        } else {
          toastOk(fill(COPY.toast.episodesDeleted, { count }));
        }
      },
    });
  }, [runEdit, own, getLib]);

  const runSplit = useCallback((card, summary, { indices, name, target }) => {
    runEdit(card, {
      op: 'split', dataset: card.id, meta_digest: summary.meta_digest, episodes: indices, new_name: name, owner_ns: own,
    }, {
      title: COPY.progress.splitTitle,
      sub: COPY.progress.wait,
      steps: COPY.progress.splitSteps,
      onDone: (outputs, landed) => {
        const newId = outputs.find((o) => o !== card.id) || target;
        const orig = landed ? getLib().local[card.id] : null;
        toastOk(fill(COPY.toast.split, {
          a: nameOf(card),
          na: plural(orig ? orig.total_episodes : (summary.episodes.length - indices.length), COPY.count.episodeOne, COPY.count.episodeMany),
          b: name,
          nb: plural(indices.length, COPY.count.episodeOne, COPY.count.episodeMany),
        }), { label: COPY.toast.splitAction, onClick: () => dispatch(openPlayer(newId)) });
      },
    });
  }, [runEdit, own, dispatch, getLib]);

  // ---- the open player's props, stable by content ---------------------------
  const openModel = useStableValue(openCard ? models[openCard.id] : null);
  const stableCard = useStableValue(openCard);

  const keysBlocked = !!dialog || menusOpen > 0 || !!jobs.dialog;
  const onMenuOpenChange = useCallback((open) => setMenusOpen((n) => Math.max(0, n + (open ? 1 : -1))), []);

  // ---- states (§G10) -----------------------------------------------------------
  let body;
  if (cloud) body = <StateBox icon="cloud" text={COPY.cloudOnly} />;
  else if (!authLoading && !isAuthenticated) body = <StateBox icon="users" text={COPY.lib.noIdentity} />;
  else if (isAuthenticated && profileLoaded && !hfUsername) body = <StateBox icon="key" text={COPY.lib.noHfAccount} />;
  else if (!identity) body = <StateBox icon="loading" text={COPY.lib.loading} />;
  else if (!connected) body = <StateBox icon="unplugged" text={COPY.lib.offline} />;
  else if (session.status === 'old_image') body = <StateBox icon="warning" text={COPY.old.image} />;
  else if (view === 'player' && stableCard && stableCard.local && openModel) {
    body = (
      <>
        {session.status === 'sidecar_down' ? (
          <div className="dat-main" style={{ paddingBottom: 0 }}>
            <div className="dat-banner dat-warn" role="status">
              <Icon name="loading" size={16} className="animate-spin" />
              <span className="dat-grow">{COPY.old.sidecar}</span>
            </div>
          </div>
        ) : null}
        <PlayerView
          key={stableCard.id}
          card={stableCard}
          hubFacts={openHubFacts}
          model={openModel}
          api={api}
          connected={connected}
          hfOff={!inSync}
          keysBlocked={keysBlocked}
          newerAcked={!!newerAck[stableCard.id]}
          actions={actions}
        />
      </>
    );
  } else {
    body = (
      <LibraryView
        cards={cards}
        models={models}
        counts={counts}
        filter={filter}
        onFilter={(f) => dispatch(setLibraryFilter(f))}
        query={query}
        onQuery={setQuery}
        diskFree={diskFree}
        hub={session.lib.hub}
        hubAt={session.lib.hubAt}
        hubLoading={session.hubLoading}
        inSync={inSync}
        onRefresh={session.refresh}
        onFetch={() => setDialog({ type: 'fetch' })}
        merge={merge}
        onToggleMerge={onToggleMerge}
        onMergePick={onMergePick}
        mergeEntries={merge.ids.map((id) => session.lib.local[id]).filter(Boolean)}
        mergeExisting={localIds}
        onMergeGo={onMergeGo}
        onAction={cardAction}
        onMenu={menuAction}
        onMenuOpenChange={onMenuOpenChange}
        peekToken={session.peekDsToken}
        getToken={session.dsToken}
        own={own}
        ownerNames={group.names}
        robotType={robotType}
        sidecarDown={session.status === 'sidecar_down'}
        loaded={session.lib.loaded}
      />
    );
  }

  // ---- dialogs -------------------------------------------------------------------
  const close = () => setDialog(null);
  let dlg = null;
  if (dialog && !jobs.dialog) {
    const c = dialog.card;
    const m = c ? models[c.id] : null;
    switch (dialog.type) {
      case 'upload':
        dlg = (
          <UploadCompareDialog
            card={c}
            fetchHubState={session.fetchHubState}
            onClose={close}
            onUpload={(args) => { close(); runUpload(c, args); }}
            onKeepBoth={(head) => { close(); runKeepBoth(c, head); }}
            onPull={(head) => { close(); runDownload(c, { mode: 'replace', revision: head }); }}
          />
        );
        break;
      case 'pull':
        dlg = (
          <PullNewerDialog
            card={c}
            crashed={!!dialog.crashed}
            online={!!dialog.online}
            partnerNote={c.ns !== own ? fill(COPY.card.partnerNote, { name: (m && m.ownerName) || c.ns }) : null}
            fetchHubState={session.fetchHubState}
            onClose={close}
            onPull={(head) => { close(); runDownload(c, { mode: 'replace', revision: head, open: false }); }}
            onKeepBoth={(head) => { close(); runKeepBoth(c, head); }}
            onUpload={(head, priv) => { close(); runUpload(c, { expected_hub_sha: head, private: !!priv }); }}
          />
        );
        break;
      case 'newer':
        dlg = (
          <NewerWarnDialog
            card={c}
            hubFacts={c.id === openId ? openHubFacts : null}
            onClose={close}
            onPull={() => setDialog({ type: 'pull', card: c })}
            onAnyway={() => {
              setNewerAck((a) => ({ ...a, [c.id]: true }));
              close();
              dialog.then();
            }}
          />
        );
        break;
      case 'delete_dataset':
        dlg = (
          <ConfirmDeleteDataset
            card={c}
            own={own}
            ownerName={m ? m.ownerName : null}
            onClose={close}
            onConfirm={() => { close(); runDeleteDataset(c); }}
          />
        );
        break;
      case 'delete_marked': {
        const s = dialog.summary;
        dlg = (
          <ConfirmDeleteMarked
            name={nameOf(c)}
            indices={dialog.marks}
            total={(s.episodes || []).length}
            ownerName={m ? m.ownerName : null}
            onlineCopy={!!c.hub && c.sync && c.sync.state !== 'local'}
            onClose={close}
            onConfirm={() => { close(); runDeleteEpisodes(c, s, dialog.marks); }}
          />
        );
        break;
      }
      case 'tool': {
        const s = dialog.summary;
        dlg = (
          <EpisodeToolDialog
            kind={dialog.kind}
            name={nameOf(c)}
            baseName={taskNameOf(c.local || { id: c.id, name: c.name }, robotType)}
            total={(s.episodes || []).length}
            marks={dialog.marks || EMPTY_LIST}
            hintIndices={dialog.hintIndices || EMPTY_LIST}
            ownerName={m ? m.ownerName : null}
            onlineCopy={!!c.hub && c.sync && c.sync.state !== 'local'}
            own={own}
            robotType={robotType}
            localIds={localIds}
            onClose={close}
            onConfirm={(r) => {
              close();
              if (dialog.kind === 'split') runSplit(c, s, r);
              else runDeleteEpisodes(c, s, r.indices);
            }}
          />
        );
        break;
      }
      case 'fetch':
        dlg = (
          <FetchDialog
            probeRepo={session.probeRepo}
            own={own}
            robotType={robotType}
            localIds={localIds}
            hubIds={hubIds}
            diskFree={diskFree}
            onClose={close}
            onFetch={({ repo, revision, target, displayName }) => {
              close();
              if (filter === 'group') dispatch(setLibraryFilter('all'));
              runDownload({ id: target, name: target.split('/')[1], ns: own, local: null }, {
                mode: 'copy', revision, target, displayName, repo,
              });
            }}
          />
        );
        break;
      default:
        dlg = null;
    }
  }
  const prog = jobs.dialog;

  return (
    <div className="dat-page" style={{ '--leader': LEADER_COLOR }}>
      {body}
      {dlg}
      {prog ? (
        <ProgressDialog
          title={prog.meta.title}
          sub={prog.meta.sub || ''}
          steps={prog.meta.steps || []}
          step={prog.row ? stepOfStage(prog.meta.op, prog.row.stage) : 0}
        />
      ) : null}
    </div>
  );
}
