/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Neu": the ONE table of creation buttons the Blockly flyout, the Sammlung
// drawer (both notations) and the code sidebar all read (owner decision D5),
// gated by one set of capability predicates (D9: the Sim-Tisch only inside the
// simulator, the camera and Vormachen only outside it).

import { describe, it, expect } from 'vitest';
import { DE } from '../../blocks/messages_de';
import { isIconName } from '../../../icons/registry';
import { NEW_ACTION_ROWS, newActionsFor, teachActionFor } from '../newActions';

const RIG = { hardware: true, teach: true, simMode: false, pinCamera: true, pinSim: true };
const SIM = { ...RIG, simMode: true, pinCamera: false };

describe('NEW_ACTION_ROWS', () => {
  it('is one frozen row per creation button, each with an id, a tab, a German label and a known icon', () => {
    expect(Object.isFrozen(NEW_ACTION_ROWS)).toBe(true);
    expect(NEW_ACTION_ROWS.map((r) => [r.id, r.tab, r.label, r.icon, r.action])).toEqual([
      ['teachRecording', 'aufnahmen', DE.FLY_TEACH_RECORDING, 'record', { type: 'teach', kind: 'recording' }],
      ['teachZiel', 'ziele', DE.FLY_TEACH_ZIEL, 'ziel', { type: 'teach', kind: 'ziel' }],
      ['pinCamera', 'ziele', DE.FLY_PIN_CAMERA, 'camera', { type: 'pinCamera' }],
      ['pinSim', 'ziele', DE.FLY_PIN_SIM, 'box', { type: 'pinSim' }],
      ['teachPose', 'positionen', DE.FLY_TEACH_POSE, 'pose', { type: 'teach', kind: 'pose' }],
    ]);
    for (const row of NEW_ACTION_ROWS) {
      expect(isIconName(row.icon)).toBe(true);
      expect(typeof row.allowed).toBe('function');
    }
  });

  it('the teach rows carry the kind icons the owner chose (D10)', () => {
    const icon = (id) => NEW_ACTION_ROWS.find((r) => r.id === id).icon;
    expect([icon('teachRecording'), icon('teachPose'), icon('teachZiel')]).toEqual(['record', 'pose', 'ziel']);
  });
});

describe('newActionsFor', () => {
  it('per tab, on a calibrated rig outside the simulator', () => {
    expect(newActionsFor(RIG, 'aufnahmen').map((a) => a.label)).toEqual([DE.FLY_TEACH_RECORDING]);
    expect(newActionsFor(RIG, 'ziele').map((a) => a.action)).toEqual([
      { type: 'teach', kind: 'ziel' }, { type: 'pinCamera' },
    ]);
    expect(newActionsFor(RIG, 'positionen').map((a) => a.action)).toEqual([{ type: 'teach', kind: 'pose' }]);
    expect(newActionsFor(RIG, 'variablen')).toEqual([]);
    expect(newActionsFor(RIG, 'aufnahmen')[0]).toEqual({
      id: 'teachRecording', label: DE.FLY_TEACH_RECORDING, icon: 'record', action: { type: 'teach', kind: 'recording' },
    });
  });

  it('all tabs at once, each action once, in flyout order', () => {
    expect(newActionsFor(RIG).map((a) => a.id)).toEqual(['teachRecording', 'teachZiel', 'pinCamera', 'teachPose']);
  });

  it('D9: the Sim-Tisch only inside the simulator, Vormachen and the camera only outside it', () => {
    expect(newActionsFor(SIM).map((a) => a.action)).toEqual([{ type: 'pinSim' }]);
    expect(newActionsFor({ ...RIG, pinCamera: true, simMode: true }).map((a) => a.id)).toEqual(['pinSim']);
    expect(newActionsFor(RIG).some((a) => a.id === 'pinSim')).toBe(false);
  });

  it('nothing on the rig without hardware (the teacher page), and nothing for no capabilities', () => {
    expect(newActionsFor({ ...RIG, hardware: false }).map((a) => a.id)).toEqual([]);
    expect(newActionsFor({ ...SIM, hardware: false }).map((a) => a.id)).toEqual(['pinSim']);
    expect(newActionsFor({ ...RIG, teach: false }).map((a) => a.id)).toEqual(['pinCamera']);
    expect(newActionsFor({ ...RIG, pinCamera: false }).map((a) => a.id)).toEqual(['teachRecording', 'teachZiel', 'teachPose']);
    expect(newActionsFor(null)).toEqual([]);
    expect(newActionsFor(undefined, 'ziele')).toEqual([]);
    expect(newActionsFor(RIG, 'bogus')).toEqual([]);
  });

  it('teachActionFor answers the one Vormachen action of a tab, or null', () => {
    expect(teachActionFor(RIG, 'aufnahmen')).toMatchObject({ id: 'teachRecording', icon: 'record' });
    expect(teachActionFor(RIG, 'ziele')).toMatchObject({ id: 'teachZiel', action: { type: 'teach', kind: 'ziel' } });
    expect(teachActionFor(RIG, 'positionen')).toMatchObject({ id: 'teachPose' });
    expect(teachActionFor(RIG, 'variablen')).toBeNull();
    expect(teachActionFor(SIM, 'ziele')).toBeNull();
  });
});
