# pull_images.ps1 — Pull Docker images with progress reporting

param(
    [string]$Registry = "nettername"
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
Write-Host "Using image tag: $ImageTag (registry: $Registry)" -ForegroundColor Cyan

$images = @(
    "${Registry}/open-manipulator:${ImageTag}",
    "${Registry}/physical-ai-server:${ImageTag}",
    "${Registry}/physical-ai-manager:${ImageTag}"
)

Write-Step "Pulling Docker images..."

# Check Docker is running
$dockerRunning = $false
try {
    docker info *>$null
    $dockerRunning = $true
} catch { }

if (-not $dockerRunning) {
    Write-Host "ERROR: Docker Desktop is not running. Start it and try again." -ForegroundColor Red
    exit 1
}

$total = $images.Count
$current = 0

foreach ($image in $images) {
    $current++
    Write-Host "`n   [$current/$total] Pulling $image ..." -ForegroundColor White
    docker pull $image
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to pull $image" -ForegroundColor Red
        exit 1
    }
    Write-OK "$image pulled"
}

Write-Step "Cleaning up old images..."
docker image prune -f *>$null
Write-OK "Old images removed"

Write-Step "All $total images pulled successfully!"
