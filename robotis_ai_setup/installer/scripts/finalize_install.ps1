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
#
# Exit codes — THE contract with the GUI. Our exit code is the AUTHORITY on what
# happened here; it is the ONLY thing the GUI branches on (gui_app.py
# ::_prompt_finalize_install::_run_elevated). Keep these stable, and keep the
# GUI's EXIT_* mirror in lockstep:
#    0  = done — import + pull both succeeded, .reboot_required cleared
#   10  = a host reboot is still required; nothing was installed yet.
#         .reboot_required is left SET (see the lifecycle comment below)
#   11  = WSL2 could not start a VM: `wsl` itself reported a hypervisor-class
#         failure (import_edubotics_wsl.ps1 classified it). Only that report
#         exits 11 — a CIM reading never does. WHICH remedy the student is given
#         (a BIOS setting / a missing Windows feature / the VM service) is
#         chosen from proof by virtualization_ready.ps1::
#         Get-HypervisorRemedyKind and travels to the GUI in the FAILED marker's
#         problem + next-step lines. Distinct from 10 because a restart is not
#         always the fix; distinct from 1 because the remedy is concrete.
#   12  = the rootfs must be re-imported, which DESTROYS the student's data, and
#         nobody consented — the one actionable remedy is "run the installer
#         again". Distinct from 1 so the GUI can say so instead of "failed".
#   1   = failed (any other reason; the transcript tail carries the cause)
#
# WHY THE REBOOT QUESTION IS NO LONGER ASKED HERE (2026-09-07, German school PC):
# this script used to own a private Test-RebootStillPending that decided „did
# they reboot?" from the WSL/VMP feature store's EnablePending state, while
# verify_system.ps1 decided the same question from flag-mtime vs
# Win32_OperatingSystem.LastBootUpTime — and verify's own comment claimed the
# two were the same check. They disagree on exactly ONE state, and it is the
# state a fresh install lands in: install_prerequisites.ps1 sets
# .reboot_required because `wsl --install --no-distribution` RAN, after which
# BOTH features read Enabled. So this script declared the reboot done, ran
# `wsl --import`, and got
#     Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE
# on every launch while telling the student to check their disk space — a disk
# whose 20 GB precheck had passed four lines earlier in the same transcript.
# Both halves now consume virtualization_ready.ps1, which asks the question
# that actually matters — is the hypervisor LIVE — instead of inferring a
# reboot from a proxy that the one writer who matters does not set.
#
# .reboot_required is NOT an exit code — it means "the deferred work is not
# finished", which is true of EVERY non-zero exit above. Routing on the flag
# instead of on the exit code is what made 10 and 12 dead code and reported
# "Neustart erforderlich" over a failed pull, forever (2026-07-17).
#
# The MARKER file is a second, NARROWER channel. The EXIT CODE still decides
# WHAT happened — the marker never overrides it and never picks a branch of the
# chain above. In exactly ONE cell it disambiguates WHICH German remedy to show:
# exit 0 (so finalize's own Test-DistroRegistered passed) while the un-elevated
# GUI still cannot see the distro. It is written "started <iso> pid=<n>
# user=<name>" the moment this script executes any code (proof it launched), and
# BOTH terminal paths overwrite it: Fail-WithNextAction with "FAILED
# <iso>\n<problem>\n<next step>", and the success path at the bottom with
# "SUCCESS <iso> user=<name> distro=<name>". The success stamp exists because
# that path used to leave the marker reading "started ...", so the GUI could not
# tell what the elevated side had observed — nor, on a managed PC where a
# DIFFERENT admin account elevates, AS WHOM. gui_app.py compares that `user=`
# against %USERNAME% to tell a per-Windows-account WSL registration split from a
# genuine stale-WSL-state failure.
#
# EVERY marker write carries -Encoding UTF8, and gui_app.py reads it back as
# utf-8-sig. PS 5.1's Set-Content default is the system ANSI codepage: on a
# German PC "Müller" was written cp1252 and decoded as "M<U+FFFD>ller", which
# can never casefold-match %USERNAME% — so the per-account branch fired on the
# SAME account and replaced correct reboot advice with advice that cannot help.

