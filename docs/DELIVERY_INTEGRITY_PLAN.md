# Delivery integrity — open work

**Produced:** 2026-07-20, against `main` @ `302d405` (clean tree).
**How:** five deep audit passes (one chain-integrity audit + four first-principles rethinks:
Windows delivery, Pi delivery, build provenance, release orchestration), each verified
independently against the code and against live registry queries.
**Status:** nothing in this document is implemented. It is a backlog for a future session.

> **Read this first.** Every claim below is marked VERIFIED (read in the code at `302d405`,
> or queried anonymously from GHCR/Docker Hub on 2026-07-20) or INFERRED (reasoned, with a
> named trigger, but not executed). Re-verify before acting — some of these are timing- or
> registry-dependent and will have moved.

---

## 0. What is already done (do not redo)

The 2026-07-19 review round landed on `main` in four commits (`2296b9c`, `10669ac`,
`3e91e62`, `302d405`) — 3 critical + 9 major + 13 hardening fixes, 36 files, +3376/−619.
Tests went 465→513 (gui/installer) and 322→353 (pi_agent); CI 22/22; `release-installer`
green (the `.iss` compiles). Highlights, so a future session does not re-report them:

- Pi `check_for_updates` now tracks `any_failed` and will not prune after a partial upgrade
  (`pi_agent/docker_manager.py`), and `prune_superseded_tags` no-ops under an
  `EDUBOTICS_IMAGE_TAG` pin — both twins symmetric with the GUI.
- The diagnostics sink moved to the leaf `%ProgramData%\EduBotics\logs` so the
  `Users:Modify` ACE is no longer on the parent of the WSL install root; `import_edubotics_wsl.ps1`
  refuses a reparse-point `-InstallRoot`.
- `ensure_environment_stopped()` hoisted above the rootfs gate in `_run_prerequisite_checks_body`.
- `verify_system.ps1` no longer exits 0 over a failed install (flag-mtime vs `LastBootUpTime`).
- `.iss` rootfs gate fails **open** on an unreadable stamp, converging with the GUI.
- The pi-agent self-repairs its own systemd units; the lifecycle lock is bounded with a German 503.
- `ps_cleanup_exit_guard` rewritten (it could not catch its own documented regression);
  all four PS guards refuse a zero-file scan; new `ci.yml::powershell-install-guards` job
  (**22 validator jobs**, not 21).
- `docker-publish` concurrency split into two lanes keyed on `github.ref`.

---

## 1. The single pattern behind almost everything

Across all five passes the recurring shape is:

> **A check exists, the author understood the risk, and the predicate cannot fire.**

| Where | The check | Why it cannot fire |
|---|---|---|
| Pi self-update | `compileall` on the staged tree | Parses, never imports. The comment at `agent.py:1350-1356` names the crash-loop it fails to prevent |
| Windows update | Content-Length **or** SHA-256 | Both gates are conditional; both can be off at once |
| `base-version-guard` | a `PAS_BASE_IMAGE` tag string moved | Cannot assert an image was ever built |
| `image_source_parity` | overlay files present in the image | Cannot detect that you edited the *other* copy |
| `docker-publish` smoke tests | pull `:latest` | Never verifies the `:X.Y.Z` students actually pull |
| `release.yml` | — | Cannot require `ci.yml`: no tag trigger, zero `workflow_run` in `.github/` |

Plus one structural inversion:

> **The golden order runs prepare and commit backwards.** W4 (images), W5 (`.exe`) and
> W5b (tarball) are *inert until advertised* — no client can reach them without the W6
> advertisement. W1 (migrations), W2 and W3 are *irreversible*. Today the irreversible
> steps run first, so a `.iss` Pascal error in W5 lands **after** migrations and two
> Railway deploys.

---

## 2. The four mechanisms

The design principle: **replace "the operator remembered" with "the build asserts."**

There are three different bridges from repo → image, with three different guarantees:

| Bridge | Used by | Guarantee today |
|---|---|---|
| COPY-wholesale | `physical_ai_server` | **Airtight** — `diff -r` byte-parity in CI |
| Overlay chain | `open_manipulator` | 7 hand-listed files; **5 already diverged**; editing the natural path ships nothing |
| Hand-pushed base | all 3 self-built bases | **No link to its Dockerfile at all** |

### M1 — Bases describe themselves; the build verifies

