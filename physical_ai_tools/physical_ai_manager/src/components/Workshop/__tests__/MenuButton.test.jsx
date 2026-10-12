/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// MenuButton — the WAI-ARIA menu button behind the toolbar's „Vormachen"
// chooser and the code sidebar's „+ Neu": keyboard, focus and dismissal.

import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import MenuButton from '../MenuButton';

function setup(over = {}) {
  const onSelect = { a: vi.fn(), b: vi.fn(), c: vi.fn() };
  const items = [
    { id: 'a', label: 'Bewegung vormachen', icon: 'record', onSelect: onSelect.a },
    { id: 'b', label: 'Position vormachen', icon: 'pose', onSelect: onSelect.b },
    { id: 'c', label: 'Ziel vormachen', icon: 'ziel', onSelect: onSelect.c },
  ];
  const outsideKeys = vi.fn();
  const view = render(
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div onKeyDown={outsideKeys}>
      <MenuButton label="Vormachen" icon="hand" items={items} menuLabel="Was möchtest du vormachen?" {...over} />
      <button type="button">Danach</button>
    </div>,
  );
  const trigger = screen.getByRole('button', { name: /Vormachen$/ });
  return { ...view, onSelect, trigger, outsideKeys };
}

const menu = () => screen.queryByRole('menu');
const menuItems = () => within(screen.getByRole('menu')).getAllByRole('menuitem');

