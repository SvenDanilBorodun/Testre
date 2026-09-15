# import_edubotics_wsl.ps1 — Import the bundled EduBotics WSL2 distro
#
# Runs after install_prerequisites.ps1 (WSL2 is guaranteed available) and after
# migrate_from_docker_desktop.ps1 (no Docker Desktop competing for resources).
# Reads wsl_rootfs/edubotics-rootfs.tar.gz shipped inside {app}, imports it as
# a WSL2 distro named "EduBotics", and waits for dockerd to come up.
#
# Safe to re-run. Upgrade behavior (2026-06-05): when the existing distro's
# baked rootfs version (/etc/edubotics-rootfs-version) matches the shipped
# wsl_rootfs/ROOTFS_VERSION, the import is SKIPPED and the distro — including
# its Docker volumes (datasets, HF cache, calibration) — is preserved. Only a
# genuine rootfs change (or -Force) unregisters and re-imports, which destroys
# the named volumes (the installer UI asks for consent first).
#
# Must run elevated (as Administrator).

param(
    [string]$DistroName   = "EduBotics",
    [string]$InstallRoot  = "$env:ProgramData\EduBotics\wsl",
    [string]$RootfsPath   = "",  # resolved below if empty
    # Restore the pre-2026-06 unconditional unregister+re-import (manual
    # recovery from a corrupted distro; DESTROYS the Docker volumes).
    [switch]$Force,
    # Permit a version-mismatch re-import (which DESTROYS the Docker volumes).
    # The .iss Step 4 passes this because ShouldImportDistro already obtained the
    # student's consent via a MsgBox; finalize_install.ps1 does NOT pass it, so a
    # GUI-driven finalize can never silently wipe data on a rootfs bump.
    [switch]$AllowDestructiveReimport,
    # Bypass the .reboot_required deferral guard below. finalize_install.ps1
    # passes this AFTER virtualization_ready.ps1's verdict came back Ready or
    # Unknown (never RebootRequired / VirtualizationDisabled) — so the flag,
    # which finalize clears only after a FULL success, must not defer the import
    # here. The installer's Step 4 does NOT pass it (its ShouldImportDistro
    # Check already gates on IsRebootRequired).
    [switch]$PostReboot
)

# EAP=Continue, NOT Stop (load-bearing — mirrors verify_system.ps1). In Windows
# PowerShell 5.1 a native command (wsl/docker) writing to stderr is promoted to a
# TERMINATING NativeCommandError under EAP=Stop — for every redirection form, and
# even on success (`docker info` prints a swap-limit warning to stderr on a healthy
# daemon). Under Stop this aborted the dockerd-readiness poll on iteration 1, so the
# import "failed" and the offline image load was silently skipped. Native outcomes
# are checked via $LASTEXITCODE; do NOT revert to Stop without wrapping every
# wsl/docker call in try/catch.
$ErrorActionPreference = "Continue"

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-FAIL { param([string]$msg) Write-Host "   FAIL: $msg" -ForegroundColor Red }

# Shared `wsl` failure classifier (Get-WslFailureClass). Dot-sourced SOFTLY:
# unlike finalize_install.ps1, this script is also the installer's [Run] Step 4,
# where hard-failing a fresh install over a missing DIAGNOSTIC file would be the
# worse trade. Absent helper => $failClass stays "" => byte-identical wording to
# before this change. Test-Path first either way (Controlled Folder Access can
# leave a partially-copied {app}\scripts).
$virtHelper = Join-Path $PSScriptRoot 'virtualization_ready.ps1'
$virtHelperOk = $false
if (Test-Path $virtHelper) {
    . $virtHelper
    $virtHelperOk = $true
}

# Three-way registration (wsl_distro_state.ps1). Soft for the same reason: an
# absent helper falls back to the old two-way read below, byte-identical to
# before, so a partially-copied {app}\scripts never hard-fails the installer.
$distroHelper = Join-Path $PSScriptRoot 'wsl_distro_state.ps1'
$distroHelperOk = $false
if (Test-Path $distroHelper) {
    . $distroHelper
    $distroHelperOk = $true
}