In `build-images.sh`'s `BUILD_BASE_*` branches, stamp:

```bash
--label edubotics.base.dockerfile_sha256="$(sha256sum "$BASE_DOCKERFILE" | cut -d' ' -f1)"
--label org.opencontainers.image.revision="$(git rev-parse HEAD)"
```

Then in the **resolve** branches (`build-images.sh:495`, `:659`) — which every CI build already
hits — read the label back and assert it equals the Dockerfile at HEAD. Refuse the build
otherwise, naming the exact rebuild command.

Strictly stronger than `base-version-guard`: compares **content** not a string; covers
`OMX_BASE_IMAGE` with no extra code; a cosmetic tag bump cannot satisfy it; fires at *build*
time so it also catches a base rebuilt from a stale checkout.

**The key consequence: you stop caring where the base was built.** A hand-pushed base becomes
*provably* correct rather than *probably* correct. This is why M1 must land **before** any
"move base builds into CI" work, not after. It makes `base-version-guard` redundant.

### M2 — Declare every bridge, then check completeness

The overlay model's stated justification (C++ re-`colcon` cost) is **void** — see §5.
So:

1. Make the in-repo path authoritative: overwrite the 5 diverged in-repo files with the
   overlay content, delete `robotis_ai_setup/docker/open_manipulator/overlays/`, and stage
   from `open_manipulator/…` exactly as `pkg_src/` is already staged.
2. Keep `apply_overlay`'s sha256 pre/post verification — it genuinely proves the upstream
   target existed and was not renamed.
3. Repoint `image_source_parity.sh` at the repo path, so it verifies **the file people edit**.
4. Add `overlay.manifest` (`repo_path → image_target`) plus the assert that closes the loop:
   **any file under the declared directories that differs from the image tree and is not in
   the manifest fails the build.**

Scope the assert to declared directories (`bringup/launch/`,
`bringup/config/omx_f_follower_ai/`, `bringup/config/omx_l_leader_ai/`,
`description/ros2_control/omx_*`) — the repo tree and the base clone sit at different
upstream revisions, so a whole-tree diff would be noise.

**Do NOT COPY-wholesale the absorbed tree.** That *would* require the re-`colcon` and would
couple the image to upstream-revision alignment, which differs per flavor (see §5c).

### M3 — Promote by digest, never by a mutable tag

`build` emits each pushed digest as a job output → `retag` creates every tag **from that
digest** → `smoke-test` pulls **by digest** and asserts the semver tag resolves to it.
Extend the revision assert from `server_repo` to all three matrix repos
(`docker-publish.yml:836` is currently server-only).

Kills the re-run hazard structurally: a `retag` re-run with no `build` job has no digest and
fails loudly instead of silently promoting whatever `:latest` has become.

### M4 — Prove it landed, once, at the end (`W7 release-verify`)

New terminal inline job, `needs:` everything, `if: always() && startsWith(github.ref, 'refs/tags/v')`,
`permissions: {}`. Asserts **live surfaces**, not job results:

| # | Surface | Assertion |
|---|---|---|
| 1 | Supabase + cloud API | `GET /health` → `status=="ok"` ∧ `schema_ok` ∧ `commit==github.sha` ∧ `version==${TAG#v}` |
| 2 | Teacher web | `/version.json` `buildId` contains the sha |
| 3 | Images | anonymous manifest probe, **all 8 repos at `:X.Y.Z`**, GHCR **and** Hub twin |
| 4 | `.exe` + Windows advert | `/version` `version==${TAG#v}` ∧ HEAD `download_url` 200 ∧ `installer_sha256` == sha256 of that asset |
| 5 | Tarball + Pi advert | HEAD `pi_agent_download_url` 200 ∧ `pi_agent_sha256` non-empty ∧ == sha256 of that asset |
| 6 | *(later)* Modal | `/health.modal_lerobot_version == LEROBOT_VERSION` |

Emit one ✓/✗-per-surface table to **`$GITHUB_STEP_SUMMARY`** — there are currently **zero**
occurrences of `GITHUB_STEP_SUMMARY` in the entire `.github/` tree.

**W7 must not auto-rollback.** A transient probe failure yanking the fleet's update is worse
than a red X. It reports; the operator executes the documented rollback (§4, item 7).

#### Coverage across the three platforms

