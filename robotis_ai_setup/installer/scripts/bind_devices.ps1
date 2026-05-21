# bind_devices.ps1 — Add AutoBind policy + `usbipd bind` for a set of VID:PID
#                     hardware-IDs.
#
# Called by the GUI's "Kameras freigeben" repair flow when the scan detects
# UVC cameras that Windows can see but usbipd has not bound (they sit in
# `Not shared` state). One elevated invocation processes the whole batch
# so the student only sees a single UAC prompt.
#
# The script is also safe to re-run by hand: every step is idempotent and
# logs to %LOCALAPPDATA%\EduBotics\install_diagnostics.log so support has a
# transcript of what happened on each PC.
#
# Must run elevated (as Administrator).
#
# Usage:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass `
#       -File bind_devices.ps1 `
#       -HardwareIds "046d:0825","0c45:6713" `
#       [-LogPath "$env:TEMP\edubotics_bind_devices.log"]

param(
    [Parameter(Mandatory = $true)]
    [string[]]$HardwareIds,
    [string]$LogPath = "$env:TEMP\edubotics_bind_devices.log"
)

$ErrorActionPreference = "Continue"

# ── Diagnostics sink (matches the install_prerequisites.ps1 layout) ───────
$DiagDir = Join-Path $env:LOCALAPPDATA "EduBotics"
if (-not (Test-Path $DiagDir)) {
    New-Item -ItemType Directory -Path $DiagDir -Force | Out-Null
}
$DiagLog = Join-Path $DiagDir "install_diagnostics.log"

