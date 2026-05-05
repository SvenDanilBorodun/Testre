; EduBotics Setup — Inno Setup Script
; Builds EduBotics_Setup.exe installer
;
; Ships a bundled WSL2 distro (assets\edubotics-rootfs.tar.gz) containing
; headless Docker Engine. Docker Desktop is uninstalled during setup if present.
;
; ─── Pinned third-party dependencies ──────────────────────────────────
; usbipd-win is downloaded + executed elevated during install. We pin
; the exact version + SHA256 here so install_prerequisites.ps1 can
; verify the MSI before msiexec runs — defends against MITM/mirror
; tampering and against an upstream `latest` shipping a breaking change.
;
; Asset naming: usbipd-win 5.x ships per-architecture MSIs named
;   usbipd-win_<VERSION>_x64.msi
; (the older 4.x suffix-less naming is gone, so the GitHub
; "latest/download/usbipd-win_x64.msi" alias also no longer resolves).
;
; Release procedure when bumping usbipd-win:
;   1. Update UsbipdVersion below.
;   2. PowerShell:
;        $url = "https://github.com/dorssel/usbipd-win/releases/download/v<NEW>/usbipd-win_<NEW>_x64.msi"
;        Invoke-WebRequest -Uri $url -OutFile "$env:TEMP\u.msi" -UseBasicParsing
;        (Get-FileHash "$env:TEMP\u.msi" -Algorithm SHA256).Hash
;      Paste the 64-char hex output into UsbipdSha256.
;   3. Smoke-test the installer end-to-end on a clean Windows VM.
#define UsbipdVersion "5.3.0"
; SHA256 of usbipd-win_5.3.0_x64.msi (downloaded + verified 2026-04-25).
; Source: https://github.com/dorssel/usbipd-win/releases/tag/v5.3.0
; UsbipdSha256: leave as the literal "RELEASE_PIN_NEEDED" sentinel ONLY if
; you intentionally want the unsigned-download fallback for a dev build —
; install_prerequisites.ps1 hard-fails on that sentinel for production.
#define UsbipdSha256 "1C984914AEC944DE19B64EFF232421439629699F8138E3DDC29301175BC6D938"