| | Windows | Jetson | Pi |
|---|---|---|---|
| Self-built base staleness | n/a (upstream base) | **M1** | **M1** |
| Upstream base drift | `BASE_DIGESTS.lock` | lock | lock |
| Skipped source edit | **M2** | **M2** | **M2** |
| Tag ≠ verified artifact | **M3** | **M3** | **M3** |
| Artifact reached the student | **M4** + forced modal | n/a | **M4** + opi gate red on tags |
| Half-applied on-device state | rootfs gate + finalize contract | — | Pi manifest + boot `converge()` |

**Order:** M1 → M3 → M2, with M4 alongside. M1 first because it makes every base provable
regardless of where it was built; M2 last of the three because collapsing the duplicate is
the only one with real churn.

---

## 3. Before the next tag — ~1 day, all small, high value

| # | Fix | Prevents |
|---|---|---|
| 1 | **Pi: import-check the staged tree** — `python3 -c "import pi_agent.agent"` against `pi_agent.new` before the swap, not just `compileall` (~5 lines) | **Fleet bricked, SSH-only recovery** |
| 2 | **Windows: refuse the download when neither verification gate can run** (~5 lines) | **Unverified `.exe` run as admin** |
| 3 | **Copy the 5 overlay files into their in-repo paths** (~1 h) | Safety edits silently not shipping |
| 4 | **Scope opi `continue-on-error` to non-tag refs** | Silent fleet strand |
| 5 | **`W7 release-verify`** (M4) | Green-but-broken **and** red-but-complete |
| 6 | **CI gates the tag** — `checks: read` poll in `version-preflight`, **empty ≡ FAIL** | Releasing over red validators |
| 7 | **Document the rollback** | No recovery story exists today |
| 8 | **Pi: free-space precondition + move the `.env` pin after the pull succeeds** | eMMC exhaustion; pinning to an unpullable ref |

### Detail

**1 — Pi import-check.** `agent.py:1356-1365` runs `compileall`, which parses but never
imports. A release adding a module-level third-party import passes, the swap commits, and the
new agent `ImportError`s under `Restart=always` / `RestartSec=5`. The recovery path does not
fire: `ExecStartPre` (`systemd/edubotics-pi.service:59`) is
`[ -d …/pi_agent ] || { mv pi_agent.old pi_agent; }` — it restores only when the tree is
**absent**; a present-but-broken tree is never rolled back and `pi_agent.old` sits unread.
Result: every Pi unreachable except by SSH. Like every apply-path fix this lands one release
late, so ship it now to arm the release after. *Consider also* widening `ExecStartPre` to
detect a broken-but-present tree.

**2 — Windows download verification.** `gui/app/update_checker.py`: gate (A) `if total > 0 and
downloaded != total` no-ops when `Content-Length` is absent (chunked response); gate (B)
`if expected and …` no-ops when `installer_sha256` is empty — which **W6 deliberately does on
a hash failure**. With both off the function returns the path, the computed SHA is discarded,
and `gui_app.py:2136` `os.startfile`s an installer that is `PrivilegesRequired=admin`. Named
trigger: a TLS-inspecting school proxy re-chunking the GitHub asset redirect, on a release
where W6's hash step degraded. Fix: refuse when neither gate can run.

**3 — Overlay reconciliation.** See §5. Do this as a pure content copy first — it makes the
natural edit path *correct* even before M2's plumbing lands, at near-zero risk and with no
build change.

**4 — opi on tags.** The `continue-on-error` decoupling exists so a routine `main` push is not
blocked by a Rockchip hiccup. That rationale does **not** transfer to a tag, which is a
fleet-wide promise. Verify the `github.ref` value inside a `workflow_call` on a tag push
before relying on the expression (INFERRED).

**6 — CI gates the tag.** `workflow_run` is structurally wrong (fires only for default-branch
workflows; `ci.yml` never runs on a tag ref). Use a Checks-API query inside `version-preflight`:
add `checks: read`, query `repos/{owner}/{repo}/commits/${GITHUB_SHA}/check-runs`, **poll
until all conclude** (raise `timeout-minutes` from 5 to ~40 — `git push && git push --tags`
means CI on main is still in flight). **An empty result set must be FAILURE** — that is the
"tagging a non-`main` commit" case, and treating it as success is the exact fail-open this
check exists to prevent. Escape hatch: add `workflow_dispatch:` to `ci.yml` (it has none), so
recovery is `gh workflow run ci.yml --ref <sha>` → wait → re-push the tag.
`version-preflight` is an **inline job, not a `uses:` callee**, so the caller-permissions-superset
rule is not engaged.

