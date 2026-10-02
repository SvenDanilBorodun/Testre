// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// A card that covers the whole stage when there is nothing to record into:
// no connection, still connecting, an inference running, or a collision (the
// CollisionModal stays the place to act; this card only says what happened,
// spec §3.16). Title and body are the page's German sentences.

import React from 'react';
import Icon from '../icons/Icon';

const ICONS = { offline: 'unplugged', connecting: 'loading', inference: 'box', collision: 'warning' };

export default function StageCard({ kind, title, body = '' }) {
  if (!kind) return null;
  const icon = ICONS[kind] || 'info';
  return (
    <div className="rec-statecard" role="status" data-testid="rec-stage-card" data-kind={kind}>
      <Icon
        name={icon}
        size={40}
        strokeWidth={1.6}
        className={kind === 'connecting' ? 'rec-card-icon animate-spin' : 'rec-card-icon'}
      />
      <h2>{title}</h2>
      {body ? <p>{body}</p> : null}
    </div>
  );
}
