// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Daten page's player (spec §J.B step 5, §F): the episode list and its
// marks, every key (and none inside an input or under a dialog), the speeds as
// whole literals, an unplayable clip's tile and row, the delete and split tools
// with their previews and the `meta_digest` they send, a stale refusal that
// re-reads the dataset and drops the marks, the progress dialog driven by
// /edubotics/daten_state, the newer warning, the 3D twin's recorded pose — and
// the render invariant: 100 driver frames render none of the player's parts.

/* eslint-disable testing-library/no-node-access, testing-library/no-container */

import React, { useSyncExternalStore } from 'react';
import {
  act, fireEvent, render, screen, waitFor, within,
} from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import DatenPage from '../DatenPage';
import { nextPlayerShown } from '../PlayerView';
import COPY from '../../../features/editDataset/datenCopy';
import { fill, fmtDate } from '../../../features/editDataset/model/format';
import { openPlayer } from '../../../features/editDataset/editDatasetSlice';
import { setRenderProbeForTests } from '../../../features/editDataset/renderProbe';
import {
  OWN, PARTNER, hubEntry, installSidecar, local, makeCommand, makeStore, summary, wrap,
} from './datenHarness';

let mockCommand = null;
vi.mock('../../../features/editDataset/hooks/useDatenCommand', () => ({ __esModule: true, default: () => mockCommand }));
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
vi.mock('../../../features/editDataset/hooks/useGroupNamespaces', () => ({ __esModule: true, default: () => mockGroup }));
vi.mock('../../../hooks/useSignalStatus', () => ({ __esModule: true, default: () => ({ payload: null, receivedAt: null }) }));
vi.mock('../../../utils/cloudMode', () => ({ __esModule: true, isCloudOnlyMode: () => false }));
const mockTwinProps = [];
vi.mock('../../UrdfTwin', () => ({
  __esModule: true,
  default: (props) => { mockTwinProps.push(props); return null; },
}));
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  fn.dismiss = vi.fn();
  return { __esModule: true, default: fn, useToasterStore: () => ({ toasts: [] }) };
});

const W = `${OWN}/omx_f_w`;
let world;

function setDaten(payload) {
  mockDaten.snap = { received: true, payload: { v: 1, seq: 1, busy: [], jobs: [], transfer: null, ...payload }, receivedAt: Date.now() };
  mockDaten.listeners.forEach((fn) => fn());
}

beforeEach(() => {
  mockGroup = { status: 'ready', own: OWN, namespaces: [OWN, PARTNER], names: { [PARTNER]: 'Max Weber' } };
  mockCommand = makeCommand();
  mockDaten.snap = { received: false, payload: null, receivedAt: null };
  mockDaten.listeners.clear();
  mockTwinProps.length = 0;
  const sm = summary(W, 3);
  sm.episodes[1].hints = [{ type: 'lag', at_s: 1.2, joint: 1, value_deg: 26 }];
  sm.episodes[2].playable = false;
  world = {
    local: [local(W, { total_episodes: 3 })],
    hub: [hubEntry(W)],
    sync: { [W]: { state: 'current', head: `head-${W}` } },
    summaries: { [W]: sm },
    hubstate: (id) => ({ v: 1, id, sync: { state: 'current' }, hub: null, local: {} }),
    probe: () => ({ found: false, refusal: 'not_found' }),
  };
  installSidecar(world);
  toast.success.mockClear();
  toast.error.mockClear();
  toast.mockClear();
  HTMLMediaElement.prototype.play = vi.fn(function play() { return Promise.resolve(); });
  HTMLMediaElement.prototype.pause = vi.fn();
  HTMLVideoElement.prototype.requestVideoFrameCallback = function rvfc(cb) { this.__rvfc = cb; return 1; };
  HTMLVideoElement.prototype.cancelVideoFrameCallback = function cvfc() { this.__rvfc = null; };
});
afterEach(() => {
  delete global.fetch;
  delete HTMLVideoElement.prototype.requestVideoFrameCallback;
  delete HTMLVideoElement.prototype.cancelVideoFrameCallback;
  setRenderProbeForTests(false);
});

