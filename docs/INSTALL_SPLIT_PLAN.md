# 2026-07-11 — Installer split: per-machine IT prerequisites + per-user student app (Path A)

Status: SPEC v2, build-ready. Layers touched: 1 (installer/rootfs), 2 (GUI), 7 (cloud /version), CI.
v2 (2026-07-11, second-session deep review): every §0 code claim re-verified line-by-line against
HEAD; fixed 4 defects that would have broken P0/P2 as written (§0b), and hardened the work items.
NOTE: APPROVED plan, published to main by owner decision (2026-07-11) as docs/
INSTALL_SPLIT_PLAN.md — same pattern as docs/ORANGE_PI_DEPLOY_PLAN.md. This deliberately
overrides the default docs/plans/-is-throwaway convention because remote sessions are
ephemeral and this spec is the cross-session source of truth for the P0-P4 work. When the
feature lands: durable invariants graduate to CLAUDE.md, the dated narrative goes to
docs/CLAUDE-CHANGELOG.md, and this file is deleted in that PR.

## 0. Verified ground truth (re-verified 2026-07-11 v2 — do not re-research)

From code (full read of installer/, gui/, wsl_rootfs/, workflows; all line refs re-checked):
- Installer is admin-everything: `PrivilegesRequired=admin` (robotis_ai_setup.iss:45), dism
  features (install_prerequisites.ps1:96-114), usbipd MSI (:220), `wsl --import` to
  %ProgramData% (import_edubotics_wsl.ps1:19,204), .exe UNSIGNED (no signtool anywhere in
  release-installer.yml — verified; PyInstaller exe manifest is asInvoker — build.spec has
  no uac_admin, so the GUI itself already runs per-user cleanly).
- Runtime is already ~non-admin: all state in %LOCALAPPDATA% (constants.py), `usbipd attach`
  non-admin (device_manager.py:226-233), WSL boot/keepalive/compose non-admin. Runtime
  elevation happens at exactly THREE `_elevate_and_wait` call sites (gui_app.py:970 usbipd
  repair, :1082 finalize, :1615 bind devices — the last shared by the camera + arm repair
  UIs), plus implicitly every GUI update (installer manifest).
- UPDATE LOCKOUT BUG (P0): skip button unlocks only after a FAILED download
  (gui_app.py:1224); `_launch_installer_and_exit` (gui_app.py:1242-1251) does
  `os.startfile()` + `sys.exit(0)` BEFORE the UAC outcome is known. Non-admin student +
  healthy network = bricked on every release (relaunch → same non-closable modal).
