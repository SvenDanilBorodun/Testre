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

# Disk-space preflight. `wsl --import` is not atomic: if it runs out of
# space mid-copy, it leaves a corrupt VHDX and a subsequent re-run tries
# to import over broken state. 20 GB gives dockerd + the 3 images + some
# working room.
try {
    $drive = ([System.IO.DirectoryInfo]$InstallRoot).Root.FullName.TrimEnd('\').TrimEnd(':')
    $vol = Get-Volume -DriveLetter $drive -ErrorAction Stop
    $freeGb = [math]::Round($vol.SizeRemaining / 1GB, 1)
    if ($freeGb -lt 20) {
        Write-FAIL "Nicht genug Speicher auf Laufwerk $drive`: ${freeGb} GB frei, 20 GB werden benoetigt."
        Write-Host "   Bitte Speicher freigeben und Installation erneut starten." -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "   (Speicherplatz-Pruefung uebersprungen: $_)" -ForegroundColor Yellow
}

# SHA256 integrity check on the rootfs tar. If a matching .sha256 file
# ships alongside, verify it before wasting 1-3 minutes on `wsl --import`
# of a corrupted/swapped tarball.
$sha256File = "$RootfsPath.sha256"
if (Test-Path $sha256File) {
    try {
        $expectedLine = (Get-Content $sha256File -First 1).Trim()
        $expected = ($expectedLine -split '\s+')[0].ToUpper()
        $actual = (Get-FileHash -Path $RootfsPath -Algorithm SHA256).Hash.ToUpper()
        if ($expected -ne $actual) {
            Write-FAIL "Rootfs SHA256 passt nicht: expected=$expected actual=$actual"
            Write-Host "   Die Installer-Datei koennte beschaedigt oder manipuliert sein." -ForegroundColor Red
            exit 1
        }
        Write-OK "Rootfs SHA256 verified"
    } catch {
        Write-Host "   (SHA256-Pruefung fehlgeschlagen: $_)" -ForegroundColor Yellow
    }
}

wsl --import $DistroName $InstallRoot $RootfsPath --version 2
if ($LASTEXITCODE -ne 0) {
    Write-FAIL "wsl --import fehlgeschlagen (exit $LASTEXITCODE)"
    Write-Host "   Prüfen Sie: Antivirus-Ausnahme, genug Speicherplatz, WSL2 aktiviert." -ForegroundColor Red
    # Clean up partial VHDX to prevent "import over broken state" on retry.
    if (Test-Path $InstallRoot) {
        try { Remove-Item -Path (Join-Path $InstallRoot 'ext4.vhdx') -Force -ErrorAction SilentlyContinue } catch {}
    }
    exit 1
}
Write-OK "Distro imported"

# Boot the distro (triggers systemd via wsl.conf) and wait for dockerd
Write-Step "Starting EduBotics-Umgebung..."
# First invocation starts the VM; echo is just a ping to force startup.
wsl -d $DistroName -- echo ready *>$null

# Poll for docker info — dockerd takes a few seconds to bring up even on
# fast hardware. 60s was tight on 5400 RPM HDDs and when Controlled
# Folder Access added latency; 180s comfortably covers first-boot rootfs
# extraction + dockerd start.
$maxWait = 180
$elapsed = 0
$dockerReady = $false
$lastErr = ""
while ($elapsed -lt $maxWait) {
    # Capture stderr alongside exit code so the operator can see why docker
    # info failed (previously silently swallowed into $null).
    $lastErr = (wsl -d $DistroName -- docker info 2>&1 | Out-String)
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
    Write-Host "   Last docker-info stderr:" -ForegroundColor Gray
    Write-Host "   $lastErr" -ForegroundColor Gray
    wsl -d $DistroName -- /usr/local/bin/start-dockerd.sh *>$null
    Start-Sleep -Seconds 3
    $lastErr = (wsl -d $DistroName -- docker info 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        Write-FAIL "Docker-Engine konnte nicht gestartet werden."
        Write-Host "   Fehler: $lastErr" -ForegroundColor Red
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