**7 — The rollback that already works.** Because both update checkers compare with a strict
`>` on a padded numeric tuple (`gui/app/update_checker.py:78`, `pi_agent/update_checker.py:121`),
and the Pi additionally refuses on an empty hash *before* it even HEADs the asset:

```bash
railway variable set GUI_VERSION=<previous>   # un-advertises the release, fleet-wide
```

No client ever downgrades; no client downloads against a stale hash. **This is a genuine
one-variable kill switch for the entire student-facing release and it is documented nowhere.**
Write it into a runbook together with the per-surface recovery for each failure state
(`docs/deploy/PIPELINE.md` and `DEPLOY.md` are both banner-DEPRECATED and should be replaced
or removed).

---

## 4. Soon — ~3–4 days

- **Prepare→commit reorder.** `needs:`-edges only (~15 lines):
  `version-preflight → W4 → W5 → W5b → W1 → W2 → W3 → W6a → W6b → W7`.
  Every documented ordering constraint survives; nothing in W4/W5/W5b reads Supabase or the
  cloud API. Payoff: every failure in the two most expensive, most failure-prone stages
  (W4 ~60 min, W5 ~90 min) leaves production 100 % untouched.
  **Honest trade:** the W2 schema-probe — currently the earliest signal that migrations and
  code are coherent — moves from ~minute 10 to ~minute 155. The proper fix for that already
  exists as `supabase-migrate.yml::apply-branch`, switched off behind
  `vars.SUPABASE_BRANCHING_ENABLED` + Supabase Pro. That is a pricing decision.

- **W6a/W6b as publisher + _publisher_** (not publisher + verifier). An earlier proposal in
  this session — W6a publishes, W6b asserts — was **wrong**: `PI_AGENT_SHA256` is written only
  by W6a, so publishing the missing opi images and re-running W6b re-probes, finds them, then
  asserts against a still-empty Railway variable and fails again. *The split would move the
  failure signal without moving the repair.* Correct shape:
  - **W6a** `publish-gui-version` — `needs: installer`. Writes `GUI_RELEASE_REPO`,
    `GUI_DOWNLOAD_URL`, `GUI_INSTALLER_SHA256`, **and explicitly `PI_AGENT_SHA256=""`**, all
    `--skip-deploys`; then `GUI_VERSION` (one redeploy). Verify loop drops the Pi assertions.
  - **W6b** `publish-pi-version` — `needs: [W6a, pi-agent-tarball]`,
    `if: always() && needs.publish-gui-version.result == 'success'`. Hashes the attached
    tarball, runs the 3-repo GHCR→Hub probe, writes `PI_AGENT_SHA256` **without**
    `--skip-deploys`, verifies `/version`. **Red on failure, never a `::warning`.**
  - Writing `PI_AGENT_SHA256=""` first makes the stale-hash window *structurally impossible*
    rather than merely ordered around. Both jobs stay `permissions: {}`.
  - **Cost:** two Railway redeploys per release instead of one (env is injected at container
    start). Blue-green, so ~60 s with no traffic impact — but it is real.

- **Rebuild the two Jetson bases** with the M1 label and bump `PAS_BASE_IMAGE`. Clears 9
  commits of drift and bootstraps the mechanism.

- **Digest→tag flow** (M3).

- **Pi manifest + boot-time `converge()`.** The tarball already ships `setup.sh`,
  `requirements.txt`, `udev/` and `systemd/`; only `docker-compose.opi.yml` and `.s6-keep` are
  missing. Add them under `pi_agent/system/`, ship a `MANIFEST.json`
  (`{version, image_tag, images[], files:{path→sha256, dest}}`) gated in W5b the same way
  `VERSION` and `versions.env` already are, and generalize the existing unit self-repair into
  a manifest-driven convergence loop run on every boot before `start_manager`. This is what
  makes a Pi's state a function of the installed tarball rather than of its provisioning
  history — a golden clone and a self-updated Pi then provably converge.

