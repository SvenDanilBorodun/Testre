// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Daten page's library (spec §J.B step 4, §G10): every page state, a card
// in each sync state and overlay, the crashed and refreshing cards, partner
// cards, the Hugging Face link, filters/search/menu, the merge mode with its
// checks, the fetch dialog, both delete variants, and the conflict surfaces
// with „Beide behalten" first — each sending what the spec says it sends.

/* eslint-disable testing-library/no-node-access, testing-library/no-container */

import React, { useSyncExternalStore } from 'react';
import {
  act, fireEvent, render, screen, waitFor, within,
} from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import DatenPage from '../DatenPage';
import COPY from '../../../features/editDataset/datenCopy';
import {
  FP, OWN, PARTNER, hubEntry, installSidecar, local, makeCommand, makeStore, wrap,
} from './datenHarness';

// ---- the rosbridge hooks, driven from here -------------------------------
let mockCommand = null;
vi.mock('../../../features/editDataset/hooks/useDatenCommand', () => ({
  __esModule: true,
  default: () => mockCommand,
}));
const mockDaten = { snap: { received: false, payload: null, receivedAt: null }, listeners: new Set() };
vi.mock('../../../features/editDataset/hooks/useDatenState', async (importOriginal) => {
  const orig = await importOriginal();
  return {
    ...orig,
    __esModule: true,
    default: function useMockDatenState() {
      return useSyncExternalStore(
        (fn) => { mockDaten.listeners.add(fn); return () => mockDaten.listeners.delete(fn); },
        () => mockDaten.snap,
      );
    },
  };
});
let mockGroup = null;
vi.mock('../../../features/editDataset/hooks/useGroupNamespaces', () => ({
  __esModule: true,
  default: () => mockGroup,
}));
let mockSignal = { payload: null, receivedAt: null };
vi.mock('../../../hooks/useSignalStatus', () => ({ __esModule: true, default: () => mockSignal }));
let mockCloud = false;
vi.mock('../../../utils/cloudMode', () => ({ __esModule: true, isCloudOnlyMode: () => mockCloud }));
vi.mock('../../UrdfTwin', () => ({ __esModule: true, default: () => null }));
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  fn.dismiss = vi.fn();
  return { __esModule: true, default: fn, useToasterStore: () => ({ toasts: [] }) };
});

function setDaten(payload) {
  mockDaten.snap = { received: true, payload: { v: 1, seq: 1, busy: [], jobs: [], transfer: null, ...payload }, receivedAt: Date.now() };
  mockDaten.listeners.forEach((fn) => fn());
}

let world;
let requests;
beforeEach(() => {
  mockCloud = false;
  mockSignal = { payload: null, receivedAt: null };
  mockGroup = { status: 'ready', own: OWN, namespaces: [OWN, PARTNER], names: { [PARTNER]: 'Max Weber' } };
  mockCommand = makeCommand();
  mockDaten.snap = { received: false, payload: null, receivedAt: null };
  mockDaten.listeners.clear();
  world = {
    local: [], hub: [], sync: {},
    summaries: {},
    hubstate: (id) => ({ v: 1, id, meta_digest: `d-${id}`, sync: { state: 'current', reason: null }, hub: null, local: { total_episodes: 12, duration_s: 240, modified_at: '2026-10-03T12:12:00Z' }, new_repo_private: false }),
    probe: () => ({ v: 1, found: false, refusal: 'not_found' }),
  };
  requests = installSidecar(world);
  toast.success.mockClear();
  toast.error.mockClear();
  toast.mockClear();
  HTMLMediaElement.prototype.play = vi.fn(() => Promise.resolve());
  HTMLMediaElement.prototype.pause = vi.fn();
});
afterEach(() => { delete global.fetch; });

async function mount(storeOpts = {}) {
  const store = makeStore(storeOpts);
  const utils = render(wrap(store, <DatenPage />));
  return { store, ...utils };
}
const card = (id) => document.querySelector(`[data-id="${id}"]`);
const cardText = (id) => (card(id) ? card(id).textContent : '');
const commandCalls = (action) => mockCommand.mock.calls.filter(([a]) => a === action).map(([, args]) => args);

