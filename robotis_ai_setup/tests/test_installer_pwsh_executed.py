"""Installer PowerShell, EXECUTED under pwsh — not grepped.

Every other test of these scripts reads their SOURCE. That is how the
2026-09-07 defect survived: two readable, reasonable-looking reboot predicates
that disagreed on exactly one state. This module runs the real functions and
the real scripts under pwsh with the Windows-only surfaces mocked, and asserts
on what they DO:

  * VerdictExecutedTest     — virtualization_ready.ps1's Get-RebootState,
    Get-VirtualizationVerdict and Test-RebootOutstanding over the reachable
    state space (mocked Get-WindowsOptionalFeature / Get-CimInstance — PowerShell
    resolves a function before a cmdlet of the same name — plus a real flag file
    whose mtime is set), against a literal table of named states AND the ladder
    exactly as PR #28 / CLAUDE.md state it.
  * ClassifierExecutedTest  — Get-WslFailureClass over real WSL error codes,
    including the byte shape Windows PowerShell 5.1 hands it (wsl.exe writes
    UTF-16LE to a pipe; 5.1 decodes native bytes in the OEM code page).
  * FinalizeEndToEndTest    — finalize_install.ps1 → import_edubotics_wsl.ps1
    with a FAKE wsl executable first on PATH (UTF-16LE on stdout, exit -1 like
    the real one): exit codes, whether `wsl --import` was ever invoked, the
    marker, the flag's content and mtime, the echoed transcript.
  * RemedyKindExecutedTest  — Get-HypervisorRemedyKind: the exit-11 words follow
    the CIM proof + wsl's code; the evidence reaches the notes.
  * DistroRegistrationExecutedTest — wsl_distro_state.ps1's Registered / Absent
    / Unresponsive over its input space, cell-by-cell against the GUI's Python
    twin (wsl_bridge.resolve_distro_registration).
  * PrerequisitesExecutedTest — install_prerequisites.ps1 keeps THIS run's reboot
    reason apart from a preserved earlier flag.
  * PreflightExecutedTest   — preflight_system.ps1's WSL2 line asks the same
    verdict; a WSL that does not answer is not a missing distro.

WHERE IT RUNS. ci.yml's python-tests job runs `unittest discover -s tests` on
ubuntu-latest, and the GitHub Ubuntu 24.04 runner image ships PowerShell 7.x
(`pwsh`), so this module EXECUTES in CI with no workflow change. Locally it
uses `pwsh` from PATH or $EDUBOTICS_PWSH, and SKIPS when neither exists — but
under GitHub Actions a missing pwsh FAILS instead, so the executed coverage
cannot silently evaporate if the runner image changes.

WHAT IT CANNOT PROVE. pwsh 7 is not Windows PowerShell 5.1: pure logic
(ladders, comparisons, string classification, exit-code plumbing) executed here
is evidence; console-encoding and stream behaviour of 5.1 is not, and is only
SIMULATED at the byte level below. The finalize scenarios are skipped on
Windows, where a real wsl.exe would shadow the fake.

TWO host-only substitutions are applied to COPIES of the scripts, each asserted
to find its target so a rename fails loudly instead of testing something else:
finalize's cosmetic „Elevated:" transcript line calls
WindowsIdentity.GetCurrent(), which throws PlatformNotSupportedException off
Windows and would end the script before any logic runs; and
install_prerequisites.ps1's Windows-build gate reads OSVersion, which is not a
Windows build number off Windows.
"""

import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_TESTS, "..", "installer", "scripts"))
_VIRT_PS1 = os.path.join(_SCRIPTS, "virtualization_ready.ps1")


def _find_pwsh():
    explicit = os.environ.get("EDUBOTICS_PWSH")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    return shutil.which("pwsh")


PWSH = _find_pwsh()
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


# ── PowerShell mocks, shared by every harness ──────────────────────────────
# Read the scenario from $env:MOCK_* AT CALL TIME, so one pwsh process can walk
# a whole state space by reassigning the variables. Every comparison against a
# sentinel string is type-guarded first: `$true -eq "throws"` is $true in
# PowerShell (the string is coerced to [bool]), which is the very trap class
# the verdict's own `-eq $true` / `-eq $false` forms exist to avoid.
_MOCKS_PS = r'''
function Get-WindowsOptionalFeature {
    [CmdletBinding()]
    param([switch]$Online, [string]$FeatureName)
    $spec = [string]$env:MOCK_FEAT
    if ($spec -eq "throws") { throw [System.Runtime.InteropServices.COMException]::new("Klasse nicht registriert (mock)") }
    $state = "Enabled"
    foreach ($pair in ($spec -split ";")) {
        $kv = $pair -split "=", 2
        if ($kv.Count -eq 2 -and $kv[0] -eq $FeatureName) { $state = $kv[1] }
    }
    return [pscustomobject]@{ FeatureName = $FeatureName; State = $state }
}
function Get-CimInstance {
    [CmdletBinding()]
    param([Parameter(Position = 0)][string]$ClassName, [string]$Namespace, [string]$Filter)
    switch ($ClassName) {
        "Win32_OperatingSystem" {
            $b = [string]$env:MOCK_BOOT
            if ($b -eq "throws") { throw "WMI-Anbieterfehler (mock)" }
            if ($b -eq "null") { return [pscustomobject]@{ LastBootUpTime = $null } }
            return [pscustomobject]@{ LastBootUpTime = [datetime]::Parse($b); Caption = "Windows (mock)" }
        }
        "Win32_ComputerSystem" {
            switch ([string]$env:MOCK_HV) {
                "throws" { throw "Zugriff verweigert (mock)" }
                "true"   { return [pscustomobject]@{ HypervisorPresent = $true } }
                "false"  { return [pscustomobject]@{ HypervisorPresent = $false } }
                default  { return [pscustomobject]@{ HypervisorPresent = $null } }
            }
        }
        "Win32_Processor" {
            switch ([string]$env:MOCK_VFE) {
                "throws" { throw "Ungültige Klasse (mock)" }
                "true"   { return [pscustomobject]@{ VirtualizationFirmwareEnabled = $true } }
                "false"  { return [pscustomobject]@{ VirtualizationFirmwareEnabled = $false } }
                default  { return [pscustomobject]@{ VirtualizationFirmwareEnabled = $null } }
            }
        }
        default { throw "unmocked CIM class $ClassName" }
    }
}
# Windows semantics for the dot-named flag file: on Unix a leading dot makes
# Get-Item skip it unless -Force; on Windows ".reboot_required" is not hidden.
function Get-Item {
    [CmdletBinding()]
    param([Parameter(Position = 0)][string[]]$Path, [string[]]$LiteralPath)
    Microsoft.PowerShell.Management\Get-Item @PSBoundParameters -Force
}
function Get-Volume { [CmdletBinding()] param([string]$DriveLetter) return [pscustomobject]@{ SizeRemaining = [int64]100GB } }
function Get-PnpDevice { [CmdletBinding()] param([switch]$PresentOnly) return $null }
# MOCK_SVC = "vmcompute=Stopped/Manual;WslService=Running/Automatic" (unset or
# "throws" -> every lookup throws, like a service that does not exist).
function Get-Service {
    [CmdletBinding()]
    param([Parameter(Position = 0)][string[]]$Name)
    $spec = [string]$env:MOCK_SVC
    foreach ($pair in ($spec -split ";")) {
        $kv = $pair -split "=", 2
        if ($kv.Count -eq 2 -and $kv[0] -eq $Name[0]) {
            $p = $kv[1] -split "/", 2
            return [pscustomobject]@{ Name = $Name[0]; Status = $p[0]; StartType = $p[1] }
        }
    }
    throw "Dienst $($Name[0]) wurde nicht gefunden (mock)"
}
# The per-user WSL registration store. Only HKCU: paths are mocked, and only when
# MOCK_LXSS is set: "missing" (no Lxss key = nothing ever registered),
# "throws" (unreadable), "none" (key present, no distros) or "Name1;Name2".
# Unset -> the real cmdlet, which off Windows has no HKCU: drive = unreadable.
function Get-ChildItem {
    [CmdletBinding()]
    param([Parameter(Position = 0)][string[]]$Path, [string[]]$LiteralPath, [string]$Filter)
    $target = @(@($LiteralPath) + @($Path) | Where-Object { $_ }) | Select-Object -First 1
    if (([string]$target -like 'HKCU:*') -and ($null -ne $env:MOCK_LXSS)) {
        switch ([string]$env:MOCK_LXSS) {
            "missing" { throw [System.Management.Automation.ItemNotFoundException]::new("Pfad nicht gefunden (mock)") }
            "throws"  { throw [System.UnauthorizedAccessException]::new("Zugriff verweigert (mock)") }
            "none"    { return @() }
            default {
                $out = @()
                foreach ($n in ([string]$env:MOCK_LXSS -split ";")) {
                    if (-not $n) { continue }
                    $o = [pscustomobject]@{ DistroNameForMock = $n }
                    $o | Add-Member -MemberType ScriptMethod -Name GetValue -Value { param($v) $this.DistroNameForMock }
                    $out += $o
                }
                return $out
            }
        }
    }
    Microsoft.PowerShell.Management\Get-ChildItem @PSBoundParameters
}
'''


class _PwshCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="edubotics-pwsh-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def setUp(self):
        if PWSH is None:
            if IN_CI:
                self.fail("pwsh is not on PATH on this GitHub runner — the "
                          "executed installer tests would silently skip. The "
                          "ubuntu-latest image ships PowerShell; if that changed, "
                          "install it in ci.yml rather than losing this coverage.")
            self.skipTest("pwsh not installed (set EDUBOTICS_PWSH to a pwsh binary)")

    def _ps(self, name, body, *args, env=None, timeout=180):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write(body)
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", path, *args],
                              capture_output=True, env=e, timeout=timeout)


