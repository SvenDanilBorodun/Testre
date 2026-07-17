# finalize_install.ps1 — Complete the install after a reboot
#
# When WSL2 is installed fresh, a reboot is required before the EduBotics
# distro can be imported. After that reboot, this script finishes the job:
#   1. Import the bundled rootfs → EduBotics WSL2 distro
#   2. Pull the 3 Docker images into the distro
#   3. Verify everything
#
# Called either from the GUI (with UAC elevation via ShellExecuteEx) or
# manually by re-running the Inno Setup installer.

param(
    [string]$LogPath    = (Join-Path ([System.IO.Path]::GetTempPath()) 'edubotics_finalize.log'),
    [string]$MarkerPath = (Join-Path ([System.IO.Path]::GetTempPath()) 'edubotics_finalize.marker'),
    [string]$DistroName = "EduBotics"
)

$ErrorActionPreference = "Continue"

# Shared dockerd-readiness helper (dot-sourced; caller must keep EAP=Continue).
. (Join-Path $PSScriptRoot 'wsl_docker_ready.ps1')

# ── Marker: Proves the script actually started and survived long enough to
# execute ANY code. If this file exists, we know PowerShell launched and
# reached this point (UAC worked, script path was parseable, no syntax error).
# ───────────────────────────────────────────────────────────────────────────
try {
    $markerDir = Split-Path -Parent $MarkerPath
    if ($markerDir -and -not (Test-Path $markerDir)) {
        New-Item -ItemType Directory -Path $markerDir -Force | Out-Null
    }
    Set-Content -Path $MarkerPath -Value ("started {0} pid={1} user={2}" -f (Get-Date).ToString("o"), $PID, $env:USERNAME) -Force
} catch { }

