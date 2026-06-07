# Next-session TODO — post-v2.6.1, after the 2026-06-07 hygiene batch

State: the 2026-06-06 follow-up list is **done** except the items below. The hygiene
batch — migration 027 (applied + verified in production), Jetson digest-precheck port,
floating-`:X.Y` retag fix, Inno Setup 6.7.0 pin, `.gitattributes` LF pins, image
whiteout-strips, AST German lint + full Rule-§1 sweep (28 strings), CLAUDE.md sync,
`modal-cleanup.sh` CLI fix, Modal + local-Mac cleanup — is on `main`
(`9448cd5..`, see CLAUDE.md "Recent changes — post-v2.6.1"). Nothing blocks students.

---

## 1. Operator actions still open (dashboards/credentials — cannot ship via CI)

| # | Action | Where | Notes |
|---|---|---|---|
| 1.1 | Run `tools/docker-hub-cleanup.sh` dry-run → `--execute` | dev terminal | needs `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN` (delete scope); 5 junk repos + 12 `*-dirty` tags ≈ 81 GB registry |
| 1.2 | (optional) Band-aid the floating `:2.6` tag now | dev terminal | `docker buildx imagetools create -t nettername/<repo>:2.6 nettername/<repo>:2.6.1` × 5 repos (hub login required). Otherwise `:2.6` self-heals at the next `vX.Y.Z` tag via the fixed retag job |
| 1.3 | Delete residual `REACT_APP_BUILD_ID` service var | Railway → teacher-web → Variables | belt-and-suspenders vs the 2026-05-19 frozen-bundle bug |
| 1.4 | Remove dead `RUNPOD_*` secrets | Railway → scintillating-empathy → Variables | no in-code consumer (audit-confirmed) |
| 1.5 | Enable leaked-password protection (HIBP) | Supabase dashboard → Auth → password security | long-standing advisor item |

## 2. Rollout watch

- **2.1 Reference rig runs the TEST artifact** (pre-tag build: pinned `IMAGE_TAG=2.6.0`, self-reports 2.6.1 → never auto-updates itself). Reinstall the real `EduBotics_Setup.exe` from the v2.6.1 GH Release on it.
- **2.2 Authenticated `/vision/detect` end-to-end check** from the React Roboter-Studio KI-Block on next rig login (30 s). The `788287e` fix is deployed + SDK-level-verified; a real student-JWT call wasn't exercised yet.
- **2.3 Support watch**: student upgrades ≤2.6.0 → 2.6.1 re-import the distro ONCE (German consent dialog; release notes warn to upload datasets first) — expect questions from non-readers.
- **2.4 Next `vX.Y.Z` tag**: confirm the floating `:X.Y` tag advances (the retag fix only exercises on tag pushes — `docker buildx imagetools inspect nettername/<repo>:X.Y` digest must equal `:X.Y.Z`'s).

## 3. Decision record (don't re-propose without new evidence)

- **docker-publish KEEPS its tag-push trigger** (dual-fire with release.yml W4 accepted for faster raw-image availability) — operator, 2026-06-06.
- **v2.6.0 GH release-notes backfill: skipped** by operator (2026-06-07). v2.6.1 is the advertised release; 2.6.0's empty body stays.
- **Local Mac docker intentionally keeps** `postgres:17` (supabase CLI), `nginx:1.27-alpine`, `edubotics-rootfs:latest`; everything else reclaimed 2026-06-07 (128.9 → 1.6 GB).
