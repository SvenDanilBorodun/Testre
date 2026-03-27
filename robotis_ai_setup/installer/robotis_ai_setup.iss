; ROBOTIS AI Setup — Inno Setup Script
; Builds Robotis_AI_Setup.exe installer

[Setup]
AppName=ROBOTIS AI
AppVersion=1.0.0
AppPublisher=ROBOTIS CO., LTD.
DefaultDirName={autopf}\ROBOTIS AI
DefaultGroupName=ROBOTIS AI
OutputBaseFilename=Robotis_AI_Setup
OutputDir=output
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
WizardStyle=modern
LicenseFile=assets\license.txt
; Uncomment when icon is available:
; SetupIconFile=assets\icon.ico

[Files]
; Docker compose files
Source: "..\docker\docker-compose.yml"; DestDir: "{app}\docker"; Flags: ignoreversion
Source: "..\docker\docker-compose.gpu.yml"; DestDir: "{app}\docker"; Flags: ignoreversion
Source: "..\docker\.env.template"; DestDir: "{app}\docker"; Flags: ignoreversion

; GUI application (PyInstaller output)
; The dist folder is created by: cd gui && pyinstaller build.spec
Source: "..\gui\dist\RobotisAI\*"; DestDir: "{app}\gui"; Flags: ignoreversion recursesubdirs

; Installer scripts (kept for manual troubleshooting)
Source: "scripts\*"; DestDir: "{app}\scripts"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\Launch ROBOTIS AI"; Filename: "{app}\gui\RobotisAI.exe"; WorkingDir: "{app}"
Name: "{group}\Launch ROBOTIS AI"; Filename: "{app}\gui\RobotisAI.exe"; WorkingDir: "{app}"
Name: "{group}\Verify Installation"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\verify_system.ps1"""; WorkingDir: "{app}"

[Run]
; Post-install steps — run in order

; Step 1: Install prerequisites (WSL2, Docker Desktop, usbipd)
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\install_prerequisites.ps1"""; \
  StatusMsg: "Installing prerequisites (WSL2, Docker, usbipd)..."; \
  Flags: runhidden waituntilterminated

; Step 2: Configure .wslconfig
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\configure_wsl.ps1"""; \
  StatusMsg: "Configuring WSL2 settings..."; \
  Flags: runhidden waituntilterminated

; Step 3: Configure usbipd policy
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\configure_usbipd.ps1"""; \
  StatusMsg: "Configuring USB device policy..."; \
  Flags: runhidden waituntilterminated

; Step 4: Pull Docker images (optional, can be slow)
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\pull_images.ps1"""; \
  StatusMsg: "Pulling Docker images (this may take a while)..."; \
  Flags: runhidden waituntilterminated; \
  Description: "Pull Docker images now (recommended)"; \
  Check: IsDockerRunning

; Step 5: Verify installation
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -File ""{app}\scripts\verify_system.ps1"""; \
  StatusMsg: "Verifying installation..."; \
  Flags: runhidden waituntilterminated postinstall; \
  Description: "Verify installation"

; Step 6: Launch the app (optional, post-install)
Filename: "{app}\gui\RobotisAI.exe"; \
  Description: "Launch ROBOTIS AI now"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop containers on uninstall
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