# The hypervisor-class report, spelled ONCE for both places this script meets
# a VM that cannot start: an existing distro whose stamp read fails that way,
# and `wsl --import`. It does three things and asserts nothing it cannot know:
#   * hands the classified CODE back to finalize_install.ps1 through
#     $global:EDUBOTICS_WSL_FAILURE_CODE (same runspace — finalize invokes this
#     script with `&`), so finalize can pick the remedy for the GUI;
#   * prints the CIM facts the remedy kind is chosen from, when the helper is
#     there to read them (this script also runs standalone as the installer's
#     [Run] Step 4, where there is no finalize transcript to carry them);
#   * prints a short German pointer that never claims a single cause — the
#     old text named „genau zwei Ursachen" and was wrong on a PC whose
#     hypervisor was running.
function Write-HypervisorRemedy {
    param([string]$Text)
    $code = ""
    if ($virtHelperOk) { $code = Get-WslFailureCode -Text $Text }
    $global:EDUBOTICS_WSL_FAILURE_CODE = $code
    $kind = "Service"
    if ($virtHelperOk) {
        try {
            $st = Get-RebootState -FlagPath $rebootFlag
            # Under -PostReboot finalize printed these same facts moments ago.
            if (-not $PostReboot) { foreach ($note in @($st.Notes)) { Write-Host "   $note" } }
            $kind = Get-HypervisorRemedyKind -State $st -FailureCode $code
        } catch {
            Write-Host "   (Virtualisierungsstatus nicht lesbar: $_)" -ForegroundColor Yellow
        }
    }
    $codeLabel = if ($code) { $code } else { "unbekannt" }
    Write-Host "   WSL2 konnte keine virtuelle Maschine starten (Hypervisor-Fehlerart: $kind, Fehlercode: $codeLabel)." -ForegroundColor Red
    # Under -PostReboot, finalize_install.ps1 prints the full remedy for this
    # kind on the very next lines (Fail-WithHypervisorRemedy); a second, shorter
    # copy here would only make the transcript say it twice.
    if ($PostReboot) { return }
    if ($kind -eq "Firmware") {
        Write-Host "   Laut Windows ist die Virtualisierung (VT-x/AMD-V) im BIOS/UEFI ausgeschaltet." -ForegroundColor Red
        Write-Host "   Bitte die IT-Betreuung der Schule bitten, sie im BIOS/UEFI einzuschalten." -ForegroundColor Red
    } else {
        Write-Host "   Bitte den PC neu starten (Neu starten, nicht Herunterfahren). Hilft das nicht," -ForegroundColor Red
        Write-Host "   bitte die IT-Betreuung informieren (Dienst vmcompute, hypervisorlaunchtype," -ForegroundColor Red
        Write-Host "   Windows-Funktion VM-Plattform, Virtualisierung im BIOS/UEFI)." -ForegroundColor Red
    }
}

