// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// Author: Kiwoong Park

/**
 * Default paths configuration for file browser modals
 */

// These are paths INSIDE the physical_ai_server container, which runs as root
// (the image declares no USER), so `~` is `/root`. They are literals rather
// than `process.env.REACT_APP_*` reads because no Dockerfile ever set those
// variables — the indirection was decoration, and it is what hid the defect
// below behind a plausible-looking default.
const CONTAINER_HOME = '/root';

// MUST equal `dataset_paths.model_root()` on the server —
// `<home>/ros2_ws/outputs/train`. Fenced by
// physical_ai_server/test/test_model_root_agreement.py.
//
// This used to be `/root/ros2_ws/src/physical_ai_tools/lerobot/outputs/train/`,
// derived from a REACT_APP_LEROBOT_OUTPUTS_PATH nothing sets — i.e. a path
// inside the vendored lerobot tree that the image build `rm -rf`s, and which
// therefore could never coincide with where the server downloads checkpoints.
// Before the 2026-08-06 browse confinement that was merely useless (the modal
// opened empty and the student navigated UP to find the checkpoint);
// afterwards „Modellpfad auswählen" opened on a path outside every browsable
// root and got a German security refusal with nowhere to navigate.
//
// InferencePanel also builds a downloaded model's local path as
// `POLICY_MODEL_PATH + repoId`, which is exactly where
// `download_huggingface_repo(repo_type='model')` writes it — so that
// construction was wrong for the same reason and is fixed by the same change.
const MODEL_OUTPUTS_TRAIN_PATH = `${CONTAINER_HOME}/ros2_ws/outputs/train`;

// MUST equal `dataset_paths.dataset_root()` — `<home>/.cache/huggingface/lerobot`.
const DATASET_ROOT_PATH = `${CONTAINER_HOME}/.cache/huggingface/lerobot`;

export const DEFAULT_PATHS = {
  // File browser defaults. Trailing slashes are load-bearing for the callers
  // that concatenate a name onto them (InferencePanel, LocalDatasetQuickPick).
  POLICY_MODEL_PATH: `${MODEL_OUTPUTS_TRAIN_PATH}/`,
  DATASET_PATH: `${DATASET_ROOT_PATH}/`,
};

/**
 * Target file names for different types of file selection
 */
export const TARGET_FILES = {
  POLICY_MODEL: 'model.safetensors',
  TRAIN_CONFIG: 'train_config.json',
};

export const TARGET_FOLDERS = {
  DATASET_METADATA: 'meta',
  DATASET_VIDEO: 'videos',
  DATASET_DATA: 'data',
};
