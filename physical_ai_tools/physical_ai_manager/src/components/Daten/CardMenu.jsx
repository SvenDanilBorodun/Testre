// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// A card's ⋮ menu (spec §G8): `role="menu"` with `menuitem`s, the house
// dismissal (components/Workshop/usePopoverDismiss: a pointerdown outside,
// focus leaving, Esc back to the trigger) and arrow-key focus. „Auf Hugging
// Face ansehen" is a real link into a new tab, `rel="noopener noreferrer"`,
// never carrying a token (§G14).

import React, { useCallback, useEffect, useRef, useState } from 'react';
import Icon from '../icons/Icon';
import usePopoverDismiss, { movePopoverFocus } from '../Workshop/usePopoverDismiss';
import { releasePointerFocus } from '../Record/ActionBar';
import COPY from '../../features/editDataset/datenCopy';

const ITEM = '[data-popover-item]';

export default function CardMenu({ items, onSelect, onOpenChange }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);
  const triggerRef = useRef(null);
  const menuRef = useRef(null);

  const setOpenState = useCallback((v) => {
    setOpen(v);
    if (onOpenChange) onOpenChange(v);
  }, [onOpenChange]);

  const close = useCallback((reason) => {
    setOpenState(false);
    if (reason === 'escape' && triggerRef.current) triggerRef.current.focus();
  }, [setOpenState]);

  usePopoverDismiss({ open, containerRef: wrapRef, onClose: close });

  useEffect(() => {
    if (!open || !menuRef.current) return;
    const first = menuRef.current.querySelector(`${ITEM}:not([disabled])`);
    if (first) first.focus();
  }, [open]);

  if (!items || items.length === 0) return null;
  return (
    <div className="dat-menu-wrap" ref={wrapRef}>
      <button
        ref={triggerRef}
        type="button"
        className="dat-icon-btn"
        aria-label={COPY.card.more}
        title={COPY.card.more}
        aria-haspopup="menu"
        aria-expanded={open ? 'true' : 'false'}
        onClick={(e) => { releasePointerFocus(e); setOpenState(!open); }}
      >
        <Icon name="moreVertical" size={16} />
      </button>
      {open ? (
        <div
          ref={menuRef}
          className="dat-menu"
          role="menu"
          tabIndex={-1}
          onKeyDown={(e) => {
            if (movePopoverFocus(menuRef.current, e.key)) {
              e.preventDefault();
              e.stopPropagation();
            }
          }}
        >
          {items.map((it, i) => {
            // eslint-disable-next-line react/no-array-index-key
            if (it.sep) return <hr key={`sep-${i}`} />;
            const body = (
              <>
                <Icon name={it.icon} size={16} />
                <span>
                  {it.label}
                  {it.small ? <small>{it.small}</small> : null}
                </span>
              </>
            );
            if (it.href) {
              return (
                <a
                  key={it.id}
                  role="menuitem"
                  data-popover-item=""
                  href={it.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={it.title}
                  onClick={() => close('select')}
                >
                  {body}
                </a>
              );
            }
            return (
              <button
                key={it.id}
                type="button"
                role="menuitem"
                data-popover-item=""
                className={it.danger ? 'dat-danger' : undefined}
                disabled={!!it.disabled}
                onClick={(e) => {
                  releasePointerFocus(e);
                  close('select');
                  // A keyboard choice puts the focus back on ⋮ first, so a
                  // dialog it opens returns the focus there (§G8); a mouse
                  // choice leaves no button focused (Space must never re-press).
                  if (!(e.detail > 0) && triggerRef.current) triggerRef.current.focus();
                  onSelect(it.id);
                }}
              >
                {body}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
