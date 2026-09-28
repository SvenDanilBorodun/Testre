/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// A WAI-ARIA menu button: the toolbar's „Vormachen" chooser (one entry per
// kind, owner decision D4) and the code sidebar's „+ Neu".
//
//   * Trigger: aria-haspopup="menu", aria-expanded, aria-controls. A click, Enter,
//     Space or ArrowDown opens the menu with the first item focused; ArrowUp
//     with the last.
//   * Menu: role="menu" of role="menuitem" buttons (tabIndex -1, roving focus):
//     ArrowUp/ArrowDown wrap, Home/End jump, Enter/Space choose, Esc closes and
//     returns focus to the trigger, Tab closes. A held Enter or Space (key
//     auto-repeat) never chooses: the press that opened the menu must not also
//     pick its first item.
//   * Dismissal is usePopoverDismiss.js, shared with „Öffnen" and „Verlauf": a
//     pointerdown outside — heard in the CAPTURE phase, because Blockly's
//     gesture stops a click into the workspace from ever bubbling — and focus
//     moving outside both close it.
//   * Choosing closes the menu, focuses the TRIGGER, then calls the item's
//     onSelect — so a dialog the choice opens (Vormachen) restores focus to it.
//   * Every key it handles is stopped here: Blockly's document-level shortcuts
//     must never see an arrow meant for this menu.

import React, {
  useCallback, useEffect, useId, useRef, useState,
} from 'react';
import Icon from '../icons/Icon';
import usePopoverDismiss from './usePopoverDismiss';

const PLACEMENT = { down: 'top-full mt-1', up: 'bottom-full mb-1' };
// The menu's box and its items: `md` for the toolbar, `sm` for a narrow
// sidebar — small text on one line, the menu as wide as its longest item but
// never narrower than the trigger (review round 1, B5: „Ziel in der Kamera
// setzen" wrapped in the 11rem code sidebar).
const SIZE = {
  md: { menu: 'min-w-[12rem]', item: 'px-3 py-1.5 text-sm' },
  sm: { menu: 'min-w-full w-max', item: 'px-2.5 py-1.5 text-xs whitespace-nowrap' },
};
// The chevron points where the menu opens.
const CHEVRON_ICON = { down: 'chevronDown', up: 'chevronUp' };
const ALIGN = { left: 'left-0', right: 'right-0' };

export default function MenuButton({
  label,
  icon = null,
  items = [],
  menuLabel,
  disabled = false,
  title,
  placement = 'down',
  align = 'left',
  size = 'md',
  className = '',
  buttonClassName = '',
}) {
  const sized = SIZE[size] || SIZE.md;
  const menuId = useId();
  const wrapRef = useRef(null);
  const triggerRef = useRef(null);
  const itemRefs = useRef([]);
  const [open, setOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);

  const openAt = useCallback((index) => {
    if (disabled || items.length === 0) return;
    setFocusIndex(index);
    setOpen(true);
  }, [disabled, items.length]);

  const close = useCallback((refocus) => {
    setOpen(false);
    if (refocus && triggerRef.current) triggerRef.current.focus();
  }, []);

  // Roving focus: the focused item follows focusIndex while open.
  useEffect(() => {
    if (!open) return;
    const el = itemRefs.current[focusIndex];
    if (el) el.focus();
  }, [open, focusIndex]);

  // Outside pointerdown or focus leaving: close without taking focus
  // anywhere. Esc: close and hand focus back to the trigger.
  const onDismiss = useCallback((reason) => close(reason === 'escape'), [close]);
  usePopoverDismiss({ open, containerRef: wrapRef, onClose: onDismiss });

  // A trigger that becomes disabled takes its menu with it.
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const choose = (item) => {
    close(true);
    if (item && typeof item.onSelect === 'function') item.onSelect();
  };

  const onTriggerKeyDown = (e) => {
    if (disabled) return;
    if (e.repeat && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
      e.preventDefault();
      e.stopPropagation();
      openAt(0);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      e.stopPropagation();
      openAt(items.length - 1);
    }
  };

  // Space activates a button on keyup in some engines; the keydown opened it.
  const onTriggerKeyUp = (e) => {
    if (e.key === ' ') e.preventDefault();
  };

  const onMenuKeyDown = (e) => {
    const n = items.length;
    const move = (index) => {
      e.preventDefault();
      e.stopPropagation();
      setFocusIndex(((index % n) + n) % n);
    };
    switch (e.key) {
      case 'ArrowDown': move(focusIndex + 1); break;
      case 'ArrowUp': move(focusIndex - 1); break;
      case 'Home': move(0); break;
      case 'End': move(n - 1); break;
      // Esc is usePopoverDismiss's: it closes and hands focus back.
      case 'Tab':
        e.stopPropagation();
        setOpen(false);
        break;
      case 'Enter':
      case ' ': {
        e.preventDefault();
        e.stopPropagation();
        // Auto-repeat of the key that opened the menu: never a choice.
        if (e.repeat) break;
        const index = itemRefs.current.indexOf(e.target);
        choose(items[index >= 0 ? index : focusIndex]);
        break;
      }
      default:
        break;
    }
  };

  const onMenuKeyUp = (e) => {
    if (e.key === ' ') e.preventDefault();
  };

  return (
    <div ref={wrapRef} className={`relative ${className}`.trim()}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled}
        title={title}
        onClick={() => (open ? close(false) : openAt(0))}
        onKeyDown={onTriggerKeyDown}
        onKeyUp={onTriggerKeyUp}
        className={`inline-flex items-center gap-1.5 ${buttonClassName}`.trim()}
      >
        {icon && <Icon name={icon} />}
        <span>{label}</span>
        <Icon name={CHEVRON_ICON[placement] || CHEVRON_ICON.down} size="0.85em" />
      </button>
      {open && (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <ul
          id={menuId}
          role="menu"
          aria-label={menuLabel || label}
          onKeyDown={onMenuKeyDown}
          onKeyUp={onMenuKeyUp}
          className={
            `absolute z-30 ${sized.menu} rounded-md border border-[var(--line)] bg-white py-1 shadow-lg ${
              PLACEMENT[placement] || PLACEMENT.down} ${ALIGN[align] || ALIGN.left}`
          }
        >
          {items.map((item, i) => (
            <li key={item.id} role="none">
              <button
                ref={(el) => { itemRefs.current[i] = el; }}
                type="button"
                role="menuitem"
                tabIndex={-1}
                onClick={() => choose(item)}
                className={
                  `flex w-full items-center gap-2 ${sized.item} text-left text-[var(--ink)] `
                  + 'hover:bg-[var(--bg-sunk)] focus:bg-[var(--bg-sunk)] focus:outline-none'
                }
              >
                {item.icon && <Icon name={item.icon} className="text-[var(--ink-3)]" />}
                <span>{item.label}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
