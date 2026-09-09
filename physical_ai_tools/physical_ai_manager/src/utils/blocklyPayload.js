/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * What leaves the editor, and to where.
 *
 * `Blockly.serialization.workspaces.save()` emits ONE key per registered
 * workspace serializer. Measured headless against the shipped plugin set
 * (Blockly 12.5.1): the CORE registers `blocks`, `variables` and
 * `workspaceComments`; `@blockly/suggested-blocks` adds `suggested-blocks` at
 * IMPORT time, and `@blockly/workspace-backpack` adds `backpack` when the
 * `Backpack` is constructed. Five in the running app.
 *
 * An ALLOWLIST is the right primitive — a denylist of two plugin names would
 * silently admit the next plugin's key. Two DIFFERENT allowlists, because the
 * two destinations want different things, and that difference is the whole
 * point of this module rather than two copies of one idea:
 *
 *   RUN  → the ROS service `/workflow/start`. The interpreter reads `blocks`
 *          and `variables` and nothing else (`Interpreter.from_json` +
 *          `WorkflowManager._parse_*`), so everything else is weight.
 *          `workspaceComments` is dropped here too and that is safe: the run
 *          never reads it and the SAVE keeps it.
 *   SAVE → `workflows.blockly_json` in the cloud. This is the DOCUMENT, so the
 *          student's free-floating canvas notes (`workspaceComments`) must
 *          survive — dropping them would destroy work — while the two plugin
 *          keys must not, for two independent reasons below.
 *
 * WHY THE SAVE PATH IS SLIMMED AT ALL (audit §11.2 / §11.3):
 *
 *   `suggested-blocks` is UNBOUNDED. Its listener does
 *   `recentlyUsedBlocks.unshift(type)` on every BLOCK_CREATE and NEVER trims,
 *   plus `defaultJsonForBlockLookup[type] = event.json`. Measured: 50 drags →
 *   0.8 KB, 500 → 8.0 KB, 2000 → 32.0 KB, growing for the life of the
 *   document. At ~16 bytes a drag, ~16 000 lifetime drags in one workflow cross
 *   `validators/workflow.py::MAX_BLOCKLY_JSON_BYTES` (256 KiB) and the document
 *   becomes UNSAVEABLE behind a German 413 the student cannot act on, because
 *   the bloat is invisible to them. Slow, monotonic, and nothing ever trims it.
 *
 *   `backpack` is one student's PRIVATE CLIPBOARD. The workflow read-visibility
 *   ladder exposes `blockly_json` to group siblings and (as a classroom
 *   template) to the whole class, and `clone_workflow` copies it wholesale — so
 *   a teacher publishing a template, or a student cloning a group sibling's
 *   workflow, carried the origin student's stashed blocks along. CLAUDE.md
 *   already treats exactly this concern as real for the multiselect plugin's
 *   `blocklyStash*` keys ("student B could paste student A's program"), which
 *   is why those three are in `STUDENT_SCOPED_KEYS`.
 *
 * DISCLOSED COST, and it is a real one: the backpack is persisted ONLY through
 * this serializer (the plugin touches no localStorage — checked, 0 hits), so
 * once it stops riding in the document a student's stash lives for the session
 * and not across a reload. Loading a workflow already emptied the backpack to
 * whatever that workflow carried; now it empties it, full stop. The durable fix
 * is a student-scoped local home for the stash (`utils/sessionScope.js` is
 * where it would be registered), which is a feature, not this change. The same
 * applies, more mildly, to the "häufig benutzt" category.
 *
 * AUTOSAVE (`useAutosave`) KEEPS THE FULL OUTPUT, deliberately. It writes to
 * LOCAL IndexedDB, namespaced per signed-in user or per browser session — it is
 * never shared with another student and never reaches the cloud, so neither
 * reason above applies to it. CLAUDE.md protects it by name ("deleting a
 * crash-recovery cache destroys work"), and a crash-recovery snapshot that
 * silently restored LESS than the editor had is the wrong shape for a cache
 * whose whole job is losing nothing.
 *
 * An EMPTY workspace serializes to `{}` with NO `blocks` key at all (measured),
 * so a missing key must stay MISSING rather than become `undefined` — hence the
 * `hasOwnProperty` guard rather than a plain read.
 */

/** The two keys the interpreter reads. */
export const RUN_PAYLOAD_SERIALIZER_KEYS = ['blocks', 'variables'];

/** The three keys that make up the student's DOCUMENT. */
export const SAVE_PAYLOAD_SERIALIZER_KEYS = ['blocks', 'variables', 'workspaceComments'];

/**
 * Copy `keys` out of a serializer output into a FRESH object.
 *
 * Never mutates the input: `handleStart` and `handleSave` read the same
 * `editorJson` object that autosave also holds, and that sharing is what keeps
 * the full-output paths intact.
 */
export function pickSerializerKeys(blocklyJson, keys) {
  if (!blocklyJson || typeof blocklyJson !== 'object') return {};
  const out = {};
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(blocklyJson, key)) {
      out[key] = blocklyJson[key];
    }
  }
  return out;
}

export function slimRunPayload(blocklyJson) {
  return pickSerializerKeys(blocklyJson, RUN_PAYLOAD_SERIALIZER_KEYS);
}

export function slimSavePayload(blocklyJson) {
  return pickSerializerKeys(blocklyJson, SAVE_PAYLOAD_SERIALIZER_KEYS);
}
