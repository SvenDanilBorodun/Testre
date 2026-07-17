# install_prerequisites.ps1 — Install WSL2 and usbipd-win
# Must run elevated (as Administrator)
#
# Docker Desktop is intentionally NOT installed here. The EduBotics WSL2 distro
# (imported by import_edubotics_wsl.ps1) ships its own headless Docker Engine.

param(
    # Production runs of this script are invoked from robotis_ai_setup.iss
    # which passes a versioned URL + SHA256 (see UsbipdVersion / UsbipdSha256
    # at the top of the .iss). The default below is only the fallback for
    # a maintainer running this script by hand — kept pointing at v5.3.0
    # because usbipd-win 5.x renamed assets to include the architecture
    # (`_x64` suffix), breaking the older `latest/download/usbipd-win_x64.msi`
    # alias.
    [string]$UsbipdMsiUrl = "https://github.com/dorssel/usbipd-win/releases/download/v5.3.0/usbipd-win_5.3.0_x64.msi",
    # Optional SHA256 of the MSI. If set, the download is verified before
    # msiexec runs — protects elevated PowerShell against a MITM / compromised
    # mirror serving a malicious MSI. Pin via `EDUBOTICS_USBIPD_SHA256` env
    # var for reproducible offline installs; production builds always pass
    # the known-good value via the .iss [Run] section.
    [string]$UsbipdMsiSha256 = $env:EDUBOTICS_USBIPD_SHA256,
    # Set by the .iss [Run] Step 1 (the installer chain), NOT by
    # finalize_install.ps1. Preserves a .reboot_required flag that an EARLIER
    # step in the SAME install session already wrote — Step 0's
    # migrate_from_docker_desktop.ps1 writes it when Docker Desktop's own
    # uninstaller returns 3010 (reboot to finish the removal). Without this, the
    # Summary at the bottom clears the flag whenever THIS script alone finds no
    # reason to reboot, silently dropping migrate's request and letting Step 4
    # import a distro alongside a half-removed Docker Desktop.
    #
    # Deliberately OFF on the post-reboot finalize path: there the flag is stale
    # by definition, and inheriting it would re-write it after every run, looping
    # the student through an endless "bitte neu starten".
    [switch]$PreserveExistingRebootFlag
)

# EAP=Continue, NOT Stop (load-bearing — mirrors verify_system.ps1). In Windows
# PowerShell 5.1 a native command (wsl/docker) writing to stderr is promoted to a
# TERMINATING NativeCommandError under EAP=Stop — for every redirection form. Under
# Stop `wsl --install` (which prints feature/progress text to stderr) threw on a
# clean WSL-less PC before its $LASTEXITCODE was ever checked. Native outcomes are
# checked via $LASTEXITCODE; do NOT revert to Stop without wrapping every wsl/docker
# call in try/catch.
$ErrorActionPreference = "Continue"
$needsReboot = $false

$FlagPath = Join-Path $PSScriptRoot ".reboot_required"
if ($PreserveExistingRebootFlag -and (Test-Path $FlagPath)) {
    $needsReboot = $true
}

