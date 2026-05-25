# Upgrade EduBotics to LeRobot v0.5.1 (dataset format v3.0)

## IMPLEMENTATION STATUS (branch `feat/lerobot-v0.5.1-dataset-v3`)

**Done + locally validated (compileall/tests/parse):**
- **Layer 1** — vendored `physical_ai_tools/lerobot/` mirrored to v0.5.1 (SHA `1396b9fab`, `version 0.5.1`, `CODEBASE_VERSION v3.0`). 514 paths changed.
- **Layer 2** — `modal_app.py`: cu124→cu126, py3.11→3.12, torch 2.6→**2.7.1**/tv **0.22.1**, `[pi0]`→**`[pi,smolvla]`**, torchcodec uninstall removed, new SHA. (cu124-has-no-torch-2.7 verified against the wheel index.)
- **Layer 5** — `training_handler.py`: `EXPECTED_CODEBASE_VERSION` v2.1→**v3.0** + sharpened German reject msg; `lerobot.scripts.train`→**`lerobot.scripts.lerobot_train`** (confirmed runnable + flags valid); processor-json upload note. Existing CLI tests pass.
- **Layer 6** — `overlays/inference_manager.py`: **processor pipeline** (`make_pre_post_processors` + `prepare_observation_for_inference` + pre/post), manual `/255` removed (no double-normalize), German guard for old models lacking `policy_preprocessor.json`, `pi0fast`→**`pi0_fast`** + new **`pi05`** (verified registered type strings + class names).
- **Layer 7** — boot-crash fix: new overlay `overlays/training_manager.py` imports `LerobotTrainer` **lazily** (the 3 moved symbols `scripts.eval`/`utils.wandb_utils`/`get_safe_torch_device` would crash the ROS node at boot otherwise). Added to the Dockerfile apply_overlay chain.
- **Layer 3** — shared thin `docker/physical_ai_server/Dockerfile`: **self-managed LeRobot v0.5.1 install** layer (clone@SHA → strip torch/torchvision → `pip install -e ".[pi,smolvla]"` → assert `CODEBASE_VERSION=='v3.0'`), floor pins protobuf **6.31.1** + numpy **>=2.0,<2.3**, training_manager overlay. Base confirmed Python 3.12 + torch 2.7.0. The same thin layer runs on the arm64 base, so it upgrades Jetson too (no `PHYSICAL_AI_TOOLS_REF` change needed).
- **Layer 8** — VERSION 3-place in-tree bump 2.4.1→**3.0.0** (`VERSION`, `.iss`, `gui/constants.py`).
- **PR1 (docs)** — CLAUDE.md §3 + §5 rewritten for v0.5.1/v3.0, self-managed install, floors, dormant-importer guard.

**REMAINING (need a Docker build + camera/Modal/hardware loop — cannot verify in this env):**
- **Layer 4 — recording overlay rewrite (the crux, NOT done).** `overlays/lerobot_dataset_wrapper.py` + the `data_manager.py` record state machine still use removed v2.1 internals. Must be rewritten to the v3.0 `DatasetWriter` API (see spec below) and verified with a record→inspect-on-disk roundtrip. High blast radius (a subtle bug silently corrupts datasets) — deliberately not blind-written.
- **PR1 CI boot-import job** — not yet added to `ci.yml`.
- **Build/verify gates:** docker build both arches; numpy-1.x→2.x ABI risk on base packages; jetson-ai-lab cu126 index wheel availability for v0.5.1 deps; Modal smoke + real train on a fresh v3.0 dataset; on-robot inference.
- **PR3 operator:** Railway `GUI_VERSION`/`GUI_DOWNLOAD_URL` + `gh release create v3.0.0`.

**Layer 4 v3.0 recording spec (verified against the swapped tree):** `LeRobotDataset.create(repo_id, fps, features, robot_type=..., use_videos=True, vcodec="h264")` (valid codecs `{h264,hevc,libsvtav1,auto}` — NOT `libx264`); `add_frame(frame)` with `task` as a key in `frame` (writer stages images to a temp dir + holds path strings → the old RAM-bytes valve premise is gone; re-frame as frame-count/temp-disk cap); `save_episode()` (computes stats itself); **`finalize()` mandatory before `push_to_hub`/upload**; delete `add_frame_without_write_image`/`save_episode_without_*`/`save_meta_info`/`compute_episode_stats_buffer`/`video_encoding`/custom FFmpegEncoder; derive video paths via `meta.get_video_file_path`; `create_tag` `v2.1`→`v3.0`. `validate_frame`/`validate_episode_buffer` moved to `feature_utils`; tasks→`meta/tasks.parquet`, episodes→`meta/episodes/*.parquet`, videos→`videos/{key}/chunk-NNN/file-NNN.mp4`.