- **LATENT BUG the original spec missed — UAC-cancel detection is ALREADY broken**:
  `_elevate_and_wait` (gui_app.py:21-89) calls `ctypes.get_last_error()` after
  `ctypes.windll.shell32.ShellExecuteExW(...)`. `ctypes.windll` loads WITHOUT
  `use_last_error=True`, so `get_last_error()` returns the never-updated thread-local copy
  (0/stale) — ERROR_CANCELLED (1223) is never seen. `cancelled` is never True; every
  „abgebrochen (UAC-Zustimmung verweigert)" branch in the three callers is dead code today
  (a cancel surfaces as „ShellExecuteEx Fehler 0" + the generic failure path). P0's
  return-to-modal-on-cancel DEPENDS on this working — fixing it is part of P0, not optional.
- There is NO IsUserAnAdmin/token pre-flight anywhere (grep-verified: zero hits for
  IsUserAnAdmin/TokenElevation under gui/).
- `constants._resolve_install_dir()` walks UP from the exe dir looking for
  docker/docker-compose.yml → a per-user install location already works; the
  `C:\Program Files\EduBotics` literal is only the last-resort fallback.
- `import_edubotics_wsl.ps1` already parameterizes -InstallRoot/-RootfsPath; its
  ROOTFS_VERSION gate (:73-134) + SHA sidecar hard-fail (:170-202) + 20 GB preflight
  (:147-159) are the semantics to port into the GUI wizard. Its dockerd-readiness poll
  (:240-284, 180 s + manual start-dockerd.sh fallback) is already duplicated in Python by
  `docker_manager.wait_for_docker` — the wizard reuses the Python one.
- Rootfs tarball (~192 MB) is BUNDLED in the .exe by CI (release-installer.yml:111-115
  artifact download, iss [Files] :101-105), never downloaded at install time.
- Phone camera server binds 0.0.0.0:8444 (phone_camera.py `host="0.0.0.0"`, start() binds)
  → Defender prompt a standard user cannot approve; the netsh command is only LOGGED
  (gui_app.py:2332-2341).
- GUI-invoked repair runs install_prerequisites.ps1 with NO -UsbipdMsiSha256 argument
  (gui_app.py:958-968) → the script's env-var fallback is empty → „SHA256 pin not set —
  skipping integrity check" (install_prerequisites.ps1:216). The repair path installs
  usbipd UNVERIFIED today — the split must close this hole (§P2 pins file), not copy it.
- Camera enumeration for policies lives in configure_usbipd.ps1:235-304 (Get-PnpDevice
  -Class Camera,Image), NOT in bind_devices.ps1 (which takes explicit -HardwareIds). M4
  reuses the configure_usbipd.ps1 block.
- `.reboot_required` + `.migrated` one-shot flags live in $PSScriptRoot (the installing
  package's scripts dir) — after the split they are M-internal; U/GUI must not path-couple
  to them (the wizard discriminates via `wsl --status` + the HKLM stamp instead).
- W6 (release.yml:169-253) sets GUI_RELEASE_REPO/GUI_DOWNLOAD_URL/GUI_INSTALLER_SHA256 with
  `--skip-deploys`, then GUI_VERSION last WITHOUT it (one atomic redeploy), then poll-gates
  /version — the exact pattern the three new PREREQ_* vars join.
- update_checker.cleanup_stale_installers() sweeps only `EduBotics_Setup*.exe` in %TEMP% —
  a downloaded Klassenraum exe needs its own sweep pattern or a matching name prefix.
- W5 floor-checks the built .exe at >100 MB (release-installer.yml:217-219) — a size gate
  the small M exe must NOT inherit verbatim.

From web (each verified with sources 2026-07-11):
- WSL distro registration is PER-USER: HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss
  (per-distro subkey carries `BasePath` — the VHDX location; readable non-admin);
  `wsl --import` needs NO admin but a loaded user profile; pre-seeding other accounts is
  impossible. (microsoft/WSL discussions #6038, issues #3817, #4591)
- WSL ships as a machine-wide MSI on github.com/microsoft/WSL/releases; on Win11 22H2+ it
  runs entirely from the MSI (no Microsoft Store needed). VirtualMachinePlatform via dism
  still required (admin, 3010). Intune has a WSL settings catalog / ADMX (AllowWSL,
  AllowWSL1, *UserSettingConfigurable) that schools may use to block WSL.
  (learn.microsoft.com/windows/wsl/enterprise, /intune)
- usbipd-win: `bind` + `policy add` = admin; `attach` = non-admin once shared; `policy
  list` = non-admin (read-only). An `AutoBind` Allow policy on a VID:PID matches ANY device
  with that hardware id (arms can move between PCs), covers devices not yet plugged in,
  persists across reboots/replug, auto-binds on first attach by a non-privileged user.
  (dorssel/usbipd-win wiki "New design: policies")
- Azure Trusted Signing (renamed "Artifact Signing"): open to individuals + orgs (3-year
  history requirement dropped), Basic $9.99/mo / 5k signatures, base SmartScreen
  reputation, GH Actions integration `azure/trusted-signing-action`. EXTERNAL DEPENDENCY:
  the OWNER must create the Azure account + pass identity validation before P1 CI work can
  land — longest lead-time item, start immediately.
- Inno `PrivilegesRequired=lowest`: no UAC, `{autopf}` auto-maps to `{userpf}` =
  {localappdata}\Programs, HKCU uninstall key; [Run] entries flagged `postinstall` are
  ALSO implicitly `skipifsilent` → they never fire under /VERYSILENT (this kills the naive
  "installer relaunches GUI" idea — see §0b-3).
- `IsUserAnAdmin()` returns False for an admin user running non-elevated under UAC (split
  token). Distinguishing "can self-elevate via consent" from "needs teacher credentials"
  requires GetTokenInformation(TokenElevationType): 2=Full (already elevated), 3=Limited
  (admin, can elevate), 1=Default (→ IsUserAnAdmin(): True = admin with UAC off, False =
  standard user).
- ShellExecuteExW with lpVerb="open" on an exe whose manifest requires elevation blocks
  through the UAC dialog: consent → success + process handle; decline → FALSE with
  GetLastError()=ERROR_CANCELLED. Same call on an asInvoker exe (the future U setup) just
  launches — one code path serves both eras.

## 0b. Defects in spec v1, fixed in this revision (do not regress)

1. **P0 was built on the broken cancel detection** (see §0). Fix `_elevate_and_wait` FIRST:
   module-level `_shell32 = ctypes.WinDLL("shell32", use_last_error=True)` (+ explicit
   argtypes/restype as today), read `ctypes.get_last_error()` only after a call through
   THAT handle. Unit-testable: mock the FFI, assert 1223 → (None, True, …).
2. **`is_user_admin()` was the wrong primitive.** Bare IsUserAnAdmin() reports False for a
   split-token admin — the modal would tell an admin-capable home user „an IT wenden".
   Replace with `elevation_capability() -> 'elevated' | 'can_elevate' | 'standard'`
   (token-elevation-type ladder above; non-win32 → 'standard'; any ctypes failure →
   'can_elevate' so we never suppress the Update button on a probe error). Messaging:
   'standard' → skip enabled immediately + „Update erfordert Administratorrechte — bitte an
   Lehrkraft/IT wenden. (Eine Lehrkraft kann sich am UAC-Dialog anmelden.)"; the Update
   button STAYS enabled in every state (a teacher can type credentials over-the-shoulder).
3. **"U self-update: installer relaunches GUI via [Run]" was dead on arrival** —
   postinstall⊃skipifsilent, so nothing relaunches under /VERYSILENT. Fix: U .iss gets a
   dedicated [Run] entry `Filename: {app}\gui\EduBotics.exe; Flags: nowait; Check:
   ShouldRelaunchGui` where ShouldRelaunchGui reads `{param:RELAUNCHGUI|0}`. The GUI's
   silent-update path launches `EduBotics_Setup.exe /VERYSILENT /NORESTART /RELAUNCHGUI=1
   /GUIPID=<os.getpid()>` via subprocess.Popen (CreateProcess — os.startfile cannot pass
   args; ShellExecute would also re-trigger SmartScreen/MOTW on the downloaded file).
4. **In-use-files race on silent update**: /VERYSILENT reaches [InstallDelete]-wipes-
   {app}\gui within ~1 s while EduBotics.exe may still be tearing down (tk, keepalive
   atexit) → locked files → broken update. Fix, both sides: (a) U .iss `AppMutex=
   EduBoticsGuiRunning` and the GUI creates that named mutex at startup (ctypes
   CreateMutexW, held for process lifetime); (b) U .iss `[Code] InitializeSetup` waits
   ≤15 s for `{param:GUIPID}` to exit before proceeding (bounded OpenProcess/Wait loop;
   absent param → no wait). Interactive installs get Inno's stock „close the app" prompt
   from AppMutex; silent installs get the PID wait.

## 1. Final minimal admin set → Package M ("EduBotics Klassenraum-Setup", IT-only)

M1. dism enable VirtualMachinePlatform (+ Microsoft-Windows-Subsystem-Linux) — 3010 reboot.
M2. Install WSL platform via PINNED wsl.X.Y.Z.x64.msi from github.com/microsoft/WSL
    releases (replaces Store-dependent `wsl --install` + `wsl --update`). Pin via .iss
    constants `WslMsiUrl`/`WslMsiSha256` + reuse the RELEASE_PIN_NEEDED sentinel pattern
    (iss:26-32) and the SHA256-verify-before-msiexec pattern (install_prerequisites.ps1:
    189-221). install_prerequisites.ps1 gains -WslMsiUrl/-WslMsiSha256 params. Install the
    MSI when WSL is absent OR older than the pin (msiexec upgrades in place) — deterministic
    fleet state, not "whatever Store had".
M3. usbipd-win 5.3.0 MSI — unchanged mechanics.
M4. usbipd AutoBind policies:
      usbipd policy add --effect Allow --operation AutoBind --hardware-id 2f5d:0103
      usbipd policy add --effect Allow --operation AutoBind --hardware-id 2f5d:2202
    + per-hardware-id policies for every UVC camera present at provisioning time (reuse the
    Get-PnpDevice enumeration block in configure_usbipd.ps1:235-304 — NOT bind_devices.ps1,
    which is the explicit-ids repair tool). Keep the existing bind-now-for-already-plugged
    devices step and the attach smoke test. The elevated bind repair in the GUI stays as
    fallback for swapped hardware.
M5. Firewall rule (idempotent delete-then-add, port-scoped — NOT program-scoped: the U exe
    path is per-user, one rule can't name every account's copy):
      netsh advfirewall firewall delete rule name="EduBotics Handy-Kamera" >nul 2>&1
      netsh advfirewall firewall add rule name="EduBotics Handy-Kamera" dir=in
      action=allow protocol=TCP localport=8444
    (8444 = PORT_PHONE_HTTPS default; a rig overriding EDUBOTICS_PHONE_HTTPS_PORT needs a
    matching manual rule — IT-Handbuch note.)
M6. Docker Desktop migration (migrate_from_docker_desktop.ps1) — unchanged, stays in M.
    (Known pre-existing limit, keep documented: its `wsl --unregister docker-desktop*` only
    acts for the account M runs as; other users' DD distros survive. Harmless — DD itself
    is removed machine-wide.)
M7. Stamp via a NEW SHARED script installer/scripts/write_prereq_stamp.ps1 (also called by
    the GUI's inline elevated path, §P2): HKLM\SOFTWARE\EduBotics, value PrereqVersion
    (REG_SZ) = contents of new source file robotis_ai_setup/installer/PREREQ_VERSION
    (integer string, starts at "1"; compare numerically, never via _parse_version). Intune
    detection rule = this value. Add CI guard job prereq-version-guard mirroring
    rootfs-version-guard (ci.yml:123-158): any change under the declared M-input set —
    klassenraum_setup.iss, install_prerequisites.ps1, configure_usbipd.ps1,
    migrate_from_docker_desktop.ps1, write_prereq_stamp.ps1, the firewall step file, and
    the Wsl/Usbipd pin lines — must bump PREREQ_VERSION.

klassenraum_setup.iss keeps `ChangesEnvironment=yes` (usbipd MSI extends the system PATH —
this directive was never about U's own files). M's AppVersion = PREREQ_VERSION (its release
identity; the HKLM stamp stays the detection key). Everything else is per-user/no-admin:
GUI install, distro import, image pulls, attach, .env/state, .wslconfig, GUI self-update.

## 2. Deliverables and file-level work items

### P0 — update-lockout hotfix (ship independently as 2.12.3 — the LAST admin-gated update;
###      once P2's per-user U ships, 2.12.x GUIs auto-update into it WITHOUT admin, which is
###      the fleet-migration vector — see §P2 "migration invariant")
- gui/app/gui_app.py:
  - Fix `_elevate_and_wait` last-error handling (§0b-1). Keep the (exit_code, cancelled,
    error) contract; all three existing callers' cancel branches come alive unchanged.
  - New `elevation_capability()` helper (§0b-2) in gui_app.py (or a tiny new
    gui/app/win_privilege.py so it's import-mockable), used by the update modal + all
    elevation call sites for message differentiation only — never to suppress the attempt.
  - `_show_update_dialog`: capability == 'standard' → „Ohne Update fortfahren" enabled
    IMMEDIATELY + the teacher/IT notice; other states keep today's failed-attempt unlock.
    Keep the failed-download unlock in all states.
  - `_launch_installer_and_exit`: launch via ShellExecuteExW "open" verb (manifest decides
    elevation — works for the current admin installer AND the future asInvoker U setup),
    SEE_MASK_NOCLOSEPROCESS, do NOT wait on the handle — `sys.exit(0)` immediately once the
    handle exists (the GUI must release {app}\gui before the installer's wipe). On
    ERROR_CANCELLED / SmartScreen-blocked launch: return to the modal with skip enabled and
    a German line distinguishing „abgebrochen" from „keine Administratorrechte verfügbar".
  - The three elevated repair flows: differentiated German message for "keine
    Administratorrechte verfügbar" vs "abgebrochen" (both now actually reachable).
- tests: robotis_ai_setup/tests/test_update_checker.py additions + new
  test_gui_admin_preflight.py (deps-free, mock ctypes; must pass on Linux CI — all Win32
  helpers return the documented non-win32 defaults). Regression test: mocked
  ShellExecuteExW returning FALSE with last_error=1223 → cancelled=True.

### P1 — signing (CI only; blocked on owner's Azure identity validation)
- release-installer.yml: after PyInstaller, sign dist/EduBotics/EduBotics.exe with
  azure/trusted-signing-action; pass a SignTool definition to ISCC (`/S` or [Setup]
  SignTool directive, + SignedUninstaller) so EduBotics_Setup.exe AND its uninstaller
  (+ later the Klassenraum exe) are signed.
- New CI secrets: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET (or OIDC
  federated), TS endpoint + account + cert-profile names. Update the "12 documented
  secrets" count in .github/zizmor.yml.
- Publish the certificate Subject CN in the IT-Handbuch for AppLocker/WDAC publisher rules.

### P2 — the split
Installer:
- NEW robotis_ai_setup/installer/klassenraum_setup.iss (Package M): PrivilegesRequired=
  admin, ChangesEnvironment=yes, AppVersion=PREREQ_VERSION, runs M1-M7 via the existing
  scripts (install_prerequisites.ps1 modified for M2; configure_usbipd.ps1 unchanged
  mechanics; new configure_firewall.ps1 (M5) + write_prereq_stamp.ps1 (M7)).
  OutputBaseFilename=EduBotics_Klassenraum_Setup (distinct asset name). Silent-capable
  (/VERYSILENT /NORESTART), idempotent, exit 0/3010. No rootfs, no GUI — small exe.
- REWORK robotis_ai_setup.iss (Package U): NEW AppId (per-user product is a distinct
  install identity; old machine-wide v2.12.x remains independently uninstallable),
  PrivilegesRequired=lowest, DefaultDirName={autopf}\EduBotics (auto-maps to
  {localappdata}\Programs\EduBotics under lowest), KEEP OutputBaseFilename=EduBotics_Setup
  (migration invariant below), DROP [Run] steps 0-6 (migrate/prereq/wsl/usbipd/import/
  pull/verify) and the explorer.exe launch trick (no elevation → no PATH race; plain [Run]
  postinstall launch), ADD the /RELAUNCHGUI [Run] entry + AppMutex + GUIPID wait (§0b-3/4),
  KEEP [Files] incl. rootfs tar + sidecar + ROOTFS_VERSION + scripts\* (the M scripts ride
  along — they're KB-sized text and power the inline elevated path below) + NEW
  installer/PREREQ_VERSION → {app}\scripts\PREREQ_VERSION, KEEP [InstallDelete] analog for
  the new location, DROP ChangesEnvironment. Uninstall: keep ConfirmDistroWipe (unregister
  is per-user → works, non-elevated). On detecting the OLD machine install (Program Files
  present / old AppId uninstall key) show a German notice to have IT uninstall it (not
  blocking; mention the doubled desktop icon until then).
  **Migration invariant**: because U keeps the EduBotics_Setup.exe asset name and needs no
  elevation, every 2.12.x GUI's forced-update modal migrates a standard-user fleet to the
  per-user layout automatically (download → run → per-user install → relaunch). Do not
  rename the asset.
GUI:
- NEW gui/app/first_run.py — „Einrichtung" wizard, replaces installer-side import + pull:
  probes (in order): `wsl --status` OK (fail-message names „PC neu starten" as first
  remedy — the dism-3010 case); find_usbipd() OK; `usbipd policy list` (non-admin OK)
  contains 2f5d rules; HKLM PrereqVersion (numeric) >= shipped {app}\scripts\PREREQ_VERSION.
  Missing → German IT screen with copyable diagnostics + BOTH remedies:
    (a) capability != 'standard' → inline elevated run of the LOCAL M scripts (U ships
        them): one `_elevate_and_wait` PowerShell chain migrate → install_prerequisites
        → configure_usbipd → configure_firewall → write_prereq_stamp, transcript-tailed
        like _prompt_repair_usbipd. NO download needed for home/BYOD users.
    (b) M-exe download link (from /version prereq_download_url, fallback literal in
        constants.py) — the IT/Intune path.
  Pin-passing: a new shipped pins file (installer/scripts/prereq_pins.psd1, values injected
  by CI/iss defines) provides -UsbipdMsiUrl/-UsbipdMsiSha256/-WslMsiUrl/-WslMsiSha256 to
  BOTH the .iss [Run] lines and the GUI-invoked scripts — closing the existing unverified-
  usbipd-repair hole (§0) in the same change.
  Then per-user import: port import_edubotics_wsl.ps1 semantics to Python: SHA sidecar
  verify (hashlib, hard-fail), 20 GB preflight (shutil.disk_usage), InstallRoot
  %LOCALAPPDATA%\EduBotics\wsl, `wsl --import EduBotics <root> <tar> --version 2` as a
  DIRECT wsl.exe subprocess with CREATE_NO_WINDOW (NOT wsl_bridge.run(), which targets a
  running distro), partial-VHDX cleanup on failure, dockerd readiness via the existing
  docker_manager.wait_for_docker, then docker_manager.pull_images (existing progress
  callbacks). German progress + retry.
- ADOPT logic (fleet migration): if is_distro_registered() for THIS user → read
  /etc/edubotics-rootfs-version; match → adopt as-is; mismatch → existing consent dialog →
  unregister + per-user re-import. ROOTFS_VERSION gate semantics byte-identical to today
  (volume preservation). Adopted distros may keep their VHDX under %ProgramData%\EduBotics\
  wsl (registered-to-student installs from the admin era): read HKCU\...\Lxss\<guid>\
  BasePath, LOG it, and warn in the Protokoll when it lives under ProgramData („Ordner
  nicht löschen — IT-Hinweis"); the IT-Handbuch cleanup step must check for adopted distros
  before deleting ProgramData VHDXs.
- gui_app.py `_run_prerequisite_checks`: replace the `_prompt_finalize_install` path with
  the wizard; keep elevated repairs as fallback; move `.wslconfig` handling in-GUI (write
  %USERPROFILE%\.wslconfig directly with configure_wsl.ps1's merge-don't-overwrite
  semantics — only add missing memory/swap keys). DELETE configure_wsl.ps1 from both M and
  U flows (decisive — the elevated profile-resolution hack dies with per-user execution).
- constants.py: shipped-PREREQ_VERSION path + HKLM stamp reader; per-user InstallRoot
  const; PREREQ_DOWNLOAD_FALLBACK_URL literal.
Update flow:
- update_checker.py: /version contract gains prereq_version, prereq_download_url,
  prereq_sha256 (all optional via .get — backward-compatible with old deploys). U
  self-update: download → verify Content-Length + SHA → subprocess.Popen
  `EduBotics_Setup.exe /VERYSILENT /NORESTART /RELAUNCHGUI=1 /GUIPID=<pid>` (NO UAC) →
  exit (§0b-3/4). M update available (HKLM stamp < prereq_version) → non-blocking teacher
  notice („IT informieren"), never gates startup. cleanup_stale_installers gains the
  Klassenraum exe pattern.
- cloud_training_api/app/routes/version.py: add the three fields (env: PREREQ_VERSION,
  PREREQ_DOWNLOAD_URL, PREREQ_INSTALLER_SHA256; sha validated like
  _resolve_installer_sha256); W6 (release.yml publish-gui-version) publishes them alongside
  the existing four vars — same --skip-deploys-then-GUI_VERSION-last atomic pattern
  (release.yml:209-226), hash-the-attached-asset-or-empty semantics for the M exe too.
CI:
- release-installer.yml: build + sign BOTH artifacts; attach both to the release; size
  gates SPLIT: keep >100 MB for U (rootfs-inclusive), add a small floor (~2 MB) for M.
  release.yml W6 extended as above. NEW prereq-version-guard job (path set in M7). Existing
  guards that must stay green: powershell-encoding + powershell-native-stderr (new/modified
  .ps1 — configure_firewall.ps1 + write_prereq_stamp.ps1 need UTF-8 BOM + the EAP pairing
  rule), german-strings-lint (new [FEHLER]/[WARNUNG] lines), shell-lint unchanged.
- Version bump: the 3 existing sites (VERSION, U-iss AppVersion, constants fallback) apply
  to U; M carries PREREQ_VERSION independently (its iss AppVersion reads it).

### P3 — IT deployment kit (docs + packaging, no product code)
- .intunewin recipe + detection rule (HKLM PrereqVersion), silent command lines, return
  codes (0/3010), SCCM/PDQ/manual variants. Note: under SYSTEM deploy the M4 camera
  enumeration + attach smoke test degrade gracefully (no distro/session — policies still
  land; smoke test skips), and M6's per-user DD-distro cleanup is a no-op (documented).
- German IT-Handbuch: required-allow GPO list (AllowWSL etc.), signing cert CN for
  AppLocker/WDAC, shared-PC guidance with REAL disk math (per-account cost = VHDX with the
  3 pulled images ≈ 10-15 GB + a 15-30 min first-run pull; 20 GB preflight is per account;
  recommend one student account per robot PC — pre-seeding impossible), old-package
  uninstall (answer „Nein" on the data question, or silent = safe default) + ProgramData
  VHDX cleanup ONLY after checking no adopted distro's BasePath points there, SmartScreen/
  MOTW note for the pre-P1 unsigned window, doubled-desktop-icon note until the old package
  is removed.

### P4 — optional hardening (separate spec when picked up)
- School proxy/TLS-interception detection + CA injection into distro dockerd
  (/usr/local/share/ca-certificates + update-ca-certificates + dockerd restart).
- Repair button for corrupt-but-registered distro (today: silent gap at gui_app.py:769-778
  — is_docker_running fail → log FEHLER + return, no action offered).

## 3. Decided (do NOT re-litigate next session)
- Import moves from installer PowerShell into the GUI (D2). Reasons: German progress UI +
  retry, kills wrong-user registration (over-the-shoulder UAC = admin-token import),
  kills the reboot dead-end + PS5.1 EAP trap class.
- U gets a NEW AppId; coexistence with old machine install is allowed, warned, documented.
- Home/BYOD stays possible WITHOUT any extra download: U ships the M scripts and the wizard
  runs them inline-elevated for admin-capable users (§P2a); the M exe is for IT fleets.
  (Refines v1's "download link only" — U already shipped scripts\*, so bundling costs ~0.)
- Shared PCs: per-user VHDX cost accepted + documented (no shared-distro hack exists).
- M6 Docker Desktop migration stays in M.
- U keeps the EduBotics_Setup.exe asset name (migration invariant, §P2).
- P0 ships alone as 2.12.3 (admin installer era) BEFORE the split lands as 2.13.0.

## 4. Open items for the OWNER (not buildable by a session)
- Start Azure Trusted Signing identity validation (blocks P1 landing, nothing else).
- Confirm publisher legal name for the cert (shows in UAC/SmartScreen).
- Pick the WSL MSI version to pin (latest stable at P2 time) — then fill WslMsiSha256.

## 5. Test matrix (standard-user VM, no admin creds, per CLAUDE.md double-install rule)
P0: UAC-cancel on update → returns to modal with skip enabled (exercises the fixed
last-error path); standard-user modal shows skip immediately; split-token admin sees the
normal flow; repair flows report „abgebrochen" vs „fehlgeschlagen" correctly.
P2: U-before-M → IT screen, no UAC loop; inline elevated M-script run from the wizard
(split-token admin) → stamp lands, wizard proceeds; U update as standard user (silent, no
prompt, GUI relaunches via /RELAUNCHGUI, no locked-file error — AppMutex/GUIPID path);
second account on shared PC (own import, disk preflight); upgrade from 2.12.x same-user
(adopt — incl. ProgramData-BasePath warning) and different-user (fresh import);
ROOTFS_VERSION match → volumes survive / mismatch → consent; Intune SYSTEM deploy of M
incl. 3010; AllowWSL-blocked GPO → error names the policy; M idempotent re-run; signed
exes pass SmartScreen without warning; old 2.12.x GUI auto-updates into per-user U without
admin (migration invariant).
