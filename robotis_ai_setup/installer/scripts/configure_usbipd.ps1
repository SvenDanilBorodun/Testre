# configure_usbipd.ps1 — Set up usbipd policy for ROBOTIS USB devices
# Requires usbipd 4.x+ for policy support
# Must run elevated (as Administrator)

$ErrorActionPreference = "Stop"

$ROBOTIS_VID = "2F5D"

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Skip { param([string]$msg) Write-Host "   SKIP: $msg" -ForegroundColor Yellow }

Write-Step "Configuring usbipd policy for ROBOTIS devices..."

# Check usbipd version
try {
    $versionOutput = usbipd --version 2>&1
    if ($versionOutput -match '(\d+\.\d+\.\d+)') {
        $version = [version]$Matches[1]
    } elseif ($versionOutput -match '(\d+\.\d+)') {
        $version = [version]$Matches[1]
    } else {
        $version = [version]"0.0"
    }
    Write-Host "   usbipd version: $version" -ForegroundColor White
} catch {
    Write-Host "ERROR: usbipd not found. Install it first." -ForegroundColor Red
    exit 1
}

# Check if policy subcommand is available (4.x+)
if ($version.Major -lt 4) {
    Write-Skip "usbipd $version does not support policy (requires 4.x+)"
    Write-Host "   Students will need to run the GUI as Administrator for USB attach." -ForegroundColor Yellow
    exit 0
}

# Add policy to allow ROBOTIS devices without admin
try {
    # Check if policy already exists
    $existingPolicies = usbipd policy list 2>&1
    if ($existingPolicies -match $ROBOTIS_VID) {
        Write-Skip "Policy for VID $ROBOTIS_VID already exists"
    } else {
        usbipd policy add --hardware-id "${ROBOTIS_VID}:*" --effect Allow
        if ($LASTEXITCODE -eq 0) {
            Write-OK "Policy added: VID ${ROBOTIS_VID}:* -> Allow"
            Write-Host "   ROBOTIS USB devices can now be attached to WSL2 without admin rights." -ForegroundColor Green
        } else {
            Write-Host "WARNING: Failed to add usbipd policy. USB attach may require admin." -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "WARNING: Failed to configure usbipd policy: $_" -ForegroundColor Yellow
    Write-Host "   USB attach may require running the GUI as Administrator." -ForegroundColor Yellow
}
