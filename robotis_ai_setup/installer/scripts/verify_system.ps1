# verify_system.ps1 — Post-install validation
#
# Verifies the EduBotics WSL2 distro is up, Docker engine runs inside it, and
# the 3 images are pulled. Does NOT check for Docker Desktop on the host.

param(
    [string]$DistroName = "EduBotics"
)

$ErrorActionPreference = "Continue"

# Mirror the diagnostics sink used by install_prerequisites.ps1 + configure_usbipd.ps1
# so the verify step also leaves evidence behind for support.
$DiagDir = Join-Path $env:LOCALAPPDATA "EduBotics"
if (-not (Test-Path $DiagDir)) {
    New-Item -ItemType Directory -Path $DiagDir -Force | Out-Null
}
$DiagLog = Join-Path $DiagDir "install_diagnostics.log"

function Write-Diag {
    param([string]$section, [string]$body)
    $ts = (Get-Date).ToString("o")
    Add-Content -Path $DiagLog -Value "`n=== $ts verify_system::$section ==="
    Add-Content -Path $DiagLog -Value $body
}

function Write-Step  { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK    { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-FAIL  { param([string]$msg) Write-Host "   FAIL: $msg" -ForegroundColor Red }
function Write-WARN  { param([string]$msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }

$allOk = $true

Write-Step "Verifying EduBotics installation..."
Write-Host "   Diagnostics log: $DiagLog" -ForegroundColor Gray

# 1. WSL2
Write-Host "   Checking WSL2..." -ForegroundColor White
try {
    wsl --status *>$null
    if ($LASTEXITCODE -eq 0) { Write-OK "WSL2" } else { Write-FAIL "WSL2 not working"; $allOk = $false }
} catch { Write-FAIL "WSL2 not found"; $allOk = $false }

# 2. EduBotics distro registered
Write-Host "   Checking EduBotics distro..." -ForegroundColor White
$distroListed = $false
try {
    $out = wsl --list --quiet 2>&1
    foreach ($line in $out) {
        if (($line -replace "`0", "").Trim() -eq $DistroName) { $distroListed = $true; break }
    }
} catch { }
if ($distroListed) {
    Write-OK "$DistroName distro registered"
} else {
    Write-FAIL "$DistroName distro not found"
    $allOk = $false
}

# 3. Docker engine inside the distro
Write-Host "   Checking Docker engine (inside $DistroName)..." -ForegroundColor White
if ($distroListed) {
    wsl -d $DistroName -- docker info *>$null 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "Docker engine"
    } else {
        Write-FAIL "Docker engine not responding inside $DistroName"
        $allOk = $false
    }
} else {
    Write-WARN "Skipped (distro missing)"
}

# 4. usbipd reachable + policy contains VID 2F5D
Write-Host "   Checking usbipd..." -ForegroundColor White
$usbipdOk = $false
try {
    $ver = usbipd --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "usbipd ($ver)"
        $usbipdOk = $true
        Write-Diag "usbipd_version" $ver
    } else {
        Write-FAIL "usbipd not reachable (exit $LASTEXITCODE)"
        $allOk = $false
    }
} catch {
    Write-FAIL "usbipd not found"
    $allOk = $false
}

if ($usbipdOk) {
    try {
        $policyOut = usbipd policy list 2>&1 | Out-String
        Write-Diag "usbipd_policy_list" $policyOut
        if ($policyOut -match "2F5D|2f5d") {
            Write-OK "usbipd policy contains VID 2F5D rule"
        } else {
            Write-WARN "usbipd policy has no VID 2F5D rule — students may need admin rights to attach"
            Write-Host "   (Re-run scripts\configure_usbipd.ps1 as Administrator to fix.)" -ForegroundColor Yellow
        }
    } catch {
        Write-Diag "usbipd_policy_list" "raised: $_"
    }

    # USB bus reachability — does Windows itself see VID 2F5D right now?
    # This is the ground truth: if Windows can't enumerate the device,
    # nothing usbipd/WSL/Docker does will help.
    Write-Host "   Checking whether Windows currently sees ROBOTIS-Geräte..." -ForegroundColor White
    try {
        $pnp = Get-PnpDevice -PresentOnly -ErrorAction Stop |
               Where-Object { $_.InstanceId -like '*VID_2F5D*' }
        if ($pnp) {
            $names = ($pnp | ForEach-Object { $_.FriendlyName }) -join ", "
            Write-OK "Windows sees ROBOTIS-Gerät(e): $names"
            Write-Diag "pnp_at_verify" ($pnp | Select-Object Status, Class, FriendlyName, InstanceId | Out-String)
        } else {
            Write-WARN "Windows currently sees no ROBOTIS-Gerät (VID 2F5D)"
            Write-Host "   This is normal if the arms are not plugged in yet." -ForegroundColor Gray
            Write-Host "   Plug them in and re-run this script to verify the full chain." -ForegroundColor Gray
            Write-Diag "pnp_at_verify" "no VID_2F5D devices present"
        }
    } catch {
        Write-Diag "pnp_at_verify" "Get-PnpDevice raised: $_"
    }

    # Also capture usbipd list at verify time so support has the snapshot.
    try {
        $listOut = usbipd list 2>&1 | Out-String
        Write-Diag "usbipd_list_at_verify" $listOut
    } catch { }
}

# 5. Docker images
# Read REGISTRY + IMAGE_TAG from docker/versions.env so this script checks
# the SAME bytes that docker-compose will run later.
# Path resolution: installed layout has {app}\docker\versions.env (parent of
# scripts dir); dev tree has it two levels up. Try installed first, fall
# back to dev. The previous "..\.." form only worked in dev — production
# installs silently always fell back to :latest.
Write-Host "   Checking Docker images..." -ForegroundColor White
# Primary registry default (GHCR). pull_images.ps1 re-tags any Docker Hub
# fallback pull to the primary name, so checking the primary ref finds the
# image regardless of which registry served it.
$registry = "ghcr.io/svendanilborodun"
$imageTag = "latest"
$AppRootForVersions = Split-Path -Parent $PSScriptRoot
$VersionsEnv = Join-Path $AppRootForVersions "docker\versions.env"
if (-not (Test-Path $VersionsEnv)) {
    $VersionsEnv = Join-Path $PSScriptRoot "..\..\docker\versions.env"
}
if (Test-Path $VersionsEnv) {
    Get-Content $VersionsEnv | ForEach-Object {
        if ($_ -match '^\s*IMAGE_TAG\s*=\s*(.+?)\s*$') { $imageTag = $Matches[1] }
        if ($_ -match '^\s*REGISTRY\s*=\s*(.+?)\s*$')  { $registry  = $Matches[1] }
    }
}
$images = @(
    "${registry}/open-manipulator:${imageTag}",
    "${registry}/physical-ai-server:${imageTag}",
    "${registry}/physical-ai-manager:${imageTag}"
)
if ($distroListed) {
    foreach ($image in $images) {
        wsl -d $DistroName -- docker image inspect $image *>$null 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-OK $image
        } else {
            Write-WARN "$image not pulled yet (will be pulled on first launch)"
        }
    }
} else {
    Write-WARN "Image check skipped (distro missing)"
}

