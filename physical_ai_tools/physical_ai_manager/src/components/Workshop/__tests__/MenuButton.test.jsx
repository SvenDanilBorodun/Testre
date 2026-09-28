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

  test('placement up opens above the trigger', () => {
    const { trigger } = setup({ placement: 'up' });
    fireEvent.click(trigger);
    expect(screen.getByRole('menu').className).toMatch(/bottom-full/);
  });
});