- **Pi: swap first, then pull.** The pre-swap `check_for_updates` (`agent.py:1164`) resolves
  `ALL_IMAGES` from the module-level **old** `IMAGE_TAG`, whose images are already local — the
  digest pre-check skips every one, so it is a **no-op on the healthy path** (INFERRED) while
  putting the old agent in the image-mutation path. Reorder to
  download → verify → compile → import-check → swap → restart → converge → pull.
  **One real loss to handle:** the pre-swap pull is what reconciles a *re-pointed same-version
  tag* (a retried release), because `_pull_images_after_self_update` fires only on an
  `IMAGE_TAG` **mismatch** (`agent.py:2305`). Make the boot pull digest-driven off the manifest
  instead.

---

## 5. Later

- **Collapse the `open_manipulator` duplicate** (M2 steps 1–3). The C++ blocker is void.
- **`overlay.manifest` + declared-directory completeness assert** (M2 step 4) — the only
  genuinely new mechanism; build it after the collapse lands.
- **`BASE_DIGESTS.lock`** — five `<ref> <digest>` lines (2 upstream ROBOTIS + 3 self-built),
  asserted on resolve. `bump-upstream-digests.sh` already computes every value; it becomes the
  lock's generator and verifier rather than a report nobody reads. Prefer this over wholesale
  `FROM …@sha256:` conversion: keeps the readable tag, covers refs that live in the shell
  script rather than a `FROM`, one reviewable diff per bump.
- **Aggregate fleet check-in.** No device ID needed: the Windows update modal is *forced*
  (`gui_app.py:1085`), so a stale Windows PC self-heals on next launch. The question is
  aggregate — "are check-ins on the new version rising while the old version's fall?" Both
  clients already make exactly one anonymous GET to `/version`; add query params to the URL
  they already build (`gui/app/update_checker.py:69`, `pi_agent/update_checker.py:110` —
  `APP_VERSION` is already passed into the GUI function at `gui_app.py:1080` and discarded).
  Cloud side: increment a counter keyed `(day, kind, app_version, image_tag)` as a
  fire-and-forget background write. **No device id, no user id, no IP stored.** Surface on the
  **admin** card, not the teacher dashboard. Known limitation: aggregate counts cannot name a
  machine that stops checking in; if per-device is ever needed, copy
  `jetson_agent/agent.py:476-501`, which already does exactly this with a token and a deletion story.
- **`/health` gains `gui_version`** (+ lazy `modal_lerobot_version`), asserted by W7. Note
  `main.py:154-160`'s existing drift guard is structurally guaranteed to fire on *every*
  release at W2 and go quiet only after W6 — a permanent false positive that trains the
  operator to ignore it.
- **`workflow_dispatch` base-build workflow** on `ubuntu-24.04-arm`. **Measure native colcon
  time first.** Never a *schedule* — that would silently re-push bases and change student
  images with no review.
- **Modal provability.** Step 1 (cheap, now): in `version-preflight`,
  `git diff --name-only <prev-tag>..HEAD -- robotis_ai_setup/modal_training/`; if non-empty,
  require the annotated tag message to carry a `Modal-Deployed: <sha>` trailer (needs
  `fetch-depth: 0`). A *claim*, not a verification — but it converts a silent omission into a
  deliberate act. Step 2 (later): the cloud API already holds `MODAL_TOKEN_ID/SECRET`; expose
  the deployed app's `LEROBOT_VERSION` on `/health` lazily, non-fatal at boot, asserted by W7.
- **Retire `base-version-guard`** once M1 is live and proven. Add `physical_ai_interfaces` to
  the parity check.
- **Hygiene:** `base-digest-check` is double-nullified (`continue-on-error: true` **and**
  `|| echo "::warning::"`); the opi 11 GB size gate can never fail the run (it is inside the
  `continue-on-error` leg); there are four separately-hardcoded repo lists in `docker-publish.yml`.

---

## 6. Explicitly not worth building

- **cosign / SLSA / in-toto attestation, SBOM.** Signs the trust you already have. Answers
  "who pushed this", not "does this match the Dockerfile" — the only question that has
  actually bitten. Negative correctness return for a one-maintainer team.
- **Bit-for-bit reproducibility chasing.** `flatten_image`'s `docker export | import` stamps
  wall-clock `created` by design. The `revision` label gives what reproducibility would, at ~0 cost.
- **Auto-rollback on W7 failure.**
- **Per-device inventory / teacher-facing device dashboard.** Imports GDPR obligations to
  answer a question the forced modal already answers.