# ── Diagnostics sink ───────────────────────────────────────────────────────
# Every prerequisite step appends to a single log so that when a student hits a
# problem later, support has the raw evidence of what the installer saw.
#
# %ProgramData%, NOT %LOCALAPPDATA%: on a managed school PC the student is a
# standard user, so launching an admin-required installer prompts for a
# DIFFERENT admin account and every [Run] step here executes as THAT admin —
# making %LOCALAPPDATA% the admin's profile. The log then split in two: install
# evidence in the admin's profile, the GUI's own scan-time entries in the
# student's, and support (reading the student's copy) saw neither the
# prerequisite nor the migrate history. ProgramData is machine-wide, so both
# halves land in one file.
#
# The Users ACL grant is what makes that work: the un-elevated GUI has to append
# to this same file, and a directory created under ProgramData by an elevated
# process is read-only for standard users by default. Re-applied on every run
# because `wsl --import` may have created %ProgramData%\EduBotics first, with the
# restrictive inherited ACL. S-1-5-32-545 is the well-known "Users" SID — never
# the localized name, this ships on German Windows (where the ACE reads back as
# VORDEFINIERT\Benutzer). Whole block is best-effort: a logging problem must
# never abort an install step.
#
# SCOPE IS LOAD-BEARING — "this folder and files", NOT the subtree.
# The WSL install root is a CHILD of this directory ($env:ProgramData\EduBotics\
# wsl, see import_edubotics_wsl.ps1 -InstallRoot). The 2026-07 grant used
# ContainerInherit,ObjectInherit, which propagated Users:Modify down onto the
# distro's ext4.vhdx — every standard user on a shared lab PC could tamper with
# the VHDX holding the datasets, HF cache and calibration. ObjectInherit +
# NoPropagateInherit grants the directory itself + the files directly in it (the
# log, which is all the GUI needs) and stops there: wsl\ inherits nothing.
# RemoveAccessRuleAll first — it matches on SID + Allow regardless of rights, so
# it drops the old over-broad ACE on machines that already ran the previous
# version, and Windows then recomputes the children's inherited ACEs (verified:
# an already-inherited Modify on wsl\ext4.vhdx disappears on the next run).
$DiagDir = Join-Path $env:ProgramData "EduBotics"
try {
    if (-not (Test-Path $DiagDir)) {
        New-Item -ItemType Directory -Path $DiagDir -Force | Out-Null
    }
    $DiagAcl = Get-Acl -Path $DiagDir
    $DiagUsers = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-545")
    $DiagAcl.RemoveAccessRuleAll((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $DiagUsers, "Modify", "Allow")))
    $DiagAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $DiagUsers, "Modify", "ObjectInherit", "NoPropagateInherit", "Allow")))
    Set-Acl -Path $DiagDir -AclObject $DiagAcl
} catch { }
$DiagLog = Join-Path $DiagDir "install_diagnostics.log"

function Write-Diag {
    param([string]$section, [string]$body)
    try {
        $ts = (Get-Date).ToString("o")
        Add-Content -Path $DiagLog -Value "`n=== $ts install_prerequisites::$section ==="
        Add-Content -Path $DiagLog -Value $body
    } catch { }
}

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Skip { param([string]$msg) Write-Host "   SKIP: $msg" -ForegroundColor Yellow }

Write-Diag "begin" "PSVersion=$($PSVersionTable.PSVersion); OS=$([System.Environment]::OSVersion.VersionString)"