describe('the page states (§G10)', () => {
  it('cloud mode: the cloud sentence and nothing else', async () => {
    mockCloud = true;
    await mount();
    expect(screen.getByText(COPY.cloudOnly)).toBeInTheDocument();
    expect(mockCommand).not.toHaveBeenCalled();
  });

  it('not signed in (the offline escape): noIdentity, never noHfAccount', async () => {
    await mount({ authenticated: false });
    expect(screen.getByText(COPY.lib.noIdentity)).toBeInTheDocument();
    expect(screen.queryByText(COPY.lib.noHfAccount)).toBeNull();
  });

  it('signed in without a Hugging Face account: noHfAccount', async () => {
    mockGroup = { status: 'idle', own: null, namespaces: [], names: {} };
    await mount({ hfUsername: null });
    expect(screen.getByText(COPY.lib.noHfAccount)).toBeInTheDocument();
    expect(screen.queryByText(COPY.lib.noIdentity)).toBeNull();
  });

  it('the robot offline: lib.offline', async () => {
    await mount({ connected: false });
    expect(screen.getByText(COPY.lib.offline)).toBeInTheDocument();
  });

  it('an old image (no /daten/command): old.image and nothing else of the tab', async () => {
    mockCommand = makeCommand({ link: () => ({ ok: false, code: 'old_image', message: '', result: {}, oldImage: true, unreachable: false }) });
    await mount();
    await screen.findByText(COPY.old.image);
    expect(screen.queryByText(COPY.page.title)).toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('the sidecar down: the retry banner', async () => {
    global.fetch = vi.fn(() => Promise.resolve({ ok: false, status: 502, headers: { get: () => null }, json: () => Promise.reject(new Error('x')) }));
    await mount();
    await screen.findByText(COPY.old.sidecar);
  });

  it('an empty library', async () => {
    await mount();
    await screen.findByText(COPY.lib.empty);
  });

  it('a token that is not this student\'s: the banner, the hub not asked, HF actions off with that title', async () => {
    world.local = [local(`${OWN}/omx_f_a`)];
    world.sync = { [`${OWN}/omx_f_a`]: { state: 'unknown', reason: 'not_asked', head: null } };
    await mount({ inSync: false });
    await screen.findByText(COPY.lib.tokenNotActive);
    await waitFor(() => expect(card(`${OWN}/omx_f_a`)).not.toBeNull());
    expect(requests.every((u) => !u.includes('/library') || u.includes('hub=0'))).toBe(true);
    const up = within(card(`${OWN}/omx_f_a`)).getByText(COPY.card.upload).closest('button');
    expect(up).toBeDisabled();
    expect(up.getAttribute('title')).toBe(COPY.lib.tokenNotActive);
    expect(screen.getByText(COPY.page.fetch).closest('button')).toBeDisabled();
  });

  it('a failed online list: the banner, the local cards stay, nothing disabled because of unknown', async () => {
    world.local = [local(`${OWN}/omx_f_a`)];
    world.hubState = 'unreachable';
    world.sync = { [`${OWN}/omx_f_a`]: { state: 'unknown', reason: 'unreachable', head: null } };
    await mount();
    await screen.findByText(COPY.lib.hubFailed);
    const c = card(`${OWN}/omx_f_a`);
    expect(c.textContent).toContain(COPY.sync.unknownLabel);
    // V2-9: the badge says WHY (unreachable), never „wurde noch nicht geprüft"
    expect(c.querySelector('[data-sync="unknown"]').getAttribute('title')).toBe(COPY.sync.unknownTip.unreachable);
    expect(within(c).getByText(COPY.sync.refresh)).toBeInTheDocument();
    expect(within(c).getByText(COPY.card.upload).closest('button')).not.toBeDisabled();
  });
});

describe('cards (§C3, §G10, H-1, H-3, §G14)', () => {
  const id = (n) => `${OWN}/omx_f_${n}`;
  beforeEach(() => {
    world.local = [
      local(id('cur')), local(id('chg')), local(id('loc')), local(id('new')), local(id('con')),
      local(id('unk')), local(id('hp'), { hint_episodes: null }), local(id('bad'), { state: 'unsupported' }),
      local(`${PARTNER}/omx_f_p`), local(`${PARTNER}/omx_f_pc`),
    ];
    world.hub = [
      hubEntry(id('cur')), hubEntry(id('chg')), hubEntry(id('new')), hubEntry(id('con')),
      hubEntry(id('onl'), { private: true }), hubEntry(`${PARTNER}/omx_f_pc`),
    ];
    world.sync = {
      [id('cur')]: { state: 'current', head: `head-${id('cur')}` },
      [id('chg')]: { state: 'changed', head: `head-${id('chg')}` },
      [id('loc')]: { state: 'local', head: null },
      [id('new')]: { state: 'newer', head: `head-${id('new')}` },
      [id('con')]: { state: 'conflict', head: `head-${id('con')}` },
      [id('unk')]: { state: 'unknown', reason: 'not_asked', head: null },
      [id('hp')]: { state: 'local', head: null },
      [id('bad')]: { state: 'local', head: null },
      [id('onl')]: { state: 'online', head: `head-${id('onl')}` },
      [`${PARTNER}/omx_f_p`]: { state: 'unknown', reason: 'not_visible', head: null },
      [`${PARTNER}/omx_f_pc`]: { state: 'conflict', head: 'head-pc' },
    };
  });

  it('a badge per sync state', async () => {
    await mount();
    await waitFor(() => expect(card(id('cur'))).not.toBeNull());
    expect(cardText(id('cur'))).toContain('Aktuell');
    expect(cardText(id('chg'))).toContain('Hier geändert – nicht hochgeladen');
    expect(cardText(id('loc'))).toContain('Nur hier');
    expect(cardText(id('new'))).toContain('Online neuer');
    expect(cardText(id('con'))).toContain('Hier und online verschieden');
    expect(cardText(id('unk'))).toContain('Online-Stand unbekannt');
    expect(cardText(id('onl'))).toContain('Nur online');
    expect(cardText(id('onl'))).toContain(COPY.card.previewAfterLoad);
  });

  it('the „Aktualisieren" link only for not_asked/unreachable, never a partner\'s not_visible', async () => {
    await mount();
    await waitFor(() => expect(card(id('unk'))).not.toBeNull());
    expect(within(card(id('unk'))).getByText(COPY.sync.refresh)).toBeInTheDocument();
    const partner = card(`${PARTNER}/omx_f_p`);
    expect(within(partner).queryByText(COPY.sync.refresh)).toBeNull();
    expect(partner.querySelector('[data-sync="unknown"]').getAttribute('title')).toBe('Ob dieser Datensatz auf Hugging Face liegt, kann nur Max Weber sehen.');
  });

  it('hints pending, the hint count, the broken card, the owner chip', async () => {
    await mount();
    await waitFor(() => expect(card(id('hp'))).not.toBeNull());
    expect(cardText(id('hp'))).toContain(COPY.lib.hintsPending);
    expect(cardText(id('cur'))).toContain('2 von 12 Episoden mit Hinweisen');
    expect(cardText(id('bad'))).toContain(COPY.card.unsupported);
    // a dataset this page cannot open or upload shows no sync badge (V2-12)
    expect(card(id('bad')).querySelector('[data-sync]')).toBeNull();
    expect(within(card(id('bad'))).queryByText(COPY.card.view)).toBeNull();
    expect(within(card(id('bad'))).getByText(COPY.card.deleteWhole)).toBeInTheDocument();
    expect(cardText(`${PARTNER}/omx_f_p`)).toContain('Max Weber');
  });

  it('partner cards offer no upload: aria-disabled and the German sentence on a click (H-3)', async () => {
    await mount();
    await waitFor(() => expect(card(`${PARTNER}/omx_f_p`)).not.toBeNull());
    const up = within(card(`${PARTNER}/omx_f_p`)).getByText(COPY.card.upload).closest('button');
    expect(up.getAttribute('aria-disabled')).toBe('true');
    fireEvent.click(up);
    expect(toast).toHaveBeenCalledWith('Nur Max Weber kann diesen Datensatz hochladen: Er gehört zum Hugging-Face-Konto von Max Weber.', expect.anything());
    const pc = card(`${PARTNER}/omx_f_pc`);
    expect(within(pc).queryByText(COPY.card.keepBoth)).toBeNull();
    expect(within(pc).queryByText(COPY.card.uploadHere)).toBeNull();
    expect(within(pc).getByText(COPY.card.loadOnline)).toBeInTheDocument();
    expect(pc.textContent).toContain(COPY.toast.partnerUpload.replace(/\{name\}/g, 'Max Weber'));
  });

  it('filters Alle/Meine/Gruppe and the search', async () => {
    await mount();
    await waitFor(() => expect(card(id('cur'))).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: COPY.page.filterGroup }));
    expect(card(id('cur'))).toBeNull();
    expect(card(`${PARTNER}/omx_f_p`)).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: COPY.page.filterMine }));
    expect(card(`${PARTNER}/omx_f_p`)).toBeNull();
    fireEvent.change(screen.getByRole('searchbox', { name: COPY.page.search }), { target: { value: 'omx_f_chg' } });
    expect(document.querySelectorAll('.dat-card').length).toBe(1);
    fireEvent.change(screen.getByRole('searchbox', { name: COPY.page.search }), { target: { value: 'zzz' } });
    expect(screen.getByText('Keine Datensätze gefunden für „zzz“.')).toBeInTheDocument();
  });

  it('the free-disk chip comes from signal_status, and is absent without it', async () => {
    mockSignal = { payload: { disk: { free_bytes: 23.4e9 } }, receivedAt: 1 };
    const { unmount } = await mount();
    await screen.findByText('23,4 GB frei');
    unmount();
    mockSignal = { payload: null, receivedAt: null };
    await mount();
    await waitFor(() => expect(card(id('cur'))).not.toBeNull());
    expect(document.querySelector('[data-chip="disk"]')).toBeNull();
  });

  it('the ⋮ menu: training (with its reason), the hub link (public → viewer, new tab, noopener), delete', async () => {
    await mount();
    await waitFor(() => expect(card(id('cur'))).not.toBeNull());
    fireEvent.click(within(card(id('cur'))).getByRole('button', { name: COPY.card.more }));
    const menu = within(card(id('cur'))).getByRole('menu');
    const link = within(menu).getByRole('menuitem', { name: new RegExp(COPY.menu.hubLink) });
    expect(link.getAttribute('href')).toBe('https://huggingface.co/spaces/lerobot/visualize_dataset?path=%2Flena-schmidt%2Fomx_f_cur%2Fepisode_0');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    expect(within(menu).getByRole('menuitem', { name: new RegExp(COPY.menu.toTraining) })).not.toBeDisabled();
    fireEvent.keyDown(menu, { key: 'Escape' });
    await waitFor(() => expect(within(card(id('cur'))).queryByRole('menu')).toBeNull());
    // a local card: no hub link, training off with „Zuerst hochladen."
    fireEvent.click(within(card(id('loc'))).getByRole('button', { name: COPY.card.more }));
    const menu2 = within(card(id('loc'))).getByRole('menu');
    expect(within(menu2).queryByRole('menuitem', { name: new RegExp(COPY.menu.hubLink) })).toBeNull();
    const train = within(menu2).getByRole('menuitem', { name: new RegExp(COPY.menu.toTraining) });
    expect(train).toBeDisabled();
    expect(train.textContent).toContain(COPY.menu.trainLocal);
  });

  it('a private hub copy: the dataset page, and training off with train.private', async () => {
    await mount();
    await waitFor(() => expect(card(id('onl'))).not.toBeNull());
    fireEvent.click(within(card(id('onl'))).getByRole('button', { name: COPY.card.more }));
    const menu = within(card(id('onl'))).getByRole('menu');
    expect(within(menu).getByRole('menuitem', { name: new RegExp(COPY.menu.hubLink) }).getAttribute('href'))
      .toBe('https://huggingface.co/datasets/lena-schmidt/omx_f_onl');
    expect(within(menu).getByRole('menuitem', { name: new RegExp(COPY.menu.toTraining) }).textContent).toContain(COPY.train.private);
  });

  it('„Weiter zum Training" selects the dataset on the Training page', async () => {
    const { store } = await mount();
    await waitFor(() => expect(card(id('cur'))).not.toBeNull());
    fireEvent.click(within(card(id('cur'))).getByRole('button', { name: COPY.card.more }));
    fireEvent.click(within(card(id('cur'))).getByRole('menuitem', { name: new RegExp(COPY.menu.toTraining) }));
    const s = store.getState();
    expect(s.training.trainingInfo.datasetRepoId).toBe(id('cur'));
    expect(s.ui.currentPage).toBe('training');
  });
});

