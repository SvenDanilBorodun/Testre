# 2026-07-11 — Installer split: per-machine IT prerequisites + per-user student app (Path A)

Status: SPEC, build-ready. Layers touched: 1 (installer/rootfs), 2 (GUI), 7 (cloud /version), CI.
NOTE: committed to the feature branch ONLY as cross-session handoff (remote sessions are
ephemeral). Per repo convention this page must NOT merge to main — delete it before merge;
durable invariants graduate to CLAUDE.md in the landing PR.

## 0. Verified ground truth (do not re-research)

From code (full read of installer/, gui/, wsl_rootfs/, workflows, 2026-07-11):
- Installer is admin-everything: `PrivilegesRequired=admin` (robotis_ai_setup.iss:45), dism
  features (install_prerequisites.ps1:97-114), usbipd MSI (:220), `wsl --import` to
  %ProgramData% (import_edubotics_wsl.ps1:19,204), .exe UNSIGNED (no signtool anywhere in
  release-installer.yml — verified).
- Runtime is already ~non-admin: all state in %LOCALAPPDATA% (constants.py), `usbipd attach`
  non-admin (device_manager.py:230-232 docstring), WSL boot/keepalive/compose non-admin.
  Runtime elevation only for: finalize (distro import), usbipd repair, device bind
  (gui_app.py `_elevate_and_wait` :21-89, callers :946-1012, :1065-1128, :1573-1645), and
  implicitly every GUI update.
- UPDATE LOCKOUT BUG (P0): skip button unlocks only after a FAILED download
  (gui_app.py:1224); `_launch_installer_and_exit` (gui_app.py:1242-1251) does
  `os.startfile()` + `sys.exit(0)` BEFORE the UAC outcome is known. Non-admin student +
  healthy network = bricked on every release (relaunch → same non-closable modal).
- `_elevate_and_wait` detects only ERROR_CANCELLED 1223; there is NO IsUserAnAdmin/token
  pre-flight anywhere.
- `constants._resolve_install_dir()` walks UP from the exe dir looking for
  docker/docker-compose.yml → a per-user install location already works; the
  `C:\Program Files\EduBotics` literal is only the last-resort fallback.
- `import_edubotics_wsl.ps1` already parameterizes -InstallRoot/-RootfsPath; its
  ROOTFS_VERSION gate (:73-134) + SHA sidecar check (:161-202) + 20 GB preflight (:144-159)
  are the semantics to port into the GUI wizard.
- Rootfs tarball (~192 MB) is BUNDLED in the .exe by CI (release-installer.yml:102-115,
  iss [Files] :99-105), never downloaded at install time.
- Phone camera server binds 0.0.0.0:8444 (phone_camera.py:319-340) → Defender prompt a
  standard user cannot approve; the netsh command is only LOGGED (gui_app.py:2332-2341).

