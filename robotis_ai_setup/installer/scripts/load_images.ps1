# load_images.ps1 — Provide the Docker images, OFFLINE bundle or online pull
#
# Dispatcher used at install Step 5 AND post-reboot by finalize_install.ps1:
#
#   * BUNDLE MODE  — when $BundleDir holds the offline bundle (a
#     bundled_digests.json marker + images\<repo>.tar.gz produced by the P3 CI
#     job and relocated here by stage_bundle.ps1): verify each tarball's
#     SHA256, `docker load` it (stdin pipe, no DrvFs re-seek), assert the image
#     is present, delete the tarball, then seed bundled_digests.json next to
#     versions.env so the GUI recognises the docker-load'd images as current
#     (moby#22011 leaves RepoDigests empty → see gui/app/docker_manager.py).
#     Zero Docker Hub traffic. Idempotent + partial-safe: a re-run skips images
#     already present and loads only what's missing; the bundle dir is removed
#     ONLY once all 3 images are confirmed present.
#
#   * ONLINE MODE  — no bundle marker (the small online / self-update .exe):
#     delegate verbatim to pull_images.ps1 (today's Docker Hub path).
#
# A bundle that is present-but-broken NEVER silently falls back to a Hub pull —
# it fails LOUD in German, because the offline promise must not degrade quietly.

param(
    [string]$BundleDir  = (Join-Path $env:ProgramData "EduBotics\bundle"),
    [string]$Registry   = "nettername",
    [string]$DistroName = "EduBotics"
)

# EAP=Continue, NOT Stop (load-bearing — mirrors verify_system.ps1). In Windows
# PowerShell 5.1 a native command (wsl/docker) writing to stderr is promoted to a
# TERMINATING NativeCommandError under EAP=Stop — for every redirection form, and
# even on success (`docker info` prints a swap-limit warning to stderr on a healthy
# daemon; `docker image inspect` of a not-yet-loaded image prints "No such image").
# Under Stop the offline `docker load` gates threw before loading anything. Native
# outcomes are checked via $LASTEXITCODE; do NOT revert to Stop without wrapping
# every wsl/docker call in try/catch.
$ErrorActionPreference = "Continue"

function Write-Step { param([string]$m) Write-Host "`n>> $m" -ForegroundColor Cyan }
function Write-OK   { param([string]$m) Write-Host "   OK: $m" -ForegroundColor Green }

function ConvertTo-WslPath([string]$p) {
    $n = $p -replace '\\', '/'
    if ($n -match '^([A-Za-z]):/(.*)$') {
        return "/mnt/$($Matches[1].ToLower())/$($Matches[2])"
    }
    return $n
}

# ── Online dispatch: no bundle marker → exactly today's Docker Hub pull ───────
$marker = if ($BundleDir) { Join-Path $BundleDir "bundled_digests.json" } else { $null }
if (-not $marker -or -not (Test-Path -LiteralPath $marker)) {
    Write-Host "Kein Offline-Paket vorhanden — Images werden aus dem Netz geladen."
    $fwd = @{}
    if ($PSBoundParameters.ContainsKey('Registry'))   { $fwd['Registry']   = $Registry }
    if ($PSBoundParameters.ContainsKey('DistroName')) { $fwd['DistroName'] = $DistroName }
    & (Join-Path $PSScriptRoot "pull_images.ps1") @fwd
    exit $LASTEXITCODE
}

# ── Bundle mode ───────────────────────────────────────────────────────────────

# Resolve IMAGE_TAG from versions.env (same walk pull_images.ps1 uses) so the
# loaded tag matches what docker-compose + the GUI reference.
$ImageTag = "latest"
$AppRoot = Split-Path -Parent $PSScriptRoot
$VersionsEnv = Join-Path $AppRoot "docker\versions.env"
if (-not (Test-Path $VersionsEnv)) {
    $VersionsEnv = Join-Path $PSScriptRoot "..\..\docker\versions.env"
}
if (Test-Path $VersionsEnv) {
    Get-Content $VersionsEnv | ForEach-Object {
        if ($_ -match '^\s*IMAGE_TAG\s*=\s*(.+?)\s*$') { $ImageTag = $Matches[1] }
        if ($_ -match '^\s*REGISTRY\s*=\s*(.+?)\s*$' -and -not $PSBoundParameters.ContainsKey('Registry')) {
            $Registry = $Matches[1]
        }
    }
}

# Stale-bundle guard: a leftover bundle from a PRIOR aborted install of a
# DIFFERENT version keeps a marker keyed to the OLD tag. A later self-update
# (small .exe, no new bundle → stage_bundle.ps1 leaves %ProgramData%\bundle
# untouched) would otherwise enter bundle mode with the NEW IMAGE_TAG, find no
# matching tarballs and hard-fail — never reaching the Docker Hub fallback. So
# if the staged bundle isn't for the CURRENT $ImageTag, drop it and load online.
# (A same-tag leftover is the legitimate partial-load retry and proceeds below.)
$bundleMatchesTag = $false
try {
    $bd = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    foreach ($key in $bd.PSObject.Properties.Name) {
        if ($key -match (":" + [regex]::Escape($ImageTag) + "$")) { $bundleMatchesTag = $true; break }
    }
} catch { }
if (-not $bundleMatchesTag) {
    Write-Host "Hinweis: Vorhandenes Offline-Paket passt nicht zur aktuellen Version ($ImageTag) — wird entfernt, Images werden aus dem Netz geladen."
    Remove-Item -LiteralPath $BundleDir -Recurse -Force -ErrorAction SilentlyContinue
    $fwd = @{}
    if ($PSBoundParameters.ContainsKey('Registry'))   { $fwd['Registry']   = $Registry }
    if ($PSBoundParameters.ContainsKey('DistroName')) { $fwd['DistroName'] = $DistroName }
    & (Join-Path $PSScriptRoot "pull_images.ps1") @fwd
    exit $LASTEXITCODE
}

