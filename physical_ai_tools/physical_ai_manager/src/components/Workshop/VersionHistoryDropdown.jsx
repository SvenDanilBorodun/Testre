/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, {
  useCallback, useEffect, useId, useRef, useState,
} from 'react';
import { useSelector } from 'react-redux';
import toast from 'react-hot-toast';
import {
  listWorkflowVersions,
  restoreWorkflowVersion,
} from '../../services/workflowApi';
import { DE } from './blocks/messages_de';
import Icon from '../icons/Icon';
import usePopoverDismiss, { movePopoverFocus, usePopoverListFocus } from './usePopoverDismiss';

function fmtTs(iso) {
  if (!iso) return '–';
  try {
    return new Date(iso).toLocaleString('de-DE');
  } catch (e) {
    return iso;
  }
}

/**
 * "Verlauf" (history) dropdown — lists the last 20 saved snapshots of
 * the active workflow's blockly_json and lets the student restore one.
 * The restore endpoint (`POST /workflows/{id}/versions/{vid}/restore`)
 * itself triggers a fresh snapshot of the current state, so restoring
 * is reversible until the 20-cap rotates the snapshot out.
 *
 * The button is disabled when no workflow is selected (i.e. the editor
 * is on local-only autosave state). When clicked, it fetches the list
 * lazily and renders a popover.
 */
// `lockedReason` (a program runs, R2-O3): a restore replaces the open
// document, so it is refused BEFORE the cloud is asked — a restore the page
// then refused to show would leave the cloud row and the editor apart. The
// history button is disabled then; a list opened before the program started
// stays clickable and each „laden" says why (review round 3, MB2f — the same
// as the „Neu" menu). `onRestoringChange(bool)` tells the page a restore is
// on its way, so no run starts under it (nb2). ONE restore at a time (review
// round 4, mc8): while one is on its way every other „laden" is disabled and
// says why, and a click that arrives anyway is refused — two overlapping
// restores told the page [true, true, false] and unlocked it mid-restore.
//
// The popover behaves like every Roboter-Studio popover (usePopoverDismiss.js,
// owner decision R1-O3): a pointerdown outside — a click into Blockly
// included, heard in the capture phase — or focus leaving closes it; Esc closes
// it and returns focus to the button; opening moves focus to the newest
// „laden" once the list has loaded, and ArrowUp/ArrowDown/Home/End move between
// the „laden" buttons. Those buttons, their titles and every refusal above are
// unchanged; nothing here closes a list the lock arrives under.
function VersionHistoryDropdown({
  workflowId, onRestore, lockedReason = null, onRestoringChange = null,
}) {
  const accessToken = useSelector((s) => s.auth?.session?.access_token);
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [restoringId, setRestoringId] = useState(null);
  // Set synchronously by the click, before React re-renders the buttons.
  const inFlightRef = useRef(false);
  // Audit §verhist-r1: without an outside-click close the popover hung around
  // after the student clicked the workspace. usePopoverDismiss hears that
  // click in the capture phase (Blockly swallows it before it bubbles).
  const containerRef = useRef(null);
  const triggerRef = useRef(null);
  const panelRef = useRef(null);
  const panelId = useId();

  const disabled = !workflowId || !accessToken || !!lockedReason;

  const onDismiss = useCallback((reason) => {
    setOpen(false);
    if (reason === 'escape' && triggerRef.current) triggerRef.current.focus();
  }, []);
  usePopoverDismiss({ open, containerRef, onClose: onDismiss });
  const focusList = usePopoverListFocus({
    open, containerRef, panelRef, triggerRef,
  });

  const refresh = useCallback(async () => {
    if (!workflowId || !accessToken) return;
    setLoading(true);
    try {
      const rows = await listWorkflowVersions(accessToken, workflowId);
      setVersions(Array.isArray(rows) ? rows : []);
    } catch (e) {
      toast.error(`${DE.VERSION_HISTORY}: ${e.message || e}`);
    } finally {
      setLoading(false);
    }
  }, [accessToken, workflowId]);

  useEffect(() => {
    if (open) refresh();
  }, [open, refresh]);

  const handleRestore = useCallback(
    async (versionId) => {
      if (!accessToken || !workflowId) return;
      if (lockedReason) {
        toast.error(lockedReason);
        return;
      }
      if (inFlightRef.current) {
        toast.error(DE.VERSION_RESTORE_IN_FLIGHT);
        return;
      }
      inFlightRef.current = true;
      setRestoringId(versionId);
      if (typeof onRestoringChange === 'function') onRestoringChange(true);
      try {
        const updated = await restoreWorkflowVersion(
          accessToken,
          workflowId,
          versionId,
        );
        if (typeof onRestore === 'function') {
          onRestore(updated);
        }
        toast.success('Version wiederhergestellt.');
        setOpen(false);
      } catch (e) {
        toast.error(`Wiederherstellen fehlgeschlagen: ${e.message || e}`);
      } finally {
        inFlightRef.current = false;
        setRestoringId(null);
        if (typeof onRestoringChange === 'function') onRestoringChange(false);
      }
    },
    [accessToken, workflowId, onRestore, lockedReason, onRestoringChange]
  );

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
    if (movePopoverFocus(panelRef.current, e.key)) {
      e.preventDefault();
      e.stopPropagation();
    }
  };

  return (
    <div className="relative inline-block" ref={containerRef}>
      <button
        ref={triggerRef}
        type="button"
        onClick={onTriggerClick}
        onKeyDown={onTriggerKeyDown}
        disabled={disabled}
        title={lockedReason || undefined}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? panelId : undefined}
        className={
          'inline-flex items-center justify-center gap-1.5 min-h-[28px] '
          + 'px-3 py-1.5 rounded-md text-sm font-medium border border-[var(--line)] '
          + 'bg-white text-[var(--ink)] hover:bg-[var(--bg-sunk)] '
          + 'disabled:opacity-50 disabled:cursor-not-allowed '
          + 'focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500'
        }
      >
        <Icon name="history" />
        {DE.VERSION_HISTORY}
      </button>
      {open && (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          ref={panelRef}
          id={panelId}
          role="dialog"
          aria-label={DE.VERSION_HISTORY}
          onKeyDown={onPanelKeyDown}
          className="absolute right-0 mt-1 z-10 w-72 bg-white border border-[var(--line)] rounded-md shadow-lg max-h-80 overflow-auto"
        >
          {loading ? (
            <p className="text-sm text-[var(--ink-3)] p-3">…</p>
          ) : versions.length === 0 ? (
            <p className="text-sm text-[var(--ink-3)] p-3">
              {DE.VERSION_NONE}
            </p>
          ) : (
            <ul className="divide-y divide-[var(--line)]">
              {versions.map((v) => (
                <li
                  key={v.id}
                  className="flex items-center justify-between gap-2 px-3 py-2 hover:bg-[var(--bg-sunk)]"
                >
                  <span className="text-xs text-[var(--ink)] font-mono">
                    {fmtTs(v.created_at)}
                  </span>
                  <button
                    type="button"
                    data-popover-item=""
                    onClick={() => handleRestore(v.id)}
                    disabled={restoringId !== null}
                    title={lockedReason
                      || (restoringId !== null && restoringId !== v.id ? DE.VERSION_RESTORE_IN_FLIGHT : undefined)}
                    className="text-xs text-blue-600 hover:underline disabled:opacity-50"
                  >
                    {restoringId === v.id ? '…' : DE.VERSION_LOAD}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export default VersionHistoryDropdown;