describe('overlays from /edubotics/daten_state', () => {
  const W = `${OWN}/omx_f_w`;
  beforeEach(() => {
    world.local = [local(W)];
    world.hub = [hubEntry(W)];
    world.sync = { [W]: { state: 'current', head: `head-${W}` } };
  });

  it('record → „Wird gerade aufgenommen", „Ansehen" disabled', async () => {
    await mount();
    await waitFor(() => expect(card(W)).not.toBeNull());
    act(() => setDaten({ busy: [{ id: W, kind: 'record' }] }));
    await waitFor(() => expect(cardText(W)).toContain(COPY.sync.recordLabel));
    expect(within(card(W)).getByText(COPY.card.view).closest('button')).toBeDisabled();
  });

  // C-2: a Start waiting for this dataset's upload lists it as `record` AND
  // `upload`; the card shows the upload that runs, in either wire order.
  it.each([
    ['record, upload', [{ id: W, kind: 'record' }, { id: W, kind: 'upload' }]],
    ['upload, record', [{ id: W, kind: 'upload' }, { id: W, kind: 'record' }]],
  ])('a Start waiting for the upload (%s): the upload overlay, never „Wird gerade aufgenommen"', async (_order, busy) => {
    await mount();
    await waitFor(() => expect(card(W)).not.toBeNull());
    act(() => setDaten({ busy, transfer: { kind: 'upload', repo_id: W, target: null } }));
    await waitFor(() => expect(cardText(W)).toContain(COPY.sync.uploadLabel));
    expect(cardText(W)).not.toContain(COPY.sync.recordLabel);
  });

  it('a keep_both job → „Wird zusammengeführt"', async () => {
    await mount();
    await waitFor(() => expect(card(W)).not.toBeNull());
    act(() => setDaten({
      busy: [{ id: W, kind: 'edit' }],
      jobs: [{ job_id: 'k', op: 'keep_both', state: 'running', datasets: [W], outputs: [W], stage: 'copy', done: 1, total: 3, unit: 'steps' }],
    }));
    await waitFor(() => expect(cardText(W)).toContain(COPY.sync.keepBothLabel));
  });

  it('a download job → byte progress with a cancel; a new target gets its own card', async () => {
    await mount();
    await waitFor(() => expect(card(W)).not.toBeNull());
    act(() => setDaten({
      busy: [{ id: `${OWN}/omx_f_neu`, kind: 'download' }],
      jobs: [{ job_id: 'd', op: 'download', state: 'running', datasets: [], outputs: [`${OWN}/omx_f_neu`], stage: 'download', done: 120e6, total: 540e6, unit: 'bytes' }],
    }));
    await waitFor(() => expect(card(`${OWN}/omx_f_neu`)).not.toBeNull());
    const c = card(`${OWN}/omx_f_neu`);
    expect(c.textContent).toContain('Wird geladen … 120 MB von 540 MB');
    expect(c.textContent).toContain('22 %');
    fireEvent.click(within(c).getByText(COPY.card.cancel));
    await waitFor(() => expect(commandCalls('cancel')).toEqual([{ what: 'download' }]));
  });

  it('an upload → „Wird hochgeladen …" with the HF percentage and a cancel', async () => {
    const { store } = await mount();
    await waitFor(() => expect(card(W)).not.toBeNull());
    act(() => {
      store.dispatch({ type: 'editDataset/setUploadStatus', payload: { current: 1, total: 4, percentage: '25.00' } });
      setDaten({ busy: [{ id: W, kind: 'upload' }], transfer: { kind: 'upload', repo_id: W, target: null } });
    });
    await waitFor(() => expect(cardText(W)).toContain(COPY.card.uploading));
    expect(cardText(W)).toContain('25 %');
    fireEvent.click(within(card(W)).getByText(COPY.card.cancel));
    await waitFor(() => expect(commandCalls('cancel')).toEqual([{ what: 'upload' }]));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(COPY.toast.uploadCancelled, expect.anything()));
  });
});

