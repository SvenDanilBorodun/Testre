// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Daten 2.0 pure models (spec §J.B step 1): the episode-number parser, the
// player's frame math, the chart data, the German formatting, the hint
// sentences, the sync badge and the merge checks.

import { describe, expect, it } from 'vitest';
import COPY from '../datenCopy';
import * as CONTRACT from '../datenContract';
import { everyFifth, parseEpisodeNumbers, toRangeText } from '../model/episodeNumbers';
import {
  ariaValueText, clampFrame, frameAt, frameFraction, frameLabel, idxFromCurrentTime, idxFromMediaTime,
  lastFrame, scrubTarget, seekTime, shiftTarget, stepTarget, timeLabel,
} from '../model/playerClock';
import {
  MAX_CHART_POINTS, columnDegrees, downsampleMinMax, jointChart, spansZero, toDegrees, yDomain,
} from '../model/chartData';
import {
  fill, fillParts, fmtBytes, fmtBytesProgress, fmtDate, fmtDay, fmtFps, fmtGB, fmtNum, fmtTime, joinAnd, plural,
} from '../model/format';
import { hintBand, hintText, hintTick } from '../model/hintText';
import { syncBadge } from '../model/syncBadge';
import { mergeChecks } from '../model/mergeChecks';
import {
  cameraLabel, codecName, driverCamera, jointLabel, nameFromRepo, orderCameras, robotName, taskNameOf,
} from '../model/labels';

describe('the shared contract (§J.2)', () => {
  it('carries the codes and states the page switches on', () => {
    expect(CONTRACT.HTTP_PORT).toBe(8095);
    expect(CONTRACT.API_PREFIX).toBe('/daten-api/v1');
    expect(CONTRACT.SYNC_STATES).toEqual(['local', 'online', 'current', 'changed', 'newer', 'conflict', 'unknown']);
    expect(CONTRACT.JOB_FAIL_CODES).toContain('stale');
    expect(Object.isFrozen(CONTRACT.ACTIONS)).toBe(true);
  });
});

describe('parseEpisodeNumbers (D9, §G6)', () => {
  it('reads numbers and ranges, 1-based in, 0-based out, sorted and unique', () => {
    expect(parseEpisodeNumbers('3, 5, 10-15', 20)).toEqual({ indices: [2, 4, 9, 10, 11, 12, 13, 14], errors: [] });
    expect(parseEpisodeNumbers('5 5 3;3', 20).indices).toEqual([2, 4]);
    expect(parseEpisodeNumbers('', 20)).toEqual({ indices: [], errors: [] });
  });

  it('takes an en or em dash and spaces around the dash', () => {
    expect(parseEpisodeNumbers('2–4', 10).indices).toEqual([1, 2, 3]);
    expect(parseEpisodeNumbers('2 — 4', 10).indices).toEqual([1, 2, 3]);
    expect(parseEpisodeNumbers('2 - 4, 6', 10).indices).toEqual([1, 2, 3, 5]);
  });

  it('swaps a reversed range', () => {
    expect(parseEpisodeNumbers('6-4', 10).indices).toEqual([3, 4, 5]);
  });

  it('one German error per bad token, the good ones still counted', () => {
    const r = parseEpisodeNumbers('x, 3, 0, 12-14', 12);
    expect(r.indices).toEqual([2]);
    expect(r.errors).toEqual([
      '„x“ ist keine Episodennummer.',
      '0: Es gibt nur Episode 1 bis 12.',
      '12-14: Es gibt nur Episode 1 bis 12.',
    ]);
  });

  it('„0-1000000" with max 12 is ONE error, never expanded (< 5 ms)', () => {
    const t0 = performance.now();
    const r = parseEpisodeNumbers('0-1000000', 12);
    const ms = performance.now() - t0;
    expect(r.errors).toEqual(['0-1000000: Es gibt nur Episode 1 bis 12.']);
    expect(r.indices).toEqual([]);
    expect(ms).toBeLessThan(5);
  });

  it('a number past nine digits is not a number', () => {
    expect(parseEpisodeNumbers('1234567890', 12).errors).toEqual(['„1234567890“ ist keine Episodennummer.']);
  });

  it('toRangeText collapses runs, 1-based', () => {
    expect(toRangeText([2, 4, 9, 10, 11, 12, 13, 14])).toBe('3, 5, 10-15');
    expect(toRangeText([1, 3, 4])).toBe('2, 4-5');
    expect(toRangeText([])).toBe('');
    expect(toRangeText([3, 3, 1])).toBe('2, 4');
  });

  it('„Jede 5. Episode" picks 5, 10, 15 …', () => {
    expect(everyFifth(12)).toEqual([4, 9]);
    expect(everyFifth(4)).toEqual([]);
  });
});

