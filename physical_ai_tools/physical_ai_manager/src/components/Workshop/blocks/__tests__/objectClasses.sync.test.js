/*
 * Verifies the frontend OBJECT_CLASSES dropdown stays byte-aligned with
 * the server-side coco_classes.py. If a future refactor renames a class
 * on either side without updating the other, this test fails the build
 * — a workflow that uses the renamed class would otherwise pass
 * frontend validation but get rejected by the interpreter at runtime.
 */

import fs from 'fs';
import path from 'path';
import { OBJECT_CLASSES } from '../messages_de';

// Resolve the server-side coco_classes.py — we look in two places:
//   1. The dev sibling-repo layout: physical_ai_tools/physical_ai_server/...
//   2. The Docker build-context staging layout: /app/_coco_classes.py
//      (build-images.sh copies the file there before `docker build` so
//      the prebuild Jest hook can run inside the manager image even
//      though that build context only includes physical_ai_manager).
const DEV_PATH = path.resolve(
  __dirname,
  '../../../../../physical_ai_server/physical_ai_server/workflow/coco_classes.py'
);
const DOCKER_PATH = '/app/_coco_classes.py';
const COCO_CLASSES_PATH = fs.existsSync(DOCKER_PATH) ? DOCKER_PATH : DEV_PATH;

function parseCocoClasses(source) {
  // Match the dict literal we emit in coco_classes.py:
  //
  //   COCO_CLASSES: dict[str, int] = {
  //       'Flasche': 39,
  //       ...
  //   }
  //
  // Only the *labels* matter for the frontend mirror — the COCO ids stay
  // server-side. Unicode keys (Löffel, Schüssel, Teddybär) must be
  // preserved literally.
  const match = source.match(/COCO_CLASSES[^=]*=\s*\{([\s\S]*?)\n\}/);
  if (!match) {
    throw new Error('Could not locate COCO_CLASSES dict in coco_classes.py');
  }
  const body = match[1];
  const keys = [];
  const re = /'([^']+)'\s*:\s*\d+/g;
  let m;
  while ((m = re.exec(body)) !== null) {
    keys.push(m[1]);
  }
  return keys;
}

describe('OBJECT_CLASSES sync', () => {
  let serverLabels;

  beforeAll(() => {
    const source = fs.readFileSync(COCO_CLASSES_PATH, 'utf8');
    serverLabels = parseCocoClasses(source);
  });

  test('coco_classes.py has at least one class', () => {
    expect(serverLabels.length).toBeGreaterThan(0);
  });

  test('frontend OBJECT_CLASSES set matches server coco_classes.py', () => {
    // Audit §3.12 — relaxed from order-exact to set-equality so a
    // server-side reordering doesn't force a synchronised JS edit.
    // The order in coco_classes.py is the dropdown order shown to
    // students; out-of-order sync would only cause a visual reshuffle
    // of the dropdown, not a runtime mismatch.
    expect(new Set(OBJECT_CLASSES)).toEqual(new Set(serverLabels));
    expect(OBJECT_CLASSES.length).toBe(serverLabels.length);
  });

  test('umlaut classes are preserved without transliteration', () => {
    // If a refactor accidentally transliterates Löffel → Loeffel on
    // either side, this test catches it.
    expect(OBJECT_CLASSES).toContain('Löffel');
    expect(OBJECT_CLASSES).toContain('Schüssel');
    expect(OBJECT_CLASSES).toContain('Teddybär');
    expect(serverLabels).toContain('Löffel');
    expect(serverLabels).toContain('Schüssel');
    expect(serverLabels).toContain('Teddybär');
  });

  test('no duplicates on either side', () => {
    expect(new Set(OBJECT_CLASSES).size).toBe(OBJECT_CLASSES.length);
    expect(new Set(serverLabels).size).toBe(serverLabels.length);
  });
});
