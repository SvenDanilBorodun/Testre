// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The Aufnahme page header (spec §3.12): eyebrow, the task name as the page
// title, the connection pill and the robot's German name. Presentational: the
// connection pill is handed in (HeartbeatStatus reads Redux itself). The robot
// pill carries the real robot's glyph (RobotGlyph: its URDF, by profile id) and
// none while the profile is still unknown.
// Sucher (§8): the eyebrow carries the tab number „02 · " (decorative,
// aria-hidden), the title is set in the serif as ONE text node.

import React from 'react';
import RobotGlyph from '../icons/RobotGlyph';

export default function RecordHeader({
  eyebrow, title, connection = null, robotName = '', robotProfile = null,
}) {
  return (
    <header className="rec-top">
      <div>
        <div className="rec-eyebrow"><span aria-hidden="true">02 · </span>{eyebrow}</div>
        <h1 className="rec-title" title={title}>{title}</h1>
      </div>
      <div className="rec-pills">
        {connection}
        {robotName ? (
          <span className="rec-pill" data-testid="rec-robot-pill">
            <RobotGlyph profile={robotProfile} size={17} />
            {robotName}
          </span>
        ) : null}
      </div>
    </header>
  );
}