# ═══════════════════════════════════════════════════════════════════════════
class VerdictExecutedTest(_PwshCase):
    """The ladder, executed. Order is the design, so it is asserted by outcome."""

    _FLAG_TIME = "2026-09-07T14:12:40"
    _BOOT = {"boot_before": "2026-09-07T13:42:40", "boot_after": "2026-09-07T14:17:40",
             "boot_equal": "2026-09-07T14:12:40", "os_throws": "throws", "boot_null": "null"}

    # One pwsh process walks every state; each record is one JSON line. `param`
    # must be a script's first statement, so the mocks follow it.
    _MATRIX_PS = r'''
param([string]$Helper, [string]$WorkDir, [string]$OutFile, [string]$FlagTime, [string]$Cases)
''' + _MOCKS_PS + r'''
$ErrorActionPreference = "Continue"
. $Helper
# NOT a dotfile here: Get-RebootState takes the path as a parameter, and this
# harness exercises it, not the filename.
$flagPath = Join-Path $WorkDir "reboot_required.flag"
$sw = [System.IO.StreamWriter]::new($OutFile, $false, [System.Text.UTF8Encoding]::new($false))
foreach ($line in [System.IO.File]::ReadAllLines($Cases)) {
    $c = $line | ConvertFrom-Json
    if (Test-Path -LiteralPath $flagPath) { Remove-Item -LiteralPath $flagPath -Force }
    switch ($c.flag) {
        "1"            { [System.IO.File]::WriteAllText($flagPath, "1`r`n", [System.Text.Encoding]::ASCII) }
        "dd-uninstall" { [System.IO.File]::WriteAllText($flagPath, "dd-uninstall`r`n", [System.Text.Encoding]::ASCII) }
        "dd-utf16"     { [System.IO.File]::WriteAllText($flagPath, "dd-uninstall`r`n", [System.Text.Encoding]::Unicode) }
        "dd-spaced"    { [System.IO.File]::WriteAllText($flagPath, "  dd-uninstall  `r`n", [System.Text.Encoding]::ASCII) }
        "empty"        { [System.IO.File]::WriteAllBytes($flagPath, [byte[]]@()) }
    }
    if ($c.flag -ne "absent") { (Get-Item -LiteralPath $flagPath).LastWriteTime = [datetime]::Parse($FlagTime) }
    $env:MOCK_BOOT = $c.boot; $env:MOCK_FEAT = $c.feat; $env:MOCK_HV = $c.hv; $env:MOCK_VFE = $c.vfe
    $st = Get-RebootState -FlagPath $flagPath
    $v = Get-VirtualizationVerdict -State $st
    $o = Test-RebootOutstanding -State $st
    $sw.WriteLine((@{ id = $c.id; verdict = $v; outstanding = $o; vtype = $v.GetType().Name; otype = $o.GetType().Name } | ConvertTo-Json -Compress))
}
$sw.Close()
'''

    @staticmethod
    def _expected(flag, time_, feat, hv, vfe):
        """The ladder as PR #28 and CLAUDE.md state it — rung by rung. This is a
        SPECIFICATION, deliberately not derived from the .ps1: an edit to the
        ladder fails here until the specification is changed on purpose."""
        present = flag != "absent"
        clock_read = present and time_ in ("boot_before", "boot_after", "boot_equal")
        no_boot = clock_read and time_ != "boot_after"      # -le: same second = no boot since
        dd = flag in ("dd-uninstall", "dd-utf16", "dd-spaced")
        pending = feat in ("VirtualMachinePlatform=EnablePending",
                           "Microsoft-Windows-Subsystem-Linux=EnablePending")
        outstanding = (dd and no_boot) or pending or (present and no_boot)
        if dd and no_boot:
            verdict = "RebootRequired"            # 1: a live hypervisor must not short-circuit it
        elif pending:
            verdict = "RebootRequired"            # 2: direct proof, above ground truth
        elif hv == "true":
            verdict = "Ready"                     # 3: ground truth, above the flag-time inference
        elif present and no_boot:
            verdict = "RebootRequired"            # 4
        elif vfe == "false":
            verdict = "VirtualizationDisabled"    # 5: only a genuine $false; only below 3
        else:
            verdict = "Unknown"                   # 6: proceeds
        return verdict, outstanding

    # (name, flag, boot, feat, hv, vfe) -> (verdict, outstanding). Literal rows:
    # the states that matter, readable without the rule function above.
    NAMED = [
        # The 2026-09-07 state: install_prerequisites wrote "1" moments ago, no
        # boot since, both features Enabled, hypervisor NOT running, BIOS on.
        ("field_block2",               "1", "boot_before", "", "false", "true",   ("RebootRequired", True)),
        ("field_block2_cim_unreadable", "1", "boot_before", "", "throws", "throws", ("RebootRequired", True)),
        ("rebooted_hypervisor_down",   "1", "boot_after", "", "false", "true",    ("Unknown", False)),
        ("rebooted_bios_off",          "1", "boot_after", "", "false", "false",   ("VirtualizationDisabled", False)),
        ("live_hypervisor_masks_vfe",  "absent", "boot_after", "", "true", "false", ("Ready", False)),
        ("dd_pending_hv_live",         "dd-uninstall", "boot_before", "", "true", "true", ("RebootRequired", True)),
        ("dd_utf16_flag_still_read",   "dd-utf16", "boot_before", "", "true", "true", ("RebootRequired", True)),
        ("enable_pending_hv_live",     "absent", "boot_after", "VirtualMachinePlatform=EnablePending", "true", "true", ("RebootRequired", True)),
        # Fast Startup: a fresh flag but a LIVE hypervisor. The ONE intended
        # divergence between the two policies, asserted positively.
        ("fresh_flag_hv_live",         "1", "boot_before", "", "true", "true",    ("Ready", True)),
        ("feature_store_throws_hv_live", "1", "boot_before", "throws", "true", "true", ("Ready", True)),
        ("no_flag_cim_null",           "absent", "boot_before", "", "null", "null", ("Unknown", False)),
        ("clock_unreadable",           "1", "os_throws", "", "false", "true",     ("Unknown", False)),
        ("boot_time_null_is_no_proof", "1", "boot_null", "", "false", "true",     ("Unknown", False)),
        ("boot_same_second_as_flag",   "1", "boot_equal", "", "false", "true",    ("RebootRequired", True)),
        ("vfe_null_is_not_false",      "absent", "boot_after", "", "false", "null", ("Unknown", False)),
    ]

    def _run_cases(self, cases):
        work = tempfile.mkdtemp(dir=self.tmp)
        cf = os.path.join(work, "cases.jsonl")
        with open(cf, "w", encoding="utf-8") as fh:
            for c in cases:
                fh.write(json.dumps(c) + "\n")
        out = os.path.join(work, "out.jsonl")
        r = self._ps("matrix.ps1", self._MATRIX_PS, "-Helper", _VIRT_PS1, "-WorkDir", work,
                     "-OutFile", out, "-FlagTime", self._FLAG_TIME, "-Cases", cf)
        self.assertTrue(os.path.isfile(out), r.stderr.decode("utf-8", "replace")[-2000:])
        with open(out, encoding="utf-8") as fh:
            return {rec["id"]: rec for rec in map(json.loads, fh)}

    def test_the_named_states(self):
        cases = [dict(id=n, flag=f, boot=self._BOOT[t], feat=fe, hv=h, vfe=v)
                 for (n, f, t, fe, h, v, _e) in self.NAMED]
        got = self._run_cases(cases)
        for (n, f, t, fe, h, v, (ev, eo)) in self.NAMED:
            with self.subTest(state=n):
                self.assertEqual((got[n]["verdict"], got[n]["outstanding"]), (ev, eo))
                # And the spec function agrees with the literal row, so the two
                # oracles cannot drift apart either.
                self.assertEqual(self._expected(f, t, fe, h, v), (ev, eo))

    def test_the_whole_reachable_state_space(self):
        cases = []
        for f in ("absent", "1", "dd-uninstall", "dd-utf16", "dd-spaced", "empty"):
            for t in self._BOOT:
                for fe in ("", "VirtualMachinePlatform=EnablePending",
                           "Microsoft-Windows-Subsystem-Linux=EnablePending",
                           "VirtualMachinePlatform=Disabled", "throws"):
                    for h in ("true", "false", "null", "throws"):
                        for v in ("true", "false", "null", "throws"):
                            cases.append(dict(id=f"{f}|{t}|{fe}|{h}|{v}", flag=f, boot=self._BOOT[t],
                                              feat=fe, hv=h, vfe=v, _k=(f, t, fe, h, v)))
        got = self._run_cases([{k: c[k] for k in c if k != "_k"} for c in cases])
        self.assertEqual(len(got), len(cases))
        wrong = []
        for c in cases:
            rec = got[c["id"]]
            if (rec["vtype"], rec["otype"]) != ("String", "Boolean"):
                wrong.append((c["id"], "types", rec["vtype"], rec["otype"]))
            if (rec["verdict"], rec["outstanding"]) != self._expected(*c["_k"]):
                wrong.append((c["id"], rec["verdict"], rec["outstanding"], self._expected(*c["_k"])))
        self.assertEqual(wrong[:10], [], f"{len(wrong)} of {len(cases)} states disagree with the ladder")


# ═══════════════════════════════════════════════════════════════════════════
def _oem(text, cp):
    """What Windows PowerShell 5.1 hands a script for wsl.exe's piped output:
    UTF-16LE bytes (CRT _O_U16TEXT) decoded one byte at a time in the OEM code
    page — every ASCII character followed by a U+0000."""
    return text.encode("utf-16-le").decode(cp)


_FIELD = ("Der Vorgang konnte nicht gestartet werden, da ein erforderliches Feature nicht installiert ist.\r\n"
          "Fehlercode: Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE\r\n")


def _wrap(s, width):
    out = []
    for ln in s.split("\r\n"):
        while len(ln) > width:
            out.append(ln[:width])
            ln = ln[width:]
        out.append(ln)
    return "\r\n".join(out)


