/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The toolbox heading item („Meine Sammlung", „Bausteine") in a REAL Blockly
// inject: it draws its text, is never selectable, keyboard navigation skips it,
// and registering it twice changes nothing.

import * as Blockly from 'blockly/core';
import { registerToolboxHeading, TOOLBOX_HEADING_KIND } from '../ToolboxHeading';

const BLOCK_TYPE = 'edu_heading_test_block';

let host;
let ws;

beforeAll(() => {
  if (!Blockly.Blocks[BLOCK_TYPE]) {
    Blockly.defineBlocksWithJsonArray([{
      type: BLOCK_TYPE, message0: 'x', previousStatement: null, nextStatement: null,
    }]);
  }
});

beforeEach(() => {
  registerToolboxHeading();
  host = document.createElement('div');
  document.body.appendChild(host);
  ws = Blockly.inject(host, {
    toolbox: {
      kind: 'categoryToolbox',
      contents: [
        { kind: 'category', name: 'Eins', contents: [{ kind: 'block', type: BLOCK_TYPE }] },
        { kind: TOOLBOX_HEADING_KIND, text: 'Meine Sammlung' },
        { kind: 'category', name: 'Zwei', contents: [{ kind: 'block', type: BLOCK_TYPE }] },
      ],
    },
  });
});

afterEach(() => {
  ws.dispose();
  host.remove();
});

describe('ToolboxHeading', () => {
  it('renders its text in an aria-hidden container between the categories', () => {
    // eslint-disable-next-line testing-library/no-node-access
    const heading = host.querySelector('.eduToolboxHeading');
    expect(heading).not.toBeNull();
    expect(heading.textContent).toBe('Meine Sammlung');
    expect(heading.getAttribute('aria-hidden')).toBe('true');
    // eslint-disable-next-line testing-library/no-node-access
    expect(heading.querySelector('.eduToolboxHeadingLabel').textContent).toBe('Meine Sammlung');
    const items = ws.getToolbox().getToolboxItems();
    expect(items).toHaveLength(3);
    expect(items[1]).toBeInstanceOf(Blockly.ToolboxSeparator);
  });

  it('is never selectable', () => {
    const toolbox = ws.getToolbox();
    const heading = toolbox.getToolboxItems()[1];
    expect(heading.isSelectable()).toBe(false);
    toolbox.selectItemByPosition(1);
    expect(toolbox.getSelectedItem()).toBeNull();
  });

  it('is skipped by the arrow keys in both directions', () => {
    const toolbox = ws.getToolbox();
    // eslint-disable-next-line testing-library/no-node-access
    const contents = host.querySelector('.blocklyToolboxCategoryGroup');
    const press = (key) => contents.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
    toolbox.selectItemByPosition(0);
    expect(toolbox.getSelectedItem().getName()).toBe('Eins');
    press('ArrowDown');
    expect(toolbox.getSelectedItem().getName()).toBe('Zwei');
    press('ArrowUp');
    expect(toolbox.getSelectedItem().getName()).toBe('Eins');
  });

  it('registers once: a second call is a no-op', () => {
    const Registered = Blockly.registry.getClass(Blockly.registry.Type.TOOLBOX_ITEM, TOOLBOX_HEADING_KIND);
    expect(() => registerToolboxHeading()).not.toThrow();
    expect(Blockly.registry.getClass(Blockly.registry.Type.TOOLBOX_ITEM, TOOLBOX_HEADING_KIND)).toBe(Registered);
  });
});