describe('playerClock (§F2)', () => {
  it('seeks to the middle of the frame', () => {
    expect(seekTime(0, 30)).toBeCloseTo(0.5 / 30, 12);
    expect(seekTime(10, 30)).toBeCloseTo(10.5 / 30, 12);
  });

  it('reads the frame from mediaTime by rounding, clamped into the episode', () => {
    expect(idxFromMediaTime(10 / 30, 30, 600)).toBe(10);
    expect(idxFromMediaTime(10.4 / 30, 30, 600)).toBe(10);
    expect(idxFromMediaTime(10.6 / 30, 30, 600)).toBe(11);
    expect(idxFromMediaTime(-1, 30, 600)).toBe(0);
    expect(idxFromMediaTime(100, 30, 600)).toBe(599);
  });

  it('the rAF fallback floors currentTime with a small epsilon', () => {
    expect(idxFromCurrentTime(10 / 30, 30, 600)).toBe(10);
    expect(idxFromCurrentTime(10.99 / 30, 30, 600)).toBe(10);
    expect(idxFromCurrentTime(1000, 30, 600)).toBe(599);
  });

  it('the end is L-1; steps go by index and clamp', () => {
    expect(lastFrame(600)).toBe(599);
    expect(lastFrame(0)).toBe(0);
    expect(stepTarget(599, 1, 600)).toBe(599);
    expect(stepTarget(0, -1, 600)).toBe(0);
    expect(stepTarget(10, 1, 600)).toBe(11);
    expect(shiftTarget(10, 5, 30, 600)).toBe(160);
    expect(shiftTarget(10, -5, 30, 600)).toBe(0);
    expect(clampFrame(1e9, 600)).toBe(599);
  });

  it('the scrubber maps fractions to frames and back', () => {
    expect(scrubTarget(0, 600)).toBe(0);
    expect(scrubTarget(1, 600)).toBe(599);
    expect(scrubTarget(0.5, 601)).toBe(300);
    expect(frameFraction(599, 600)).toBe(1);
    expect(frameFraction(0, 1)).toBe(0);
  });

  it('labels: time, „Bild i / n", aria-valuetext; hint seconds → frame', () => {
    expect(timeLabel(102, 30, 600)).toEqual(['0:03,4', '0:20,0']);
    expect(frameLabel(0, 600)).toBe('Bild 1 / 600');
    expect(ariaValueText(102, 30, 600)).toBe('0:03,4 von 0:20,0');
    expect(frameAt(7.3, 30, 600)).toBe(219);
  });
});

describe('chartData (§F5)', () => {
  it('converts radians to degrees, the gripper too', () => {
    expect(toDegrees(Math.PI)).toBeCloseTo(180, 9);
    expect(columnDegrees([[0, Math.PI / 2], [1, -Math.PI]], 1)).toEqual([90, -180]);
  });

  it('keeps a short series whole and caps a long one at 600 points, keeping min and max per bucket', () => {
    expect(downsampleMinMax([1, 2, 3]).map((p) => p.v)).toEqual([1, 2, 3]);
    const n = 6000;
    const values = Array.from({ length: n }, (_, i) => Math.sin(i / 50));
    values[3333] = 99; // a spike
    values[4444] = -99;
    const pts = downsampleMinMax(values);
    expect(pts.length).toBeLessThanOrEqual(MAX_CHART_POINTS);
    expect(pts.some((p) => p.i === 3333 && p.v === 99)).toBe(true);
    expect(pts.some((p) => p.i === 4444 && p.v === -99)).toBe(true);
    // frame order
    for (let i = 1; i < pts.length; i += 1) expect(pts[i].i).toBeGreaterThan(pts[i - 1].i);
  });

  it('pads the y range to at least 10°, and knows when it spans zero', () => {
    const [lo, hi] = yDomain([1, 2], [3]);
    expect(hi - lo).toBeGreaterThanOrEqual(10);
    expect((hi + lo) / 2).toBeCloseTo(2, 9);
    expect(spansZero(yDomain([-20, 20]))).toBe(true);
    expect(spansZero(yDomain([5, 30]))).toBe(false);
  });

  it('jointChart builds the follower and leader series of one joint', () => {
    const data = { state: [[0], [0.1], [0.2]], action: [[0], [0.2], [0.4]] };
    const c = jointChart(data, 0);
    expect(c.follower.map((p) => p.i)).toEqual([0, 1, 2]);
    expect(c.leader[2].v).toBeCloseTo(toDegrees(0.4), 9);
  });
});

