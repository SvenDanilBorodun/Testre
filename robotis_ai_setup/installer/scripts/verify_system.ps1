# verify_system.ps1 — Post-install validation

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

# 2. Docker
Write-Host "   Checking Docker..." -ForegroundColor White
try {
    docker info *>$null
    if ($LASTEXITCODE -eq 0) { Write-OK "Docker Desktop" } else { Write-FAIL "Docker not running"; $allOk = $false }
} catch { Write-FAIL "Docker not found"; $allOk = $false }

# 3. usbipd
Write-Host "   Checking usbipd..." -ForegroundColor White
try {
    $ver = usbipd --version 2>&1
    Write-OK "usbipd ($ver)"
} catch { Write-FAIL "usbipd not found"; $allOk = $false }

# 4. Docker images
# Read REGISTRY + IMAGE_TAG from docker/versions.env so this script checks
# the SAME bytes that docker-compose will run later. Falls back to :latest if
# the file is missing (older installs that predate versions.env).
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
foreach ($image in $images) {
    docker image inspect $image *>$null
    if ($LASTEXITCODE -eq 0) {
        Write-OK $image
    } else {
        Write-WARN "$image not pulled yet (will be pulled on first launch)"
    }
}

# 5. GPU (optional)
Write-Host "   Checking NVIDIA GPU..." -ForegroundColor White
try {
    nvidia-smi *>$null
    if ($LASTEXITCODE -eq 0) { Write-OK "NVIDIA GPU detected" } else { Write-WARN "No NVIDIA GPU (CPU mode)" }
} catch { Write-WARN "No NVIDIA GPU (CPU mode will be used)" }

# 6. Install directory and required files
Write-Host "   Checking install directory..." -ForegroundColor White
# Derive install dir from script location — scripts/ is one level below {app}
$installDir = Split-Path -Parent $PSScriptRoot
$requiredFiles = @(
    @{ Path = "$installDir\docker\docker-compose.yml";                     Label = "docker-compose.yml" },
    @{ Path = "$installDir\docker\docker-compose.gpu.yml";                 Label = "docker-compose.gpu.yml" },
    @{ Path = "$installDir\docker\physical_ai_server\.s6-keep";            Label = "s6 autostart marker" }
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