# Bail if prerequisites phase still needs a reboot (WSL2 not fully up yet).
#
# -PostReboot bypasses this guard. finalize_install.ps1 keeps .reboot_required
# set until its FULL success (import + pull), so that a mid-way failure still
# leaves the GUI the "reboot pending" routing signal it reads BEFORE our exit
# code. That means the flag is still on disk when finalize calls us on the
# legitimate post-reboot path — without the switch we would defer forever and
# finalize would report success over an import that never ran.
$rebootFlag = Join-Path $PSScriptRoot ".reboot_required"
if ((Test-Path $rebootFlag) -and (-not $PostReboot)) {
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

# Rootfs upgrade gate (2026-06-05). The unconditional unregister+re-import on
# every upgrade silently DESTROYED the distro's Docker volumes (recorded
# datasets, HF cache, Roboter-Studio calibration). build_rootfs.sh now bakes
# wsl_rootfs/ROOTFS_VERSION into /etc/edubotics-rootfs-version; when the
# existing distro carries the SAME stamp as this installer's shipped rootfs,
# the import is skipped and the student's data survives the upgrade. The
# installer UI additionally asks for consent before a destructive re-import
# (robotis_ai_setup.iss::ShouldImportDistro); this script re-checks the gate
# so manual invocations behave identically. Distros imported by installers
# <= 2.6.0 have no marker -> gate misses -> one final re-import.
$shippedVersion = ""
foreach ($vf in @(
    (Join-Path (Split-Path -Parent $RootfsPath) "ROOTFS_VERSION"),  # production: next to the tarball
    (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "wsl_rootfs\ROOTFS_VERSION")  # dev tree
)) {
    if (Test-Path $vf) {
        try { $shippedVersion = (Get-Content $vf -First 1).Trim() } catch { $shippedVersion = "" }
        if ($shippedVersion) { break }
    }
}

# Detect an existing EduBotics distro (upgrade path)
Write-Step "Checking for existing EduBotics distro..."
$existing = $false
if ($distroHelperOk) {
    $registration = Get-EduBoticsDistroRegistration -DistroName $DistroName
    if ($registration -eq "Unresponsive") {
        # WSL did not answer, so whether the distro exists is UNKNOWN. Both
        # things this script could do next are wrong for that: an import over
        # a registration that may exist, or the upgrade path's unregister of a
        # distro whose data we cannot even see. Refuse before either.
        Write-FAIL "WSL antwortet gerade nicht — die EduBotics-Umgebung wird NICHT eingerichtet oder verändert."
        Write-Host "   Bitte den PC neu starten (Neu starten, nicht Herunterfahren) und die Einrichtung erneut starten." -ForegroundColor Red
        Write-Host "   Eine vorhandene Umgebung und ihre Daten bleiben unberührt." -ForegroundColor Yellow
        exit 1
    }
    $existing = ($registration -eq "Registered")
} else {
    try {
        $listed = wsl --list --quiet 2>&1
        foreach ($line in $listed) {
            if (($line -replace "`0", "").Trim() -eq $DistroName) {
                $existing = $true
                break
            }
        }
    } catch { }
}

$skipImport = $false
if ($existing) {
    $distroVersion = ""
    $stampOut = ""
    if (-not $Force -and $shippedVersion) {
        try {
            $stampOut = ((wsl -d $DistroName -- cat /etc/edubotics-rootfs-version 2>&1 | Out-String -Width 4096) -replace "`0", "")
            $distroVersion = $stampOut.Trim()
            if ($LASTEXITCODE -ne 0) { $distroVersion = "" }
        } catch { $distroVersion = "" }
    }
    # An UNREADABLE stamp is the designed trigger for the one-final re-import
    # (distros from installers <= 2.6.0 carry no stamp). But the read also fails
    # when the distro CANNOT START, and then the rebuild is not a remedy: the
    # unregister below destroys every dataset, the HF cache and the calibration,
    # and the `wsl --import` after it fails on the same dead hypervisor —
    # executed with a fake wsl, that sequence ran to the end with the distro
    # gone. So when the failed read PROVES the hypervisor fault (the same
    # code-token classifier as the import below), refuse before anything is
    # destroyed, whatever consent was given, and report the hypervisor cause.
    # Unclassified failures keep the old behaviour; -Force skips the read.
    if ((-not $distroVersion) -and $virtHelperOk -and
            ((Get-WslFailureClass -Text $stampOut) -eq "hypervisor")) {
        if (-not [string]::IsNullOrWhiteSpace($stampOut)) { Write-Host $stampOut.TrimEnd() }
        Write-FAIL "Die vorhandene EduBotics-Umgebung kann nicht gestartet werden — sie wird NICHT neu aufgebaut."
        Write-HypervisorRemedy -Text $stampOut
        Write-Host "   Die Umgebung und ihre Daten bleiben erhalten." -ForegroundColor Yellow
        # 11 like the import's own hypervisor refusal below: finalize maps it to
        # $EXIT_VIRT; Inno's [Run] Step 4 ignores exit codes.
        exit 11
    }
    if ($distroVersion -and ($distroVersion -eq $shippedVersion)) {
        Write-OK "Vorhandene EduBotics-Umgebung ist aktuell (Rootfs-Version $shippedVersion) — Import übersprungen."
        Write-Host "   Lokale Daten (Datensätze, Modelle, Kalibrierung) bleiben erhalten." -ForegroundColor Green
        $skipImport = $true
    } else {
        # Destructive re-import (unregister + re-import) WIPES the distro's
        # Docker volumes (datasets, HF cache, calibration). Require explicit
        # consent — -Force (manual recovery) OR -AllowDestructiveReimport (the
        # .iss Step 4, gated behind the ShouldImportDistro consent MsgBox).
        # Without either, REFUSE rather than silently wipe: this closes the gap
        # where finalize_install.ps1 (which passes NEITHER) would have wiped a
        # student's data on a rootfs bump during a reboot-pending upgrade.
        if (-not ($Force -or $AllowDestructiveReimport)) {
            Write-FAIL "Rootfs-Update erfordert eine Neuinstallation über den Installer."
            Write-Host "   Vorhandene Rootfs-Version '$distroVersion' passt nicht zur neuen Version '$shippedVersion'." -ForegroundColor Yellow
            Write-Host "   Ein automatischer Neuaufbau würde lokale Daten löschen und wird ohne" -ForegroundColor Yellow
            Write-Host "   Zustimmung nicht durchgeführt. Bitte den EduBotics-Installer erneut" -ForegroundColor Yellow
            Write-Host "   ausführen — er fragt vor dem Neuaufbau nach Zustimmung." -ForegroundColor Yellow
            # 12, not 1: this refusal has ONE specific remedy, and it is the
            # designed-for outcome rather than a malfunction.
            # finalize_install.ps1 maps it to the German "Installer erneut
            # ausführen" instruction instead of a generic failure. Inno's [Run]
            # Step 4 ignores exit codes, so this code is finalize-only.
            exit 12
        }
        if ($Force) {
            Write-Host "   Re-Import erzwungen (-Force)." -ForegroundColor White
        } else {
            Write-Host "   Rootfs-Version unterschiedlich (vorhanden: '$distroVersion', neu: '$shippedVersion') — Neuaufbau mit Zustimmung." -ForegroundColor White
        }
        Write-Host "   Unregistering existing $DistroName (upgrade)..." -ForegroundColor White
        wsl --unregister $DistroName *>$null
        if ($LASTEXITCODE -ne 0) {
            Write-FAIL "Konnte existierenden Distro nicht entfernen."
            exit 1
        }
        Write-OK "Existing distro removed"
    }
}

if (-not $skipImport) {
    # Ensure install root exists.
    #
    # A PRE-EXISTING $InstallRoot is not automatically trustworthy. We run
    # elevated and are about to write ext4.vhdx — every recorded dataset, the HF
    # cache and the Roboter-Studio calibration — into this path. %ProgramData%
    # itself carries an inherited `BUILTIN\Users:(CI)(AD)` ACE (create-folder) and
    # `CREATOR OWNER:(OI)(CI)(IO)(F)`, so a standard user can create a directory
    # here and OWN it before we ever run; if it is a junction/symlink, the write
    # lands wherever they pointed it and this becomes an elevated arbitrary-write.
    # (Until 2026-07-19 the diagnostics ACL made that strictly worse by granting
    # Users:Modify on the PARENT — that grant now sits on the …\EduBotics\logs
    # leaf; see install_prerequisites.ps1.) We only CREATE the directory when it
    # is absent, so we never re-ACL an existing one — refuse the two shapes we can
    # detect instead of importing into them.
    if (Test-Path $InstallRoot) {
        $rootItem = $null
        try {
            $rootItem = Get-Item -LiteralPath $InstallRoot -Force -ErrorAction Stop
        } catch {
            Write-FAIL "Installationsverzeichnis $InstallRoot konnte nicht geprüft werden: $_"
            Write-Host "   Bitte das Verzeichnis entfernen und die Installation erneut starten." -ForegroundColor Red
            exit 1
        }
        if ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            Write-FAIL "Installationsverzeichnis $InstallRoot ist eine Verknüpfung (Junction) und wird nicht verwendet."
            Write-Host "   Das kann ein Manipulationsversuch sein. Bitte das Verzeichnis entfernen" -ForegroundColor Red
            Write-Host "   und die Installation erneut starten." -ForegroundColor Red
            exit 1
        }
        if (-not ($rootItem.Attributes -band [System.IO.FileAttributes]::Directory)) {
            Write-FAIL "Installationspfad $InstallRoot ist kein Verzeichnis."
            Write-Host "   Bitte die Datei entfernen und die Installation erneut starten." -ForegroundColor Red
            exit 1
        }
    } else {
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
            Write-FAIL "Nicht genug Speicher auf Laufwerk $drive`: ${freeGb} GB frei, 20 GB werden benötigt."
            Write-Host "   Bitte Speicher freigeben und Installation erneut starten." -ForegroundColor Red
            exit 1
        }
    } catch {
        Write-Host "   (Speicherplatz-Prüfung übersprungen: $_)" -ForegroundColor Yellow
    }

    # SHA256 integrity check on the rootfs tar. If a matching .sha256 file
    # ships alongside, verify it before wasting 1-3 minutes on `wsl --import`
    # of a corrupted/swapped tarball.
    #
    # Audit H22: the sidecar verify used to be fail-SOFT — a yellow warning
    # on read error and the import proceeded. That defeats the entire
    # point of shipping a sidecar (tamper detection). The sidecar IS
    # shipped by build_rootfs.sh, so its absence is suspicious, not a
    # routine state. Hard-fail on read or parse error.
    $sha256File = "$RootfsPath.sha256"
    if (Test-Path $sha256File) {
        $expectedLine = $null
        try {
            $expectedLine = (Get-Content $sha256File -First 1).Trim()
        } catch {
            Write-FAIL "SHA256-Sidecar konnte nicht gelesen werden: $_"
            Write-Host "   Die Datei $sha256File ist beschädigt oder gesperrt." -ForegroundColor Red
            exit 1
        }
        if (-not $expectedLine) {
            Write-FAIL "SHA256-Sidecar ist leer: $sha256File"
            exit 1
        }
        $expected = ($expectedLine -split '\s+')[0].ToUpper()
        if (-not $expected -or $expected.Length -ne 64) {
            Write-FAIL "SHA256-Sidecar enthält keinen gültigen 64-stelligen Hash."
            exit 1
        }
        $actual = (Get-FileHash -Path $RootfsPath -Algorithm SHA256).Hash.ToUpper()
        if ($expected -ne $actual) {
            Write-FAIL "Rootfs SHA256 passt nicht: expected=$expected actual=$actual"
            Write-Host "   Die Installer-Datei könnte beschädigt oder manipuliert sein." -ForegroundColor Red
            exit 1
        }
        Write-OK "Rootfs SHA256 verified"
    } else {
        # No sidecar at all — log a warning but allow (older installers
        # may legitimately ship without one). build_rootfs.sh always
        # generates the sidecar, so a missing one in a fresh build is
        # the suspicious case; an old release shipped to upgrade is fine.
        Write-Host "   (kein SHA256-Sidecar gefunden: $sha256File)" -ForegroundColor Yellow
    }

    # CAPTURE the import's own words, then echo them VERBATIM so the transcript
    # keeps exactly the evidence it had before, and classify what they say.
    # Until 2026-09-11 this was a bare call whose output nobody read, and the
    # failure branch printed ONE hardcoded triad for every cause — so a dead
    # hypervisor was reported as a disk/antivirus problem, in a transcript where
    # the 20 GB disk precheck and the rootfs SHA-256 had both just PASSED.
    #
    # `2>&1` + NUL-strip is this file's existing house pattern for wsl.exe (see
    # the `wsl --list --quiet` reads above): its output can be BOM-less UTF-16LE.
    # EAP is Continue in this file (see the note at the top), so merging stderr
    # cannot become a terminating NativeCommandError. -Width 4096 matters: the
    # default wraps at the host width and the code token we classify on sits at
    # column 79 of its line.
    $importOut = ((& wsl --import $DistroName $InstallRoot $RootfsPath --version 2 2>&1 | Out-String -Width 4096) -replace "`0", "")
    $importExit = $LASTEXITCODE
    if (-not [string]::IsNullOrWhiteSpace($importOut)) { Write-Host $importOut.TrimEnd() }
    if ($importExit -ne 0) {
        # Classify on the ERROR CODE, never the message: the field transcript
        # carried a German sentence beside an ASCII code token, which is the same
        # reason install_prerequisites.ps1 deleted its English-only
        # `systeminfo | Select-String "Hyper-V Requirements"` probe.
        $failClass = ""
        if ($virtHelperOk) { $failClass = Get-WslFailureClass -Text $importOut }
        Write-FAIL "wsl --import fehlgeschlagen (exit $importExit)"
        if ($failClass -eq "hypervisor") {
            Write-HypervisorRemedy -Text $importOut
        } else {
            Write-Host "   Prüfen Sie: Antivirus-Ausnahme, genug Speicherplatz, WSL2 aktiviert." -ForegroundColor Red
        }
        # Clean up partial VHDX to prevent "import over broken state" on retry.
        if (Test-Path $InstallRoot) {
            try { Remove-Item -Path (Join-Path $InstallRoot 'ext4.vhdx') -Force -ErrorAction SilentlyContinue } catch {}
        }
        # 11, not 1, for the hypervisor class — finalize_install.ps1 maps it to
        # its own $EXIT_VIRT and the GUI turns that into the German reboot/BIOS
        # dialog. Exactly mirrors the `exit 12` consent refusal above: Inno's
        # [Run] Step 4 ignores exit codes, so this code is finalize-only.
        if ($failClass -eq "hypervisor") { exit 11 }
        exit 1
    }
    Write-OK "Distro imported"

    # Read back the baked version marker so the NEXT upgrade can skip. A
    # missing marker (rootfs built before the stamp existed, or an unstamped
    # dev build) is only a warning — that upgrade will re-import once more.
    try {
        $markerVer = ((wsl -d $DistroName -- cat /etc/edubotics-rootfs-version 2>&1 | Out-String) -replace "`0", "").Trim()
        if ($LASTEXITCODE -eq 0 -and $markerVer) {
            Write-OK "Rootfs-Versionsstempel: $markerVer"
        } else {
            Write-Warn "Rootfs ohne Versionsstempel (Dev-Build?) — das nächste Upgrade importiert erneut."
        }
    } catch {
        Write-Warn "Rootfs-Versionsstempel konnte nicht gelesen werden."
    }
}