class ClassifierExecutedTest(_PwshCase):
    """Get-WslFailureClass / Get-WslFailureCode read wsl's STAGE and CODE, never
    the localized message — and the GUI's Python twin
    (wsl_bridge.classify_wsl_failure) gives the same answer on every row.

    Row = (name, text, class, code). The stage rows are real WSL error paths
    (microsoft/WSL issues cited in virtualization_ready.ps1)."""

    CORPUS = [
        ("field_verbatim", _FIELD, "hypervisor", "HCS_E_SERVICE_NOT_AVAILABLE"),
        ("english", _FIELD.replace("Der Vorgang konnte nicht gestartet werden, da ein erforderliches Feature "
                                   "nicht installiert ist.", "The operation could not be started because a "
                                   "required feature is not installed.").replace("Fehlercode", "Error code"),
         "hypervisor", "HCS_E_SERVICE_NOT_AVAILABLE"),
        # The 5.1 byte shape, NOT NUL-stripped: the classifier must survive a
        # caller that forgets the strip, exactly as it survives one that forgets
        # -Width. Before 2026-09-11 this row classified as "".
        ("field_cp850_with_nuls", _oem(_FIELD, "cp850"), "hypervisor", "HCS_E_SERVICE_NOT_AVAILABLE"),
        ("field_cp437_with_nuls", _oem(_FIELD, "cp437"), "hypervisor", "HCS_E_SERVICE_NOT_AVAILABLE"),
        ("field_wrapped_at_60", _wrap(_FIELD, 60), "hypervisor", "HCS_E_SERVICE_NOT_AVAILABLE"),
        ("field_wrapped_at_80", _wrap(_FIELD, 80), "hypervisor", "HCS_E_SERVICE_NOT_AVAILABLE"),
        ("hyperv_not_installed_symbolic",
         "Fehlercode: Wsl/Service/CreateInstance/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED\r\n",
         "hypervisor", "HCS_E_HYPERV_NOT_INSTALLED"),
        ("legacy_0x80370102", "WslRegisterDistribution failed with error: 0x80370102\r\n",
         "hypervisor", "HCS_E_HYPERV_NOT_INSTALLED"),
        ("legacy_0x80370114", "Error: 0x80370114 The operation could not be started because a required "
                              "feature is not installed.\r\n", "hypervisor", "HCS_E_SERVICE_NOT_AVAILABLE"),
        ("lowercase_token", "error code: wsl/service/registerdistro/createvm/hcs/hcs_e_service_not_available",
         "hypervisor", "HCS_E_SERVICE_NOT_AVAILABLE"),
        # A CreateVm STAGE is a VM-start failure whatever the code (review item 2).
        ("hcs_connection_timeout", "Fehlercode: Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_CONNECTION_TIMEOUT\r\n",
         "hypervisor", "HCS_E_CONNECTION_TIMEOUT"),
        ("hcs_connection_timeout_no_hcs_segment",
         "Error code: Wsl/Service/CreateInstance/CreateVm/HCS_E_CONNECTION_TIMEOUT\r\n",
         "hypervisor", "HCS_E_CONNECTION_TIMEOUT"),
        ("service_disabled_0x80070422", "Fehlercode: Wsl/Service/RegisterDistro/CreateVm/HCS/0x80070422\r\n",
         "hypervisor", "0x80070422"),
        ("createvm_file_not_found", "Fehlercode: Wsl/Service/CreateInstance/CreateVm/HCS/ERROR_FILE_NOT_FOUND\r\n",
         "hypervisor", "ERROR_FILE_NOT_FOUND"),
        ("createvm_lowercase_hex", "fehlercode: wsl/service/createinstance/createvm/hcs/0x800706bA\r\n",
         "hypervisor", "0x800706BA"),
        # A disk STAGE outranks the CreateVm it sits in: attaching a disk failed.
        ("mountvhd_under_createvm_0x80070032",
         "Fehler beim Anfügen des Datenträgers \"C:\\Program Files\\WSL\\system.vhd\" an WSL2: Die Anforderung wird nicht unterstützt.\r\n"
         "Fehlercode: Wsl/Service/CreateInstance/CreateVm/MountVhd/HCS/0x80070032\r\n", "disk", "0x80070032"),
        ("mountvhd_access_denied", "Error code: Wsl/Service/CreateInstance/MountVhd/HCS/E_ACCESSDENIED\r\n",
         "disk", "E_ACCESSDENIED"),
        ("mountvhd_corrupt", "Error code: Wsl/Service/CreateInstance/MountVhd/HCS/0x80070570\r\n", "disk", "0x80070570"),
        ("attachdisk_sharing_violation", "Fehlercode: Wsl/Service/CreateInstance/AttachDisk/HCS/ERROR_SHARING_VIOLATION\r\n",
         "disk", "ERROR_SHARING_VIOLATION"),
        ("mountdisk_not_found", "Fehlercode: Wsl/Service/CreateInstance/CreateVm/MountDisk/HCS/ERROR_PATH_NOT_FOUND\r\n",
         "disk", "ERROR_PATH_NOT_FOUND"),
        ("disk_full_symbolic", "Auf dem Datenträger ist nicht genügend Speicherplatz vorhanden.\r\n"
                               "Fehlercode: Wsl/Service/RegisterDistro/ERROR_DISK_FULL\r\n", "disk", "ERROR_DISK_FULL"),
        ("disk_full_hex", "Error code: Wsl/Service/RegisterDistro/0x80070070\r\n", "disk", "0x80070070"),
        ("disk_full_under_createvm", "Fehlercode: Wsl/Service/CreateInstance/CreateVm/HCS/ERROR_DISK_FULL\r\n",
         "disk", "ERROR_DISK_FULL"),
        # The two named HCS codes win over a disk stage.
        ("hcs_token_under_mountvhd", "Fehlercode: Wsl/Service/CreateInstance/CreateVm/MountVhd/HCS/HCS_E_SERVICE_NOT_AVAILABLE\r\n",
         "hypervisor", "HCS_E_SERVICE_NOT_AVAILABLE"),
        # Never a class: each of these has its own, different remedy — but the
        # code is still read, so a remedy can echo it.
        ("kernel_update_0x800701bc", "Fehlercode: Wsl/Service/RegisterDistro/0x800701bc\r\n", "", "0x800701BC"),
        ("access_denied_no_stage", "Zugriff verweigert\r\nFehlercode: Wsl/Service/RegisterDistro/E_ACCESSDENIED\r\n",
         "", "E_ACCESSDENIED"),
        ("already_exists", "Fehlercode: Wsl/Service/RegisterDistro/ERROR_ALREADY_EXISTS\r\n", "", "ERROR_ALREADY_EXISTS"),
        ("vm_not_available_is_no_hcs_code", "Fehlercode: Wsl/Service/ERROR_VM_NOT_AVAILABLE\r\n", "", "ERROR_VM_NOT_AVAILABLE"),
        ("service_down", "Fehlercode: Wsl/Service/E_UNEXPECTED\r\n", "", "E_UNEXPECTED"),
        ("distro_not_found", "Fehlercode: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n", "", "WSL_E_DISTRO_NOT_FOUND"),
        # A path glued to the next line's words must not be read into the code:
        # the code is read on text whose line breaks are intact.
        ("path_then_next_line", "Fehlercode: Wsl/Service/RegisterDistro/ERROR_ALREADY_EXISTS\r\nNaechsteZeile\r\n",
         "", "ERROR_ALREADY_EXISTS"),
        ("no_path_legacy_hex", "WslRegisterDistribution failed with error: 0x80070070\r\n", "", ""),
        ("german_umlauts_cp850", _oem("Für diesen Vorgang ist ein Neustart erforderlich.\r\n", "cp850"), "", ""),
        ("empty", "", "", ""),
        ("whitespace_only", " \r\n\t", "", ""),
    ]

    _PS = r"""
param([string]$Helper, [string]$Dir)
$ErrorActionPreference = "Continue"
. $Helper
foreach ($f in (Get-ChildItem -LiteralPath $Dir -Filter "*.txt" | Sort-Object Name)) {
    $t = [System.IO.File]::ReadAllText($f.FullName, [System.Text.UTF8Encoding]::new($false))
    "{0}`t{1}`t{2}" -f $f.BaseName, (Get-WslFailureClass -Text $t), (Get-WslFailureCode -Text $t)
}
"null`t{0}`t{1}" -f (Get-WslFailureClass -Text $null), (Get-WslFailureCode -Text $null)
"""

    def _run_corpus(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        for i, (name, text, _c, _k) in enumerate(self.CORPUS):
            with open(os.path.join(d, f"{i:02d}_{name}.txt"), "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        r = self._ps("classify.ps1", self._PS, "-Helper", _VIRT_PS1, "-Dir", d)
        got = {}
        for ln in r.stdout.decode("utf-8").splitlines():
            parts = ln.split("\t")
            if len(parts) == 3:
                got[parts[0]] = (parts[1], parts[2])
        self.assertIn("null", got, r.stderr.decode("utf-8", "replace")[-1500:])
        return got

    def test_the_corpus(self):
        got = self._run_corpus()
        for i, (name, _text, cls, code) in enumerate(self.CORPUS):
            with self.subTest(case=name):
                self.assertEqual(got.get(f"{i:02d}_{name}"), (cls, code))
        self.assertEqual(got.get("null"), ("", ""))

    def test_the_python_twin_agrees_on_every_row(self):
        """THE TWIN LOCKSTEP: the GUI classifies the SAME wsl output when a
        registered distro does not start (_run_prerequisite_checks_body). Two
        implementations of one decision, fenced by one corpus — cell by cell,
        against what the .ps1 actually RETURNED, not against the table."""
        got = self._run_corpus()
        sys.path.insert(0, os.path.normpath(os.path.join(_TESTS, "..")))
        from gui.app import wsl_bridge
        drift = []
        for i, (name, text, _cls, _code) in enumerate(self.CORPUS):
            twin = wsl_bridge.classify_wsl_failure(text)
            if twin != got.get(f"{i:02d}_{name}"):
                drift.append((name, got.get(f"{i:02d}_{name}"), twin))
        self.assertEqual(drift, [], "the .ps1 classifier and wsl_bridge.classify_wsl_failure disagree")
        self.assertEqual(wsl_bridge.classify_wsl_failure(None), ("", ""))

    def test_the_gui_picks_the_remedy_finalize_would_without_cim(self):
        """gui_app.py::_distro_start_remedy_kind is Get-HypervisorRemedyKind with
        no CIM state (the GUI reads none). Over every corpus row, the kind the
        .ps1 returns for the .ps1's own (class, code) with a $null state must be
        the kind the GUI returns for the twin's pair."""
        import ast
        got = self._run_corpus()
        d = tempfile.mkdtemp(dir=self.tmp)
        cases = os.path.join(d, "cases.tsv")
        with open(cases, "w", encoding="utf-8") as fh:
            for key, (cls, code) in got.items():
                fh.write(f"{key}\t{cls}\t{code}\n")
        ps = r"""
param([string]$Helper, [string]$Cases)
$ErrorActionPreference = "Continue"
. $Helper
foreach ($line in [System.IO.File]::ReadAllLines($Cases)) {
    $f = $line -split "`t", 3
    "{0}`t{1}" -f $f[0], (Get-HypervisorRemedyKind -State $null -FailureCode $f[2] -FailureClass $f[1])
}
"""
        r = self._ps("gui_kind.ps1", ps, "-Helper", _VIRT_PS1, "-Cases", cases)
        ps_kind = dict(ln.split("\t", 1) for ln in r.stdout.decode("utf-8").splitlines() if "\t" in ln)
        gui_src = open(os.path.normpath(os.path.join(_TESTS, "..", "gui", "app", "gui_app.py")), encoding="utf-8").read()
        fn = next(n for n in ast.parse(gui_src).body
                  if isinstance(n, ast.FunctionDef) and n.name == "_distro_start_remedy_kind")
        sys.path.insert(0, os.path.normpath(os.path.join(_TESTS, "..")))
        from gui.app import wsl_bridge
        ns = {"wsl_bridge": wsl_bridge}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "gui_app.py", "exec"), ns)
        drift = [(k, ps_kind.get(k), ns["_distro_start_remedy_kind"](*got[k]))
                 for k in got if ps_kind.get(k) != ns["_distro_start_remedy_kind"](*got[k])]
        self.assertEqual(len(ps_kind), len(got), r.stderr.decode("utf-8", "replace")[-800:])
        self.assertEqual(drift, [])
        self.assertIn("Disk", set(ps_kind.values()))
        self.assertIn("Unclassified", set(ps_kind.values()))


# ═══════════════════════════════════════════════════════════════════════════
class RemedyKindExecutedTest(_PwshCase):
    """Get-HypervisorRemedyKind: the words follow the PROOF.

    The review of PR #28 found a transcript saying „Virtualisierung ist aktiv"
    and then blaming VT-x in the BIOS. wsl's own DISK report outranks a CIM
    reading; Firmware wording is allowed ONLY when Windows reports both no
    running hypervisor AND firmware virtualization off (a running hypervisor
    masks the firmware property); the HYPERV_NOT_INSTALLED code gets the feature
    wording; any other hypervisor-class failure the service wording; and a start
    that failed with nothing known the unclassified wording, which claims no
    cause. A bare code (no class passed) is classified on its own."""

    _PS = r"""
param([string]$Helper)
$ErrorActionPreference = "Continue"
. $Helper
foreach ($hv in @('true', 'false', 'null')) {
  foreach ($vfe in @('true', 'false', 'null')) {
    foreach ($code in @('', 'HCS_E_SERVICE_NOT_AVAILABLE', 'HCS_E_HYPERV_NOT_INSTALLED', 'ERROR_DISK_FULL', '0x80070422')) {
      foreach ($class in @('', 'hypervisor', 'disk')) {
        $st = @{ HypervisorPresent = $null; VirtFirmwareEnabled = $null }
        if ($hv -eq 'true') { $st.HypervisorPresent = $true } elseif ($hv -eq 'false') { $st.HypervisorPresent = $false }
        if ($vfe -eq 'true') { $st.VirtFirmwareEnabled = $true } elseif ($vfe -eq 'false') { $st.VirtFirmwareEnabled = $false }
        "{0}|{1}|{2}|{3}`t{4}" -f $hv, $vfe, $code, $class, (Get-HypervisorRemedyKind -State $st -FailureCode $code -FailureClass $class)
      }
    }
  }
}
"null-state`t{0}" -f (Get-HypervisorRemedyKind -State $null -FailureCode 'HCS_E_HYPERV_NOT_INSTALLED')
"no-class-param`t{0}" -f (Get-HypervisorRemedyKind -State @{} -FailureCode 'HCS_E_SERVICE_NOT_AVAILABLE')
"""

    _CODE_CLASS = {"HCS_E_SERVICE_NOT_AVAILABLE": "hypervisor", "HCS_E_HYPERV_NOT_INSTALLED": "hypervisor",
                   "ERROR_DISK_FULL": "disk", "0x80070422": ""}

    @classmethod
    def _expected(cls, hv, vfe, code, klass):
        """The specification, rung by rung — not derived from the .ps1."""
        effective = klass or (cls._CODE_CLASS.get(code, "") if code else "")
        if effective == "disk":
            return "Disk"
        if hv == "false" and vfe == "false":
            return "Firmware"
        if code == "HCS_E_HYPERV_NOT_INSTALLED":
            return "Feature"
        if effective == "hypervisor":
            return "Service"
        return "Unclassified"

    def test_every_combination(self):
        r = self._ps("kind.ps1", self._PS, "-Helper", _VIRT_PS1)
        got = dict(ln.split("\t", 1) for ln in r.stdout.decode("utf-8").splitlines() if "\t" in ln)
        self.assertEqual(len(got), 3 * 3 * 5 * 3 + 2, r.stderr.decode("utf-8", "replace")[-1500:])
        for hv in ("true", "false", "null"):
            for vfe in ("true", "false", "null"):
                for code in ("", "HCS_E_SERVICE_NOT_AVAILABLE", "HCS_E_HYPERV_NOT_INSTALLED", "ERROR_DISK_FULL", "0x80070422"):
                    for klass in ("", "hypervisor", "disk"):
                        with self.subTest(hv=hv, vfe=vfe, code=code, klass=klass):
                            self.assertEqual(got[f"{hv}|{vfe}|{code}|{klass}"],
                                             self._expected(hv, vfe, code, klass))
        self.assertEqual(got["null-state"], "Feature")
        self.assertEqual(got["no-class-param"], "Service")
        # The cells the review was about, spelled out.
        self.assertEqual(got["true|true|0x80070422|hypervisor"], "Service",
                         "a CreateVm-stage failure with any code gets the VM remedy")
        self.assertEqual(got["false|false|ERROR_DISK_FULL|disk"], "Disk",
                         "wsl's own disk report outranks a CIM firmware reading")
        self.assertEqual(got["true|true||"], "Unclassified",
                         "nothing known must not be dressed up as the VM service")

    def test_the_evidence_reaches_the_notes(self):
        """The facts a remedy is chosen from must be IN the transcript — the
        review found „the notes printed just above show those values" while
        they printed neither CIM value."""
        ps = r'''
param([string]$Helper)
''' + _MOCKS_PS + r'''
$ErrorActionPreference = "Continue"
. $Helper
$st = Get-RebootState -FlagPath ""
@($st.Notes) -join "`n"
'''
        env = {"MOCK_HV": "false", "MOCK_VFE": "null", "MOCK_BOOT": "2026-09-07T13:00:00",
               "MOCK_FEAT": "", "MOCK_SVC": "vmcompute=Stopped/Disabled"}
        r = self._ps("notes.ps1", ps, "-Helper", _VIRT_PS1, env=env)
        out = r.stdout.decode("utf-8", "replace")
        self.assertIn("HypervisorPresent = False", out)
        self.assertIn("VirtualizationFirmwareEnabled = (leer)", out, "$null is not $false")
        self.assertIn("Dienst vmcompute = Stopped (Disabled)", out)
        self.assertIn("(Dienst WslService nicht lesbar:", out, "an unreadable service is said, not skipped")


