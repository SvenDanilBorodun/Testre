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

# ── Camera policy (USB Video Class devices) ───────────────────────────────
# Mirror the ROBOTIS policy add for every USB webcam currently visible to
# Windows. Without this, a student's webcam sits in usbipd's `Not shared`
# state and the GUI's "Kameras scannen" finds nothing — usbipd attach
# requires either admin or an AutoBind policy, and we can't prompt for
# UAC every time the student replugs the camera.
#
# We don't ship a static VID list because webcam vendors are unpredictable
# (Logitech / Microsoft / Sunplus / Chicony / Realtek / Bison / Quanta /
# Lenovo OEM / etc.) and the same vendor IDs ship non-camera devices we
# DON'T want auto-attached (Logitech mice + keyboards, especially). So we
# do it dynamically: ask Get-PnpDevice for class=Camera/Image at install
# time, extract the VID:PID from each, and add a per-device policy. Future
# replugs of those exact cameras auto-attach without admin.
#
# Trade-off: a brand-new camera plugged in AFTER install needs the student
# to re-run this script (or wait for a future installer that re-detects on
# launch). Better than auto-trusting an entire vendor namespace.
try {
    $camPnp = Get-PnpDevice -Class Camera, Image -PresentOnly -Status OK -ErrorAction SilentlyContinue
    if ($camPnp) {
        Write-Step "Scanning Windows for USB webcams (UVC class)..."
        $cameraIds = @()
        foreach ($cam in $camPnp) {
            if ($cam.InstanceId -match 'USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})') {
                $vid = $matches[1].ToLower()
                $pidHex = $matches[2].ToLower()
                $hwid = "${vid}:${pidHex}"
                if ($cameraIds -notcontains $hwid) {
                    $cameraIds += $hwid
                    Write-Host "   Found camera: $($cam.FriendlyName) ($hwid)" -ForegroundColor White
                }
            }
        }
        Write-Diag "cameras_detected" ($cameraIds -join "`n")
        foreach ($hwid in $cameraIds) {
            if ($existingPolicies -match $hwid) {
                Write-Skip "Policy for $hwid already exists"
                continue
            }
            $added = $false
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
                $splat = $attempt.args
                $output = & usbipd @splat 2>&1 | Out-String
                Write-Diag "policy_add_camera_${hwid}_$($attempt.label)" "rc=$LASTEXITCODE`n$output"
                if ($LASTEXITCODE -eq 0) {
                    Write-OK "Camera policy added ($($attempt.label)): $hwid -> Allow"
                    $added = $true
                    $addedCount++
                    break
                }
            }
            # Even with a policy, the FIRST usbipd attach after install needs the
            # device to be `bind`'d. AutoBind handles future replugs but the
            # currently-plugged camera is still `Not shared` until we explicitly
            # bind it here (we ARE elevated — this is the only chance). Without
            # this the student would have to unplug+replug after install just to
            # get the camera into a shareable state.
            if ($added) {
                $bindOut = usbipd bind --hardware-id $hwid 2>&1 | Out-String
                Write-Diag "camera_bind_$hwid" "rc=$LASTEXITCODE`n$bindOut"
                if ($LASTEXITCODE -eq 0) {
                    Write-OK "Camera $hwid bound (immediately attachable without replug)"
                }
            }
        }
    } else {
        Write-Skip "No USB webcams plugged in at install time — student can re-run this script after plugging one in"
        Write-Diag "cameras_detected" "Get-PnpDevice -Class Camera,Image returned no PresentOnly devices"
    }
} catch {
    Write-Diag "cameras_detected" "Get-PnpDevice raised: $_"
}

# Refresh the policy snapshot for the log so post-install support has
# the after-state cached.
try {
    $afterPolicies = usbipd policy list 2>&1 | Out-String
    Write-Diag "usbipd_policy_list_after" $afterPolicies
} catch { }

if ($addedCount -gt 0) {
    Write-Host "   EduBotics USB-Geräte can now be attached to the EduBotics WSL2 distro without admin rights." -ForegroundColor Green
    Write-Host "   (usage: usbipd attach --wsl EduBotics --busid <BUSID>)" -ForegroundColor Gray
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
$attachOut = usbipd attach --wsl $DistroName --busid $smokeBusid 2>&1 | Out-String
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