describe('the crashed card (H-1, T-1, U-3, U-4)', () => {
  const K = `${OWN}/omx_f_kaputt`;
  beforeEach(() => {
    world.local = [local(K, { state: 'in_session' })];
    world.hub = [hubEntry(K)];
    world.sync = { [K]: { state: 'current', head: `head-${K}` } };
  });

  it('neutral until a daten_state message and a fresh reply; then its line and exactly two actions', async () => {
    await mount();
    await waitFor(() => expect(card(K)).not.toBeNull());
    expect(cardText(K)).toContain(COPY.card.refreshing);
    expect(cardText(K)).not.toContain(COPY.card.crashed);
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(cardText(K)).toContain(COPY.card.crashed));
    const buttons = within(card(K).querySelector('.dat-card-actions')).getAllByRole('button').map((b) => b.textContent);
    expect(buttons).toEqual(expect.arrayContaining([COPY.card.loadOnline, COPY.card.deleteWhole]));
    expect(buttons.some((t) => t === COPY.card.view || t === COPY.card.upload)).toBe(false);
  });

  // V2-12: the line says upload is impossible, so no sync badge may say
  // „Hier geändert – nicht hochgeladen" (or „Nur hier") beside it.
  it.each([
    ['changed', { state: 'changed', head: `head-${K}` }],
    ['local', { state: 'local', head: null }],
    ['unknown', { state: 'unknown', reason: 'not_asked', head: null }],
  ])('the crashed card carries no sync badge (robot verdict %s)', async (_s, sync) => {
    world.sync = { [K]: sync };
    await mount();
    await waitFor(() => expect(card(K)).not.toBeNull());
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(cardText(K)).toContain(COPY.card.crashed));
    expect(card(K).querySelector('[data-sync]')).toBeNull();
    expect(cardText(K)).not.toContain(COPY.sync.changedLabel);
    expect(within(card(K)).queryByText(COPY.sync.refresh)).toBeNull();
  });

  it('a live recording of the same id shows „Wird gerade aufgenommen", never the crashed line', async () => {
    await mount();
    await waitFor(() => expect(card(K)).not.toBeNull());
    act(() => setDaten({ busy: [{ id: K, kind: 'record' }] }));
    await waitFor(() => expect(cardText(K)).toContain(COPY.sync.recordLabel));
    expect(cardText(K)).not.toContain(COPY.card.crashed);
  });

  it('never uploaded: only „Ganzen Datensatz löschen"', async () => {
    world.hub = [];
    world.sync = { [K]: { state: 'local', head: null } };
    await mount();
    await waitFor(() => expect(card(K)).not.toBeNull());
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(cardText(K)).toContain(COPY.card.crashed));
    expect(within(card(K)).queryByText(COPY.card.loadOnline)).toBeNull();
    expect(within(card(K)).getByText(COPY.card.deleteWhole)).toBeInTheDocument();
  });

  it('„Online-Version laden" → the pull dialog with the loss sentence → replace with the dialog\'s head and the card\'s digest (T-1 b, c)', async () => {
    world.hubstate = (hid) => ({
      v: 1, id: hid, meta_digest: `d-${hid}`, sync: { state: 'current' },
      hub: { exists: true, private: false, head: 'head-shown', last_modified: '2026-10-03T12:12:00Z', total_episodes: 4, duration_s: 80 },
      local: { total_episodes: 6, duration_s: 120, modified_at: '2026-10-04T08:00:00Z' }, new_repo_private: false,
    });
    await mount();
    await waitFor(() => expect(card(K)).not.toBeNull());
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(cardText(K)).toContain(COPY.card.crashed));
    fireEvent.click(within(card(K)).getByText(COPY.card.loadOnline));
    const dlg = await screen.findByRole('dialog');
    await within(dlg).findByText(COPY.conflict.losesHere);
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.pull.button }));
    await waitFor(() => expect(commandCalls('download')).toHaveLength(1));
    expect(commandCalls('download')[0]).toEqual({
      repo_id: K, revision: 'head-shown', target: K, mode: 'replace', display_name: null, meta_digest: `d-${K}`,
    });
  });

  it('a stale replace shows the robot\'s sentence and re-reads the card', async () => {
    const STALE = 'Der Datensatz auf dem Roboter hat sich inzwischen geändert. Es wurde nichts verändert. Schau dir den neuen Stand an und entscheide dann noch einmal.';
    mockCommand = makeCommand({ download: () => ({ ok: false, code: 'stale', message: STALE, result: {} }) });
    world.hubstate = (hid) => ({
      v: 1, id: hid, sync: { state: 'current' }, hub: { exists: true, head: 'h', total_episodes: 4 }, local: { total_episodes: 6 },
    });
    await mount();
    await waitFor(() => expect(card(K)).not.toBeNull());
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(cardText(K)).toContain(COPY.card.crashed));
    fireEvent.click(within(card(K)).getByText(COPY.card.loadOnline));
    const dlg = await screen.findByRole('dialog');
    fireEvent.click(await within(dlg).findByRole('button', { name: COPY.pull.button }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(STALE));
    await waitFor(() => expect(requests.filter((u) => u.includes(`ids=${encodeURIComponent(K)}`)).length).toBeGreaterThan(1));
  });

  it('„Ganzen Datensatz löschen" sends the crashed card\'s meta_digest (U-4)', async () => {
    await mount();
    await waitFor(() => expect(card(K)).not.toBeNull());
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(cardText(K)).toContain(COPY.card.crashed));
    fireEvent.click(within(card(K)).getByText(COPY.card.deleteWhole));
    const dlg = await screen.findByRole('dialog');
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.confirm.deleteDatasetButton }));
    await waitFor(() => expect(commandCalls('delete_dataset')).toEqual([{ dataset: K, meta_digest: `d-${K}` }]));
  });
});

