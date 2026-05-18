# Windows build + test prompt (for Claude Code running on the test PC)

Send the prompt block below verbatim to Claude Code on the Windows machine.
It pulls the latest source, rebuilds `EduBotics_Setup.exe` with the new
install diagnostics + scan UX, and guides through testing on a clean install.

The change being tested: install_diagnostics.log + `_scan_arms` diagnostics —
when "Arme scannen" finds nothing, the GUI now tells the student which link
in the host → WSL → docker chain broke (cable, driver, policy, attach, etc.)
instead of a generic "Nicht gefunden". See the bottom commit on `main` for
the full list of files touched.

---

## Prompt to paste into Claude Code on Windows

> You are running on a Windows 11 build machine. Your job is to rebuild the
> EduBotics installer from the latest commit on `main` and produce a fresh
> `EduBotics_Setup.exe` so the human can test it on this PC.
>
> ### Step 0 — locate the repo
>
> The repo is `SvenDanilBorodun/Testre` (private). It's most likely cloned at
> `C:\Users\<user>\Documents\EduBotics\Testre` or somewhere similar. Use
> `Get-ChildItem` to find a directory that contains `CLAUDE.md` and
> `robotis_ai_setup/`. If you can't find it, ask the user where it is or
> clone it fresh with `gh repo clone SvenDanilBorodun/Testre`.
>
> ### Step 1 — pull latest
>
> ```powershell
> cd <repo-root>
> git fetch origin
> git status
> git pull --ff-only origin main
> git log -1 --oneline
> ```
>
> Confirm the latest commit subject mentions install diagnostics or scan UX.
> If `git pull` reports merge conflicts, stop and ask the user — don't
> resolve blindly.
>
> ### Step 2 — verify build prerequisites
>
> Confirm these are reachable from PowerShell (don't install anything
> silently — if any is missing, tell the user and wait):
>
> 1. **Python 3.11+** with `pyinstaller`:
>    ```powershell
>    python --version
>    python -m pip show pyinstaller
>    ```
>    If pyinstaller is missing: `python -m pip install pyinstaller`.
>    If python is missing entirely: tell the user to install Python 3.11+
>    from python.org and add it to PATH — don't try to auto-install.
>
> 2. **Inno Setup 6** — the compiler is `iscc.exe`. Try `Get-Command iscc`.
>    If missing, look in the default install dir:
>    `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`.
>    If absent entirely, tell the user to install Inno Setup 6 from
>    https://jrsoftware.org/isdl.php — don't try to auto-install.
>
> 3. **WSL rootfs tarball + sidecar** — these are gitignored, so a fresh
>    clone won't have them:
>    ```powershell
>    Test-Path robotis_ai_setup\installer\assets\edubotics-rootfs.tar.gz
>    Test-Path robotis_ai_setup\installer\assets\edubotics-rootfs.tar.gz.sha256
>    ```
>    If either is missing, see "Rootfs missing" at the bottom — STOP and ask.
>    Do NOT try to rebuild the rootfs from this Windows session.
>
> ### Step 3 — rebuild the PyInstaller bundle
>
> Picks up the device_manager.py + gui_app.py changes.
>
> ```powershell
> cd robotis_ai_setup\gui
> # Clean stale dist so we never accidentally ship old bundled python.
> if (Test-Path dist) { Remove-Item dist -Recurse -Force }
> if (Test-Path build) { Remove-Item build -Recurse -Force }
> python -m PyInstaller build.spec
> ```
>
> After it finishes, `gui\dist\EduBotics\EduBotics.exe` should exist.
> Confirm with `Test-Path dist\EduBotics\EduBotics.exe`. If it doesn't,
> read the PyInstaller stderr and stop — don't try to "fix" by editing
> the spec without asking.
>
> ### Step 4 — compile the installer
>
> ```powershell
> cd ..\installer
> # If iscc is not on PATH, use the full path:
> #   & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" robotis_ai_setup.iss
> iscc robotis_ai_setup.iss
> ```
>
> The output is at `installer\output\EduBotics_Setup.exe`. Confirm
> with `Test-Path output\EduBotics_Setup.exe`. Report the file size
> (should be ~200 MB).
>
> ### Step 5 — test path (the human runs these manually)
>
> Tell the user, in order:
>
> 1. **Uninstall the existing EduBotics**: Start menu → "Apps & Features"
>    → find EduBotics → Uninstall. Reboot is not required.
> 2. **Optional but recommended**: delete the existing diagnostics log
>    so the new install run produces a clean one:
>    ```powershell
>    Remove-Item $env:LOCALAPPDATA\EduBotics\install_diagnostics.log -ErrorAction SilentlyContinue
>    ```
> 3. **Run the new installer**: double-click `installer\output\EduBotics_Setup.exe`.
>    Accept UAC. Watch the install progress dialog for any red error text.
> 4. **Read the install diagnostics**:
>    ```powershell
>    Get-Content $env:LOCALAPPDATA\EduBotics\install_diagnostics.log
>    ```
>    Expected sections in the log (timestamped, one per install step):
>    - `install_prerequisites::begin`
>    - `install_prerequisites::wsl_update`
>    - `install_prerequisites::path_refresh_after_usbipd` (after MSI install)
>    - `install_prerequisites::pnp_vid_2f5d` (whether Windows sees the arms)
>    - `install_prerequisites::usbipd_postinstall_probe`
>    - `configure_usbipd::version`
>    - `configure_usbipd::usbipd_list_at_install`
>    - `configure_usbipd::usbipd_policy_list_before` / `_after`
>    - `configure_usbipd::policy_add_2F5D:0103_*` (one entry per attempt)
>    - `configure_usbipd::attach_smoke` (only if arms plugged in at install)
>    - `verify_system::usbipd_version`
>    - `verify_system::usbipd_policy_list`
>    - `verify_system::pnp_at_verify`
>    - `verify_system::verify_summary`
>
>    If a section is missing, the install bailed early — surface the
>    last successful section to the user.
>
> 5. **Smoke-test the GUI**: launch EduBotics from the desktop shortcut.
>    With arms plugged in, click "Arme scannen". Both arms should be found
>    within ~10 s. With arms UNPLUGGED, click "Arme scannen" — the log
>    pane should now show a specific German diagnostic message naming
>    the broken link (e.g. "Windows erkennt kein ROBOTIS-Gerät (VID 2F5D)").
>
> 6. **Report back**: paste the contents of `install_diagnostics.log`,
>    the GUI log pane after both scan attempts, and a screenshot of the
>    arms scan result.
>
> ### Don't do
>
> - Don't push, commit, or modify source files. This Windows session is
>   build + test only.
> - Don't bump VERSION or create a GitHub release — that's a separate
>   step the human will do once they're happy with what they see.
> - Don't install Docker Desktop, Python, or Inno Setup automatically
>   without asking.
> - Don't `wsl --unregister EduBotics` to "clean state" — that wipes the
>   student's named volumes (datasets, calibration, HF cache). The
>   uninstaller in step 5.1 already calls this; don't double-tap.
>
> ### Rootfs missing
>
> If `edubotics-rootfs.tar.gz` is missing in `installer/assets/`, the .iss
> build will fail because the file is gitignored. Two options:
>
> - **Reuse the existing one**: if EduBotics 2.3.0 was previously installed
>   on this machine, the tarball was used at install time but not retained.
>   Check if the user has a copy from a prior build or a teammate's box.
> - **Rebuild it**: requires WSL + Docker on this Windows machine. From the
>   repo root, in WSL Ubuntu: `cd robotis_ai_setup/wsl_rootfs && bash build_rootfs.sh`.
>   This is a 10-15 min build the first time. The output drops into
>   `installer/assets/edubotics-rootfs.tar.gz` + `.sha256`.
>
> Either way, stop and tell the user before proceeding — don't silently
> wait or guess.

