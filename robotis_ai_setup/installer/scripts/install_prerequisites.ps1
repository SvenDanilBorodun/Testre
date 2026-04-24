# install_prerequisites.ps1 — Install WSL2 and usbipd-win
# Must run elevated (as Administrator)
#
# Docker Desktop is intentionally NOT installed here. The EduBotics WSL2 distro
# (imported by import_edubotics_wsl.ps1) ships its own headless Docker Engine.

param(
    [string]$UsbipdMsiUrl = "https://github.com/dorssel/usbipd-win/releases/latest/download/usbipd-win_x64.msi",
    # Optional SHA256 of the MSI. If set, the download is verified before
    # msiexec runs — protects elevated PowerShell against a MITM / compromised
    # mirror serving a malicious MSI. Pin via `EDUBOTICS_USBIPD_SHA256` env
    # var for reproducible offline installs; the installer releases should
    # bake a known-good value in the .iss rather than leaving it empty.
    [string]$UsbipdMsiSha256 = $env:EDUBOTICS_USBIPD_SHA256
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

# ── Check Windows edition ──
# WSL2 + Hyper-V require Pro/Enterprise/Education. Home edition silently
# fails at `wsl --install` with an unhelpful error. Fail loud up front.
try {
    $edition = (Get-CimInstance Win32_OperatingSystem).Caption
    Write-Host "   Edition: $edition" -ForegroundColor Gray
    if ($edition -match '\bHome\b') {
        Write-Host "ERROR: Windows Home edition cannot run WSL2 with Hyper-V." -ForegroundColor Red
        Write-Host "       EduBotics requires Windows 11 Pro, Enterprise, or Education." -ForegroundColor Red
        Write-Host "       Please upgrade the edition or use a different machine." -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "   (Edition check skipped: $_)" -ForegroundColor Yellow
}

# ── Check virtualization ──
Write-Step "Checking virtualization support..."
$vmInfo = systeminfo | Select-String "Hyper-V Requirements"
if ($vmInfo -match "VM Monitor Mode Extensions:\s+Yes") {
    Write-OK "Virtualization enabled"
} else {
    Write-Host "WARNING: Virtualization may not be enabled. If WSL2 fails, enable it in BIOS." -ForegroundColor Yellow
}

# ── Controlled Folder Access ──
# CFA blocks elevated installers from writing to %ProgramFiles% even with
# admin rights. The symptom is a silent install that lands a half-broken
# EduBotics with missing scripts. Detect + warn.
try {
    $mp = Get-MpPreference -ErrorAction Stop
    if ($mp.EnableControlledFolderAccess -in 1, 2) {
        Write-Host "WARNING: Controlled Folder Access is enabled." -ForegroundColor Yellow
        Write-Host "         Add C:\Program Files\EduBotics to the CFA allowlist, or the" -ForegroundColor Yellow
        Write-Host "         installer may silently fail to write some files." -ForegroundColor Yellow
    }
} catch {
    # Defender cmdlet not available — ignore.
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
    Write-OK "WSL2 installed (reboot required before EduBotics distro can be imported)"
    $needsReboot = $true
} else {
    Write-Skip "WSL2 already installed"
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

    if ($UsbipdMsiSha256) {
        $actual = (Get-FileHash -Path $msiPath -Algorithm SHA256).Hash
        if ($actual -ne $UsbipdMsiSha256.Trim().ToUpper()) {
            Write-Host "ERROR: usbipd MSI SHA256 mismatch." -ForegroundColor Red
            Write-Host "   Expected: $UsbipdMsiSha256" -ForegroundColor Red
            Write-Host "   Actual:   $actual" -ForegroundColor Red
            Write-Host "   Refusing to install — possible tampering or updated release." -ForegroundColor Red
            Remove-Item $msiPath -Force -ErrorAction SilentlyContinue
            exit 1
        }
        Write-Host "   SHA256 verified" -ForegroundColor Green
    } else {
        Write-Host "   (SHA256 pin not set — skipping integrity check)" -ForegroundColor Yellow
    }

    Write-Host "   Installing usbipd-win..." -ForegroundColor White
    Start-Process msiexec.exe -ArgumentList "/i", $msiPath, "/quiet", "/norestart" -Wait
    Write-OK "usbipd-win installed"
} else {
    Write-Skip "usbipd-win already installed"
}

# ── Summary ──
Write-Step "Prerequisites installation complete!"
if ($needsReboot) {
    # Write flag file so Inno Setup knows a reboot is required before image pull / WSL import.
    $flagPath = Join-Path $PSScriptRoot ".reboot_required"
    Set-Content -Path $flagPath -Value "1"
    Write-Host "`nA REBOOT IS REQUIRED to complete WSL2 installation." -ForegroundColor Yellow
} else {
    # Remove flag if no reboot needed (re-run after reboot)
    $flagPath = Join-Path $PSScriptRoot ".reboot_required"
    if (Test-Path $flagPath) { Remove-Item $flagPath -Force }
}