# ── Persist the usbipd pin for the post-reboot finalize path ───────────────
# finalize_install.ps1 re-invokes THIS script when it finds WSL absent, but it
# is launched by the GUI rather than by Inno, so it cannot know the
# {#UsbipdVersion}/{#UsbipdSha256} values the .iss [Run] Step 1 passes us.
# Without them the elevated MSI download silently skips its integrity check.
# Persist what we were handed, next to the .reboot_required flag (machine-wide
# {app}\scripts, elevated-write only), so finalize can replay it verbatim —
# the .iss stays the single source of truth instead of the hash being copied
# into a third file that can drift out of sync.
#
# The RELEASE_PIN_NEEDED sentinel is persisted too, on purpose: replaying it
# makes finalize hard-fail exactly like the installer does, rather than
# degrading into an unverified download.
if ($UsbipdMsiUrl -and $UsbipdMsiSha256) {
    try {
        Set-Content -Path (Join-Path $PSScriptRoot ".usbipd_pin") `
                    -Value @($UsbipdMsiUrl, $UsbipdMsiSha256) -Encoding ASCII -Force
        Write-Diag "usbipd_pin" "Pin persisted for the finalize path (url + sha256)."
    } catch {
        Write-Diag "usbipd_pin" "Could not persist the usbipd pin: $_"
    }
}

# ── Check Windows version ──
Write-Step "Checking Windows version..."
$osVersion = [System.Environment]::OSVersion.Version
if ($osVersion.Build -lt 22000) {
    Write-Host "ERROR: Windows 11 (build 22000+) is required. Current build: $($osVersion.Build)" -ForegroundColor Red
    exit 1
}
Write-OK "Windows 11 build $($osVersion.Build)"

# ── Log Windows edition (informational only) ──
# WSL2 supports ALL Windows 10/11 editions including Home since Windows 10
# v2004 (build 19041, May 2020). The earlier WSL1+Hyper-V combo needed
# Pro/Enterprise/Education, but WSL2 uses the lightweight Virtual Machine
# Platform feature which is available on Home as well. usbipd-win has no
# edition restrictions either. The actual prerequisite is the build
# number (checked above) plus the VMP + WSL features (enabled via dism
# below). Do not refuse on edition alone — that was a 2020-era false
# alarm that silently locked out Home users for years.
try {
    $edition = (Get-CimInstance Win32_OperatingSystem).Caption
    Write-Host "   Edition: $edition" -ForegroundColor Gray
    Write-Diag "edition" $edition
} catch {
    Write-Host "   (Edition probe failed: $_)" -ForegroundColor Yellow
    Write-Diag "edition" "Get-CimInstance raised: $_"
}

# ── Enable Virtual Machine Platform + WSL features explicitly ──
# `wsl --install` (called below) IS supposed to enable both features, but
# its behaviour across Windows 11 builds — especially on Home edition where
# the dism manifests differ slightly — has been inconsistent in the field.
# Driving the feature enablement explicitly via dism is idempotent (no-op
# if already on) and gives us a deterministic exit code:
#   0    = feature was enabled successfully (or was already on)
#   3010 = feature enabled, host reboot required
#   any other non-zero = real failure; we log and let wsl --install retry
# We do NOT exit on dism failure — `wsl --install` is the authoritative
# fallback. We just want to maximize the chance Home machines reach the
# WSL2 install step in a workable state.
Write-Step "Ensuring WSL2 prerequisite Windows features are enabled..."
foreach ($feature in @("VirtualMachinePlatform", "Microsoft-Windows-Subsystem-Linux")) {
    # State-check BEFORE touching dism. Running `dism /enable-feature`
    # UNCONDITIONALLY was the v2.13.0 pilot-incident trigger: on a machine with
    # an UNRELATED pending servicing transaction (e.g. a pending Windows Update
    # reboot), a no-op enable of an ALREADY-ENABLED feature still returns
    # rc=3010, which we then mis-read as "our WSL feature needs a reboot" -> a
    # spurious .reboot_required that skips the in-installer image pull + distro
    # import (ShouldPullImages/ShouldImportDistro in the .iss gate on it). Only
    # invoke dism when the feature is genuinely off. When the feature store is
    # unreadable — Get-WindowsOptionalFeature throws a COMException while a
    # servicing op is pending — defer to the authoritative `wsl --status` probe
    # below rather than manufacturing a reboot from a dism 3010.
    $featureState = $null
    try {
        $featureState = (Get-WindowsOptionalFeature -Online -FeatureName $feature -ErrorAction Stop).State
        Write-Diag "feature_$feature" "state=$featureState"
    } catch {
        Write-Diag "feature_$feature" "Get-WindowsOptionalFeature failed: $_ (skipping dism to avoid a spurious rc=3010; wsl --status is authoritative)"
        Write-Host "   ($feature state unreadable — deferring to wsl --status)" -ForegroundColor Yellow
        continue
    }
    if ($featureState -eq "Enabled") {
        Write-OK "$feature already enabled"
        continue
    }
    if ($featureState -eq "EnablePending") {
        Write-OK "$feature enable is pending a reboot"
        $needsReboot = $true
        continue
    }
    # Disabled / DisablePending — actually enable it now.
    try {
        $dismOut = dism /online /enable-feature /featurename:$feature /all /norestart 2>&1 | Out-String
        $rc = $LASTEXITCODE
        Write-Diag "feature_$feature" "enable rc=$rc`n$dismOut"
        if ($rc -eq 0) {
            Write-OK "$feature is enabled"
        } elseif ($rc -eq 3010) {
            Write-OK "$feature enabled (reboot required to take effect)"
            $needsReboot = $true
        } else {
            Write-Host "   (dism $feature rc=$rc — wsl --install will attempt to enable too)" -ForegroundColor Yellow
        }
    } catch {
        Write-Diag "feature_$feature" "dism raised: $_"
        Write-Host "   (Could not run dism for ${feature}: $_)" -ForegroundColor Yellow
    }
}

# ── Check virtualization ──
Write-Step "Checking virtualization support..."
$vmInfo = systeminfo | Select-String "Hyper-V Requirements"
if ($vmInfo -match "VM Monitor Mode Extensions:\s+Yes") {
    Write-OK "Virtualization enabled"
} else {
    Write-Host "WARNING: Virtualization may not be enabled. If WSL2 fails, enable it in BIOS." -ForegroundColor Yellow
}

