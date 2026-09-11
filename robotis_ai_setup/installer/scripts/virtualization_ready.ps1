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
        try {
            $state.FlagTime = (Get-Item -LiteralPath $FlagPath -ErrorAction Stop).LastWriteTime
            $state.BootTime = (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime
            $state.TimeReadable = $true
            # -le, not -lt: a boot stamped at the same second as the flag cannot
            # have happened AFTER it, so treat it as "no boot since".
            $state.NoBootSinceFlag = ($state.BootTime -le $state.FlagTime)
            $notes += ("Neustart-Markierung {0}, letzter Start {1}" -f $state.FlagTime.ToString('s'), $state.BootTime.ToString('s'))
        } catch {
            $state.TimeReadable = $false
            $notes += "(Zeitstempel von Markierung/Systemstart nicht lesbar: $_)"
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
    try {
        $state.HypervisorPresent = (Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop).HypervisorPresent
    } catch {
        $state.CimReadable = $false
        $notes += "(Hypervisor-Status nicht lesbar: $_)"
    }
    try {
        $state.VirtFirmwareEnabled = (Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop |
            Select-Object -First 1).VirtualizationFirmwareEnabled
    } catch {
        $state.CimReadable = $false
        $notes += "(Virtualisierungs-Status der Firmware nicht lesbar: $_)"
    }

    $state.Notes = @($notes)
    return $state
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
# Unknown PROCEEDS (the caller runs the import anyway). Fail-closed would brick
# working machines on a WMI hiccup, and the import classifies its own failure as
# the backstop. The same fail-open-on-no-proof doctrine as
# device_manager::serial_path_family_conflict and the HF namespace guard.
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

# ── Classify a `wsl` failure by its ERROR CODE, never by its message. ──────
# The 2026-09-07 transcript is the proof: the sentence was German
# ("Der Vorgang konnte nicht gestartet werden, da ein erforderliches Feature
# nicht installiert ist.") while the code token beside it was ASCII
# ("Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE").
# Matching the message is the same mistake install_prerequisites.ps1 already
# corrected when it deleted `systeminfo | Select-String "Hyper-V Requirements"`,
# which matched an ENGLISH-ONLY label and therefore never matched on the student
# PCs it was written for.
#
# Returns 'hypervisor' when the text proves the Windows hypervisor / Host
# Compute Service was unavailable, otherwise "". Never throws; a $null or empty
# text is simply unclassified, which keeps the caller's existing wording.
function Get-WslFailureClass {
    param(
        [string]$Text
    )
    if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
    # Match against a WHITESPACE-STRIPPED copy. `Out-String` wraps at the host
    # width, and the field log's own line — „Fehlercode: Wsl/Service/
    # RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE" — is 79
    # characters, i.e. one character from being split through the middle of the
    # token we are looking for. Every token below is whitespace-free, so
    # stripping can only ever rescue a wrapped match; it cannot invent one out
    # of unrelated text. (Callers also pass -Width 4096; this is the belt to
    # that brace, because a caller that forgets it must still classify.)
    #
    # NULs are stripped for the same reason. wsl.exe writes UTF-16LE to a pipe
    # (CRT _O_U16TEXT unless WSL_UTF8=1), and Windows PowerShell 5.1 decodes a
    # native command's bytes in the OEM code page, so every ASCII character of
    # the token arrives followed by a U+0000 that `\s` does not match. The one
    # caller strips them today; executed against that exact byte shape, a copy
    # of the text that still carries them classified as "" — the disk/antivirus
    # triad over a hypervisor fault, i.e. the 2026-09-07 failure — and nothing
    # in the suite noticed when the caller's strip was deleted.
    $flat = ($Text -replace '[\s\x00]', '')
    foreach ($token in @(
        "HCS_E_SERVICE_NOT_AVAILABLE",
        "HCS_E_HYPERV_NOT_INSTALLED",
        "ERROR_VM_NOT_AVAILABLE",
        "0x80370102",
        "0x80370114"
    )) {
        if ($flat -like "*$token*") { return "hypervisor" }
    }
    return ""
}
