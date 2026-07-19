# pull_images.ps1 — Pull Docker images into the EduBotics WSL2 distro
#
# Routes every docker command through `wsl -d EduBotics -- docker ...` so we
# never depend on the host having Docker Desktop. The distro ships its own
# headless Docker Engine, imported by import_edubotics_wsl.ps1.

param(
    [string]$Registry         = "ghcr.io/svendanilborodun",
    [string]$RegistryFallback = "nettername",
    [string]$DistroName       = "EduBotics"
)

# EAP=Continue, NOT Stop (load-bearing — mirrors verify_system.ps1). In Windows
# PowerShell 5.1 a native command (wsl/docker) writing to stderr is promoted to a
# TERMINATING NativeCommandError under EAP=Stop — for every redirection form, and
# even on success (`docker info` prints a swap-limit warning to stderr; `docker pull`
# streams its layer progress to stderr). Under Stop the online pull threw on the first
# image. Native outcomes are checked via $LASTEXITCODE; do NOT revert to Stop without
# wrapping every wsl/docker call in try/catch.
$ErrorActionPreference = "Continue"

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }

# Read IMAGE_TAG from docker/versions.env so we pull the SAME bytes that
# docker-compose.yml will run later. Falls back to :latest if the file is
# missing (e.g. dev install before any maintainer build has shipped).
#
# Path resolution: in the installed layout the script lives at
# {app}\scripts\pull_images.ps1 and versions.env at {app}\docker\versions.env,
# so we resolve via the parent of $PSScriptRoot (= {app}), not via "..\.."
# which would walk above {app}. The previous "..\.." form only worked from
# the dev tree (robotis_ai_setup/installer/scripts/), so production installs
# silently always fell back to :latest.
$ImageTag = "latest"
$AppRoot = Split-Path -Parent $PSScriptRoot
$VersionsEnv = Join-Path $AppRoot "docker\versions.env"
# Dev-tree fallback: the dev layout has scripts at robotis_ai_setup/installer/
# scripts/ and versions.env at robotis_ai_setup/docker/versions.env — two
# levels up. Try the dev path if the installed path is missing.
if (-not (Test-Path $VersionsEnv)) {
    $VersionsEnv = Join-Path $PSScriptRoot "..\..\docker\versions.env"
}
if (Test-Path $VersionsEnv) {
    Get-Content $VersionsEnv | ForEach-Object {
        if ($_ -match '^\s*IMAGE_TAG\s*=\s*(.+?)\s*$') { $ImageTag = $Matches[1] }
        if ($_ -match '^\s*REGISTRY\s*=\s*(.+?)\s*$' -and -not $PSBoundParameters.ContainsKey('Registry')) {
            $Registry = $Matches[1]
        }
        if ($_ -match '^\s*REGISTRY_FALLBACK\s*=\s*(.+?)\s*$' -and -not $PSBoundParameters.ContainsKey('RegistryFallback')) {
            $RegistryFallback = $Matches[1]
        }
    }
}
Write-Host "Using image tag: $ImageTag (registry: $Registry, fallback: $RegistryFallback, distro: $DistroName)" -ForegroundColor Cyan

# Image SHORT names; the primary ref is ${Registry}/<name>:<tag>, the fallback
# (Docker Hub twin, dual-pushed → digest-identical) is ${RegistryFallback}/<name>:<tag>.
$repoNames = @("open-manipulator", "physical-ai-server", "physical-ai-manager")

Write-Step "Pulling Docker images into $DistroName..."

# Check the distro exists and docker runs inside it
$listed = $false
try {
    $out = wsl --list --quiet 2>&1
    foreach ($line in $out) {
        if (($line -replace "`0", "").Trim() -eq $DistroName) { $listed = $true; break }
    }
} catch { }
if (-not $listed) {
    Write-Host "ERROR: WSL2 distro '$DistroName' not found. Run import_edubotics_wsl.ps1 first." -ForegroundColor Red
    exit 1
}

# Ensure dockerd is up before pulling (F3: single-shot `docker info` raced the
# cold-start on a freshly-imported distro). Wait-DockerReady triggers the VM
# boot, polls by exit code, and runs the start-dockerd.sh wrapper as a fallback.
# This distro uses start-dockerd.sh (the wsl.conf [boot] command), NOT systemd,
# so the old "systemctl status docker" hint was wrong.
. (Join-Path $PSScriptRoot 'wsl_docker_ready.ps1')
if (-not (Wait-DockerReady -DistroName $DistroName)) {
    Write-Host "ERROR: Docker-Engine in $DistroName konnte nicht gestartet werden. Diagnose: wsl -d $DistroName -- tail -n 50 /var/log/dockerd.log" -ForegroundColor Red
    exit 1
}

