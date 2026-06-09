# Offline-bundle launcher (`EduBotics_Setup_Full`)

These files build the **offline installer for NEW students**: a tiny Windows
launcher `EduBotics_Setup_Full.exe` plus its companion data file
`EduBotics_Setup_Full.dat`. The student downloads **both into one folder** and
double-clicks the `.exe`; it extracts the bundle and runs the real Inno
installer fully offline (all 3 amd64 Docker images `docker load`ed at install,
zero Docker Hub download on first run). Existing machines auto-update over the
network via the small `EduBotics_Setup.exe` and never touch this bundle.

## Why two files, not one `.exe`

**Windows refuses to *launch* any single `.exe` whose on-disk size exceeds
~4 GiB.** The PE image-size field is a 32-bit `ULONG` — a design limit since
1996, confirmed by Microsoft, Flexera, and 7-Zip's author Igor Pavlov
(*"Windows doesn't support EXE files larger than 4 GB"*). It is **architecture-
independent**: a 64-bit SFX stub does **not** lift it. The previous one-file
design (`cat 7zSD.sfx config.txt archive.7z > …exe`) produced an ~19 GB `.exe`
that the Windows loader rejected before any code ran — the *"this type can't be
opened here"* failure. Even the CUDA-slimmed ~5–6 GB bundle stays above 4 GiB,
so a single runnable `.exe` is **physically impossible** at our payload sizes.

The only loader-legal shape is therefore a **small launcher `.exe`** (a few
hundred KB — always loadable) that reads the multi-GB payload from a
**separate** file with 64-bit I/O. That payload file is the `.dat`.

## Files here

| File | Role |
|---|---|
| `main.c` | The Windows launcher (`wWinMain`). Finds its sibling `.dat`, free-space-prechecks, extracts to `%LOCALAPPDATA%\EduBotics\stage-<pid>` with a German progress window + Cancel, then runs the extracted `EduBotics_Setup.exe` from there (Inno `{src}` ⇒ the extract dir) and exits. Single-instance mutex; conservative stale-dir cleanup. |
| `edb_payload.c` / `edb_payload.h` | Portable C99 reader of the `EDBP1` container (sequential, 64-bit sizes, path-traversal-safe, UTF-8↔UTF-16 on Windows). Shared by the launcher AND the CI test. |
| `pack_payload.py` | The **writer** (and a reference `unpack`/`list`). CI runs `pack sfx_root EduBotics_Setup_Full.dat`. |
| `edb_extract_cli.c` | Portable CLI around `edb_extract`, built **natively** by the CI roundtrip test to exercise the >4 GiB path on Linux. Not shipped. |

## Container format (`EDBP1`)

All integers big-endian, no compression (members are `.tar.gz` already):

```
magic  : 5 bytes "EDBP1"
total  : uint64   sum of member sizes (progress + truncation check)
count  : uint32
then count members: [uint16 name_len][name UTF-8 '/'-sep][uint64 size][size bytes]
```

Extraction is purely **sequential** (no seeking), so the only 64-bit quantity
is per-member size — there is no >4 GiB *file-offset* hazard anywhere.

## Build (Linux runner, cross-compile)

```bash
x86_64-w64-mingw32-gcc -O2 -municode -mwindows -std=c11 \
  -finput-charset=UTF-8 -fwide-exec-charset=UTF-16LE \
  -o EduBotics_Setup_Full.exe main.c edb_payload.c \
  -lcomctl32 -lshell32 -luser32 -lkernel32
python3 pack_payload.py pack sfx_root EduBotics_Setup_Full.dat
```

Both run on `ubuntu-latest` in the `bundle` job of `.github/workflows/release.yml`
(and `bundle-test.yml`). The `bundle-stub-roundtrip` job in `ci.yml` packs a
synthetic **>4 GiB** payload and extracts it with the natively-built core every
PR — the only way to prove the 64-bit arithmetic without a Windows rig.

## Validation (P4 — only provable on a real Windows rig)

The temp-extract, the `{src}` = extract-dir resolution, the true ~5–6 GB
sibling read, the German progress UI, and Defender/SmartScreen/Smart-App-Control
behavior on the unsigned launcher can only be confirmed by **double-clicking the
built `EduBotics_Setup_Full.exe` (with its `.dat` beside it) on a clean Windows 11
PC** and confirming it installs **fully offline**. Keep that in the P4 gate; if
the classroom image has **Smart App Control** on, an unsigned launcher is
hard-blocked and code-signing graduates from deferred to required.

**Integrity:** the bundle is corruption-protected, **not tamper-resistant** — each
`<repo>.tar.gz.sha256` (verified by `load_images.ps1` before `docker load`) ships
*inside the same `.dat`* as the tarball it checks, so integrity rests on the HTTPS
download (an attacker who can replace the `.dat` can recompute the co-located hash).
Treat OV/EV code-signing of the launcher as the classroom-rollout lead-time item that
also clears Smart App Control.
