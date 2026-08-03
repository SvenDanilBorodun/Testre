# preflight_system.ps1 — Pre-flight self-diagnosis (German) before the student
#                        hits an install/runtime failure.
#
# Runs five non-fatal checks and prints German OK/WARNUNG/FEHLER lines, mirroring
# verify_system.ps1's diagnostics-log style. Every line is also appended to
# %ProgramData%\EduBotics\logs\install_diagnostics.log so support has evidence
# even when the console is gone.
#
# Checks:
#   1. Temp path / dotted-username hazard (ground-truth write probe)
#   2. UAC / self-elevation feasibility
#   3. WSL2 availability
#   4. dockerd reachability (only if the EduBotics distro already exists)
#   5. Distro image on disk but NOT registered for THIS Windows account
#
# Always exits 0 — this is a diagnostic, never a gate. Pass -Quiet for GUI use
# (suppresses the console, still writes the log).

param(
    [switch]$Quiet
)

$ErrorActionPreference = "Continue"

# ── Diagnostics sink (matches verify_system.ps1 / configure_usbipd.ps1) ─────
# %ProgramData%, not %LOCALAPPDATA% — this was the LAST holdout, and the most
# damaging one: preflight is Step 0a, the FIRST diagnostic, and the one that
# captures the hostile-environment evidence (dotted-username temp path, UAC off,
# WSL2 missing). Writing it to the elevating ADMIN's profile while every other
# logger writes machine-wide split the support artifact in two — on a managed
# school PC the installing admin is not the student, so support reading the
# student's copy saw the whole install history EXCEPT the self-diagnosis that
# explains it. The `logs` LEAF and the scope ("this folder and files", never the
# subtree) are both load-bearing: %ProgramData%\EduBotics is the PARENT of the
# WSL install root and must keep its default admin-only inherited ACL, or a
# standard user can pre-create …\EduBotics\wsl and own the distro's ext4.vhdx.
# See install_prerequisites.ps1 for the full rationale. Keep all six copies of
# this block identical.
#
# try/catch is not decoration here: this script runs -Quiet from the .iss [Run]
# and from the GUI, where an unhandled Add-Content error record goes to a stream
# nobody reads. Logging must never be able to abort a diagnostic.
$DiagDir = Join-Path $env:ProgramData "EduBotics\logs"
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
        Add-Content -Path $DiagLog -Value "`n=== $ts preflight_system::$section ==="
        Add-Content -Path $DiagLog -Value $body
    } catch { }
}

# Emit one German status line to the console (unless -Quiet) AND to the log.
# Level is one of OK / WARNUNG / FEHLER / INFO. The [FEHLER]/[WARNUNG] tags are
# the ones german-strings-lint scans, so the messages use literal ä ö ü.
function Emit {
    param(
        [ValidateSet('OK', 'WARNUNG', 'FEHLER', 'INFO')][string]$Level,
        [string]$Message
    )
    $logLine = "[$Level] $Message"
    # try/catch: under -Quiet an unhandled Add-Content error record would go to a
    # stream nobody reads, and a locked/undeletable log must not silently swallow
    # the console half of the diagnosis too.
    try {
        Add-Content -Path $DiagLog -Value "$((Get-Date).ToString('o')) preflight_system:: $logLine"
    } catch { }
    if (-not $Quiet) {
        $color = switch ($Level) {
            'OK'      { 'Green' }
            'WARNUNG' { 'Yellow' }
            'FEHLER'  { 'Red' }
            default   { 'White' }
        }
        Write-Host "   $logLine" -ForegroundColor $color
    }
}

if (-not $Quiet) {
    Write-Host "`n>> EduBotics Selbstdiagnose..." -ForegroundColor Cyan
    Write-Host "   Diagnoseprotokoll: $DiagLog" -ForegroundColor Gray
}
Write-Diag "preflight_start" "Quiet=$Quiet USERNAME=$env:USERNAME"

