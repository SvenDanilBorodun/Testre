/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// E1 (Roboter Studio review fixes): the names a Python file gives the robot
// module and its functions at the top level — `import robot as r`,
// `from robot import replay as abspielen` — which every editor scanner
// follows (usage, rename, lint, completion, hover, the replay scan). The
// parser reads text whose strings and comments are ALREADY blanked
// (codeAssetUsage.codeOnlyText): it imports nothing from codeAssetUsage, which
// imports it.

import fs from 'fs';
import path from 'path';
import { describe, it, expect } from 'vitest';
import { codeOnlyText } from '../codeAssetUsage';
import { bareNamesOf, pythonRobotAliases } from '../pythonAliases';

const aliasesOf = (src) => {
  const a = pythonRobotAliases(codeOnlyText(src, 'python'));
  return { receivers: a.receivers, functions: [...a.functions.entries()] };
};

describe('pythonRobotAliases', () => {
  it('always names `robot` itself, and nothing else for a plain import', () => {
    expect(aliasesOf('import robot\nrobot.home()\n')).toEqual({ receivers: ['robot'], functions: [] });
    expect(aliasesOf('')).toEqual({ receivers: ['robot'], functions: [] });
  });

  it('reads `import robot as NAME`, also inside a comma list', () => {
    expect(aliasesOf('import robot as r\n').receivers).toEqual(['robot', 'r']);
    expect(aliasesOf('import os, robot as r, math as m\n').receivers).toEqual(['robot', 'r']);
    expect(aliasesOf('import robot as röbi\n').receivers).toEqual(['robot', 'röbi']);
    // `import robotik as r` and `import robot.x as r` are another module.
    expect(aliasesOf('import robotik as r\nimport robot.x as q\n').receivers).toEqual(['robot']);
  });

  it('reads `from robot import ROW as NAME` for a public row only', () => {
    expect(aliasesOf('from robot import replay as abspielen\n').functions).toEqual([['abspielen', 'replay']]);
    expect(aliasesOf('from robot import replay as abspielen, home as heim, move_to\n').functions)
      .toEqual([['abspielen', 'replay'], ['heim', 'home']]);
    expect(aliasesOf('from robot import (\n    replay as abspielen,\n    home,\n)\n').functions)
      .toEqual([['abspielen', 'replay']]);
    // Not a public row (a type, an internal frame, a typo): no alias.
    expect(aliasesOf('from robot import Greifobjekt as G, __vars as v, replya as x\n').functions).toEqual([]);
    expect(aliasesOf('from robotik import replay as abspielen\n').functions).toEqual([]);
  });

  it('reads every statement of a `;` line and a `\\` continuation', () => {
    expect(aliasesOf('import math; import robot as r\n').receivers).toEqual(['robot', 'r']);
    expect(aliasesOf('from robot import replay \\\n    as abspielen\n').functions).toEqual([['abspielen', 'replay']]);
  });

  it('reads only the top level: not inside a function, a block or a bracket', () => {
    const src = [
      'def f():',
      '    import robot as r',
      'if True:',
      '    from robot import replay as abspielen',
      'x = [',
      'import robot as q',
      ']',
      '',
    ].join('\n');
    expect(aliasesOf(src)).toEqual({ receivers: ['robot'], functions: [] });
  });

  it('never reads an alias out of a string or a comment', () => {
    const src = [
      '# import robot as r',
      'text = "import robot as s"',
      'doc = """',
      'from robot import replay as abspielen',
      '"""',
      '',
    ].join('\n');
    expect(aliasesOf(src)).toEqual({ receivers: ['robot'], functions: [] });
  });

  it('is total on garbage', () => {
    for (const bad of [null, undefined, 42, {}, 'import robot as', 'from robot import', 'import robot as 1x']) {
      const a = pythonRobotAliases(bad);
      expect(a.receivers).toEqual(['robot']);
      expect(a.functions.size).toBe(0);
    }
  });

  it('imports nothing from codeAssetUsage (which imports it) and stays pure', () => {
    const src = fs.readFileSync(path.resolve(__dirname, '..', 'pythonAliases.js'), 'utf8');
    expect(src).not.toMatch(/from ['"][^'"]*codeAssetUsage['"]/);
    expect(src).not.toMatch(/from ['"](react|codemirror|@codemirror\/|@lezer\/|blockly)/);
    expect(src).not.toMatch(/import\(/);
  });
});

// Fix round 1, R2: which bare names a file binds from robot as themselves.
describe('pythonRobotAliases — the bare names bound from robot (R2)', () => {
  const bound = (src) => {
    const a = pythonRobotAliases(codeOnlyText(src, 'python'));
    return { star: a.star, imported: [...a.imported] };
  };

  it('reads `from robot import NAME`, `*` and a multi-line list', () => {
    expect(bound('import robot\n')).toEqual({ star: false, imported: [] });
    expect(bound('from robot import replay, home as heim\n')).toEqual({ star: false, imported: ['replay'] });
    expect(bound('from robot import *\n')).toEqual({ star: true, imported: [] });
    expect(bound('from robot import (\n    replay,\n    move_to,\n)\n')).toEqual({ star: false, imported: ['replay', 'move_to'] });
    // Another module, a non-public name, a nested import: nothing.
    expect(bound('from roboter import *\nfrom robot import replya\ndef f():\n    from robot import replay\n'))
      .toEqual({ star: false, imported: [] });
  });

  it('bareNamesOf names the row itself only when it is bound', () => {
    const of = (src) => bareNamesOf(pythonRobotAliases(codeOnlyText(src, 'python')), 'replay');
    expect(of('')).toEqual([]);
    expect(of('def replay(n):\n    pass\n')).toEqual([]);
    expect(of('from robot import replay\n')).toEqual(['replay']);
    expect(of('from robot import *\n')).toEqual(['replay']);
    expect(of('from robot import replay as abspielen\n')).toEqual(['abspielen']);
    expect(of('from robot import *\nfrom robot import home as replay\n')).toEqual([]);
  });
});
