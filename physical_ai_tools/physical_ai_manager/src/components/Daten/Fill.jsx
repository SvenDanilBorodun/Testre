// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// A copy template with markup in its values (spec §G9): ONE string literal
// with `{name}` placeholders, a value may be an element (bold, mono). The
// sentence stays one translatable string; the markup never splits it.

import React from 'react';
import { fillParts } from '../../features/editDataset/model/format';

export default function Fill({ template, values }) {
  const parts = fillParts(template, values);
  return (
    <>
      {parts.map((part, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <React.Fragment key={i}>{part}</React.Fragment>
      ))}
    </>
  );
}