describe('MenuButton', () => {
  test('the trigger announces a closed menu it controls', () => {
    const { trigger } = setup();
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(menu()).toBeNull();
  });

  test('a click opens the labelled menu, focuses the first item and wires aria-controls', () => {
    const { trigger } = setup();
    fireEvent.click(trigger);
    const m = screen.getByRole('menu', { name: 'Was möchtest du vormachen?' });
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(trigger.getAttribute('aria-controls')).toBe(m.id);
    expect(menuItems().map((i) => i.textContent)).toEqual([
      'Bewegung vormachen', 'Position vormachen', 'Ziel vormachen',
    ]);
    for (const item of menuItems()) expect(item).toHaveAttribute('tabindex', '-1');
    expect(menuItems()[0]).toHaveFocus();
  });

  test.each([['Enter'], [' '], ['ArrowDown']])('%j on the trigger opens and focuses the first item', (key) => {
    const { trigger } = setup();
    trigger.focus();
    fireEvent.keyDown(trigger, { key });
    expect(menu()).not.toBeNull();
    expect(menuItems()[0]).toHaveFocus();
  });

  test('ArrowUp on the trigger opens and focuses the last item', () => {
    const { trigger } = setup();
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'ArrowUp' });
    expect(menuItems()[2]).toHaveFocus();
  });

  test('ArrowDown/ArrowUp wrap, Home/End jump', () => {
    const { trigger } = setup();
    fireEvent.click(trigger);
    const [a, b, c] = menuItems();
    fireEvent.keyDown(a, { key: 'ArrowDown' });
    expect(b).toHaveFocus();
    fireEvent.keyDown(b, { key: 'ArrowDown' });
    fireEvent.keyDown(c, { key: 'ArrowDown' });
    expect(a).toHaveFocus();
    fireEvent.keyDown(a, { key: 'ArrowUp' });
    expect(c).toHaveFocus();
    fireEvent.keyDown(c, { key: 'Home' });
    expect(a).toHaveFocus();
    fireEvent.keyDown(a, { key: 'End' });
    expect(c).toHaveFocus();
  });

  test('choosing closes the menu, focuses the trigger FIRST, then calls onSelect once', () => {
    const { trigger, onSelect } = setup();
    let focusedAtSelect = null;
    // eslint-disable-next-line testing-library/no-node-access
    onSelect.b.mockImplementation(() => { focusedAtSelect = document.activeElement; });
    fireEvent.click(trigger);
    fireEvent.click(menuItems()[1]);
    expect(onSelect.b).toHaveBeenCalledTimes(1);
    expect(onSelect.a).not.toHaveBeenCalled();
    expect(focusedAtSelect).toBe(trigger);
    expect(menu()).toBeNull();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  test.each([['Enter'], [' ']])('%j on an item chooses it', (key) => {
    const { trigger, onSelect } = setup();
    fireEvent.click(trigger);
    fireEvent.keyDown(menuItems()[0], { key: 'ArrowDown' });
    fireEvent.keyDown(menuItems()[1], { key });
    expect(onSelect.b).toHaveBeenCalledTimes(1);
    expect(trigger).toHaveFocus();
  });

  test('Esc closes and returns focus to the trigger without choosing', () => {
    const { trigger, onSelect } = setup();
    fireEvent.click(trigger);
    fireEvent.keyDown(menuItems()[0], { key: 'Escape' });
    expect(menu()).toBeNull();
    expect(trigger).toHaveFocus();
    Object.values(onSelect).forEach((fn) => expect(fn).not.toHaveBeenCalled());
  });

  test('Tab closes the menu without choosing', () => {
    const { trigger, onSelect } = setup();
    fireEvent.click(trigger);
    fireEvent.keyDown(menuItems()[0], { key: 'Tab' });
    expect(menu()).toBeNull();
    Object.values(onSelect).forEach((fn) => expect(fn).not.toHaveBeenCalled());
  });

  test('a pointerdown outside closes it; one inside does not', () => {
    const { trigger } = setup();
    fireEvent.click(trigger);
    fireEvent.pointerDown(menuItems()[1]);
    expect(menu()).not.toBeNull();
    fireEvent.pointerDown(document.body);
    expect(menu()).toBeNull();
  });

  test('handled keys never reach the page behind (Blockly shortcuts)', () => {
    const { trigger, outsideKeys } = setup();
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    for (const key of ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape']) {
      fireEvent.keyDown(document.activeElement, { key }); // eslint-disable-line testing-library/no-node-access
      if (key === 'Escape') break;
    }
    expect(outsideKeys).not.toHaveBeenCalled();
  });

  test('a second click on the trigger closes it again', () => {
    const { trigger } = setup();
    fireEvent.click(trigger);
    fireEvent.click(trigger);
    expect(menu()).toBeNull();
  });

  test('disabled: the reason is the title, and nothing opens', () => {
    const { trigger } = setup({ disabled: true, title: 'Keine Verbindung zum Roboter-Dienst.' });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute('title', 'Keine Verbindung zum Roboter-Dienst.');
    fireEvent.click(trigger);
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    expect(menu()).toBeNull();
  });

  test('becoming disabled while open closes the menu', () => {
    const { trigger, rerender, onSelect } = setup();
    fireEvent.click(trigger);
    expect(menu()).not.toBeNull();
    rerender(
      <div>
        <MenuButton
          label="Vormachen"
          items={[{ id: 'a', label: 'Bewegung vormachen', onSelect: onSelect.a }]}
          menuLabel="Was möchtest du vormachen?"
          disabled
        />
      </div>,
    );
    expect(menu()).toBeNull();
  });

  test('each item and the trigger draw their icon, decorative', () => {
    const { trigger, container } = setup();
    // eslint-disable-next-line testing-library/no-node-access, testing-library/no-container
    expect(container.querySelector('button[aria-haspopup] svg[data-icon="hand"]')).not.toBeNull();
    fireEvent.click(trigger);
    // eslint-disable-next-line testing-library/no-node-access
    const icons = menuItems().map((i) => i.querySelector('svg'));
    expect(icons.map((svg) => svg.getAttribute('data-icon'))).toEqual(['record', 'pose', 'ziel']);
    icons.forEach((svg) => expect(svg).toHaveAttribute('aria-hidden', 'true'));
  });

  test('placement up opens above the trigger, and its chevron points up', () => {
    const { trigger, container } = setup({ placement: 'up' });
    // eslint-disable-next-line testing-library/no-node-access
    expect(trigger.querySelector('svg[data-icon="chevronUp"]')).not.toBeNull();
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/bottom-full/);
    // eslint-disable-next-line testing-library/no-node-access, testing-library/no-container
    expect(container.querySelector('svg[data-icon="chevronDown"]')).toBeNull();
  });

  test('placement down keeps the chevron pointing down', () => {
    const { trigger } = setup();
    // eslint-disable-next-line testing-library/no-node-access
    expect(trigger.querySelector('svg[data-icon="chevronDown"]')).not.toBeNull();
  });

  // Review round 1 (A4): holding Enter on the trigger opened the menu with the
  // first item focused, and the key's auto-repeat then CHOSE that item.
  test.each([['Enter'], [' ']])('a held %j (key auto-repeat) never chooses an item', (key) => {
    const { trigger, onSelect } = setup();
    trigger.focus();
    fireEvent.keyDown(trigger, { key });
    expect(menuItems()[0]).toHaveFocus();
    fireEvent.keyDown(menuItems()[0], { key, repeat: true });
    fireEvent.keyDown(menuItems()[0], { key, repeat: true });
    Object.values(onSelect).forEach((fn) => expect(fn).not.toHaveBeenCalled());
    expect(menu()).not.toBeNull();
    // A fresh press still chooses.
    fireEvent.keyDown(menuItems()[0], { key });
    expect(onSelect.a).toHaveBeenCalledTimes(1);
  });

  test('a repeated Enter on the closed trigger does not toggle it', () => {
    const { trigger } = setup();
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'Enter', repeat: true });
    expect(menu()).toBeNull();
  });

  test('focus moving OUTSIDE the component closes it; focus going nowhere does not', () => {
    const { trigger } = setup();
    fireEvent.click(trigger);
    // A window switch / a control disabled under the menu: no relatedTarget.
    fireEvent.focusOut(menuItems()[0], { relatedTarget: null });
    expect(menu()).not.toBeNull();
    // Inside (the trigger): stays open.
    fireEvent.focusOut(menuItems()[0], { relatedTarget: trigger });
    expect(menu()).not.toBeNull();
    fireEvent.focusOut(menuItems()[0], { relatedTarget: screen.getByRole('button', { name: 'Danach' }) });
    expect(menu()).toBeNull();
  });

  test('a pointerdown outside is heard in the capture phase, even when the target stops it', () => {
    const { trigger } = setup();
    const blocker = document.createElement('div');
    blocker.addEventListener('pointerdown', (e) => { e.stopPropagation(); e.preventDefault(); });
    document.body.appendChild(blocker);
    try {
      fireEvent.click(trigger);
      fireEvent.pointerDown(blocker);
      expect(menu()).toBeNull();
    } finally {
      blocker.remove();
    }
  });
});

