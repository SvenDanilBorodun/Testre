// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// A round key (Sucher §4.4): a disc with an icon, `on` = the signal radial.
// `label` is VISIBLE text below the disc (and so the button's name);
// without it the caller must pass `aria-label`. Press, hover, focus ring and
// the pointer-focus release behave like Key's; the ring is drawn round the disc.

import React, { forwardRef } from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';
import { releasePointerFocus } from './pointerFocus';

const RoundKey = forwardRef(function RoundKey({
  size = 52,
  on = false,
  icon,
  label,
  'aria-label': ariaLabel,
  title,
  disabled,
  onClick,
  className,
  type = 'button',
  ...rest
}, ref) {
  if (!label && !ariaLabel && process.env.NODE_ENV !== 'production') {
    // eslint-disable-next-line no-console
    console.error('RoundKey: without a visible label an aria-label is required.');
  }
  const iconSize = Math.round(size * 0.38);
  return (
    <button
      {...rest}
      ref={ref}
      type={type}
      aria-label={ariaLabel}
      title={title}
      disabled={disabled}
      className={clsx('su-rkey', on && 'su-rkey--on', label && 'su-rkey--labelled', className)}
      onClick={(e) => {
        releasePointerFocus(e);
        if (onClick) onClick(e);
      }}
    >
      <span className="su-rkey__disc" style={{ width: size, height: size }}>
        {icon ? <Icon name={icon} size={iconSize} /> : null}
      </span>
      {label ? <span className="su-rkey__label">{label}</span> : null}
    </button>
  );
});

export default RoundKey;
