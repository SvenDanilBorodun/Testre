/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// MUST stay in sync with server-side coco_classes.py — enforced by a
// Jest test (objectClasses.sync.test.js).
export const OBJECT_CLASSES = [
  'Flasche',
  'Tasse',
  'Gabel',
  'Löffel',
  'Schüssel',
  'Banane',
  'Apfel',
  'Orange',
  'Karotte',
  'Brokkoli',
  'Maus',
  'Fernbedienung',
  'Handy',
  'Buch',
  'Schere',
  'Teddybär',
];

export const COLORS = [
  ['Rot', 'rot'],
  ['Grün', 'gruen'],
  ['Blau', 'blau'],
  ['Gelb', 'gelb'],
];

export const DE = {
  CATEGORY_BEWEGUNG: 'Bewegung',
  CATEGORY_WAHRNEHMUNG: 'Wahrnehmung',
  CATEGORY_ZIELE: 'Ziele',
  CATEGORY_LOGIK: 'Logik',
  CATEGORY_VARIABLEN: 'Variablen',
  CATEGORY_AUSGABE: 'Ausgabe',
  HOME: 'Heimposition',
  OPEN_GRIPPER: 'Greifer öffnen',
  CLOSE_GRIPPER: 'Greifer schließen',
  MOVE_TO: 'bewege zu %1',
  PICKUP: 'aufnehmen %1',
  DROP_AT: 'ablegen bei %1',
  WAIT_SECONDS: 'warte %1 Sekunden',
  DETECT_COLOR: 'erkenne Farbe %1',
  WAIT_UNTIL_COLOR: 'warte bis Farbe %1 erkannt (max %2 s)',
  COUNT_COLOR: 'Anzahl Farbe %1',
  DETECT_MARKER: 'erkenne Marker %1',
  WAIT_UNTIL_MARKER: 'warte bis Marker %1 erkannt (max %2 s)',
  DETECT_OBJECT: 'erkenne Objekt %1',
  WAIT_UNTIL_OBJECT: 'warte bis Objekt %1 erkannt (max %2 s)',
  COUNT_OBJECT: 'Anzahl Objekt %1',
  DESTINATION_PIN: 'setze %1 = Pin (Klick auf Szenenkamera)',
  DESTINATION_CURRENT: 'setze %1 = aktuelle Position',
  LOG: 'melde %1',
  PLAY_SOUND: 'Ton spielen',
};
