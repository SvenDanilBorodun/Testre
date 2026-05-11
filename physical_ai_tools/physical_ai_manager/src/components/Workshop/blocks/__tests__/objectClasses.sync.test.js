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
// Two dev candidates: the React app may be inside `physical_ai_manager/`
// (a sibling of `physical_ai_server/` under `physical_ai_tools/`), or
// the build context may stage the file at the Docker-only `/app`
// location. Try both — fall back to `DOCKER_PATH` only when running
// inside the manager Docker build.
const DEV_PATH_5_UPS = path.resolve(
  __dirname,
  '../../../../../physical_ai_server/physical_ai_server/workflow/coco_classes.py'
);
const DEV_PATH_6_UPS = path.resolve(
  __dirname,
  '../../../../../../physical_ai_server/physical_ai_server/workflow/coco_classes.py'
);
const DOCKER_PATH = '/app/_coco_classes.py';
const RESOLVED_PATH = fs.existsSync(DOCKER_PATH)
  ? DOCKER_PATH
  : fs.existsSync(DEV_PATH_6_UPS)
    ? DEV_PATH_6_UPS
    : fs.existsSync(DEV_PATH_5_UPS)
      ? DEV_PATH_5_UPS
      : null;

// When neither dev-layout nor Docker-staging path resolves we're being
// run from a build context that didn't stage coco_classes.py (e.g.
// `railway up` against `physical_ai_manager/` without the wrapper script
// staging `_coco_classes.py` first). Skip rather than fail-loud — the
// canonical build paths (`build-images.sh`, `manager-build-validate` in
// CI, and `physical_ai_manager/scripts/railway-deploy.sh`) all stage the
// file, so missing it here means "this is a different build path" not
// "the dropdown drifted from the server allowlist". A printed warning
// keeps the skip visible in build logs.
const COCO_CLASSES_PATH = RESOLVED_PATH;
const describer = RESOLVED_PATH ? describe : describe.skip;
if (!RESOLVED_PATH) {
  // eslint-disable-next-line no-console
  console.warn(
    '[objectClasses.sync] coco_classes.py not present at any of the '
      + 'known paths (Docker /app/_coco_classes.py, dev 5-up, dev 6-up). '
      + 'Skipping the dropdown↔server sync check. Stage the file with '
      + 'build-images.sh or scripts/railway-deploy.sh for production builds.'
  );
}

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

describer('OBJECT_CLASSES sync', () => {
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
