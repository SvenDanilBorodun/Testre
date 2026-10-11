/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Every German string of the pane dividers (layout/ResizeHandle.jsx) and of
// the simulator notice's close button. `%1` is filled by fillLayout.

export const LAYOUT_DE = Object.freeze({
  RESIZE_TITLE: 'Ziehen oder mit den Pfeiltasten die Größe ändern – Doppelklick setzt zurück.',
  VALUE_PX: '%1 Pixel',
  VALUE_PERCENT: '%1 Prozent',
  DOCK_WIDTH: 'Breite der Werkzeuge',
  DOCK_SPLIT: 'Aufteilung der beiden Werkzeuge',
  SIM_WIDTH: 'Breite des Simulators',
  SIM_SPLIT: 'Aufteilung zwischen 3D-Ansicht und Tisch',
  SIM_HINT_CLOSE: 'Hinweis ausblenden',
});

/** `template` with every `%1` replaced by `value`. */
export function fillLayout(template, value) {
  return String(template).split('%1').join(String(value));
}
