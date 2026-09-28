/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The toolbar's compact „Öffnen" control: the TemplatePicker (a card list of
// templates + own workflows) in a popover, so the band stays a single slim row.
//
// It behaves like every Roboter-Studio popover (usePopoverDismiss.js, owner
// decision R1-O3): a pointerdown outside — a click into Blockly included,
// heard in the capture phase — or focus leaving closes it; Esc closes it and
// returns focus to the button; opening moves focus to the first choice (once
// the list has loaded), ArrowUp/ArrowDown/Home/End move between the choices.
//
// `lockedReason` (a program runs, R2-O3): the button is disabled and says why;
// a popover already open KEEPS its list, every choice in it disabled — so
// nothing here closes it when the lock arrives.

import React, {
  useCallback, useId, useRef, useState,
} from 'react';
import TemplatePicker from './TemplatePicker';
import Icon from '../icons/Icon';
import { DE } from './blocks/messages_de';
import usePopoverDismiss, { movePopoverFocus, usePopoverListFocus } from './usePopoverDismiss';

// Every choice in the picker is a button.
const CHOICE = 'button';

export default function OpenWorkflowPopover({ onPicked, lockedReason = null }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const ref = useRef(null);
  const triggerRef = useRef(null);
  const panelRef = useRef(null);

  const onDismiss = useCallback((reason) => {
    setOpen(false);
    if (reason === 'escape' && triggerRef.current) triggerRef.current.focus();
  }, []);
  usePopoverDismiss({ open, containerRef: ref, onClose: onDismiss });
  const focusList = usePopoverListFocus({
    open, containerRef: ref, panelRef, triggerRef, selector: CHOICE,
  });

  const onTriggerClick = () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    focusList('first');
  };

  const onTriggerKeyDown = (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    e.stopPropagation();
    if (!open) setOpen(true);
    focusList(e.key === 'ArrowUp' ? 'last' : 'first');
  };

  const onPanelKeyDown = (e) => {
    if (movePopoverFocus(panelRef.current, e.key, CHOICE)) {
      e.preventDefault();
      e.stopPropagation();
    }
  };

  return (
    <div className="relative" ref={ref}>
      <button
        ref={triggerRef}
        type="button"
        onClick={onTriggerClick}
        onKeyDown={onTriggerKeyDown}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        disabled={!!lockedReason}
        title={lockedReason || 'Vorlage oder gespeicherten Workflow öffnen'}
        className={
          'inline-flex items-center gap-1 min-h-[28px] px-3 py-1.5 rounded-md '
          + 'text-sm font-medium border border-[var(--line)] bg-white text-[var(--ink)] '
          + 'hover:bg-[var(--bg-sunk)] focus:outline-none focus-visible:ring-2 '
          + 'focus-visible:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed'
        }
      >
        <Icon name="folderOpen" />
        {DE.DOCK_OPEN_WORKFLOW}
        <Icon name="chevronDown" size="0.85em" />
      </button>
      {open && (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          ref={panelRef}
          id={panelId}
          role="dialog"
          aria-label={DE.DOCK_OPEN_WORKFLOW}
          onKeyDown={onPanelKeyDown}
          className="absolute z-30 mt-1 left-0 w-80 max-h-[60vh] overflow-auto rounded-md border border-[var(--line)] bg-white shadow-lg p-3"
        >
          <TemplatePicker
            lockedReason={lockedReason}
            onPicked={(wf) => {
              setOpen(false);
              if (onPicked) onPicked(wf);
            }}
          />
        </div>
      )}
    </div>
  );
}
