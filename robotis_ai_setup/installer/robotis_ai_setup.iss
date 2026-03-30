; EduBotics Setup — Inno Setup Script
; Builds EduBotics_Setup.exe installer

[Setup]
AppId={{B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}
AppName=EduBotics
AppVersion=2.0.0
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
; Uncomment when icon is available:
; SetupIconFile=assets\icon.ico

[InstallDelete]
; Clean old PyInstaller artifacts to prevent DLL conflicts on upgrade
Type: filesandordirs; Name: "{app}\gui\_internal"
Type: filesandordirs; Name: "{app}\gui\__pycache__"
; Remove old .env so GUI regenerates it with new camera schema
Type: files; Name: "{app}\docker\.env"

[Files]
; Docker compose files
Source: "..\docker\docker-compose.yml"; DestDir: "{app}\docker"; Flags: ignoreversion
Source: "..\docker\docker-compose.gpu.yml"; DestDir: "{app}\docker"; Flags: ignoreversion
Source: "..\docker\.env.template"; DestDir: "{app}\docker"; Flags: ignoreversion
; s6-overlay marker to auto-start ROS2 services (mounted by docker-compose.yml)
Source: "..\docker\physical_ai_server\.s6-keep"; DestDir: "{app}\docker\physical_ai_server"; Flags: ignoreversion

; GUI application (PyInstaller output)
; The dist folder is created by: cd gui && pyinstaller build.spec
Source: "..\gui\dist\EduBotics\*"; DestDir: "{app}\gui"; Flags: ignoreversion recursesubdirs

; Installer scripts (kept for manual troubleshooting)
Source: "scripts\*"; DestDir: "{app}\scripts"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\EduBotics starten"; Filename: "{app}\gui\EduBotics.exe"; WorkingDir: "{app}"
Name: "{group}\EduBotics starten"; Filename: "{app}\gui\EduBotics.exe"; WorkingDir: "{app}"
Name: "{group}\Installation prüfen"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\verify_system.ps1"""; WorkingDir: "{app}"

[Run]
; Post-install steps — run in order (hidden, students only see Inno Setup progress)

; Step 1: Voraussetzungen installieren (WSL2, Docker Desktop, usbipd)
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\install_prerequisites.ps1"""; \
  StatusMsg: "Voraussetzungen werden installiert (WSL2, Docker, usbipd)..."; \
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

; Step 4: Docker-Images herunterladen (optional, kann dauern)
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\pull_images.ps1"""; \
  StatusMsg: "Docker-Images werden heruntergeladen (kann etwas dauern)..."; \
  Flags: runhidden waituntilterminated; \
  Description: "Docker-Images jetzt herunterladen (empfohlen)"; \
  Check: IsDockerRunning

; Step 5: Installation überprüfen
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\scripts\verify_system.ps1"""; \
  StatusMsg: "Installation wird überprüft..."; \
  Flags: runhidden waituntilterminated postinstall; \
  Description: "Installation überprüfen"

; Step 6: App starten (optional, nach der Installation)
Filename: "{app}\gui\EduBotics.exe"; \
  Description: "EduBotics jetzt starten"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Container beim Deinstallieren stoppen
Filename: "docker"; \
  Parameters: "compose -f ""{app}\docker\docker-compose.yml"" down"; \
  Flags: runhidden; \
  RunOnceId: "StopContainers"

[Code]
// Pascal Script: Check if Docker is running before attempting image pull
function IsDockerRunning(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec('docker', 'info', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

// Stop running containers before installing new files (upgrade safety)
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  ComposeFile: String;
begin
  if CurStep = ssInstall then
  begin
    ComposeFile := ExpandConstant('{app}\docker\docker-compose.yml');
    if FileExists(ComposeFile) and IsDockerRunning() then
    begin
      Exec('docker', 'compose -f "' + ComposeFile + '" down', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;
