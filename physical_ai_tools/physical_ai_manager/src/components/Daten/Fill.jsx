// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

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
