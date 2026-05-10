/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Static index of bundled tutorials. The body of each is shipped as a
// JSON document under public/tutorials/{id}.json so the editor can
// fetch it on demand without bundling all 6 tutorials into the main
// chunk. The index here is the list shown in the SkillmapPlayer
// sidebar before a student picks one.

export const TUTORIAL_INDEX = Object.freeze([
  {
    id: 'sage_hallo',
    title_de: 'Sage Hallo',
    level: 1,
  },
  {
    id: 'bewege_zum_punkt_a',
    title_de: 'Bewege zum Punkt A',
    level: 1,
  },
  {
    id: 'roten_wuerfel_aufnehmen',
    title_de: 'Hebe einen roten Würfel auf',
    level: 2,
  },
  {
    id: 'zaehle_blaue_objekte',
    title_de: 'Zähle blaue Objekte und sage die Anzahl',
    level: 2,
  },
  {
    id: 'stapele_drei_wuerfel',
    title_de: 'Stapele drei Würfel',
    level: 3,
  },
  {
    id: 'sortiere_nach_klasse',
    title_de: 'Sortiere nach Klasse',
    level: 4,
  },
]);

const cache = new Map();

function _validateTutorial(doc, id) {
  if (!doc || typeof doc !== 'object') {
    throw new Error('Tutorial-Datei ist leer oder ungültig.');
  }
  if (typeof doc.title_de !== 'string' || !doc.title_de) {
    throw new Error('Tutorial ohne Titel.');
  }
  if (!Array.isArray(doc.steps) || doc.steps.length === 0) {
    throw new Error('Tutorial enthält keine Schritte.');
  }
  doc.steps.forEach((step, i) => {
    if (!step || typeof step !== 'object') {
      throw new Error(`Schritt ${i + 1} ist kein Objekt.`);
    }
    if (typeof step.title !== 'string' || !step.title) {
      throw new Error(`Schritt ${i + 1}: Titel fehlt.`);
    }
    if (typeof step.body !== 'string') {
      throw new Error(`Schritt ${i + 1}: Beschreibung fehlt.`);
    }
    if (!Array.isArray(step.allowed_blocks)) {
      throw new Error(`Schritt ${i + 1}: allowed_blocks fehlt.`);
    }
  });
  if (!doc.id) doc.id = id;
}

export async function loadTutorial(id) {
  if (!id) throw new Error('Keine Tutorial-ID.');
  if (cache.has(id)) return cache.get(id);
  const url = `${process.env.PUBLIC_URL || ''}/tutorials/${encodeURIComponent(id)}.json`;
  const resp = await fetch(url, { cache: 'no-store' });
  if (!resp.ok) {
    throw new Error(`Tutorial-Datei nicht gefunden (${resp.status}).`);
  }
  let doc;
  try {
    doc = await resp.json();
  } catch (e) {
    throw new Error(`Tutorial-Datei ist kein gültiges JSON: ${e.message || e}`);
  }
  _validateTutorial(doc, id);
  cache.set(id, doc);
  return doc;
}
