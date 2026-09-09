/**
 * RS-39 — the three destination sockets must carry NO Blockly `check`.
 *
 * This test asserts the ABSENCE of a guard rail, so it needs its reasoning on
 * the record or the next reader will "fix" it back.
 *
 * The underlying defect is real. `find_object` outputs 'Greifziel';
 * „bewege zu" / „aufnehmen" / „ablegen bei" carry no check, so a „finde <Typ>"
 * block plugs straight in with one drag and the block then does the wrong thing
 * silently. Re-measured 2026-09-08 with the real solvers, tag yaw 1.1 rad, the
 * object on each arm's +x axis, against the split blocks' correct answer for the
 * SAME detection — quoting the ROLL JOINT, so the three rows are one quantity:
 *
 *   omx_full     correct z 0.0150 q_roll +0.4708 -> pickup z 0.0270 q_roll +1.5708
 *   edu6_studio  correct z 0.0150 q_roll -0.4708 -> pickup z 0.0270 q_roll +1.5708
 *   edu1_studio  correct z 0.0150 q_roll -0.4708 -> pickup z 0.0270 q_roll -1.5708
 *
 * i.e. +12.0 mm of descend height (GRASP_CLEARANCE_M added on top of a z that IS
 * already the grasp band) and — pickup(Greifziel) never seeing the tag yaw — a
 * wrist error of exactly that yaw, 1.1 rad = 63.03 degrees, on all three arms,
 * reported as success. (An earlier revision of this comment gave the edu6 cell as
 * roll -2.6708 and the error as 56-57 degrees; see motion._refuse_greifziel.)
 *
 * A `check: 'String'` on these three sockets was shipped and then REVERTED
 * 2026-09-08, because it is not a safe way to fix it. Blockly enforces a check
 * at DESERIALISATION by throwing, and the throw ABORTS THE REST OF THE LOAD.
 * Measured on real Blockly 12.5.1: a saved workspace whose „finde"-into-
 * „aufnehmen" mistake predates the check loaded 3 of its 5 blocks, lost the
 * connection, and re-serialised the truncated program. `BlocklyWorkspace.jsx`
 * swallows the throw to console.error with no toast, and the next edit
 * autosaves over the good version — so the guard rail silently destroyed the
 * saved work of exactly the students it was meant to protect: the ones who had
 * already made the mistake.
 *
 * That is the same failure mode that (correctly) blocked removing
 * `edubotics_forever`'s nextStatement for RS-35, and the two must be judged the
 * same way. Adding a client-side check to an input that previously had none is
 * only safe behind a load-time migration that rewrites the offending sockets
 * BEFORE `Blockly.serialization.workspaces.load` sees them. Until such a
 * migration exists, the server refusal (`motion._refuse_greifziel`) is the only
 * fence — it is loud, German, fires on all three arms, and cannot eat a file.
 *
 * If you add the migration, this test is what you change, deliberately.
 */
import { describe, it, expect } from 'vitest';
import { MOTION_BLOCKS } from '../motion';

const byType = (type) => MOTION_BLOCKS.find((b) => b.type === type);
const inputCheck = (type, name) => {
  const block = byType(type);
  if (!block) throw new Error(`${type} must exist`);
  const arg = (block.args0 || []).find((a) => a.name === name);
  if (!arg) throw new Error(`${type} must have an input named ${name}`);
  return arg.check;
};

describe('RS-39: the destination sockets stay uncheck-ed until a load migration exists', () => {
  it.each([
    ['edubotics_move_to', 'DESTINATION'],
    ['edubotics_pickup', 'TARGET'],
    ['edubotics_drop_at', 'DESTINATION'],
  ])('%s/%s carries no check, so an existing saved workspace still loads', (type, name) => {
    expect(inputCheck(type, name)).toBeUndefined();
  });

  it('the split-grasp blocks keep their Greifziel check', () => {
    // These are safe to check: they have carried `check: 'Greifziel'` since the
    // blocks were introduced, so no saved workspace can contain a connection
    // that the check would now reject on load.
    expect(inputCheck('edubotics_move_above', 'ZIEL')).toBe('Greifziel');
    expect(inputCheck('edubotics_descend_to', 'ZIEL')).toBe('Greifziel');
    expect(inputCheck('edubotics_close_on_object', 'ZIEL')).toBe('Greifziel');
  });

  it('„Position von" and „Ziel" both still fit: they output a Ziel-Wert', () => {
    // Read the two producer sources directly — importing the modules pulls in
    // Blockly's runtime registration, which these pure block-shape assertions
    // do not need.
    const fs = require('fs');
    const path = require('path');
    const here = path.dirname(new URL(import.meta.url).pathname);
    const perception = fs.readFileSync(path.join(here, '..', 'perception.js'), 'utf8');
    const destinations = fs.readFileSync(path.join(here, '..', 'destinations.js'), 'utf8');
    expect(perception).toMatch(/edubotics_object_position[\s\S]{0,400}?output: 'String'/);
    expect(destinations).toMatch(/edubotics_destination_ref[\s\S]{0,400}?output: 'String'/);
  });
});