# Boot the distro (its wsl.conf [boot] command is /usr/local/bin/start-dockerd.sh,
# NOT systemd — systemd is unreliable on a custom-imported rootfs) and wait for dockerd.
#
# Wait-DockerReady is the SINGLE source of truth for dockerd readiness — VM ping,
# exit-code-only polling (180 s: 60 s was tight on 5400 RPM HDDs and when
# Controlled Folder Access added latency), the start-dockerd.sh fallback (30 s),
# and the stderr->FILE probe that keeps `docker info`'s healthy-daemon warning out
# of the PowerShell streams. Until 2026-07-19 this script kept a full INLINE COPY
# of all of it, differing only by its $lastErr reporting path — two divergent
# implementations of safety-critical readiness logic, one of which nobody would
# remember to fix. The $lastErr path now lives in the helper as -LastError, and
# -ShowProgress reproduces the per-poll German progress line this script printed.
$readyHelper = Join-Path $PSScriptRoot 'wsl_docker_ready.ps1'
if (-not (Test-Path $readyHelper)) {
    Write-FAIL "Die Datei wsl_docker_ready.ps1 fehlt in $PSScriptRoot."
    Write-Host "   Die Installation ist unvollständig. Bitte den EduBotics-Installer erneut ausführen." -ForegroundColor Red
    exit 1
}
. $readyHelper

