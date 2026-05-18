# configure_usbipd.ps1 — Set up usbipd policy for EduBotics USB-Geräte
# Requires usbipd 4.x+ for policy support
# Must run elevated (as Administrator)
#
# Behavior contract:
#   1. Always append a transcript to %LOCALAPPDATA%\EduBotics\install_diagnostics.log
#      so post-mortems on "Arme scannen findet nichts" have raw evidence.
#   2. Add policy for known PIDs (0103, 2202) + any VID-2F5D PID that is
#      currently plugged in. If `--operation AutoBind` is rejected by the
#      installed usbipd, retry without that flag — older 5.x patch releases
#      changed the operation enum.
#   3. If an arm is physically connected, perform a real `usbipd attach`
#      smoke test so the install transcript proves the policy actually
#      works on this machine. Detach again on success — the GUI will
#      re-attach when the student clicks "Arme scannen".

$ErrorActionPreference = "Continue"

$ROBOTIS_VID = "2F5D"
$DistroName  = "EduBotics"

# ── Diagnostics sink ───────────────────────────────────────────────────────
$DiagDir = Join-Path $env:LOCALAPPDATA "EduBotics"
if (-not (Test-Path $DiagDir)) {
    New-Item -ItemType Directory -Path $DiagDir -Force | Out-Null
}
$DiagLog = Join-Path $DiagDir "install_diagnostics.log"

