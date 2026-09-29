/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Review round 1 (A1, confirmed): a click into the Blockly workspace must
// close an open MenuButton. Blockly's gesture calls stopPropagation() and
// preventDefault() on the workspace's pointerdown, so a bubble-phase listener
// on document never heard it and the menu stayed open over the editor. This
// test injects a REAL workspace and presses on its SVG, through Blockly's own
// handler.

import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import MenuButton from '../MenuButton';

const realOffset = {
  width: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth'),
  height: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight'),
};

let host;
let ws;

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get() { return 800; } });
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get() { return 600; } });
  if (!SVGElement.prototype.getBBox) SVGElement.prototype.getBBox = () => ({ x: 0, y: 0, width: 40, height: 16 });
  host = document.createElement('div');
  document.body.appendChild(host);
  ws = Blockly.inject(host, {});
});

afterEach(() => {
  Blockly.Touch.clearTouchIdentifier();
  ws.dispose();
  host.remove();
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', realOffset.width);
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', realOffset.height);
});

function pointer(type, pointerId) {
  return new PointerEvent(type, { bubbles: true, cancelable: true, pointerId, pointerType: 'mouse', button: 0 });
}

function openMenu() {
  const onSelect = vi.fn();
  render(
    <MenuButton
      label="Vormachen"
      items={[{ id: 'a', label: 'Bewegung vormachen', onSelect }]}
      menuLabel="Was möchtest du vormachen?"
    />,
  );
  fireEvent.click(screen.getByRole('button', { name: /Vormachen/ }));
  expect(screen.getByRole('menu')).toBeInTheDocument();
  return onSelect;
}

test('Blockly really swallows the workspace pointerdown (the premise of the capture listener)', () => {
  const bubble = vi.fn();
  document.addEventListener('pointerdown', bubble);
  try {
    act(() => { ws.getSvgGroup().dispatchEvent(pointer('pointerdown', 51)); });
    ws.getSvgGroup().dispatchEvent(pointer('pointerup', 51));
  } finally {
    document.removeEventListener('pointerdown', bubble);
  }
  expect(bubble).not.toHaveBeenCalled();
});

test('a pointerdown on the Blockly workspace closes the open menu, choosing nothing', () => {
  const onSelect = openMenu();
  // Hold focus where it is, so ONLY the pointerdown listener can close the
  // menu here (the focusout path has its own test below).
  const noFocus = [
    vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(() => {}),
    vi.spyOn(SVGElement.prototype, 'focus').mockImplementation(() => {}),
  ];
  const item = screen.getByRole('menuitem');
  expect(item).toHaveFocus();
  let openAfterDown;
  try {
    act(() => { ws.getSvgGroup().dispatchEvent(pointer('pointerdown', 52)); });
    // Closed by the pointerdown itself, before any focus could move.
    openAfterDown = screen.queryByRole('menu');
    ws.getSvgGroup().dispatchEvent(pointer('pointerup', 52));
  } finally {
    noFocus.forEach((spy) => spy.mockRestore());
  }
  expect(openAfterDown).toBeNull();
  expect(screen.queryByRole('menu')).toBeNull();
  expect(onSelect).not.toHaveBeenCalled();
});

test('focus moving into the Blockly workspace closes the open menu', () => {
  openMenu();
  act(() => { Blockly.getFocusManager().focusNode(ws); });
  expect(screen.queryByRole('menu')).toBeNull();
});