// R0-11: the menu opens toward the inside of the viewport, wherever it is
// placed. jsdom lays nothing out, so the boxes are mocked; a zero-size box (the
// jsdom default) is unmeasurable and must change nothing.
describe('MenuButton — the menu stays inside the viewport', () => {
  const box = ({ left = 0, top = 0, width = 0, height = 0 }) => ({
    left, top, width, height, right: left + width, bottom: top + height, x: left, y: top,
  });
  let menuBox;
  let triggerBox;
  let spies = [];
  let innerWidth;
  let innerHeight;
  beforeEach(() => {
    menuBox = box({});
    triggerBox = box({});
    innerWidth = window.innerWidth;
    innerHeight = window.innerHeight;
    spies = [
      vi.spyOn(HTMLUListElement.prototype, 'getBoundingClientRect').mockImplementation(() => menuBox),
      vi.spyOn(HTMLButtonElement.prototype, 'getBoundingClientRect').mockImplementation(() => triggerBox),
    ];
  });
  afterEach(() => {
    spies.forEach((s) => s.mockRestore());
    window.innerWidth = innerWidth;
    window.innerHeight = innerHeight;
  });
  const chevron = (trigger, name) => trigger.querySelector(`svg[data-icon="${name}"]`); // eslint-disable-line testing-library/no-node-access

  test('describedBy lands on the trigger; without it there is no aria-describedby', () => {
    const { unmount } = setup({ describedBy: 'grund-1', disabled: true });
    expect(screen.getByRole('button', { name: /Vormachen$/ })).toHaveAttribute('aria-describedby', 'grund-1');
    unmount();
    const { trigger } = setup();
    expect(trigger).not.toHaveAttribute('aria-describedby');
  });

  test('a menu that would leave the right edge is right-aligned', () => {
    window.innerWidth = 1366;
    window.innerHeight = 768;
    menuBox = box({ left: 1234, top: 60, width: 192, height: 110 }); // right 1426
    triggerBox = box({ left: 1234, top: 20, width: 110, height: 32 });
    const { trigger } = setup();
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/\bright-0\b/);
    expect(screen.getByRole('menu').className).not.toMatch(/\bleft-0\b/);
  });

  test('a menu that fits keeps its alignment and placement', () => {
    window.innerWidth = 1366;
    window.innerHeight = 768;
    menuBox = box({ left: 933, top: 60, width: 192, height: 110 });
    triggerBox = box({ left: 933, top: 20, width: 110, height: 32 });
    const { trigger } = setup();
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/\bleft-0\b/);
    expect(screen.getByRole('menu').className).toMatch(/top-full/);
  });

  test('a right-aligned menu that would leave the left edge is left-aligned', () => {
    window.innerWidth = 1366;
    window.innerHeight = 768;
    menuBox = box({ left: -40, top: 60, width: 192, height: 110 });
    triggerBox = box({ left: 4, top: 20, width: 110, height: 32 });
    const { trigger } = setup({ align: 'right' });
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/\bleft-0\b/);
  });

  test('a down menu leaving the bottom opens upwards when there is room above, and its chevron follows', () => {
    window.innerWidth = 1366;
    window.innerHeight = 768;
    triggerBox = box({ left: 100, top: 700, width: 110, height: 32 });
    menuBox = box({ left: 100, top: 736, width: 192, height: 110 }); // bottom 846
    const { trigger } = setup();
    expect(chevron(trigger, 'chevronDown')).not.toBeNull();
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/bottom-full/);
    expect(chevron(trigger, 'chevronUp')).not.toBeNull();
    expect(chevron(trigger, 'chevronDown')).toBeNull();
    fireEvent.click(trigger);
    expect(menu()).toBeNull();
    expect(chevron(trigger, 'chevronDown')).not.toBeNull();
  });

  test('a down menu leaving the bottom stays down when there is no room above', () => {
    window.innerWidth = 1366;
    window.innerHeight = 140;
    triggerBox = box({ left: 100, top: 40, width: 110, height: 32 });
    menuBox = box({ left: 100, top: 76, width: 192, height: 110 });
    const { trigger } = setup();
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/top-full/);
  });

  test('an up menu leaving the top opens downwards when there is room below', () => {
    window.innerWidth = 1366;
    window.innerHeight = 768;
    triggerBox = box({ left: 100, top: 30, width: 110, height: 32 });
    menuBox = box({ left: 100, top: -84, width: 192, height: 110 });
    const { trigger } = setup({ placement: 'up' });
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/top-full/);
    expect(chevron(trigger, 'chevronDown')).not.toBeNull();
  });

  test('every open starts from the props again', () => {
    window.innerWidth = 1366;
    window.innerHeight = 768;
    menuBox = box({ left: 1234, top: 60, width: 192, height: 110 });
    triggerBox = box({ left: 1234, top: 20, width: 110, height: 32 });
    const { trigger } = setup();
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/\bright-0\b/);
    fireEvent.click(trigger);
    menuBox = box({ left: 400, top: 60, width: 192, height: 110 });
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/\bleft-0\b/);
  });

  test('a zero-size box (not laid out) never flips: the code sidebar\'s up/right „+ Neu" keeps bottom-full and right-0', () => {
    const { trigger } = setup({ placement: 'up', align: 'right' });
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/bottom-full/);
    expect(screen.getByRole('menu').className).toMatch(/\bright-0\b/);
    expect(chevron(trigger, 'chevronUp')).not.toBeNull();
  });
});
