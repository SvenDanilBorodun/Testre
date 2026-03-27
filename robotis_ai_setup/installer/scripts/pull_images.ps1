# pull_images.ps1 — Pull Docker images with progress reporting

param(
    [string]$Registry = "nettername"
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }

$images = @(
    "$Registry/open-manipulator:latest",
    "$Registry/physical-ai-server:latest",
    "$Registry/physical-ai-manager:latest"
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

Write-Step "All $total images pulled successfully!"
