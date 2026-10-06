// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// What one library card shows and offers (the approved mockup's cards, spec
// §C3, §D6, §E8, §E10, §G10, §G14), pure: the card component only draws this.
//
// Kinds, in the order they win:
//   refreshing  a busy change is newer than the reply shown (T-1 a): no action
//   live        the robot records into it: „Wird gerade aufgenommen"
//   crashed     a session left it unfinished (H-1): its line, „Online-Version
//               laden" (only with a proven hub copy) and „Ganzen Datensatz löschen"
//   broken      incomplete / old format / unsupported: its line, delete only
//   online      only on Hugging Face: „Laden und ansehen"
//   ok          the seven sync states, with the busy overlays of daten_state
//
// Every Hugging Face action is disabled, with `copy.lib.tokenNotActive` as its
// title, while the robot does not hold this student's token. A partner's
// dataset offers no upload (H-3): „Hochladen" is aria-disabled and explains
// itself on a click, and its conflict card offers only „Online-Version laden".

import COPY from '../datenCopy';
import { fill, fmtBytes, fmtFps, fmtTime } from './format';
import { cardBusyKind, cardPhase } from './libraryState';
import { hubDatasetUrl } from './hubLinks';

const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);

/** The training button's reason to be off, or '' (spec §E8). */
export function trainingBlock(card) {
  const s = card.sync && card.sync.state;
  if (card.hub && card.hub.private === true) return COPY.train.private;
  if (s === 'changed' || s === 'conflict') return COPY.menu.trainChanged;
  // „Nur hier" only on the sidecar's proof; `unknown` keeps the button (R-4).
  if (s === 'local') return COPY.menu.trainLocal;
  return '';
}

/** „Auf Hugging Face ansehen" for a card whose dataset is proven on the hub (§G14), or null. */
export function hubLink(card) {
  if (!card.hub) return null;
  const href = hubDatasetUrl(card.id, card.hub.private);
  if (!href) return null;
  return {
    href,
    title: card.hub.private === false ? COPY.menu.hubLinkTitlePublic : COPY.menu.hubLinkTitlePrivate,
  };
}

function statsOf(card) {
  const src = card.local || card.hub || {};
  const eps = num(src.total_episodes);
  const dur = num(src.duration_s);
  const size = num(src.size_bytes);
  const fps = num(src.fps);
  return {
    episodes: eps === null ? '–' : String(eps),
    duration: dur === null ? '–' : fmtTime(dur, false),
    size: size === null ? '–' : fmtBytes(size),
    fps: fps === null ? '–' : fmtFps(fps),
  };
}

function hintOf(entry) {
  if (!entry) return { kind: 'online', text: COPY.card.hintsAfterLoad, icon: 'info' };
  const h = entry.hint_episodes;
  if (h === null || h === undefined) return { kind: 'pending', text: COPY.lib.hintsPending, icon: 'loading', spin: true };
  if (Number(h) > 0) {
    return { kind: 'warn', text: fill(COPY.card.hintsCount, { n: h, m: entry.total_episodes ?? '–' }), icon: 'warning' };
  }
  return { kind: 'ok', text: COPY.card.hintsNone, icon: 'check' };
}

/**
 * @param {object} card a libraryCards() row `{id, ns, name, local, hub, sync}`
 * @param {object} ctx
 * @param {string} ctx.own the student's account
 * @param {Object<string,string>} ctx.ownerNames partner namespace → display name
 * @param {string[]} ctx.busyKinds every daten_state busy kind of this id (useDatenState::busyKindsById)
 * @param {object|null} ctx.job a running job row touching this id
 * @param {number|null} ctx.uploadPct the HF worker's upload percentage when it uploads this id
 * @param {boolean} ctx.stateSeen a daten_state message has arrived
 * @param {object} ctx.lib the library state (stamps, entrySeq)
 * @param {boolean} ctx.inSync the robot holds this student's token
 * @param {boolean} ctx.mergeMode / ctx.mergeSelected
 */
