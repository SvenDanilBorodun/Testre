# Offline-bundle 7-Zip SFX assets

These files build `EduBotics_Setup_Full.exe` — the single, self-contained
**offline** installer for NEW students (all 3 amd64 Docker images bundled,
`docker load`ed at install, zero Docker Hub download on first run). It is built
on a Linux runner by the `bundle` job in `.github/workflows/release.yml` and
uploaded to Cloudflare R2. (Existing machines auto-update over the network via
the small `EduBotics_Setup.exe`; they never re-download this file.)

The big installer is a **7-Zip self-extracting archive**: at run time it
extracts the inner `EduBotics_Setup.exe` + `images/<repo>.tar.gz` +
`bundled_digests.json` to a temp dir and auto-runs the inner Inno installer
(`config.txt` `RunProgram`). The inner installer's `stage_bundle.ps1` then
moves the bundle to `%ProgramData%\EduBotics\bundle` and `load_images.ps1`
loads it — see `installer/scripts/`.

## Files here

| File | Role |
|---|---|
| `7zSD.sfx` | The 7-Zip installer SFX stub (Windows PE). Extracts to a temp dir, honours `config.txt`'s `RunProgram`, and **waits synchronously** for it. |
| `7zSD.sfx.sha256` | sha256 of the stub; the CI `bundle` job's preflight runs `sha256sum -c` on it before use. |
| `config.txt` | SFX config: `RunProgram="EduBotics_Setup.exe"`. **UTF-8, CRLF, no BOM** (the `;!@Install@!UTF-8!` marker must be the very first bytes — a BOM would break recognition). |
| `LICENSE` | LZMA SDK declaration — `7zSD.sfx` is **public domain** ("LZMA SDK is written and placed in the public domain by Igor Pavlov"), so there is no redistribution obligation; kept for attribution. |

## Provenance of `7zSD.sfx` (how it was vendored)

- **Source:** `https://www.7-zip.org/a/lzma2501.7z` → `bin/7zSD.sfx` (LZMA SDK **25.01**, public domain).
- The classic full-featured GUI installer module — NOT `7za.exe`/`7zCon.sfx` (console/archive-only) and NOT the third-party `7zsfx.info` fork.
- `config.txt`'s format matches the SDK's reference `bin/installer/config.txt`.

**To bump the 7-Zip version:** download a newer `lzma<ver>.7z`, extract `bin/7zSD.sfx`, replace it here, and regenerate the hash:
`sha256sum 7zSD.sfx > 7zSD.sfx.sha256`. Then re-run the P4 pilot.

## Validation (P4 — only provable on a real Windows rig)

The `{src}` → SFX-extract-dir assumption and the extract → UAC-elevation →
`stage_bundle` move chain can only be proven by **double-clicking the built
`EduBotics_Setup_Full.exe` on a clean Windows PC** and confirming it
auto-runs the inner installer (not just extracts) and installs **fully
offline**. Keep that in the P4 acceptance gate.
