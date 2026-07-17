# migrate_from_docker_desktop.ps1 — Force-migrate off Docker Desktop
#
# Runs before install_prerequisites.ps1 on every install. On the first install
# where Docker Desktop is still present, this script:
#   1. Stops any running EduBotics compose stack inside Docker Desktop
#   2. Silent-uninstalls Docker Desktop
#   3. Unregisters the docker-desktop* WSL2 distros
#   4. Drops a .migrated marker
# After this, install_prerequisites.ps1 + import_edubotics_wsl.ps1 bring up the
# new self-contained EduBotics distro.
#
# Must run elevated (as Administrator).

$ErrorActionPreference = "Continue"

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Skip { param([string]$msg) Write-Host "   SKIP: $msg" -ForegroundColor Yellow }
function Write-Warn { param([string]$msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-FAIL { param([string]$msg) Write-Host "   FAIL: $msg" -ForegroundColor Red }

# ── Diagnostics sink (mirrors install_prerequisites.ps1) ──
# This script runs `runhidden`, so Write-Host output goes nowhere. Without an
# on-disk sink, a FAILED Docker Desktop removal — the root of the v2.13.0
# incident class (a half-removed DD entangles WSL/VirtualMachinePlatform and
# makes install_prerequisites' DISM return a spurious rc=3010 that skips the
# in-installer image pull) — left ZERO evidence in the support artifact. Append
# to the same log every other installer step uses.
#
# %ProgramData%, NOT %LOCALAPPDATA% (see the long rationale in
# install_prerequisites.ps1): the elevating admin is not the student on a
# managed PC, so %LOCALAPPDATA% split the support artifact across two profiles.
# This script is Step 0 — the FIRST thing the installer runs — so it is also
# what creates the directory + the Users ACL that lets the un-elevated GUI
# append to the same log. Best-effort throughout; logging never aborts a step.
# Scope ("this folder and files", never the subtree) is load-bearing — the WSL
# install root lives under this directory; see install_prerequisites.ps1 for the
# full rationale. Keep all five copies of this block identical.
$DiagDir = Join-Path $env:ProgramData "EduBotics"
try {
    if (-not (Test-Path $DiagDir)) { New-Item -ItemType Directory -Path $DiagDir -Force | Out-Null }
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
        Add-Content -Path $DiagLog -Value "`n=== $ts migrate_from_docker_desktop::$section ==="
        Add-Content -Path $DiagLog -Value $body
    } catch { }
}

# Durable one-shot marker. NOT {app}\scripts: the .iss [InstallDelete] wipes
# that on every upgrade, which reset the old marker so migrate re-ran each
# upgrade — silently uninstalling a Docker Desktop the user may have installed
# AFTER EduBotics for other coursework.
#
# It lives in machine-wide %ProgramData%, not %LOCALAPPDATA%: keyed on the
# latter, "has this machine been migrated?" was really "has THIS ADMIN migrated
# it?" — so on a managed fleet an upgrade elevated by a different admin
# re-ran the migration and re-armed the exact bug the durable marker was added
# to fix. Both legacy locations are still READ, so a machine that already
# migrated is never migrated a second time.
$MigratedFlag = Join-Path $DiagDir ".migrated"
$LegacyMarkers = @(
    (Join-Path $PSScriptRoot ".migrated"),                          # pre-2.13 ({app}\scripts)
    (Join-Path (Join-Path $env:LOCALAPPDATA "EduBotics") ".migrated")  # 2.13 (per-admin profile)
)

$alreadyMigrated = Test-Path $MigratedFlag
if (-not $alreadyMigrated) {
    foreach ($m in $LegacyMarkers) {
        if ($m -and (Test-Path $m)) { $alreadyMigrated = $true; break }
    }
}
if ($alreadyMigrated) {
    Write-Skip "Migration already completed on this machine."
    Write-Diag "skip" "Marker present — migration already done."
    exit 0
}

# Set when Docker Desktop's uninstaller asks for a reboot to finish the job.
$rebootPending = $false
$RebootFlag = Join-Path $PSScriptRoot ".reboot_required"

Write-Step "Detecting Docker Desktop..."

$dockerDesktopExe = $null
$candidates = @(
    "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
    "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe"
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $dockerDesktopExe = $c; break }
}