async function openPlayerView(storeOpts = {}) {
  const store = makeStore(storeOpts);
  store.dispatch(openPlayer(W));
  const utils = render(wrap(store, <DatenPage />));
  await screen.findByText(COPY.player.eyebrow);
  await waitFor(() => expect(document.querySelectorAll('.dat-ep-row').length).toBe(3));
  return { store, ...utils };
}
const frameText = () => document.querySelector('[data-frame]').textContent;
const requestsHubstate = () => global.fetch.mock.calls.filter(([u]) => String(u).endsWith('/hubstate')).length;
const currentRow = () => document.querySelector('.dat-ep-open[aria-current="true"]').closest('[data-episode]').getAttribute('data-episode');
const key = (code, extra = {}) => fireEvent.keyDown(window, { code, ...extra });
const commandCalls = (action) => mockCommand.mock.calls.filter(([a]) => a === action).map(([, args]) => args);
const sceneVideo = () => document.querySelector('[data-camera="scene"] video');
// The render probe's counters (spec §G12), copied.
function probeCounts() {
  return { ...window.__datenRenders };
}
const frame = (el, i, edt) => act(() => { const cb = el.__rvfc; el.__rvfc = null; if (cb) cb(0, { mediaTime: i / 30, expectedDisplayTime: edt }); });

