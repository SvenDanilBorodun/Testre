/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * „sonst" on a freshly dragged „wenn" block.
 *
 * @blockly/block-plus-minus 9.0.10 unregisters Blockly's own
 * `controls_if_mutator` and installs a ⊕/⊖ replacement whose `plus()` only ever
 * calls `addElseIf_()`; `hasElse_` is set exclusively by `domToMutation` /
 * `loadExtraState`. So a `controls_if` restored from a saved file can have an
 * else, but one dragged out of the Logik category never can — the category
 * could not express if/else at all for new work, while `interpreter.py::
 * _exec_if` has always run the ELSE branch.
 *
 * `registerControlsIfElseMutator()` re-registers the same mutator name on top
 * with else support. THE HARD CONSTRAINT IS BACKWARD SAFETY: the last
 * unreviewed Blockly-side change (`check: 'String'` on the destination sockets)
 * had to be reverted because it ate saved student workspaces. So the four
 * proofs below run against the REAL shipped Blockly 12.5.1 + the REAL plugin
 * out of node_modules:
 *
 *   1. an existing saved workspace WITH an else round-trips byte-identically
 *   2. one WITHOUT an else round-trips byte-identically
 *   3. bytes written here load in the OLD code — proved in the sibling
 *      `controlsIfElseBackCompat.test.js`, which never registers this mutator
 *   4. no load path throws MissingConnection or leaves a partial workspace
 */
import { describe, it, expect, beforeAll } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerMotionBlocks } from '../motion';
import { registerControlBlocks, registerControlsIfElseMutator } from '../control';
import {
  ALL_FIXTURES,
  IF_PLAIN,
  IF_WITH_ELSE,
  IF_ELSEIF_ELSE,
  IF_ELSEIF_NO_ELSE,
} from './controlsIfFixtures';

const inputNames = (block) => block.inputList.map((i) => i.name);
// Blockly queues events onto `requestAnimationFrame(() => setTimeout(…, 0))`.
const flushEvents = async () => {
  await new Promise((resolve) => {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => { setTimeout(resolve, 0); });
    } else {
      setTimeout(resolve, 0);
    }
  });
  await new Promise((resolve) => { setTimeout(resolve, 0); });
};
const roundTrip = (fixture) => {
  const workspace = new Blockly.Workspace();
  Blockly.serialization.workspaces.load(fixture, workspace);
  const saved = Blockly.serialization.workspaces.save(workspace);
  const blocks = workspace.getAllBlocks(false);
  workspace.dispose();
  return { saved, blockCount: blocks.length };
};