---


## Context

EduBotics currently pins **LeRobot 0.2.0** at SHA `989f3d05ba47f872d75c587e76838e9cc574857a`, writing datasets in **codebase_version `v2.1`**. The newest LeRobot is **v0.5.1** (PyPI 2026-04-07, tag = commit `1396b9fab7aecddd10006c33c47a487ffdcb54b4`), which ships **dataset format v3.0** — a complete rewrite of on-disk layout and the recording API. This upgrade touches every layer (vendored lib → Modal worker → Docker images → recording/training/inference code → CI/docs). It is worth doing now because the student base image (`robotis/ros:jazzy...torch2.7.0`, ROS Jazzy = Ubuntu 24.04 = Python 3.12) already aligns with v0.5.1's hard floors (Python ≥3.12, torch ≥2.7).

**User decisions (locked):**
1. **Target = v0.5.1 / dataset v3.0** (newest).
2. **Clean break on data** — students re-record going forward; old v2.1 datasets/policies are archived out-of-band. No converter, no dual-read. Training preflight *rejects* v2.1 with a German "neu aufnehmen" error.
3. **Self-managed lerobot install** — we clone+pin lerobot v0.5.1 in our own Dockerfiles (like the Modal worker), not waiting for a ROBOTIS base bundle. We own the pinning contract.

## Verified v0.5.1 API facts (drive the rewrite)

- `CODEBASE_VERSION = "v3.0"` (now in `datasets/dataset_metadata.py`).
- **Recording:** `add_frame(frame)` — **task is now a key inside `frame`** (no separate arg). `save_episode(episode_data=None, parallel_encoding=True)`. **`finalize()` is NEW and MANDATORY before `push_to_hub`.** `LeRobotDataset` is a facade over an internal `DatasetWriter` that already buffers in RAM, stages images to a temp dir, defers/streams video encoding, and writes parquet incrementally.
- **Removed (all imported by our overlay today):** `write_info`, `write_episode`, `write_episode_stats`, `validate_frame`, `validate_episode_buffer` (from `datasets.utils`); `meta.add_task`, `meta.get_episode_chunk`, `self._save_episode_table`. **Survives:** `DEFAULT_FEATURES`, `get_feature_stats`, `meta.info` (still a dict), `meta.get_video_file_path`, `meta.get_task_index`.
- **Layout:** tasks → `meta/tasks.parquet`; episodes → `meta/episodes/*.parquet`; videos → `videos/{video_key}/chunk-NNN/file-NNN.mp4` (was `videos/chunk-NNN/{key}/episode_NNNNNN.mp4`).
- **Training:** `python -m lerobot.scripts.train` **is gone** → use `python -m lerobot.scripts.lerobot_train` (or `lerobot-train`). All current Draccus flags still valid. Checkpoint dir still `checkpoints/last/pretrained_model/`, now **also** containing `policy_preprocessor.json`/`policy_postprocessor.json`.
- **Inference:** v0.5.1 **requires the processor pipeline** — normalization moved out of the policy. Use `make_pre_post_processors(policy.config, pretrained_path=...)` then `preprocessor(obs) → select_action → postprocessor(action)`. Per-policy `modeling_*` module paths unchanged.
- **Dep floors:** Python ≥3.12, torch ≥2.7,<2.11, torchvision ≥0.22,<0.26, **numpy ≥2.0,<2.3**, **transformers ==5.3.0**, torchcodec ≥0.3,<0.11, **protobuf ≥6.31.1,<6.32**, safetensors ≥0.4.3.

## Full layer coverage (every layer that touches LeRobot — verified by sweep)

I swept all 10 architecture layers + Railway. Verdict per layer:

| Layer | Touches LeRobot / dataset format? | Action |
|---|---|---|
| **1. Windows installer** (`installer/` .iss + 9 .ps1) | **CLEAN.** Zero lerobot/v2.1/codebase_version. Only deals with WSL2/usbipd/Docker image *tags* (`versions.env`, `IMAGE_TAG`). | None beyond shipping the rebuilt image tag + `AppVersion` bump via the normal release. |
| **2. tkinter GUI** (`gui/gui_app.py`, `app/*.py`) | **CLEAN.** Pure Windows/WSL/Docker launcher + native camera bridge. No dataset bytes, no lerobot, no format strings. `Aufnahme`/`Inferenz` German labels are static. | None. |
| **3. React student app** (`physical_ai_manager/src`) | **Passive only.** `codebaseVersion` is stored (`editDatasetSlice.js:28`, `DatasetDeleteSection.js:185`) but **never rendered**. `paths.js` folder constants (`meta`/`videos`/`data`) survive v3.0. Aufnahme/Training/Inferenz pages have no format logic. | None required (auto-shows `v3.0` if ever surfaced). |
| **4. Recording overlay (Aufnahme)** `overlays/lerobot_dataset_wrapper.py` + `data_manager.py` (+ twins) | **CRITICAL.** Deep v2.1 internal-API reimplementation; v2.1 chunk paths, `data/*/*.parquet` HF card glob, `_check_dataset_exists` destructive rmtree on layout, `create_tag('v2.1')`. | **Full rewrite — Layer 4 below.** |
| **5. Training** `modal_training/{modal_app.py,training_handler.py}` | **HIGH.** SHA pin, Python 3.11→3.12, torch/torchcodec, `EXPECTED_CODEBASE_VERSION`, `lerobot.scripts.train`→`lerobot_train`, checkpoint+processor jsons. | **Layers 2 & 5 below.** |
| **6. Inference (Inferenz)** `overlays/inference_manager.py` (+ twin) | **HIGH.** `from_pretrained`+`select_action(obs_dict)` with hand-rolled `/255` preprocessing → must adopt the **required v0.5.1 processor pipeline**. Shared by student image **and Jetson**. | **Layer 6 below.** |
| **7. Cloud Roboter Studio / vision** (`vision_app.py`, workflow overlays) | **CLEAN.** `vision_app.py` is a separate Modal app (own torch pin, no lerobot). Workflow/Blockly + classical CV don't touch lerobot. | None. |
| **8. Jetson — agent** (`jetson_agent/agent.py`, `docker-compose.jetson.yml`, route `jetson.py`, migrations 019/022) | **CLEAN relay.** Pure Docker-lifecycle / heartbeat / rosbridge-proxy / pairing-auth. No lerobot import, no `from_pretrained`/`select_action`, no codebase_version. Host is Py3.10 but only relays. | None. |
| **8. Jetson — IMAGE** (`Dockerfile.arm64` + shared thin overlay `docker/physical_ai_server/Dockerfile`) | **YES — the Jetson RUNS LeRobot inference.** It builds `physical-ai-server-jetson` which applies the same `inference_manager.py` overlay (→ needs processor pipeline) and ships `lerobot_dataset_wrapper.py`. arm64-specific pins must change. | **Layer 3 (arm64) + Layer 6 (shared inference rewrite).** |
| **9. Railway — Cloud API** (`cloud_training_api/`) | **CLEAN of format.** Dockerfile has no lerobot/torch. Dispatches to Modal. `model_type` validated against env `ALLOWED_POLICIES` (default `act`; v0.5.1 names unchanged); training params map to the same CLI flags. One comment only (`training.py:118` eval_freq). | None for format. Optionally widen `ALLOWED_POLICIES`/`POLICY_MAX_TIMEOUT_HOURS` to expose new v0.5.1 policies. **VERSION env bump** (`GUI_VERSION`/`GUI_DOWNLOAD_URL`) is the only required Railway action — in PR 3; `/health.version` auto-updates via `COPY VERSION`. |
| **9. Railway — teacher-web** (`Dockerfile.web`) | **CLEAN.** React admin/teacher SPA, no lerobot. | None. |

