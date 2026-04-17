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
    [string]$LogPath    = "$env:TEMP\edubotics_finalize.log",
    [string]$MarkerPath = "$env:TEMP\edubotics_finalize.marker"
)

$ErrorActionPreference = "Continue"

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

try {
    Write-Step "EduBotics-Einrichtung läuft..."
    Write-Host "   Script:  $PSCommandPath"
    Write-Host "   Scripts-Verzeichnis: $PSScriptRoot"
    Write-Host "   LogPath: $LogPath"
    Write-Host "   Elevated: $([bool]([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"

    # Clear the reboot flag — prerequisites are in place now (post-reboot).
    $flagPath = Join-Path $PSScriptRoot ".reboot_required"
    if (Test-Path $flagPath) { Remove-Item $flagPath -Force }

    # Phase 1: Import the distro
    Write-Step "Schritt 1/2: EduBotics-Umgebung wird eingerichtet..."
    & (Join-Path $PSScriptRoot "import_edubotics_wsl.ps1")
    if ($LASTEXITCODE -ne 0) {
        Write-FAIL "Rootfs-Import fehlgeschlagen (exit $LASTEXITCODE)."
        exit 1
    }
    Write-OK "EduBotics-Umgebung eingerichtet"

    # Phase 2: Pull images
    Write-Step "Schritt 2/2: Docker-Images werden heruntergeladen..."
    & (Join-Path $PSScriptRoot "pull_images.ps1")
    if ($LASTEXITCODE -ne 0) {
        Write-FAIL "Image-Download fehlgeschlagen (exit $LASTEXITCODE). Internetverbindung prüfen."
        exit 1
    }
    Write-OK "Images heruntergeladen"

    Write-Step "Fertig! Sie können EduBotics jetzt nutzen."
    exit 0
} finally {
    if ($transcriptActive) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}