describe('the player (§F)', () => {
  it('the summary, the list with hints and the unplayable flag, the details', async () => {
    await openPlayerView();
    expect(document.querySelectorAll('.dat-ep-row').length).toBe(3);
    expect(currentRow()).toBe('0');
    const rows = document.querySelectorAll('.dat-ep-row');
    expect(rows[1].querySelector('.dat-ep-flag').textContent).toBe('1');
    expect(rows[2].querySelector('[title]').getAttribute('title')).toBe(COPY.player.unplayable);
    expect(screen.getByText(COPY.player.detailsTitle.replace('{n}', '1'))).toBeInTheDocument();
    expect(document.body.textContent).toContain('In den Daten: episode_index 0');
    expect(document.body.textContent).toContain(COPY.player.changedAt);
    expect(document.body.textContent).not.toContain('Aufgenommen');
  });

  it('every key: frame steps, 5 s, episodes, play, mark, first/last frame', async () => {
    await openPlayerView();
    expect(frameText()).toBe('Bild 1 / 60');
    key('ArrowRight');
    expect(frameText()).toBe('Bild 2 / 60');
    key('ArrowLeft');
    expect(frameText()).toBe('Bild 1 / 60');
    key('ArrowRight', { shiftKey: true });
    expect(frameText()).toBe('Bild 60 / 60');
    key('Home');
    expect(frameText()).toBe('Bild 1 / 60');
    key('End');
    expect(frameText()).toBe('Bild 60 / 60');
    key('ArrowDown');
    await waitFor(() => expect(currentRow()).toBe('1'));
    key('ArrowUp');
    await waitFor(() => expect(currentRow()).toBe('0'));
    key('Space');
    await screen.findByRole('button', { name: COPY.player.pause });
    key('Space');
    await screen.findByRole('button', { name: COPY.player.play });
    key('Delete');
    await waitFor(() => expect(document.querySelector('.dat-ep-row[data-episode="0"]').className).toContain('dat-marked'));
    key('Backspace');
    await waitFor(() => expect(document.querySelector('.dat-ep-row[data-episode="0"]').className).not.toContain('dat-marked'));
  });

  it('keys are ignored inside an input, with a modifier, and while a dialog is open', async () => {
    await openPlayerView();
    const input = document.createElement('input');
    document.body.appendChild(input);
    fireEvent.keyDown(input, { code: 'ArrowRight' });
    expect(frameText()).toBe('Bild 1 / 60');
    input.remove();
    key('ArrowRight', { ctrlKey: true });
    expect(frameText()).toBe('Bild 1 / 60');
    fireEvent.click(screen.getByRole('button', { name: new RegExp(COPY.tools.deleteEpisodes) }));
    const dlg = await screen.findByRole('dialog');
    fireEvent.keyDown(dlg, { code: 'ArrowRight' });
    key('ArrowRight');
    expect(frameText()).toBe('Bild 1 / 60');
  });

  it('the speeds are the four whole literals and set playbackRate', async () => {
    await openPlayerView();
    const group = screen.getByRole('group', { name: COPY.player.speedLabel });
    expect(within(group).getAllByRole('button').map((b) => b.textContent)).toEqual(['0,25×', '0,5×', '1×', '2×']);
    fireEvent.click(within(group).getByText('0,5×'));
    expect(within(group).getByText('0,5×').getAttribute('aria-pressed')).toBe('true');
    document.querySelectorAll('video').forEach((v) => expect(v.playbackRate).toBe(0.5));
  });

  it('a clip the cutter refuses shows the unplayable sentence in its tile; the other camera stays', async () => {
    world.clip = (rest) => (rest.endsWith('/video/0.mp4') ? { status: 409, code: 'unplayable' } : null);
    await openPlayerView();
    const grip = document.querySelector('[data-camera="gripper"] video');
    fireEvent.error(grip);
    await waitFor(() => expect(document.querySelector('[data-camera="gripper"]').textContent).toContain(COPY.player.unplayable));
    expect(document.querySelector('[data-camera="scene"] video')).not.toBeNull();
  });

  it('a clip that cannot load (not unplayable, not overloaded) offers „Erneut versuchen"', async () => {
    world.clip = () => ({ status: 500, code: 'internal' });
    await openPlayerView();
    fireEvent.error(document.querySelector('[data-camera="scene"] video'));
    await waitFor(() => expect(document.querySelector('[data-camera="scene"]').textContent).toContain(COPY.player.loadFailed));
    fireEvent.click(within(document.querySelector('[data-camera="scene"]')).getByText(COPY.player.retry));
    expect(document.querySelector('[data-camera="scene"] video')).not.toBeNull();
  });

  it('the 3D twin is posed from the recording: follower state as the arm, leader action as a violet ghost', async () => {
    await openPlayerView();
    await waitFor(() => expect(mockTwinProps.length).toBeGreaterThan(0));
    const { poseSource, showChrome, viewPreset } = mockTwinProps[mockTwinProps.length - 1];
    expect(showChrome).toBe(false);
    expect(viewPreset).toBe('persp');
    await waitFor(() => expect(poseSource().pose).not.toBeNull());
    const f = poseSource();
    expect(f.pose.names[0]).toBe('joint1');
    expect(f.ghost.color).toBe('#7A6FE0');
    key('ArrowRight');
    expect(poseSource().version).not.toBe(f.version);
  });

  it('the open dataset turns crashed (a session into it ended unfinished): the player stays, without a sync badge (V2-12)', async () => {
    await openPlayerView();
    expect(document.querySelector('.dat-pl-meta [data-sync]')).not.toBeNull();
    world.local = [local(W, { total_episodes: 3, state: 'in_session' })];
    act(() => setDaten({ busy: [{ id: W, kind: 'record' }] }));
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(document.querySelector('.dat-pl-meta [data-sync]')).toBeNull());
    expect(screen.getByText(COPY.player.eyebrow)).toBeInTheDocument();
  });

  // T2-2: the player shows what the card shows when its dataset cannot be
  // viewed or edited right now — never „Hier geändert … Jetzt hochladen" or the
  // edit tools beside a dataset the robot refuses to upload or edit.
  it('the open dataset is recorded, then the session ends unfinished: the card\'s sentence and actions, no tools (T2-2)', async () => {
    world.sync = { [W]: { state: 'changed', head: `head-${W}` } };
    const { store } = await openPlayerView();
    expect(document.querySelector('[data-banner="changed"]')).not.toBeNull();
    expect(screen.getByRole('toolbar', { name: COPY.tools.label })).toBeInTheDocument();
    const summaries = () => global.fetch.mock.calls.filter(([u]) => String(u).endsWith('/summary')).length;
    const asked = summaries();

    // a recording into it starts: the live state
    world.local = [local(W, { total_episodes: 3, state: 'in_session', meta_digest: 'd-rec' })];
    delete world.summaries[W];
    act(() => setDaten({ busy: [{ id: W, kind: 'record' }] }));
    await waitFor(() => expect(document.querySelector('[data-player-state="live"]')).not.toBeNull());
    expect(document.querySelector('[data-player-state="live"]').textContent).toBe(COPY.player.live);
    expect(screen.queryByRole('toolbar', { name: COPY.tools.label })).toBeNull();
    expect(document.querySelector('[data-banner]')).toBeNull();
    expect(document.querySelectorAll('.dat-ep-row').length).toBe(0);

    // the session ends unfinished (the marker stays, no live recording): crashed
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(document.querySelector('[data-player-state="crashed"]')).not.toBeNull());
    const state = document.querySelector('[data-player-state="crashed"]');
    expect(state.textContent).toContain(COPY.card.crashed);
    expect(within(state).getAllByRole('button').map((b) => b.textContent)).toEqual([COPY.card.loadOnline, COPY.card.deleteWhole]);
    expect(screen.queryByRole('toolbar', { name: COPY.tools.label })).toBeNull();
    expect(document.querySelector('[data-banner]')).toBeNull();
    expect(document.body.textContent).not.toContain(COPY.banner.changedButton);
    expect(document.body.textContent).not.toContain(COPY.tools.deleteEpisodes);
    expect(document.body.textContent).not.toContain(COPY.http.generic);
    expect(document.querySelector('.dat-pl-meta [data-sync]')).toBeNull();
    expect(screen.getByText(COPY.player.eyebrow)).toBeInTheDocument();
    // no summary was asked for a dataset the robot answers 409 for, and no key marks
    key('Delete');
    key('ArrowDown');
    expect(store.getState().editDataset.marks[W]).toBeUndefined();
    await new Promise((r) => { setTimeout(r, 50); });
    expect(summaries()).toBe(asked);

    // the card's actions: „Ganzen Datensatz löschen" opens the whole-delete confirm
    fireEvent.click(within(state).getByRole('button', { name: COPY.card.deleteWhole }));
    const dlg = await screen.findByRole('dialog');
    expect(within(dlg).getByRole('heading').textContent).toBe(COPY.confirm.deleteDatasetTitle);
  });

  it.each([
    ['no recorded sync (T2-1)', null, COPY.keepBoth.tipNoBase],
    ['a recorded sync', { hub_sha: 'head-base', synced_at: null, source_repo: null, private: false, tag_ok: true }, COPY.keepBoth.tip],
  ])('the conflict banner, %s: „Beide behalten" says what it will do', async (_k, record, tip) => {
    world.local = [local(W, { total_episodes: 3, record })];
    world.sync = { [W]: { state: 'conflict', head: `head-${W}` } };
    await openPlayerView();
    const banner = document.querySelector('[data-banner="conflict"]');
    expect(within(banner).getByRole('button', { name: COPY.keepBoth.button }).getAttribute('title')).toBe(tip);
    expect(banner.textContent).toContain(tip);
  });

  it('a crashed dataset repaired while open („Online-Version laden" landed): the player comes back', async () => {
    await openPlayerView();
    world.local = [local(W, { total_episodes: 3, state: 'in_session' })];
    act(() => setDaten({ busy: [{ id: W, kind: 'record' }] }));
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(document.querySelector('[data-player-state="crashed"]')).not.toBeNull());
    world.local = [local(W, { total_episodes: 4, meta_digest: 'd-pulled' })];
    world.summaries[W] = summary(W, 4, { meta_digest: 'd-pulled' });
    act(() => setDaten({ busy: [{ id: W, kind: 'download' }] }));
    act(() => setDaten({ busy: [] }));
    await waitFor(() => expect(document.querySelectorAll('.dat-ep-row').length).toBe(4));
    expect(document.querySelector('[data-player-state]')).toBeNull();
    expect(screen.getByRole('toolbar', { name: COPY.tools.label })).toBeInTheDocument();
  });

  it('the twin is not mounted while the robot link is down', async () => {
    const store = makeStore({ connected: false });
    store.dispatch(openPlayer(W));
    render(wrap(store, <DatenPage />));
    await screen.findByText(COPY.lib.offline);
    expect(mockTwinProps.length).toBe(0);
  });
});