# ── Transcript: Captures all stdout/stderr to $LogPath so the GUI can show
# what actually happened inside the elevated child (we cannot use
# -RedirectStandardOutput with -Verb RunAs on Start-Process).
# ───────────────────────────────────────────────────────────────────────────
$transcriptActive = $false
try {
    if (Test-Path $LogPath) { Remove-Item $LogPath -Force -ErrorAction SilentlyContinue }
    Start-Transcript -Path $LogPath -Force -IncludeInvocationHeader | Out-Null
    $transcriptActive = $true
} catch {
    # Silent: the GUI will detect an empty transcript + report exit code.
}

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" }
function Write-FAIL { param([string]$msg) Write-Host "   FAIL: $msg" }
function Write-Warn { param([string]$msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }

# Verified-STATE checks. The orchestrator trusts observed state, not child
# $LASTEXITCODE — a cosmetic non-zero from an import child must NOT skip the
# image pull (the compounding root cause), and a thrown terminating error in a
# child must become a warning, not an abort.

# True iff the WSL distro is registered.
function Test-DistroRegistered {
    param([string]$DistroName)
    try {
        $out = wsl --list --quiet 2>&1
        foreach ($line in $out) {
            if ((($line -replace "`0", "").Trim()) -eq $DistroName) { return $true }
        }
    } catch { }
    return $false
}

# True iff all three EduBotics images are present inside the distro. Resolves
# $Registry/$ImageTag exactly like pull_images.ps1 (docker/versions.env, with
# the same installed-layout + dev-tree fallback and the same defaults).
function Test-ImagesPresent {
    param([string]$DistroName)
    $Registry = "ghcr.io/svendanilborodun"
    $ImageTag = "latest"
    $AppRoot = Split-Path -Parent $PSScriptRoot
    $VersionsEnv = Join-Path $AppRoot "docker\versions.env"
    if (-not (Test-Path $VersionsEnv)) {
        $VersionsEnv = Join-Path $PSScriptRoot "..\..\docker\versions.env"
    }
    if (Test-Path $VersionsEnv) {
        Get-Content $VersionsEnv | ForEach-Object {
            if ($_ -match '^\s*REGISTRY\s*=\s*(.+?)\s*$')  { $Registry = $Matches[1] }
            if ($_ -match '^\s*IMAGE_TAG\s*=\s*(.+?)\s*$') { $ImageTag = $Matches[1] }
        }
    }
    foreach ($name in @("open-manipulator", "physical-ai-server", "physical-ai-manager")) {
        & wsl -d $DistroName -- docker image inspect "${Registry}/${name}:${ImageTag}" *>$null 2>&1
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    return $true
}

# Fail the run with a German next-action for the student, record it in the
# marker, and exit non-zero.
function Fail-WithNextAction {
    param([string]$Problem, [string]$NextStep)
    Write-FAIL $Problem
    Write-Host "   Nächster Schritt: $NextStep" -ForegroundColor Yellow
    Write-Host "   Protokoll: $LogPath"
    try {
        Set-Content -LiteralPath $MarkerPath -Value ("FAILED {0}`n{1}`n{2}" -f (Get-Date).ToString("o"), $Problem, $NextStep) -Force
    } catch { }
    exit 1
}

try {
    Write-Step "EduBotics-Einrichtung läuft..."
    Write-Host "   Script:  $PSCommandPath"
    Write-Host "   Scripts-Verzeichnis: $PSScriptRoot"
    Write-Host "   LogPath: $LogPath"
    Write-Host "   Elevated: $([bool]([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"

    # Phase 0: Make sure WSL2 + usbipd are actually installed before we try to
    # import a distro. The GUI calls finalize when the EduBotics distro is
    # missing, but that can also mean WSL itself never installed (a failed or
    # skipped prerequisites step, or a machine where `wsl --install` needed a
    # reboot that never happened). Importing into a non-existent WSL would fail
    # with a cryptic error — run the prerequisites first so this one button
    # fixes both cases.
    $wslOk = $false
    try {
        wsl --status *>$null
        if ($LASTEXITCODE -eq 0) { $wslOk = $true }
    } catch { }
    if (-not $wslOk) {
        Write-Step "Schritt 0/2: WSL2/usbipd werden installiert..."
        & (Join-Path $PSScriptRoot "install_prerequisites.ps1")
        $prereqRc = $LASTEXITCODE
        # install_prerequisites writes .reboot_required when a fresh WSL2
        # install needs a host reboot before a distro can be imported.
        $rebootFlagAfter = Join-Path $PSScriptRoot ".reboot_required"
        if (Test-Path $rebootFlagAfter) {
            Write-Step "NEUSTART ERFORDERLICH: Bitte den PC neu starten und EduBotics erneut öffnen."
            exit 0
        }
        if ($prereqRc -ne 0) {
            Write-FAIL "Voraussetzungen konnten nicht installiert werden (exit $prereqRc)."
            exit 1
        }
        Write-OK "Voraussetzungen installiert"
    }

    # Clear the reboot flag — prerequisites are in place now (post-reboot).
    $flagPath = Join-Path $PSScriptRoot ".reboot_required"
    if (Test-Path $flagPath) { Remove-Item $flagPath -Force }

    # Phase 1: Import the distro. Gate on VERIFIED STATE, not the child's
    # $LASTEXITCODE — a cosmetic non-zero (or a thrown terminating error) from
    # the import child must not abort or skip Phase 2. We check the distro is
    # actually registered and dockerd is actually up instead.
    Write-Step "Schritt 1/2: EduBotics-Umgebung wird eingerichtet..."
    try { & (Join-Path $PSScriptRoot "import_edubotics_wsl.ps1") } catch { Write-Warn "Import meldete einen Fehler, prüfe tatsächlichen Zustand: $_" }
    if (-not (Test-DistroRegistered $DistroName)) {
        Fail-WithNextAction "Die EduBotics-Umgebung wurde nicht eingerichtet." "Bitte den PC neu starten und EduBotics erneut öffnen."
    }
    if (-not (Wait-DockerReady -DistroName $DistroName -MaxWaitSeconds 120)) {
        Fail-WithNextAction "Die Docker-Engine ist nicht gestartet." "Diagnose: wsl -d $DistroName -- tail -n 50 /var/log/dockerd.log"
    }
    Write-OK "EduBotics-Umgebung eingerichtet (Zustand verifiziert)"

    # Phase 2: Provide images — idempotent + state-gated. Skip if the images are
    # already present (a re-run after a partial finalize keeps what it pulled);
    # otherwise pull, then verify the images actually landed rather than trusting
    # the child exit code.
    if (Test-ImagesPresent $DistroName) {
        Write-OK "Images bereits vorhanden - Schritt 2 übersprungen."
    } else {
        Write-Step "Schritt 2/2: Docker-Images werden bereitgestellt..."
        try { & (Join-Path $PSScriptRoot "pull_images.ps1") } catch { Write-Warn "Pull-Skript meldete: $_" }
        if (-not (Test-ImagesPresent $DistroName)) {
            Fail-WithNextAction "Images konnten nicht bereitgestellt werden." "Internetverbindung prüfen und EduBotics erneut öffnen - bereits geladene Images bleiben erhalten."
        }
    }
    Write-OK "Images bereitgestellt (Zustand verifiziert)"

    Write-Step "Fertig! Sie können EduBotics jetzt nutzen."
    exit 0
} finally {
    if ($transcriptActive) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}