[Setup]
AppId={{B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}
AppName=EduBotics
AppVersion=2.2.3
AppPublisher=EduBotics
DefaultDirName={autopf}\EduBotics
DefaultGroupName=EduBotics
OutputBaseFilename=EduBotics_Setup
OutputDir=output
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
WizardStyle=modern
LicenseFile=assets\license.txt
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\gui\EduBotics.exe

[InstallDelete]
; Wipe the entire gui/ folder before upgrade — guarantees no stale files
; from older versions (renamed modules, removed DLLs, etc.) stick around.
; PyInstaller's full output goes here anyway, so nothing user-modified.
Type: filesandordirs; Name: "{app}\gui"
; Wipe scripts/ for same reason — we sometimes rename/remove .ps1 helpers.
Type: filesandordirs; Name: "{app}\scripts"
; Old v2.1.0 / v2.2.0 layout wrote .env into Program Files. v2.2.1+ moved
; it to %LOCALAPPDATA%\EduBotics\.env, so clean up the legacy file.
Type: files; Name: "{app}\docker\.env"
; Old rootfs copies from previous versions — keep the ship folder tidy.
Type: filesandordirs; Name: "{app}\wsl_rootfs"

[Files]
; Docker compose files
Source: "..\docker\docker-compose.yml"; DestDir: "{app}\docker"; Flags: ignoreversion
Source: "..\docker\docker-compose.gpu.yml"; DestDir: "{app}\docker"; Flags: ignoreversion
Source: "..\docker\.env.template"; DestDir: "{app}\docker"; Flags: ignoreversion
; s6-overlay marker to auto-start ROS2 services (mounted by docker-compose.yml)
Source: "..\docker\physical_ai_server\.s6-keep"; DestDir: "{app}\docker\physical_ai_server"; Flags: ignoreversion

; Bundled WSL2 rootfs — contains Ubuntu 22.04 + headless Docker Engine + nvidia-container-toolkit.
; Built by wsl_rootfs/build_rootfs.sh before running iscc.
Source: "assets\edubotics-rootfs.tar.gz"; DestDir: "{app}\wsl_rootfs"; Flags: ignoreversion
; SHA256 sidecar — import_edubotics_wsl.ps1 verifies the tarball against
; this hash before `wsl --import`, so a corrupted download / swapped tar
; fails fast with a clear error instead of wedging WSL2.
Source: "assets\edubotics-rootfs.tar.gz.sha256"; DestDir: "{app}\wsl_rootfs"; Flags: ignoreversion

; GUI application (PyInstaller output)
; The dist folder is created by: cd gui && pyinstaller build.spec
Source: "..\gui\dist\EduBotics\*"; DestDir: "{app}\gui"; Flags: ignoreversion recursesubdirs

; Installer scripts (kept for manual troubleshooting)
Source: "scripts\*"; DestDir: "{app}\scripts"; Flags: ignoreversion

; Brand icon — also copied into {app} so shortcuts keep a stable IconFilename
; independent of PyInstaller's dist layout.
Source: "assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\EduBotics starten"; Filename: "{app}\gui\EduBotics.exe"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{group}\EduBotics starten"; Filename: "{app}\gui\EduBotics.exe"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Installation prüfen"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\verify_system.ps1"""; WorkingDir: "{app}"

[Run]
; Post-install steps — run in order (hidden, students only see Inno Setup progress)

; Step 0: Alte Docker-Desktop-Installation entfernen (falls vorhanden)
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\migrate_from_docker_desktop.ps1"""; \
  StatusMsg: "Frühere Docker-Desktop-Installation wird entfernt..."; \
  Flags: runhidden waituntilterminated

; Step 1: Voraussetzungen installieren (WSL2, usbipd)
; Pin usbipd-win to the version + SHA256 declared at the top of this
; .iss. install_prerequisites.ps1 verifies the SHA before msiexec runs.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\install_prerequisites.ps1"" -UsbipdMsiUrl ""https://github.com/dorssel/usbipd-win/releases/download/v{#UsbipdVersion}/usbipd-win_{#UsbipdVersion}_x64.msi"" -UsbipdMsiSha256 ""{#UsbipdSha256}"""; \
  StatusMsg: "Voraussetzungen werden installiert (WSL2, usbipd)..."; \
  Flags: runhidden waituntilterminated

; Step 2: .wslconfig konfigurieren
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\configure_wsl.ps1"""; \
  StatusMsg: "WSL2-Einstellungen werden konfiguriert..."; \
  Flags: runhidden waituntilterminated

; Step 3: usbipd-Richtlinie konfigurieren
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\configure_usbipd.ps1"""; \
  StatusMsg: "USB-Geräterichtlinie wird konfiguriert..."; \
  Flags: runhidden waituntilterminated

; Step 4: EduBotics WSL2-Distro importieren (skipped if reboot pending)
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\import_edubotics_wsl.ps1"""; \
  StatusMsg: "EduBotics-Umgebung wird eingerichtet (kann 1-3 Min. dauern)..."; \
  Flags: runhidden waituntilterminated; \
  Check: ShouldImportDistro

; Step 5: Docker-Images herunterladen (skipped if reboot pending or distro missing)
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\pull_images.ps1"""; \
  StatusMsg: "Docker-Images werden heruntergeladen (kann etwas dauern)..."; \
  Flags: runhidden waituntilterminated; \
  Description: "Docker-Images jetzt herunterladen (empfohlen)"; \
  Check: ShouldPullImages

; Step 6: Installation überprüfen
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\verify_system.ps1"""; \
  StatusMsg: "Installation wird überprüft..."; \
  Flags: runhidden waituntilterminated postinstall; \
  Description: "Installation überprüfen"

; Step 7: App starten (optional, nach der Installation)
Filename: "{app}\gui\EduBotics.exe"; \
  Description: "EduBotics jetzt starten"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Container beim Deinstallieren stoppen (läuft jetzt in der WSL-Distro).
; Das Script ermittelt die korrekten Pfade selbst — keine hart-codierten Pfade.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\uninstall_stop_containers.ps1"""; \
  Flags: runhidden; \
  RunOnceId: "StopContainers"

; EduBotics WSL2-Distro deregistrieren
Filename: "wsl.exe"; \
  Parameters: "--unregister EduBotics"; \
  Flags: runhidden; \
  RunOnceId: "UnregisterDistro"

[Code]
// Pascal Script: Check if the EduBotics WSL distro is registered
function IsDistroRegistered(): Boolean;
var
  ResultCode: Integer;
  TempFile: String;
  Lines: TArrayOfString;
  i: Integer;
  Line: String;
begin
  Result := False;
  TempFile := ExpandConstant('{tmp}\wsl_list.txt');
  // `wsl --list --quiet` prints distros in UTF-16LE; write to a file then read
  if not Exec(ExpandConstant('{cmd}'), '/c wsl --list --quiet > "' + TempFile + '" 2>&1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    exit;
  if not LoadStringsFromFile(TempFile, Lines) then
    exit;
  for i := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Trim(Lines[i]);
    if Line = 'EduBotics' then
    begin
      Result := True;
      exit;
    end;
  end;
end;

// Check if a reboot is required (flag written by install_prerequisites.ps1)
function IsRebootRequired(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\scripts\.reboot_required'));
end;

// Import the distro only when WSL2 is fully up (no pending reboot).
function ShouldImportDistro(): Boolean;
begin
  Result := not IsRebootRequired();
end;

// Pull images only when the distro is ready.
function ShouldPullImages(): Boolean;
begin
  Result := (not IsRebootRequired()) and IsDistroRegistered();
end;

// Tell Inno Setup a reboot is needed after install
function NeedRestart(): Boolean;
begin
  Result := IsRebootRequired();
end;

// Delete the installer file from %TEMP% / %LOCALAPPDATA%\Temp after Setup
// finishes so stale copies from auto-update downloads don't pile up.
procedure CleanupSourceInstaller();
var
  SrcExe: String;
  LowerSrc: String;
  LowerLocal: String;
begin
  SrcExe := ExpandConstant('{srcexe}');
  LowerSrc := LowerCase(SrcExe);
  LowerLocal := LowerCase(ExpandConstant('{localappdata}'));
  if (Pos(LowerLocal, LowerSrc) > 0)
     or (Pos('\temp\', LowerSrc) > 0)
     or (Pos('\tmp\', LowerSrc) > 0) then
  begin
    DeleteFile(SrcExe);
  end;
end;

// Convert a Windows path to its /mnt/<drive>/... WSL form for the distro.
function ToWslPath(WinPath: String): String;
var
  Normalized: String;
  Drive: String;
  Rest: String;
begin
  Result := WinPath;
  if WinPath = '' then
    exit;
  Normalized := WinPath;
  StringChangeEx(Normalized, '\', '/', True);
  if (Length(Normalized) >= 2) and (Normalized[2] = ':') then
  begin
    Drive := LowerCase(Copy(Normalized, 1, 1));
    Rest := Copy(Normalized, 3, Length(Normalized));
    while (Length(Rest) > 0) and (Rest[1] = '/') do
      Rest := Copy(Rest, 2, Length(Rest));
    Result := '/mnt/' + Drive + '/' + Rest;
  end
  else
    Result := Normalized;
end;

// Stop running containers in the EduBotics WSL distro before installing new
// files (upgrade safety), and cleanup the downloaded installer from %TEMP%.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  DockerDirWsl: String;
begin
  if CurStep = ssInstall then
  begin
    if IsDistroRegistered() then
    begin
      DockerDirWsl := ToWslPath(ExpandConstant('{app}\docker'));
      Exec('wsl.exe', '-d EduBotics --cd "' + DockerDirWsl + '" -- docker compose down',
           '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
  if CurStep = ssDone then
  begin
    CleanupSourceInstaller();
  end;
end;