export function cardModel(card, ctx) {
  const own = card.ns === ctx.own;
  const ownerName = own ? null : ((ctx.ownerNames && ctx.ownerNames[card.ns]) || card.ns);
  const local = card.local;
  const record = local && local.record ? local.record : null;
  const title = (local && local.display_name) || card.name;
  const busyKinds = Array.isArray(ctx.busyKinds) ? ctx.busyKinds : [];
  const busyKind = cardBusyKind(busyKinds);
  const job = ctx.job || null;
  const lib = ctx.lib || {};
  const phase = local ? cardPhase(local, {
    busyKinds,
    stateSeen: !!ctx.stateSeen,
    stamp: lib.stamps ? lib.stamps[card.id] : undefined,
    entrySeq: lib.entrySeq ? lib.entrySeq[card.id] : undefined,
  }) : (lib.stamps && lib.stamps[card.id] !== undefined && !((lib.entrySeq?.[card.id] ?? -Infinity) > lib.stamps[card.id]) ? 'refreshing' : null);
  const hfOff = !ctx.inSync;
  const hfTitle = hfOff ? COPY.lib.tokenNotActive : undefined;
  const sync = card.sync || { state: 'unknown', reason: 'not_asked', head: null };
  const hubHead = (card.hub && card.hub.head) || sync.head || null;
  const link = hubLink(card);

  const base = {
    id: card.id,
    title,
    repo: card.id,
    own,
    ownerName,
    source: record && record.source_repo ? record.source_repo : null,
    stats: statsOf(card),
    badge: { state: sync.state, reason: sync.reason, overlay: null, ownerName: ownerName || '' },
    thumb: local && local.state === 'ok' ? 'image' : (local ? 'none' : 'online'),
    hint: hintOf(local),
    progress: null,
    actions: [],
    actions2: [],
    note: null,
    menu: null,
    mergePick: null,
    kind: 'ok',
    hubHead,
    link,
  };

  const menuItems = ({ online = false, training = true, merge = true } = {}) => {
    const items = [];
    if (training) {
      const reason = trainingBlock(card);
      items.push({ id: 'training', icon: 'chart', label: COPY.menu.toTraining, disabled: !!reason, small: reason });
    }
    if (merge) {
      const pickable = local && local.state === 'ok';
      items.push({
        id: 'merge_pick', icon: 'mergeData', label: COPY.menu.mergePick,
        disabled: !pickable, small: online ? COPY.menu.mergeNeedsLoad : '',
      });
    }
    if (link) items.push({ id: 'hub_link', icon: 'externalLink', label: COPY.menu.hubLink, href: link.href, title: link.title });
    items.push({ sep: true });
    const size = local && num(local.size_bytes) !== null ? fmtBytes(local.size_bytes) : null;
    items.push({
      id: 'delete', icon: 'trash', label: COPY.menu.deleteWhole, danger: true, disabled: online,
      small: online ? COPY.menu.deleteOnlineOnly : (size ? fill(COPY.menu.deleteFrees, { size }) : ''),
    });
    return items;
  };

  const mergePick = (disabledTitle) => (ctx.mergeMode ? {
    selected: !!ctx.mergeSelected,
    disabled: !!disabledTitle,
    title: disabledTitle || '',
  } : null);

  // ---- refreshing (T-1 a): neutral, no action
  if (phase === 'refreshing') {
    return {
      ...base,
      kind: 'refreshing',
      badge: { ...base.badge, overlay: 'refreshing' },
      hint: { kind: 'state', text: COPY.card.refreshing, icon: 'loading', spin: true },
      thumb: local && local.state === 'ok' ? 'image' : base.thumb,
      mergePick: mergePick(COPY.card.refreshing),
    };
  }

  // ---- live recording (busy record): „Wird gerade aufgenommen"
  if (phase === 'live' || (local && busyKind === 'record')) {
    return {
      ...base,
      kind: 'live',
      badge: { ...base.badge, overlay: 'record' },
      thumb: base.thumb === 'image' ? 'image' : 'none',
      actions: [{ id: 'view', label: COPY.card.view, icon: 'play', variant: 'primary', disabled: true, title: COPY.card.viewAfterRecording }],
      inlineNote: COPY.card.editAfterRecording,
      mergePick: mergePick(COPY.card.viewAfterRecording),
    };
  }

  // ---- crashed (H-1)
  if (phase === 'crashed') {
    const actions = [];
    if (card.hub && sync.head) {
      actions.push({ id: 'load_online', label: COPY.card.loadOnline, icon: 'cloudDownload', disabled: hfOff, title: hfTitle });
    }
    actions.push({ id: 'delete_whole', label: COPY.card.deleteWhole, icon: 'trash', variant: 'danger' });
    return {
      ...base,
      kind: 'crashed',
      thumb: 'none',
      hint: { kind: 'bad', text: COPY.card.crashed, icon: 'failed' },
      actions,
      menu: link ? [{ id: 'hub_link', icon: 'externalLink', label: COPY.menu.hubLink, href: link.href, title: link.title }] : null,
      mergePick: mergePick(COPY.card.crashed),
    };
  }

  // ---- a local dataset this page cannot open
  if (local && local.state !== 'ok') {
    const text = COPY.card[local.state] || COPY.card.incomplete;
    return {
      ...base,
      kind: 'broken',
      thumb: 'none',
      hint: { kind: 'bad', text, icon: 'failed' },
      actions: [{ id: 'delete_whole', label: COPY.card.deleteWhole, icon: 'trash', variant: 'danger' }],
      menu: menuItems({ merge: false, training: !!card.hub }),
      mergePick: mergePick(text),
    };
  }

  // ---- busy overlays from daten_state (and the jobs) ------------------------
  const isDownloadJob = job && job.op === 'download';
  const isKeepBoth = job && job.op === 'keep_both';
  let overlay = null;
  let progress = null;
  if (busyKind === 'upload') {
    overlay = 'upload';
    const pct = Math.max(0, Math.min(100, Math.round(Number(ctx.uploadPct) || 0)));
    progress = { label: COPY.card.uploading, right: fill(COPY.card.percent, { pct }), pct, cancel: ctx.uploadPct === null ? null : 'upload' };
  } else if (isDownloadJob || busyKind === 'download') {
    overlay = 'download';
    if (isDownloadJob && num(job.total) && job.unit === 'bytes') {
      const pct = Math.max(0, Math.min(100, Math.round((Number(job.done) / Number(job.total)) * 100)));
      progress = {
        label: fill(COPY.card.downloading, { done: fmtBytes(job.done), total: fmtBytes(job.total) }),
        right: fill(COPY.card.percent, { pct }),
        pct,
        cancel: 'download',
      };
    } else {
      progress = { label: COPY.card.downloadingStart, right: '', pct: 0, cancel: isDownloadJob ? 'download' : null };
    }
  } else if (isKeepBoth || busyKind === 'edit' || busyKind === 'delete') {
    overlay = isKeepBoth ? 'keep_both' : (busyKind || 'edit');
    const total = job ? num(job.total) : null;
    const pct = total ? Math.max(0, Math.min(100, Math.round((Number(job.done) / total) * 100))) : 0;
    progress = { label: isKeepBoth ? COPY.card.keepingBoth : COPY.card.editing, right: total ? fill(COPY.card.percent, { pct }) : '', pct, cancel: null };
  }
  if (overlay) {
    return {
      ...base,
      kind: local ? 'busy' : 'online',
      badge: { ...base.badge, overlay },
      progress,
      mergePick: mergePick(COPY.card.editing),
    };
  }

  // ---- online only
  if (!local) {
    return {
      ...base,
      kind: 'online',
      actions: [{ id: 'load_view', label: COPY.card.loadAndView, icon: 'cloudDownload', variant: 'primary', disabled: hfOff, title: hfTitle }],
      inlineNote: base.stats.size,
      menu: menuItems({ online: true }),
      mergePick: mergePick(COPY.card.mergePickBlocked),
    };
  }

  // ---- an ordinary local dataset ---------------------------------------------
  const view = { id: 'view', label: COPY.card.view, icon: 'play', variant: 'primary' };
  const partnerUpload = (label) => ({
    id: 'upload_partner', label, icon: 'cloudUpload', ariaDisabled: true,
    title: fill(COPY.card.partnerOnly, { name: ownerName }),
  });
  const actions = [view];
  const actions2 = [];
  let note = null;
  switch (sync.state) {
    case 'changed':
      actions.push(own
        ? { id: 'upload', label: COPY.card.uploadNow, icon: 'cloudUpload', disabled: hfOff, title: hfTitle }
        : partnerUpload(COPY.card.uploadNow));
      break;
    case 'newer':
      actions.push({ id: 'pull', label: COPY.card.pullNewer, icon: 'cloudDownload', disabled: hfOff, title: hfTitle });
      break;
    case 'local':
    case 'unknown':
      actions.push(own
        ? { id: 'upload', label: COPY.card.upload, icon: 'cloudUpload', disabled: hfOff, title: hfTitle }
        : partnerUpload(COPY.card.upload));
      break;
    case 'conflict':
      if (own) {
        actions.push({ id: 'keep_both', label: COPY.card.keepBoth, icon: 'keepBoth', disabled: hfOff, title: hfTitle || COPY.keepBoth.tip });
        actions2.push({ id: 'load_online', label: COPY.card.loadOnline, icon: 'cloudDownload', disabled: hfOff, title: hfTitle });
        actions2.push({ id: 'upload_here', label: COPY.card.uploadHere, icon: 'cloudUpload', disabled: hfOff, title: hfTitle });
      } else {
        actions.push({ id: 'load_online', label: COPY.card.loadOnline, icon: 'cloudDownload', disabled: hfOff, title: hfTitle });
        note = fill(COPY.card.partnerNote, { name: ownerName });
      }
      break;
    default:
      break;
  }
  return {
    ...base,
    kind: 'ok',
    actions,
    actions2,
    note,
    menu: menuItems(),
    mergePick: mergePick(''),
  };
}
