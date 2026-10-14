// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

/**
 * onClick helper for the page's buttons: after a POINTER click, give the focus
 * back to the page, so Space is „Aufnahme starten" again and not „press this
 * button once more". A keyboard click (Enter/Space, `detail === 0`) keeps the
 * focus where the student put it.
 *
 * Moved here from components/Record/ActionBar.jsx (which re-exports it for its
 * existing importers) so the Sucher primitives and EbUI can use it without an
 * import cycle through the Aufnahme page.
 */
export function releasePointerFocus(event) {
  if (!event || !(event.detail > 0)) return;
  const el = event.currentTarget;
  if (el && typeof el.blur === 'function') el.blur();
}