- **Two-phase-commit machinery for Railway/Supabase** (staging project, blue-green schema,
  shadow deploys). The §4 reorder gets ~90 % of the atomicity for a `needs:`-edge change.
- **`workflow_run` chaining.** Structurally cannot see tag refs.
- **Moving `modal deploy` into CI.** Rule §6 records the debugging-loop rationale.
- **COPY-wholesale for `open_manipulator/`.** Buys the re-`colcon` cost currently avoided and
  forces upstream-revision alignment across two differently-pinned flavors.
- **Re-running the installer pre-tag as a proving build.** 90 min duplicated; the §4 reorder
  makes it unnecessary.
- Metrics stack, status page, canary/staged rollout, release-candidate tag namespace.

---

## 7. Open residuals carried from the 2026-07-19 fix round

Each is documented in `CLAUDE.md` so it does not read as closed:

1. `C:\ProgramData` ships an inherited `BUILTIN\Users:(CI)(AD)`, so a standard user can still
   pre-create `…\EduBotics\wsl` as a **plain user-owned directory**. The junction /
   elevated-arbitrary-write escalation *is* closed; this variant is not. Closing it means
   resetting ownership on `-InstallRoot`, risking WSL's own access to the VHDX.
2. W6's opi gate is **existence-only**. A retried release where opi built on attempt 1 but
   failed on attempt 2 passes it. `release.yml` already explains correctly why marker-based
   provenance cannot work there.
3. `gui_app.py` early returns at the reboot-pending and distro-missing paths lack a teardown.
   Distro-missing has nothing to tear down; reboot-pending precedes the dockerd gate so a
   teardown would silently no-op. Covering it properly means booting dockerd before the fast
   finalize route.
4. Pi compose / `.s6-keep` drift is unprovable — fixed by the §4 manifest work.
5. A Windows username containing `$` could reach the paste-fallback command via the
   `%LOCALAPPDATA%` fallback path and be interpolated by the interactive PowerShell.

---

## 8. Appendix — verified facts (2026-07-20)

**Base images (anonymous registry queries):**
- `nettername/physical-ai-server-jetson-base:0.8.2` — pushed **2026-05-17**;
  `Dockerfile.arm64` has **9 commits since** (`ee6197f`, `16b8378`, `f3d3c58`, `24ac7b9`,
  `807b2c2`, `a5dc69b`, `3fd4596`, `cdcffbe`, `9686907`) including `a5dc69b` (`numpy<2`,
  cv_bridge ABI segfault) and `cdcffbe` (`scipy<1.18`) — both Rule §5 hard caps. The pin at
  `build-images.sh:89` never moved. Survives only because the thin overlay redoes
  lerobot/numpy/scipy/control-msgs; **torch is inherited and merely asserted**.
- `nettername/open-manipulator-jetson-base:4.1.4` — pushed 2026-05-17. Pinned as
  `OMX_BASE_IMAGE` (`build-images.sh:88`, `:111`) and fenced by **nothing**:
  `ci.yml:262`'s `pin_tag()` matches only `PAS_BASE_IMAGE=`, and `:295`/`:298` check only two
  files, neither of which is `open_manipulator/docker/Dockerfile`. Latent, not live — that
  file last changed 2026-04-10, before the base push.
- `nettername/physical-ai-server-opi-base:0.8.2-opi2` — pushed 2026-07-16, carries
  `revision=968690788ec1`.
- **Both Jetson bases carry no `revision` label at all** — they predate `OCI_LABELS`. Not
  merely stale: untraceable to any commit.
- `robotis/physical-ai-server:amd64-0.8.2` → `sha256:3652bae8…`, `robotis/open-manipulator:amd64-4.1.4`
  → `sha256:02ac3795…`, both pushed 2026-03-18. The latter's layer history shows
  `git clone -b jazzy … open_manipulator.git` with **no `git checkout`** — the amd64 fleet's
  `open_manipulator` source revision is pinned nowhere.

**Overlay divergence — 5 of 7 differ from their in-repo twins:**

| overlay | in-repo twin | Δ lines |
|---|---|---|
| `omx_f_hardware_controller_manager.yaml` | `bringup/config/omx_f_follower_ai/hardware_controller_manager.yaml` | **83** |
| `omx_l_leader_ai_hardware_controller_manager.yaml` | `bringup/config/omx_l_leader_ai/hardware_controller_manager.yaml` | **49** |
| `omx_f_follower_ai.launch.py` | `bringup/launch/` | 29 |
| `omx_f.ros2_control.xacro` | `description/ros2_control/` | 23 |
| `omx_l_leader_ai.launch.py` | `bringup/launch/` | 4 |