param(
    [string]$LogPath    = (Join-Path ([System.IO.Path]::GetTempPath()) 'edubotics_finalize.log'),
    [string]$MarkerPath = (Join-Path ([System.IO.Path]::GetTempPath()) 'edubotics_finalize.marker'),
    [string]$DistroName = "EduBotics",
    # Consent to a DESTRUCTIVE rootfs re-import (unregister + re-import, which
    # WIPES the distro's Docker volumes: datasets, HF cache, calibration).
    # The GUI passes this ONLY after showing its own German data-loss dialog on
    # a positively-detected rootfs mismatch; we merely FORWARD it to
    # import_edubotics_wsl.ps1. Never synthesize it here — without it import
    # refuses the wipe, which is exactly the intended safety default.
    [switch]$AllowDestructiveReimport
)

$ErrorActionPreference = "Continue"

# Exit-code constants (see the header). Named so the intent survives a skim.
# Declared BEFORE the dot-source below so its failure path can use them.
$EXIT_DONE    = 0
$EXIT_REBOOT  = 10
$EXIT_CONSENT = 12
$EXIT_VIRT    = 11

# The $EXIT_VIRT remedies, declared ONCE, one per Get-HypervisorRemedyKind
# result. A single remedy for every hypervisor failure is how this script used
# to print „Virtualisierung ist aktiv" and then blame VT-x in the BIOS in the
# same transcript. Each sentence states only what its kind PROVES:
#   Firmware — Windows itself reports the BIOS/UEFI setting off, so a restart is
#              not offered as the fix.
#   Feature  — wsl reported HCS_E_HYPERV_NOT_INSTALLED: restart first (a pending
#              feature enable completes then), then the feature and BIOS for IT.
#   Service  — everything else, including the 2026-09-07 field code: restart
#              first, then the VM service / hypervisorlaunchtype / BIOS for IT.
# „Neu starten, nicht Herunterfahren": with Fast Startup a shutdown resumes the
# old kernel, so neither a pending feature nor a hypervisor launch setting
# takes effect. preflight_system.ps1 and gui_app.py restate some of these; tests
# pin them to the spellings here. No typographic quotes inside these strings:
# PowerShell parses „ and " as double quotes.
$VIRT_FIRMWARE_PROBLEM_DE  = "Laut Windows ist die Virtualisierung (VT-x/AMD-V) im BIOS/UEFI dieses PCs ausgeschaltet — ohne sie kann WSL2 nicht starten."
$VIRT_FIRMWARE_NEXTSTEP_DE = "Bitte die IT-Betreuung der Schule bitten, die Virtualisierung im BIOS/UEFI einzuschalten, und EduBotics danach erneut öffnen."
$VIRT_FEATURE_PROBLEM_DE   = "WSL2 konnte seine virtuelle Maschine nicht starten, weil eine dafür nötige Windows-Funktion fehlt oder noch nicht aktiv ist."
$VIRT_FEATURE_NEXTSTEP_DE  = "Bitte den PC neu starten (Neu starten, nicht Herunterfahren) und EduBotics danach erneut öffnen. Hilft das nicht, bitte die IT-Betreuung informieren: die Windows-Funktion VM-Plattform und die Virtualisierung (VT-x/AMD-V) im BIOS/UEFI prüfen."
$VIRT_SERVICE_PROBLEM_DE   = "WSL2 konnte seine virtuelle Maschine nicht starten, weil der Windows-Dienst für virtuelle Maschinen nicht verfügbar ist."
$VIRT_SERVICE_NEXTSTEP_DE  = "Bitte den PC neu starten (Neu starten, nicht Herunterfahren) und EduBotics danach erneut öffnen. Hilft das nicht, bitte die IT-Betreuung informieren: den Dienst vmcompute, die Starteinstellung hypervisorlaunchtype und die Virtualisierung (VT-x/AMD-V) im BIOS/UEFI prüfen."
$EXIT_FAILED  = 1

# ── Marker: Proves the script actually started and survived long enough to
# execute ANY code. If this file exists, we know PowerShell launched and
# reached this point (UAC worked, script path was parseable, no syntax error).
# ───────────────────────────────────────────────────────────────────────────
try {
    $markerDir = Split-Path -Parent $MarkerPath
    if ($markerDir -and -not (Test-Path $markerDir)) {
        New-Item -ItemType Directory -Path $markerDir -Force | Out-Null
    }
    Set-Content -Path $MarkerPath -Value ("started {0} pid={1} user={2}" -f (Get-Date).ToString("o"), $PID, $env:USERNAME) -Encoding UTF8 -Force
} catch { }

