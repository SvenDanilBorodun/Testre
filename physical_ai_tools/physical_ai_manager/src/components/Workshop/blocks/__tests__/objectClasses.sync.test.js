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

const COCO_CLASSES_PATH = path.resolve(
  __dirname,
  '../../../../../physical_ai_server/physical_ai_server/workflow/coco_classes.py'
);

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

  test('frontend OBJECT_CLASSES order matches server coco_classes.py exactly', () => {
    expect(OBJECT_CLASSES).toEqual(serverLabels);
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