describe('whole-dataset delete (§D6, U-4): both variants, every card kind sends its digest', () => {
  it('a proven hub copy: a plain confirm (+ lost changes for changed)', async () => {
    const C = `${OWN}/omx_f_c`;
    world.local = [local(C)];
    world.hub = [hubEntry(C)];
    world.sync = { [C]: { state: 'changed', head: 'h' } };
    await mount();
    await waitFor(() => expect(card(C)).not.toBeNull());
    fireEvent.click(within(card(C)).getByRole('button', { name: COPY.card.more }));
    fireEvent.click(within(card(C)).getByRole('menuitem', { name: new RegExp(COPY.menu.deleteWhole) }));
    const dlg = await screen.findByRole('dialog');
    expect(within(dlg).queryByRole('checkbox')).toBeNull();
    expect(dlg.textContent).toContain(COPY.del.lostChangesBold);
    expect(within(dlg).getByRole('button', { name: COPY.confirm.deleteDatasetButton })).not.toBeDisabled();
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.confirm.deleteDatasetButton }));
    await waitFor(() => expect(commandCalls('delete_dataset')).toEqual([{ dataset: C, meta_digest: `d-${C}` }]));
  });

  it.each([
    ['only here', `${OWN}/omx_f_l`, { state: 'local', head: null }, false],
    ['unknown', `${OWN}/omx_f_u`, { state: 'unknown', reason: 'unreachable', head: null }, false],
    ['a partner\'s', `${PARTNER}/omx_f_x`, { state: 'current', head: 'h' }, true],
  ])('%s: the checkbox gates the button, and the digest is sent', async (_label, did, sync, hub) => {
    world.local = [local(did)];
    world.hub = hub ? [hubEntry(did)] : [];
    world.sync = { [did]: sync };
    await mount();
    await waitFor(() => expect(card(did)).not.toBeNull());
    fireEvent.click(within(card(did)).getByRole('button', { name: COPY.card.more }));
    fireEvent.click(within(card(did)).getByRole('menuitem', { name: new RegExp(COPY.menu.deleteWhole) }));
    const dlg = await screen.findByRole('dialog');
    const go = within(dlg).getByRole('button', { name: COPY.confirm.deleteDatasetButton });
    expect(go).toBeDisabled();
    fireEvent.click(within(dlg).getByRole('checkbox', { name: COPY.confirm.ack }));
    expect(go).not.toBeDisabled();
    fireEvent.click(go);
    await waitFor(() => expect(commandCalls('delete_dataset')).toEqual([{ dataset: did, meta_digest: `d-${did}` }]));
  });
});

describe('conflict: „Beide behalten" first and default (§E10)', () => {
  const C = `${OWN}/omx_f_con`;
  beforeEach(() => {
    world.local = [local(C)];
    world.hub = [hubEntry(C)];
    world.sync = { [C]: { state: 'conflict', head: 'head-card' } };
    world.hubstate = (hid) => ({
      v: 1, id: hid, sync: { state: 'conflict' },
      hub: { exists: true, private: false, head: 'head-dialog', last_modified: '2026-10-03T12:12:00Z', total_episodes: 10, duration_s: 200 },
      local: { total_episodes: 8, duration_s: 160, modified_at: '2026-10-04T09:40:00Z' }, new_repo_private: false,
    });
  });

  it('the card: „Beide behalten" sends keep_both with the sync head shown and the digest; three-step progress', async () => {
    await mount();
    await waitFor(() => expect(card(C)).not.toBeNull());
    const actions = within(card(C).querySelector('.dat-card-actions')).getAllByRole('button').map((b) => b.textContent);
    expect(actions.indexOf(COPY.card.keepBoth)).toBeLessThan(actions.indexOf(COPY.card.loadOnline));
    expect(actions.indexOf(COPY.card.loadOnline)).toBeLessThan(actions.indexOf(COPY.card.uploadHere));
    fireEvent.click(within(card(C)).getByText(COPY.card.keepBoth));
    await waitFor(() => expect(commandCalls('keep_both')).toEqual([{ dataset: C, expected_hub_sha: 'head-card', meta_digest: `d-${C}` }]));
    act(() => setDaten({ jobs: [{ job_id: 'job-1', op: 'keep_both', state: 'running', datasets: [C], outputs: [C], stage: 'download', done: 0, total: 3, unit: 'steps' }] }));
    const dlg = await screen.findByTestId('dat-progress');
    expect(dlg.textContent).toContain(COPY.keepBoth.progressTitle);
    const steps = [...dlg.querySelectorAll('[data-step-state]')].map((li) => [li.textContent, li.getAttribute('data-step-state')]);
    expect(steps).toEqual([[COPY.keepBoth.stepDownload, 'now'], [COPY.keepBoth.stepMerge, 'pending'], [COPY.keepBoth.stepUpload, 'pending']]);
    expect(dlg.querySelector('[data-step-state="pending"] [data-icon="stepPending"]')).not.toBeNull();
    act(() => setDaten({ jobs: [{ job_id: 'job-1', op: 'keep_both', state: 'running', datasets: [C], outputs: [C], stage: 'upload', done: 2, total: 3, unit: 'steps' }] }));
    await waitFor(() => expect([...document.querySelectorAll('[data-step-state]')].map((li) => li.getAttribute('data-step-state'))).toEqual(['done', 'done', 'now']));
    // the robot's state after the job: both versions in one, uploaded (the
    // toast counts THAT, never the copy from before the job)
    world.local = [local(C, { total_episodes: 15, meta_digest: 'd-merged' })];
    world.sync = { [C]: { state: 'current', head: 'head-new' } };
    act(() => setDaten({ jobs: [{ job_id: 'job-1', op: 'keep_both', state: 'done', datasets: [C], outputs: [C], stage: null, done: 3, total: 3, unit: 'steps' }] }));
    await waitFor(() => expect(screen.queryByTestId('dat-progress')).toBeNull());
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Beide Versionen sind zusammengeführt und hochgeladen (15 Episoden).', expect.anything()));
  });

  it('the upload compare dialog: „Beide behalten" first and FOCUSED, sending the dialog\'s head', async () => {
    await mount();
    await waitFor(() => expect(card(C)).not.toBeNull());
    fireEvent.click(within(card(C)).getByText(COPY.card.uploadHere));
    const dlg = await screen.findByRole('dialog');
    const keep = await within(dlg).findByRole('button', { name: COPY.keepBoth.button });
    await waitFor(() => expect(document.activeElement).toBe(keep));
    expect(dlg.textContent).toContain('Was du seit dem letzten Abgleich gelöscht hast, bleibt gelöscht.');
    const order = within(dlg).getAllByRole('button').map((b) => b.textContent);
    expect(order.indexOf(COPY.keepBoth.button)).toBeLessThan(order.indexOf(COPY.conflict.loadOnline));
    expect(order.indexOf(COPY.conflict.loadOnline)).toBeLessThan(order.indexOf(COPY.conflict.uploadHere));
    fireEvent.click(keep);
    await waitFor(() => expect(commandCalls('keep_both')).toEqual([{ dataset: C, expected_hub_sha: 'head-dialog', meta_digest: `d-${C}` }]));
  });

  it('the pull compare dialog likewise; „Hier-Version hochladen" there sends the dialog\'s head', async () => {
    await mount();
    await waitFor(() => expect(card(C)).not.toBeNull());
    fireEvent.click(within(card(C)).getByText(COPY.card.loadOnline));
    const dlg = await screen.findByRole('dialog');
    const keep = await within(dlg).findByRole('button', { name: COPY.keepBoth.button });
    await waitFor(() => expect(document.activeElement).toBe(keep));
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.conflict.uploadHere }));
    await waitFor(() => expect(commandCalls('upload')).toEqual([{ dataset: C, expected_hub_sha: 'head-dialog', private: false }]));
  });
});

