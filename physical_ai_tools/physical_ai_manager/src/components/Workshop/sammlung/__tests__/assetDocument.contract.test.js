/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/*
 * ONE contract, TWO documents. The Sammlung drawer, the Vormachen overlay and
 * TeachHost talk to „the document" only through an asset document
 * (`assetDoc`): a Blockly workspace (sammlung/assetDocument.js) or a Python /
 * Java program (code/codeAssetDocument.js). The same assertions run against
 * both, over two documents that say the same thing — a recording used once, a
 * Ziel used once and once switched off (a disabled block / a commented-out
 * call), a Position nobody uses, a Ziel the program defines itself.
 */

import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerTrajectoryBlocks } from '../../blocks/trajectories';
import { registerDestinationBlocks } from '../../blocks/destinations';
import { registerMotionBlocks } from '../../blocks/motion';
import {
  createDetachedDestinationStore,
  getDestinationStore,
  registerDestinationSerializer,
} from '../destinationStore';
import { createBlocklyAssetDocument } from '../assetDocument';
import { createCodeAssetDocument, CODE_SIDEBAR_WIDTH_PX } from '../../code/codeAssetDocument';
import { CODE_DE, formatCode } from '../../code/codeMessagesDe';

const PIN = { name: 'Ablage', kind: 'pin', source: 'camera', x: 0.2, y: 0, z: 0 };
const POSE = { name: 'Hoch', kind: 'pose', source: 'capture', x: 0.1, y: 0.1, z: 0.15 };
const SNAPSHOT = {
  capabilities: { hardware: true, drawer: true, preview: true },
  robotType: 'omx_f',
  trajectories: {
    status: 'ready',
    items: [{ id: 't1', name: 'Winken', created_at: '2026-09-27T10:00:00Z', robot_profile: 'omx_f' }],
  },
  lastPreviewResult: {},
  variableValues: {},
};

let workspaces = [];

beforeAll(() => {
  Blockly.setLocale(De);
  registerTrajectoryBlocks();
  registerDestinationBlocks();
  registerMotionBlocks();
  registerDestinationSerializer();
});

afterEach(() => {
  workspaces.forEach((ws) => ws.dispose());
  workspaces = [];
});

const moveTo = (name, extra = {}) => ({
  type: 'edubotics_move_to',
  ...extra,
  inputs: { DESTINATION: { block: { type: 'edubotics_destination_ref', fields: { NAME: name } } } },
});

function blocklyDoc({ empty = false } = {}) {
  const ws = new Blockly.Workspace();
  workspaces.push(ws);
  if (!empty) {
    Blockly.serialization.workspaces.load({
      blocks: {
        languageVersion: 0,
        blocks: [
          { type: 'edubotics_replay_trajectory', x: 0, y: 0, fields: { NAME: 'Winken' } },
          { ...moveTo('Ablage'), x: 0, y: 100 },
          { ...moveTo('Ablage', { enabled: false }), x: 0, y: 200 },
          {
            type: 'edubotics_destination_pin', x: 0, y: 300, fields: { NAME: 'Mitte' },
          },
        ],
      },
      variables: [{ name: 'punkte', id: 'v_punkte' }],
    }, ws);
  }
  const store = getDestinationStore(ws);
  if (!empty) {
    store.add(PIN);
    store.add(POSE);
  }
  const doc = createBlocklyAssetDocument(ws);
  return {
    doc,
    store,
    refNames: () => ws.getAllBlocks(false)
      .filter((b) => b.type === 'edubotics_destination_ref').map((b) => b.getFieldValue('NAME')),
    replayNames: () => ws.getAllBlocks(false)
      .filter((b) => b.type === 'edubotics_replay_trajectory').map((b) => b.getFieldValue('NAME')),
    // The PROGRAM (its blocks), not the document's Ziele serializer.
    programText: () => JSON.stringify(Blockly.serialization.workspaces.save(ws).blocks || {}),
  };
}

const CODE = [
  'import robot',
  'punkte = 0',
  'robot.replay("Winken")',
  'robot.move_to("Ablage")',
  '# robot.move_to("Ablage")',
  'robot.pin("Mitte", 0.1, 0.0, 0.0)',
  '',
].join('\n');