function Write-Diag {
    param([string]$section, [string]$body)
    $ts = (Get-Date).ToString("o")
    Add-Content -Path $DiagLog -Value "`n=== $ts bind_devices::$section ==="
    Add-Content -Path $DiagLog -Value $body
}

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Skip { param([string]$msg) Write-Host "   SKIP: $msg" -ForegroundColor Yellow }
function Write-Warn { param([string]$msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-FAIL { param([string]$msg) Write-Host "   FAIL: $msg" -ForegroundColor Red }

# ── Transcript so the GUI can surface the tail of what we did ─────────────
$transcriptActive = $false
try {
    if (Test-Path $LogPath) { Remove-Item $LogPath -Force -ErrorAction SilentlyContinue }
    Start-Transcript -Path $LogPath -Force -IncludeInvocationHeader | Out-Null
    $transcriptActive = $true
} catch { }

try {
    # ── Resolve usbipd. The MSI puts it on the system PATH, but elevated
    # shells inherit env from explorer.exe at logon, so the freshly-installed
    # binary may not be on this shell's PATH yet. Probe known locations the
    # same way the GUI's usbipd_resolver does, so this script is robust on
    # the same install timeline. ───────────────────────────────────────────
    Write-Step "Resolving usbipd.exe..."
    $usbipdExe = $null
    $cmd = Get-Command usbipd -ErrorAction SilentlyContinue
    if ($cmd) { $usbipdExe = $cmd.Source }
    if (-not $usbipdExe) {
        $candidates = @(
            (Join-Path $env:ProgramFiles "usbipd-win\usbipd.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "usbipd-win\usbipd.exe"),
            "$env:ProgramW6432\usbipd-win\usbipd.exe"
        ) | Where-Object { $_ } | Select-Object -Unique
        foreach ($c in $candidates) {
            if (Test-Path $c) { $usbipdExe = $c; break }
        }
    }
    if (-not $usbipdExe) {
        Write-FAIL "usbipd.exe nicht gefunden. install_prerequisites.ps1 erneut ausführen."
        Write-Diag "resolve" "usbipd not found on PATH or in Program Files"
        exit 2
    }
    Write-OK "usbipd: $usbipdExe"

    # ── Detect usbipd major version for `--operation` syntax handling ─────
    $version = [version]"0.0"
    try {
        $verOut = & $usbipdExe --version 2>&1 | Out-String
        Write-Diag "version" $verOut
        if ($verOut -match '(\d+\.\d+\.\d+)') { $version = [version]$Matches[1] }
        elseif ($verOut -match '(\d+\.\d+)')  { $version = [version]$Matches[1] }
    } catch { }
    Write-Host "   usbipd version: $version" -ForegroundColor White

    if ($version.Major -lt 4) {
        Write-FAIL "usbipd $version unterstützt keine Policy (4.x oder neuer benötigt)."
        Write-Diag "version" "Aborting: usbipd too old"
        exit 3
    }

    # Snapshot existing policy once so the loop below doesn't re-call
    # `policy list` per device.
    $existingPolicies = ""
    try { $existingPolicies = & $usbipdExe policy list 2>&1 | Out-String } catch { }
    Write-Diag "policy_list_before" $existingPolicies

    # ── Bind each VID:PID. ────────────────────────────────────────────────
    # Workflow per device:
    #   1. If no Allow policy yet, add one (AutoBind preferred, Connected
    #      fallback, bare 'add' as last resort — usbipd patch versions have
    #      shifted the operation enum). This is required for non-admin
    #      attaches by the GUI after this elevated session ends.
    #   2. usbipd bind --hardware-id $hwid moves the currently-plugged
    #      device into the Shared state so the GUI's next attach call
    #      succeeds WITHOUT a replug.
    # Both steps are idempotent; re-running on a fully-set-up device is a
    # cheap no-op + diagnostic line.
    $boundCount = 0
    $failedCount = 0
    foreach ($hwid in $HardwareIds) {
        $hwid = $hwid.Trim().ToLower()
        if (-not ($hwid -match '^[0-9a-f]{4}:[0-9a-f]{4}$')) {
            Write-Warn "Ungültige Hardware-ID übersprungen: '$hwid' (erwartet vvvv:pppp)"
            Write-Diag "skip_invalid" $hwid
            continue
        }

        Write-Step "Verarbeite $hwid..."

        # Policy add (only if not already present)
        if ($existingPolicies -match [regex]::Escape($hwid)) {
            Write-Skip "Policy für $hwid existiert bereits"
        } else {
            $attempts = @(
                @{ label = "AutoBind"; args = @("policy", "add", "--hardware-id", $hwid, "--effect", "Allow", "--operation", "AutoBind") },
                @{ label = "Connected"; args = @("policy", "add", "--hardware-id", $hwid, "--effect", "Allow", "--operation", "Connected") },
                @{ label = "no-op-flag"; args = @("policy", "add", "--hardware-id", $hwid, "--effect", "Allow") }
            )
            $added = $false
            foreach ($attempt in $attempts) {
                $splat = $attempt.args
                $out = & $usbipdExe @splat 2>&1 | Out-String
                Write-Diag "policy_add_${hwid}_$($attempt.label)" "rc=$LASTEXITCODE`n$out"
                if ($LASTEXITCODE -eq 0) {
                    Write-OK "Policy hinzugefügt ($($attempt.label)): $hwid -> Allow"
                    $added = $true
                    break
                }
            }
            if (-not $added) {
                Write-Warn "Konnte keine Policy für $hwid hinzufügen — bind wird trotzdem versucht"
            }
        }

        # Bind — moves the currently-attached device into Shared so the GUI
        # can attach it without admin on this same plug-in session.
        $bindOut = & $usbipdExe bind --hardware-id $hwid 2>&1 | Out-String
        Write-Diag "bind_$hwid" "rc=$LASTEXITCODE`n$bindOut"
        if ($LASTEXITCODE -eq 0) {
            Write-OK "$hwid gebunden — sofort attachbar"
            $boundCount++
        } else {
            # usbipd 5.x returns non-zero with "already shared" on re-bind.
            # Treat that as success.
            if ($bindOut -match "already shared|is already shared") {
                Write-Skip "$hwid bereits gebunden"
                $boundCount++
            } else {
                Write-Warn "bind fehlgeschlagen für $hwid"
                $failedCount++
            }
        }
    }

    # ── Snapshot policy state after, for the diag log ─────────────────────
    try {
        $afterPolicies = & $usbipdExe policy list 2>&1 | Out-String
        Write-Diag "policy_list_after" $afterPolicies
    } catch { }

    Write-Step "Fertig: $boundCount erfolgreich, $failedCount fehlgeschlagen."
    if ($failedCount -gt 0) { exit 1 }
    exit 0
} finally {
    if ($transcriptActive) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}