# Also resolve Docker Desktop from the Uninstall registry — covers custom
# install locations the two hard-coded paths miss (mirrors how
# configure_usbipd.ps1 resolves usbipd from the registry).
$UninstallRoots = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
)
function Get-DockerDesktopUninstallEntry {
    foreach ($root in $UninstallRoots) {
        try {
            $e = Get-ItemProperty -Path $root -ErrorAction SilentlyContinue |
                 Where-Object { $_.DisplayName -like 'Docker Desktop*' } | Select-Object -First 1
        } catch { $e = $null }
        if ($e) { return $e }
    }
    return $null
}
$ddEntry = Get-DockerDesktopUninstallEntry
$ddInstallLocation = $null
if ($ddEntry -and $ddEntry.InstallLocation) { $ddInstallLocation = $ddEntry.InstallLocation }

$dockerDesktopInstaller = $null
$installerCandidates = @(
    "$env:ProgramFiles\Docker\Docker\Docker Desktop Installer.exe",
    "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop Installer.exe"
)
if ($ddInstallLocation) {
    $installerCandidates += (Join-Path $ddInstallLocation "Docker Desktop Installer.exe")
}
foreach ($c in $installerCandidates) {
    if ($c -and (Test-Path $c)) { $dockerDesktopInstaller = $c; break }
}

Write-Diag "detect" "exe=$dockerDesktopExe`ninstaller=$dockerDesktopInstaller`ninstallLocation=$ddInstallLocation`nregistryEntry=$([bool]$ddEntry)"

if (-not $dockerDesktopExe -and -not $dockerDesktopInstaller -and -not $ddEntry) {
    Write-Skip "Docker Desktop not installed — nothing to migrate."
    Set-Content -Path $MigratedFlag -Value "1"
    Write-Diag "skip" "Docker Desktop not present; durable marker written."
    exit 0
}

Write-Host "   Found Docker Desktop at: $dockerDesktopExe" -ForegroundColor White

# 1. Best-effort: stop any running EduBotics compose stack
Write-Step "Stopping EduBotics containers (best-effort)..."
$composeFile = Join-Path $PSScriptRoot "..\docker\docker-compose.yml"
if (Test-Path $composeFile) {
    try {
        docker compose -f $composeFile down *>$null
        Write-OK "Containers stopped"
    } catch {
        Write-Skip "Docker not responsive — skipping compose down"
    }
} else {
    Write-Skip "No compose file found"
}

# 1b. Stop Docker Desktop's service + processes so the uninstaller and the
# `wsl --unregister` below don't fail on locked files / a mounted VHDX.
Write-Step "Stopping Docker Desktop service and processes..."
try {
    $svc = Get-Service -Name "com.docker.service" -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne 'Stopped') {
        Stop-Service -Name "com.docker.service" -Force -ErrorAction Stop
        Write-OK "Stopped com.docker.service"
    }
} catch {
    Write-Warn "Could not stop com.docker.service: $_"
    Write-Diag "stop_service" "raised: $_"
}
foreach ($pname in @("Docker Desktop", "com.docker.backend", "com.docker.build",
                     "com.docker.dev-envs", "com.docker.extensions", "vpnkit",
                     "com.docker.proxy", "dockerd")) {
    try {
        Get-Process -Name $pname -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
    } catch { }
}