$repos = @("open-manipulator", "physical-ai-server", "physical-ai-manager")
$imagesDir = Join-Path $BundleDir "images"

Write-Step "Offline-Paket wird geladen (Tag: $ImageTag, Registry: $Registry)..."

# Distro + dockerd must be up (mirrors pull_images.ps1).
$listed = $false
try {
    foreach ($line in (wsl --list --quiet 2>&1)) {
        if (($line -replace "`0", "").Trim() -eq $DistroName) { $listed = $true; break }
    }
} catch { }
if (-not $listed) {
    Write-Host "[FEHLER] WSL2-Distribution '$DistroName' nicht gefunden. Bitte zuerst die EduBotics-Umgebung einrichten." -ForegroundColor Red
    exit 1
}
wsl -d $DistroName -- docker info *>$null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FEHLER] Docker-Engine in '$DistroName' nicht bereit." -ForegroundColor Red
    exit 1
}

$total = $repos.Count
$current = 0
foreach ($repo in $repos) {
    $current++
    $image = "${Registry}/${repo}:${ImageTag}"

    # Idempotent: a re-run (e.g. after a partial failure) skips images already
    # loaded and only loads the missing ones.
    wsl -d $DistroName -- docker image inspect $image *>$null 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "[$current/$total] $repo bereits geladen — wird übersprungen"
        continue
    }

    $tar = Join-Path $imagesDir "${repo}.tar.gz"
    if (-not (Test-Path -LiteralPath $tar)) {
        Write-Host "[FEHLER] Offline-Paket unvollständig: '$repo' fehlt und ist nicht im Paket. Bitte EduBotics neu installieren." -ForegroundColor Red
        exit 1
    }

    # Integrity gate before load (mirrors the rootfs .sha256 gate). A truncated
    # download must fail fast, not corrupt the Docker store. The checksum is
    # MANDATORY: every CI bundle ships <repo>.tar.gz.sha256 inside images/, so a
    # missing one means a broken/tampered bundle — hard-fail rather than load
    # unverified bytes (defense-in-depth, not opt-in-by-file-presence).
    $shaFile = "${tar}.sha256"
    if (-not (Test-Path -LiteralPath $shaFile)) {
        Write-Host "[FEHLER] Prüfsumme für '$repo' fehlt im Paket — Installation abgebrochen. Bitte EduBotics neu herunterladen." -ForegroundColor Red
        exit 1
    }
    $expected = $null
    if ((Get-Content -LiteralPath $shaFile -Raw) -match '([0-9a-fA-F]{64})') {
        $expected = $Matches[1].ToLower()
    }
    $actual = (Get-FileHash -LiteralPath $tar -Algorithm SHA256).Hash.ToLower()
    if (-not $expected -or $expected -ne $actual) {
        Write-Host "[FEHLER] Prüfsumme von '$repo' stimmt nicht — Download beschädigt. Bitte erneut herunterladen." -ForegroundColor Red
        exit 1
    }

    Write-Host "`n   [$current/$total] $repo wird geladen ..." -ForegroundColor White
    $wslTar = ConvertTo-WslPath $tar
    wsl -d $DistroName -- bash -c "cat '$wslTar' | docker load"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FEHLER] '$repo' konnte nicht geladen werden." -ForegroundColor Red
        exit 1
    }

    # Confirm the expected tag actually materialised (don't trust load's exit
    # code alone — a tarball saved under the wrong tag would mismatch compose).
    wsl -d $DistroName -- docker image inspect $image *>$null 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FEHLER] '$image' nach dem Laden nicht vorhanden (falscher Tag im Paket?)." -ForegroundColor Red
        exit 1
    }
    Write-OK "$repo geladen"

    # Free the tarball immediately to bound peak disk during the load run.
    Remove-Item -LiteralPath $tar -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $shaFile -Force -ErrorAction SilentlyContinue
}

# Seed the digest sidecar next to versions.env (machine-wide, world-readable —
# NOT %LOCALAPPDATA%, which under this elevated installer is the admin's
# profile, not the student's). The GUI reads it via constants.BUNDLED_DIGESTS_FILE.
$srcDigests = Join-Path $BundleDir "bundled_digests.json"
$dstDigests = Join-Path $AppRoot "docker\bundled_digests.json"
try {
    $dstDir = Split-Path -Parent $dstDigests
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
    Copy-Item -LiteralPath $srcDigests -Destination $dstDigests -Force
    Write-OK "Image-Prüfsummen hinterlegt"
} catch {
    # Non-fatal: without the sidecar the first online launch re-pulls once, but
    # the images ARE loaded and usable offline now.
    Write-Host "   Hinweis: Image-Prüfsummen konnten nicht hinterlegt werden ($($_.Exception.Message))."
}

# All images confirmed present → drop the staged bundle to reclaim disk.
try {
    Remove-Item -LiteralPath $BundleDir -Recurse -Force -ErrorAction Stop
} catch {
    Write-Host "   Hinweis: Offline-Paket-Ordner konnte nicht entfernt werden ($($_.Exception.Message))."
}

wsl -d $DistroName -- docker image prune -f *>$null 2>&1

Write-Step "Offline-Paket erfolgreich geladen — alle $total Images bereit."
exit 0