# ═══════════════════════════════════════════════════════════════════════════
_DISTRO_PS1 = os.path.join(_SCRIPTS, "wsl_distro_state.ps1")


class DistroRegistrationExecutedTest(_PwshCase):
    """wsl_distro_state.ps1 — Registered / Absent / Unresponsive.

    A failed `wsl --list` used to mean „nicht registriert" everywhere, which is
    how a PC whose WSL service was down got „Bitte den Installer erneut
    ausführen" and an import attempt. The pure resolver is walked over its whole
    input space; the registry reader over its four shapes."""

    _PS = r'''
param([string]$Helper, [string]$Cases)
''' + _MOCKS_PS + r'''
$ErrorActionPreference = "Continue"
. $Helper
foreach ($line in [System.IO.File]::ReadAllLines($Cases)) {
    $c = $line | ConvertFrom-Json
    $names = @()
    if ($c.regnames) { $names = @($c.regnames -split ";") }
    $v = Resolve-DistroRegistration -DistroName "EduBotics" -ListRan ([bool]$c.ran) -ListExitCode ([int]$c.rc) `
        -ListText ([string]$c.text) -RegistryReadable ([bool]$c.regok) -RegistryNames $names
    "{0}`t{1}" -f $c.id, $v
}
foreach ($shape in @("missing", "throws", "none", "Ubuntu;EduBotics")) {
    $env:MOCK_LXSS = $shape
    $r = Get-LxssDistroNames
    "lxss:{0}`t{1}|{2}" -f $shape, $r.Readable, (@($r.Names) -join ",")
}
'''

    @staticmethod
    def _expected(ran, rc, text, regok, regnames):
        listed = ran and any(ln.replace("\x00", "").strip().lower() == "edubotics" for ln in text.splitlines())
        if listed:
            return "Registered"
        if ran and rc == 0:
            return "Absent"
        if "WSL_E_DEFAULT_DISTRO_NOT_FOUND" in "".join(text.split()).replace("\x00", "").upper():
            return "Absent"
        if regok:
            return "Unresponsive" if "edubotics" in regnames.lower().split(";") else "Absent"
        return "Unresponsive"

    def test_the_resolver_over_its_input_space(self):
        texts = {
            "listed": "Ubuntu\r\nEduBotics\r\n",
            "listed_utf16_nuls": "\x00".join("EduBotics\r\n") + "\x00",
            "other": "Ubuntu\r\n",
            "listed_lowercase": "ubuntu\r\nedubotics\r\n",
            "empty": "",
            "no_distros_store": "Windows Subsystem for Linux has no installed distributions.\r\n"
                                "Error code: Wsl/WSL_E_DEFAULT_DISTRO_NOT_FOUND\r\n",
            "no_distros_inbox_german": "Das Windows-Subsystem für Linux verfügt über keine installierten Distributionen.\r\n",
            "service_down": "Fehlercode: Wsl/Service/E_UNEXPECTED\r\n",
            "no_distros_lowercase_token": "error code: wsl/wsl_e_default_distro_not_found\r\n",
        }
        cases = []
        for tname, text in texts.items():
            for ran in (True, False):
                for rc in (0, 255):
                    for regok in (True, False):
                        for regnames in ("", "Ubuntu", "Ubuntu;EduBotics", "EDUBOTICS"):
                            cases.append(dict(id=f"{tname}|{ran}|{rc}|{regok}|{regnames}", text=text if ran else "",
                                              ran=ran, rc=rc, regok=regok, regnames=regnames))
        d = tempfile.mkdtemp(dir=self.tmp)
        cf = os.path.join(d, "cases.jsonl")
        with open(cf, "w", encoding="utf-8") as fh:
            for c in cases:
                fh.write(json.dumps(c) + "\n")
        r = self._ps("reg.ps1", self._PS, "-Helper", _DISTRO_PS1, "-Cases", cf)
        got = dict(ln.split("\t", 1) for ln in r.stdout.decode("utf-8").splitlines() if "\t" in ln)
        wrong = [(c["id"], got.get(c["id"]), self._expected(c["ran"], c["rc"], c["text"], c["regok"], c["regnames"]))
                 for c in cases
                 if got.get(c["id"]) != self._expected(c["ran"], c["rc"], c["text"], c["regok"], c["regnames"])]
        self.assertEqual(wrong[:8], [], f"{len(wrong)} of {len(cases)} disagree; stderr: "
                                        f"{r.stderr.decode('utf-8', 'replace')[-800:]}")
        # THE TWIN LOCKSTEP: the GUI's Python resolver must give the SAME answer
        # (lower-cased) on every one of these inputs — two implementations of one
        # decision, fenced by one table.
        sys.path.insert(0, os.path.normpath(os.path.join(_TESTS, "..")))
        from gui.app import wsl_bridge
        twin = [(c["id"], got.get(c["id"]),
                 wsl_bridge.resolve_distro_registration(
                     c["ran"], c["rc"], c["text"], c["regok"],
                     [n for n in c["regnames"].split(";") if n]))
                for c in cases]
        drift = [t for t in twin if (t[1] or "").lower() != t[2]]
        self.assertEqual(drift[:8], [], f"{len(drift)} cells where the .ps1 and wsl_bridge.py disagree")
        # The cells the field failure lives in, spelled out:
        self.assertEqual(got["service_down|False|255|True|Ubuntu;EduBotics"], "Unresponsive")
        self.assertEqual(got["no_distros_inbox_german|True|255|True|"], "Absent",
                         "a fresh PC whose wsl exits non-zero with a LOCALIZED sentence must still set up")
        self.assertEqual(got["service_down|True|255|False|"], "Unresponsive",
                         "no listing and no readable store proves nothing")
        self.assertEqual(got["listed_utf16_nuls|True|255|False|"], "Registered")

    def test_the_registry_reader_shapes(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        cf = os.path.join(d, "cases.jsonl")
        open(cf, "w").close()
        r = self._ps("lxss.ps1", self._PS, "-Helper", _DISTRO_PS1, "-Cases", cf)
        got = dict(ln.split("\t", 1) for ln in r.stdout.decode("utf-8").splitlines() if "\t" in ln)
        self.assertEqual(got.get("lxss:missing"), "True|", "no Lxss key = readable, nothing registered")
        self.assertEqual(got.get("lxss:throws"), "False|")
        self.assertEqual(got.get("lxss:none"), "True|")
        self.assertEqual(got.get("lxss:Ubuntu;EduBotics"), "True|Ubuntu,EduBotics")


# ═══════════════════════════════════════════════════════════════════════════
_FAKE_WSL = r'''
import os, sys
state = os.environ["FAKEWSL_STATE"]
args = sys.argv[1:]
p = lambda n: os.path.join(state, n)
with open(p("calls.log"), "a") as fh:
    fh.write(" ".join(args) + "\n")
def rd(n, d=""):
    try:
        return open(p(n)).read().strip()
    except OSError:
        return d
def emit(text):
    # Like wsl.exe: UTF-16LE when stdout is not a console (WslClient sets the
    # CRT to _O_U16TEXT unless WSL_UTF8=1), and the top-level error goes to
    # STDOUT (wslutil::PrintMessage's default stream).
    sys.stdout.flush(); sys.stdout.buffer.write(text.encode("utf-16-le")); sys.stdout.buffer.flush()
if args[:1] == ["--status"]:
    seq = rd("status_seq", "0").splitlines() or ["0"]
    if len(seq) > 1:
        open(p("status_seq"), "w").write("\n".join(seq[1:]))
    sys.exit(int(seq[0]))
if args[:2] == ["--list", "--quiet"]:
    mode = rd("list_mode", "ok")
    # "fail": the WSL service is not answering. "fail_until_registered": a fresh
    # PC whose zero-distro listing exits non-zero (some builds do) until the
    # import has registered one.
    if mode == "fail" or (mode == "fail_until_registered" and not os.path.exists(p("registered"))):
        emit("Fehlercode: Wsl/Service/E_UNEXPECTED\r\n"); sys.exit(255)
    if os.path.exists(p("registered")):
        emit("EduBotics\r\n")
    sys.exit(0)
if args[:1] == ["--version"]:
    emit("WSL-Version: 2.5.10.0\r\nKernelversion: 6.6.87.2-1\r\n"); sys.exit(0)
if args[:1] in (["--install"], ["--update"]):
    sys.exit(0)
if args[:1] == ["--unregister"]:
    for n in ("registered", "vm_dead", "no_stamp", "stamp"):
        if os.path.exists(p(n)):
            os.remove(p(n))
    sys.exit(0)
if args[:1] == ["--import"]:
    mode = rd("import_mode", "ok")
    if mode == "ok":
        open(p("registered"), "w").close(); sys.exit(0)
    if mode == "hcs":
        emit("Der Vorgang konnte nicht gestartet werden, da ein erforderliches Feature nicht installiert ist.\r\n"
             "Fehlercode: Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE\r\n")
    elif mode == "hyperv":
        emit("Der virtuelle Computer konnte nicht gestartet werden, weil ein erforderliches Feature nicht installiert ist.\r\n"
             "Fehlercode: Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED\r\n")
    elif mode == "disk":
        emit("Auf dem Datenträger ist nicht genügend Speicherplatz vorhanden.\r\n"
             "Fehlercode: Wsl/Service/RegisterDistro/ERROR_DISK_FULL\r\n")
    elif mode == "custom":
        emit(open(p("import_text"), encoding="utf-8").read())
    sys.exit(255)   # wsl.exe exits -1; a POSIX status can only say 255
if args[:1] == ["-d"]:
    if not os.path.exists(p("registered")):
        sys.exit(255)
    if os.path.exists(p("vm_dead")):
        # registered, but the VM cannot start: every -d command fails with wsl's
        # OWN words (vm_dead's content; empty = the 2026-09-07 HCS class)
        emit(open(p("vm_dead"), encoding="utf-8").read() or ("Der Vorgang konnte nicht gestartet werden, da ein erforderliches Feature nicht installiert ist.\r\n"
                               "Fehlercode: Wsl/Service/CreateInstance/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE\r\n"))
        sys.exit(255)
    rest = args[3:] if len(args) > 2 and args[2] == "--" else args[2:]
    if rest[:3] == ["--exec", "/bin/sh", "-c"] and len(rest) == 4:
        # The distro's VM is up: run the caller's script under a REAL /bin/sh,
        # with the distro's stamp file mapped into the state dir (absent when
        # no_stamp, "7" unless a "stamp" file says otherwise). Linux output is
        # UTF-8, not UTF-16 — only wsl.exe's own messages are UTF-16LE.
        stamp_file = p("distro_etc_stamp")
        if os.path.exists(stamp_file):
            os.remove(stamp_file)
        if not os.path.exists(p("no_stamp")):
            with open(stamp_file, "w") as fh:
                fh.write(rd("stamp", "7") + "\n")
        import subprocess
        r = subprocess.run(["/bin/sh", "-c", rest[3].replace("/etc/edubotics-rootfs-version", stamp_file)],
                           capture_output=True)
        sys.stdout.buffer.write(r.stdout); sys.stderr.buffer.write(r.stderr)
        sys.exit(r.returncode)
    if rest[:1] == ["cat"]:
        if os.path.exists(p("no_stamp")):     # a distro from an installer <= 2.6.0
            sys.stderr.write("cat: /etc/edubotics-rootfs-version: No such file or directory\n"); sys.exit(1)
        sys.stdout.write("7\n"); sys.exit(0)
    if rest[:3] == ["docker", "image", "inspect"]:
        sys.exit(0 if os.path.exists(p("images")) else 1)
    sys.exit(0)
sys.exit(0)
'''

_WRAPPER_PS = r'''
param([string]$Script, [string]$LogPath = "", [string]$MarkerPath = "", [switch]$Destructive,
      [switch]$Preserve, [switch]$PostReboot)
''' + _MOCKS_PS + r'''
$ErrorActionPreference = "Continue"
$splat = @{}
if ($LogPath) { $splat["LogPath"] = $LogPath }
if ($MarkerPath) { $splat["MarkerPath"] = $MarkerPath }
if ($Destructive) { $splat["AllowDestructiveReimport"] = $true }
if ($Preserve) { $splat["PreserveExistingRebootFlag"] = $true }
if ($PostReboot) { $splat["PostReboot"] = $true }
$global:LASTEXITCODE = 0
& $Script @splat
exit $LASTEXITCODE
'''

_ELEVATED_NEEDLE = ("[bool]([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]"
                    "::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)")
_BUILD_GATE_NEEDLE = "if ($osVersion.Build -lt 22000) {"


@unittest.skipIf(sys.platform == "win32", "a real wsl.exe would shadow the fake on Windows")
class _InstallerSandbox(_PwshCase):
    """A copied {app}\\scripts + fake {app}\\wsl_rootfs + a fake wsl on PATH."""

    def _sandbox(self, *, flag=None, flag_age=0, boot_age=1800, status="0", feat="", hv="false",
                 vfe="true", import_mode="hcs", images=False, registered=False, vm_dead=False,
                 no_stamp=False, list_mode="ok", lxss=None, svc="vmcompute=Stopped/Manual;WslService=Running/Automatic",
                 stamp=None, import_text=None):
        base = tempfile.mkdtemp(dir=self.tmp)
        scripts = os.path.join(base, "app", "scripts")
        shutil.copytree(_SCRIPTS, scripts)
        fin = os.path.join(scripts, "finalize_install.ps1")
        src = open(fin, encoding="utf-8-sig").read()
        self.assertIn(_ELEVATED_NEEDLE, src, "host-only substitution target moved")
        open(fin, "w", encoding="utf-8-sig").write(src.replace(_ELEVATED_NEEDLE, "$true"))
        pre = os.path.join(scripts, "install_prerequisites.ps1")
        src = open(pre, encoding="utf-8-sig").read()
        self.assertIn(_BUILD_GATE_NEEDLE, src, "host-only substitution target moved")
        open(pre, "w", encoding="utf-8-sig").write(src.replace(_BUILD_GATE_NEEDLE, "if ($false) {"))
        rootfs = os.path.join(base, "app", "wsl_rootfs")
        os.makedirs(rootfs)
        with open(os.path.join(rootfs, "edubotics-rootfs.tar.gz"), "wb") as fh:
            fh.write(b"fake-rootfs")
        with open(os.path.join(rootfs, "edubotics-rootfs.tar.gz.sha256"), "w") as fh:
            fh.write(hashlib.sha256(b"fake-rootfs").hexdigest() + "  edubotics-rootfs.tar.gz\n")
        with open(os.path.join(rootfs, "ROOTFS_VERSION"), "w") as fh:
            fh.write("7\n")
        state = os.path.join(base, "wslstate")
        os.makedirs(state)
        for name, value in (("status_seq", status), ("import_mode", import_mode), ("list_mode", list_mode)):
            with open(os.path.join(state, name), "w") as fh:
                fh.write(value)
        for name, on in (("images", images), ("registered", registered), ("vm_dead", vm_dead),
                         ("no_stamp", no_stamp)):
            if on:
                with open(os.path.join(state, name), "w") as fh:
                    # vm_dead may carry the exact wsl text to emit
                    fh.write(on if isinstance(on, str) else "")
        if stamp is not None:
            with open(os.path.join(state, "stamp"), "w") as fh:
                fh.write(stamp)
        if import_text is not None:
            with open(os.path.join(state, "import_text"), "w", encoding="utf-8") as fh:
                fh.write(import_text)
        bindir = os.path.join(base, "bin")
        os.makedirs(bindir)
        for name, body in (("wsl", _FAKE_WSL), ("usbipd", "import sys; print('5.3.0')"),
                           ("dism", "import sys; sys.exit(3010)")):
            path = os.path.join(bindir, name)
            with open(path, "w") as fh:
                fh.write(f"#!{sys.executable}\n{body}\n")
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        now = time.time()
        flag_path = os.path.join(scripts, ".reboot_required")
        if flag is not None:
            with open(flag_path, "w", newline="") as fh:
                fh.write(flag + "\r\n")
            os.utime(flag_path, (now - flag_age, now - flag_age))
        progdata = os.path.join(base, "ProgramData")
        os.makedirs(progdata)
        env = {"PATH": bindir + os.pathsep + os.environ.get("PATH", ""), "FAKEWSL_STATE": state,
               "MOCK_FEAT": feat, "MOCK_HV": hv, "MOCK_VFE": vfe, "ProgramData": progdata, "MOCK_SVC": svc,
               "MOCK_BOOT": dt.datetime.fromtimestamp(now - boot_age).isoformat(timespec="seconds")}
        if lxss is not None:
            env["MOCK_LXSS"] = lxss
        return dict(base=base, scripts=scripts, state=state, flag=flag_path, env=env,
                    flag_mtime=(now - flag_age) if flag is not None else None, logs=progdata)

    def _calls(self, sb):
        path = os.path.join(sb["state"], "calls.log")
        return open(path).read().splitlines() if os.path.exists(path) else []


def _finalize_const(name):
    """A German $CONSTANT from finalize_install.ps1, read from the real file, so
    an assertion about the student-facing sentence cannot drift from it."""
    import re
    src = open(os.path.join(_SCRIPTS, "finalize_install.ps1"), encoding="utf-8-sig").read()
    m = re.search(r'(?m)^\$%s\s*=\s*"([^"]*)"' % re.escape(name), src)
    assert m, f"${name} is no longer a one-line assignment in finalize_install.ps1"
    return m.group(1)


def _problem_with_code(name, code):
    """A remedy's problem line as Fail-WithHypervisorRemedy writes it: wsl's
    code inside the sentence's final period."""
    problem = _finalize_const(name)
    return f"{problem.rstrip('.')} (Fehlercode: {code})." if code else problem


class FinalizeEndToEndTest(_InstallerSandbox):

    def _finalize(self, sb):
        log = os.path.join(sb["logs"], "edubotics_finalize.log")
        marker = os.path.join(sb["logs"], "edubotics_finalize.marker")
        r = self._ps("wrapper.ps1", _WRAPPER_PS, "-Script", os.path.join(sb["scripts"], "finalize_install.ps1"),
                     "-LogPath", log, "-MarkerPath", marker, env=sb["env"])
        transcript = open(log, encoding="utf-8", errors="replace").read() if os.path.exists(log) else ""
        mark = open(marker, encoding="utf-8-sig").read() if os.path.exists(marker) else ""
        return r.returncode, transcript, mark

    def _imported(self, sb):
        return any(c.startswith("--import") for c in self._calls(sb))

    def test_the_field_state_asks_for_a_reboot_before_touching_wsl(self):
        """2026-09-07: install_prerequisites asked for a reboot seven seconds
        earlier, both features read Enabled, the hypervisor was not running.
        Old code: `wsl --import` -> HCS_E_SERVICE_NOT_AVAILABLE -> exit 1 +
        disk/antivirus advice. Now: exit 10 and wsl is asked nothing but --status."""
        sb = self._sandbox(flag="1", flag_age=7, boot_age=1800, hv="false", vfe="true")
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 10, transcript[-3000:])
        self.assertEqual(set(self._calls(sb)), {"--status", "--version"},
                         "the verdict must run BEFORE any state-changing wsl call "
                         "(--version is read-only evidence)")
        self.assertIn("Ergebnis: RebootRequired", transcript)
        self.assertNotIn("Neustart bereits erfolgt", transcript)
        self.assertIn("nicht Herunterfahren", transcript,
                      "Fast Startup: a shutdown does not complete the pending work")
        with open(sb["flag"], "rb") as fh:
            self.assertEqual(fh.read(), b"1\r\n", "the flag's reason is untouched")
        self.assertAlmostEqual(os.path.getmtime(sb["flag"]), sb["flag_mtime"], delta=1.0)

    def _marker_lines(self, marker):
        lines = marker.splitlines()
        self.assertTrue(lines and lines[0].startswith("FAILED "), marker)
        self.assertEqual(len(lines), 3, f"the FAILED shape is exactly three lines: {marker!r}")
        return lines[1], lines[2]

    def test_a_cim_firmware_off_reading_never_blocks_the_import(self):
        """CIM chooses WORDS, never whether to try. Both CIM values genuinely
        $false used to exit 11 before the import, on every launch — a property
        that misreports would have locked the PC out for good."""
        for flag in (None, "1"):
            with self.subTest(flag=flag, import_mode="ok"):
                sb = self._sandbox(flag=flag, flag_age=7200, boot_age=600, hv="false", vfe="false",
                                   import_mode="ok", images=True)
                rc, transcript, marker = self._finalize(sb)
                self.assertEqual(rc, 0, transcript[-3000:])
                self.assertTrue(self._imported(sb))
                self.assertIn("Laut Windows ist die Virtualisierung im BIOS/UEFI ausgeschaltet", transcript)
                self.assertTrue(marker.startswith("SUCCESS "), marker)
            with self.subTest(flag=flag, import_mode="hcs"):
                sb = self._sandbox(flag=flag, flag_age=7200, boot_age=600, hv="false", vfe="false")
                rc, transcript, marker = self._finalize(sb)
                self.assertEqual(rc, 11, transcript[-3000:])
                self.assertTrue(self._imported(sb), "the import is tried; ITS failure exits 11")
                problem, nextstep = self._marker_lines(marker)
                self.assertEqual(problem, _problem_with_code("VIRT_FIRMWARE_PROBLEM_DE",
                                                             "HCS_E_SERVICE_NOT_AVAILABLE"))
                self.assertEqual(nextstep, _finalize_const("VIRT_FIRMWARE_NEXTSTEP_DE"))

    def test_an_unknown_verdict_is_caught_by_the_import_classifier(self):
        """Unknown proceeds by design; import must then classify wsl's OWN
        words — UTF-16LE on stdout, exactly the real wsl.exe byte shape — and
        with NO firmware proof the student gets the SERVICE remedy, not BIOS."""
        sb = self._sandbox(flag=None, boot_age=600, hv="null", vfe="null", import_mode="hcs")
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 11, transcript[-3000:])
        self.assertTrue(self._imported(sb))
        self.assertIn("Fehlercode: Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE",
                      transcript, "wsl's own words must be echoed into the transcript")
        self.assertNotIn("\x00", transcript, "the echo must be NUL-free")
        self.assertIn("WSL2 konnte nicht starten (Fehlerart: Service, Fehlercode: HCS_E_SERVICE_NOT_AVAILABLE).",
                      transcript)
        self.assertEqual(transcript.count("Fehlerart:"), 1, "the remedy kind is reported once")
        self.assertNotIn("Antivirus-Ausnahme", transcript)
        problem, nextstep = self._marker_lines(marker)
        self.assertEqual(problem, _problem_with_code("VIRT_SERVICE_PROBLEM_DE", "HCS_E_SERVICE_NOT_AVAILABLE"))
        self.assertEqual(nextstep, _finalize_const("VIRT_SERVICE_NEXTSTEP_DE"))
        # The evidence the remedy was chosen from is IN the transcript.
        for fact in ("HypervisorPresent = (leer)", "VirtualizationFirmwareEnabled = (leer)",
                     "Dienst vmcompute = Stopped (Manual)", "Dienst WslService = Running (Automatic)",
                     "WSL-Version: 2.5.10.0"):
            self.assertIn(fact, transcript)

    def test_a_running_hypervisor_that_still_fails_is_never_blamed_on_the_bios(self):
        """The review's probe R: HypervisorPresent $true, import fails with HCS.
        The old transcript said „Virtualisierung ist aktiv" and then
        „…nicht verfügbar (VT-x/AMD-V)". Now: the hypervisor line says only what
        was read, and the remedy is the VM service's."""
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="true", import_mode="hcs")
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 11, transcript[-3000:])
        self.assertIn("Der Windows-Hypervisor läuft", transcript)
        self.assertNotIn("Virtualisierung ist aktiv", transcript)
        self.assertNotIn("ausgeschaltet", transcript)
        problem, _ = self._marker_lines(marker)
        self.assertEqual(problem, _problem_with_code("VIRT_SERVICE_PROBLEM_DE", "HCS_E_SERVICE_NOT_AVAILABLE"))

    def test_hyperv_not_installed_gets_the_feature_remedy(self):
        sb = self._sandbox(flag=None, boot_age=600, hv="false", vfe="true", import_mode="hyperv")
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 11, transcript[-3000:])
        problem, nextstep = self._marker_lines(marker)
        self.assertEqual(problem, _problem_with_code("VIRT_FEATURE_PROBLEM_DE", "HCS_E_HYPERV_NOT_INSTALLED"))
        self.assertIn("VM-Plattform", nextstep)

    def test_a_pending_feature_enable_defers_even_with_no_flag(self):
        """The verdict gate is not gated on the flag."""
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="true", import_mode="ok",
                           feat="VirtualMachinePlatform=EnablePending")
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 10, transcript[-3000:])
        self.assertFalse(self._imported(sb))

    def test_a_status_hiccup_on_a_live_hypervisor_does_not_demand_a_restart(self):
        """Phase 0 used to exit 10 on the bare existence of the flag after its
        child. Hypervisor live, flag minutes old, first `wsl --status` fails
        once: the review measured exit 10. The verdict ranks the live
        hypervisor above the flag-time inference, so the install completes."""
        sb = self._sandbox(flag="1", flag_age=60, boot_age=1800, status="1\n0", hv="true", vfe="true",
                           import_mode="ok", images=True)
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 0, transcript[-3000:])
        self.assertTrue(marker.startswith("SUCCESS "), marker)
        self.assertFalse(os.path.exists(sb["flag"]))

    def test_the_same_hiccup_without_a_hypervisor_still_asks_for_the_restart(self):
        sb = self._sandbox(flag="1", flag_age=60, boot_age=1800, status="1\n0", hv="false", vfe="true",
                           import_mode="ok", images=True)
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 10, transcript[-3000:])
        self.assertFalse(self._imported(sb))

    def test_custody_does_not_preserve_a_stale_flag(self):
        """The rebooted-since flag is not owed a reboot: custody must let the
        child drop it, or every launch would re-announce a restart."""
        sb = self._sandbox(flag="1", flag_age=7200, boot_age=600, status="1\n0", hv="true", vfe="true",
                           import_mode="ok", images=True)
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 0, transcript[-3000:])
        self.assertNotIn("wiederhergestellt", transcript)
        self.assertFalse(os.path.exists(sb["flag"]))

    def test_an_unresponsive_wsl_never_imports(self):
        """`wsl --list` fails and the registration store names the distro: the
        distro may well exist, so no import — and the student is told WSL is
        not answering, never that the environment is missing."""
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="true", import_mode="ok",
                           list_mode="fail", lxss="EduBotics")
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 1, transcript[-3000:])
        self.assertFalse(any(c.startswith(("--import", "--unregister")) for c in self._calls(sb)))
        problem, nextstep = self._marker_lines(marker)
        self.assertEqual(problem, _finalize_const("WSL_UNRESPONSIVE_PROBLEM_DE"))
        self.assertNotIn("Installer erneut", marker)
        self.assertNotIn("nicht eingerichtet", problem)

    def test_a_fresh_pc_whose_listing_fails_still_imports(self):
        """The other half, and the one that must not regress: zero distros make
        some WSL builds exit non-zero with a localized sentence. No Lxss key =
        nothing registered = import."""
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="true", import_mode="ok",
                           images=True, list_mode="fail_until_registered", lxss="missing")
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 0, transcript[-3000:])
        self.assertTrue(self._imported(sb))
        self.assertTrue(marker.startswith("SUCCESS "), marker)
        self.assertNotIn(_finalize_const("WSL_UNRESPONSIVE_PROBLEM_DE"), transcript)

    def test_unknown_with_every_value_read_does_not_claim_it_could_not_check(self):
        """Rebooted since the flag, no hypervisor running, firmware
        virtualization on (e.g. `hypervisorlaunchtype off`): every CIM value WAS
        read, so „konnte nicht geprüft werden" would be false in the very
        transcript that prints those values."""
        sb = self._sandbox(flag="1", flag_age=7200, boot_age=600, hv="false", vfe="true", import_mode="hcs")
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 11, transcript[-3000:])
        self.assertIn("Ergebnis: Unknown", transcript)
        self.assertIn("nicht eindeutig bestätigt", transcript)
        self.assertNotIn("konnte nicht geprüft werden", transcript)

    def test_a_disk_failure_keeps_the_disk_wording(self):
        sb = self._sandbox(flag=None, boot_age=600, hv="null", vfe="null", import_mode="disk")
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 1, transcript[-3000:])
        self.assertIn("ERROR_DISK_FULL", transcript)
        self.assertIn("Prüfen Sie: Antivirus-Ausnahme, genug Speicherplatz", transcript)
        self.assertNotIn("Der Hypervisor von Windows läuft nicht", transcript)

    def test_a_live_hypervisor_imports_and_finishes(self):
        """Ground truth beats the flag-time proxy (Fast Startup): a FRESH flag
        with a live hypervisor imports, pulls nothing (images present) and
        clears the flag."""
        sb = self._sandbox(flag="1", flag_age=7, boot_age=1800, hv="true", vfe="false",
                           import_mode="ok", images=True)
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 0, transcript[-3000:])
        self.assertTrue(self._imported(sb))
        self.assertTrue(marker.startswith("SUCCESS "), marker)
        self.assertFalse(os.path.exists(sb["flag"]), "full success clears the flag")

    def test_a_pending_docker_desktop_removal_outranks_a_live_hypervisor(self):
        sb = self._sandbox(flag="dd-uninstall", flag_age=60, boot_age=1800, hv="true", vfe="true",
                           import_mode="ok", images=True)
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 10, transcript[-3000:])
        self.assertFalse(self._imported(sb))

    def test_a_direct_run_rotates_the_previous_transcript(self):
        """The GUI rotates before it launches; a DIRECT run (support, a manual
        repair) relies on finalize's own rotation. Same name on both sides."""
        sb = self._sandbox(flag="1", flag_age=7, boot_age=1800, hv="false", vfe="true")
        log = os.path.join(sb["logs"], "edubotics_finalize.log")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("VORHERIGER VERSUCH\n")
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 10, transcript[-3000:])
        prev = os.path.join(sb["logs"], "edubotics_finalize.prev.log")
        self.assertTrue(os.path.isfile(prev))
        with open(prev, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VORHERIGER VERSUCH\n")
        self.assertNotIn("VORHERIGER VERSUCH", transcript)

    # ── An existing distro whose VM cannot start is not a rootfs question ──
    # The stamp read (`wsl -d EduBotics -- cat /etc/edubotics-rootfs-version`)
    # fails both on a distro that predates the stamp AND on one whose hypervisor
    # is dead. Only the first earns the one-final destructive re-import.
    def _import_directly(self, sb, destructive):
        args = ["-Script", os.path.join(sb["scripts"], "import_edubotics_wsl.ps1")]
        if destructive:
            args.append("-Destructive")
        r = self._ps("wrapper.ps1", _WRAPPER_PS, *args, env=sb["env"])
        return r.returncode, r.stdout.decode("utf-8", "replace")

    def test_an_existing_distro_that_cannot_start_is_not_a_rootfs_question(self):
        """finalize on an upgrade: an existing distro, a hypervisor that will not
        start, a verdict of Unknown. It used to exit 12 („Neuaufbau erforderlich —
        Installer erneut ausführen"), sending the student to a DESTRUCTIVE rebuild
        that cannot help."""
        sb = self._sandbox(flag="1", flag_age=7200, boot_age=600, hv="null", vfe="null",
                           import_mode="ok", registered=True, vm_dead=True)
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 11, transcript[-3000:])
        calls = self._calls(sb)
        self.assertFalse(any(c.startswith(("--unregister", "--import")) for c in calls), calls)
        self.assertIn("wird NICHT neu aufgebaut", transcript)
        self.assertIn("BIOS/UEFI", marker)

    def test_consent_cannot_destroy_a_distro_whose_vm_cannot_start(self):
        """The installer's Step 4 passes -AllowDestructiveReimport after its
        consent box; on a dead hypervisor that consent used to unregister the
        distro (every dataset gone) and then fail the import on the same HCS
        error. Refused on PROOF, before anything is destroyed."""
        sb = self._sandbox(flag=None, boot_age=600, hv="null", vfe="null", import_mode="hcs",
                           registered=True, vm_dead=True)
        rc, out = self._import_directly(sb, destructive=True)
        self.assertEqual(rc, 11, out[-3000:])
        self.assertNotIn("--unregister EduBotics", self._calls(sb))
        self.assertIn("Die Umgebung und ihre Daten bleiben erhalten", out)

    def test_a_stampless_distro_still_gets_its_one_final_reimport(self):
        """The designed case the unreadable-stamp path exists for must be
        untouched: a HEALTHY distro from an installer <= 2.6.0 (no stamp) is
        refused (12) without consent and rebuilt with it — now on PROOF: its VM
        answered the probe and the stamp file does not exist."""
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="true", import_mode="ok",
                           registered=True, no_stamp=True)
        rc, out = self._import_directly(sb, destructive=False)
        self.assertEqual(rc, 12, out[-3000:])
        self.assertNotIn("--unregister EduBotics", self._calls(sb))
        self.assertIn("keinen Rootfs-Versionsstempel", out)
        self.assertNotIn("Rootfs-Version ''", out, "the false mismatch line is gone")
        rc, out = self._import_directly(sb, destructive=True)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("--unregister EduBotics", self._calls(sb))

    def test_custody_restores_the_reason_and_the_ORIGINAL_mtime(self):
        """Phase 0: finalize's `wsl --status` fails, the child's succeeds, so the
        child finds no reason of its own and deletes a dd-uninstall flag it
        never wrote. finalize must put it back byte-for-byte AND with its
        original write time — a fresh mtime would move the flag past the next
        boot's LastBootUpTime comparison."""
        sb = self._sandbox(flag="dd-uninstall", flag_age=60, boot_age=1800, status="1\n0",
                           hv="true", vfe="true", import_mode="ok", images=True)
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 10, transcript[-3000:])
        self.assertIn("wiederhergestellt", transcript)
        with open(sb["flag"], "rb") as fh:
            self.assertEqual(fh.read(), b"dd-uninstall\r\n")
        self.assertAlmostEqual(os.path.getmtime(sb["flag"]), sb["flag_mtime"], delta=1.0)

    def test_consent_cannot_rebuild_over_a_wsl_that_does_not_answer(self):
        """The installer's Step 4 with -AllowDestructiveReimport, WSL not
        answering, the store naming the distro: nothing is unregistered and
        nothing imported."""
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="true", import_mode="ok",
                           registered=True, list_mode="fail", lxss="EduBotics")
        rc, out = self._import_directly(sb, destructive=True)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertFalse(any(c.startswith(("--unregister", "--import")) for c in self._calls(sb)))
        self.assertIn("WSL antwortet gerade nicht", out)

    # ── Review item 1: no destructive rebuild without POSITIVE proof ───────────
    _MOUNTVHD_0032 = ("Fehler beim Anfügen des Datenträgers \"C:\\Program Files\\WSL\\system.vhd\" an WSL2: "
                      "Die Anforderung wird nicht unterstützt.\r\n"
                      "Fehlercode: Wsl/Service/CreateInstance/CreateVm/MountVhd/HCS/0x80070032\r\n")

    def test_a_dead_vm_with_an_unlisted_code_is_never_rebuilt(self):
        """The review's High finding. A registered distro whose VM fails with a
        code OUTSIDE the old four tokens (MountVhd/HCS/0x80070032 after 24H2)
        used to read as "no stamp": exit 12, the false line „Rootfs-Version ''
        passt nicht", and after the installer's consent `wsl --unregister`."""
        sb = self._sandbox(flag="1", flag_age=86400, boot_age=3600, hv="true", vfe="false", import_mode="ok",
                           registered=True, vm_dead=self._MOUNTVHD_0032)
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 11, transcript[-3000:])
        calls = self._calls(sb)
        self.assertFalse(any(c.startswith(("--unregister", "--import")) for c in calls), calls)
        self.assertNotIn("Rootfs-Version ''", transcript)
        self.assertNotIn("Installer erneut", marker)
        problem, nextstep = self._marker_lines(marker)
        self.assertEqual(problem, _problem_with_code("VIRT_DISK_PROBLEM_DE", "0x80070032"))
        self.assertEqual(nextstep, _finalize_const("VIRT_DISK_NEXTSTEP_DE"))
        # And the installer's Step 4, which arrives WITH consent (and only
        # runs when no reboot flag is pending):
        os.remove(sb["flag"])
        rc, out = self._import_directly(sb, destructive=True)
        self.assertEqual(rc, 11, out[-3000:])
        self.assertNotIn("--unregister EduBotics", self._calls(sb))
        self.assertIn("Die Umgebung und ihre Daten bleiben erhalten", out)

    def test_a_dead_vm_that_names_nothing_known_is_not_rebuilt_either(self):
        """No classifier can list every failure, so the rule is proof-only: a
        distro that did not start is never rebuilt, whatever wsl said. The words
        then claim no cause, but still carry whatever code wsl printed."""
        for text, code in (("Fehlercode: Wsl/Service/E_UNEXPECTED\r\n", "E_UNEXPECTED"),
                           ("Zeitüberschreitung beim Warten auf die virtuelle Maschine.\r\n", "")):
            with self.subTest(code=code or "(none)"):
                sb = self._sandbox(flag="1", flag_age=86400, boot_age=3600, hv="null", vfe="null",
                                   import_mode="ok", registered=True, vm_dead=text)
                rc, transcript, marker = self._finalize(sb)
                self.assertEqual(rc, 11, transcript[-3000:])
                self.assertFalse(any(c.startswith(("--unregister", "--import")) for c in self._calls(sb)))
                problem, nextstep = self._marker_lines(marker)
                self.assertEqual(problem, _problem_with_code("VIRT_UNCLASSIFIED_PROBLEM_DE", code))
                self.assertEqual(nextstep, _finalize_const("VIRT_UNCLASSIFIED_NEXTSTEP_DE"))
                os.remove(sb["flag"])
                rc, out = self._import_directly(sb, destructive=True)
                self.assertEqual(rc, 11, out[-3000:])
                self.assertNotIn("--unregister EduBotics", self._calls(sb))

    def test_a_readable_different_stamp_still_needs_consent_and_gets_it(self):
        """A VM that started and a stamp that DIFFERS is the one proven rebuild
        besides the absent stamp: 12 without consent (naming both versions),
        rebuilt with it."""
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="true", import_mode="ok",
                           registered=True, stamp="6", images=True)
        rc, out = self._import_directly(sb, destructive=False)
        self.assertEqual(rc, 12, out[-3000:])
        self.assertIn("Vorhandene Rootfs-Version '6' passt nicht zur neuen Version '7'", out)
        self.assertNotIn("--unregister EduBotics", self._calls(sb))
        rc, out = self._import_directly(sb, destructive=True)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("--unregister EduBotics", self._calls(sb))

    def test_a_matching_stamp_keeps_the_distro(self):
        sb = self._sandbox(flag="1", flag_age=86400, boot_age=3600, hv="true", vfe="true", import_mode="ok",
                           registered=True, images=True)
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 0, transcript[-3000:])
        self.assertIn("Import übersprungen", transcript)
        self.assertFalse(any(c.startswith(("--unregister", "--import")) for c in self._calls(sb)))
        self.assertTrue(marker.startswith("SUCCESS "), marker)

    def test_the_stamp_is_read_by_one_command_inside_the_distro(self):
        """The proof is a sentinel printed by the SAME command that reads the
        stamp; a separate probe could race a VM that dies in between."""
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="true", import_mode="ok",
                           registered=True, images=True)
        self._import_directly(sb, destructive=False)
        probes = [c for c in self._calls(sb) if c.startswith("-d EduBotics --exec /bin/sh -c ")]
        self.assertEqual(len(probes), 1, self._calls(sb))
        self.assertIn("echo EDUBOTICS_VM_UP;", probes[0])
        self.assertFalse(any(c.startswith("-d EduBotics -- cat") for c in self._calls(sb)),
                         "the unproven read is gone")

    # ── Review item 2: classify by STAGE ─────────────────────────────────────
    def test_a_createvm_stage_import_failure_gets_the_vm_remedy(self):
        """CreateVm/HCS/0x80070422 (a disabled service) is outside the old four
        tokens and got the disk/antivirus triad — the original complaint."""
        sb = self._sandbox(flag=None, boot_age=600, hv="null", vfe="null", import_mode="custom",
                           import_text="Der Dienst kann nicht gestartet werden.\r\n"
                                       "Fehlercode: Wsl/Service/RegisterDistro/CreateVm/HCS/0x80070422\r\n")
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 11, transcript[-3000:])
        self.assertNotIn("Antivirus-Ausnahme", transcript)
        problem, _ = self._marker_lines(marker)
        self.assertEqual(problem, _problem_with_code("VIRT_SERVICE_PROBLEM_DE", "0x80070422"))

    def test_a_disk_stage_import_failure_keeps_the_disk_wording(self):
        """A FRESH import that cannot attach its disk has no data to protect:
        exit 1 and the disk triad, as for a full disk."""
        sb = self._sandbox(flag=None, boot_age=600, hv="null", vfe="null", import_mode="custom",
                           import_text="Zugriff verweigert.\r\n"
                                       "Fehlercode: Wsl/Service/RegisterDistro/MountVhd/HCS/E_ACCESSDENIED\r\n")
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 1, transcript[-3000:])
        self.assertIn("Prüfen Sie: Antivirus-Ausnahme, genug Speicherplatz", transcript)

    def test_import_without_its_helpers_keeps_the_old_wording(self):
        """Step 4 of a partially-copied {app}\\scripts must degrade, never
        hard-fail: no classifier, no three-way read, the pre-2026-09 triad."""
        sb = self._sandbox(flag=None, boot_age=600, hv="null", vfe="null", import_mode="hcs")
        for helper in ("virtualization_ready.ps1", "wsl_distro_state.ps1"):
            os.remove(os.path.join(sb["scripts"], helper))
        rc, out = self._import_directly(sb, destructive=False)
        self.assertEqual(rc, 1, out[-3000:])
        self.assertIn("Prüfen Sie: Antivirus-Ausnahme, genug Speicherplatz", out)
        self.assertTrue(any(c.startswith("--import") for c in self._calls(sb)))


