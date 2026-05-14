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

$MigratedFlag = Join-Path $PSScriptRoot ".migrated"

if (Test-Path $MigratedFlag) {
    Write-Skip "Migration already completed on this machine."
    exit 0
}

Write-Step "Detecting Docker Desktop..."

$dockerDesktopExe = $null
$candidates = @(
    "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
    "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe"
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $dockerDesktopExe = $c; break }
}

$dockerDesktopInstaller = $null
$installerCandidates = @(
    "$env:ProgramFiles\Docker\Docker\Docker Desktop Installer.exe",
    "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop Installer.exe"
)
foreach ($c in $installerCandidates) {
    if (Test-Path $c) { $dockerDesktopInstaller = $c; break }
}

if (-not $dockerDesktopExe -and -not $dockerDesktopInstaller) {
    Write-Skip "Docker Desktop not installed — nothing to migrate."
    Set-Content -Path $MigratedFlag -Value "1"
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

# 2. Silent-uninstall Docker Desktop
Write-Step "Uninstalling Docker Desktop..."
if ($dockerDesktopInstaller) {
    try {
        $proc = Start-Process -FilePath $dockerDesktopInstaller `
                              -ArgumentList "uninstall", "--quiet" `
                              -Wait -PassThru -WindowStyle Hidden
        if ($proc.ExitCode -eq 0) {
            Write-OK "Docker Desktop uninstalled"
        } else {
            Write-Warn "Uninstaller exited with code $($proc.ExitCode) — continuing"
        }
    } catch {
        Write-Warn "Uninstaller invocation failed: $_"
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

Set-Content -Path $MigratedFlag -Value "1"
Write-Step "Migration complete. EduBotics will now use its own WSL2 distro."