describe('nextPlayerShown (T2-2): a blocked player opens again only after the robot\'s re-read', () => {
  const run = (kinds, start = 'ok') => kinds.reduce((s, k) => nextPlayerShown(s, k), { kind: start, reread: false }).kind;
  it.each([
    [['live'], 'live'],
    [['live', 'ok'], 'live'], // the busy change renders once with the reply from before it
    [['live', 'ok', 'refreshing'], 'live'],
    [['live', 'ok', 'refreshing', 'crashed'], 'crashed'],
    [['live', 'refreshing', 'ok'], 'ok'], // a recording that ended cleanly
    [['crashed', 'busy', 'refreshing', 'ok'], 'ok'], // „Online-Version laden" landed
    [['refreshing', 'ok'], 'ok'],
    [['busy', 'ok'], 'ok'],
    [['broken', 'ok'], 'broken'],
  ])('%j → %s', (kinds, shown) => {
    expect(run(kinds)).toBe(shown);
  });
  it('the same object when nothing changes (no render loop)', () => {
    const s = { kind: 'ok', reread: false };
    expect(nextPlayerShown(s, 'ok')).toBe(s);
    expect(nextPlayerShown(s, 'refreshing')).toBe(s);
    const b = { kind: 'crashed', reread: false };
    expect(nextPlayerShown(b, 'crashed')).toBe(b);
    expect(nextPlayerShown(b, 'ok')).toBe(b);
  });
});