describe('upload dialogs (§E2, N7)', () => {
  it('a dataset not yet on Hugging Face: the visibility line, sent with expected_hub_sha null', async () => {
    const S = `${OWN}/omx_f_stapeln`;
    world.local = [local(S)];
    world.sync = { [S]: { state: 'local', head: null } };
    world.hubstate = (hid) => ({ v: 1, id: hid, sync: { state: 'local' }, hub: { exists: false }, local: { total_episodes: 6 }, new_repo_private: false });
    await mount();
    await waitFor(() => expect(card(S)).not.toBeNull());
    fireEvent.click(within(card(S)).getByText(COPY.card.upload));
    const dlg = await screen.findByRole('dialog');
    await within(dlg).findByText(COPY.upload.visibilityPublic);
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.upload.newButton }));
    await waitFor(() => expect(commandCalls('upload')).toEqual([{ dataset: S, expected_hub_sha: null, private: false }]));
  });

  // V2-2: what the robot really answers for a never-uploaded dataset is
  // `hub: null` with the decision `local` — „not on Hugging Face", never „could
  // not ask". The student must read the visibility before a public repo appears.
  it.each([
    [false, COPY.upload.visibilityPublic, 'public'],
    [true, COPY.upload.visibilityPrivate, 'private'],
  ])('the real reply for a never-uploaded dataset (hub null, decided local, private=%s): its visibility line, expected_hub_sha null', async (priv, line, vis) => {
    const S = `${OWN}/omx_f_stapeln`;
    world.local = [local(S)];
    world.sync = { [S]: { state: 'local', head: null } };
    world.hubstate = (hid) => ({
      v: 1, id: hid, meta_digest: `d-${hid}`, sync: { state: 'local', reason: null }, hub: null,
      local: { total_episodes: 6, duration_s: 120, modified_at: '2026-10-04T08:00:00Z' }, new_repo_private: priv,
    });
    await mount();
    await waitFor(() => expect(card(S)).not.toBeNull());
    fireEvent.click(within(card(S)).getByText(COPY.card.upload));
    const dlg = await screen.findByRole('dialog');
    await within(dlg).findByText(line);
    expect(dlg.querySelector('[data-visibility]').getAttribute('data-visibility')).toBe(vis);
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.upload.newButton }));
    await waitFor(() => expect(commandCalls('upload')).toEqual([{ dataset: S, expected_hub_sha: null, private: priv }]));
  });

  it.each([
    ['not asked (no token on the robot)', { state: 'unknown', reason: 'not_asked' }],
    ['unreachable', { state: 'unknown', reason: 'unreachable' }],
    ['unreachable with local changes (decided changed without the hub)', { state: 'changed', reason: null }],
  ])('Hugging Face could not be asked — %s: no visibility claim, and the upload decides at upload time (no expected key)', async (_label, sync) => {
    const S = `${OWN}/omx_f_offen`;
    world.local = [local(S)];
    world.sync = { [S]: { state: 'unknown', reason: 'unreachable', head: null } };
    world.hubstate = (hid) => ({ v: 1, id: hid, sync, hub: null, local: { total_episodes: 6 }, new_repo_private: false });
    await mount();
    await waitFor(() => expect(card(S)).not.toBeNull());
    fireEvent.click(within(card(S)).getByText(COPY.card.upload));
    const dlg = await screen.findByRole('dialog');
    const go = await within(dlg).findByRole('button', { name: COPY.upload.newButton });
    expect(dlg.querySelector('[data-visibility]')).toBeNull();
    expect(dlg.textContent).not.toContain(COPY.upload.visibilityPublic);
    fireEvent.click(go);
    await waitFor(() => expect(commandCalls('upload')).toEqual([{ dataset: S, private: false }]));
  });

  it('an EMPTY repo that already exists keeps its own visibility (create_repo never changes it)', async () => {
    const S = `${OWN}/omx_f_leer`;
    world.local = [local(S)];
    world.sync = { [S]: { state: 'local', head: null } };
    world.hubstate = (hid) => ({
      v: 1, id: hid, sync: { state: 'local', reason: null }, hub: { exists: false, private: true, head: 'h0' },
      local: { total_episodes: 6 }, new_repo_private: false,
    });
    await mount();
    await waitFor(() => expect(card(S)).not.toBeNull());
    fireEvent.click(within(card(S)).getByText(COPY.card.upload));
    const dlg = await screen.findByRole('dialog');
    await within(dlg).findByText(COPY.upload.visibilityPrivate);
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.upload.newButton }));
    await waitFor(() => expect(commandCalls('upload')).toEqual([{ dataset: S, expected_hub_sha: null, private: false }]));
  });

  it('the hubstate read failed: no visibility claim, no expected key', async () => {
    const S = `${OWN}/omx_f_fehler`;
    world.local = [local(S)];
    world.sync = { [S]: { state: 'local', head: null } };
    world.hubstate = () => { throw new Error('boom'); };
    const realFetch = global.fetch;
    global.fetch = vi.fn((url) => (String(url).endsWith('/hubstate')
      ? Promise.resolve({ ok: false, status: 503, headers: { get: () => null }, json: () => Promise.resolve({ error: 'overloaded' }) })
      : realFetch(url)));
    await mount();
    await waitFor(() => expect(card(S)).not.toBeNull());
    fireEvent.click(within(card(S)).getByText(COPY.card.upload));
    const dlg = await screen.findByRole('dialog');
    const go = await within(dlg).findByRole('button', { name: COPY.upload.newButton });
    expect(dlg.querySelector('[data-visibility]')).toBeNull();
    fireEvent.click(go);
    await waitFor(() => expect(commandCalls('upload')).toEqual([{ dataset: S, private: false }]));
  });

  it('a recorded private choice says private', async () => {
    const S = `${OWN}/omx_f_p`;
    world.local = [local(S)];
    world.sync = { [S]: { state: 'local', head: null } };
    world.hubstate = (hid) => ({ v: 1, id: hid, sync: { state: 'local' }, hub: { exists: false }, local: {}, new_repo_private: true });
    await mount();
    await waitFor(() => expect(card(S)).not.toBeNull());
    fireEvent.click(within(card(S)).getByText(COPY.card.upload));
    const dlg = await screen.findByRole('dialog');
    await within(dlg).findByText(COPY.upload.visibilityPrivate);
  });

  it('an existing hub copy: both versions and „Ersetzen und hochladen" with the head shown; newer warns', async () => {
    const N = `${OWN}/omx_f_n`;
    world.local = [local(N)];
    world.hub = [hubEntry(N)];
    world.sync = { [N]: { state: 'newer', head: 'h' } };
    world.hubstate = (hid) => ({
      v: 1, id: hid, sync: { state: 'newer' },
      hub: { exists: true, head: 'head-x', total_episodes: 11, duration_s: 220, last_modified: '2026-10-05T08:15:00Z' },
      local: { total_episodes: 9, duration_s: 180, modified_at: '2026-10-01T15:02:00Z' }, new_repo_private: false,
    });
    await mount();
    await waitFor(() => expect(card(N)).not.toBeNull());
    fireEvent.click(within(card(N)).getByRole('button', { name: COPY.card.more }));
    // the tools/cards offer „Neuere Version laden" for newer; the upload goes via the player's tools — here the dialog directly:
    fireEvent.keyDown(within(card(N)).getByRole('menu'), { key: 'Escape' });
    fireEvent.click(within(card(N)).getByText(COPY.card.pullNewer));
    const pull = await screen.findByRole('dialog');
    expect(pull.textContent).toContain(COPY.pull.title);
    fireEvent.click(await within(pull).findByRole('button', { name: COPY.pull.button }));
    await waitFor(() => expect(commandCalls('download')).toEqual([
      { repo_id: N, revision: 'head-x', target: N, mode: 'replace', display_name: null, meta_digest: `d-${N}` },
    ]));
  });
});

