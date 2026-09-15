# virtualization_ready.ps1 — Single source of truth for "can this PC run WSL2
# right now, and if not, why". Replaces finalize_install.ps1's former private
# Test-RebootStillPending.
#
# Dot-source it from a caller (do NOT run it standalone):
#     . (Join-Path $PSScriptRoot 'virtualization_ready.ps1')
#     $state = Get-RebootState -FlagPath $flagPath
#     switch (Get-VirtualizationVerdict -State $state) { ... }
#
# WHY THIS FILE EXISTS (2026-09-07 field failure, German school PC):
# finalize_install.ps1 and verify_system.ps1 each had their OWN answer to "is a
# reboot outstanding", and the weaker one guarded the expensive/destructive
# operation. finalize asked the WSL/VMP feature store for "EnablePending";
# verify compared the flag's write time against Win32_OperatingSystem
# .LastBootUpTime — and verify's own comment claimed it did this "exactly like
# finalize's Test-RebootStillPending", which it did not. The two disagree on
# EXACTLY one state, and it is the state a fresh install lands in:
# install_prerequisites.ps1 sets $needsReboot because `wsl --install
# --no-distribution` RAN, after which both features read "Enabled". So finalize
# declared the reboot done, ran `wsl --import`, and got
#     Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE
# forever, across GUI launches, with the student told to check their disk space.
#
# THE DESIGN RULE: stop INFERRING "did they reboot" and ask "is the hypervisor
# LIVE". A reboot is a proxy; HypervisorPresent is the thing the reboot was for.
#
# CALLER CONTRACT
#   * Keep $ErrorActionPreference = "Continue". This file deliberately does NOT
#     assign it — a dot-sourced file assigning EAP would poison its caller, and
#     ci.yml::powershell-native-stderr exists because of that class of bug.
#   * This file makes NO native calls (no wsl, no docker) and contains NO `exit`
#     and NO `throw`: it returns values. A dot-source that can exit the caller
#     is how a diagnostic becomes an outage, and ps-readiness-retry-lint
#     discovers hard-exit helpers cross-file.
#   * Nothing here renders German to the console. Callers render; finalize to
#     the transcript, verify_system to its diagnostics log. Calling a Write-*
#     helper from here would break in whichever caller has not defined it
#     (the lesson wsl_docker_ready.ps1 records).

