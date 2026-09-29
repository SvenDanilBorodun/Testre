// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The Aufnahme page header (spec §3.12): eyebrow, the task name as the page
// title, the connection pill and the robot's German name. Presentational: the
// connection pill is handed in (HeartbeatStatus reads Redux itself).

import React from 'react';
import Icon from '../icons/Icon';

export default function RecordHeader({ eyebrow, title, connection = null, robotName = '' }) {
  return (
    <header className="rec-top">
      <div>
        <div className="rec-eyebrow">{eyebrow}</div>
        <h1 className="rec-title" title={title}>{title}</h1>
      </div>
      <div className="rec-pills">
        {connection}
        {robotName ? (
          <span className="rec-pill" data-testid="rec-robot-pill">
            <Icon name="robotArm" size={15} />
            {robotName}
          </span>
        ) : null}
      </div>
    </header>
  );
}
