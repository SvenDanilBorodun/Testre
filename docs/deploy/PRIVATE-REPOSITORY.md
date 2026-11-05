# Making this repository private without breaking updates

Every step that touches GitHub settings, Railway or a repository's visibility is an owner action (CLAUDE.md, "When to ask the user").

---

## What depends on this repository being public

| Thing | Depends on it? | Why |
|---|---|---|
| GUI and Pi self-updates | **Yes**, until the release channel moves | `/version` hands out `https://github.com/<GUI_RELEASE_REPO>/releases/download/v<X.Y.Z>/…`. A private repository's release assets answer **404** to anonymous clients; `update_checker` reads 404 as "gone" and silently offers no update. |
| W6's hash downloads | Yes, same reason | `curl` without a token |
| GHCR images (11 packages) | **No** | A package's visibility is its own and stays public. Anonymous pulls, RunPod's trainer pull and release.yml's anonymous-pull probes keep working. A **new** package name starts private and must be made public by hand on its first push. |
| Docker Hub `nettername/*` | No | |
| First installs | No | The installer is downloaded from the release channel, the images from GHCR |
| Servo-Station test PC | **Yes** | It runs from a checkout of this repository (`docs/servo-station.md`). It needs a GitHub login after the switch. A third party gets a zip of `tools/servo_station/`, never collaborator access. |
| CI | Changes, see the plan section | Private runners are 2 vCPU / 8 GB instead of 4 / 16, and compute is billed beyond the plan's included amount |

---

## The release channel

Both asset uploads (`release-installer.yml`, `release.yml::pi-agent-tarball`) and W6 (`release.yml::publish-gui-version`) resolve the target repository from `${{ vars.RELEASES_REPO || github.repository }}`:
- **Variable unset:** assets go to this repository (the behaviour before 2026-11-05).
- **Variable set** (e.g. `SvenDanilBorodun/edubotics-releases`): assets go to that public repository, written with `secrets.RELEASES_REPO_TOKEN`. A check step before each upload fails when the variable is set but the token is missing.

W6 then sets Railway's `GUI_RELEASE_REPO` and `GUI_DOWNLOAD_URL` to the public repository. GUIs and Pis follow on their next check, with no client update.

**`version-preflight` checks the channel for the whole release before W1** (tag pushes). The release stops before any migration or deploy when:
- this repository is private and `RELEASES_REPO` is unset (fails closed on an unknown visibility);
- the variable is set without the token;
- the token is rejected by the GitHub API (expired, revoked, no access);
- the token cannot write there (the preflight creates and deletes a draft release, invisible to the public);
- the channel repository is not public.

The fine-grained token expires: **renew it before its expiry date**, or the next tag stops in the preflight.

### Moving the channel

1. **Create the public repository.**
   - It holds a German README (what EduBotics is, system requirements, that updates install themselves, how to compare the SHA-256) and the EULA as `LICENSE`.
   - **No code, ever.**
2. **Token (browser only; GitHub has no API for creating PATs).**
   - Settings → Developer settings → Fine-grained tokens.
   - Repository access: **only** the public repository; permission **Contents: Read and write**; expiry ≤ 1 year (note the date).
   - Store it as the Actions secret `RELEASES_REPO_TOKEN` on this repository.
3. **Copy the current release there**, byte for byte, so the advertised hashes stay valid:
   ```bash
   V=v2.37.0   # whatever /version advertises today
   mkdir -p /tmp/rel && cd /tmp/rel
   gh release download "$V" -R SvenDanilBorodun/Testre -p EduBotics_Setup.exe -p edubotics-pi-agent.tar.gz
   shasum -a 256 EduBotics_Setup.exe edubotics-pi-agent.tar.gz   # must equal /version's installer_sha256 / pi_agent_sha256
   gh release create "$V" -R SvenDanilBorodun/edubotics-releases EduBotics_Setup.exe edubotics-pi-agent.tar.gz --title "$V" --notes ""
   curl -fsSIL "https://github.com/SvenDanilBorodun/edubotics-releases/releases/download/$V/EduBotics_Setup.exe" | head -1
   ```
4. **Point the update gate at it** (Railway service `scintillating-empathy`, production):
   - set `GUI_RELEASE_REPO` with `--skip-deploys`;
   - set `GUI_DOWNLOAD_URL` without it, so exactly one redeploy happens;
   - then check that `curl -s …/version` shows the new URLs with the **same** hashes.
5. **Set the repository variable** `RELEASES_REPO` to the public repository. From now on every release uploads there.
6. **Mirror the previous release too** (`--latest=false`), so the documented rollback (install the older `.exe` and pin) stays downloadable after the flip. v2.36.0 is mirrored.
7. **Prove it:**
   - one Windows PC and one Pi check for updates (same version → no update; the forced-update path works);
   - the next tagged release runs W5 → W5b → W6 through the new channel.

---

## Choose the plan before the flip

| Plan | Private-repo protection | Included compute |
|---|---|---|
| Free | No protected branches, rulesets, environments or environment secrets | $12/month |
| Pro | Protected branches; environments with deployment branch/tag policies and environment secrets. **Required reviewers and wait timers are public-repo-only on Free/Pro/Team.** | $18/month |

- **Cost:** October 2026 used $28.68 of compute on 4-core public runners; 2-core private runners take roughly 1.3–2× as long. Without a payment method, Actions stops once the included amount is used.
- **Raise** the 60-minute build timeouts before the flip.
- **Options to cut minutes:** full image builds only on tags or image path changes; cancel superseded runs; shorter artifact retention; or a self-hosted runner (private repositories only).

---

## Flip, then verify the same hour

1. Settings → General → Danger Zone → Change visibility → Private.
2. Verify:
   - CI is green on a no-op commit;
   - `/version`'s URLs answer 200 anonymously and their hashes match;
   - anonymous `docker manifest inspect` works on all 11 GHCR images and the Docker Hub twins;
   - a training starts (RunPod pulls `edubotics-trainer`);
   - the W2/W3 health gates pass;
   - one PC's forced-update path works.

### What the flip changes

- **Forks:** public forks are detached and **stay public**. `SWFDtf/Testre` keeps the history up to 2026-06-19.
- **Stars and watchers** are erased; the repository leaves the GitHub Archive Program.
- **Secret scanning and push protection** stop for a user-owned private repository. Keep the CI `secret-scan` job and add a local hook:
  ```bash
  printf '#!/bin/sh\nexec gitleaks git --pre-commit --staged --redact --no-banner\n' > .git/hooks/pre-commit
  chmod +x .git/hooks/pre-commit
  ```
- **Dependabot alerts** stay (read-only); code scanning stops.

---

## What stays readable after the flip, by design

Whatever we ship can be read by whoever runs it:
- the robot server, code runner and trainer code inside the public images;
- the PyInstaller GUI;
- the minified web app.

Protection comes from the server code never being shipped, no secret in shipped code, and the EULA. That is the industry norm.
