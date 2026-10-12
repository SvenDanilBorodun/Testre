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
//   * The menu opens toward the INSIDE of the viewport: measured on every open
//     (8 px margin), a menu that would leave the right edge is right-aligned
//     (and a right-aligned one leaving the left edge left-aligned), and one
//     that would leave the bottom opens upwards when the trigger has room
//     above it (and the mirror for `placement="up"`). While open the chevron
//     follows the placement it resolved to. A menu with a zero-size box (not
//     laid out, as in jsdom) is unmeasurable and is never flipped.
//   * `describedBy` names an element that says why the trigger is disabled
//     (its aria-describedby); the accessible name stays the label.

import React, {
  useCallback, useEffect, useId, useLayoutEffect, useRef, useState,
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
// The menu keeps this far from the viewport's edges.
const VIEWPORT_MARGIN_PX = 8;

// Where a menu measured at `menu` (and its trigger at `trigger`) opens so that
// it stays inside the viewport, starting from the props' align/placement.
function insidePlacement(menu, trigger, align, placement) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let nextAlign = align;
  if (align === 'left' && menu.right > vw - VIEWPORT_MARGIN_PX) nextAlign = 'right';
  else if (align === 'right' && menu.left < VIEWPORT_MARGIN_PX) nextAlign = 'left';
  let nextPlacement = placement;
  if (trigger) {
    if (placement === 'down' && menu.bottom > vh - VIEWPORT_MARGIN_PX && trigger.top >= menu.height) {
      nextPlacement = 'up';
    } else if (placement === 'up' && menu.top < VIEWPORT_MARGIN_PX && vh - trigger.bottom >= menu.height) {
      nextPlacement = 'down';
    }
  }
  return { align: nextAlign, placement: nextPlacement };
}

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
  describedBy,
}) {
  const sized = SIZE[size] || SIZE.md;
  const menuId = useId();
  const wrapRef = useRef(null);
  const triggerRef = useRef(null);
  const itemRefs = useRef([]);
  const menuRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  // Where the open menu really sits: the props, flipped inward on measurement.
  const [resolved, setResolved] = useState({ align, placement });

  const openAt = useCallback((index) => {
    if (disabled || items.length === 0) return;
    setFocusIndex(index);
    // Every open starts from the props, so the measurement below sees the
    // menu where the caller asked for it.
    setResolved({ align, placement });
    setOpen(true);
  }, [disabled, items.length, align, placement]);

  // Measure the menu as it opens (before paint) and flip it inward.
  useLayoutEffect(() => {
    if (!open) return;
    const menu = menuRef.current;
    if (!menu || typeof menu.getBoundingClientRect !== 'function' || typeof window === 'undefined') return;
    const rect = menu.getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) return;
    const trigger = triggerRef.current && typeof triggerRef.current.getBoundingClientRect === 'function'
      ? triggerRef.current.getBoundingClientRect()
      : null;
    const next = insidePlacement(rect, trigger, align, placement);
    setResolved((prev) => (prev.align === next.align && prev.placement === next.placement ? prev : next));
    // Measured once per open; the props are the start of that measurement.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

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
        aria-describedby={describedBy || undefined}
        disabled={disabled}
        title={title}
        onClick={() => (open ? close(false) : openAt(0))}
        onKeyDown={onTriggerKeyDown}
        onKeyUp={onTriggerKeyUp}
        className={`inline-flex items-center gap-1.5 ${buttonClassName}`.trim()}
      >
        {icon && <Icon name={icon} />}
        <span>{label}</span>
        <Icon name={CHEVRON_ICON[open ? resolved.placement : placement] || CHEVRON_ICON.down} size="0.85em" />
      </button>
      {open && (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <ul
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={menuLabel || label}
          onKeyDown={onMenuKeyDown}
          onKeyUp={onMenuKeyUp}
          className={
            `absolute z-30 ${sized.menu} rounded-md border border-[var(--line)] bg-white py-1 shadow-lg ${
              PLACEMENT[resolved.placement] || PLACEMENT.down} ${ALIGN[resolved.align] || ALIGN.left}`
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
