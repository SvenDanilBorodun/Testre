// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Start page used to print `packageJson.version` — 0.9.0 — as „EduBotics
// v0.9.0", which is the SPA's own version and not the product's (2.17.0). A
// student reading it out during a support call read the wrong number.
//
// The rule this module enforces, and every test below pins one half of it:
// quote the product version only when it is actually known, and otherwise say
// NOTHING. A version that is not this one is worse than no version.

import { productVersion } from '../productVersion';

function withSearch(search) {
  const original = window.location;
  delete window.location;
  window.location = { ...original, search };
  return () => { window.location = original; };
}

describe('productVersion', () => {
  let restore = () => {};
  afterEach(() => { restore(); });

  it('reads the release from the WebView cache-buster', () => {
    // gui_app.py::open_student_window appends `_v=<IMAGE_TAG>`, and on a
    // release IMAGE_TAG is the baked product version.
    restore = withSearch('?_v=2.17.0&robot=omx_full');
    expect(productVersion()).toBe('2.17.0');
  });

  it('refuses „latest" — an unpinned rig does not know its version', () => {
    // This is the case that would otherwise print „EduBotics latest".
    restore = withSearch('?_v=latest');
    expect(productVersion()).toBeNull();
  });

  it('refuses anything that is not a dotted release number', () => {
    for (const v of ['a3f91c', '2.17', '2.17.0-dirty', 'v2.17.0', '']) {
      restore();
      restore = withSearch(`?_v=${v}`);
      expect(productVersion()).toBeNull();
    }
  });

  it('returns null where the parameter never exists (Pi mode, teacher web)', () => {
    restore = withSearch('');
    expect(productVersion()).toBeNull();
    restore();
    restore = withSearch('?cloud=1');
    expect(productVersion()).toBeNull();
  });

  it('tolerates surrounding whitespace', () => {
    restore = withSearch('?_v=%202.17.0%20');
    expect(productVersion()).toBe('2.17.0');
  });
});
