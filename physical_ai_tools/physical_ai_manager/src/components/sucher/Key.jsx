// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// A key (Sucher §4.3): the rectangular button of the device. Variants
// default / signal / dark / flat / danger, sizes sm / md / lg; an optional
// leading icon and a key label (`kbd`, e.g. „Leertaste"). A mouse click gives
// the focus back to the page (D11); a keyboard click keeps it.

import React, { forwardRef } from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';
import { releasePointerFocus } from './pointerFocus';

const VARIANTS = new Set(['default', 'signal', 'dark', 'flat', 'danger']);
const SIZES = new Set(['sm', 'md', 'lg']);

const Key = forwardRef(function Key({
  variant = 'default',
  size = 'md',
  icon,
  kbd,
  type = 'button',
  className,
  children,
  onClick,
  ...rest
}, ref) {
  const v = VARIANTS.has(variant) ? variant : 'default';
  const s = SIZES.has(size) ? size : 'md';
  return (
    <button
      {...rest}
      ref={ref}
      type={type}
      className={clsx('su-key', `su-key--${v}`, `su-key--${s}`, className)}
      onClick={(e) => {
        releasePointerFocus(e);
        if (onClick) onClick(e);
      }}
    >
      {icon ? <Icon name={icon} size={16} /> : null}
      {children}
      {kbd ? <kbd className="su-kbd">{kbd}</kbd> : null}
    </button>
  );
});

export default Key;