describe('the tools (§G6, §J.3, R-9)', () => {
  it('delete by numbers: errors per token, the all-episodes refusal, the preview, the digest sent', async () => {
    await openPlayerView();
    fireEvent.click(screen.getByRole('button', { name: new RegExp(COPY.tools.deleteEpisodes) }));
    const dlg = await screen.findByRole('dialog');
    const input = within(dlg).getByPlaceholderText(COPY.tool.placeholder);
    const t0 = performance.now();
    fireEvent.change(input, { target: { value: '0-1000000' } });
    expect(performance.now() - t0).toBeLessThan(200);
    expect(dlg.textContent).toContain('0-1000000: Es gibt nur Episode 1 bis 3.');
    fireEvent.change(input, { target: { value: 'x' } });
    expect(dlg.textContent).toContain('„x“ ist keine Episodennummer.');
    fireEvent.change(input, { target: { value: '1-3' } });
    expect(dlg.textContent).toContain(COPY.tool.errAll);
    expect(within(dlg).getByRole('button', { name: COPY.tool.deleteButton })).toBeDisabled();
    fireEvent.change(input, { target: { value: '1, 3' } });
    expect(dlg.textContent).toContain('2 von 3 Episoden werden gelöscht, 1 bleiben und heißen danach Episode 1 bis 1.');
    fireEvent.click(within(dlg).getByRole('button', { name: '2 Episoden löschen' }));
    await waitFor(() => expect(commandCalls('edit')).toEqual([{ op: 'delete', dataset: W, meta_digest: `d-${W}`, episodes: [0, 2] }]));
  });

  it('the quick pick „Alle mit Hinweisen" fills the hinted episodes', async () => {
    await openPlayerView();
    fireEvent.click(screen.getByRole('button', { name: new RegExp(COPY.tools.deleteEpisodes) }));
    const dlg = await screen.findByRole('dialog');
    fireEvent.click(within(dlg).getByText('Alle mit Hinweisen (1)'));
    expect(within(dlg).getByPlaceholderText(COPY.tool.placeholder).value).toBe('2');
  });

  it('split: the preview of both parts, the name checks, the split sent with its name and namespace', async () => {
    world.local.push(local(`${OWN}/omx_f_besetzt`));
    world.sync[`${OWN}/omx_f_besetzt`] = { state: 'local' };
    await openPlayerView();
    fireEvent.click(screen.getByRole('button', { name: new RegExp(COPY.tools.split) }));
    const dlg = await screen.findByRole('dialog');
    fireEvent.change(within(dlg).getByPlaceholderText(COPY.tool.placeholder), { target: { value: '2' } });
    // V2-11: no display name → the task part („W"), never „omx_f_w Teil 2" (omx_f_omx_f_…)
    const nameInput = within(dlg).getByDisplayValue('W Teil 2');
    expect(dlg.textContent).toContain(COPY.tool.splitKeeps);
    expect(dlg.textContent).toContain('1 Episode: 2');
    fireEvent.change(nameInput, { target: { value: '' } });
    expect(dlg.textContent).toContain(COPY.tool.errNoName);
    fireEvent.change(nameInput, { target: { value: 'besetzt' } });
    expect(dlg.textContent).toContain(COPY.tool.errNameExists);
    fireEvent.change(nameInput, { target: { value: 'Würfel Teil 2' } });
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.tool.splitButton }));
    await waitFor(() => expect(commandCalls('edit')).toEqual([{
      op: 'split', dataset: W, meta_digest: `d-${W}`, episodes: [1], new_name: 'Würfel Teil 2', owner_ns: OWN,
    }]));
  });

  it('the progress dialog follows the job on daten_state (pending steps are an outline circle) and ends with a toast', async () => {
    await openPlayerView();
    key('Delete');
    await waitFor(() => expect(document.querySelector('.dat-ep-row[data-episode="0"]').className).toContain('dat-marked'));
    fireEvent.click(screen.getByRole('button', { name: COPY.player.deleteMarked }));
    const confirm = await screen.findByRole('dialog');
    expect(confirm.textContent).toContain('Bild und Daten der übrigen Episoden bleiben unverändert.');
    fireEvent.click(within(confirm).getByRole('button', { name: '1 Episode löschen' }));
    await waitFor(() => expect(commandCalls('edit')).toHaveLength(1));
    act(() => setDaten({ jobs: [{ job_id: 'job-1', op: 'delete', state: 'running', datasets: [W], outputs: [W], stage: 'copy', done: 1, total: 3, unit: 'steps' }] }));
    const prog = await screen.findByTestId('dat-progress');
    expect(prog.textContent).toContain(COPY.progress.deleteTitle);
    expect([...prog.querySelectorAll('[data-step-state]')].map((li) => li.getAttribute('data-step-state'))).toEqual(['done', 'now', 'pending']);
    expect(prog.querySelector('[data-step-state="pending"] [data-icon="stepPending"]')).not.toBeNull();
    // locked: Esc does not close it
    expect(prog.getAttribute('role')).toBe('dialog');
    fireEvent.keyDown(prog, { key: 'Escape' });
    expect(screen.getByTestId('dat-progress')).toBeInTheDocument();
    act(() => setDaten({ jobs: [{ job_id: 'job-1', op: 'delete', state: 'done', datasets: [W], outputs: [W], stage: null, done: 3, total: 3, unit: 'steps' }] }));
    await waitFor(() => expect(screen.queryByTestId('dat-progress')).toBeNull());
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('1 Episode gelöscht.', expect.anything()));
  });

  // V2-5: the toast is decided by the state AFTER the edit — the mockup's
  // „… Lade den Datensatz hoch, damit das Training die neue Version nutzt." with
  // „Jetzt hochladen" for a dataset whose hub copy is now behind.
  async function deleteFirstEpisode() {
    key('Delete');
    fireEvent.click(await screen.findByRole('button', { name: COPY.player.deleteMarked }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '1 Episode löschen' }));
    await waitFor(() => expect(commandCalls('edit')).toHaveLength(1));
  }
  const finishJob = () => act(() => setDaten({ jobs: [{ job_id: 'job-1', op: 'delete', state: 'done', datasets: [W], outputs: [W], stage: null, done: 3, total: 3, unit: 'steps' }] }));
  const actionToast = () => toast.success.mock.calls.find(([body]) => typeof body === 'function');
  const UPLOAD_TOAST = '1 Episode gelöscht. Lade den Datensatz hoch, damit das Training die neue Version nutzt.';

  it('an uploaded (current) dataset: the robot re-reads it as changed → the upload toast with „Jetzt hochladen" (V2-5)', async () => {
    await openPlayerView();
    await deleteFirstEpisode();
    // what the robot answers once the edit is done
    world.local = [local(W, { total_episodes: 2, meta_digest: 'd-after' })];
    world.sync = { [W]: { state: 'changed', head: `head-${W}` } };
    world.summaries[W] = summary(W, 2, { meta_digest: 'd-after' });
    finishJob();
    await waitFor(() => expect(actionToast()).toBeDefined());
    const [body, opts] = actionToast();
    const view = render(body({ id: 't1' }));
    expect(view.container.textContent).toContain(UPLOAD_TOAST);
    expect(within(view.container).getByRole('button', { name: COPY.toast.uploadAction })).toBeInTheDocument();
    expect(opts).toEqual({ duration: 8000 });
    fireEvent.click(within(view.container).getByRole('button', { name: COPY.toast.uploadAction }));
    const dlg = await screen.findByRole('dialog');
    await within(dlg).findByText(fill(COPY.upload.newTitle, { name: 'omx_f_w' }));
  });

  it('the re-read did not land: an uploaded dataset is still judged changed (an edit always changes it here)', async () => {
    await openPlayerView();
    await deleteFirstEpisode();
    const realFetch = global.fetch;
    global.fetch = vi.fn((url) => (String(url).includes('/library')
      ? Promise.resolve({ ok: false, status: 502, headers: { get: () => null }, json: () => Promise.reject(new Error('x')) })
      : realFetch(url)));
    finishJob();
    await waitFor(() => expect(actionToast()).toBeDefined());
    const view = render(actionToast()[0]({ id: 't2' }));
    expect(view.container.textContent).toContain(UPLOAD_TOAST);
  });

  async function plainToastAfter(after, head) {
    world.local = [local(W, { total_episodes: 2, meta_digest: 'd-after' })];
    world.sync = { [W]: { state: after, head } };
    world.summaries[W] = summary(W, 2, { meta_digest: 'd-after' });
    finishJob();
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('1 Episode gelöscht.', expect.anything()));
    expect(actionToast()).toBeUndefined();
  }

  it('only here (local): the plain toast', async () => {
    world.sync = { [W]: { state: 'local', head: null } };
    await openPlayerView();
    await deleteFirstEpisode();
    await plainToastAfter('local', null);
  });

  it('a newer hub copy edited anyway (now both sides changed): the plain toast — nothing invites a plain upload', async () => {
    world.sync = { [W]: { state: 'newer', head: 'h' } };
    const { store } = await openPlayerView();
    key('Delete');
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: COPY.newer.anyway }));
    await waitFor(() => expect(store.getState().editDataset.marks[W].indices).toEqual([0]));
    fireEvent.click(await screen.findByRole('button', { name: COPY.player.deleteMarked }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '1 Episode löschen' }));
    await waitFor(() => expect(commandCalls('edit')).toHaveLength(1));
    await plainToastAfter('conflict', 'h');
  });

  it('a failed job: its German message, the dialog closes', async () => {
    await openPlayerView();
    key('Delete');
    fireEvent.click(await screen.findByRole('button', { name: COPY.player.deleteMarked }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '1 Episode löschen' }));
    await waitFor(() => expect(commandCalls('edit')).toHaveLength(1));
    act(() => setDaten({ jobs: [{ job_id: 'job-1', op: 'delete', state: 'failed', datasets: [W], outputs: [], stage: null, done: 0, total: 3, unit: 'steps', code: 'verify_failed', message: 'Das Ergebnis der Bearbeitung war nicht in Ordnung. Der Datensatz wurde nicht verändert.' }] }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Das Ergebnis der Bearbeitung war nicht in Ordnung. Der Datensatz wurde nicht verändert.'));
    expect(screen.queryByTestId('dat-progress')).toBeNull();
  });

  it('stale (R-9): the robot\'s sentence, the dataset re-read, the marks dropped with marks.cleared', async () => {
    const STALE = 'Der Datensatz hat sich inzwischen geändert. Lade ihn neu und markiere erneut.';
    mockCommand = makeCommand({ edit: () => ({ ok: false, code: 'stale', message: STALE, result: {} }) });
    const { store } = await openPlayerView();
    key('Delete');
    await waitFor(() => expect(store.getState().editDataset.marks[W].indices).toEqual([0]));
    // another browser deleted an episode meanwhile: a new version
    world.local = [local(W, { total_episodes: 2, meta_digest: 'd-new' })];
    world.summaries[W] = summary(W, 2, { meta_digest: 'd-new' });
    fireEvent.click(screen.getByRole('button', { name: COPY.player.deleteMarked }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '1 Episode löschen' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(STALE));
    await waitFor(() => expect(document.querySelectorAll('.dat-ep-row').length).toBe(2));
    await waitFor(() => expect(store.getState().editDataset.marks[W]).toBeUndefined());
    expect(toast).toHaveBeenCalledWith(COPY.marks.cleared, expect.anything());
  });

  // V2-15: the banners and the newerWarn dialog name the counts and dates the
  // mockup shows — from the robot's hubstate (the library lists a local
  // dataset's hub entry without numbers); without it, the sentence without them.
  const HUB_AT = '2026-10-05T08:15:00Z';
  const withHubState = (state, n = 11) => {
    world.hubstate = (hid) => ({
      v: 1, id: hid, sync: { state, reason: null },
      hub: { exists: true, private: false, head: 'h', last_modified: HUB_AT, total_episodes: n, duration_s: 220 },
      local: { total_episodes: 3 }, new_repo_private: false,
    });
  };

  it('the newer banner: „(11 Episoden, <date>)"; the newerWarn dialog: the bold count, its date, „Hier sind es 3."', async () => {
    world.sync = { [W]: { state: 'newer', head: 'h' } };
    world.local = [local(W, { total_episodes: 3, display_name: 'Würfel' })];
    withHubState('newer');
    await openPlayerView();
    const banner = document.querySelector('[data-banner="newer"]');
    await waitFor(() => expect(banner.textContent).toContain(fill(COPY.banner.newer, { n: 11, date: fmtDate(HUB_AT) })));
    key('Delete');
    const dlg = await screen.findByRole('dialog');
    expect(dlg.textContent).toContain(`Auf Hugging Face liegt eine neuere Version von „Würfel“: 11 Episoden (${fmtDate(HUB_AT)}). Hier sind es 3.`);
    expect(within(dlg).getByText('11 Episoden').tagName).toBe('B');
  });

  it('the changed banner: „Das Training nutzt noch die alte Version (10 Episoden)."', async () => {
    world.sync = { [W]: { state: 'changed', head: 'h' } };
    withHubState('changed', 10);
    await openPlayerView();
    await waitFor(() => expect(document.querySelector('[data-banner="changed"]').textContent).toContain(fill(COPY.banner.changed, { n: 10 })));
  });

  it('hubstate unavailable: the banner without numbers, never „–"', async () => {
    world.sync = { [W]: { state: 'newer', head: 'h' } };
    world.hubstate = () => ({ v: 1, sync: { state: 'unknown', reason: 'unreachable' }, hub: null, local: {} });
    await openPlayerView();
    const banner = document.querySelector('[data-banner="newer"]');
    await waitFor(() => expect(requestsHubstate()).toBeGreaterThan(0));
    await act(async () => { await Promise.resolve(); });
    expect(banner.textContent).toContain(COPY.banner.newerShort);
    expect(banner.textContent).not.toMatch(/\(–|– Episoden|Episoden, –/);
  });

  it('a NEWER hub copy: marking asks first („Trotzdem hier bearbeiten" goes on, once)', async () => {
    world.sync = { [W]: { state: 'newer', head: 'h' } };
    const { store } = await openPlayerView();
    expect(document.querySelector('[data-banner="newer"]')).not.toBeNull();
    key('Delete');
    const dlg = await screen.findByRole('dialog');
    expect(dlg.textContent).toContain(COPY.newer.title);
    fireEvent.click(within(dlg).getByRole('button', { name: COPY.newer.anyway }));
    await waitFor(() => expect(store.getState().editDataset.marks[W].indices).toEqual([0]));
    key('Delete');
    await waitFor(() => expect(store.getState().editDataset.marks[W].indices).toEqual([]));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('a partner\'s dataset: „Hochladen" in the tools is aria-disabled and explains itself', async () => {
    const P = `${PARTNER}/omx_f_p`;
    world.local = [local(P)];
    world.sync = { [P]: { state: 'unknown', reason: 'not_visible' } };
    world.summaries[P] = summary(P, 2);
    const store = makeStore();
    store.dispatch(openPlayer(P));
    render(wrap(store, <DatenPage />));
    await screen.findByText(COPY.player.eyebrow);
    const up = within(screen.getByRole('toolbar', { name: COPY.tools.label })).getByRole('button', { name: new RegExp(COPY.tools.upload) });
    expect(up.getAttribute('aria-disabled')).toBe('true');
    fireEvent.click(up);
    expect(toast).toHaveBeenCalledWith('Nur Max Weber kann diesen Datensatz hochladen: Er gehört zum Hugging-Face-Konto von Max Weber.', expect.anything());
  });

  it('„Zusammenführen" in the tools hands the dataset to the library\'s merge mode (§G13)', async () => {
    await openPlayerView();
    fireEvent.click(within(screen.getByRole('toolbar', { name: COPY.tools.label })).getByRole('button', { name: new RegExp(COPY.tools.merge) }));
    await screen.findByRole('region', { name: COPY.merge.title });
    expect(document.querySelector(`[data-id="${W}"] .dat-selbox`).getAttribute('aria-pressed')).toBe('true');
  });
});

describe('the render invariant (§F2, §G12, AC-B3)', () => {
  it('100 driver frames: the time and frame move, PlayerView, Stage, Transport, JointCharts and EpisodeList render 0 times', async () => {
    setRenderProbeForTests(true);
    await openPlayerView();
    await waitFor(() => expect(sceneVideo()).not.toBeNull());
    await waitFor(() => expect(sceneVideo().__rvfc).toBeTruthy());
    key('Space');
    await screen.findByRole('button', { name: COPY.player.pause });
    // let the effects after play settle
    await act(async () => { await Promise.resolve(); });
    const before = probeCounts();
    for (let k = 1; k <= 100; k += 1) frame(sceneVideo(), k % 50, 1000 + k * 16);
    expect(frameText()).toBe(`Bild ${(100 % 50) + 1} / 60`);
    frame(sceneVideo(), 37, 9999);
    expect(frameText()).toBe('Bild 38 / 60');
    const counts = probeCounts();
    ['PlayerView', 'Stage', 'Transport', 'JointCharts', 'EpisodeList'].forEach((name) => {
      expect(`${name}: ${counts[name] - before[name]}`).toBe(`${name}: 0`);
    });
  });

  it('daten_state messages for other datasets do not re-render the player', async () => {
    setRenderProbeForTests(true);
    await openPlayerView();
    await act(async () => { await Promise.resolve(); });
    act(() => setDaten({ busy: [] }));
    await act(async () => { await Promise.resolve(); });
    const before = probeCounts();
    for (let k = 0; k < 10; k += 1) {
      act(() => setDaten({ busy: [{ id: `${OWN}/omx_f_other`, kind: 'upload' }], seq: k }));
    }
    const counts = probeCounts();
    ['PlayerView', 'Stage', 'Transport', 'JointCharts', 'EpisodeList'].forEach((name) => {
      expect(`${name}: ${counts[name] - before[name]}`).toBe(`${name}: 0`);
    });
  });
});
