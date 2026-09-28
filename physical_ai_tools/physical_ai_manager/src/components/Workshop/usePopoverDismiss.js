/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// How every Roboter-Studio popover goes away — the „Vormachen" chooser and
// „+ Neu" (MenuButton), „Öffnen" (OpenWorkflowPopover) and „Verlauf"
// (VersionHistoryDropdown) — one implementation, so they cannot drift apart:
//
//   * A pointerdown OUTSIDE closes it, heard in the CAPTURE phase. Blockly's
//     gesture calls stopPropagation() and preventDefault() on the pointerdown
//     of a click into the workspace or a flyout, so a bubble-phase listener on
//     document — and any `mousedown` listener at all — never hears that click.
//   * Focus moving to an element OUTSIDE closes it (a focusout whose
//     relatedTarget lies outside). Focus going NOWHERE does not: a window
//     switch, or a trigger disabled under an open list (a program started — the
//     „Öffnen" and „Verlauf" lists stay open and say why, R2-O3), is not the
//     student leaving the popover.
//   * Esc closes it and returns focus to the trigger (the caller's
//     `onEscape`), and is stopped here so nothing behind reacts to it.
//
// The caller owns `open`; `onClose(reason)` is told why ('outside', 'focus',
// 'escape').

import { useCallback, useEffect, useRef } from 'react';

export default function usePopoverDismiss({ open, containerRef, onClose }) {
  const onCloseRef = useRef(onClose);
  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  useEffect(() => {
    if (!open) return undefined;
    const inside = (node) => {
      const root = containerRef.current;
      return !!(root && node instanceof Node && root.contains(node));
    };
    const onPointerDown = (e) => {
      if (!inside(e.target)) onCloseRef.current('outside');
    };
    const onFocusOut = (e) => {
      if (e.relatedTarget && !inside(e.relatedTarget)) onCloseRef.current('focus');
    };
    const onKeyDown = (e) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      onCloseRef.current('escape');
    };
    const root = containerRef.current;
    document.addEventListener('pointerdown', onPointerDown, true);
    if (root) {
      root.addEventListener('focusout', onFocusOut);
      root.addEventListener('keydown', onKeyDown);
    }
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      if (root) {
        root.removeEventListener('focusout', onFocusOut);
        root.removeEventListener('keydown', onKeyDown);
      }
    };
  }, [open, containerRef]);
}

const ITEM_SELECTOR = '[data-popover-item]';

function enabledItems(container, selector) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(selector || ITEM_SELECTOR)).filter((el) => !el.disabled);
}

/**
 * Roving focus over the enabled items (`selector`, default the buttons marked
 * `data-popover-item`) inside `container`: ArrowDown/ArrowUp (wrapping),
 * Home, End. Returns true when it handled the key (the caller then prevents
 * and stops it).
 */
export function movePopoverFocus(container, key, selector = ITEM_SELECTOR) {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(key)) return false;
  const items = enabledItems(container, selector);
  if (items.length === 0) return true;
  const at = items.indexOf(document.activeElement);
  let next;
  if (key === 'Home') next = 0;
  else if (key === 'End') next = items.length - 1;
  else if (key === 'ArrowDown') next = at < 0 ? 0 : (at + 1) % items.length;
  else next = at < 0 ? items.length - 1 : (at - 1 + items.length) % items.length;
  items[next].focus();
  return true;
}

/**
 * A list that arrives AFTER its popover opened („Öffnen" and „Verlauf" load
 * theirs from the cloud): `request('first' | 'last')` focuses that end of it as
 * soon as it exists — now, or on the first change inside `panelRef` — but only
 * while focus still rests on the trigger (or nowhere), so a student who has
 * moved on is never pulled back. Closing, a key or a pointerdown inside the
 * popover drops the request.
 */
export function usePopoverListFocus({
  open, containerRef, panelRef, triggerRef, selector = ITEM_SELECTOR,
}) {
  const wantRef = useRef(null);

  const tryFocus = useCallback(() => {
    const want = wantRef.current;
    if (!want) return true;
    const active = document.activeElement;
    const free = !active || active === document.body || active === triggerRef.current;
    if (!free) {
      wantRef.current = null;
      return true;
    }
    const items = enabledItems(panelRef.current, selector);
    if (items.length === 0) return false;
    (want === 'last' ? items[items.length - 1] : items[0]).focus();
    wantRef.current = null;
    return true;
  }, [panelRef, triggerRef, selector]);

  useEffect(() => {
    if (!open) {
      wantRef.current = null;
      return undefined;
    }
    if (tryFocus()) return undefined;
    const panel = panelRef.current;
    const root = containerRef.current;
    const cancel = () => { wantRef.current = null; };
    const observer = typeof MutationObserver === 'function' && panel
      ? new MutationObserver(() => { if (tryFocus()) observer.disconnect(); })
      : null;
    if (observer) observer.observe(panel, { childList: true, subtree: true, attributes: true });
    if (root) {
      root.addEventListener('pointerdown', cancel, true);
      root.addEventListener('keydown', cancel, true);
    }
    return () => {
      if (observer) observer.disconnect();
      if (root) {
        root.removeEventListener('pointerdown', cancel, true);
        root.removeEventListener('keydown', cancel, true);
      }
    };
  }, [open, tryFocus, panelRef, containerRef]);

  return useCallback((which) => {
    wantRef.current = which === 'last' ? 'last' : 'first';
    return tryFocus();
  }, [tryFocus]);
}