function Write-Diag {
    param([string]$section, [string]$body)
    $ts = (Get-Date).ToString("o")
    $header = "`n=== $ts configure_usbipd::$section ==="
    Add-Content -Path $DiagLog -Value $header
    Add-Content -Path $DiagLog -Value $body
}

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Skip { param([string]$msg) Write-Host "   SKIP: $msg" -ForegroundColor Yellow }
function Write-Warn { param([string]$msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }

Write-Step "Configuring usbipd policy for EduBotics-Geräte..."

# ── usbipd version detection ───────────────────────────────────────────────
try {
    $versionOutput = usbipd --version 2>&1
    Write-Diag "version" ($versionOutput | Out-String)
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
    Write-Diag "version" "usbipd --version raised: $_"
    exit 1
}

if ($version.Major -lt 4) {
    Write-Skip "usbipd $version does not support policy (requires 4.x+)"
    Write-Host "   Students will need to run the GUI as Administrator for USB attach." -ForegroundColor Yellow
    Write-Diag "version" "usbipd $version too old — skipping policy add"
    exit 0
}

# ── Discover connected ROBOTIS PIDs ────────────────────────────────────────
$knownPIDs = @("0103", "2202")  # OpenRB-150 default + alt firmware
$listOutput = ""
try {
    $listOutput = usbipd list 2>&1 | Out-String
    Write-Diag "usbipd_list_at_install" $listOutput
    foreach ($line in $listOutput -split "`n") {
        if ($line -match "($ROBOTIS_VID):([0-9a-fA-F]{4})") {
            $discoveredPID = $Matches[2]
            if ($knownPIDs -notcontains $discoveredPID) {
                $knownPIDs += $discoveredPID
                Write-Host "   Discovered ROBOTIS PID: $discoveredPID" -ForegroundColor White
            }
        }
    }
} catch {
    Write-Diag "usbipd_list_at_install" "usbipd list raised: $_"
}

$armConnected = $listOutput -match "($ROBOTIS_VID):"

# ── Add policy entries ────────────────────────────────────────────────────
$existingPolicies = ""
try {
    $existingPolicies = usbipd policy list 2>&1 | Out-String
    Write-Diag "usbipd_policy_list_before" $existingPolicies
} catch { }

$addedCount = 0
foreach ($productId in $knownPIDs) {
    $hwid = "${ROBOTIS_VID}:${productId}"
    if ($existingPolicies -match $hwid) {
        Write-Skip "Policy for $hwid already exists"
        continue
    }

    $added = $false
    # Try the 5.x AutoBind syntax first; fall back to the 4.x form, then a
    # bare `policy add` without --operation. Different usbipd patch releases
    # have shifted the enum values; we want the policy on the box, not a
    # specific syntactic dance.
    $attempts = @()
    if ($version.Major -ge 5) {
        $attempts += @{
            label = "5.x --operation AutoBind"
            args  = @("policy", "add", "--hardware-id", $hwid, "--effect", "Allow", "--operation", "AutoBind")
        }
        $attempts += @{
            label = "5.x --operation Connected"
            args  = @("policy", "add", "--hardware-id", $hwid, "--effect", "Allow", "--operation", "Connected")
        }
    }
    $attempts += @{
        label = "no --operation"
        args  = @("policy", "add", "--hardware-id", $hwid, "--effect", "Allow")
    }

    foreach ($attempt in $attempts) {
        # Splat the argument array into the call. PowerShell's @-splat
        # operator is on a *variable name*, not on a literal expression,
        # so we copy into a local before invoking.
        $splat = $attempt.args
        $output = & usbipd @splat 2>&1 | Out-String
        Write-Diag "policy_add_${hwid}_$($attempt.label)" "rc=$LASTEXITCODE`n$output"
        if ($LASTEXITCODE -eq 0) {
            Write-OK "Policy added ($($attempt.label)): $hwid -> Allow"
            $added = $true
            $addedCount++
            break
        }
    }

    if (-not $added) {
        Write-Warn "Could not add policy for $hwid — see install_diagnostics.log"
    }
}

# Refresh the policy snapshot for the log so post-install support has
# the after-state cached.
try {
    $afterPolicies = usbipd policy list 2>&1 | Out-String
    Write-Diag "usbipd_policy_list_after" $afterPolicies
} catch { }

if ($addedCount -gt 0) {
    Write-Host "   EduBotics USB-Geräte can now be attached to the EduBotics WSL2 distro without admin rights." -ForegroundColor Green
    Write-Host "   (usage: usbipd attach --wsl --distribution EduBotics --busid <BUSID>)" -ForegroundColor Gray
} elseif ($addedCount -eq 0 -and $existingPolicies -match $ROBOTIS_VID) {
    Write-Host "   All EduBotics policies already configured." -ForegroundColor Green
} else {
    Write-Warn "No policies were added. USB attach may require running as Administrator."
    Write-Host "   Tip: plug in an EduBotics-Gerät and re-run this script to auto-detect its PID." -ForegroundColor Yellow
}

# ── Real attach smoke test (only if an arm is physically connected) ───────
# This is the load-bearing check: it proves on THIS machine that the
# combination of (driver, usbipd version, WSL kernel, policy) is wired
# up correctly. If the student plugged in an arm at install time and
# this fails, they'll see a clear error before reaching "Arme scannen".
if (-not $armConnected) {
    Write-Skip "No ROBOTIS-Gerät plugged in at install time — attach smoke test deferred to first launch"
    Write-Diag "attach_smoke" "No VID 2F5D device present; smoke test skipped"
    exit 0
}

# Need the EduBotics WSL distro to attach to. If the rootfs import hasn't
# happened yet (e.g. pending reboot), skip — install_edubotics_wsl.ps1
# runs later and the GUI will attach on first scan.
$distroReady = $false
try {
    $listed = wsl --list --quiet 2>&1
    foreach ($line in $listed) {
        if (($line -replace "`0", "").Trim() -eq $DistroName) {
            $distroReady = $true
            break
        }
    }
} catch { }

if (-not $distroReady) {
    Write-Skip "EduBotics distro not yet registered — attach smoke test deferred"
    Write-Diag "attach_smoke" "Distro $DistroName not registered at this stage"
    exit 0
}

# Find the first VID 2F5D BUSID in the list output we already captured.
$smokeBusid = ""
foreach ($line in $listOutput -split "`n") {
    if ($line -match "^\s*(\d+-\d+)\s+$ROBOTIS_VID`:[0-9a-fA-F]{4}") {
        $smokeBusid = $Matches[1]
        break
    }
}

if (-not $smokeBusid) {
    Write-Diag "attach_smoke" "Could not parse a BUSID from usbipd list output"
    exit 0
}

Write-Step "Attach smoke test on busid=$smokeBusid"
$attachOut = usbipd attach --wsl --distribution $DistroName --busid $smokeBusid 2>&1 | Out-String
Write-Diag "attach_smoke" "attach rc=$LASTEXITCODE`n$attachOut"

if ($LASTEXITCODE -eq 0) {
    Write-OK "USB attach works on this machine."
    # Detach again — the GUI will re-attach when the student clicks scan.
    $detachOut = usbipd detach --busid $smokeBusid 2>&1 | Out-String
    Write-Diag "attach_smoke_detach" "detach rc=$LASTEXITCODE`n$detachOut"
} else {
    Write-Warn "USB attach smoke test failed. The GUI will show a diagnostic when the student clicks 'Arme scannen'."
    Write-Host "   See: $DiagLog" -ForegroundColor Yellow
}