class RestartRecordExecutedTest(_InstallerSandbox):
    """Review item 4: one restart per reason, not one per launch.

    A PC whose `wsl --status` fails on every boot re-runs `wsl --install` in
    finalize's Phase 0 (exit 0), which refreshes the flag IN THIS BOOT, so the
    verdict says RebootRequired after every restart — exit 10, forever. finalize
    now records the boot it asked in and the rung that asked; a RebootRequired
    verdict for the SAME rung after a REAL restart (LastBootUpTime moved by more
    than 60 s) exits 11 with „Ein Neustart hat nicht geholfen"."""

    def _finalize(self, sb):
        return FinalizeEndToEndTest._finalize(self, sb)

    def _record(self, sb):
        path = os.path.join(sb["scripts"], ".restart_requested")
        return open(path, encoding="ascii").read() if os.path.exists(path) else None

    @staticmethod
    def _boot(seconds_ago):
        return dt.datetime.fromtimestamp(time.time() - seconds_ago).isoformat(timespec="seconds")

    def _marker_pair(self, marker):
        lines = marker.splitlines()
        self.assertTrue(lines and lines[0].startswith("FAILED "), marker)
        return lines[1], lines[2]

    def test_the_loop_ends_after_one_fruitless_restart(self):
        sb = self._sandbox(flag="1", flag_age=7200, boot_age=600, status="1", hv="false", vfe="true")
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 10, transcript[-3000:])
        self.assertIn("reason=NoBootSinceFlag", self._record(sb) or "", "the request is recorded")
        # The student restarts (a real restart: the boot moves by minutes) and reopens.
        sb["env"]["MOCK_BOOT"] = self._boot(60)
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 11, transcript[-3000:])
        problem, nextstep = self._marker_pair(marker)
        self.assertEqual(problem, _finalize_const("RESTART_DID_NOT_HELP_PROBLEM_DE"))
        self.assertEqual(nextstep, _finalize_const("RESTART_DID_NOT_HELP_NEXTSTEP_DE"))
        self.assertNotIn("Neu starten, nicht Herunterfahren", nextstep, "no request for another restart")
        self.assertFalse(any(c.startswith("--import") for c in self._calls(sb)))
        # Reopened again in the same boot, and after yet another restart: it keeps
        # saying the restart did not help, and never asks for one again.
        for boot_ago in (60, 5):
            sb["env"]["MOCK_BOOT"] = self._boot(boot_ago)
            rc, transcript, _ = self._finalize(sb)
            self.assertEqual(rc, 11, transcript[-3000:])

    def test_reopening_without_a_restart_still_asks_for_one(self):
        """Same boot (or Fast Startup, which does not move LastBootUpTime)."""
        sb = self._sandbox(flag="1", flag_age=7200, boot_age=600, status="1", hv="false", vfe="true")
        self.assertEqual(self._finalize(sb)[0], 10)
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 10, transcript[-3000:])
        self.assertIn("nicht Herunterfahren", transcript)

    def test_a_clock_step_inside_the_tolerance_is_not_a_restart(self):
        sb = self._sandbox(flag="1", flag_age=7200, boot_age=600, status="1", hv="false", vfe="true")
        boot = sb["env"]["MOCK_BOOT"]
        self.assertEqual(self._finalize(sb)[0], 10)
        sb["env"]["MOCK_BOOT"] = (dt.datetime.fromisoformat(boot) + dt.timedelta(seconds=45)).isoformat()
        self.assertEqual(self._finalize(sb)[0], 10)

    def test_a_restart_that_uncovered_another_reason_gets_its_own_restart(self):
        """A pending feature enable asks for a restart; after it the WSL install
        needs one. That is progress, not a loop."""
        sb = self._sandbox(flag=None, boot_age=600, status="0", hv="false", vfe="true",
                           feat="VirtualMachinePlatform=EnablePending")
        self.assertEqual(self._finalize(sb)[0], 10)
        self.assertIn("reason=EnablePending", self._record(sb) or "")
        sb["env"]["MOCK_BOOT"] = self._boot(60)
        sb["env"]["MOCK_FEAT"] = ""
        with open(os.path.join(sb["state"], "status_seq"), "w") as fh:
            fh.write("1")
        rc, transcript, _ = self._finalize(sb)
        self.assertEqual(rc, 10, transcript[-3000:])
        self.assertIn("reason=NoBootSinceFlag", self._record(sb) or "")

    def test_a_stuck_feature_enable_is_bounded_too(self):
        sb = self._sandbox(flag=None, boot_age=600, status="0", hv="false", vfe="true",
                           feat="VirtualMachinePlatform=EnablePending")
        self.assertEqual(self._finalize(sb)[0], 10)
        sb["env"]["MOCK_BOOT"] = self._boot(60)
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 11, transcript[-3000:])
        self.assertEqual(self._marker_pair(marker)[0], _finalize_const("RESTART_DID_NOT_HELP_PROBLEM_DE"))

    def test_a_restart_that_helped_clears_the_record(self):
        """The legitimate first reboot: asked once, the restart settles it, the
        install completes — and no stale record can turn a LATER genuine
        request into „hat nicht geholfen"."""
        sb = self._sandbox(flag="1", flag_age=7, boot_age=1800, status="0", hv="false", vfe="true",
                           import_mode="ok", images=True)
        self.assertEqual(self._finalize(sb)[0], 10)
        self.assertIsNotNone(self._record(sb))
        sb["env"]["MOCK_BOOT"] = self._boot(60)
        sb["env"]["MOCK_HV"] = "true"
        rc, transcript, marker = self._finalize(sb)
        self.assertEqual(rc, 0, transcript[-3000:])
        self.assertIsNone(self._record(sb), "the verdict passed, so the request is settled")

    def test_an_unreadable_boot_time_keeps_asking_and_records_nothing(self):
        sb = self._sandbox(flag=None, boot_age=600, status="0", hv="false", vfe="true",
                           feat="VirtualMachinePlatform=EnablePending")
        sb["env"]["MOCK_BOOT"] = "throws"
        for _ in range(2):
            rc, transcript, _ = self._finalize(sb)
            self.assertEqual(rc, 10, transcript[-3000:])
        self.assertIsNone(self._record(sb))

    def test_the_record_reader_and_the_test_refuse_what_they_cannot_prove(self):
        ps = r"""
param([string]$Helper, [string]$Dir)
$ErrorActionPreference = "Continue"
. $Helper
$boot = [datetime]::Parse("2026-09-15T10:00:00")
$ok = Join-Path $Dir "ok"
Set-Content -LiteralPath $ok -Value (Format-RestartRequest -BootTime $boot -Reason "NoBootSinceFlag") -Encoding ASCII
$cases = [ordered]@{
  "missing" = (Join-Path $Dir "nope");
  "garbage" = "garbage"; "no_reason" = "noreason"; "bad_ticks" = "badticks"; "neg_ticks" = "negticks"
}
Set-Content -LiteralPath (Join-Path $Dir "garbage") -Value "hello" -Encoding ASCII
Set-Content -LiteralPath (Join-Path $Dir "noreason") -Value ("boot={0}" -f $boot.ToUniversalTime().Ticks) -Encoding ASCII
Set-Content -LiteralPath (Join-Path $Dir "badticks") -Value "boot=abc`nreason=NoBootSinceFlag" -Encoding ASCII
Set-Content -LiteralPath (Join-Path $Dir "negticks") -Value "boot=-5`nreason=NoBootSinceFlag" -Encoding ASCII
foreach ($k in $cases.Keys) {
  $path = $cases[$k]; if (-not [System.IO.Path]::IsPathRooted($path)) { $path = Join-Path $Dir $path }
  "read:{0}`t{1}" -f $k, ($null -eq (Read-RestartRequest -Path $path))
}
$req = Read-RestartRequest -Path $ok
"roundtrip`t{0}|{1}" -f ($req.Boot -eq $boot.ToUniversalTime()), $req.Reason
$st = @{ BootTime = $boot.AddSeconds(61) }
"moved61`t{0}" -f (Test-RestartDidNotHelp -State $st -Request $req -Reason "NoBootSinceFlag")
"moved60`t{0}" -f (Test-RestartDidNotHelp -State @{ BootTime = $boot.AddSeconds(60) } -Request $req -Reason "NoBootSinceFlag")
"other_reason`t{0}" -f (Test-RestartDidNotHelp -State $st -Request $req -Reason "EnablePending")
"no_reason`t{0}" -f (Test-RestartDidNotHelp -State $st -Request $req -Reason "")
"no_boot`t{0}" -f (Test-RestartDidNotHelp -State @{ BootTime = $null } -Request $req -Reason "NoBootSinceFlag")
"no_request`t{0}" -f (Test-RestartDidNotHelp -State $st -Request $null -Reason "NoBootSinceFlag")
"backwards`t{0}" -f (Test-RestartDidNotHelp -State @{ BootTime = $boot.AddSeconds(-600) } -Request $req -Reason "NoBootSinceFlag")
"""
        d = tempfile.mkdtemp(dir=self.tmp)
        r = self._ps("record.ps1", ps, "-Helper", _VIRT_PS1, "-Dir", d)
        got = dict(ln.split("\t", 1) for ln in r.stdout.decode("utf-8").splitlines() if "\t" in ln)
        for k in ("missing", "garbage", "no_reason", "bad_ticks", "neg_ticks"):
            self.assertEqual(got.get(f"read:{k}"), "True", (k, r.stderr.decode("utf-8", "replace")[-800:]))
        self.assertEqual(got.get("roundtrip"), "True|NoBootSinceFlag")
        self.assertEqual(got.get("moved61"), "True")
        self.assertEqual(got.get("moved60"), "False", "60 s is inside the tolerance")
        for k in ("other_reason", "no_reason", "no_boot", "no_request", "backwards"):
            self.assertEqual(got.get(k), "False", k)


