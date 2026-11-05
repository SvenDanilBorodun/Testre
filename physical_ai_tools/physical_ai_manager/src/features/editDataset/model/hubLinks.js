// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// „Auf Hugging Face ansehen" (spec §G14, P33): ONE pure function picks the URL.
// A PUBLIC hub copy opens Hugging Face's online LeRobot dataset viewer, which
// reads our v3.0 layout; a private one — or one whose visibility is unknown —
// opens the dataset's own page (the viewer reads anonymously). The URL never
// carries a token or any query parameter but the viewer's path.

import { REPO_ID_RE } from '../datenContract';

const REPO = new RegExp(REPO_ID_RE);

export const VIEWER_BASE = 'https://huggingface.co/spaces/lerobot/visualize_dataset';
export const DATASET_BASE = 'https://huggingface.co/datasets/';

/**
 * @param {string} repoId `<namespace>/<name>`
 * @param {boolean|null|undefined} isPrivate the hub entry's `private`; only `false` counts as public
 * @returns {string|null} null for anything that is not a valid repo id
 */
export function hubDatasetUrl(repoId, isPrivate) {
  const id = String(repoId || '');
  if (!REPO.test(id)) return null;
  if (isPrivate === false) {
    return `${VIEWER_BASE}?path=${encodeURIComponent(`/${id}/episode_0`)}`;
  }
  return `${DATASET_BASE}${id}`;
}
