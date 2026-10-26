/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Methoden column's pure half (methodDocs.js) against the generated
// robot_api_docs.json: the example lines rendered here are the generator's
// own, byte for byte (the lockstep with robot_api.py::_example_line), a
// student's own Sammlung name replaces the table's only where the parameter
// takes it, and the hover finds a robot method only in code.

import { describe, expect, it } from 'vitest';
import docs from '../robot_api_docs.json';
import {
  METHOD_DOCS,
  defaultTextFor,
  exampleArgsFor,
  methodAtOffset,
  methodByName,
  methodHoverParts,
  methodShortForm,
  rangeTextDe,
  renderExample,
} from '../methodDocs';
import { CODE_DE, formatCode } from '../codeMessagesDe';

const byName = (name) => METHOD_DOCS.byName[name];

describe('the data', () => {
  it('is every public row in table order, and the two types', () => {
    expect(METHOD_DOCS.methods.map((m) => m.name)).toEqual(docs.methods.map((m) => m.name));
    expect(METHOD_DOCS.methods).toHaveLength(33);
    expect(METHOD_DOCS.types.map((t) => t.name)).toEqual(['Greifziel', 'Greifobjekt']);
    expect(METHOD_DOCS.methods.some((m) => m.name.startsWith('__'))).toBe(false);
  });

  it('finds a row by the name each language writes', () => {
    expect(methodByName('python', 'move_to')).toBe(byName('move_to'));
    expect(methodByName('java', 'moveTo')).toBe(byName('move_to'));
    expect(methodByName('java', 'move_to')).toBeNull();
    expect(methodByName('python', 'waitSeconds')).toBeNull();
    expect(methodByName('python', '__vars')).toBeNull();
  });
});

describe('renderExample — the lockstep with the generator', () => {
  it('renders every row exactly as robot_api.py did, in both languages', () => {
    for (const m of METHOD_DOCS.methods) {
      expect(renderExample(m, 'python')).toBe(m.example_py);
      expect(renderExample(m, 'java')).toBe(m.example_java);
    }
  });

  it('spells a float by its kind, though JSON.parse dropped its .0', () => {
    const tone = byName('tone');
    expect(tone.example.args).toEqual([440, 0.5]);
    expect(renderExample(tone, 'python')).toBe('robot.tone(440.0, 0.5)');
    expect(renderExample(byName('wait_until_held'), 'java')).toBe('boolean gehalten = Robot.waitUntilHeld(3.0);');
  });

  it('a Greifziel argument is the nested find call (never an undefined variable, N9), and a value row declares its variable', () => {
    // The example used to name a variable `ziel` the student's program does
    // not have; it now carries the call that produces the Greifziel.
    expect(renderExample(byName('object_position'), 'python'))
      .toBe('position = robot.object_position(robot.find("wuerfel"))');
    expect(renderExample(byName('object_position'), 'java'))
      .toBe('double[] position = Robot.objectPosition(Robot.find("wuerfel"));');
    expect(renderExample(byName('move_above'), 'python')).toBe('robot.move_above(robot.find("wuerfel"))');
    expect(renderExample(byName('move_above'), 'java')).toBe('Robot.moveAbove(Robot.find("wuerfel"));');
    // No public row's example names a variable any more.
    for (const m of METHOD_DOCS.methods) {
      for (const a of (m.example && m.example.args) || []) {
        expect(!!a && typeof a === 'object' && !Array.isArray(a) && 'ref' in a).toBe(false);
      }
    }
    expect(renderExample(byName('find'), 'java')).toBe('Robot.Greifziel ziel = Robot.find("wuerfel");');
  });

  it('a nested call argument ({"call": …}) is that row’s own call, rendered by its own kinds (N9)', () => {
    // The shape robot_api.py's docs json uses for an insertable example that
    // needs a Greifziel: `robot.move_above(robot.find("wuerfel"))`, never an
    // undefined variable. Both shapes ({ref} and {call}) render, so the docs
    // json can move from one to the other without this file going red.
    const moveAbove = { ...byName('move_above'), example: { args: [{ call: 'find', args: ['wuerfel'] }], var: null } };
    expect(renderExample(moveAbove, 'python')).toBe('robot.move_above(robot.find("wuerfel"))');
    expect(renderExample(moveAbove, 'java')).toBe('Robot.moveAbove(Robot.find("wuerfel"));');
    const position = { ...byName('object_position'), example: { args: [{ call: 'find', args: ['wuerfel'] }], var: 'position' } };
    expect(renderExample(position, 'python')).toBe('position = robot.object_position(robot.find("wuerfel"))');
    expect(renderExample(position, 'java')).toBe('double[] position = Robot.objectPosition(Robot.find("wuerfel"));');
    // A float argument of the nested row keeps its kind's spelling.
    const nestedFloat = { ...byName('move_above'), example: { args: [{ call: 'wait_until_seen', args: ['wuerfel', 5] }], var: null } };
    expect(renderExample(nestedFloat, 'python')).toBe('robot.move_above(robot.wait_until_seen("wuerfel", 5.0))');
    // The old {ref} shape still renders its name.
    const legacy = { ...byName('move_above'), example: { args: [{ ref: 'ziel' }], var: null } };
    expect(renderExample(legacy, 'python')).toBe('robot.move_above(ziel)');
    expect(renderExample(legacy, 'java')).toBe('Robot.moveAbove(ziel);');
  });

  it('renders substituted arguments', () => {
    expect(renderExample(byName('replay'), 'python', ['Tanz'])).toBe('robot.replay("Tanz")');
    expect(renderExample(byName('move_to'), 'java', ['Hoch'])).toBe('Robot.moveTo("Hoch");');
  });
});