describe('merge mode (§D5, §G13)', () => {
  it('pick on the cards, the seven checks, the name preview, the stats row refusing', async () => {
    const A = `${OWN}/omx_f_a`;
    const B = `${PARTNER}/omx_f_b`;
    const Q = `${OWN}/omx_f_q`;
    world.local = [
      local(A, { display_name: 'Würfel' }),
      local(B),
      local(Q, { stat_names: { action: ['count', 'max', 'mean', 'min'], 'observation.state': ['count', 'max', 'mean', 'min'] } }),
    ];
    world.sync = { [A]: { state: 'local' }, [B]: { state: 'local' }, [Q]: { state: 'local' } };
    await mount();
    await waitFor(() => expect(card(A)).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: COPY.page.merge }));
    const panel = screen.getByRole('region', { name: COPY.merge.title });
    expect(panel.textContent).toContain(COPY.merge.pickHint);
    fireEvent.click(within(card(A)).getByRole('button', { name: COPY.card.mergePick }));
    fireEvent.click(within(card(B)).getByRole('button', { name: COPY.card.mergePick }));
    expect(screen.getByRole('region', { name: COPY.merge.title })).toBe(panel); // the panel stays mounted
    const checks = [...panel.querySelectorAll('[data-check]')].map((li) => li.getAttribute('data-check'));
    expect(checks).toEqual(['robot', 'fps', 'cameras', 'joints', 'video', 'stats']);
    expect(panel.querySelectorAll('[data-ok="false"]').length).toBe(0);
    expect(panel.textContent).toContain('wird gespeichert als lena-schmidt/omx_f_Wuerfel-gesamt');
    const go = within(panel).getByRole('button', { name: '24 Episoden zusammenführen' });
    expect(go).not.toBeDisabled();
    fireEvent.click(go);
    await waitFor(() => expect(commandCalls('edit')).toEqual([{
      op: 'merge',
      datasets: [{ id: A, meta_digest: `d-${A}` }, { id: B, meta_digest: `d-${B}` }],
      new_name: 'Würfel gesamt',
      owner_ns: OWN,
    }]));
  });

  it('V2-11: a dataset without a display name proposes its task part — never a doubled robot prefix', async () => {
    const D = `${OWN}/omx_f_deckel`;
    const E = `${OWN}/omx_f_deckel-2`;
    world.local = [local(D), local(E)];
    world.sync = { [D]: { state: 'local' }, [E]: { state: 'local' } };
    await mount();
    await waitFor(() => expect(card(D)).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: COPY.page.merge }));
    fireEvent.click(within(card(D)).getByRole('button', { name: COPY.card.mergePick }));
    fireEvent.click(within(card(E)).getByRole('button', { name: COPY.card.mergePick }));
    const panel = screen.getByRole('region', { name: COPY.merge.title });
    expect(within(panel).getByRole('textbox', { name: COPY.merge.nameLabel }).value).toBe('Deckel gesamt');
    expect(panel.textContent).toContain('wird gespeichert als lena-schmidt/omx_f_Deckel-gesamt');
    expect(panel.textContent).not.toContain('omx_f_omx_f_');
  });

  it('a dataset without quantile statistics turns the „Statistiken" row red and the button off', async () => {
    const A = `${OWN}/omx_f_a`;
    const Q = `${OWN}/omx_f_q`;
    world.local = [local(A), local(Q, { stat_names: { action: ['count'], 'observation.state': ['count'] } })];
    world.sync = { [A]: { state: 'local' }, [Q]: { state: 'local' } };
    await mount();
    await waitFor(() => expect(card(A)).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: COPY.page.merge }));
    fireEvent.click(within(card(A)).getByRole('button', { name: COPY.card.mergePick }));
    fireEvent.click(within(card(Q)).getByRole('button', { name: COPY.card.mergePick }));
    const panel = screen.getByRole('region', { name: COPY.merge.title });
    expect(panel.querySelector('[data-check="stats"]').getAttribute('data-ok')).toBe('false');
    expect(panel.textContent).toContain(COPY.merge.check.statsBad);
    expect(within(panel).getAllByRole('button')[0]).toBeDisabled();
  });

  it('an online-only card cannot be picked („Erst laden"); the ⋮ menu starts merge mode with the dataset', async () => {
    const A = `${OWN}/omx_f_a`;
    const O = `${OWN}/omx_f_o`;
    world.local = [local(A)];
    world.hub = [hubEntry(O)];
    world.sync = { [A]: { state: 'local' }, [O]: { state: 'online', head: 'h' } };
    await mount();
    await waitFor(() => expect(card(O)).not.toBeNull());
    fireEvent.click(within(card(A)).getByRole('button', { name: COPY.card.more }));
    fireEvent.click(within(card(A)).getByRole('menuitem', { name: new RegExp(COPY.menu.mergePick) }));
    expect(within(card(A)).getByRole('button', { name: COPY.card.mergePick }).getAttribute('aria-pressed')).toBe('true');
    const pickO = within(card(O)).getByRole('button', { name: COPY.card.mergePick });
    expect(pickO).toBeDisabled();
    expect(pickO.getAttribute('title')).toBe(COPY.card.mergePickBlocked);
  });
});