From web (each verified with sources 2026-07-11):
- WSL distro registration is PER-USER: HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss;
  `wsl --import` needs NO admin but a loaded user profile; pre-seeding other accounts is
  impossible. (microsoft/WSL discussions #6038, issues #3817, #4591)
- WSL ships as a machine-wide MSI on github.com/microsoft/WSL/releases; on Win11 22H2+ it
  runs entirely from the MSI (no Microsoft Store needed). VirtualMachinePlatform via dism
  still required (admin, 3010). Intune has a WSL settings catalog / ADMX (AllowWSL,
  AllowWSL1, *UserSettingConfigurable) that schools may use to block WSL.
  (learn.microsoft.com/windows/wsl/enterprise, /intune)
- usbipd-win: `bind` + `policy add` = admin; `attach` = non-admin once shared.
  `usbipd policy add --effect Allow --operation AutoBind --hardware-id VID:PID` covers
  devices NOT yet plugged in, persists across reboots/replug, auto-binds on first attach by
  a non-privileged user. (dorssel/usbipd-win wiki "New design: policies")
- Azure Trusted Signing (renamed "Artifact Signing"): open to individuals + orgs (3-year
  history requirement dropped), Basic $9.99/mo / 5k signatures, base SmartScreen
  reputation, GH Actions integration `azure/trusted-signing-action`. EXTERNAL DEPENDENCY:
  the OWNER must create the Azure account + pass identity validation before P1 CI work can
  land — longest lead-time item, start immediately.
- Inno `PrivilegesRequired=lowest`: no UAC, {localappdata} install, HKCU uninstall key.

## 1. Final minimal admin set → Package M ("EduBotics Klassenraum-Setup", IT-only)

M1. dism enable VirtualMachinePlatform (+ Microsoft-Windows-Subsystem-Linux) — 3010 reboot.
M2. Install WSL platform via PINNED wsl.X.Y.Z.x64.msi from github.com/microsoft/WSL
    releases (replaces Store-dependent `wsl --install` + `wsl --update`). Pin via .iss
    constants `WslMsiUrl`/`WslMsiSha256` + reuse the RELEASE_PIN_NEEDED sentinel pattern
    (iss:26-32) and the SHA256-verify-before-msiexec pattern (install_prerequisites.ps1:
    189-221). install_prerequisites.ps1 gains -WslMsiUrl/-WslMsiSha256 params.
M3. usbipd-win 5.3.0 MSI — unchanged mechanics.
M4. usbipd AutoBind policies:
      usbipd policy add --effect Allow --operation AutoBind --hardware-id 2f5d:0103
      usbipd policy add --effect Allow --operation AutoBind --hardware-id 2f5d:2202
    + per-hardware-id policies for every UVC camera present at provisioning time (reuse the
    enumeration in bind_devices.ps1). Keeps the elevated bind repair in the GUI as fallback
    for swapped hardware.
M5. Firewall rule (idempotent delete-then-add):
      netsh advfirewall firewall add rule name="EduBotics Handy-Kamera" dir=in
      action=allow protocol=TCP localport=8444
M6. Docker Desktop migration (migrate_from_docker_desktop.ps1) — unchanged, stays in M.
M7. Stamp: HKLM\SOFTWARE\EduBotics, value PrereqVersion (REG_SZ) = contents of new source
    file robotis_ai_setup/installer/PREREQ_VERSION (starts at "1"). Intune detection rule
    = this value. Add CI guard job prereq-version-guard mirroring rootfs-version-guard:
    any change under the M-package inputs must bump PREREQ_VERSION.

Everything else is per-user/no-admin: GUI install, distro import, image pulls, attach,
.env/state, .wslconfig, GUI self-update.

## 2. Deliverables and file-level work items

### P0 — update-lockout hotfix (ship independently, next patch)
- gui/app/gui_app.py:
  - New `is_user_admin()` helper (ctypes shell32.IsUserAnAdmin or token check) used by all
    elevation callers + the update modal.
  - `_show_update_dialog`: if not admin-capable → „Ohne Update fortfahren" enabled
    IMMEDIATELY + German notice „Update erfordert Administratorrechte — bitte an
    Lehrkraft/IT wenden." Keep failed-download unlock too.
  - `_launch_installer_and_exit`: launch via `_elevate_and_wait`-style ShellExecuteExW
    ("open" verb is fine — the installer manifest elevates); only `sys.exit(0)` after the
    process handle exists; on ERROR_CANCELLED return to the modal (skip now enabled).
  - All four repair flows: differentiated German message for "keine Administratorrechte
    verfügbar" vs "abgebrochen".
- tests: robotis_ai_setup/tests/test_update_checker.py additions + new
  test_gui_admin_preflight.py (deps-free, mock ctypes).

### P1 — signing (CI only; blocked on owner's Azure identity validation)
- release-installer.yml: after PyInstaller, sign dist/EduBotics/EduBotics.exe with
  azure/trusted-signing-action; pass `SignTool` to ISCC (`iscc /S...` or [Setup] SignTool
  directive) so EduBotics_Setup.exe (+ later Klassenraum exe) is signed.
- New CI secrets: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET (or OIDC
  federated), TS endpoint + account + cert-profile names. Document in .github/zizmor.yml
  secrets count.
- Publish the certificate Subject CN in the IT-Handbuch for AppLocker/WDAC publisher rules.

### P2 — the split
Installer:
- NEW robotis_ai_setup/installer/klassenraum_setup.iss (Package M): PrivilegesRequired=
  admin, runs M1-M7 via the existing scripts (install_prerequisites.ps1 modified for M2;
  configure_usbipd.ps1 extended for M4 hardware-id policies; new firewall + stamp steps —
  either a small new configure_firewall.ps1 or fold into configure_usbipd.ps1). Silent-
  capable (/VERYSILENT /NORESTART), idempotent, exit 0/3010. No rootfs, no GUI — small exe.
- REWORK robotis_ai_setup.iss (Package U): NEW AppId (per-user product is a distinct
  install identity; old machine-wide v2.12.x remains independently uninstallable),
  PrivilegesRequired=lowest, DefaultDirName={localappdata}\Programs\EduBotics, DROP [Run]
  steps 0-6 (migrate/prereq/wsl/usbipd/import/pull/verify) and the explorer.exe launch
  trick (no elevation → no PATH race; plain [Run] launch), KEEP [Files] incl. rootfs tar +
  sidecar + ROOTFS_VERSION, KEEP [InstallDelete] analog for the new location, DROP
  ChangesEnvironment. Uninstall: keep ConfirmDistroWipe (unregister is per-user → works).
  On detecting the OLD machine install (Program Files present) show a German notice to have
  IT uninstall it (not blocking).
GUI:
- NEW gui/app/first_run.py — „Einrichtung" wizard, replaces installer-side import + pull:
  probes (in order): wsl --status OK; find_usbipd() OK; `usbipd policy list` contains
  2f5d rules; HKLM PrereqVersion >= shipped PREREQ_VERSION. Missing → German IT screen with
  copyable diagnostics; if is_user_admin() → offer inline elevated Klassenraum-Setup run
  (bundle the M exe? NO — download link + local path probe; keep U small. Home/BYOD users
  download M once).
  Then per-user import: port import_edubotics_wsl.ps1 semantics to Python on wsl_bridge:
  SHA sidecar verify (hard-fail), 20 GB preflight, InstallRoot %LOCALAPPDATA%\EduBotics\wsl,
  `wsl --import EduBotics <root> <tar> --version 2`, dockerd readiness poll (reuse
  docker_manager.wait_for_docker), then pull_images. German progress + retry.
- ADOPT logic (fleet migration): if is_distro_registered() for THIS user → read
  /etc/edubotics-rootfs-version; match → adopt as-is (works regardless of old VHDX
  location); mismatch → existing consent dialog → unregister + per-user re-import.
  ROOTFS_VERSION gate semantics byte-identical to today (volume preservation).
- gui_app.py `_run_prerequisite_checks`: replace `_prompt_finalize_install` path with the
  wizard; keep elevated repairs as fallback; keep `.wslconfig` handling in-GUI (write
  %USERPROFILE%\.wslconfig directly — delete configure_wsl.ps1's profile-resolution hack
  from the U flow; configure_wsl.ps1 stays only if M needs it, likely DELETE from M too).
- constants.py: PREREQ_VERSION shipped file path + HKLM stamp reader; per-user InstallRoot
  const.
Update flow:
- update_checker.py: /version contract gains prereq_version, prereq_download_url,
  prereq_sha256 (all optional, backward-compatible). U self-update: download → verify
  Content-Length + SHA → run `EduBotics_Setup.exe /VERYSILENT /NORESTART` (NO UAC) → exit;
  installer relaunches GUI via [Run]. M update available → non-blocking teacher notice
  („IT informieren"), never gates startup.
- cloud_training_api/app/routes/version.py: add the three fields (env: PREREQ_VERSION,
  PREREQ_DOWNLOAD_URL, PREREQ_INSTALLER_SHA256); W6 (release.yml publish-gui-version)
  publishes them alongside the existing four vars, same atomic-redeploy pattern.
CI:
- release-installer.yml: build + sign BOTH artifacts; attach both to the release;
  floor-check M exe too. release.yml W6 extended as above. NEW prereq-version-guard job.
  Existing guards that must stay green: powershell-encoding + powershell-native-stderr
  (new/modified .ps1), german-strings-lint (new [FEHLER]/[WARNUNG] lines), shell-lint
  unchanged.
- Version bump: the 3 existing sites (VERSION, iss AppVersion, constants fallback) apply to
  U; M carries PREREQ_VERSION independently.

### P3 — IT deployment kit (docs + packaging, no product code)
- .intunewin recipe + detection rule (HKLM PrereqVersion), silent command lines, return
  codes (0/3010), SCCM/PDQ/manual variants.
- German IT-Handbuch: required-allow GPO list (AllowWSL etc.), signing cert CN for
  AppLocker/WDAC, shared-PC guidance (per-user VHDX disk math; recommend one student
  account per robot PC — pre-seeding impossible), old-package uninstall + optional
  ProgramData VHDX cleanup.

### P4 — optional hardening (separate spec when picked up)
- School proxy/TLS-interception detection + CA injection into distro dockerd
  (/usr/local/share/ca-certificates + update-ca-certificates + dockerd restart).
- Repair button for corrupt-but-registered distro (today: silent gap at gui_app.py:769-778).

## 3. Decided (do NOT re-litigate next session)
- Import moves from installer PowerShell into the GUI (D2). Reasons: German progress UI +
  retry, kills wrong-user registration (over-the-shoulder UAC = admin-token import),
  kills the reboot dead-end + PS5.1 EAP trap class.
- U gets a NEW AppId; coexistence with old machine install is allowed, warned, documented.
- Home/BYOD stays possible: U detects missing prereqs and offers elevated M run for
  admin-capable users.
- Shared PCs: per-user VHDX cost accepted + documented (no shared-distro hack exists).
- M6 Docker Desktop migration stays in M.

## 4. Open items for the OWNER (not buildable by a session)
- Start Azure Trusted Signing identity validation (blocks P1 landing, nothing else).
- Confirm publisher legal name for the cert (shows in UAC/SmartScreen).
- Pick the WSL MSI version to pin (latest stable at P2 time) — then fill WslMsiSha256.

## 5. Test matrix (standard-user VM, no admin creds, per CLAUDE.md double-install rule)
U-before-M → IT screen, no UAC loop; U update as standard user (silent, no prompt);
second account on shared PC (own import, disk preflight); upgrade from 2.12.x same-user
(adopt) and different-user (fresh import); ROOTFS_VERSION match → volumes survive /
mismatch → consent; Intune SYSTEM deploy of M incl. 3010; AllowWSL-blocked GPO → error
names the policy; M idempotent re-run; signed exes pass SmartScreen without warning.