describe('exampleArgsFor — the student’s own names', () => {
  const sammlung = { places: ['Ablage 2', 'Hoch'], recordings: ['Tanz', 'Winken'], objects: ['banane'] };

  it('takes the first Ziel, Bewegung or object type where the method takes one', () => {
    expect(exampleArgsFor(byName('move_to'), sammlung)).toEqual(['Ablage 2']);
    expect(exampleArgsFor(byName('ziel'), sammlung)).toEqual(['Ablage 2']);
    expect(exampleArgsFor(byName('replay'), sammlung)).toEqual(['Tanz']);
    expect(exampleArgsFor(byName('grasp'), sammlung)).toEqual(['banane']);
    expect(exampleArgsFor(byName('wait_until_seen'), sammlung)).toEqual(['banane', 5]);
  });

  it('keeps the table’s name for something NEW (pin, counter, zeige) and with no Sammlung', () => {
    expect(exampleArgsFor(byName('pin'), sammlung)).toEqual(['Ablage', 0.2, 0, 0]);
    expect(exampleArgsFor(byName('counter_add'), sammlung)).toEqual(['Punkte']);
    expect(exampleArgsFor(byName('zeige'), sammlung)).toEqual(['Anzahl', 3]);
    expect(exampleArgsFor(byName('replay'), null)).toEqual(['Winken']);
    expect(exampleArgsFor(byName('replay'), { recordings: [] })).toEqual(['Winken']);
    expect(exampleArgsFor(byName('home'), sammlung)).toEqual([]);
  });

  it('refuses a name the parameter’s pattern refuses, or one that would break the literal', () => {
    expect(exampleArgsFor(byName('replay'), { recordings: ['Tanz!'] })).toEqual(['Winken']);
    expect(exampleArgsFor(byName('grasp'), { objects: ['große Kiste'] })).toEqual(['wuerfel']);
    expect(exampleArgsFor(byName('move_to'), { places: ['a"b'] })).toEqual(['Ablage']);
    expect(exampleArgsFor(byName('move_to'), { places: ['a\\b'] })).toEqual(['Ablage']);
    expect(exampleArgsFor(byName('move_to'), { places: ['a\nb'] })).toEqual(['Ablage']);
    expect(exampleArgsFor(byName('replay'), { recordings: ['Größe 1'] })).toEqual(['Größe 1']);
  });

  it('never changes the row itself', () => {
    const args = exampleArgsFor(byName('replay'), sammlung);
    args.push('x');
    expect(byName('replay').example.args).toEqual(['Winken']);
  });
});

describe('ranges, defaults, short forms', () => {
  it('says each range in German, by kind', () => {
    const replay = byName('replay');
    expect(rangeTextDe(replay.params[1])).toBe(formatCode(CODE_DE.METHODS_RANGE, '0.25', '3.0'));
    expect(rangeTextDe(replay.params[0])).toBe(formatCode(CODE_DE.METHODS_MAX_CHARS, 40));
    expect(rangeTextDe(byName('toast').params[2])).toBe('1 bis 15');
    expect(rangeTextDe(byName('find').params[0])).toBe('höchstens 24 Zeichen');
    expect(rangeTextDe(byName('move_to').params[0])).toBe('');
    expect(rangeTextDe({ kind: 'float', lo: 0 })).toBe(formatCode(CODE_DE.METHODS_MIN, '0.0'));
  });

  it('gives a default only to an optional parameter', () => {
    expect(defaultTextFor(byName('replay').params[1], 'python')).toBe('1.0');
    expect(defaultTextFor(byName('toast').params[1], 'java')).toBe('"info"');
    expect(defaultTextFor(byName('replay').params[0], 'python')).toBeNull();
  });

  it('a row’s short form: Python’s signature, Java’s name with „…" for its overloads’ parameters', () => {
    expect(methodShortForm(byName('move_to'), 'python')).toBe('robot.move_to(target)');
    expect(methodShortForm(byName('replay'), 'python')).toBe('robot.replay(name, speed=1.0)');
    expect(methodShortForm(byName('wait_until_seen'), 'java')).toBe('Robot.waitUntilSeen(…)');
    expect(methodShortForm(byName('move_to'), 'java')).toBe('Robot.moveTo(…)');
    expect(methodShortForm(byName('home'), 'java')).toBe('Robot.home()');
  });
});