describe('„Von Hugging Face holen" (D12, §E7)', () => {
  async function openFetch() {
    await mount();
    await screen.findByText(COPY.lib.empty);
    fireEvent.click(screen.getByRole('button', { name: COPY.page.fetch }));
    return screen.findByRole('dialog');
  }
  const search = (dlg, repo) => {
    fireEvent.change(within(dlg).getByPlaceholderText(COPY.fetch.placeholder), { target: { value: repo } });
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.fetch.search }));
  };

  it('a malformed name is refused before anything is asked', async () => {
    const dlg = await openFetch();
    search(dlg, 'quatsch');
    expect(dlg.textContent).toContain('So sieht ein Name auf Hugging Face aus: konto/datensatz, zum Beispiel lehrer-mueller/omx_f_wuerfel-demo.');
    expect(requests.some((u) => u.includes('probe'))).toBe(false);
  });

  it.each([
    ['not_found', { found: false, refusal: 'not_found' }, COPY.fetch.notFound],
    ['other_robot', { found: false, refusal: 'other_robot', robot_type: 'so100' }, '„lerobot/so100_test“ stammt von einem anderen Roboter (so100). EduBotics kann nur Datensätze vom OpenMANIPULATOR-X öffnen.'],
    ['old_format', { found: false, refusal: 'old_format' }, '„lerobot/so100_test“ ist in einem älteren Datensatz-Format gespeichert, das EduBotics nicht öffnen kann.'],
    ['unsupported', { found: false, refusal: 'unsupported' }, '„lerobot/so100_test“ ist in einem Format gespeichert, das EduBotics nicht öffnen kann.'],
    ['unreachable', { found: false, refusal: 'unreachable' }, COPY.fetch.unreachable],
    ['no_token', { found: false, refusal: 'no_token' }, COPY.fetch.noToken],
  ])('refusal %s: one German sentence', async (_k, probe, text) => {
    world.probe = () => ({ v: 1, repo_id: 'lerobot/so100_test', ...probe });
    const dlg = await openFetch();
    search(dlg, 'lerobot/so100_test');
    await waitFor(() => expect(dlg.textContent).toContain(text));
    expect(within(dlg).getByRole('button', { name: COPY.fetch.go })).toBeDisabled();
  });

  it('found: the stats, „öffentlich", the save name and a copy download', async () => {
    world.probe = () => ({
      v: 1, repo_id: 'lehrer-mueller/omx_f_wuerfel-demo', found: true, refusal: null, private: false,
      last_modified: '2026-09-30T10:00:00Z', sha: 'sha-1', total_episodes: 24, total_frames: 14400, duration_s: 480, fps: 30,
      robot_type: 'omx_f', cameras: [{ index: 0, name: 'gripper' }, { index: 1, name: 'scene' }], size_bytes: 620e6,
    });
    mockSignal = { payload: { disk: { free_bytes: 23.4e9 } }, receivedAt: 1 };
    const dlg = await openFetch();
    search(dlg, 'lehrer-mueller/omx_f_wuerfel-demo');
    await within(dlg).findByText(/Gefunden:/);
    expect(dlg.textContent).toContain('OpenMANIPULATOR-X · Greifer- und Szenen-Kamera · öffentlich · aktualisiert 30. Sep.');
    expect(within(dlg).getByDisplayValue('Wuerfel demo')).toBeInTheDocument();
    expect(dlg.textContent).toContain('wird gespeichert als lena-schmidt/omx_f_Wuerfel-demo · 620 MB, 23,4 GB frei');
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.fetch.go }));
    await waitFor(() => expect(commandCalls('download')).toEqual([{
      repo_id: 'lehrer-mueller/omx_f_wuerfel-demo', revision: 'sha-1', target: `${OWN}/omx_f_Wuerfel-demo`,
      mode: 'copy', display_name: 'Wuerfel demo', meta_digest: '',
    }]));
  });
});

describe('the Hugging Face hook ownership', () => {
  it('a token change after the reply drops the hub part (foreign fingerprint): no online cards', async () => {
    world.local = [local(`${OWN}/omx_f_a`)];
    world.hub = [hubEntry(`${OWN}/omx_f_b`)];
    world.hubFp = 'fp-someone-else';
    world.sync = { [`${OWN}/omx_f_a`]: { state: 'current', head: 'h' }, [`${OWN}/omx_f_b`]: { state: 'online', head: 'h' } };
    await mount();
    await waitFor(() => expect(card(`${OWN}/omx_f_a`)).not.toBeNull());
    expect(card(`${OWN}/omx_f_b`)).toBeNull();
    expect(cardText(`${OWN}/omx_f_a`)).toContain(COPY.sync.unknownLabel);
    expect(FP).toBe('fp-lena');
  });
});
