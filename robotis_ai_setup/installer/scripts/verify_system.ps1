# verify_system.ps1 — Post-install validation

$ErrorActionPreference = "Continue"

function Write-Step  { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK    { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-FAIL  { param([string]$msg) Write-Host "   FAIL: $msg" -ForegroundColor Red }
function Write-WARN  { param([string]$msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }

$allOk = $true

Write-Step "Verifying ROBOTIS AI Setup installation..."

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
Write-Host "   Checking Docker images..." -ForegroundColor White
$registry = "nettername"
$images = @(
    "$registry/open-manipulator:latest",
    "$registry/physical-ai-server:latest",
    "$registry/physical-ai-manager:latest"
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

# 6. Install directory
Write-Host "   Checking install directory..." -ForegroundColor White
$installDir = "C:\Program Files\ROBOTIS AI"
if (Test-Path "$installDir\docker\docker-compose.yml") {
    Write-OK "Install directory"
} else {
    Write-FAIL "docker-compose.yml not found in $installDir\docker\"
    $allOk = $false
}

# Summary
Write-Step "Verification complete!"
if ($allOk) {
    Write-Host "   All checks passed. You're ready to go!" -ForegroundColor Green
} else {
    Write-Host "   Some checks failed. Review the output above." -ForegroundColor Yellow
}
