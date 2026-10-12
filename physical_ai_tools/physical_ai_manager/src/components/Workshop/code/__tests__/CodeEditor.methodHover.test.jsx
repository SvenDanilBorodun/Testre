/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The method hover (owner decision C): over a REAL CodeMirror state, the
// pure builder finds a robot method only in code and builds its tooltip
// with textContent alone — a doc string is never parsed as markup.

import { methodHoverAt, buildMethodHoverDom } from '../CodeEditor';
import { METHOD_DOCS } from '../methodDocs';
import { CODE_DE, formatCode } from '../codeMessagesDe';

let EditorState;

beforeAll(async () => {
  ({ EditorState } = await import('@codemirror/state'));
});

const stateOf = (doc) => EditorState.create({ doc });
const hoverAt = (doc, needle, language, delta = 1) => methodHoverAt(stateOf(doc), doc.indexOf(needle) + delta, language);

describe('methodHoverAt', () => {
  test('a Python call: its signature and its doc, over the name', () => {
    const doc = 'import robot\nrobot.replay("Winken", speed=2.0)\n';
    const hit = hoverAt(doc, 'replay', 'python');
    expect(hit).toEqual({
      pos: doc.indexOf('replay'),
      end: doc.indexOf('replay') + 'replay'.length,
      signature: 'robot.replay(name, speed=1.0)',
      doc: METHOD_DOCS.byName.replay.doc_de,
    });
  });

  test('a Java call: the first overload, the variant count, the Java sentence', () => {
    const doc = 'import edubotics.Robot;\nclass Main { void m() { Robot.moveTo("A"); } }\n';
    const hit = hoverAt(doc, 'moveTo', 'java');
    expect(hit.signature).toBe(`void Robot.moveTo(double[] target) ${formatCode(CODE_DE.METHODS_VARIANTS, 1)}`);
    expect(hit.doc).toBe(METHOD_DOCS.byName.move_to.doc_de_java);
  });

  test('nothing in a comment, a string, on the receiver or on a stranger', () => {
    expect(hoverAt('# robot.home()\n', 'home', 'python')).toBeNull();
    expect(hoverAt('x = "robot.home()"\n', 'home', 'python')).toBeNull();
    expect(hoverAt('robot.home()\n', 'robot', 'python')).toBeNull();
    expect(hoverAt('math.floor(1.5)\n', 'floor', 'python')).toBeNull();
    expect(hoverAt('Robot.home();', 'home', 'python')).toBeNull();
  });
});

describe('buildMethodHoverDom', () => {
  test('a code line and a paragraph, filled with text only', () => {
    const dom = buildMethodHoverDom({ signature: 'robot.home()', doc: '<b>Fett</b> & mehr' });
    expect(dom.className).toBe('cm-edubotics-method-hover');
    expect([...dom.children].map((c) => c.tagName)).toEqual(['CODE', 'P']);
    expect(dom.children[0].textContent).toBe('robot.home()');
    expect(dom.children[1].textContent).toBe('<b>Fett</b> & mehr');
    expect(dom.getElementsByTagName('b')).toHaveLength(0);
  });
});