# 6. GPU (optional)
Write-Host "   Checking NVIDIA GPU..." -ForegroundColor White
try {
    nvidia-smi *>$null
    if ($LASTEXITCODE -eq 0) { Write-OK "NVIDIA GPU detected" } else { Write-WARN "No NVIDIA GPU (CPU mode)" }
} catch { Write-WARN "No NVIDIA GPU (CPU mode will be used)" }

# 7. Install directory and required files
Write-Host "   Checking install directory..." -ForegroundColor White
# Derive install dir from script location — scripts/ is one level below {app}
$installDir = Split-Path -Parent $PSScriptRoot
$requiredFiles = @(
    @{ Path = "$installDir\docker\docker-compose.yml";                     Label = "docker-compose.yml" },
    @{ Path = "$installDir\docker\docker-compose.gpu.yml";                 Label = "docker-compose.gpu.yml" },
    @{ Path = "$installDir\docker\physical_ai_server\.s6-keep";            Label = "s6 autostart marker" },
    @{ Path = "$installDir\wsl_rootfs\edubotics-rootfs.tar.gz";            Label = "EduBotics WSL rootfs" }
)
foreach ($file in $requiredFiles) {
    if (Test-Path $file.Path) {
        Write-OK $file.Label
    } else {
        Write-FAIL "$($file.Label) not found at $($file.Path)"
        $allOk = $false
    }
}

# Summary
Write-Step "Verification complete!"
if ($allOk) {
    Write-Host "   All checks passed. You're ready to go!" -ForegroundColor Green
    Write-Diag "verify_summary" "PASS"
    exit 0
} else {
    # Audit H23: previously this branch logged a yellow warning and
    # exited 0, so Inno Setup's [Run] step considered the verify
    # successful and the installer reported a green checkmark on a
    # half-broken install. Propagating the failure exits the .iss
    # step non-zero so the installer can surface a German error /
    # offer to re-run prerequisites.
    Write-Host "   Some checks failed. Review the output above." -ForegroundColor Yellow
    Write-Host "   Full diagnostics: $DiagLog" -ForegroundColor Yellow
    Write-Diag "verify_summary" "FAIL"
    exit 1
}