describe('methodHoverParts', () => {
  it('Python: the one signature and the doc', () => {
    expect(methodHoverParts(byName('replay'), 'python')).toEqual({
      signature: 'robot.replay(name, speed=1.0)', doc: byName('replay').doc_de,
    });
  });

  it('Java: the first overload, the count of the others, the Java sentence where there is one', () => {
    const parts = methodHoverParts(byName('move_to'), 'java');
    expect(parts.signature).toBe(`void Robot.moveTo(double[] target) ${formatCode(CODE_DE.METHODS_VARIANTS, 1)}`);
    expect(parts.doc).toBe(byName('move_to').doc_de_java);
    expect(methodHoverParts(byName('home'), 'java')).toEqual({ signature: 'void Robot.home()', doc: byName('home').doc_de });
    expect(methodHoverParts(byName('zeige'), 'java').signature).toMatch(/\(\+5 Varianten\)$/);
  });
});

describe('methodAtOffset — a robot method in code, nowhere else', () => {
  const at = (text, language, needle, delta = 2) => methodAtOffset(text, language, text.indexOf(needle) + delta);

  it('hits robot.move_to anywhere on its name', () => {
    const text = 'import robot\nrobot.move_to("Ablage")\n';
    const hit = at(text, 'python', 'move_to');
    expect(hit).toMatchObject({ from: text.indexOf('move_to'), to: text.indexOf('move_to') + 7 });
    expect(hit.method.name).toBe('move_to');
    expect(methodAtOffset(text, 'python', text.indexOf('move_to'))).not.toBeNull();
    expect(methodAtOffset(text, 'python', text.indexOf('move_to') + 7)).not.toBeNull();
    expect(at('x = robot . find ("w")', 'python', 'find')).not.toBeNull();
  });

  it('misses in a comment, a string, another receiver and a bare name', () => {
    expect(at('# robot.move_to("Ablage")\n', 'python', 'move_to')).toBeNull();
    expect(at('print("robot.move_to")\n', 'python', 'move_to')).toBeNull();
    expect(at('xrobot.move_to("A")\n', 'python', 'move_to')).toBeNull();
    expect(at('Größrobot.move_to("A")\n', 'python', 'move_to')).toBeNull();
    expect(at('move_to("A")\n', 'python', 'move_to')).toBeNull();
    expect(at('robot.move_tox("A")\n', 'python', 'move_tox')).toBeNull();
    expect(at('"robot".move_to("A")\n', 'python', 'move_to')).toBeNull();
    expect(at('robot.move_to("A")\n', 'python', 'robot', 1)).toBeNull();
  });

  it('Java: Robot. and edubotics.Robot., never the Python spelling', () => {
    expect(at('Robot.moveTo("A");', 'java', 'moveTo').method.name).toBe('move_to');
    expect(at('edubotics.Robot.moveTo("A");', 'java', 'moveTo').method.name).toBe('move_to');
    expect(at('Robot.move_to("A");', 'java', 'move_to')).toBeNull();
    expect(at('robot.moveTo("A");', 'java', 'moveTo')).toBeNull();
    expect(at('// Robot.moveTo("A");', 'java', 'moveTo')).toBeNull();
    expect(at('/* Robot.home(); */', 'java', 'home')).toBeNull();
  });

  it('answers null for garbage input', () => {
    expect(methodAtOffset(null, 'python', 0)).toBeNull();
    expect(methodAtOffset('robot.home()', 'cobol', 7)).toBeNull();
    expect(methodAtOffset('robot.home()', 'python', 99)).toBeNull();
    expect(methodAtOffset('robot.home()', 'python', -1)).toBeNull();
  });
});

// E1 (Roboter Studio review fixes): a top-level `import robot as r` makes `r.`
// a receiver of the hover too — in that file only.
describe('methodAtOffset — an alias of the robot module', () => {
  const at = (text, needle, delta = 2) => methodAtOffset(text, 'python', text.indexOf(needle) + delta);

  it('hits r.home() after `import robot as r`', () => {
    const text = 'import robot as r\nr.home()\n';
    const hit = at(text, 'home');
    expect(hit).toMatchObject({ from: text.indexOf('home'), to: text.indexOf('home') + 4 });
    expect(hit.method.name).toBe('home');
    expect(at('import robot as r\nimport robot\nrobot.home()\n', 'home')).not.toBeNull();
  });

  it('misses r.home() without the alias, with a commented alias and with another module', () => {
    expect(at('r.home()\n', 'home')).toBeNull();
    expect(at('# import robot as r\nr.home()\n', 'home')).toBeNull();
    expect(at('import math as r\nr.home()\n', 'home')).toBeNull();
    expect(at('import robot as r\nxr.home()\n', 'home')).toBeNull();
  });
});
