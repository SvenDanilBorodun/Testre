# pull_images.ps1 — Pull Docker images into the EduBotics WSL2 distro
#
# Routes every docker command through `wsl -d EduBotics -- docker ...` so we
# never depend on the host having Docker Desktop. The distro ships its own
# headless Docker Engine, imported by import_edubotics_wsl.ps1.

param(
    [string]$Registry   = "nettername",
    [string]$DistroName = "EduBotics"
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }

# Read IMAGE_TAG from docker/versions.env so we pull the SAME bytes that
# docker-compose.yml will run later. Falls back to :latest if the file is
# missing (e.g. dev install before any maintainer build has shipped).
$ImageTag = "latest"
$VersionsEnv = Join-Path $PSScriptRoot "..\..\docker\versions.env"
if (Test-Path $VersionsEnv) {
    Get-Content $VersionsEnv | ForEach-Object {
        if ($_ -match '^\s*IMAGE_TAG\s*=\s*(.+?)\s*$') { $ImageTag = $Matches[1] }
        if ($_ -match '^\s*REGISTRY\s*=\s*(.+?)\s*$' -and -not $PSBoundParameters.ContainsKey('Registry')) {
            $Registry = $Matches[1]
        }
    }
}
Write-Host "Using image tag: $ImageTag (registry: $Registry, distro: $DistroName)" -ForegroundColor Cyan

$images = @(
    "${Registry}/open-manipulator:${ImageTag}",
    "${Registry}/physical-ai-server:${ImageTag}",
    "${Registry}/physical-ai-manager:${ImageTag}"
)

Write-Step "Pulling Docker images into $DistroName..."

# Check the distro exists and docker runs inside it
$listed = $false
try {
    $out = wsl --list --quiet 2>&1
    foreach ($line in $out) {
        if (($line -replace "`0", "").Trim() -eq $DistroName) { $listed = $true; break }
    }
} catch { }
if (-not $listed) {
    Write-Host "ERROR: WSL2 distro '$DistroName' not found. Run import_edubotics_wsl.ps1 first." -ForegroundColor Red
    exit 1
}

wsl -d $DistroName -- docker info *>$null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker engine not running inside $DistroName. Check: wsl -d $DistroName -- systemctl status docker" -ForegroundColor Red
    exit 1
}

$total = $images.Count
$current = 0
foreach ($image in $images) {
    $current++
    Write-Host "`n   [$current/$total] Pulling $image ..." -ForegroundColor White
    wsl -d $DistroName -- docker pull $image
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to pull $image" -ForegroundColor Red
        exit 1
    }
    Write-OK "$image pulled"
}

Write-Step "Cleaning up old images..."
wsl -d $DistroName -- docker image prune -f *>$null
Write-OK "Old images removed"

Write-Step "All $total images pulled successfully!"