**Net:** every dataset-format / lerobot risk lives in exactly two places — the **docker overlay layer** (`physical_ai_server/overlays/`) and **Modal training** (`modal_training/`) — anchored by the shared SHA pin. Installer, both GUIs, Roboter Studio, the Jetson *agent*, and both Railway services are decoupled and need no format work (only the routine image-tag + VERSION-env bumps they'd get on any release). The Jetson *image*, however, runs inference and rides the shared `inference_manager.py` rewrite + arm64 pin bumps.

### Jetson arm64 specifics (`physical_ai_tools/physical_ai_server/Dockerfile.arm64`)
Already torch 2.7.0 (L1) and Python 3.12 (L12) — **both satisfy v0.5.1**. Must change: `pip install 'numpy<2'` (L88) → `numpy>=2.0,<2.3` (**blocker**); `protobuf==6.31.0` (L112) → `>=6.31.1,<6.32`; `LEROBOT_EXPECTED_SHA` (L52) + `PHYSICAL_AI_TOOLS_REF` (L51) → v0.5.1 (or replace the submodule-SHA guard with our own pinned clone per Decision 3); re-check the `sed` torch/torchvision strip (L79-81) still matches v0.5.1's pyproject and the `[smolvla]` extra (L84). **Verify** the `pypi.jetson-ai-lab.io/jp6/cu126` index (L76) serves numpy 2.x / protobuf ≥6.31.1 / safetensors / pillow arm64 wheels (PyPI fallback at L77 helps). Rebuild + push via `BUILD_BASE_ARM64=1 PLATFORM=arm64 build-images.sh`.

## Implementation — by layer

### Layer 1 — Vendored lib `physical_ai_tools/lerobot/`
Replace the entire flattened tree with the v0.5.1 source at `1396b9fab...`. Confirm `pyproject.toml` version `0.5.1` and `datasets/dataset_metadata.py` `CODEBASE_VERSION="v3.0"`.

### Layer 2 — Modal worker `robotis_ai_setup/modal_training/modal_app.py`
- `LEROBOT_COMMIT` (L19) → `1396b9fab7aecddd10006c33c47a487ffdcb54b4`.
- `add_python` (L23) `"3.11"` → `"3.12"`.
- torch/torchvision force-reinstall (L47-60): bump to a cu124-index pair satisfying torch≥2.7,<2.11 + torchvision≥0.22,<0.26 (verify against `download.pytorch.org/whl/cu124`). Keep `index_url` + `--force-reinstall`.
- **Stop uninstalling torchcodec** (L61) — v3.0 uses it for dataset video reads during training (smoke-test both ways; fall back to `video_backend="pyav"` only if needed).
- Ensure no `numpy<2` leaks in.

### Layer 3 — Student image (self-managed install)
- `robotis_ai_setup/docker/physical_ai_server/Dockerfile`: add a layer that clones+`pip install -e .` lerobot at `1396b9fab...`, using the existing torch/torchvision `sed`-strip pattern (`Dockerfile.amd64:37-40`). **Pre-check blocker:** `docker run robotis/physical-ai-server:amd64-0.8.2 python3 --version` must be ≥3.12 or this is a hard blocker (need newer base / 3.12 venv).
- Re-derive floor pins (`Dockerfile:304-329`): `protobuf` `6.31.0`→`6.31.1`; add `numpy>=2.0,<2.3`; `safetensors`/`pillow` OK. Update the in-layer verification print.
- `Dockerfile.amd64:28`: pin the currently-unpinned `jazzy` clone.
- `Dockerfile.arm64:51-68`: set `LEROBOT_EXPECTED_SHA`→`1396b9fab...` and bump `PHYSICAL_AI_TOOLS_REF` to a matching upstream commit, **or** replace the submodule-SHA guard with our own pinned clone. Update floor pins (L110-116) and **remove `numpy<2`** (L85). Verify jetson cu126 index has torch≥2.7 wheels.

### Layer 4 — Recording overlay rewrite (THE CRUX)
Files: `robotis_ai_setup/docker/physical_ai_server/overlays/lerobot_dataset_wrapper.py` + `overlays/data_manager.py`, **mirrored in lockstep** to twins `physical_ai_tools/physical_ai_server/physical_ai_server/data_processing/{lerobot_dataset_wrapper,data_manager}.py`.

The v2.1 wrapper reimplements internals that are all removed — **delete, don't patch**. Invert to a thin wrapper around v3.0's `DatasetWriter` (which already does the RAM buffering + image staging + deferred encoding + incremental parquet the optimized-save-mode hand-rolled):

1. **`add_frame`:** new `add_frame_edu(frame, task)` does `frame = {**frame, "task": task}`, runs the RAM-valve byte accounting, then `super().add_frame(frame)`. Drop the optimized-vs-normal dispatch branch in `data_manager.py:184-192`.
2. **PRESERVE the RAM safety valve** verbatim (`_episode_image_bytes`, `_buffer_full`, German `_buffer_full_warning`, `_DEFAULT_MAX_BUFFER_BYTES` from `EDUBOTICS_MAX_BUFFER_GB`, `reset_buffer_accounting`); only move the increment hook into `add_frame_edu`. **Re-measure** per-frame RSS growth on the new writer (images now stage to a temp dir, so the ~74 s cap likely loosens) and adjust the GB default; keep the valve as the OOM backstop. `data_manager.py:173-182` consumer unchanged.
3. **Video encoding** — recommend **Path A**: delete the custom `_create_video`/`video_encoding`/`check_video_encoding_completed`/`encoders` machinery and pass `vcodec="libx264"` to `create()` to keep ACT-training input fidelity. If `create()` doesn't expose crf/preset/pix_fmt, fall back to **Path B**: wrap the EduBotics `FFmpegEncoder` to the `StreamingVideoEncoder` interface and inject via `create(..., streaming_encoding=True)`. Confirm by reading `datasets/dataset_writer.py` + `datasets/video_utils.py`. Compare ffprobe of old vs new mp4 (crf23/yuv420p/libx264).
4. **Save path:** delete `save_episode_without_*`, `save_meta_info`, `compute_episode_stats_buffer`. `data_manager.save()` collapses to a single `self._lerobot_dataset.save_episode()` (v3.0 computes stats itself).
5. **`finalize()`** — add the mandatory call right before HF upload in `data_manager.upload_huggingface_repo` / the `'finish'` branch (~L263-279, ~L1180-1242). Replaces the old `video_encoding()` sweep.
6. **`_verify_saved_video_files`** in `data_manager.py` (~L300-360): derive expected mp4 paths via `meta.get_video_file_path(ep_idx, key)` (new v3.0 layout), not hand-built v2.1 strings.
7. **`create()`** (`data_manager.py:770`): keep features from `DEFAULT_FEATURES.copy()` + cameras; add `vcodec="libx264"` and `robot_type=self._robot_type` (apply at create time instead of mutating `meta.info`).
8. **Upload tag** (`data_manager.py:1242-1243` + twin `:914-915`): `'v2.1'` → `'v3.0'`.
9. Verify reopen/resume of a partial v3.0 dataset in `check_lerobot_dataset` (`data_manager.py:744`).

### Layer 5 — Training handler `robotis_ai_setup/modal_training/training_handler.py`
- `EXPECTED_CODEBASE_VERSION` (L40) `"v2.1"`→`"v3.0"`; sharpen the mismatch German message (L226-237) to name v2.1 and instruct re-recording.
- `_build_training_command` (L295): `-m lerobot.scripts.train` → `-m lerobot.scripts.lerobot_train`. All other flags + ACT `n_action_steps=15` guard unchanged.
- `_upload_model_to_hf` (L378): `checkpoints/last/pretrained_model/` still correct; ensure the new `policy_preprocessor.json`/`policy_postprocessor.json` ride along in `upload_large_folder` (inference needs them).

### Layer 6 — Inference overlay `overlays/inference_manager.py` (+ twin `physical_ai_tools/.../inference/inference_manager.py`)
- Add the processor pipeline in `load_policy` (L85-91): `make_pre_post_processors(self.policy.config, pretrained_path=self.policy_path)` → store `self.preprocessor/self.postprocessor`.
- `_predict` (L149-155): `obs = self.preprocessor(observation)` → `select_action(obs)` → `action = self.postprocessor(action)`.
- **Remove the manual `/255`+permute+`.to(device)`** in `_preprocess`/`_convert_images2tensors` (L156-200) — the processor now owns normalization/device. Verify the obs format the preprocessor expects (HWC-uint8 vs CHW-float) and assemble accordingly to avoid double-normalization.
- `_get_policy_class` (L200) module paths unchanged. Add a German error if a loaded model lacks `policy_preprocessor.json` (old v2.1 model guard).

### Layer 7 — Dormant-but-imported modules (verify, fix import paths)
`physical_ai_tools/.../training/training_manager.py`, `.../training/trainers/lerobot/lerobot_trainer.py`, `.../evaluation/evaluation_manager.py` import lerobot symbols and load at ROS-node boot — a moved symbol crashes `physical_ai_server.py` at startup even though the path is dormant. Verify each symbol at v0.5.1 paths (`make_dataset`→`datasets.factory`, `make_policy`→`policies.factory`, `TrainPipelineConfig`→`configs.train`, etc.) and fix. `compileall` won't catch this → see new CI job.

### Layer 8 — CI / docs / VERSION
- **CLAUDE.md §5** + every `989f3d05`/`v2.1`/`lerobot.scripts.train` reference: rewrite to the new SHA, v3.0, `lerobot_train`, Python 3.12 floor, new floor pins (protobuf 6.31.1, numpy 2.x, transformers 5.3.0), and the self-managed-install decision.
- **`.github/workflows/ci.yml`:** add a **boot-import smoke job** that imports the overlay+twin manager modules against installed v0.5.1 (catches Layer-7 breakage `compileall` misses). Confirm `modal-import-validate` still green.
- **VERSION 4-place bump** (this is major → e.g. `3.0.0`): `VERSION`, `installer/robotis_ai_setup.iss AppVersion`, `gui/app/constants.py` fallback, + Railway `GUI_VERSION`/`GUI_DOWNLOAD_URL` after `gh release create`.
- GUI `DatasetDeleteSection.js:185` auto-displays v3.0 — no change.

## Risks / de-risking (do the Python-version pre-check FIRST)
- **Python 3.12 floor vs base/jetson** → pre-check `python3 --version` in the 0.8.2 base + jetson cu126 wheels. Potential blocker.
- **RAM valve math** under temp-dir image staging → instrument a 5-min record, watch `docker stats`, recompute the GB default.
- **Custom encoder vs v3.0** → read `dataset_writer.py`/`video_utils.py`; ffprobe-compare old vs new mp4.
- **torchcodec** now needed for v3.0 reads on Modal → smoke-train.
- **Inference double-normalization** → strip manual `/255`; A/B a known action on a v3.0 ACT model.
- **Floor-pin contradictions** (protobuf 6.31.0 < floor; numpy<2) → re-derive, rebuild both arches.

## Verification (record → train → infer on ONE fresh v3.0 dataset)
- `cd robotis_ai_setup && python -m unittest discover -s tests -v`
- `cd robotis_ai_setup/cloud_training_api && SUPABASE_URL=... python -m unittest discover -s app/tests -v` (assert command uses `lerobot_train`; v2.1 rejected with German message)
- `cd physical_ai_tools/physical_ai_manager && npm test -- --watchAll=false`
- **Record:** student container records a 2-cam episode; assert on disk `meta/info.json` codebase_version `v3.0`, `meta/tasks.parquet`, `meta/episodes/*.parquet`, `data/.../*.parquet`, `videos/{key}/chunk-000/*.mp4`; parquet footers valid (pyarrow open) ⇒ `finalize()` ran. Trigger the RAM valve with a low `EDUBOTICS_MAX_BUFFER_GB`.
- **Modal:** `modal run -m modal_app::smoke_test` (image builds, py3.12, torch≥2.7, lerobot imports); then a real `train` on the fresh dataset — preflight passes v3.0 / rejects synthetic v2.1, checkpoint dir contains model + processor jsons, HF upload includes them.
- **Inference:** load the v3.0 model, confirm `make_pre_post_processors` + sane actions; confirm a v2.1 model raises the German error.
- New tests: v3.0 record→read roundtrip; preflight v2.1-rejection; training-command builder; inference processor pipeline; CI boot-import job.

## PR sequencing (format break forces an atomic cutover)
- **PR 1 (safe, early):** CLAUDE.md §5 doc rewrite + the CI boot-import job.
- **PR 2 (THE atomic cutover — one PR):** Layers 1–7 + VERSION 4-place bump. The pinning contract and the v2.1→v3.0 format are one unit; a half-upgraded fleet records v2.1 while training expects v3.0. Land behind the full record→train→infer e2e gate (amd64, and arm64 if hardware available).
- **PR 3 (post-release):** Railway `GUI_VERSION`/`GUI_DOWNLOAD_URL` + `gh release create v3.0.0` with the new `EduBotics_Setup.exe` so existing installs get the update gate.
- Archive v2.1 datasets/policies out-of-band before PR 2; the preflight + inference German errors are the re-record safety net.

## Critical files
- `robotis_ai_setup/docker/physical_ai_server/overlays/lerobot_dataset_wrapper.py` (+ twin) — recording rewrite
- `robotis_ai_setup/docker/physical_ai_server/overlays/data_manager.py` (+ twin) — orchestration, finalize(), v3.0 paths, tag
- `robotis_ai_setup/docker/physical_ai_server/overlays/inference_manager.py` (+ twin) — processor pipeline
- `robotis_ai_setup/modal_training/{modal_app.py,training_handler.py}` — worker + training command + preflight
- `physical_ai_tools/lerobot/` — vendored tree swap
- `physical_ai_tools/physical_ai_server/Dockerfile.amd64`, `Dockerfile.arm64`, `robotis_ai_setup/docker/physical_ai_server/Dockerfile` — self-managed install + floor pins
- `CLAUDE.md` §5, `.github/workflows/ci.yml`, `VERSION` (+ iss/constants/Railway)

---

## CODE REVIEW (2026-05-25, 4 Opus agents) — findings + fixes applied

### Fixes applied this round (committed)
- **modal/training torchcodec ABI (was CRITICAL):** `lerobot[...]` resolves newest torchcodec (>=0.3,<0.11) then torch is force-reinstalled to 2.7.1 → torchcodec ABI-mismatched → crash on first v3.0 video read (find_spec succeeds so LeRobot picks it). **Fix:** `_build_training_command` now passes `--dataset.video_backend=pyav` (av is a core dep, no torch ABI). `DatasetConfig.video_backend` confirmed valid.
- **checkpoint symlink upload (was HIGH):** `checkpoints/last` is a relative symlink; `upload_large_folder` could skip it → silent empty model. **Fix:** `_upload_model_to_hf` now `.resolve()`s the path and asserts `*.safetensors`/`*.bin` + `config.json` exist before upload.
- **Dockerfile sed over-strip (was HIGH):** `"torch[^"]*"` also deleted the `torchcodec` core-dep line. **Fix:** anchored seds `"torch>=` / `"torchvision>=` + a `grep -q '"torchcodec'` guard.
- **numpy-2 ABI build guard (was MEDIUM):** added `import torch, torchvision, cv2, onnxruntime, pupil_apriltags` to the floor-pin verifier so a numpy 1.x→2.x ABI break fails the build loudly.
- **`pi0fast`→`pi0_fast` cross-cutting (was HIGH):** fixed in `cloud_training_api/app/routes/training.py` (ALLOWED_POLICIES + POLICY_MAX_TIMEOUT_HOURS, added `pi05`), `physical_ai_manager/Dockerfile.web`, `PolicySelector.js`, and `tests/test_training_handler_cli.py`. Recording/training/inference policy names now consistent.
- **docstring:** `training_handler.py` top docstring `scripts.train`→`scripts.lerobot_train`.

### CONFIRMED CORRECT by review (no action)
- cu126 carries torch 2.7.1/tv 0.22.1 cp312; base 12.6.3 tag real; v0.5.1 floors all satisfied. Every train CLI flag valid; `lerobot_train` runnable; preflight v3.0 schema correct; `save_checkpoint` writes processor jsons into pretrained_model/. Inference pipeline matches v0.5.1 canonical `predict_action` (no double-normalize); all 8 policy module paths/classes/type-strings correct. `training_manager` lazy import fully removes lerobot_trainer from the boot graph. Vendored swap complete; LFS removal safe; apply_overlay chain 1:1; self-managed install path/ordering/pins correct.

### STILL OPEN — handle in Layer 4 work or on-build
- **BOOT CRASH until Layer 4 lands (KNOWN):** `overlays/lerobot_dataset_wrapper.py` still imports removed v2.1 symbols from `lerobot.datasets.utils` (`write_info`, `write_episode`, `write_episode_stats`, `validate_frame`, `validate_episode_buffer`). It's on the boot path (data_manager imports it), so **the image will NOT boot until the Layer 4 rewrite is done.** Expected (Layer 4 pending).
- **arm64 base (`Dockerfile.arm64`) NOT updated:** still pins old `LEROBOT_EXPECTED_SHA=989f3d05` + `numpy<2` + `protobuf==6.31.0`. The shared thin layer overrides these at build so the final Jetson image is *probably* correct, but the base double-installs old lerobot, the SHA guard is misleading, and **the jetson-ai-lab `jp6/cu126` index may be pruned** (cu129 reported as the usable one late-2025) — MUST verify aarch64 wheels (numpy2/datasets4/transformers5/av) on a real arm64 build.
- **CI boot-import job** not added to `ci.yml`.
- **MUST-VERIFY-ON-BUILD:** base python>=3.12 (`docker run`); numpy-2 ABI on cv_bridge (ROS-sourced, not in the build smoke); torchcodec/pyav decode; full record→train→infer e2e on a fresh v3.0 dataset.

### Layer 4 recording-rewrite spec — CORRECTED (these supersede the spec above)
1. **vcodec = `"h264"` (NOT `libx264`)** — `resolve_vcodec` raises on `libx264`. Valid set `{h264,hevc,libsvtav1,auto}`.
2. **Resume is NOT `__init__`** — `LeRobotDataset(repo_id, root)` is READ-ONLY in v0.5.1 (writer=None → add_frame raises). Use `LeRobotDataset.resume(repo_id, root=<path>)` (requires explicit `root`). Fix `data_manager.check_lerobot_dataset`.
3. **`start_image_writer` moved to `DatasetWriter`** — pass `image_writer_processes`/`image_writer_threads` to `create()`/`resume()`, or call `self.writer.start_image_writer(...)`.
4. **`episode_buffer` is on `self.writer`** — use the facade helper `has_pending_frames()`.
5. **`write_episode_stats` is GONE** (not a rename) — rework the call site; `validate_frame`/`validate_episode_buffer`→`lerobot.datasets.feature_utils`; `write_info`/`write_episode`→`lerobot.datasets.io_utils`.
6. **`meta.add_task`→`meta.save_episode_tasks(list)`**; `meta.get_episode_chunk`/`_save_episode_table` gone (v3.0 `save_episode` owns this).
7. **`_verify_saved_video_files`** — v3.0 path `videos/{key}/chunk-NNN/file-NNN.mp4`; derive via `meta.get_video_file_path(ep, key)` AFTER `save_episode` (indices unknown before save).
8. **`set_robot_type`** — pass `robot_type=` to `create()`/`resume()` at construction.
9. **`finalize()`** — call once before HF upload; `create_tag('v2.1')`→`'v3.0'` (or use `dataset.push_to_hub(...)` which auto-tags CODEBASE_VERSION, making manual create_tag redundant).
10. **RAM valve must be RE-FRAMED** — v3.0 stages images to a temp dir; buffer holds path strings, so the decoded-ndarray byte premise is false. Use frame-count / temp-disk / wall-clock cap.
11. **README `data/*/*.parquet` glob is STILL VALID** for v3.0 (one level deep) — earlier spec flagged it wrongly; the **video** glob does change.
12. **Twins are dead/overlaid** — the `physical_ai_tools/physical_ai_server/.../*` twins are overwritten by apply_overlay at build and are NOT on a separate boot path; do NOT mirror edits into them. Only the `overlays/` copies ship.

---

## FINAL INVESTIGATION (2026-05-25, 3 Opus agents, read-only) — verdict + extra Layer-4 gaps

**Verdict:** review fixes (commit 29d4a6e) are **regression-free**; Layers 1,2,5,6,7,8 **confirmed correct**. The ONLY blocker is Layer 4 (recording), which is un-migrated and makes the image **non-bootable** (the `lerobot_dataset_wrapper.py` overlay imports removed v2.1 symbols → ImportError at ROS boot). CI is **false-green** on this (compileall doesn't import; tests are mocked) — add the boot-import job.

**Fixes re-confirmed:** pyav backend valid (routes to torchvision VideoReader, never imports torchcodec; valid set {torchcodec,pyav,video_reader}); sed strips torch+torchvision only (torchcodec kept); numpy-2 ABI smoke has NO false-fail risk (cv2/onnxruntime/pupil_apriltags installed at Dockerfile L265-273, before the verifier L389); checkpoint `.resolve()`+assert correct (`model.safetensors` caught); `pi0fast→pi0_fast` complete (only dead twins + comments remain).

**7 ADDITIONAL Layer-4 gaps the spec above under-stated (handle in the rewrite):**
1. `info['total_videos']` / `info['total_chunks']` are **absent KEYS** in v3.0 info.json → `save_meta_info`'s writes are `KeyError`, not just removed methods. (React only reads `codebase_version`, so likely safe — grep before landing.)
2. `validate_frame` semantic change: now **requires `task` as a key in the frame** AND rejects any `DEFAULT_FEATURES` key in the frame. (Handled if you route through `super().add_frame`; breaks any manual `validate_frame` call.)
3. `get_episode_index()` (wrapper) is consumed by `data_manager.get_save_rosbag_path()` (~L140) and the video-path derivation (~L315) — re-express as `self.writer.episode_buffer['episode_index']`, don't silently drop it (rosbag pathing breaks otherwise).
4. **Student-facing regression:** the SAVING-phase **progress bar** (`_get_encoding_progress` ~L550 → `TaskStatus.encoding_progress`) reads `self._lerobot_dataset.encoders`, which is GONE under v3.0's synchronous `save_episode()`. It will read 100%/dead. Decide: use `streaming_encoding=True` to keep a live signal, or accept a blocking save with no percentage. Not just an internal refactor.
5. `_verify_saved_video_files`: v3.0 **concatenates multiple episodes into one `file-NNN.mp4`** (`concatenate_video_files`), so per-episode mp4 verification is no longer 1:1. Existence/zero-byte check still works on the shared file; the per-episode German warning wording is misleading.
6. `_check_dataset_exists` (~L716) passes structurally on a leftover v2.1 directory (all of `meta/`,`videos/`,`data/` exist) → add a real `codebase_version == "v3.0"` gate (or, given the clean-break decision, just always rmtree+recreate).
7. `get_video_file_path(ep,key)` raises **IndexError** (not empty path) if called before `save_episode` — it indexes `meta.episodes[ep]`. Always call it AFTER `save_episode`.

**Cosmetic (non-blocking):** (a) `modal_app.py:64-66` comment calls torchcodec "the safe default backend" — now in tension with the pyav override; add a one-line reconciling note. (b) `test_training_handler_cli.py` loops omit the new `pi05` (coverage drift; tests pass).

**MUST-VERIFY-ON-BUILD (Windows/Docker/hardware):** base image Python ≥3.12 (`docker run robotis/physical-ai-server:amd64-0.8.2 python3 --version`); numpy-2 ABI on `cv_bridge` (ROS-sourced, not in the build smoke — exercised at node boot); Modal image build (torch 2.7.1+cu126); arm64 build + jetson-ai-lab cu126 wheel availability (may be pruned → cu129); full record→train→infer on a fresh v3.0 dataset.
