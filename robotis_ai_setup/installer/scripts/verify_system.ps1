# verify_system.ps1 — Post-install validation
#
# Verifies the EduBotics WSL2 distro is up, Docker engine runs inside it, and
# the 3 images are pulled. Does NOT check for Docker Desktop on the host.

param(
    [string]$DistroName = "EduBotics"
)

$ErrorActionPreference = "Continue"

function Write-Step  { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK    { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-FAIL  { param([string]$msg) Write-Host "   FAIL: $msg" -ForegroundColor Red }
function Write-WARN  { param([string]$msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }

$allOk = $true

Write-Step "Verifying EduBotics installation..."

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

# 4. usbipd
Write-Host "   Checking usbipd..." -ForegroundColor White
try {
    $ver = usbipd --version 2>&1
    Write-OK "usbipd ($ver)"
} catch { Write-FAIL "usbipd not found"; $allOk = $false }

# 5. Docker images
# Read REGISTRY + IMAGE_TAG from docker/versions.env so this script checks
# the SAME bytes that docker-compose will run later.
Write-Host "   Checking Docker images..." -ForegroundColor White
$registry = "nettername"
$imageTag = "latest"
$VersionsEnv = Join-Path $PSScriptRoot "..\..\docker\versions.env"
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
} else {
    Write-Host "   Some checks failed. Review the output above." -ForegroundColor Yellow
}
