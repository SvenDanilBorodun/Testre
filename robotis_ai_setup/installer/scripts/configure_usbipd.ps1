# configure_usbipd.ps1 — Set up usbipd policy for EduBotics USB-Geräte
# Requires usbipd 4.x+ for policy support
# Must run elevated (as Administrator)

$ErrorActionPreference = "Stop"

$ROBOTIS_VID = "2F5D"

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Skip { param([string]$msg) Write-Host "   SKIP: $msg" -ForegroundColor Yellow }

Write-Step "Configuring usbipd policy for EduBotics-Geräte..."

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

# Collect device PIDs to add policies for.
# Start with known PIDs; also scan connected devices for any we haven't seen.
$knownPIDs = @("0103", "2202")  # OpenRB-150 (0103), OpenRB-150 alternate firmware (2202)
$targetPairs = @()

# Discover connected EduBotics-Geräte and capture their PIDs
try {
    $listOutput = usbipd list 2>&1 | Out-String
    foreach ($line in $listOutput -split "`n") {
        if ($line -match "($ROBOTIS_VID):([0-9a-fA-F]{4})") {
            $discoveredPID = $Matches[2]
            if ($knownPIDs -notcontains $discoveredPID) {
                $knownPIDs += $discoveredPID
            }
        }
    }
} catch { }

foreach ($productId in $knownPIDs) {
    $targetPairs += "${ROBOTIS_VID}:${productId}"
}

# Check existing policies
$existingPolicies = ""
try { $existingPolicies = usbipd policy list 2>&1 | Out-String } catch { }

$addedCount = 0
foreach ($hwid in $targetPairs) {
    if ($existingPolicies -match $hwid) {
        Write-Skip "Policy for $hwid already exists"
        continue
    }
    try {
        if ($version.Major -ge 5) {
            # usbipd 5.x requires --operation
            usbipd policy add --hardware-id $hwid --effect Allow --operation AutoBind
        } else {
            # usbipd 4.x does not have --operation
            usbipd policy add --hardware-id $hwid --effect Allow
        }
        if ($LASTEXITCODE -eq 0) {
            Write-OK "Policy added: $hwid -> Allow"
            $addedCount++
        } else {
            Write-Host "WARNING: Failed to add policy for $hwid." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "WARNING: Failed to add policy for ${hwid}: $_" -ForegroundColor Yellow
    }
}

if ($addedCount -gt 0) {
    Write-Host "   EduBotics USB-Geräte can now be attached to WSL2 without admin rights." -ForegroundColor Green
} elseif ($addedCount -eq 0 -and $existingPolicies -match $ROBOTIS_VID) {
    Write-Host "   All EduBotics policies already configured." -ForegroundColor Green
} else {
    Write-Host "WARNING: No policies were added. USB attach may require running as Administrator." -ForegroundColor Yellow
    Write-Host "   Tip: plug in ein EduBotics-Gerät and re-run this script to auto-detect its PID." -ForegroundColor Yellow
}