# ── Facts, gathered once ────────────────────────────────────────────────────
# The ONLY place that touches the flag, the feature store and CIM. Everything
# else in this file is pure policy over this hashtable.
#
# That alone is NOT what makes the two policies below incapable of drifting
# apart — it only settles the STATE. Each policy is a ladder over the same three
# PROOFS, and until 2026-09-11 both spelled all three inline, i.e. this file
# carried two copies of every proof while claiming it could not. The proofs are
# now named predicates (Test-DdUninstallOutstanding, Test-FeatureEnablePending,
# Test-NoBootSinceFlag) and each is spelled ONCE; one state reader plus one
# spelling per proof is what closes the seam.
#
# $FlagPath may legitimately NOT EXIST: the verdict's virtualization rungs are
# flag-independent, so callers run this unconditionally rather than inside a
# Test-Path. An absent flag is a fact, not an error, and produces no note.
function Get-RebootState {
    param(
        [string]$FlagPath
    )

    $state = @{
        FlagPresent          = $false
        FlagReason           = ""
        FlagTime             = $null
        BootTime             = $null
        NoBootSinceFlag      = $false
        TimeReadable         = $false
        EnablePending        = $false
        FeatureStoreReadable = $true
        HypervisorPresent    = $null
        VirtFirmwareEnabled  = $null
        CimReadable          = $true
        Notes                = @()
    }
    $notes = @()

    if ($FlagPath) {
        try {
            $state.FlagPresent = (Test-Path -LiteralPath $FlagPath)
        } catch {
            $state.FlagPresent = $false
        }
    }

    # The last boot, read ONCE and whether or not a flag exists: the flag-time
    # proof below compares against it, and so does the restart record
    # (Test-RestartDidNotHelp), which must also judge an EnablePending verdict
    # that has no flag at all. Only a real [datetime] counts — a $null or an
    # unreadable CIM leaves BootTime $null, which proves nothing anywhere.
    $bootError = "LastBootUpTime leer"
    try {
        $bootRead = (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime
        if ($bootRead -is [datetime]) { $state.BootTime = $bootRead }
    } catch {
        $bootError = "$_"
    }

    # The flag CONTENT names WHY the reboot was requested. "dd-uninstall" is
    # migrate_from_docker_desktop.ps1's reason (Docker Desktop's uninstaller
    # returned 3010 — its removal completes on the next boot);
    # install_prerequisites.ps1 writes a bare "1". The feature store is BLIND to
    # a pending DD removal (WSL/VMP read Enabled throughout it), which is why the
    # reason has to be carried in the flag and settled on TIME.
    if ($state.FlagPresent) {
        try {
            $first = (Get-Content -LiteralPath $FlagPath -TotalCount 1 -ErrorAction Stop)
            if ($null -ne $first) { $state.FlagReason = ([string]$first).Trim() }
        } catch {
            $notes += "(Neustart-Grund nicht lesbar: $_)"
        }
        $timeError = $bootError
        try {
            $state.FlagTime = (Get-Item -LiteralPath $FlagPath -ErrorAction Stop).LastWriteTime
        } catch {
            $state.FlagTime = $null
            $timeError = "$_"
        }
        # BOTH times must be real [datetime]s. The old single try got a $null
        # LastBootUpTime right only because the note's .ToString('s') threw
        # inside it; the explicit type test says the same thing on purpose.
        if (($state.FlagTime -is [datetime]) -and ($state.BootTime -is [datetime])) {
            $state.TimeReadable = $true
            # -le, not -lt: a boot stamped at the same second as the flag cannot
            # have happened AFTER it, so treat it as "no boot since".
            $state.NoBootSinceFlag = ($state.BootTime -le $state.FlagTime)
            $notes += ("Neustart-Markierung {0}, letzter Start {1}" -f $state.FlagTime.ToString('s'), $state.BootTime.ToString('s'))
        } else {
            $state.TimeReadable = $false
            $notes += "(Zeitstempel von Markierung/Systemstart nicht lesbar: $timeError)"
        }
    }

    # EnablePending is DIRECT PROOF that a feature enable is waiting on a
    # reboot. It is the only signal the old finalize looked at, and it stays the
    # FIRST virtualization-independent rung below — never demote it.
    foreach ($feature in @("VirtualMachinePlatform", "Microsoft-Windows-Subsystem-Linux")) {
        try {
            $featureState = (Get-WindowsOptionalFeature -Online -FeatureName $feature -ErrorAction Stop).State
            $notes += "$feature = $featureState"
            if ($featureState -eq "EnablePending") { $state.EnablePending = $true }
        } catch {
            $state.FeatureStoreReadable = $false
            $notes += "($feature-Status nicht lesbar: $_)"
        }
    }

    # HypervisorPresent is $true whenever a hypervisor (Hyper-V / WSL2's VMP) is
    # actually RUNNING — i.e. whenever the Host Compute Service can start, which
    # is precisely what HCS_E_SERVICE_NOT_AVAILABLE says it could not.
    # VirtualizationFirmwareEnabled reflects the BIOS/UEFI setting, and a RUNNING
    # hypervisor commonly MASKS it to $false — which is why the verdict may only
    # consult it BELOW the HypervisorPresent rung. Keep both reads here and the
    # ordering rule there.
    #
    # Both values are WRITTEN INTO THE NOTES as well as the state: a transcript
    # that ends in an exit-11 remedy must show the facts the remedy was chosen
    # from (Get-HypervisorRemedyKind below), or support cannot tell a BIOS
    # setting from a stopped service.
    try {
        $state.HypervisorPresent = (Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop).HypervisorPresent
        $notes += ("HypervisorPresent = {0}" -f (Format-RebootFact $state.HypervisorPresent))
    } catch {
        $state.CimReadable = $false
        $notes += "(Hypervisor-Status nicht lesbar: $_)"
    }
    try {
        $state.VirtFirmwareEnabled = (Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop |
            Select-Object -First 1).VirtualizationFirmwareEnabled
        $notes += ("VirtualizationFirmwareEnabled = {0}" -f (Format-RebootFact $state.VirtFirmwareEnabled))
    } catch {
        $state.CimReadable = $false
        $notes += "(Virtualisierungs-Status der Firmware nicht lesbar: $_)"
    }

    # The two services WSL2 needs to start a VM. EVIDENCE ONLY — no rung reads
    # them: a Stopped service with StartType Manual is the healthy idle state of
    # vmcompute (it is trigger-started), so a status is not a verdict. What they
    # buy is a transcript that shows a DISABLED service instead of leaving
    # support to guess (Get-Service's StartType is an enum, i.e. locale-free).
    foreach ($svc in @("vmcompute", "WslService")) {
        try {
            $s = Get-Service -Name $svc -ErrorAction Stop
            $notes += ("Dienst {0} = {1} ({2})" -f $svc, $s.Status, $s.StartType)
        } catch {
            $notes += "(Dienst $svc nicht lesbar: $_)"
        }
    }

    $state.Notes = @($notes)
    return $state
}

# One rendering for a CIM fact in the notes: $null is not $false, and a note
# that printed an empty string for it would hide exactly that difference.
function Format-RebootFact {
    param($Value)
    if ($null -eq $Value) { return "(leer)" }
    return [string]$Value
}

# ── The three proofs, each spelled ONCE ─────────────────────────────────────
# The two policies below are ladders over the SAME three proofs. Until
# 2026-09-11 each policy spelled all three INLINE — two copies of every proof,
# in the one file whose header claims its policies are "incapable of drifting
# apart again". That claim was true of the STATE (Get-RebootState is the only
# reader of the flag, the feature store and CIM) and false of the PROOFS: a
# future edit to one copy would not have followed on the other, which is the
# exact defect class this file exists to eliminate, one scope smaller.
#
# The CALLER CONTRACT in the header governs every function here, not only the
# two the callers name: each predicate is pure over the hashtable, tolerates a
# $null state by answering $false, makes no native calls, and contains no
# `exit`, no `throw` and no German.
#
# A pending Docker-Desktop removal. The feature store is BLIND to it (WSL/VMP
# read Enabled throughout), so the flag's REASON plus "no boot since it was
# written" is the only evidence there is.
function Test-DdUninstallOutstanding {
    param(
        [hashtable]$State
    )
    if ($null -eq $State) { return $false }
    if ($State.FlagReason -eq "dd-uninstall" -and $State.TimeReadable -and $State.NoBootSinceFlag) {
        return $true
    }
    return $false
}

# EnablePending is DIRECT PROOF that a feature enable is waiting on a reboot. It
# is the only signal the old finalize looked at, and it stays the FIRST
# virtualization-independent rung of the verdict — never demote it.
function Test-FeatureEnablePending {
    param(
        [hashtable]$State
    )
    if ($null -eq $State) { return $false }
    if ($State.EnablePending) { return $true }
    return $false
}

# The flag is present and nothing has booted since it was written. An INFERENCE,
# not proof of anything mechanical: Windows Fast Startup makes LastBootUpTime an
# unreliable proxy, which is why the verdict ranks this BELOW ground truth while
# verify_system is content with it.
function Test-NoBootSinceFlag {
    param(
        [hashtable]$State
    )
    if ($null -eq $State) { return $false }
    if ($State.FlagPresent -and $State.TimeReadable -and $State.NoBootSinceFlag) { return $true }
    return $false
}

# ── Policy 1: is a reboot outstanding? Proof only. ──────────────────────────
# Consumed by verify_system.ps1, to downgrade an expected FAIL to a benign
# "Neustart steht noch aus".
#
# Get-VirtualizationVerdict does NOT call this. It SHARES the three predicates
# above instead, because its ground-truth rung must sit BETWEEN proof 2
# (EnablePending) and proof 3 (no boot since the flag): a single boolean cannot
# express a ladder that has another rung in the middle of it. So the two
# policies compose the same proofs in different orders — what neither may ever
# do again is re-spell a proof.
#
# THE ONE INTENDED DIVERGENCE, deliberate and not drift: when the hypervisor is
# LIVE and a reboot is still outstanding, this returns $true (verify_system
# reports the benign "Neustart steht noch aus") while the verdict returns
# "Ready" (finalize may import, because ground truth beats the proxy). Both
# answers are correct about their own question — "is a reboot still owed?" and
# "can this PC run WSL2 right now?" — and collapsing them into one answer is
# how the 2026-09-07 failure happened in the first place.
#
# Returns $true ONLY on proof; every unreadable state returns $false, which is
# verify_system's documented SAFE direction (manufacturing a benign verdict out
# of an unknown state is how the Audit-H23 regression survived).
function Test-RebootOutstanding {
    param(
        [hashtable]$State
    )
    if ($null -eq $State) { return $false }
    if (Test-DdUninstallOutstanding -State $State) { return $true }
    if (Test-FeatureEnablePending -State $State) { return $true }
    if (Test-NoBootSinceFlag -State $State) { return $true }
    return $false
}

# ── Policy 2: the verdict ladder. ORDER IS LOAD-BEARING. ────────────────────
#   1. Test-DdUninstallOutstanding       -> RebootRequired
#      FIRST, so a live hypervisor cannot short-circuit the half-removed
#      Docker-Desktop guard this proof exists to protect.
#   2. Test-FeatureEnablePending         -> RebootRequired
#      Direct proof. ABOVE the hypervisor rung: a PC running a hypervisor for
#      Hyper-V's own sake would otherwise skip the only check that has ever
#      worked here.
#   3. HypervisorPresent -eq $true       -> Ready
#      GROUND TRUTH, deliberately ABOVE the flag-time rung, which is only an
#      INFERENCE: Windows Fast Startup makes LastBootUpTime an unreliable proxy,
#      and trusting the proxy over ground truth tells a working PC to reboot
#      forever. This rung is why the verdict cannot delegate to
#      Test-RebootOutstanding — it lands BETWEEN two of that boolean's proofs.
#   4. Test-NoBootSinceFlag              -> RebootRequired
#   5. VirtFirmwareEnabled -eq $false    -> VirtualizationDisabled
#      Sound ONLY here, below rung 3 — see the masking note in Get-RebootState.
#      `$null -eq $false` is $false in PowerShell, so an absent/unreadable
#      property can never produce this verdict; only a genuine $false does.
#   6. anything else, or any CIM/WMI error -> Unknown
#
# Unknown AND VirtualizationDisabled both PROCEED (finalize runs the import
# anyway). Fail-closed would brick working machines on a WMI hiccup — and a CIM
# property that misreports would brick them on EVERY launch, with no way past
# it. The import classifies its own failure; that failure, not a CIM read, is
# what exits 11. VirtualizationDisabled survives as a word because preflight
# warns on it and it names the likely remedy. The same fail-open-on-no-proof
# doctrine as device_manager::serial_path_family_conflict and the HF namespace
# guard. Only the RebootRequired rungs stop an import, and each asks for ONE
# restart: when a real restart has already happened and the same rung still
# fires, Test-RestartDidNotHelp below says so and finalize stops asking.
function Get-VirtualizationVerdict {
    param(
        [hashtable]$State
    )
    if ($null -eq $State) { return "Unknown" }

    if (Test-DdUninstallOutstanding -State $State) { return "RebootRequired" }
    if (Test-FeatureEnablePending -State $State) { return "RebootRequired" }
    if ($State.HypervisorPresent -eq $true) { return "Ready" }
    if (Test-NoBootSinceFlag -State $State) { return "RebootRequired" }
    if ($State.VirtFirmwareEnabled -eq $false) { return "VirtualizationDisabled" }
    return "Unknown"
}

# ── Did a real restart already fail to settle a RebootRequired verdict? ─────
# A RebootRequired verdict asks for a restart. When that restart happens and the
# SAME rung fires again, asking for another one is a loop, not a remedy: the
# 2026-09-15 review replayed a PC whose `wsl --status` fails on every boot, so
# finalize's Phase 0 re-runs `wsl --install --no-distribution` (exit 0), which
# refreshes the flag IN THIS BOOT, so rung 4 fires after every restart — exit 10,
# forever, each time with „bitte neu starten".
#
# finalize records the boot it asked in and the rung that asked
# (.restart_requested next to the flag, content from Format-RestartRequest). The
# functions below read that record and answer whether the restart it asked for
# has happened while the same reason is still there. They are pure over the
# state + the record, like the proofs above; the WRITE is finalize's.
#
# "A restart happened" = LastBootUpTime moved FORWARD by more than 60 seconds.
#   * Fast Startup („Herunterfahren") does not move LastBootUpTime, so a shutdown
#     is correctly NOT a restart and the student is asked again.
#   * The tolerance absorbs a clock step within one boot (LastBootUpTime is
#     derived as now - uptime, so a time sync moves it). A real restart moves it
#     by at least the time the PC had been up when finalize asked, plus the
#     restart itself — finalize ran elevated with a `wsl --status` probe and the
#     verdict by then, so that is well over a minute.
#   * The rung must be the SAME: a restart that settled one reason and uncovered
#     another (a Docker-Desktop removal, then the WSL install) is progress and
#     gets its one restart too.
# A PC that REVERTS on restart (PC-Wächter, Deep Freeze, UWF) reverts this record
# as well, so this does NOT bound that loop.
function Get-RebootRequiredReason {
    param(
        [hashtable]$State
    )
    # The RebootRequired rungs of Get-VirtualizationVerdict, in its order, as
    # names. Meaningful ONLY for a state whose verdict IS RebootRequired: rung 3
    # (a live hypervisor) sits between proofs 2 and 3 and is not asked here.
    if ($null -eq $State) { return "" }
    if (Test-DdUninstallOutstanding -State $State) { return "DdUninstall" }
    if (Test-FeatureEnablePending -State $State) { return "EnablePending" }
    if (Test-NoBootSinceFlag -State $State) { return "NoBootSinceFlag" }
    return ""
}

# The record's content, spelled once for the one writer (finalize) and the
# reader below. Ticks of the UTC boot time: no locale, no date format to parse.
function Format-RestartRequest {
    param(
        [datetime]$BootTime,
        [string]$Reason
    )
    return ("boot={0}`nreason={1}" -f $BootTime.ToUniversalTime().Ticks, $Reason)
}

# @{ Boot = [datetime] (UTC); Reason = [string] }, or $null for an absent,
# unreadable, partial or malformed record — which proves no earlier request.
function Read-RestartRequest {
    param(
        [string]$Path
    )
    if (-not $Path) { return $null }
    $lines = @()
    try {
        if (-not (Test-Path -LiteralPath $Path)) { return $null }
        $lines = @(Get-Content -LiteralPath $Path -ErrorAction Stop)
    } catch {
        return $null
    }
    $ticks = $null
    $reason = ""
    foreach ($line in $lines) {
        $l = ([string]$line).Trim()
        if ($l -like 'boot=*') {
            $parsed = [long]0
            if ([long]::TryParse($l.Substring(5), [ref]$parsed)) { $ticks = $parsed }
        } elseif ($l -like 'reason=*') {
            $reason = $l.Substring(7)
        }
    }
    if (($null -eq $ticks) -or (-not $reason)) { return $null }
    try {
        $boot = [datetime]::new($ticks, [System.DateTimeKind]::Utc)
    } catch {
        return $null
    }
    return @{ Boot = $boot; Reason = $reason }
}

# $true ONLY on proof: a readable record for the SAME reason, a readable boot
# time, and that boot time more than 60 s after the recorded one. Every
# unreadable state answers $false, i.e. „ask for the restart" — the behaviour
# before this existed.
function Test-RestartDidNotHelp {
    param(
        [hashtable]$State,
        [hashtable]$Request,
        [string]$Reason
    )
    if (($null -eq $State) -or ($null -eq $Request)) { return $false }
    if (-not $Reason) { return $false }
    if ($Request.Reason -ne $Reason) { return $false }
    if (-not ($State.BootTime -is [datetime])) { return $false }
    if (-not ($Request.Boot -is [datetime])) { return $false }
    $moved = ($State.BootTime.ToUniversalTime() - $Request.Boot.ToUniversalTime()).TotalSeconds
    if ($moved -gt 60) { return $true }
    return $false
}

# ── Classify a `wsl` failure by its STAGE and CODE, never by its message ──────
# The 2026-09-07 transcript is the proof: the sentence was German
# ("Der Vorgang konnte nicht gestartet werden, da ein erforderliches Feature
# nicht installiert ist.") while the code token beside it was ASCII
# ("Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE").
# Matching the message is the same mistake install_prerequisites.ps1 already
# corrected when it deleted `systeminfo | Select-String "Hyper-V Requirements"`,
# which matched an ENGLISH-ONLY label and therefore never matched on the student
# PCs it was written for.
#
# WSL prints a CONTEXTUALIZED path: the execution contexts that were active,
# then the error (microsoft/WSL src/windows/common/ExecutionContext.h — Service,
# RegisterDistro, CreateInstance, AttachDisk, CreateVm, MountDisk, HCS, ...;
# builds before 2.5 also printed MountVhd). The STAGE says what failed, the last
# segment says how:
#   Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE   (field)
#   Wsl/Service/CreateInstance/CreateVm/HCS_E_CONNECTION_TIMEOUT          (#12094)
#   Wsl/Service/CreateInstance/MountVhd/HCS/E_ACCESSDENIED                (#11323)
#   Wsl/Service/CreateInstance/CreateVm/MountVhd/HCS/0x80070032           (#11052)
#
# Classes:
#   "hypervisor" — the VM itself could not be started: a CreateVm stage, or one
#                  of the two HCS codes that name it (also in their legacy hex
#                  spellings, which inbox WSL prints without a path).
#   "disk"       — a virtual disk could not be attached (a MountVhd / AttachDisk
#                  / MountDisk stage: a VHDX that is missing, locked by another
#                  program, denied, on unsupported storage or corrupt), or the
#                  disk is full (ERROR_DISK_FULL / ERROR_HANDLE_DISK_FULL in any
#                  stage). A disk stage outranks the CreateVm it sits in: the VM
#                  was being created, but what failed was the disk.
#   ""           — anything else, including no text at all; the caller keeps its
#                  own wording.
# The two named HCS codes win over any stage: they say the hypervisor side is
# what was missing, whatever it was doing at the time.
#
# TWIN: gui/app/wsl_bridge.py::classify_wsl_failure makes the same decision for
# the GUI; tests/test_installer_pwsh_executed.py::ClassifierExecutedTest drives
# both over one corpus. Never throws.
function Get-WslFailureClass {
    param(
        [string]$Text
    )
    if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
    $byCode = Get-WslCodeClass -Code (Get-WslFailureCode -Text $Text)
    if ($byCode) { return $byCode }
    # Stages are matched on a whitespace/NUL-stripped, upper-cased copy WITH
    # their slashes: a segment name is only ever followed by another segment,
    # so a wrapped line cannot glue anything onto it.
    $flat = ($Text -replace '[\s\x00]', '').ToUpperInvariant()
    foreach ($stage in @("/MOUNTVHD/", "/ATTACHDISK/", "/MOUNTDISK/")) {
        if ($flat.Contains($stage)) { return "disk" }
    }
    if ($flat.Contains("/CREATEVM/")) { return "hypervisor" }
    return ""
}

# The class a CODE alone proves, or "": the two HCS codes that name the
# hypervisor, and a full disk. Shared by Get-WslFailureClass and by
# Get-HypervisorRemedyKind for a caller that passes only a code.
function Get-WslCodeClass {
    param(
        [string]$Code
    )
    if ($Code -in @("HCS_E_SERVICE_NOT_AVAILABLE", "HCS_E_HYPERV_NOT_INSTALLED")) { return "hypervisor" }
    if ($Code -in @("ERROR_DISK_FULL", "0x80070070", "ERROR_HANDLE_DISK_FULL", "0x80070027")) { return "disk" }
    return ""
}

# The error CODE the text carries, normalised (symbolic names upper-case, hex as
# 0x + eight upper-case digits), or "". Used for the remedy family and echoed in
# the German remedy, so a student's screenshot names what wsl said.
#
#   1. The two HCS codes, symbolic or hex, matched on a WHITESPACE- and
#      NUL-STRIPPED copy. `Out-String` wraps at the host width and the field
#      log's own line is 79 characters — one short of splitting the token — and
#      Windows PowerShell 5.1 decodes wsl.exe's UTF-16LE in the OEM code page, so
#      every ASCII character arrives followed by a U+0000 that `\s` does not
#      match. Executed against that byte shape, a copy that kept the NULs
#      classified as "". (Callers also pass -Width 4096 and strip NULs; this is
#      the belt to that brace.)
#   2. Otherwise the LAST segment of the first `Wsl/…` path, read on the
#      NUL-stripped text with its whitespace intact — the last segment is
#      followed by a line break, and a stripped copy would glue the next line
#      onto it. A line wrapped INSIDE such a path yields a truncated code; the
#      callers' -Width 4096 is what prevents that.
#   HCS_E_HYPERV_NOT_INSTALLED  = 0x80370102  "the virtual machine could not be
#       started because a required feature is not installed" — Microsoft's
#       remedy: the Virtual Machine Platform feature and BIOS virtualization.
#   HCS_E_SERVICE_NOT_AVAILABLE = 0x80370114  "the operation could not be
#       started because a required feature is not installed" — the 2026-09-07
#       field code. An `ERROR_VM_NOT_AVAILABLE` entry used to sit in this table;
#       no WSL build or field report emits it, so it is no longer a hypervisor
#       code (a path ending in it is still echoed as its code, unclassified).
function Get-WslFailureCode {
    param(
        [string]$Text
    )
    if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
    $noNul = ($Text -replace '\x00', '')
    $flat = ($noNul -replace '\s', '')
    foreach ($pair in @(
        @("HCS_E_HYPERV_NOT_INSTALLED", "HCS_E_HYPERV_NOT_INSTALLED"),
        @("0x80370102", "HCS_E_HYPERV_NOT_INSTALLED"),
        @("HCS_E_SERVICE_NOT_AVAILABLE", "HCS_E_SERVICE_NOT_AVAILABLE"),
        @("0x80370114", "HCS_E_SERVICE_NOT_AVAILABLE")
    )) {
        if ($flat -like ("*" + $pair[0] + "*")) { return $pair[1] }
    }
    $m = [regex]::Match($noNul, '(?<![A-Za-z0-9_])[Ww][Ss][Ll][Gg]?/(?:[A-Za-z0-9_]+/)*([A-Za-z0-9_]+)')
    if (-not $m.Success) { return "" }
    $raw = $m.Groups[1].Value
    if ($raw -match '^0[xX][0-9A-Fa-f]{8}$') { return ("0x" + $raw.Substring(2).ToUpperInvariant()) }
    return $raw.ToUpperInvariant()
}

# ── Which remedy fits a failure to start WSL2 — from PROOF, never a default ──
# Returns "Disk", "Firmware", "Feature", "Service" or "Unclassified". Pure over
# the state, the code and the class; the German text for each kind lives in the
# callers (see the header contract).
#
#   Disk         — wsl itself named a disk stage or a full disk. wsl's own report
#                  outranks a CIM reading: it got far enough to attach a disk.
#   Firmware     — CIM read HypervisorPresent = $false AND VirtualizationFirmware-
#                  Enabled = $false: Windows itself says the BIOS/UEFI setting is
#                  off, and no hypervisor is running to mask that property. Both
#                  must be a genuine $false; $null (unreadable) proves nothing.
#   Feature      — wsl reported HCS_E_HYPERV_NOT_INSTALLED (0x80370102), whose
#                  documented causes are the VM-Plattform feature and BIOS
#                  virtualization.
#   Service      — any other hypervisor-class failure, the 2026-09-07
#                  HCS_E_SERVICE_NOT_AVAILABLE included: restart first, then
#                  vmcompute / hypervisorlaunchtype / BIOS for IT.
#   Unclassified — the VM did not start and wsl named nothing known. The words
#                  for it claim no cause.
# -FailureClass is what import classified; when a caller passes only a code, the
# code is classified on its own (a bare code carries no stage).
#
# It only chooses WORDS. Nothing gates on it.
function Get-HypervisorRemedyKind {
    param(
        [hashtable]$State,
        [string]$FailureCode = "",
        [string]$FailureClass = ""
    )
    $class = $FailureClass
    if ((-not $class) -and $FailureCode) { $class = Get-WslCodeClass -Code $FailureCode }
    if ($class -eq "disk") { return "Disk" }
    if (($null -ne $State) -and ($State.HypervisorPresent -eq $false) -and
            ($State.VirtFirmwareEnabled -eq $false)) {
        return "Firmware"
    }
    if ($FailureCode -eq "HCS_E_HYPERV_NOT_INSTALLED") { return "Feature" }
    if ($class -eq "hypervisor") { return "Service" }
    return "Unclassified"
}