# 2. Silent-uninstall Docker Desktop
Write-Step "Uninstalling Docker Desktop..."
$uninstallRc = $null
if ($dockerDesktopInstaller) {
    try {
        $proc = Start-Process -FilePath $dockerDesktopInstaller `
                              -ArgumentList "uninstall", "--quiet" `
                              -Wait -PassThru -WindowStyle Hidden
        $uninstallRc = $proc.ExitCode
        Write-Diag "uninstall_installer" "rc=$uninstallRc"
        if ($uninstallRc -eq 0) {
            Write-OK "Docker Desktop uninstalled"
        } elseif ($uninstallRc -eq 3010) {
            # Docker Desktop's uninstaller finishes on the next boot (it
            # deregisters its WSL distros lazily). Continuing into Step 1/4 in
            # THIS session would run `wsl --import EduBotics` next to a
            # half-removed DD that still holds WSL/VirtualMachinePlatform state —
            # the entanglement behind the v2.13.0 spurious-3010 incident class.
            # So request a reboot: the .iss ShouldImportDistro/ShouldPullImages
            # Checks read this flag and skip, NeedRestart() prompts the student,
            # and the post-reboot finalize completes the install cleanly. We do
            # NOT write the .migrated marker (see the gate below), so the next
            # run re-verifies that DD really is gone.
            $rebootPending = $true
            Write-Warn "Docker Desktop benötigt einen Neustart, um die Entfernung abzuschließen."
            Write-Diag "uninstall_installer" "rc=3010 — deferring: reboot flag written, marker withheld."
            try {
                Set-Content -Path $RebootFlag -Value "1" -Force
            } catch {
                Write-Warn "Konnte die Neustart-Markierung nicht schreiben: $_"
                Write-Diag "uninstall_installer" "Could not write $RebootFlag : $_"
            }
        } else {
            Write-Warn "Uninstaller exited with code $uninstallRc — continuing"
        }
    } catch {
        Write-Warn "Uninstaller invocation failed: $_"
        Write-Diag "uninstall_installer" "raised: $_"
    }
} else {
    # Audit M14: prefer Get-Package (HKLM Uninstall registry — instant)
    # over Get-CimInstance Win32_Product (triggers a full MSI consistency
    # check across every installed package — 90+ s on a 100-app machine,
    # the single slowest WMI query in Windows). Fall back to Win32_Product
    # only if Get-Package is unavailable (very old PowerShell builds).
    try {
        $pkgs = $null
        if (Get-Command Get-Package -ErrorAction SilentlyContinue) {
            $pkgs = Get-Package -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like '*Docker Desktop*' }
        }
        if ($pkgs) {
            Write-Host "   Using Get-Package uninstall..." -ForegroundColor White
            foreach ($p in $pkgs) {
                try {
                    $p | Uninstall-Package -Force -ErrorAction Stop | Out-Null
                } catch {
                    Write-Warn "Uninstall-Package failed for $($p.Name): $_"
                }
            }
            Write-OK "Docker Desktop uninstalled via Get-Package"
        } else {
            # Last-resort fallback. Slow but works on stripped-down
            # PowerShell installs that lack PackageManagement.
            $pkg = Get-CimInstance -ClassName Win32_Product `
                -Filter "Name LIKE '%Docker Desktop%'" -ErrorAction SilentlyContinue
            if ($pkg) {
                Write-Host "   Using Win32_Product WMI uninstall (this takes 1-2 minutes)..." -ForegroundColor White
                Invoke-CimMethod -InputObject $pkg -MethodName Uninstall | Out-Null
                Write-OK "Docker Desktop uninstalled via WMI"
            } else {
                Write-Skip "No Docker Desktop package found"
            }
        }
    } catch {
        Write-Warn "Fallback uninstall failed: $_"
    }
}

# 3. Remove leftover Docker Desktop WSL2 distros
Write-Step "Removing Docker Desktop WSL2 distros..."
$distros = @("docker-desktop", "docker-desktop-data")
foreach ($d in $distros) {
    try {
        $listed = wsl --list --quiet 2>&1 | Where-Object { $_ -replace "`0", "" -eq $d }
        if ($listed) {
            wsl --unregister $d *>$null
            if ($LASTEXITCODE -eq 0) {
                Write-OK "Unregistered $d"
            } else {
                Write-Warn "Could not unregister $d (exit $LASTEXITCODE)"
            }
        } else {
            Write-Skip "$d not registered"
        }
    } catch {
        Write-Warn "WSL query failed for ${d}: $_"
    }
}