class PrerequisitesExecutedTest(_InstallerSandbox):
    """install_prerequisites.ps1 keeps THIS run's reboot reason apart from an
    earlier step's preserved flag (the GUI's usbipd repair passes
    -PreserveExistingRebootFlag)."""

    def _prereq(self, sb, preserve):
        args = ["-Script", os.path.join(sb["scripts"], "install_prerequisites.ps1")]
        if preserve:
            args.append("-Preserve")
        r = self._ps("wrapper.ps1", _WRAPPER_PS, *args, env=sb["env"])
        return r.returncode, r.stdout.decode("utf-8", "replace")

    def test_a_preserved_stale_flag_is_not_announced_as_a_wsl2_reboot(self):
        """The field log's block 2: a stale flag, WSL healthy — „A REBOOT IS
        REQUIRED to complete WSL2 installation" and a skipped WSL update."""
        sb = self._sandbox(flag="1", flag_age=7200, boot_age=600, status="0", hv="true", vfe="true")
        rc, out = self._prereq(sb, preserve=True)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertNotIn("A REBOOT IS REQUIRED", out)
        self.assertIn("Neustart-Markierung aus einem früheren Einrichtungsschritt", out)
        self.assertIn("--update", self._calls(sb), "the WSL update must still run")
        with open(sb["flag"], "rb") as fh:
            self.assertEqual(fh.read(), b"1\r\n", "the preserved flag is kept verbatim")
        self.assertAlmostEqual(os.path.getmtime(sb["flag"]), sb["flag_mtime"], delta=1.0)

    def test_an_own_reason_refreshes_an_existing_flags_time_but_not_its_reason(self):
        """`wsl --install` ran in THIS boot: the reboot is owed after NOW, so the
        write time moves forward (else the verdict reads „rebooted since" and the
        student gets exit 11 instead of 10). The dd-uninstall reason survives."""
        sb = self._sandbox(flag="dd-uninstall", flag_age=7200, boot_age=600, status="1", hv="false", vfe="true")
        before = time.time()
        rc, out = self._prereq(sb, preserve=True)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("A REBOOT IS REQUIRED", out)
        self.assertNotIn("--update", self._calls(sb))
        with open(sb["flag"], "rb") as fh:
            self.assertEqual(fh.read(), b"dd-uninstall\r\n")
        self.assertGreaterEqual(os.path.getmtime(sb["flag"]), before - 2)

    def test_without_preserve_a_run_with_no_reason_drops_the_flag(self):
        sb = self._sandbox(flag="1", flag_age=7200, boot_age=600, status="0", hv="true", vfe="true")
        rc, out = self._prereq(sb, preserve=False)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertFalse(os.path.exists(sb["flag"]))
        self.assertNotIn("A REBOOT IS REQUIRED", out)