describe('controls_if „sonst" — mutator with else support', () => {
  beforeAll(async () => {
    Blockly.setLocale(De);
    registerMotionBlocks();
    registerControlBlocks();
    // Order matters: the plugin unregisters + re-registers `controls_if_mutator`
    // at import time, so ours has to land after it. This mirrors
    // BlocklyWorkspace.jsx::initPlugins.
    await import('@blockly/block-plus-minus');
    registerControlsIfElseMutator();
  });

  // ---------------------------------------------------------------- behaviour
  it('a freshly created block offers the „sonst" row', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'ELSE_ADD']);
    expect(block.saveExtraState()).toBeNull();
    workspace.dispose();
  });

  it('pressing the „sonst" ⊕ adds a real ELSE input — the defect this fixes', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus('ELSE');
    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'ELSE']);
    expect(block.saveExtraState()).toEqual({ hasElse: true });
    workspace.dispose();
  });

  it('the plugin behaviour is unchanged: plain ⊕ still adds „sonst wenn"', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus();
    block.plus();
    expect(inputNames(block)).toEqual([
      'IF0', 'DO0', 'IF1', 'DO1', 'IF2', 'DO2', 'ELSE_ADD',
    ]);
    expect(block.saveExtraState()).toEqual({ elseIfCount: 2 });
    workspace.dispose();
  });

  it('the ELSE row stays LAST as „sonst wenn" clauses are added', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus('ELSE');
    block.plus();
    block.plus();
    expect(inputNames(block)).toEqual([
      'IF0', 'DO0', 'IF1', 'DO1', 'IF2', 'DO2', 'ELSE',
    ]);
    expect(block.saveExtraState()).toEqual({ elseIfCount: 2, hasElse: true });
    workspace.dispose();
  });

  it('⊖ on the ELSE row removes it and brings the „sonst" ⊕ back', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus('ELSE');
    block.plus();
    block.minus('ELSE');
    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'IF1', 'DO1', 'ELSE_ADD']);
    expect(block.saveExtraState()).toEqual({ elseIfCount: 1 });
    workspace.dispose();
  });

  it('removing the ELSE keeps the student’s blocks on the canvas', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus('ELSE');
    const body = workspace.newBlock('edubotics_home');
    block.getInput('ELSE').connection.connect(body.previousConnection);
    expect(block.getInputTargetBlock('ELSE')).toBe(body);

    block.minus('ELSE');

    // Disconnected, NOT disposed — removeInput would have taken it with the
    // input, and a student who mis-clicks would lose the blocks they wrote.
    expect(body.isDisposed()).toBe(false);
    expect(body.getParent()).toBeNull();
    workspace.dispose();
  });

  it('an undo-shaped loadExtraState also keeps the ELSE body alive', () => {
    // Rückgängig on „sonst hinzufügen" replays loadExtraState with the OLD
    // state. Blockly core's updateShape_ disposes whatever was in the ELSE
    // there; ours disconnects it first.
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus('ELSE');
    const body = workspace.newBlock('edubotics_home');
    block.getInput('ELSE').connection.connect(body.previousConnection);

    block.loadExtraState({});

    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'ELSE_ADD']);
    expect(body.isDisposed()).toBe(false);
    workspace.dispose();
  });

  it('removing a MIDDLE „sonst wenn" still works with no ELSE present', () => {
    // The plugin's removeElseIf_ shift loop breaks on the input named 'ELSE'.
    // With no else, the trailing row is the DUMMY 'ELSE_ADD' whose connection
    // is null, so an un-widened break would throw here and leave the block
    // half-rebuilt. This is the one place our copy differs from the plugin's.
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus();
    block.plus();
    const inDo1 = workspace.newBlock('edubotics_home');
    const inDo2 = workspace.newBlock('edubotics_home');
    block.getInput('DO1').connection.connect(inDo1.previousConnection);
    block.getInput('DO2').connection.connect(inDo2.previousConnection);

    expect(() => block.minus(1)).not.toThrow();

    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'IF1', 'DO1', 'ELSE_ADD']);
    // DO2's contents shifted up into DO1 — the plugin's "no gaps" contract.
    expect(block.getInputTargetBlock('DO1')).toBe(inDo2);
    expect(inDo1.getParent()).toBeNull();
    expect(inDo1.isDisposed()).toBe(false);
    workspace.dispose();
  });

  it('removing a MIDDLE „sonst wenn" still works WITH an ELSE present', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus('ELSE');
    block.plus();
    block.plus();
    const inDo2 = workspace.newBlock('edubotics_home');
    block.getInput('DO2').connection.connect(inDo2.previousConnection);

    expect(() => block.minus(1)).not.toThrow();

    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'IF1', 'DO1', 'ELSE']);
    expect(block.getInputTargetBlock('DO1')).toBe(inDo2);
    workspace.dispose();
  });

  it('a second „sonst" press is a no-op, and ⊖ on a block with no else too', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.minus('ELSE');
    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'ELSE_ADD']);
    block.plus('ELSE');
    block.plus('ELSE');
    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'ELSE']);
    expect(block.saveExtraState()).toEqual({ hasElse: true });
    workspace.dispose();
  });

  it('the ⊕/⊖ FIELDS drive it, and each fires a mutation event for Rückgängig',
    async () => {
      // The direct plus()/minus() calls above never touch `withMutationEvent`.
      // A student only ever reaches this through the field's click handler, and
      // without the BlockChange('mutation') event Blockly's undo stack cannot
      // restore the shape.
      const workspace = new Blockly.Workspace();
      const block = workspace.newBlock('controls_if');
      const events = [];
      workspace.addChangeListener((e) => { if (e) events.push(e); });

      block.getField('ELSE_PLUS').showEditor_();
      expect(inputNames(block)).toEqual(['IF0', 'DO0', 'ELSE']);

      block.getField('ELSE_MINUS').showEditor_();
      expect(inputNames(block)).toEqual(['IF0', 'DO0', 'ELSE_ADD']);

      block.getField('PLUS').showEditor_();
      expect(inputNames(block)).toEqual(['IF0', 'DO0', 'IF1', 'DO1', 'ELSE_ADD']);

      await flushEvents();
      const mutations = events.filter(
        (e) => e.type === Blockly.Events.BLOCK_CHANGE && e.element === 'mutation',
      );
      expect(mutations).toHaveLength(3);
      expect(mutations[0].newValue).toBe('{"hasElse":true}');
      expect(mutations[1].newValue).toBe('');
      expect(mutations[2].newValue).toBe('{"elseIfCount":1}');
      workspace.dispose();
    });

  // ------------------------------------------------------- serialization form
  it('saveExtraState emits ONLY the two legacy keys, in the legacy order', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus('ELSE');
    block.plus();
    // Key ORDER is part of the byte contract: both Blockly core and the plugin
    // write elseIfCount first. JSON.stringify preserves insertion order.
    expect(JSON.stringify(block.saveExtraState()))
      .toBe('{"elseIfCount":1,"hasElse":true}');
    workspace.dispose();
  });

  it('hasElse is never serialized as false — it is omitted', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus();
    expect(JSON.stringify(block.saveExtraState())).toBe('{"elseIfCount":1}');
    workspace.dispose();
  });

  it('the ⊕/⊖ fields and the „sonst" label are NOT serializable', () => {
    // If any of them were, every saved controls_if would gain a `fields` key
    // and stop being byte-compatible with what core and the plugin write.
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus();
    const saved = Blockly.serialization.blocks.save(block);
    expect(saved.fields).toBeUndefined();
    block.plus('ELSE');
    expect(Blockly.serialization.blocks.save(block).fields).toBeUndefined();
    workspace.dispose();
  });

  // ------------------------------------------------------------- proofs 1 + 2
  it.each(ALL_FIXTURES)(
    'proof 1/2 — a saved workspace (%s) round-trips byte-identically',
    (_label, fixture) => {
      const { saved } = roundTrip(fixture);
      expect(JSON.stringify(saved)).toBe(JSON.stringify(fixture));
    },
  );

  // ----------------------------------------------------------------- proof 4
  it('proof 4 — every fixture loads WHOLE: no throw, no missing block', () => {
    // `Blockly.serialization.workspaces.load` aborts the REST of the load on a
    // MissingConnection, leaving a partially loaded workspace that the next
    // edit autosaves over. Count the blocks, don't just check for a throw.
    const expected = {
      'if / dann': 2,
      'if / dann / sonst': 3,
      'if / sonst wenn x2 / sonst': 5,
      'if / sonst wenn x1, kein sonst': 3,
    };
    ALL_FIXTURES.forEach(([label, fixture]) => {
      const workspace = new Blockly.Workspace();
      expect(() => Blockly.serialization.workspaces.load(fixture, workspace))
        .not.toThrow();
      expect(workspace.getAllBlocks(false).length).toBe(expected[label]);
      workspace.dispose();
    });
  });

  it('a loaded else block carries the ELSE input and its body', () => {
    const workspace = new Blockly.Workspace();
    Blockly.serialization.workspaces.load(IF_WITH_ELSE, workspace);
    const block = workspace.getBlockById('if-else-00000000000');
    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'ELSE']);
    expect(block.getInputTargetBlock('ELSE').id).toBe('else-body-000000000');
    workspace.dispose();
  });

  it('a loaded else-less block gets the „sonst" ⊕ row, so it can grow one', () => {
    // This is the whole point for an EXISTING workspace: the student opens an
    // old program and can now add the else the plugin denied them.
    const workspace = new Blockly.Workspace();
    Blockly.serialization.workspaces.load(IF_ELSEIF_NO_ELSE, workspace);
    const block = workspace.getBlockById('if-eif-000000000000');
    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'IF1', 'DO1', 'ELSE_ADD']);
    block.plus('ELSE');
    expect(JSON.stringify(Blockly.serialization.blocks.save(block).extraState))
      .toBe('{"elseIfCount":1,"hasElse":true}');
    workspace.dispose();
  });

  // --------------------------------------------------------------- XML legacy
  it('the legacy XML mutation shape still round-trips', () => {
    // Older saved workflows carry `extraState` as an XML STRING; Blockly routes
    // those through domToMutation/mutationToDom instead.
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.domToMutation(
      Blockly.utils.xml.textToDom('<mutation elseif="1" else="1"></mutation>'),
    );
    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'IF1', 'DO1', 'ELSE']);
    const xml = Blockly.Xml.domToText(block.mutationToDom());
    // (Blockly stamps its own xmlns on a created element; the ATTRIBUTES are
    // the contract.)
    expect(xml).toContain('elseif="1"');
    expect(xml).toContain('else="1"');
    workspace.dispose();
  });

  it('an XML mutation with no else drops the ELSE input again', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    block.plus('ELSE');
    block.domToMutation(
      Blockly.utils.xml.textToDom('<mutation elseif="0"></mutation>'),
    );
    expect(inputNames(block)).toEqual(['IF0', 'DO0', 'ELSE_ADD']);
    expect(block.mutationToDom()).toBeNull();
    workspace.dispose();
  });

  // ---------------------------------------------------------------- fixtures
  it('the fixtures really are what Blockly writes (guards the proofs above)', () => {
    // A fixture that no writer ever produces would make proofs 1-4 vacuous.
    const perFixture = [IF_PLAIN, IF_WITH_ELSE, IF_ELSEIF_ELSE, IF_ELSEIF_NO_ELSE];
    perFixture.forEach((fixture) => {
      const { blockCount } = roundTrip(fixture);
      expect(blockCount).toBeGreaterThan(0);
    });
    expect(ALL_FIXTURES).toHaveLength(4);
  });
});