# ── Controlled Folder Access ──
# CFA blocks elevated installers from writing to %ProgramFiles% even with
# admin rights. The symptom is a silent install that lands a half-broken
# EduBotics with missing scripts. Detect + warn.
try {
    $mp = Get-MpPreference -ErrorAction Stop
    if ($mp.EnableControlledFolderAccess -in 1, 2) {
        Write-Host "WARNING: Controlled Folder Access is enabled." -ForegroundColor Yellow
        Write-Host "         Add C:\Program Files\EduBotics to the CFA allowlist, or the" -ForegroundColor Yellow
        Write-Host "         installer may silently fail to write some files." -ForegroundColor Yellow
    }
} catch {
    # Defender cmdlet not available — ignore.
}

# ── Install WSL2 ──
Write-Step "Checking WSL2..."
$wslInstalled = $false
try {
    $wslStatus = wsl --status 2>&1
    if ($LASTEXITCODE -eq 0) { $wslInstalled = $true }
} catch { }

if (-not $wslInstalled) {
    Write-Host "   Installing WSL2..." -ForegroundColor White
    wsl --install --no-distribution
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: WSL2 installation failed." -ForegroundColor Red
        exit 1
    }
    Write-OK "WSL2 installed (reboot required before EduBotics distro can be imported)"
    $needsReboot = $true
} else {
    Write-Skip "WSL2 already installed"
}

# `wsl --install` installs the feature but defers the Linux kernel update
# until first distro launch. Force it now (idempotent) so `wsl --import`
# in import_edubotics_wsl.ps1 doesn't fight an outdated kernel. Failure
# here is non-fatal — older Windows builds without `wsl --update` will
# self-update on first distro boot anyway.
if (-not $needsReboot) {
    Write-Step "Ensuring WSL2 kernel is current..."
    try {
        $updateOut = wsl --update 2>&1 | Out-String
        Write-Diag "wsl_update" "rc=$LASTEXITCODE`n$updateOut"
        if ($LASTEXITCODE -eq 0) {
            Write-OK "WSL2 kernel is current"
        } else {
            Write-Host "   (wsl --update returned $LASTEXITCODE — will retry on first boot)" -ForegroundColor Yellow
        }
    } catch {
        Write-Diag "wsl_update" "wsl --update raised: $_"
    }
}

# ── Install usbipd-win ──
Write-Step "Checking usbipd-win..."
$usbipdInstalled = $false
try {
    $usbipdPath = Get-Command usbipd -ErrorAction SilentlyContinue
    if ($usbipdPath) { $usbipdInstalled = $true }
} catch { }

if (-not $usbipdInstalled) {
    # Fail loud if the .iss was published without bumping the SHA pin.
    # The literal sentinel comes from robotis_ai_setup.iss UsbipdSha256.
    if ($UsbipdMsiSha256 -eq "RELEASE_PIN_NEEDED") {
        Write-Host "ERROR: usbipd-win SHA256 pin was not filled in for this release." -ForegroundColor Red
        Write-Host "       Update UsbipdSha256 in robotis_ai_setup.iss before shipping." -ForegroundColor Red
        Write-Host "       (Get-FileHash <downloaded.msi> -Algorithm SHA256)" -ForegroundColor Red
        exit 1
    }

    Write-Host "   Downloading usbipd-win..." -ForegroundColor White
    Write-Host "   URL: $UsbipdMsiUrl" -ForegroundColor Gray
    $msiPath = "$env:TEMP\usbipd-win.msi"
    Invoke-WebRequest -Uri $UsbipdMsiUrl -OutFile $msiPath -UseBasicParsing

    if ($UsbipdMsiSha256) {
        $actual = (Get-FileHash -Path $msiPath -Algorithm SHA256).Hash
        if ($actual -ne $UsbipdMsiSha256.Trim().ToUpper()) {
            Write-Host "ERROR: usbipd MSI SHA256 mismatch." -ForegroundColor Red
            Write-Host "   Expected: $UsbipdMsiSha256" -ForegroundColor Red
            Write-Host "   Actual:   $actual" -ForegroundColor Red
            Write-Host "   Refusing to install — possible tampering or updated release." -ForegroundColor Red
            Remove-Item $msiPath -Force -ErrorAction SilentlyContinue
            exit 1
        }
        Write-Host "   SHA256 verified" -ForegroundColor Green
    } else {
        Write-Host "   (SHA256 pin not set — skipping integrity check)" -ForegroundColor Yellow
    }

    Write-Host "   Installing usbipd-win..." -ForegroundColor White
    Start-Process msiexec.exe -ArgumentList "/i", $msiPath, "/quiet", "/norestart" -Wait
    Write-OK "usbipd-win installed"

    # PATH refresh — msiexec updates the system PATH, but the current
    # PowerShell session inherited its env from Inno Setup at launch and
    # won't see the new entry. configure_usbipd.ps1 runs as a child of
    # this script in the same Inno [Run] sequence and would silently
    # fail to find `usbipd.exe`. Recompose PATH from the registry so the
    # remaining install steps work without requiring a reboot.
    try {
        $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
        $userPath    = [System.Environment]::GetEnvironmentVariable("Path", "User")
        $env:Path = ($machinePath, $userPath) -join ";"
        Write-Diag "path_refresh_after_usbipd" "PATH recomposed from registry"
    } catch {
        Write-Diag "path_refresh_after_usbipd" "PATH refresh raised: $_"
    }
} else {
    Write-Skip "usbipd-win already installed"
}