// The contract's insertions need a spot the student chose (owner decision
// R3-O4): the cursor below `import robot`.
function codeDoc({ empty = false, cursor = { file: 'main.py', line: 1 } } = {}) {
  let files = { 'main.py': empty ? 'import robot\n' : CODE };
  const store = createDetachedDestinationStore([]);
  if (!empty) {
    store.add(PIN);
    store.add(POSE);
  }
  const reveals = [];
  const doc = createCodeAssetDocument({
    language: 'python',
    store,
    getFiles: () => files,
    applyFiles: (next) => { files = next; },
    requestReveal: (at) => reveals.push(at),
    getCursor: () => cursor,
  });
  return {
    doc,
    store,
    reveals,
    refNames: () => [...files['main.py'].matchAll(/move_to\("([^"]*)"\)/g)].map((m) => m[1]),
    replayNames: () => [...files['main.py'].matchAll(/replay\("([^"]*)"\)/g)].map((m) => m[1]),
    programText: () => files['main.py'],
  };
}

describe.each([
  ['blockly', blocklyDoc],
  ['code', codeDoc],
])('the asset document contract — %s', (kind, make) => {
  it('says which kind it is and where the drawer sits', () => {
    const { doc } = make();
    expect(doc.kind).toBe(kind);
    expect(typeof doc.anchorLeft()).toBe('number');
    expect(doc.getStore()).toBeTruthy();
  });

  it('builds the SAME index shape: counts, usage chips, disabled uses', () => {
    const { doc } = make();
    const index = doc.buildIndex(SNAPSHOT);
    expect(index.counts).toMatchObject({ aufnahmen: 1, ziele: 1, positionen: 1 });
    expect(index.recordings[0]).toMatchObject({ assetName: 'Winken', usage: { enabled: 1, disabled: 0 } });
    expect(index.pins[0]).toMatchObject({ assetName: 'Ablage', usage: { enabled: 1, disabled: 1 } });
    expect(index.poses[0]).toMatchObject({ assetName: 'Hoch', usage: { enabled: 0, disabled: 0 } });
    expect(index.programPins.map((p) => p.assetName)).toEqual(['Mitte']);
    expect(index.variables.map((v) => v.assetName)).toEqual(['punkte']);
  });

  it('lists „Benutzt in" rows with ids the document can jump to, the switched-off one marked', () => {
    const { doc } = make();
    const rows = doc.usageRows('pin', 'Ablage');
    expect(rows).toHaveLength(2);
    expect(rows.map((r) => r.disabled).sort()).toEqual([false, true]);
    for (const r of rows) {
      expect(typeof r.id).toBe('string');
      expect(typeof r.label).toBe('string');
      expect(r.label.length).toBeGreaterThan(0);
      expect(() => doc.jump(r.id)).not.toThrow();
    }
    expect(doc.usageRows('recording', 'Winken')).toHaveLength(1);
    expect(doc.usageRows('recording', 'Gibtsnicht')).toEqual([]);
  });

  it('takes the store names AND the program-defined names as taken', () => {
    const { doc } = make();
    expect(doc.takenPlaceNames().sort()).toEqual(['Ablage', 'Hoch', 'Mitte']);
  });

  it('renames a Ziel and every reference in one step, never a definition', () => {
    const { doc, store, refNames } = make();
    const id = store.getByName('Ablage').id;
    const result = doc.renamePlace(id, 'Tisch');
    expect(result).toMatchObject({ ok: true, oldName: 'Ablage' });
    expect(store.getByName('Tisch')).toBeTruthy();
    expect(refNames()).toEqual(expect.arrayContaining(['Tisch']));
    expect(refNames()).not.toContain('Ablage');
    expect(doc.takenPlaceNames()).toContain('Mitte');
    // A refused rename changes nothing.
    expect(doc.renamePlace(id, 'Hoch')).toMatchObject({ ok: false });
    expect(store.getByName('Tisch')).toBeTruthy();
  });

  it('rewrites a recording’s references through its rename target', async () => {
    const { doc, replayNames } = make();
    const target = doc.renameRecordingTarget();
    if (target.rewrite) target.rewrite('Winken', 'Gruss');
    else {
      const { rewriteReplayBlocks } = await import('../assetCommands');
      rewriteReplayBlocks(target.workspace, 'Winken', 'Gruss');
    }
    expect(replayNames()).toEqual(['Gruss']);
  });

  it('deletes a Ziel without touching the program, and restores it', () => {
    const { doc, store, programText } = make();
    const before = programText();
    const id = store.getByName('Ablage').id;
    const removed = doc.deletePlace(id);
    expect(removed.ok).toBe(true);
    expect(store.getEntries().some((e) => e.name === 'Ablage')).toBe(false);
    expect(programText()).toBe(before);
    expect(doc.restorePlace(removed.entry, removed.index).ok).toBe(true);
    expect(store.getByName('Ablage')).toBeTruthy();
  });

  it('notifies a subscriber on a store change and unsubscribes cleanly', () => {
    const { doc, store } = make();
    const fn = vi.fn();
    const off = doc.subscribe(fn);
    store.add({ ...PIN, name: 'Neu' });
    expect(fn).toHaveBeenCalled();
    const calls = fn.mock.calls.length;
    off();
    store.add({ ...PIN, name: 'Neu2' });
    expect(fn.mock.calls.length).toBe(calls);
  });

  it('inserts a Vormachen round as program steps', async () => {
    const { doc, programText } = make({ empty: true });
    const items = [
      { kind: 'recording', name: 'Winken', status: 'saved' },
      { kind: 'pin', name: 'Ablage', entryId: 'x' },
    ];
    const result = await doc.insertProgram(items, { placeNameOf: (it) => it.name, gripperStateOf: () => null });
    expect(result.count).toBe(2);
    expect(programText()).toMatch(/Winken/);
    expect(programText()).toMatch(/Ablage/);
  });
});

describe('the code document alone', () => {
  it('sits beside the file sidebar and never edits variables', () => {
    const { doc } = codeDoc();
    expect(doc.anchorLeft()).toBe(CODE_SIDEBAR_WIDTH_PX);
    expect(doc.canEditVariables).toBe(false);
    expect(doc.canInsertSnippets).toBe(true);
  });

  it('jumps by <file>:L<line> through requestReveal', () => {
    const { doc, reveals } = codeDoc();
    const [row] = doc.usageRows('recording', 'Winken');
    expect(row.id).toBe('main.py:L3');
    doc.jump(row.id);
    expect(reveals).toEqual([{ file: 'main.py', line: 3 }]);
  });

  it('„Einfügen" writes one line directly below the cursor’s line (R3-O4)', async () => {
    const { doc, programText, reveals } = codeDoc({ cursor: { file: 'main.py', line: 4 } });
    const result = await doc.insertSnippet({ kind: 'pose', name: 'Hoch' });
    expect(result).toMatchObject({ count: 1, file: 'main.py', firstLine: 5 });
    expect(programText().split('\n')[4]).toBe('robot.move_to("Hoch")');
    expect(reveals[reveals.length - 1]).toEqual({ file: 'main.py', line: 5 });
  });

  it('without a cursor NOTHING is written: click first — the lines ride along for the clipboard', async () => {
    const { doc, programText, reveals } = codeDoc({ cursor: null });
    const result = await doc.insertSnippet({ kind: 'pose', name: 'Hoch' });
    expect(result).toEqual({
      count: 0, error: CODE_DE.CLICK_FIRST_HINT, noCursor: true, lines: ['robot.move_to("Hoch")'],
    });
    expect(programText()).toBe(CODE);
    expect(reveals).toEqual([]);
    const round = await doc.insertProgram([{ kind: 'pin', name: 'Ablage', entryId: 'e' }, { kind: 'recording', name: 'Winken', status: 'saved' }],
      { placeNameOf: (it) => it.name, gripperStateOf: () => null });
    expect(round).toMatchObject({ count: 0, noCursor: true, lines: ['robot.move_to("Ablage")', 'robot.replay("Winken")'] });
  });

  it('mb9: a document switched while the insertion loaded gets nothing, and the student is told', async () => {
    let token = 1;
    let applied = 0;
    let files = { 'main.py': CODE };
    const doc = createCodeAssetDocument({
      language: 'python',
      store: createDetachedDestinationStore([]),
      getFiles: () => files,
      applyFiles: (next) => { applied += 1; files = next; },
      requestReveal: () => {},
      getCursor: () => ({ file: 'main.py', line: 4 }),
      getDocumentToken: () => token,
    });
    const pending = doc.insertSnippet({ kind: 'pose', name: 'Hoch' });
    // Before the module's promise settles, the student opens another program.
    token = 2;
    files = { 'Main.java': 'public class Main {\n}\n' };
    expect(await pending).toEqual({ count: 0, error: CODE_DE.INSERT_DOCUMENT_CHANGED });
    expect(applied).toBe(0);
    // The same document meanwhile: written as usual.
    files = { 'main.py': CODE };
    expect(await doc.insertSnippet({ kind: 'pose', name: 'Hoch' })).toMatchObject({ count: 1, firstLine: 5 });
    expect(applied).toBe(1);
  });

  it('a variable use row covers assignments, reads and zeige calls', () => {
    let files = { 'main.py': 'import robot\npunkte = 0\npunkte += 1\nrobot.zeige("punkte", punkte)\n' };
    const doc = createCodeAssetDocument({
      language: 'python',
      store: createDetachedDestinationStore([]),
      getFiles: () => files,
      applyFiles: (next) => { files = next; },
      requestReveal: () => {},
      getCursor: () => null,
    });
    expect(doc.usageRows('variable', 'punkte').map((r) => r.id))
      .toEqual(['main.py:L2', 'main.py:L3', 'main.py:L4']);
  });
});

describe('the code document alone — where insertions go (review M2, R3-O4)', () => {
  const make = (files, language = 'python', cursor = null) => {
    let current = { ...files };
    const reveals = [];
    const doc = createCodeAssetDocument({
      language,
      store: createDetachedDestinationStore([]),
      getFiles: () => current,
      applyFiles: (next) => { current = next; },
      requestReveal: (at) => reveals.push(at),
      getCursor: () => cursor,
    });
    return { doc, reveals, files: () => current };
  };

  it('„Einfügen" under a 2-space block uses 2 spaces', async () => {
    const { doc, files } = make({ 'main.py': 'import robot\nfor i in range(3):\n  robot.home()\n' },
      'python', { file: 'main.py', line: 2 });
    await doc.insertSnippet({ kind: 'recording', name: 'Winken' });
    expect(files()['main.py']).toBe('import robot\nfor i in range(3):\n  robot.replay("Winken")\n  robot.home()\n');
  });

  it('„Als Programm einfügen" under a tab-indented block uses tabs', async () => {
    const { doc, files } = make({ 'main.py': 'while x:\n\tpass\n' }, 'python', { file: 'main.py', line: 1 });
    const r = await doc.insertProgram(
      [{ kind: 'pin', name: 'Ablage', entryId: 'e' }],
      { placeNameOf: (it) => it.name, gripperStateOf: () => null },
    );
    expect(r.count).toBe(1);
    expect(files()['main.py']).toBe('while x:\n\trobot.move_to("Ablage")\n\tpass\n');
  });

  it('below a line that never lets the next one run: nothing written, the reason instead', async () => {
    const src = 'import robot\nrobot.home()\nwhile True:\n    robot.beep()\n\n';
    const { doc, files, reveals } = make({ 'main.py': src }, 'python', { file: 'main.py', line: 5 });
    const r = await doc.insertSnippet({ kind: 'pose', name: 'Hoch' });
    expect(r).toEqual({ count: 0, error: formatCode(CODE_DE.INSERT_NEVER_RUNS_HINT, 'eine Endlosschleife') });
    expect(files()['main.py']).toBe(src);
    expect(reveals).toEqual([]);
  });

  it('Java below a class line, or below a one-line main: nothing written, a German hint instead', async () => {
    for (const [src, line] of [['public class Main {\n    static void hilfe() {\n    }\n}\n', 1],
      ['public class Main {\n    public static void main(String[] a) { Robot.home(); }\n}\n', 2]]) {
      const { doc, files, reveals } = make({ 'Main.java': src }, 'java', { file: 'Main.java', line });
      const r = await doc.insertSnippet({ kind: 'pose', name: 'Hoch' });
      expect(r).toEqual({ count: 0, error: CODE_DE.NOT_IN_METHOD_HINT });
      expect(files()['Main.java']).toBe(src);
      expect(reveals).toEqual([]);
      expect(await doc.insertProgram([{ kind: 'pin', name: 'A', entryId: 'e' }],
        { placeNameOf: (it) => it.name, gripperStateOf: () => null }))
        .toEqual({ count: 0, error: CODE_DE.NOT_IN_METHOD_HINT });
    }
  });

  it('Java: below main’s header, the first line of its body', async () => {
    const src = 'public class Main {\n    public static void main(String[] a) {\n        Robot.home();\n    }\n}\n';
    const { doc, files, reveals } = make({ 'Main.java': src }, 'java', { file: 'Main.java', line: 2 });
    const r = await doc.insertSnippet({ kind: 'recording', name: 'Winken' });
    expect(r).toMatchObject({ count: 1, file: 'Main.java', firstLine: 3, lastLine: 3 });
    expect(files()['Main.java']).toBe(
      'public class Main {\n    public static void main(String[] a) {\n        Robot.replay("Winken");\n        Robot.home();\n    }\n}\n',
    );
    expect(reveals).toEqual([{ file: 'Main.java', line: 3 }]);
  });
});

describe('the Blockly document alone', () => {
  it('is the page’s own workspace behind the same calls', () => {
    const { doc } = blocklyDoc();
    expect(doc.canEditVariables).toBe(true);
    expect(doc.canInsertSnippets).toBe(false);
    expect(doc.renameRecordingTarget()).toEqual({ workspace: doc.workspace });
  });
});