describe('format (§J.6 numbers)', () => {
  it('a decimal comma and m:ss,d', () => {
    expect(fmtNum(3.14159, 1)).toBe('3,1');
    expect(fmtTime(3.4)).toBe('0:03,4');
    expect(fmtTime(65.99)).toBe('1:05,9');
    expect(fmtTime(240, false)).toBe('4:00');
    expect(fmtTime(-3)).toBe('0:00,0');
    expect(fmtTime(19.99999999)).toBe('0:19,9');
  });

  it('sizes in MB and GB', () => {
    expect(fmtBytes(312e6)).toBe('312 MB');
    expect(fmtBytes(1.08e9)).toBe('1,1 GB');
    expect(fmtBytes(1)).toBe('1 MB');
    expect(fmtBytes(0)).toBe('0 MB');
    expect(fmtGB(23.4e9)).toBe('23,4 GB');
  });

  it('a progress pair: one unit, done and percent rounded down, never past the total (V2-14)', () => {
    expect(fmtBytesProgress(5e3, 4e6)).toEqual({ done: '0,0 MB', total: '4,0 MB', pct: 0 });
    expect(fmtBytesProgress(3.66e6, 4e6)).toEqual({ done: '3,6 MB', total: '4,0 MB', pct: 91 });
    expect(fmtBytesProgress(120e6, 540e6)).toEqual({ done: '120 MB', total: '540 MB', pct: 22 });
    expect(fmtBytesProgress(0.5e9, 1.08e9)).toEqual({ done: '0,5 GB', total: '1,1 GB', pct: 46 });
    expect(fmtBytesProgress(12e9, 12e9)).toEqual({ done: '12 GB', total: '12 GB', pct: 100 });
    expect(fmtBytesProgress(9e9, 4e6)).toEqual({ done: '4,0 MB', total: '4,0 MB', pct: 100 });
    expect(fmtBytesProgress(1, 0)).toEqual({ done: '–', total: '–', pct: 0 });
  });

  it('dates „3. Okt., 14:12" and „30. Sep." in local time', () => {
    expect(fmtDate(new Date(2026, 9, 3, 14, 12).toISOString())).toBe('3. Okt., 14:12');
    expect(fmtDay(new Date(2026, 8, 30, 8, 0))).toBe('30. Sep.');
    expect(fmtDate(null)).toBe('–');
    expect(fmtDate('nonsense')).toBe('–');
  });

  it('fill fills a whole-literal template; unknown placeholders stay', () => {
    expect(fill(COPY.merge.check.videoOk, { codec: 'H.264', w: 640, h: 480 })).toBe('Gleiches Videoformat: H.264 · 640 × 480');
    expect(fill('{a} und {b}', { a: 1 })).toBe('1 und {b}');
    expect(fillParts('a {x} b', { x: { el: 1 } })).toEqual(['a ', { el: 1 }, ' b']);
  });

  it('plural picks one or many', () => {
    expect(plural(1, COPY.count.episodeOne, COPY.count.episodeMany)).toBe('1 Episode');
    expect(plural(3, COPY.count.episodeOne, COPY.count.episodeMany)).toBe('3 Episoden');
    expect(plural(0, COPY.count.episodeOne, COPY.count.episodeMany)).toBe('0 Episoden');
  });

  it('joinAnd and fmtFps', () => {
    expect(joinAnd(['15', '30'], COPY.count.listAnd)).toBe('15 und 30');
    expect(joinAnd(['15', '25', '30'], COPY.count.listAnd)).toBe('15, 25 und 30');
    expect(fmtFps(30)).toBe('30');
    expect(fmtFps(29.97)).toBe('29,97');
  });
});

