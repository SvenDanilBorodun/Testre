# import_edubotics_wsl.ps1 — Import the bundled EduBotics WSL2 distro
#
# Runs after install_prerequisites.ps1 (WSL2 is guaranteed available) and after
# migrate_from_docker_desktop.ps1 (no Docker Desktop competing for resources).
# Reads wsl_rootfs/edubotics-rootfs.tar.gz shipped inside {app}, imports it as
# a WSL2 distro named "EduBotics", and waits for dockerd to come up.
#
# Safe to re-run: on upgrades, the existing distro is unregistered first so the
# fresh rootfs replaces it (named volumes live inside the distro, so this is a
# one-time re-pull on upgrade — documented in release notes).
#
# Must run elevated (as Administrator).

param(
    [string]$DistroName   = "EduBotics",
    [string]$InstallRoot  = "$env:ProgramData\EduBotics\wsl",
    [string]$RootfsPath   = ""  # resolved below if empty
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-FAIL { param([string]$msg) Write-Host "   FAIL: $msg" -ForegroundColor Red }

# Bail if prerequisites phase still needs a reboot (WSL2 not fully up yet)
$rebootFlag = Join-Path $PSScriptRoot ".reboot_required"
if (Test-Path $rebootFlag) {
    Write-Warn "Reboot pending from WSL2 install — deferring EduBotics import until next launch."
    exit 0
}

# Resolve rootfs path. Production: {app}\wsl_rootfs\edubotics-rootfs.tar.gz
# (shipped by the Inno Setup [Files] section). Dev tree: both the build script's
# output directory (installer\assets\) and the production-style sibling work.
if (-not $RootfsPath) {
    $appRoot = Split-Path -Parent $PSScriptRoot
    $candidates = @(
        (Join-Path $appRoot "wsl_rootfs\edubotics-rootfs.tar.gz"),  # production
        (Join-Path $appRoot "assets\edubotics-rootfs.tar.gz"),       # dev (next to this script)
        (Join-Path (Split-Path -Parent $appRoot) "installer\assets\edubotics-rootfs.tar.gz")  # dev (one-up)
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $RootfsPath = $c; break }
    }
    if (-not $RootfsPath) { $RootfsPath = $candidates[0] }  # for the error message below
}

Write-Step "Checking rootfs archive..."
if (-not (Test-Path $RootfsPath)) {
    Write-FAIL "Rootfs nicht gefunden: $RootfsPath"
    Write-Host "   Der Installer ist unvollständig. Bitte neu herunterladen." -ForegroundColor Red
    exit 1
}
$sizeMB = [math]::Round((Get-Item $RootfsPath).Length / 1MB, 1)
Write-OK "Rootfs: $RootfsPath ($sizeMB MB)"

# Unregister any existing EduBotics distro (upgrade path)
Write-Step "Checking for existing EduBotics distro..."
$existing = $false
try {
    $listed = wsl --list --quiet 2>&1
    foreach ($line in $listed) {
        if (($line -replace "`0", "").Trim() -eq $DistroName) {
            $existing = $true
            break
        }
    }
} catch { }

if ($existing) {
    Write-Host "   Unregistering existing $DistroName (upgrade)..." -ForegroundColor White
    wsl --unregister $DistroName *>$null
    if ($LASTEXITCODE -ne 0) {
        Write-FAIL "Konnte existierenden Distro nicht entfernen."
        exit 1
    }
    Write-OK "Existing distro removed"
}

# Ensure install root exists
if (-not (Test-Path $InstallRoot)) {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
}

Write-Step "Importing $DistroName (can take 1-3 minutes)..."
wsl --import $DistroName $InstallRoot $RootfsPath --version 2
if ($LASTEXITCODE -ne 0) {
    Write-FAIL "wsl --import fehlgeschlagen (exit $LASTEXITCODE)"
    Write-Host "   Prüfen Sie: Antivirus-Ausnahme, genug Speicherplatz, WSL2 aktiviert." -ForegroundColor Red
    exit 1
}
Write-OK "Distro imported"

# Boot the distro (triggers systemd via wsl.conf) and wait for dockerd
Write-Step "Starting EduBotics-Umgebung..."
# First invocation starts the VM; echo is just a ping to force startup.
wsl -d $DistroName -- echo ready *>$null

# Poll for docker info — systemd needs a few seconds to bring docker up
$maxWait = 60
$elapsed = 0
$dockerReady = $false
while ($elapsed -lt $maxWait) {
    wsl -d $DistroName -- docker info *>$null 2>&1
    if ($LASTEXITCODE -eq 0) {
        $dockerReady = $true
        break
    }
    Start-Sleep -Seconds 2
    $elapsed += 2
    Write-Host "   Warte auf Docker-Engine... ${elapsed}s/${maxWait}s" -ForegroundColor Gray
}

if (-not $dockerReady) {
    # Boot-time autostart didn't fire — invoke the dockerd wrapper directly
    Write-Warn "dockerd nicht automatisch gestartet — Wrapper wird manuell ausgeführt"
    wsl -d $DistroName -- /usr/local/bin/start-dockerd.sh *>$null
    Start-Sleep -Seconds 3
    wsl -d $DistroName -- docker info *>$null 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-FAIL "Docker-Engine konnte nicht gestartet werden."
        Write-Host "   Diagnose: wsl -d $DistroName -- tail -n 50 /var/log/dockerd.log" -ForegroundColor Red
        exit 1
    }
}

Write-OK "Docker-Engine läuft in $DistroName"

# Sanity: show docker version inside the distro so install logs are useful
try {
    $ver = wsl -d $DistroName -- docker --version 2>&1
    Write-Host "   $ver" -ForegroundColor Gray
} catch { }

Write-Step "EduBotics-Umgebung bereit."
