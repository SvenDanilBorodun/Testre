# install_prerequisites.ps1 — Install WSL2 and usbipd-win
# Must run elevated (as Administrator)
#
# Docker Desktop is intentionally NOT installed here. The EduBotics WSL2 distro
# (imported by import_edubotics_wsl.ps1) ships its own headless Docker Engine.

param(
    # Production runs of this script are invoked from robotis_ai_setup.iss
    # which passes a versioned URL + SHA256 (see UsbipdVersion / UsbipdSha256
    # at the top of the .iss). The default below is only the fallback for
    # a maintainer running this script by hand — kept pointing at v5.3.0
    # because usbipd-win 5.x renamed assets to include the architecture
    # (`_x64` suffix), breaking the older `latest/download/usbipd-win_x64.msi`
    # alias.
    [string]$UsbipdMsiUrl = "https://github.com/dorssel/usbipd-win/releases/download/v5.3.0/usbipd-win_5.3.0_x64.msi",
    # Optional SHA256 of the MSI. If set, the download is verified before
    # msiexec runs — protects elevated PowerShell against a MITM / compromised
    # mirror serving a malicious MSI. Pin via `EDUBOTICS_USBIPD_SHA256` env
    # var for reproducible offline installs; production builds always pass
    # the known-good value via the .iss [Run] section.
    [string]$UsbipdMsiSha256 = $env:EDUBOTICS_USBIPD_SHA256
)

$ErrorActionPreference = "Stop"
$needsReboot = $false

# ── Diagnostics sink ───────────────────────────────────────────────────────
# Every prerequisite step appends to a single log in %LOCALAPPDATA% so that
# when a student hits a problem later, support has the raw evidence of what
# the installer saw.
$DiagDir = Join-Path $env:LOCALAPPDATA "EduBotics"
if (-not (Test-Path $DiagDir)) {
    New-Item -ItemType Directory -Path $DiagDir -Force | Out-Null
}
$DiagLog = Join-Path $DiagDir "install_diagnostics.log"

function Write-Diag {
    param([string]$section, [string]$body)
    $ts = (Get-Date).ToString("o")
    Add-Content -Path $DiagLog -Value "`n=== $ts install_prerequisites::$section ==="
    Add-Content -Path $DiagLog -Value $body
}

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Skip { param([string]$msg) Write-Host "   SKIP: $msg" -ForegroundColor Yellow }

Write-Diag "begin" "PSVersion=$($PSVersionTable.PSVersion); OS=$([System.Environment]::OSVersion.VersionString)"

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

# `wsl --install` installs the feature but defers the Linux kernel update
# until first distro launch. Force it now (idempotent) so `wsl --import`
# in import_edubotics_wsl.ps1 doesn't fight an outdated kernel. Failure
# here is non-fatal — older Windows builds without `wsl --update` will
# self-update on first distro boot anyway.
if (-not $needsReboot) {
    Write-Step "Ensuring WSL2 kernel is current..."
    try {
        $updateOut = wsl --update 2>&1 | Out-String
        Write-Diag "wsl_update" "rc=$LASTEXITCODE`n$updateOut"
        if ($LASTEXITCODE -eq 0) {
            Write-OK "WSL2 kernel is current"
        } else {
            Write-Host "   (wsl --update returned $LASTEXITCODE — will retry on first boot)" -ForegroundColor Yellow
        }
    } catch {
        Write-Diag "wsl_update" "wsl --update raised: $_"
    }
}

# ── Install usbipd-win ──
Write-Step "Checking usbipd-win..."
$usbipdInstalled = $false
try {
    $usbipdPath = Get-Command usbipd -ErrorAction SilentlyContinue
    if ($usbipdPath) { $usbipdInstalled = $true }
} catch { }

if (-not $usbipdInstalled) {
    # Fail loud if the .iss was published without bumping the SHA pin.
    # The literal sentinel comes from robotis_ai_setup.iss UsbipdSha256.
    if ($UsbipdMsiSha256 -eq "RELEASE_PIN_NEEDED") {
        Write-Host "ERROR: usbipd-win SHA256 pin was not filled in for this release." -ForegroundColor Red
        Write-Host "       Update UsbipdSha256 in robotis_ai_setup.iss before shipping." -ForegroundColor Red
        Write-Host "       (Get-FileHash <downloaded.msi> -Algorithm SHA256)" -ForegroundColor Red
        exit 1
    }

    Write-Host "   Downloading usbipd-win..." -ForegroundColor White
    Write-Host "   URL: $UsbipdMsiUrl" -ForegroundColor Gray
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

    # PATH refresh — msiexec updates the system PATH, but the current
    # PowerShell session inherited its env from Inno Setup at launch and
    # won't see the new entry. configure_usbipd.ps1 runs as a child of
    # this script in the same Inno [Run] sequence and would silently
    # fail to find `usbipd.exe`. Recompose PATH from the registry so the
    # remaining install steps work without requiring a reboot.
    try {
        $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
        $userPath    = [System.Environment]::GetEnvironmentVariable("Path", "User")
        $env:Path = ($machinePath, $userPath) -join ";"
        Write-Diag "path_refresh_after_usbipd" "PATH recomposed from registry"
    } catch {
        Write-Diag "path_refresh_after_usbipd" "PATH refresh raised: $_"
    }
} else {
    Write-Skip "usbipd-win already installed"
}

# ── Post-install verification: did Windows already enumerate any ROBOTIS-Geräte? ──
# This is the key install-time signal for the "Arme scannen findet
# nichts" failure mode. If a board is plugged in and Windows sees it,
# this log line will say so — and configure_usbipd.ps1's smoke test
# will then validate the full attach chain. If the student plugged
# nothing in yet, we just note it; the GUI will diagnose at scan time.
Write-Step "Checking whether Windows already sees ROBOTIS-Geräte (VID 2F5D)..."
try {
    $pnp = Get-PnpDevice -PresentOnly -ErrorAction Stop |
           Where-Object { $_.InstanceId -like '*VID_2F5D*' }
    if ($pnp) {
        $count = ($pnp | Measure-Object).Count
        Write-OK "$count ROBOTIS device(s) enumerated by Windows"
        $pnpDump = ($pnp | Select-Object Status, Class, FriendlyName, InstanceId | Out-String)
        Write-Diag "pnp_vid_2f5d" $pnpDump
        Write-Host $pnpDump -ForegroundColor Gray
    } else {
        Write-Host "   No ROBOTIS-Gerät plugged in (or driver missing) at install time." -ForegroundColor Yellow
        Write-Host "   This is OK — the GUI will diagnose at 'Arme scannen' time." -ForegroundColor Gray
        Write-Diag "pnp_vid_2f5d" "Get-PnpDevice returned no VID_2F5D entries"
    }
} catch {
    Write-Diag "pnp_vid_2f5d" "Get-PnpDevice raised: $_"
}

# Probe usbipd from THIS shell — if PATH didn't propagate, the rest of the
# install would silently skip the policy step. Fail loud here instead.
try {
    $usbipdProbe = & usbipd --version 2>&1 | Out-String
    Write-Diag "usbipd_postinstall_probe" "rc=$LASTEXITCODE`n$usbipdProbe"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: usbipd installed but not reachable from this shell." -ForegroundColor Yellow
        Write-Host "      configure_usbipd.ps1 may fail — a reboot will fix this." -ForegroundColor Yellow
    }
} catch {
    Write-Diag "usbipd_postinstall_probe" "usbipd raised: $_"
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