describe('hintText (§F6, every type)', () => {
  const ctx = { jointNames: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'], durationS: 20 };
  it('still — the N4 sentence with the joint range', () => {
    const h = hintText({ type: 'still', value_deg: 6.4 }, ctx);
    expect(h.title).toBe('Arm bewegt sich kaum');
    expect(h.text).toBe('Kein Gelenk bewegt sich mehr als 6°. Vielleicht wurde die Aufgabe nicht ausgeführt.');
  });
  it('idle_end and idle_start', () => {
    expect(hintText({ type: 'idle_end', from_s: 16.2, to_s: 20 }, ctx)).toMatchObject({
      title: 'Lange Pause am Ende',
      text: '3,8 s ohne Bewegung (ab 0:16,2). Das Modell lernt daraus, still zu stehen. Lösche die Episode oder nimm sie neu auf.',
      t: 16.2,
    });
    expect(hintText({ type: 'idle_start', from_s: 0, to_s: 3.5 }, ctx)).toMatchObject({
      title: 'Lange Pause am Anfang', text: '3,5 s ohne Bewegung bis 0:03,5.', t: 0,
    });
  });
  it('no_grasp and lag (the joint named by the table)', () => {
    expect(hintText({ type: 'no_grasp', value_deg: 3 }, ctx).text).toBe('In dieser Episode greift der Arm nichts. War das Absicht?');
    expect(hintText({ type: 'lag', at_s: 7.3, joint: 1, value_deg: 26.4 }, ctx)).toMatchObject({
      title: 'Der Follower-Arm kommt nicht hinterher',
      text: 'Gelenk 2 liegt bis zu 26° hinter dem Leader-Arm (bei 0:07,3). Bewege den Leader-Arm ruhiger.',
      t: 7.3,
    });
  });
  it('short and long', () => {
    expect(hintText({ type: 'short', ratio: 0.31, median_s: 20 }, { ...ctx, durationS: 6.2 }).text)
      .toBe('Nur 6,2 s statt meist 20,0 s. Vielleicht wurde zu früh gespeichert.');
    expect(hintText({ type: 'long', ratio: 1.7, median_s: 20 }, { ...ctx, durationS: 34 }))
      .toMatchObject({ title: 'Viel länger als die anderen', text: '34,0 s statt meist 20,0 s. Prüfe, ob am Ende noch etwas passiert.' });
  });
  it('bands for idle hints, ticks for lag', () => {
    expect(hintBand({ type: 'idle_end', from_s: 16, to_s: 20 }, 20)).toEqual({ from: 16, to: 20 });
    expect(hintBand({ type: 'lag', at_s: 3 }, 20)).toBeNull();
    expect(hintTick({ type: 'lag', at_s: 3 })).toBe(3);
    expect(hintTick({ type: 'still' })).toBeNull();
  });
});

describe('syncBadge (all seven states, every unknown reason)', () => {
  it.each([
    ['local', 'Nur hier', 'hardDrive', 'neutral'],
    ['online', 'Nur online', 'cloud', 'sky'],
    ['current', 'Aktuell', 'cloudSynced', 'ok'],
    ['changed', 'Hier geändert – nicht hochgeladen', 'cloudUpload', 'warn'],
    ['newer', 'Online neuer', 'cloudDownload', 'sky'],
    ['conflict', 'Hier und online verschieden', 'syncConflict', 'danger'],
    ['unknown', 'Online-Stand unbekannt', 'syncUnknown', 'muted'],
  ])('%s', (state, label, icon, tone) => {
    const b = syncBadge({ state, reason: 'not_asked' });
    expect(b).toMatchObject({ key: state, label, icon, tone });
  });

  it('unknown: the link only for not_asked and unreachable; each its own tip', () => {
    const notAsked = syncBadge({ state: 'unknown', reason: 'not_asked' });
    const unreachable = syncBadge({ state: 'unknown', reason: 'unreachable' });
    const notVisible = syncBadge({ state: 'unknown', reason: 'not_visible', ownerName: 'Max Weber' });
    expect(notAsked.link).toBe(true);
    expect(unreachable.link).toBe(true);
    expect(notVisible.link).toBe(false);
    expect(notAsked.tip).toBe(COPY.sync.unknownTip.not_asked);
    expect(unreachable.tip).toBe(COPY.sync.unknownTip.unreachable);
    expect(notVisible.tip).toBe('Ob dieser Datensatz auf Hugging Face liegt, kann nur Max Weber sehen.');
    expect(syncBadge({ state: 'current' }).link).toBe(false);
  });

  it('an overlay wins over the state', () => {
    expect(syncBadge({ state: 'current', overlay: 'record' })).toMatchObject({ label: 'Wird gerade aufgenommen', icon: 'liveRecording', tone: 'rec' });
    expect(syncBadge({ state: 'current', overlay: 'upload' })).toMatchObject({ label: 'Wird hochgeladen', spin: true });
    expect(syncBadge({ state: 'current', overlay: 'keep_both' }).label).toBe('Wird zusammengeführt');
    expect(syncBadge({ state: 'current', overlay: 'refreshing' }).label).toBe('Wird aktualisiert …');
  });

  it('an unknown state reads as unknown', () => {
    expect(syncBadge({ state: 'whatever' }).key).toBe('unknown');
  });
});

describe('mergeChecks (§D5, each check failing alone)', () => {
  const entry = (patch = {}) => ({
    id: 'lena/omx_f_a',
    codebase_version: 'v3.0',
    robot_type: 'omx_f',
    fps: 30,
    cameras: [
      { index: 0, key: 'observation.images.gripper', name: 'gripper', codec: 'h264', pix_fmt: 'yuv420p', width: 640, height: 480, fps: 30 },
      { index: 1, key: 'observation.images.scene', name: 'scene', codec: 'h264', pix_fmt: 'yuv420p', width: 640, height: 480, fps: 30 },
    ],
    joints: {
      state: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'],
      action: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'],
    },
    stat_names: { action: ['count', 'max', 'mean', 'min', 'q01'], 'observation.state': ['count', 'max', 'mean', 'min', 'q01'] },
    ...patch,
  });

  it('two compatible datasets: every row green, the mockup texts', () => {
    const r = mergeChecks([entry(), entry({ id: 'max/omx_f_a' })]);
    expect(r.ok).toBe(true);
    expect(r.checks.map((c) => c.text)).toEqual([
      'Gleicher Roboter: OpenMANIPULATOR-X',
      'Gleiche Bildrate: 30 Bilder/s',
      'Gleiche Kameras: Greifer-Kamera, Szenen-Kamera',
      'Gleiche Gelenke (5 Gelenke + Greifer)',
      'Gleiches Videoformat: H.264 · 640 × 480',
      'Gleiche Statistiken',
    ]);
  });

  it('fewer than two: one row', () => {
    expect(mergeChecks([entry()])).toEqual({ ok: false, checks: [{ id: 'min', ok: false, text: 'Mindestens zwei Datensätze auswählen' }] });
  });

  it.each([
    ['version', { codebase_version: 'v2.1' }, 'Unterschiedliches Datensatz-Format – lässt sich nicht zusammenführen'],
    ['robot', { robot_type: 'edu6_studio' }, 'Unterschiedliche Roboter – lässt sich nicht zusammenführen'],
    ['fps', { fps: 15 }, 'Unterschiedliche Bildrate (15 und 30 Bilder/s) – lässt sich nicht zusammenführen'],
    ['cameras', {
      cameras: [{ index: 0, key: 'observation.images.scene', name: 'scene', codec: 'h264', pix_fmt: 'yuv420p', width: 640, height: 480, fps: 30 }],
    }, 'Unterschiedliche Kameras – lässt sich nicht zusammenführen'],
    ['joints', { joints: { state: ['a', 'b'], action: ['a', 'b'] } }, 'Unterschiedliche Gelenke – lässt sich nicht zusammenführen'],
    ['video', {
      cameras: [
        { index: 0, key: 'observation.images.gripper', name: 'gripper', codec: 'av1', pix_fmt: 'yuv420p', width: 640, height: 480, fps: 30 },
        { index: 1, key: 'observation.images.scene', name: 'scene', codec: 'h264', pix_fmt: 'yuv420p', width: 640, height: 480, fps: 30 },
      ],
    }, 'Unterschiedliches Videoformat – lässt sich nicht zusammenführen'],
    ['stats', { stat_names: { action: ['count', 'max', 'mean', 'min'], 'observation.state': ['count', 'max', 'mean', 'min'] } },
      'Unterschiedliche Statistiken – lässt sich nicht zusammenführen'],
  ])('%s fails alone', (id, patch, text) => {
    const r = mergeChecks([entry(), entry({ id: 'x/y', ...patch })]);
    expect(r.ok).toBe(false);
    const failing = r.checks.filter((c) => !c.ok);
    expect(failing.map((c) => c.id)).toEqual([id]);
    expect(failing[0].text).toBe(text);
  });
});

describe('labels (§J.6 tables, §F1 order)', () => {
  it('robots, codecs and joints with their fallbacks', () => {
    expect(robotName('omx_f')).toBe('OpenMANIPULATOR-X');
    expect(robotName('edu6_studio')).toBe('Edu:6');
    expect(robotName('so100')).toBe('so100');
    expect(codecName('libx264')).toBe('H.264');
    expect(codecName('libsvtav1')).toBe('AV1');
    expect(codecName('vp9')).toBe('VP9');
    expect(jointLabel('joint5')).toBe('Gelenk 5 · Hand drehen');
    expect(jointLabel('wrist_roll')).toBe('wrist_roll');
  });

  it('nameFromRepo: the robot prefix and separators gone, the first letter up', () => {
    expect(nameFromRepo('lehrer-mueller/omx_f_wuerfel-demo', 'omx_f')).toBe('Wuerfel demo');
    expect(nameFromRepo('lerobot/so100_test', 'omx_f')).toBe('So100 test');
    expect(nameFromRepo('a/omx_f_', 'omx_f')).toBe('');
  });

  // V2-11: a name to build on („… gesamt", „… Teil 2") never repeats the
  // folder's robot prefix — „omx_f_deckel gesamt" became omx_f_omx_f_deckel-gesamt.
  it('taskNameOf: the display name, else the task part of the folder name', () => {
    expect(taskNameOf({ id: 'lena/omx_f_deckel', name: 'omx_f_deckel', display_name: 'Deckel auflegen' }, 'omx_f')).toBe('Deckel auflegen');
    expect(taskNameOf({ id: 'lena/omx_f_deckel', name: 'omx_f_deckel', display_name: null }, 'omx_f')).toBe('Deckel');
    expect(taskNameOf({ id: 'lena/omx_f_Wuerfel-in-die-Schale', name: 'omx_f_Wuerfel-in-die-Schale', display_name: '  ' }, 'omx_f'))
      .toBe('Wuerfel in die Schale');
    // the entry's own robot type first (a dataset recorded on another profile)
    expect(taskNameOf({ id: 'lena/edu6_studio_becher', name: 'edu6_studio_becher', robot_type: 'edu6_studio' }, 'omx_f')).toBe('Becher');
    // no prefix to strip: the name as it is
    expect(taskNameOf({ id: 'lena/meine-daten', name: 'meine-daten' }, 'omx_f')).toBe('Meine daten');
    expect(taskNameOf({ id: 'lena/omx_f_', name: 'omx_f_' }, 'omx_f')).toBe('omx_f_');
    expect(taskNameOf(null, 'omx_f')).toBe('');
  });

  it('cameras: Greifer, Szene, then others by key; the scene drives', () => {
    const cams = [
      { index: 0, key: 'observation.images.top', name: 'top' },
      { index: 1, key: 'observation.images.scene', name: 'scene' },
      { index: 2, key: 'observation.images.gripper', name: 'gripper' },
    ];
    expect(orderCameras(cams).map((c) => c.index)).toEqual([2, 1, 0]);
    expect(orderCameras(cams).map(cameraLabel)).toEqual(['Greifer-Kamera', 'Szenen-Kamera', 'top']);
    expect(driverCamera(cams).index).toBe(1);
    expect(driverCamera([{ index: 0, name: 'gripper' }]).index).toBe(0);
  });
});