# ── 1. Temp path / dotted-username hazard ──────────────────────────────────
# A username with a dot yields an 8.3 tilde temp path that was implicated in a
# terminating -LiteralPath binding crash (F2). Ground truth beats
# string-matching: actually write + delete a probe file in the temp dir, inside
# try/catch (the load-bearing guard). GetTempPath() is the consistent path
# source — note it reads the same TMP env var and does NOT expand an 8.3 path
# to its long form; the try/catch is what makes this probe crash-proof.
$tmp = [System.IO.Path]::GetTempPath()
$probe = Join-Path $tmp ("edubotics_probe_{0}.tmp" -f $PID)
$writable = $false
try {
    Set-Content -LiteralPath $probe -Value "x" -Force
    Remove-Item -LiteralPath $probe -Force
    $writable = $true
} catch {
    $writable = $false
    Write-Diag "temp_probe" "write/delete raised: $_"
}
$hazard = ($env:USERNAME -match '\.') -or ($tmp -match '~')
Write-Diag "temp_check" "tmp=$tmp hazard=$hazard writable=$writable"
if (-not $writable) {
    Emit FEHLER "Das temporäre Verzeichnis ($tmp) kann nicht beschrieben werden. Bitte prüfen, ob %TEMP% erreichbar ist, und EduBotics als der angemeldete Benutzer ausführen."
} elseif ($hazard) {
    Emit WARNUNG "Benutzername mit Punkt oder 8.3-Kurzpfad im temporären Verzeichnis erkannt — der Schreibtest war dennoch erfolgreich."
} else {
    Emit OK "Temporäres Verzeichnis ist beschreibbar ($tmp)."
}

# ── 2. UAC / self-elevation feasibility ────────────────────────────────────
$enableLua = $null
$consent = $null
try {
    $sys = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -ErrorAction SilentlyContinue
    if ($sys) {
        $enableLua = $sys.EnableLUA
        $consent = $sys.ConsentPromptBehaviorAdmin
    }
} catch {
    Write-Diag "uac" "Get-ItemProperty raised: $_"
}
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Diag "uac_check" "isAdmin=$isAdmin EnableLUA=$enableLua ConsentPromptBehaviorAdmin=$consent"
if ((-not $isAdmin) -and ($enableLua -eq 0)) {
    Emit FEHLER "UAC ist deaktiviert und Sie sind kein Administrator - die Einrichtung kann sich nicht selbst erhöhen. Bitte EduBotics mit der rechten Maustaste als Administrator ausführen."
} elseif ($isAdmin) {
    Emit OK "Ausführung mit Administratorrechten."
} else {
    Emit INFO "UAC aktiv - die Einrichtung fordert bei Bedarf eine Erhöhung an."
}

# ── 3. WSL2 availability ───────────────────────────────────────────────────
$wslOk = $false
try {
    wsl --status *>$null
    if ($LASTEXITCODE -eq 0) { $wslOk = $true }
} catch {
    Write-Diag "wsl" "wsl --status raised: $_"
}
if ($wslOk) {
    Emit OK "WSL2 aktiv"
} else {
    Emit WARNUNG "WSL2 noch nicht aktiv - wird bei der Einrichtung installiert (Neustart möglich)."
}

# ── 4. dockerd reachability (only if the EduBotics distro already exists) ───
$distroPresent = $false
try {
    $listed = wsl --list --quiet 2>&1
    foreach ($line in $listed) {
        if ((($line -replace "`0", "").Trim()) -eq "EduBotics") {
            $distroPresent = $true
            break
        }
    }
} catch {
    Write-Diag "distro" "wsl --list raised: $_"
}
if ($distroPresent) {
    # Reuse the single source of truth for dockerd-readiness. Dot-source it from
    # the same scripts dir; it exists at runtime alongside this file.
    $dockerReady = $false
    try {
        $readyHelper = Join-Path $PSScriptRoot 'wsl_docker_ready.ps1'
        if (Test-Path $readyHelper) {
            . $readyHelper
            # -NoFallback: a DIAGNOSTIC must not mutate the distro (starting
            # dockerd is the repair paths' job) and must stay inside the small
            # time budget the .iss [Run] stall and the GUI's capture timeout
            # assume (~35 s worst case instead of ~65-95 s with the fallback).
            $dockerReady = Wait-DockerReady -DistroName "EduBotics" -MaxWaitSeconds 30 -NoFallback
        } else {
            Write-Diag "docker" "wsl_docker_ready.ps1 not found next to preflight"
        }
    } catch {
        Write-Diag "docker" "Wait-DockerReady raised: $_"
    }
    if ($dockerReady) {
        Emit OK "Docker-Engine erreichbar"
    } else {
        Emit WARNUNG "Docker-Engine antwortet nicht - wird beim nächsten Start hochgefahren."
    }
} else {
    Emit INFO "EduBotics-Distro noch nicht vorhanden - wird bei der Einrichtung importiert."
}

