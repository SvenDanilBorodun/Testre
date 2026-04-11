# install_prerequisites.ps1 — Install WSL2, Docker Desktop, and usbipd-win
# Must run elevated (as Administrator)

param(
    [string]$DockerInstallerUrl = "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe",
    [string]$UsbipdMsiUrl = "https://github.com/dorssel/usbipd-win/releases/latest/download/usbipd-win_x64.msi"
)

$ErrorActionPreference = "Stop"
$needsReboot = $false

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Skip { param([string]$msg) Write-Host "   SKIP: $msg" -ForegroundColor Yellow }

# ── Check Windows version ──
Write-Step "Checking Windows version..."
$osVersion = [System.Environment]::OSVersion.Version
if ($osVersion.Build -lt 22000) {
    Write-Host "ERROR: Windows 11 (build 22000+) is required. Current build: $($osVersion.Build)" -ForegroundColor Red
    exit 1
}
Write-OK "Windows 11 build $($osVersion.Build)"

# ── Check virtualization ──
Write-Step "Checking virtualization support..."
$vmInfo = systeminfo | Select-String "Hyper-V Requirements"
if ($vmInfo -match "VM Monitor Mode Extensions:\s+Yes") {
    Write-OK "Virtualization enabled"
} else {
    Write-Host "WARNING: Virtualization may not be enabled. If WSL2 fails, enable it in BIOS." -ForegroundColor Yellow
}

# ── Install WSL2 ──
Write-Step "Checking WSL2..."
$wslInstalled = $false
try {
    $wslStatus = wsl --status 2>&1
    if ($LASTEXITCODE -eq 0) { $wslInstalled = $true }
} catch { }

if (-not $wslInstalled) {
    Write-Host "   Installing WSL2..." -ForegroundColor White
    wsl --install --no-distribution
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: WSL2 installation failed." -ForegroundColor Red
        exit 1
    }
    Write-OK "WSL2 installed (reboot may be required)"
    $needsReboot = $true
} else {
    Write-Skip "WSL2 already installed"
}

# ── Install Docker Desktop ──
Write-Step "Checking Docker Desktop..."
$dockerInstalled = $false
try {
    $dockerPath = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerPath) { $dockerInstalled = $true }
} catch { }

if (-not $dockerInstalled) {
    Write-Host "   Downloading Docker Desktop..." -ForegroundColor White
    $installerPath = "$env:TEMP\DockerDesktopInstaller.exe"
    Invoke-WebRequest -Uri $DockerInstallerUrl -OutFile $installerPath -UseBasicParsing
    Write-Host "   Installing Docker Desktop (silent)..." -ForegroundColor White
    Start-Process -FilePath $installerPath -ArgumentList "install", "--quiet", "--accept-license" -Wait
    Write-OK "Docker Desktop installed"
    # Docker Desktop needs a logout/login to update PATH
    $needsReboot = $true
} else {
    Write-Skip "Docker Desktop already installed"
}

# ── Install usbipd-win ──
Write-Step "Checking usbipd-win..."
$usbipdInstalled = $false
try {
    $usbipdPath = Get-Command usbipd -ErrorAction SilentlyContinue
    if ($usbipdPath) { $usbipdInstalled = $true }
} catch { }

if (-not $usbipdInstalled) {
    Write-Host "   Downloading usbipd-win..." -ForegroundColor White
    $msiPath = "$env:TEMP\usbipd-win.msi"
    Invoke-WebRequest -Uri $UsbipdMsiUrl -OutFile $msiPath -UseBasicParsing
    Write-Host "   Installing usbipd-win..." -ForegroundColor White
    Start-Process msiexec.exe -ArgumentList "/i", $msiPath, "/quiet", "/norestart" -Wait
    Write-OK "usbipd-win installed"
} else {
    Write-Skip "usbipd-win already installed"
}

# ── Ensure Docker Desktop starts on login ──
Write-Step "Configuring Docker Desktop auto-start..."
$dockerExe = $null
$candidates = @(
    "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
    "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe",
    "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $dockerExe = $c; break }
}

if ($dockerExe) {
    # When running elevated, HKCU points to the admin's hive, not the student's.
    # Find the logged-in user's SID via explorer.exe and write to their hive.
    $targetRegPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    try {
        $explorerProc = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction Stop | Select-Object -First 1
        $ownerInfo = Invoke-CimMethod -InputObject $explorerProc -MethodName GetOwner -ErrorAction Stop
        $loggedInUser = $ownerInfo.User
        $loggedInDomain = $ownerInfo.Domain
        $userSid = (New-Object System.Security.Principal.NTAccount("$loggedInDomain\$loggedInUser")).Translate([System.Security.Principal.SecurityIdentifier]).Value
        $targetRegPath = "Registry::HKEY_USERS\$userSid\Software\Microsoft\Windows\CurrentVersion\Run"
    } catch {
        Write-Host "   WARNUNG: Konnte eingeloggten Benutzer nicht ermitteln — nutze HKCU" -ForegroundColor Yellow
    }

    $existing = Get-ItemProperty -Path $targetRegPath -Name "Docker Desktop" -ErrorAction SilentlyContinue
    if (-not $existing) {
        New-ItemProperty -Path $targetRegPath -Name "Docker Desktop" -Value "`"$dockerExe`"" -PropertyType String -Force | Out-Null
        Write-OK "Docker Desktop will start on login"
    } else {
        Write-Skip "Docker Desktop auto-start already configured"
    }
} else {
    Write-Skip "Docker Desktop executable not found — auto-start not configured"
}

# ── Summary ──
Write-Step "Prerequisites installation complete!"
if ($needsReboot) {
    # Write flag file so Inno Setup knows a reboot is required
    $flagPath = Join-Path $PSScriptRoot ".reboot_required"
    Set-Content -Path $flagPath -Value "1"
    Write-Host "`nA REBOOT IS REQUIRED to complete WSL2/Docker installation." -ForegroundColor Yellow
} else {
    # Remove flag if no reboot needed (re-run after reboot)
    $flagPath = Join-Path $PSScriptRoot ".reboot_required"
    if (Test-Path $flagPath) { Remove-Item $flagPath -Force }
}