class PreflightExecutedTest(_InstallerSandbox):
    """preflight_system.ps1's WSL2 line asks the verdict finalize asks."""

    def _preflight(self, sb):
        r = self._ps("wrapper.ps1", _WRAPPER_PS, "-Script",
                     os.path.join(sb["scripts"], "preflight_system.ps1"), env=sb["env"])
        return r.returncode, r.stdout.decode("utf-8", "replace")

    def test_the_field_state_is_not_reported_as_active(self):
        """The 2026-09-07 launch printed „[OK] WSL2 aktiv" and then failed the
        import on HCS_E_SERVICE_NOT_AVAILABLE."""
        sb = self._sandbox(flag="1", flag_age=7, boot_age=1800, hv="false", vfe="true")
        rc, out = self._preflight(sb)
        self.assertEqual(rc, 0, "a diagnostic never gates")
        self.assertNotIn("[OK] WSL2", out)
        self.assertIn("[WARNUNG] WSL2 ist installiert, aber noch nicht einsatzbereit", out)
        self.assertIn("neu starten (Neu starten, nicht Herunterfahren)", out)

    def test_bios_off_names_the_same_remedy_finalize_gives(self):
        sb = self._sandbox(flag="1", flag_age=7200, boot_age=600, hv="false", vfe="false")
        _, out = self._preflight(sb)
        self.assertNotIn("[OK] WSL2", out)
        for name in ("VIRT_FIRMWARE_PROBLEM_DE", "VIRT_FIRMWARE_NEXTSTEP_DE"):
            self.assertIn(_finalize_const(name), out,
                          f"one remedy for one proof: finalize's ${name} verbatim")

    def test_a_firmware_reading_without_proof_warns_about_nothing(self):
        """VirtualizationDisabled with an UNREADABLE HypervisorPresent is not
        proof (a running hypervisor masks the firmware property)."""
        sb = self._sandbox(flag=None, boot_age=600, hv="null", vfe="false")
        _, out = self._preflight(sb)
        self.assertIn("[OK] WSL2 installiert", out)
        self.assertNotIn("[WARNUNG] WSL2", out)

    def test_an_unresponsive_wsl_is_not_reported_as_a_missing_distro(self):
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="true", list_mode="fail", lxss="EduBotics")
        _, out = self._preflight(sb)
        self.assertIn("[WARNUNG] WSL antwortet gerade nicht", out)
        self.assertNotIn("noch nicht vorhanden", out)

    def test_a_live_hypervisor_is_active(self):
        sb = self._sandbox(flag=None, boot_age=600, hv="true", vfe="false")
        _, out = self._preflight(sb)
        self.assertIn("[OK] WSL2 installiert, der Windows-Hypervisor läuft", out)
        self.assertNotIn("[WARNUNG] WSL2", out)

    def test_no_proof_downgrades_nothing(self):
        """Unknown — CIM unreadable, or the helper missing — must not produce a
        warning: refuse only on proof. It also must not claim a running hypervisor."""
        sb = self._sandbox(flag=None, boot_age=600, hv="throws", vfe="throws")
        _, out = self._preflight(sb)
        self.assertIn("[OK] WSL2 installiert", out)
        self.assertNotIn("Hypervisor läuft", out, "Unknown proves no running hypervisor")
        self.assertNotIn("[WARNUNG] WSL2", out)
        os.remove(os.path.join(sb["scripts"], "virtualization_ready.ps1"))
        _, out = self._preflight(sb)
        self.assertIn("[OK] WSL2 installiert", out)
        self.assertNotIn("[WARNUNG] WSL2", out)

    def test_a_restart_that_did_not_help_is_said_in_finalizes_words(self):
        """preflight runs beside finalize in the same launch; after a fruitless
        restart it must not print „bitte den PC neu starten" above finalize's
        „Ein Neustart hat nicht geholfen"."""
        sb = self._sandbox(flag="1", flag_age=7, boot_age=1800, hv="false", vfe="true")
        boot = dt.datetime.fromisoformat(sb["env"]["MOCK_BOOT"])
        earlier = boot - dt.timedelta(hours=1)
        ticks = int((earlier.astimezone(dt.timezone.utc).replace(tzinfo=None) - dt.datetime(1, 1, 1)).total_seconds()) * 10**7
        with open(os.path.join(sb["scripts"], ".restart_requested"), "w") as fh:
            fh.write(f"boot={ticks}\nreason=NoBootSinceFlag\n")
        _, out = self._preflight(sb)
        self.assertIn(_finalize_const("RESTART_DID_NOT_HELP_PROBLEM_DE"), out)
        self.assertIn(_finalize_const("RESTART_DID_NOT_HELP_NEXTSTEP_DE"), out)
        self.assertNotIn("noch nicht einsatzbereit", out)
        os.remove(os.path.join(sb["scripts"], ".restart_requested"))
        _, out = self._preflight(sb)
        self.assertIn("noch nicht einsatzbereit", out, "without a record it still asks for the restart")

    def test_wsl_missing_keeps_its_old_wording(self):
        sb = self._sandbox(flag=None, boot_age=600, status="1", hv="false", vfe="true")
        _, out = self._preflight(sb)
        self.assertIn("[WARNUNG] WSL2 noch nicht aktiv - wird bei der Einrichtung installiert", out)


if __name__ == "__main__":
    unittest.main()