---

## Maintainer notes (for the macOS dev box)

This file lives in the repo because it's the canonical handoff doc when
deploying installer changes to a Windows test environment. Update it
whenever the build prereqs change (e.g. new pip dep, new Inno Setup
version, new asset).

**What's in this build that wasn't in 2.3.0:**
- `%LOCALAPPDATA%\EduBotics\install_diagnostics.log` — timestamped log
  appended by `install_prerequisites.ps1`, `configure_usbipd.ps1`,
  `verify_system.ps1`, AND `device_manager.py` at runtime.
- `install_prerequisites.ps1` runs `wsl --update`, refreshes PATH after
  the usbipd MSI install, and probes `Get-PnpDevice` for VID_2F5D.
- `configure_usbipd.ps1` runs a real `usbipd attach` smoke test when an
  arm is plugged in at install time, and cascades 3 policy-add syntaxes
  for usbipd 5.x compatibility.
- `verify_system.ps1` checks `usbipd policy list` for VID 2F5D and
  snapshots Windows PnP enumeration to the diag log.
- GUI `_scan_arms` calls `diagnose_usb_environment()` on failure and
  prints a specific German error in the log pane naming which link in
  the chain is broken.

**No Docker image, Modal app, Railway code, Supabase migration, or React
build was changed.** Cloud-side deploys are not needed for this test.
