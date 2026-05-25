# Continue the LeRobot v0.5.1 upgrade — Windows session kickoff

Branch: `feat/lerobot-v0.5.1-dataset-v3`. The Mac session did Layers 1,2,3,5,6,7,8 +
an independent 4-agent code review (fixes applied) + a final read-only investigation.
**The image is NOT bootable yet** — Layer 4 (recording rewrite) is the remaining blocker.

Full status + the verified, gap-closed Layer-4 spec: **`docs/LEROBOT_V051_UPGRADE_STATUS.md`**
(read it first — especially the "CODE REVIEW" and "FINAL INVESTIGATION" sections).

## Paste this into the new Claude session

```
We're finishing a LeRobot 0.2.0 -> v0.5.1 (dataset v2.1 -> v3.0) upgrade on branch
feat/lerobot-v0.5.1-dataset-v3. A previous session did Layers 1-3,5-8 + a code review
+ a final investigation, all pushed. Read docs/LEROBOT_V051_UPGRADE_STATUS.md IN FULL
and CLAUDE.md §5 first, then `git log --oneline -5` to confirm state.

The image is currently NON-BOOTABLE: overlays/lerobot_dataset_wrapper.py still imports
removed v2.1 symbols. Finish the work, in order:

1. LAYER 4 (the blocker): rewrite robotis_ai_setup/docker/physical_ai_server/overlays/
   lerobot_dataset_wrapper.py + data_manager.py to the v3.0 DatasetWriter API. The status
   doc has the full corrected spec PLUS 7 extra gaps (resume() not __init__, write_episode_stats
   removed, vcodec="h264", finalize() before upload, create_tag v3.0, the dead encoder
   progress-bar, per-episode->shared mp4, codebase_version gate, etc.). Plan it first.
2. Build the student image (robotis_ai_setup/docker/build-images.sh) — verifies the
   self-managed v0.5.1 install, the numpy 1.x->2.x ABI smoke, and CODEBASE_VERSION==v3.0.
3. Add the CI boot-import job to .github/workflows/ci.yml (imports the overlay managers
   against installed v0.5.1 so this class of boot crash can't be a silent false-green).
4. Modal: `cd robotis_ai_setup/modal_training && modal deploy modal_app.py` then
   `modal run -m modal_app::smoke_test`; then a real train on a freshly-recorded v3.0 dataset.
5. Verify record -> train -> infer end-to-end on the robot.
6. arm64/Jetson: confirm the jetson-ai-lab cu126 index serves v0.5.1 deps (may be pruned);
   reconcile Dockerfile.arm64 stale pins (numpy<2, protobuf 6.31.0, SHA 989f3d05).
7. PR3 (operator): Railway GUI_VERSION/GUI_DOWNLOAD_URL + `gh release create v3.0.0`.

You have Docker/WSL + robot hardware here, which the Mac session did not. Use plan mode
for Layer 4 before editing.
```

## Remaining checklist (mirror of the status doc)
- [ ] Layer 4 recording rewrite (wrapper + data_manager) — **blocker**
- [ ] CI boot-import job
- [ ] Build both arches; numpy-2 ABI smoke passes; base Python ≥3.12 confirmed
- [ ] Modal deploy + smoke + real train on a v3.0 dataset
- [ ] record → train → infer e2e on hardware
- [ ] arm64 base pin reconcile + jetson-ai-lab cu126 wheel check
- [ ] PR3: Railway env + GitHub release v3.0.0