Write-Step "Starting EduBotics-Umgebung..."
$dockerErr = ""
if (-not (Wait-DockerReady -DistroName $DistroName -LastError ([ref]$dockerErr) -ShowProgress)) {
    Write-FAIL "Docker-Engine konnte nicht gestartet werden."
    Write-Host "   Fehler: $dockerErr" -ForegroundColor Red
    Write-Host "   Diagnose: wsl -d $DistroName -- tail -n 50 /var/log/dockerd.log" -ForegroundColor Red
    exit 1
}

Write-OK "Docker-Engine läuft in $DistroName"

# Sanity: show docker version inside the distro so install logs are useful
try {
    $ver = wsl -d $DistroName -- docker --version 2>&1
    Write-Host "   $ver" -ForegroundColor Gray
} catch { }

Write-Step "EduBotics-Umgebung bereit."

# Explicit success — mirrors pull_images.ps1. Without this, the script's exit
# code falls through to the COSMETIC `docker --version` probe above: under
# EAP=Continue a non-throwing native failure never enters the catch and never
# resets $LASTEXITCODE, and Write-Step is a Write-Host (no effect on it). A
# transient wsl/distro hiccup in that one log line therefore made
# finalize_install.ps1 report "Rootfs-Import fehlgeschlagen" over an import that
# actually succeeded.
exit 0
