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

# ── Summary ──
Write-Step "Prerequisites installation complete!"
if ($needsReboot) {
    Write-Host "`nA REBOOT IS REQUIRED to complete WSL2/Docker installation." -ForegroundColor Yellow
    Write-Host "After rebooting, run the installer again to continue setup." -ForegroundColor Yellow
}