# ── Post-install verification: did Windows already enumerate any ROBOTIS-Geräte? ──
# This is the key install-time signal for the "Arme scannen findet
# nichts" failure mode. If a board is plugged in and Windows sees it,
# this log line will say so — and configure_usbipd.ps1's smoke test
# will then validate the full attach chain. If the student plugged
# nothing in yet, we just note it; the GUI will diagnose at scan time.
Write-Step "Checking whether Windows already sees ROBOTIS-Geräte (VID 2F5D)..."
try {
    $pnp = Get-PnpDevice -PresentOnly -ErrorAction Stop |
           Where-Object { $_.InstanceId -like '*VID_2F5D*' }
    if ($pnp) {
        $count = ($pnp | Measure-Object).Count
        Write-OK "$count ROBOTIS device(s) enumerated by Windows"
        $pnpDump = ($pnp | Select-Object Status, Class, FriendlyName, InstanceId | Out-String)
        Write-Diag "pnp_vid_2f5d" $pnpDump
        Write-Host $pnpDump -ForegroundColor Gray
    } else {
        Write-Host "   No ROBOTIS-Gerät plugged in (or driver missing) at install time." -ForegroundColor Yellow
        Write-Host "   This is OK — the GUI will diagnose at 'Arme scannen' time." -ForegroundColor Gray
        Write-Diag "pnp_vid_2f5d" "Get-PnpDevice returned no VID_2F5D entries"
    }
} catch {
    Write-Diag "pnp_vid_2f5d" "Get-PnpDevice raised: $_"
}

# Probe usbipd from THIS shell — if PATH didn't propagate, the rest of the
# install would silently skip the policy step. Fail loud here instead.
try {
    $usbipdProbe = & usbipd --version 2>&1 | Out-String
    Write-Diag "usbipd_postinstall_probe" "rc=$LASTEXITCODE`n$usbipdProbe"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: usbipd installed but not reachable from this shell." -ForegroundColor Yellow
        Write-Host "      configure_usbipd.ps1 may fail — a reboot will fix this." -ForegroundColor Yellow
    }
} catch {
    Write-Diag "usbipd_postinstall_probe" "usbipd raised: $_"
}

# ── Summary ──
Write-Step "Prerequisites installation complete!"
if ($needsReboot) {
    # Write flag file so Inno Setup knows a reboot is required before image pull / WSL import.
    Set-Content -Path $FlagPath -Value "1"
    Write-Host "`nA REBOOT IS REQUIRED to complete WSL2 installation." -ForegroundColor Yellow
} else {
    # Remove flag if no reboot needed (re-run after reboot). $needsReboot is
    # pre-seeded from an existing flag under -PreserveExistingRebootFlag, so an
    # earlier step's request is never dropped here.
    if (Test-Path $FlagPath) { Remove-Item $FlagPath -Force }
}

# Explicit success — mirrors pull_images.ps1. Every genuine failure above exits
# non-zero on its own; without this line the exit code instead falls through to
# the last native command, the COSMETIC `usbipd --version` probe (the cmdlets
# after it don't reset $LASTEXITCODE). That probe's own warning says "a reboot
# will fix this" — yet its rc made finalize_install.ps1 report "Voraussetzungen
# konnten nicht installiert werden" over prerequisites that installed fine.
exit 0
