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
# happened here; the GUI routes on it and on nothing else (gui_app.py
# ::_prompt_finalize_install::_run_elevated). Keep these stable, and keep the
# GUI's EXIT_* mirror in lockstep:
#    0  = done — import + pull both succeeded, .reboot_required cleared
#   10  = a host reboot is still required; nothing was installed yet.
#         .reboot_required is left SET (see the lifecycle comment below)
#   12  = the rootfs must be re-imported, which DESTROYS the student's data, and
#         nobody consented — the one actionable remedy is "run the installer
#         again". Distinct from 1 so the GUI can say so instead of "failed".
#   1   = failed (any other reason; the transcript tail carries the cause)
#
# .reboot_required is NOT an exit code — it means "the deferred work is not
# finished", which is true of EVERY non-zero exit above. Routing on the flag
# instead of on the exit code is what made 10 and 12 dead code and reported
# "Neustart erforderlich" over a failed pull, forever (2026-07-17).

param(
    [string]$LogPath    = "$env:TEMP\edubotics_finalize.log",
    [string]$MarkerPath = "$env:TEMP\edubotics_finalize.marker",
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
$EXIT_DONE    = 0
$EXIT_REBOOT  = 10
$EXIT_CONSENT = 12
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
function Write-WARN { param([string]$msg) Write-Host "   WARN: $msg" }
function Write-FAIL { param([string]$msg) Write-Host "   FAIL: $msg" }

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

# True only when a Windows feature enable is genuinely still waiting on a
# reboot. `wsl --status` is NOT a discriminator here — it exits 0 on a machine
# whose WSL feature is merely EnablePending, where no distro can be imported
# yet. Ask the feature store instead: it is the same signal
# install_prerequisites.ps1 used to write the flag in the first place. An
# unreadable feature store (COMException while a servicing op is pending) falls
# back to the caller's `wsl --status` verdict rather than manufacturing a reboot.
function Test-RebootStillPending {
    param([bool]$WslResponds)
    # The flag CONTENT names WHY the reboot was requested. "dd-uninstall" is
    # migrate_from_docker_desktop.ps1's reason (Docker Desktop's uninstaller
    # returned 3010 — its removal completes on the next boot). The feature-store
    # probe below is BLIND to that state: WSL/VMP read Enabled throughout a
    # pending DD removal, so it would declare the reboot done and let the import
    # run next to a half-removed Docker Desktop — the exact entanglement the
    # flag was written to prevent. For that reason, discriminate on TIME
    # instead: a boot AFTER the flag was written means the reboot happened (and
    # whatever remains of DD will not be fixed by another one — proceed rather
    # than loop); no boot since the flag means the reboot is genuinely still
    # outstanding, whatever the student clicked. Falls through to the feature
    # checks either way a) so a combined reason (DD 3010 + a feature
    # EnablePending in the same session) still defers, and b) on any read error.
    try {
        $flagReason = (Get-Content -Path $flagPath -TotalCount 1 -ErrorAction Stop)
        if ($null -ne $flagReason -and $flagReason.Trim() -eq "dd-uninstall") {
            $flagTime = (Get-Item -Path $flagPath -ErrorAction Stop).LastWriteTime
            $bootTime = (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime
            Write-Host "   Docker-Desktop-Entfernung: Markierung $($flagTime.ToString('s')), letzter Start $($bootTime.ToString('s'))"
            if ($bootTime -le $flagTime) {
                Write-Host "   Seit der Markierung wurde nicht neu gestartet — die Docker-Desktop-Entfernung ist noch nicht abgeschlossen."
                return $true
            }
        }
    } catch {
        Write-Host "   (Neustart-Grund nicht lesbar: $_ — Windows-Features werden geprüft)"
    }
    $pending = $false
    $unreadable = $false
    foreach ($feature in @("VirtualMachinePlatform", "Microsoft-Windows-Subsystem-Linux")) {
        try {
            $state = (Get-WindowsOptionalFeature -Online -FeatureName $feature -ErrorAction Stop).State
            Write-Host "   $feature = $state"
            if ($state -eq "EnablePending") { $pending = $true }
        } catch {
            $unreadable = $true
            Write-Host "   ($feature-Status nicht lesbar: $_)"
        }
    }
    if ($pending) { return $true }
    if ($unreadable -and -not $WslResponds) { return $true }
    return $false
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
        & (Join-Path $PSScriptRoot "install_prerequisites.ps1") @prereqArgs
        $prereqRc = $LASTEXITCODE
        # install_prerequisites writes .reboot_required when a fresh WSL2
        # install needs a host reboot before a distro can be imported. Leave the
        # flag exactly where it is (the GUI's entry check re-routes here after
        # the reboot) and report the reason via $EXIT_REBOOT, which is what the
        # GUI turns into the reboot instructions.
        if (Test-Path $flagPath) {
            Write-Step "NEUSTART ERFORDERLICH: Bitte den PC neu starten und EduBotics erneut öffnen."
            exit $EXIT_REBOOT
        }
        if ($prereqRc -ne 0) {
            Write-FAIL "Voraussetzungen konnten nicht installiert werden (exit $prereqRc)."
            exit $EXIT_FAILED
        }
        Write-OK "Voraussetzungen installiert"
    }

    # A flag surviving to this point means an EARLIER run asked for a reboot.
    # Decide whether it already happened — if not, stop BEFORE the import (which
    # would fail cryptically) and keep the flag set so the GUI can say so.
    if (Test-Path $flagPath) {
        Write-Step "Neustart-Status wird geprüft..."
        if (Test-RebootStillPending -WslResponds $wslOk) {
            Write-Step "NEUSTART ERFORDERLICH: Bitte den PC neu starten und EduBotics erneut öffnen."
            exit $EXIT_REBOOT
        }
        Write-OK "Neustart bereits erfolgt — die Windows-Features sind aktiv."
    }

    # Phase 1: Import the distro.
    # -PostReboot: the flag above is still set on purpose (we clear it only on
    # full success), and we have just proven the reboot already happened — so
    # import must not defer on it. -AllowDestructiveReimport is forwarded ONLY
    # when the GUI obtained the student's data-loss consent; without it import
    # refuses a rootfs-mismatch wipe, which is the intended default.
    # HASHTABLE splat, never an array: array splatting binds POSITIONALLY, so
    # @("-PostReboot") would arrive as $DistroName = "-PostReboot" instead of
    # setting the switch — an import against a nonexistent distro name.
    Write-Step "Schritt 1/2: EduBotics-Umgebung wird eingerichtet..."
    $importArgs = @{ PostReboot = $true }
    if ($AllowDestructiveReimport) {
        Write-Host "   Zustimmung zum Neuaufbau liegt vor (Daten werden neu angelegt)."
        $importArgs["AllowDestructiveReimport"] = $true
    }
    & (Join-Path $PSScriptRoot "import_edubotics_wsl.ps1") @importArgs
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
    if ($importRc -ne 0) {
        Write-FAIL "Rootfs-Import fehlgeschlagen (exit $importRc)."
        exit $EXIT_FAILED
    }
    Write-OK "EduBotics-Umgebung eingerichtet"

    # Phase 2: Provide images — pull from the primary registry (GHCR) with a
    # Docker Hub fallback (pull_images.ps1 reads versions.env for tag+registry).
    Write-Step "Schritt 2/2: Docker-Images werden bereitgestellt..."
    & (Join-Path $PSScriptRoot "pull_images.ps1")
    if ($LASTEXITCODE -ne 0) {
        Write-FAIL "Images konnten nicht bereitgestellt werden (exit $LASTEXITCODE). Bei Online-Installation bitte Internetverbindung prüfen."
        exit $EXIT_FAILED
    }
    Write-OK "Images bereitgestellt"

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
    exit $EXIT_DONE
} finally {
    if ($transcriptActive) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}