# 4. Remove the auto-start registry entry written by the old install_prerequisites.ps1
Write-Step "Cleaning Docker Desktop auto-start entry..."
try {
    $explorerProc = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction Stop | Select-Object -First 1
    $ownerInfo = Invoke-CimMethod -InputObject $explorerProc -MethodName GetOwner -ErrorAction Stop
    $loggedInUser = $ownerInfo.User
    $loggedInDomain = $ownerInfo.Domain
    $userSid = (New-Object System.Security.Principal.NTAccount("$loggedInDomain\$loggedInUser")).Translate([System.Security.Principal.SecurityIdentifier]).Value
    $regPath = "Registry::HKEY_USERS\$userSid\Software\Microsoft\Windows\CurrentVersion\Run"
    if (Get-ItemProperty -Path $regPath -Name "Docker Desktop" -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $regPath -Name "Docker Desktop" -Force
        Write-OK "Auto-start entry removed"
    } else {
        Write-Skip "No auto-start entry present"
    }
} catch {
    Write-Skip "Could not query logged-in user registry"
}

# 5. Verify Docker Desktop is actually gone BEFORE marking the machine migrated.
# The old code wrote the marker unconditionally, so a FAILED uninstall left DD
# half-present — its WSL/VirtualMachinePlatform entanglement is the prime
# suspect for the spurious DISM rc=3010 that skipped the image pull (the v2.13.0
# incident). If DD survives, warn loudly, log, and do NOT write the marker so
# the next install retries. Non-fatal: EduBotics runs on its own WSL distro, so
# a leftover Docker Desktop doesn't block the install.
Write-Step "Verifying Docker Desktop removal..."
$stillPresent = $false
foreach ($c in @(
    "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
    "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe"
)) {
    if (Test-Path $c) { $stillPresent = $true; break }
}
if (-not $stillPresent -and (Get-DockerDesktopUninstallEntry)) { $stillPresent = $true }

if ($stillPresent) {
    Write-Warn "Docker Desktop is STILL present after the uninstall attempt."
    Write-Host "   EduBotics uses its own WSL2 distro and will still work, but a" -ForegroundColor Yellow
    Write-Host "   leftover Docker Desktop can interfere with WSL/VirtualMachinePlatform." -ForegroundColor Yellow
    Write-Host "   Please remove Docker Desktop manually (Settings > Apps) and reboot." -ForegroundColor Yellow
    Write-Diag "verify" "Docker Desktop STILL PRESENT after uninstall (rc=$uninstallRc). Marker NOT written; migration will retry on the next install."
    exit 0
}

# Docker Desktop's removal only completes on the next boot (rc=3010 above). Its
# files may already look gone, but the marker means "this machine is migrated,
# never run me again" — writing it now would skip the post-reboot re-verify.
# The reboot flag written above has already stopped the import + pull.
if ($rebootPending) {
    Write-Step "NEUSTART ERFORDERLICH: Docker Desktop wird beim Neustart vollständig entfernt."
    Write-Host "   Bitte den PC neu starten. Die EduBotics-Umgebung wird danach" -ForegroundColor Yellow
    Write-Host "   automatisch eingerichtet, wenn Sie EduBotics öffnen." -ForegroundColor Yellow
    Write-Diag "defer_reboot" "Uninstaller returned 3010; marker withheld so the next run re-verifies the removal."
    exit 0
}

Set-Content -Path $MigratedFlag -Value "1"
Write-Diag "complete" "Docker Desktop removed (uninstall rc=$uninstallRc); durable marker written."
Write-Step "Migration complete. EduBotics will now use its own WSL2 distro."