# ── 5. Distro image on disk, but not registered for THIS Windows account ────
# WSL2 distros are registered PER WINDOWS USER (HKCU\...\Lxss), but the distro
# IMAGE is machine-wide: import_edubotics_wsl.ps1's default -InstallRoot puts
# ext4.vhdx under %ProgramData%\EduBotics\wsl. The installer is
# PrivilegesRequired=admin, so on a managed school PC where the student is a
# standard user and a DIFFERENT admin account elevates, `wsl --import` writes
# the registration into the ADMIN's hive and the student's un-elevated GUI
# cannot see the distro at all. The GUI reports that state as a stale
# WSL-service condition - the wrong diagnosis, and one no amount of rebooting
# can fix, because the environment is not missing, it is invisible to this user.
#
# Disk-yes / list-no is the SYMPTOM, not the cause, and it has a second
# producer: after a failed `wsl --import` the importer removes the partial
# ext4.vhdx under -ErrorAction SilentlyContinue, and that removal commonly
# fails because the WSL service or an AV scanner still holds the handle. The
# leftover file then reproduces this signature with no account problem at all -
# and telling a student to switch Windows accounts sends them somewhere they
# cannot go on a managed PC.
#
# The DISCRIMINATOR is the finalize marker's `user=` field - the same field
# gui_app.py parses, recording the account the elevated finalize actually ran
# as. Only a name that DIFFERS from $env:USERNAME proves the split and earns a
# FEHLER that names one cause; no marker, an unparseable one, or the same
# account gets a WARNUNG that names BOTH possibilities and a remedy that fixes
# either. $distroPresent is REUSED from check 4 rather than re-running
# `wsl --list`: one enumeration, one verdict.
$VhdxPath = Join-Path $env:ProgramData "EduBotics\wsl\ext4.vhdx"
$vhdxPresent = $false
try {
    $vhdxPresent = Test-Path -LiteralPath $VhdxPath
} catch {
    # This script's own convention: a diagnostic must never be able to abort.
    Write-Diag "account_scope" "Test-Path raised: $_"
}

# finalize_install.ps1 writes the marker into this same diagnostics leaf, and
# writes it -Encoding UTF8 - so read it as UTF8. A U+FFFD in the parsed name
# means an OLD (ANSI-written) marker was decoded: unparseable, never "a
# different account", because a wrong accusation is worse than the generic
# advice below.
$MarkerPath = Join-Path $DiagDir "edubotics_finalize.marker"
$markerUser = ""
try {
    if (Test-Path -LiteralPath $MarkerPath) {
        foreach ($line in (Get-Content -LiteralPath $MarkerPath -Encoding UTF8)) {
            $at = $line.IndexOf("user=")
            if ($at -lt 0) { continue }
            $rest = $line.Substring($at + 5)
            # %USERNAME% may contain spaces, so the value runs to end-of-line -
            # except in the SUCCESS shape, where " distro=" follows it.
            $cut = $rest.IndexOf(" distro=")
            if ($cut -ge 0) { $rest = $rest.Substring(0, $cut) }
            $markerUser = $rest.Trim()
            break
        }
    }
} catch {
    Write-Diag "account_scope" "marker read raised: $_"
}
if ($markerUser.IndexOf([char]0xFFFD) -ge 0) { $markerUser = "" }

# -ine, not -ne: Windows account names are case-insensitive, so a case
# difference is the SAME account. (PowerShell's -ne is already case-insensitive;
# spelling it out keeps the intent from reading like an oversight.)
$accountSplit = ($markerUser -ne "") -and ($env:USERNAME) -and ($markerUser -ine $env:USERNAME)
Write-Diag "account_scope" "vhdx=$VhdxPath present=$vhdxPresent distroRegistered=$distroPresent USERNAME=$env:USERNAME markerUser=$markerUser split=$accountSplit"
if ($vhdxPresent -and (-not $distroPresent)) {
    if ($accountSplit) {
        Emit FEHLER "Die EduBotics-Umgebung wurde mit dem Windows-Konto ($markerUser) eingerichtet, angemeldet sind Sie aber als ($env:USERNAME). Eine WSL-Umgebung gehört immer genau einem Windows-Konto und ist für jedes andere Konto unsichtbar. Bitte EduBotics mit dem Konto ($markerUser) starten - oder den EduBotics-Installer erneut ausführen, während Sie mit Ihrem eigenen Konto angemeldet sind."
    } else {
        Emit WARNUNG "Auf diesem PC liegt bereits eine EduBotics-Datenträgerdatei, für das angemeldete Windows-Konto ($env:USERNAME) ist aber keine WSL-Umgebung registriert. Dafür gibt es zwei Ursachen: Die Einrichtung lief unter einem anderen Windows-Konto, oder ein früherer Einrichtungsversuch ist abgebrochen und hat eine unvollständige Datei zurückgelassen. Bitte den EduBotics-Installer erneut ausführen, während Sie mit Ihrem eigenen Konto angemeldet sind - das behebt beide Fälle."
    }
}

if (-not $Quiet) {
    Write-Host "`n>> Selbstdiagnose abgeschlossen." -ForegroundColor Cyan
}
Write-Diag "preflight_end" "done"
exit 0