The divergence direction is **in-repo = stock upstream**. The in-repo
`hardware_controller_manager.yaml` still carries `goal_time: 1.0`, `trajectory: 0.15`,
`goal: 0.05` — the exact upstream values CLAUDE.md records as having aborted the 3 s quintic
boot-sync into a compose restart-storm — and contains **`gpio_command_controller` 0 times**
versus **3** in the overlay, i.e. none of the collision e-stop's force-signal surface.
Likewise the `Present Load` / `Present Current` / `Hardware Error Status` state interfaces
exist **only** in the overlay xacro.

**The overlay model's justification is void:** all 7 overlays are `.py`/`.xacro`/`.yaml`
(**zero C++**); `robotis_ai_setup/docker/open_manipulator/Dockerfile` contains **zero**
`colcon` invocations; the base builds with `--symlink-install`. Nothing recompiles after the
overlay lands.

**CLAUDE.md routes maintainers to the dead copy** — it cites
`open_manipulator/open_manipulator_bringup/launch/omx_f_follower_ai.launch.py:161` for the
`/leader/joint_trajectory` command rail, but in *that* file the remap is at line **144**;
line 161 is where it lives in the **overlay**.

**Release plumbing:**
- `ci.yml` triggers are `push` (branches: main) and `pull_request` only — **no
  `workflow_dispatch`, no tag trigger**; **zero `workflow_run` anywhere in `.github/`**. 22
  parallel jobs, none `continue-on-error`, so there is no aggregate check-run to query.
- `docker-publish.yml` `SRC_TAG="latest"`; all smoke assertions pull `:latest`; the revision
  assert is scoped to `server_repo` only.
- `-opi:2.12.1` does **not exist** on either registry — the opi decoupling produces missing
  semver tags in practice, not just in theory.
- At `:2.13.0` all 8 repos on both registries carry `revision = 968690788ec1` (the tag commit);
  at `:latest`, `302d405ca14a`. **Every GHCR digest equals its Hub twin.** The dual-push is correct.
- At `:2.13.0` the three flavors carry three *distinct* `created` timestamps, none equal to
  the tag commit's time, whereas `:latest` matches its commit time exactly (VERIFIED; the
  mechanism — the two same-sha lanes interleaving over a mutable `:latest` — is INFERRED).

**Pi:**
- `agent.py:1164` `check_for_updates` runs **before** `:1230` `_apply_agent_update_and_restart`.
- `systemd/edubotics-pi.service:59` `ExecStartPre` restores only when `pi_agent/` is absent.
- **No free-space precondition exists anywhere in `pi_agent/`** (Windows gates at 20 GB).
- `.env` `IMAGE_TAG` is advanced at `agent.py:2310`, before the pull proves the images exist
  at `:2360`, with no rollback.
- The tarball ships `setup.sh`, `requirements.txt`, `udev/`, `systemd/`; `requirements.txt`
  and `udev/` are **never consumed** by the agent.

**Both update checkers** compare with a strict `>` on a padded numeric tuple
(`gui/app/update_checker.py:78`, `pi_agent/update_checker.py:121`); a malformed version parses
to `(0,0,0)`. Hence the `GUI_VERSION` rollback is safe.

---

## 9. INFERRED — verify before relying on

- The `github.ref` value inside a `workflow_call` on a tag push (§3 item 4).
- GitHub "Re-run failed jobs" resume semantics for reusable-workflow caller jobs.
- `actions/upload-artifact` failing on a duplicate name without `overwrite: true` (the opi
  marker upload).
- `railway variable set --skip-deploys` not reaching an already-running pod.
- Which Checks-API route needs `checks: read` vs `actions: read`.
- That the pre-swap Pi pull is a no-op on the healthy path.
- That a native arm64 base build fits inside the GHA job limit (the ~14 h figure observed in
  the Jetson OMX base's layer history is QEMU-on-a-Mac and possibly a paused build —
  **measure once**).
- That `torch` specifically is what the thin overlay does not redo on the Jetson path (from
  CLAUDE.md's account, not re-derived from the Dockerfiles).