# ── Transcript: Captures all stdout/stderr to $LogPath so the GUI can show
# what actually happened inside the elevated child (we cannot use
# -RedirectStandardOutput with -Verb RunAs on Start-Process).
# ───────────────────────────────────────────────────────────────────────────
$transcriptActive = $false
try {
    # ROTATE, never delete. Start-Transcript -Force overwrites anyway, so the
    # old Remove-Item bought nothing and cost the PREVIOUS attempt's evidence —
    # and on a machine that LOOPS (the 2026-09-07 failure repeated identically
    # across GUI launches) the last transcript is the least informative one,
    # because it cannot show what changed. One generation is enough:
    # .prev.log answers „what did the attempt before this one say".
    if (Test-Path $LogPath) {
        $prevLog = [System.IO.Path]::ChangeExtension($LogPath, '.prev.log')
        try {
            Move-Item -LiteralPath $LogPath -Destination $prevLog -Force -ErrorAction Stop
        } catch {
            Remove-Item $LogPath -Force -ErrorAction SilentlyContinue
        }
    }
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

# True iff the WSL distro is registered — the success gate below. Registration
# itself is answered by wsl_distro_state.ps1 (dot-sourced further down), which
# tells "not registered" from "WSL is not answering"; this wrapper is only the
# boolean the state-gated phases read.
function Test-DistroRegistered {
    param([string]$DistroName)
    return ((Get-EduBoticsDistroRegistration -DistroName $DistroName) -eq "Registered")
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
#
# $ExitCode defaults to $EXIT_FAILED and exists so the ONE marker writer can
# also serve the routed non-zero outcomes ($EXIT_VIRT). A second inline
# Set-Content of the FAILED marker shape would be a copy of exactly the kind
# this change set exists to delete, and the copy would be the one that forgets
# -Encoding UTF8 (see the marker paragraph in the header).
function Fail-WithNextAction {
    param([string]$Problem, [string]$NextStep, [int]$ExitCode = $EXIT_FAILED)
    Write-FAIL $Problem
    Write-Host "   Nächster Schritt: $NextStep" -ForegroundColor Yellow
    Write-Host "   Protokoll: $LogPath"
    try {
        Set-Content -LiteralPath $MarkerPath -Value ("FAILED {0}`n{1}`n{2}" -f (Get-Date).ToString("o"), $Problem, $NextStep) -Encoding UTF8 -Force
    } catch { }
    # $ExitCode (default $EXIT_FAILED), never a bare literal — the exit codes ARE
    # the GUI contract, and a second spelling of the same number is how the two
    # drift apart.
    exit $ExitCode
}

# ── Shared dockerd-readiness helper (dot-sourced; caller must keep EAP=Continue).
# Test-Path FIRST, mirroring preflight_system.ps1's guard: install_prerequisites
# .ps1 warns that Controlled Folder Access "may silently fail to write some files"
# into %ProgramFiles%, so a partially-copied {app}\scripts is a state this codebase
# already anticipates — and an unguarded dot-source of a missing file THROWS.
# Placed here, AFTER the marker + transcript + Fail-WithNextAction rather than at
# the top of the file: a throw (or an early exit) above those would reach the GUI
# as "Marker-Datei fehlt" over an EMPTY transcript, i.e. a diagnosed launch
# failure when the real cause is one missing file. Now it names the file, in
# German, in the marker AND the transcript.
$readyHelper = Join-Path $PSScriptRoot 'wsl_docker_ready.ps1'
if (-not (Test-Path $readyHelper)) {
    Fail-WithNextAction "Die Datei wsl_docker_ready.ps1 fehlt in $PSScriptRoot." "Die Installation ist unvollständig. Bitte den EduBotics-Installer erneut ausführen."
}
. $readyHelper

# ── .reboot_required lifecycle (load-bearing — read before touching) ────────
# install_prerequisites.ps1 writes this flag under {app}\scripts when a Windows
# feature enable needs a host reboot. Its meaning is "the deferred install work
# is NOT finished" — NOT "a reboot is needed". Only our exit code says why (10 =
# reboot, 12 = needs consent, 1 = failed).
#
# The flag is what the un-elevated GUI reads BEFORE it runs us, to decide whether
# to route into finalize at all: on an UPGRADE the old distro still exists, so
# is_distro_registered() alone would look like a finished install. That entry
# check is the flag's ONLY job. Once we have run, our exit code — not the flag —
# is what the GUI reports (gui_app.py::_run_elevated).
#
# Therefore: clear it ONLY after BOTH deferred jobs (import + pull) succeed.
# Until 2026-07 it was cleared right after Phase 0 — i.e. before either could
# fail. A fresh install whose GUI was opened BEFORE the reboot (`wsl --status`
# happily exits 0 on a not-yet-rebooted machine) lost the flag, then failed the
# import, and every later launch dead-ended on a generic
# "Einrichtung fehlgeschlagen" with the reboot guidance permanently gated off.
# The premature clear ALSO defeated import_edubotics_wsl.ps1's own reboot guard,
# which is why the import proceeds here via an explicit -PostReboot switch.
$flagPath = Join-Path $PSScriptRoot ".reboot_required"

# ── The reboot / virtualization verdict lives in ONE place ─────────────────
# virtualization_ready.ps1 owns Get-RebootState (the only reader of the flag,
# the feature store and CIM), Test-RebootOutstanding (proof-only boolean) and
# Get-VirtualizationVerdict (the Ready / RebootRequired / VirtualizationDisabled
# / Unknown ladder). verify_system.ps1 consumes the SAME file — that is the
# whole point, see the drift narrative in the header. Test-Path FIRST, mirroring
# the wsl_docker_ready.ps1 dot-source above: Controlled Folder Access can leave
# a partially-copied {app}\scripts, and an unguarded dot-source of a missing file
# THROWS. Placed after Fail-WithNextAction so the failure is reportable in German
# instead of reaching the GUI as an empty transcript.
$virtHelper = Join-Path $PSScriptRoot 'virtualization_ready.ps1'
if (-not (Test-Path $virtHelper)) {
    Fail-WithNextAction "Die Datei virtualization_ready.ps1 fehlt in $PSScriptRoot." "Die Installation ist unvollständig. Bitte den EduBotics-Installer erneut ausführen."
}
. $virtHelper

# ── "Is the distro registered?" — three answers, one implementation ─────────
# wsl_distro_state.ps1 separates "not registered" from "WSL is not answering"
# (see its header). Guarded exactly like the two helpers above.
$distroHelper = Join-Path $PSScriptRoot 'wsl_distro_state.ps1'
if (-not (Test-Path $distroHelper)) {
    Fail-WithNextAction "Die Datei wsl_distro_state.ps1 fehlt in $PSScriptRoot." "Die Installation ist unvollständig. Bitte den EduBotics-Installer erneut ausführen."
}
. $distroHelper

# The one German sentence for a WSL that does not answer. Declared once; the
# GUI and preflight say the same thing in their own words, never "fehlt".
$WSL_UNRESPONSIVE_PROBLEM_DE  = "WSL antwortet gerade nicht — ob die EduBotics-Umgebung vorhanden ist, lässt sich deshalb nicht feststellen. Sie wird NICHT neu eingerichtet."
$WSL_UNRESPONSIVE_NEXTSTEP_DE = "Bitte den PC neu starten (Neu starten, nicht Herunterfahren) und EduBotics danach erneut öffnen. Hilft das nicht, bitte die IT-Betreuung informieren."

# Render Get-RebootState's German fact lines into the transcript. Deliberately
# LOCAL to this script: virtualization_ready.ps1 must never call a Write-*
# helper its callers define (the lesson wsl_docker_ready.ps1 records — it would
# blow up in whichever caller has not defined one).
function Write-RebootNotes {
    param([hashtable]$State)
    foreach ($note in @($State.Notes)) { Write-Host "   $note" }
}

# Evidence only: which WSL build this PC runs. Printed here, not gathered by
# virtualization_ready.ps1, whose contract is no native calls. NUL-stripped
# (wsl.exe writes UTF-16LE to a pipe); never gates anything.
function Write-WslVersion {
    try {
        $verOut = ((wsl --version 2>&1 | Out-String -Width 4096) -replace "`0", "")
        $verRc = $LASTEXITCODE
        Write-Host "   wsl --version (exit $verRc):"
        foreach ($ln in ($verOut -split "`r?`n")) {
            if (-not [string]::IsNullOrWhiteSpace($ln)) { Write-Host "     $($ln.Trim())" }
        }
    } catch {
        Write-Host "   (wsl --version nicht lesbar: $_)"
    }
}

# Fail with the $EXIT_VIRT remedy that the PROOF supports (see the constants at
# the top and virtualization_ready.ps1::Get-HypervisorRemedyKind). ONE exit-11
# path, so the three remedies cannot drift into three call sites.
function Fail-WithHypervisorRemedy {
    param([hashtable]$State, [string]$FailureCode = "")
    $kind = Get-HypervisorRemedyKind -State $State -FailureCode $FailureCode
    switch ($kind) {
        "Firmware" { Fail-WithNextAction $VIRT_FIRMWARE_PROBLEM_DE $VIRT_FIRMWARE_NEXTSTEP_DE $EXIT_VIRT }
        "Feature"  { Fail-WithNextAction $VIRT_FEATURE_PROBLEM_DE $VIRT_FEATURE_NEXTSTEP_DE $EXIT_VIRT }
        default    { Fail-WithNextAction $VIRT_SERVICE_PROBLEM_DE $VIRT_SERVICE_NEXTSTEP_DE $EXIT_VIRT }
    }
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
        # Replay the usbipd pin that the .iss [Run] Step 1 handed
        # install_prerequisites.ps1 at install time. We are launched by the GUI,
        # not by Inno, so we never see the {#UsbipdVersion}/{#UsbipdSha256}
        # defines ourselves — and without them the elevated MSI download would
        # silently skip its SHA-256 verification AND slip past the
        # RELEASE_PIN_NEEDED sentinel. install_prerequisites.ps1 persists what it
        # was given next to this flag, which keeps the .iss the single source of
        # truth instead of copying the hash into a third file that can drift.
        $prereqArgs = @{}
        $pinPath = Join-Path $PSScriptRoot ".usbipd_pin"
        if (Test-Path $pinPath) {
            try {
                $pinLines = @(Get-Content -Path $pinPath -ErrorAction Stop)
                if ($pinLines.Count -ge 2 -and $pinLines[0].Trim() -and $pinLines[1].Trim()) {
                    $prereqArgs["UsbipdMsiUrl"]    = $pinLines[0].Trim()
                    $prereqArgs["UsbipdMsiSha256"] = $pinLines[1].Trim()
                    Write-Host "   usbipd-Pin aus der Installation übernommen."
                }
            } catch {
                Write-WARN "usbipd-Pin nicht lesbar: $_"
            }
        }
        if ($prereqArgs.Count -eq 0) {
            Write-WARN "Kein usbipd-Pin gefunden — die MSI-Integritätsprüfung greift nur bei gesetztem EDUBOTICS_USBIPD_SHA256."
        }

        # ── Custody of .reboot_required across the child call ───────────────
        # We deliberately do NOT pass -PreserveExistingRebootFlag (read that
        # param's own comment in install_prerequisites.ps1: a preserved STALE
        # flag makes the bare Test-Path below announce "Neustart erforderlich"
        # on every launch, forever). The price is that the child's Summary block
        # DELETES the flag whenever THAT run finds no reboot reason of its own —
        # including a flag it never wrote. The one that matters is
        # migrate_from_docker_desktop.ps1's "dd-uninstall" request, whose reboot
        # has NOT happened: losing it makes the Test-Path below fall through and
        # Phase 1 imports the distro next to a half-removed Docker Desktop —
        # exactly the entanglement the flag exists to prevent. (The GUI's usbipd
        # repair was fixed for this same class in ae7814a; finalize was not.)
        #
        # So take custody instead of choosing one horn: snapshot the flag, and
        # restore it ONLY when the reboot it asks for is GENUINELY outstanding
        # (Test-RebootOutstanding, the same proof-only predicate the verdict gate
        # below is built on). A stale flag stays deleted, which is what keeps the
        # reboot loop closed.
        $flagSnapshot = $null
        if (Test-Path $flagPath) {
            Write-Step "Vorhandene Neustart-Markierung wird geprüft..."
            if (Test-RebootOutstanding -State (Get-RebootState -FlagPath $flagPath)) {
                try {
                    $flagSnapshot = @{
                        Content = [string](Get-Content -Path $flagPath -Raw -ErrorAction Stop)
                        Written = (Get-Item -Path $flagPath -ErrorAction Stop).LastWriteTime
                    }
                } catch {
                    Write-Warn "Neustart-Markierung konnte nicht gesichert werden: $_"
                }
            }
        }

        & (Join-Path $PSScriptRoot "install_prerequisites.ps1") @prereqArgs
        $prereqRc = $LASTEXITCODE

        if (($null -ne $flagSnapshot) -and (-not (Test-Path $flagPath))) {
            # The child dropped a flag it did not write. Put it back verbatim —
            # CONTENT carries the reason ("dd-uninstall") and the ORIGINAL write
            # time is what Get-RebootState compares against the last boot.
            # Re-writing it fresh would reset both and declare the reboot done.
            try {
                Set-Content -Path $flagPath -Value $flagSnapshot.Content -NoNewline -Force
                (Get-Item -Path $flagPath -ErrorAction Stop).LastWriteTime = $flagSnapshot.Written
                Write-Warn "Neustart-Markierung wiederhergestellt (die Voraussetzungen hatten sie entfernt)."
            } catch {
                Write-Warn "Neustart-Markierung konnte nicht wiederhergestellt werden: $_"
            }
        }
        # Exit code FIRST, flag second. A failed prereq run never WRITES the
        # flag (its Summary block is skipped on every exit-1 path), so a flag
        # surviving a non-zero exit is by construction pre-existing/stale —
        # reporting it as $EXIT_REBOOT would loop the student through reboots
        # that can never fix the real failure (e.g. `wsl --install` with no
        # internet), which is exactly the masked-failure class this contract
        # exists to kill.
        if ($prereqRc -ne 0) {
            # Fail-WithNextAction, not a bare Write-FAIL + exit: this is the MOST
            # likely early failure — install_prerequisites.ps1 exits 1 on a failed
            # MSI download, a SHA mismatch, the RELEASE_PIN_NEEDED sentinel and a
            # failed `wsl --install`, i.e. "no internet in the classroom" lands
            # here. A bare "(exit 1)" gave the student no German next step and
            # left the marker still reading "started ...", which the GUI reads as
            # "never finished" rather than "failed for THIS reason" (the file
            # header at the top of this script says every failure exits via
            # Fail-WithNextAction; this path was the exception).
            Fail-WithNextAction "Voraussetzungen konnten nicht installiert werden (exit $prereqRc)." "Internetverbindung prüfen und EduBotics erneut öffnen."
        }
        # NO bare `if (Test-Path $flagPath) { exit $EXIT_REBOOT }` here any more.
        # A flag that survived the child is EITHER one the child wrote for its
        # own reason (a feature enable / `wsl --install` in THIS boot — which
        # also refreshes an existing flag's write time) OR one custody restored
        # because Test-RebootOutstanding PROVED its reboot is still owed. The
        # verdict gate right below reads exactly those proofs — and, unlike a
        # bare Test-Path, ranks a LIVE hypervisor above the flag-time inference.
        # The bare check sent a PC whose hypervisor was running, whose flag was
        # minutes old and whose first `wsl --status` merely hiccuped into a
        # restart it did not need (executed: exit 10; the verdict says Ready).
        Write-OK "Voraussetzungen installiert"
    }

    # Can this PC actually run WSL2 right now? Stop BEFORE the import if not —
    # an import into a dead hypervisor fails cryptically, and the flag stays set
    # either way so the GUI's entry check still re-routes here next launch.
    #
    # UNCONDITIONAL — deliberately NOT gated on `Test-Path $flagPath` any more.
    # The verdict's virtualization rungs are flag-INDEPENDENT, and the
    # 2026-09-07 failure (a live-looking feature store over a dead hypervisor)
    # needs no flag to happen. An absent flag is simply one more fact.
    #
    # The deleted line here used to read „Neustart bereits erfolgt — die
    # Windows-Features sind aktiv." That sentence was the single most misleading
    # line in the field log: it is what the script printed immediately before
    # importing into a hypervisor that was not running.
    Write-Step "Virtualisierung und Neustart-Status werden geprüft..."
    $rebootState = Get-RebootState -FlagPath $flagPath
    Write-RebootNotes -State $rebootState
    Write-WslVersion
    $virtVerdict = Get-VirtualizationVerdict -State $rebootState
    Write-Host "   Ergebnis: $virtVerdict"
    if ($virtVerdict -eq "RebootRequired") {
        Write-Step "NEUSTART ERFORDERLICH: Bitte den PC neu starten (Neu starten, nicht Herunterfahren) und EduBotics erneut öffnen."
        exit $EXIT_REBOOT
    }
    # Every other verdict PROCEEDS to the import — deliberately including
    # VirtualizationDisabled. That word comes from a CIM property, and a CIM
    # property that misreports would otherwise refuse this PC on every launch,
    # forever, with nothing the student or IT could do in EduBotics to get past
    # it. The import is cheap to try, refuses nothing destructive on a dead
    # hypervisor, and classifies wsl's OWN failure; that report — not a CIM
    # read — is what exits $EXIT_VIRT below, with the remedy the CIM facts
    # support.
    if ($virtVerdict -eq "Ready") {
        # Ready means HypervisorPresent read $true — say exactly that, and no
        # more: a running hypervisor with a stopped VM service can still fail
        # the import, and a line claiming „Virtualisierung ist aktiv" above a
        # VM-service remedy was a contradiction in the old transcript.
        Write-OK "Der Windows-Hypervisor läuft — die Einrichtung wird fortgesetzt."
    } elseif ($virtVerdict -eq "VirtualizationDisabled") {
        Write-Warn "Laut Windows ist die Virtualisierung im BIOS/UEFI ausgeschaltet — der Import wird trotzdem versucht, denn diese Angabe stimmt nicht auf jedem PC."
    } else {
        # Unknown. „nicht eindeutig", not „konnte nicht geprüft werden": Unknown
        # is ALSO the verdict when every value WAS read (no hypervisor running,
        # firmware virtualization on, no fresh flag — e.g.
        # `hypervisorlaunchtype off`); the HypervisorPresent and
        # VirtualizationFirmwareEnabled lines printed just above show which.
        Write-Warn "Virtualisierung ist nicht eindeutig bestätigt — die Einrichtung wird trotzdem fortgesetzt."
    }

    # Phase 1: Import the distro.
    # -PostReboot: the flag above is still set on purpose (we clear it only on
    # full success), and the verdict gate above has just ruled out an outstanding
    # reboot — Ready (proof the hypervisor is live), or Unknown /
    # VirtualizationDisabled (no proof that settles it, which proceeds by design
    # and is caught by import's own classification) — so import must not defer
    # on the flag.
    # -AllowDestructiveReimport is forwarded ONLY
    # when the GUI obtained the student's data-loss consent; without it import
    # refuses a rootfs-mismatch wipe, which is the intended default.
    # HASHTABLE splat, never an array: array splatting binds POSITIONALLY, so
    # @("-PostReboot") would arrive as $DistroName = "-PostReboot" instead of
    # setting the switch — an import against a nonexistent distro name.
    Write-Step "Schritt 1/2: EduBotics-Umgebung wird eingerichtet..."
    # A WSL that does not answer is never a reason to (re)import: whether the
    # distro exists is exactly what we cannot tell, and an import over an
    # existing registration — or import's own upgrade path — is the wrong
    # remedy for a service that is down. import re-checks the same word for its
    # standalone [Run] Step 4; stopping here keeps finalize's transcript clean.
    if ((Get-EduBoticsDistroRegistration -DistroName $DistroName) -eq "Unresponsive") {
        Fail-WithNextAction $WSL_UNRESPONSIVE_PROBLEM_DE $WSL_UNRESPONSIVE_NEXTSTEP_DE
    }
    $importArgs = @{ PostReboot = $true }
    if ($AllowDestructiveReimport) {
        Write-Host "   Zustimmung zum Neuaufbau liegt vor (Daten werden neu angelegt)."
        $importArgs["AllowDestructiveReimport"] = $true
    }
    # SUCCESS is judged on VERIFIED STATE below (Test-DistroRegistered +
    # Wait-DockerReady), never on the child's exit code — a cosmetic non-zero
    # (or a thrown terminating error) from the import child must not abort or
    # skip Phase 2. The ONE exit code that IS meaningful is import's 12
    # (consent-refusal): it has a specific, actionable remedy the GUI must
    # relay, so it is passed straight through. $LASTEXITCODE is reset first so
    # a stale value from an earlier native call can never masquerade as that 12
    # when the import child dies on a terminating error before its own exit.
    $global:LASTEXITCODE = 0
    # import hands its classified wsl error code back through this ONE global
    # (same runspace: it is invoked with `&`). Reset first so a value from an
    # earlier call can never masquerade as this import's.
    $global:EDUBOTICS_WSL_FAILURE_CODE = ""
    try { & (Join-Path $PSScriptRoot "import_edubotics_wsl.ps1") @importArgs } catch { Write-Warn "Import meldete einen Fehler, prüfe tatsächlichen Zustand: $_" }
    $importRc = $LASTEXITCODE
    if ($importRc -eq 12) {
        # import refused a DESTRUCTIVE rootfs re-import because nobody consented
        # (we were called without -AllowDestructiveReimport). This is not a
        # generic failure — it has one specific, actionable remedy. Pass import's
        # 12 straight through instead of flattening it into $EXIT_FAILED: the GUI
        # shows the remedy as its OWN message, so the student sees it even if the
        # transcript tail scrolls past.
        Write-FAIL "Die EduBotics-Umgebung muss neu aufgebaut werden (System-Update)."
        Write-Host "   Bitte den Installer erneut ausführen, um die Umgebung neu aufzubauen."
        Write-Host "   Der Installer fragt vorher nach Ihrer Zustimmung — laden Sie Ihre"
        Write-Host "   Datensätze vorher in der Web-Oberfläche zu Hugging Face hoch."
        exit $EXIT_CONSENT
    }
    # import classified its OWN failure: wsl reported that no VM could start
    # (HCS_E_SERVICE_NOT_AVAILABLE / HCS_E_HYPERV_NOT_INSTALLED, on the import or
    # on an existing distro's stamp read). Pass it through rather than
    # flattening it into $EXIT_FAILED, for the same reason the 12 above is passed
    # through: the remedy is specific and the GUI shows it as its own message.
    # This is the ONLY way to exit 11 — the verdict gate above never refuses on
    # a CIM reading. The remedy WORDS come from $rebootState (the CIM facts the
    # gate printed) plus the code import handed back.
    # Deliberately placed BELOW the 12 branch: both read $importRc, and
    # test_finalize_consent_branch_actually_exits_the_consent_code matches the
    # FIRST `if ($importRc -eq N)` block in this file.
    if ($importRc -eq $EXIT_VIRT) {
        Fail-WithHypervisorRemedy -State $rebootState -FailureCode ([string]$global:EDUBOTICS_WSL_FAILURE_CODE)
    }
    $registration = Get-EduBoticsDistroRegistration -DistroName $DistroName
    if ($registration -eq "Unresponsive") {
        # Not "nicht eingerichtet": WSL did not answer, so that is unknown — and
        # the disk/download next step below would be a guess about a PC we
        # could not even ask.
        Fail-WithNextAction $WSL_UNRESPONSIVE_PROBLEM_DE $WSL_UNRESPONSIVE_NEXTSTEP_DE
    }
    if ($registration -ne "Registered") {
        # Next step names the two known NON-transient causes (SHA-mismatch of
        # the rootfs tarball, low disk) explicitly — a bare "restart the PC"
        # would mislead when the import failed on one of those; the exact
        # reason is in the transcript either way.
        Fail-WithNextAction "Die EduBotics-Umgebung wurde nicht eingerichtet." "Bitte das Protokoll prüfen (häufige Ursachen: zu wenig freier Speicherplatz, beschädigter Download). Danach den PC neu starten und EduBotics erneut öffnen."
    }
    if (-not (Wait-DockerReady -DistroName $DistroName -MaxWaitSeconds 120)) {
        Fail-WithNextAction "Die Docker-Engine ist nicht gestartet." "Diagnose: wsl -d $DistroName -- tail -n 50 /var/log/dockerd.log"
    }
    Write-OK "EduBotics-Umgebung eingerichtet (Zustand verifiziert)"

    # Phase 2: Provide images — idempotent + state-gated. Skip if the images are
    # already present (a re-run after a partial finalize keeps what it pulled);
    # otherwise pull from the primary registry (GHCR) with a Docker Hub fallback
    # (pull_images.ps1 reads versions.env for tag+registry), then verify the
    # images actually landed rather than trusting the child exit code.
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

    # Both deferred jobs are done — NOW the flag may go. Anything that clears it
    # earlier strands the student (see the lifecycle comment above).
    if (Test-Path $flagPath) {
        Remove-Item $flagPath -Force -ErrorAction SilentlyContinue
        if (Test-Path $flagPath) {
            # We still exit 0 — the install IS done, and the exit code is what
            # the GUI reports. But the flag is the GUI's ENTRY check, so a stuck
            # flag would re-route the next launch back into finalize (which
            # re-runs two idempotent steps and retries this delete). The GUI
            # latches our EXIT_DONE for the session so it cannot loop on it
            # in-session; name it here so the transcript explains the re-prompt.
            Write-WARN "Die Markierung '.reboot_required' konnte nicht entfernt werden — bitte EduBotics erneut öffnen."
        }
    }

    Write-Step "Fertig! Sie können EduBotics jetzt nutzen."
    # Stamp the OUTCOME, mirroring Fail-WithNextAction's FAILED shape (see the
    # marker paragraph in the header). Deliberately the LAST thing before the
    # exit, so it can only ever be written on a genuinely complete run. `user=`
    # is not last on the line, so a reader must cut at " distro=" — %USERNAME%
    # can contain spaces. -Encoding UTF8 because the GUI decodes UTF-8 and a
    # German name written in the ANSI codepage comes back with a replacement
    # character (see the marker paragraph in the header). try/catch like every
    # other marker write: the marker is diagnostic, and failing to stamp it must
    # never turn a finished install into a failed one.
    try {
        Set-Content -LiteralPath $MarkerPath -Value ("SUCCESS {0} user={1} distro={2}" -f (Get-Date).ToString("o"), $env:USERNAME, $DistroName) -Encoding UTF8 -Force
    } catch { }
    exit $EXIT_DONE
} finally {
    if ($transcriptActive) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}