$total = $repoNames.Count
$current = 0
foreach ($name in $repoNames) {
    $current++
    $primary = "${Registry}/${name}:${ImageTag}"
    Write-Host "`n   [$current/$total] Pulling $primary ..." -ForegroundColor White
    # Retry the PRIMARY (GHCR) a few times before falling back. GHCR/Fastly
    # throws transient 502/503 Bad-Gateway errors mid-pull (observed on a German
    # classroom rig); an immediate Docker Hub fallback on a single blip would
    # stampede 30 PCs onto rate-limited Docker Hub (the 100/6h limit this whole
    # migration exists to escape). The GUI (_pull_one_image) and the Jetson agent
    # already retry; match that here. 5s/10s backoff between attempts.
    $pulled = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        wsl -d $DistroName -- docker pull $primary
        if ($LASTEXITCODE -eq 0) { $pulled = $true; break }
        if ($attempt -lt 3) {
            Write-Host "   GHCR pull failed (attempt $attempt/3), retrying..." -ForegroundColor Yellow
            Start-Sleep -Seconds (5 * $attempt)
        }
    }
    if ($pulled) {
        Write-OK "$primary pulled"
        continue
    }
    # All GHCR attempts failed — fall back to the digest-identical Docker Hub twin
    # and re-tag it to the primary name so docker-compose's ${REGISTRY} ref
    # resolves locally. Drop the redundant fallback tag afterwards (layers stay,
    # kept by the primary tag). Only hard-fail if BOTH registries fail.
    if ($RegistryFallback -and $RegistryFallback -ne $Registry) {
        $fallback = "${RegistryFallback}/${name}:${ImageTag}"
        Write-Host "   GHCR unavailable, falling back to Docker Hub: $fallback ..." -ForegroundColor Yellow
        wsl -d $DistroName -- docker pull $fallback
        if ($LASTEXITCODE -eq 0) {
            wsl -d $DistroName -- docker tag $fallback $primary
            if ($LASTEXITCODE -eq 0) {
                wsl -d $DistroName -- docker image rm $fallback *>$null 2>&1
                Write-OK "$primary pulled (via Docker Hub fallback)"
                continue
            }
        }
    }
    Write-Host "ERROR: Failed to pull $primary (GHCR and Docker Hub fallback)" -ForegroundColor Red
    exit 1
}

# Remove SUPERSEDED tags by enumeration. After a self-update reinstall pulls
# :X.Y.Z, the previously-installed :X.Y-1 image still carries its tag — and
# `docker image prune -f` is dangling-ONLY, so it never reclaims a tagged
# image. Over a few classroom updates the VHDX would otherwise grow by a full
# image per past version. We can't decrement the tag (2.7.0->2.8.0 isn't
# "X.Y-1"; patch bumps make it meaningless), so enumerate the local tags per
# repo and remove every one that isn't the current $ImageTag. Untag-by-tag is
# layer-safe: only layers NOT shared with the kept :$ImageTag are dropped.
$repos = $repoNames | ForEach-Object { "${Registry}/$_" }
Write-Step "Removing superseded image tags..."
foreach ($repo in $repos) {
    $tags = wsl -d $DistroName -- docker images $repo --format '{{.Tag}}' 2>$null
    foreach ($tag in $tags) {
        $tag = ($tag -replace "`0", "").Trim()
        if ($tag -and $tag -ne '<none>' -and $tag -ne $ImageTag) {
            wsl -d $DistroName -- docker image rm "${repo}:${tag}" *>$null 2>&1
        }
    }
}

Write-Step "Cleaning up old images..."
wsl -d $DistroName -- docker image prune -f *>$null
Write-OK "Old images removed"

Write-Step "All $total images pulled successfully!"

# Explicit success: don't let the script's exit code fall through to the prune's
# $LASTEXITCODE. A transient non-zero prune would otherwise make the finalize
# (post-reboot) caller falsely report a failed image step (finalize_install.ps1
# and the .iss Step 5 both propagate/check $LASTEXITCODE).
exit 0
