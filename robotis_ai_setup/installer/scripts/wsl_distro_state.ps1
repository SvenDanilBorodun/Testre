# wsl_distro_state.ps1 — "is the EduBotics distro registered?" with THREE answers.
#
# Dot-source it (do NOT run it standalone):
#     . (Join-Path $PSScriptRoot 'wsl_distro_state.ps1')
#     switch (Get-EduBoticsDistroRegistration -DistroName "EduBotics") { ... }
#
# Returns exactly one of:
#   "Registered"   — wsl listed it (or a listing that failed is contradicted by
#                    nothing: see below).
#   "Absent"       — wsl answered and did not list it, wsl said it has no
#                    distributions at all, or wsl did not answer but the
#                    per-user registration store has no such distro.
#   "Unresponsive" — wsl did not answer AND the registration store either names
#                    the distro or cannot be read. WSL is not answering; the
#                    distro may well exist.
#
# WHY THREE (2026-09-07 German school PC). Every reader used to collapse "wsl
# --list failed" into "not registered". On a PC whose Windows VM service was
# down, `wsl --list --quiet` failed, the GUI routed into a finalize that ran
# `wsl --import` (HCS_E_SERVICE_NOT_AVAILABLE), and „Arme scannen" told the
# student „Die EduBotics-WSL-Umgebung ist nicht registriert. Bitte den Installer
# erneut ausführen." — minutes before the same machine answered with the distro
# present. "WSL is not answering" must never be reported as "missing", never
# start an import and never reach `wsl --unregister`.
#
# THE REGISTRY IS THE TIE-BREAKER, NOT THE PRIMARY. WSL keeps per-user
# registrations under HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss\{GUID}
# with a DistributionName value — the store `wsl --list` itself reads. It is
# consulted ONLY when the listing failed, because a failed listing on a fresh PC
# (zero distros: store WSL exits non-zero with Wsl/WSL_E_DEFAULT_DISTRO_NOT_FOUND,
# inbox WSL with a localized sentence and no token) is indistinguishable from a
# broken WSL by exit code alone, and matching the localized sentence is the
# mistake this codebase keeps deleting. A missing Lxss key is "no distros" (read
# fine); any other registry error is "cannot tell". HKCU is per Windows account,
# exactly like `wsl --list` — an elevated finalize run by a DIFFERENT admin sees
# that admin's hive either way (see the finalize marker's `user=` contract).
#
# TWIN: gui/app/wsl_bridge.py::resolve_distro_registration makes the same
# decision for the GUI (lower-cased words); both are driven over one input table
# by tests/test_installer_pwsh_executed.py::DistroRegistrationExecutedTest.
#
# CALLER CONTRACT — mirrors wsl_docker_ready.ps1:
#   * Keep $ErrorActionPreference = "Continue"; this file never assigns it.
#   * No `exit`, no `throw`: it returns a word.
#   * One native call (`wsl --list --quiet`), stderr merged and NULs stripped
#     (wsl.exe writes BOM-less UTF-16LE to a pipe).

# Registry facts: @{ Readable = [bool]; Names = [string[]] }.
function Get-LxssDistroNames {
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
    $subkeys = @()
    try {
        $subkeys = @(Get-ChildItem -LiteralPath $key -ErrorAction Stop)
    } catch [System.Management.Automation.ItemNotFoundException] {
        # No Lxss key at all: nothing has ever been registered for this account.
        return @{ Readable = $true; Names = @() }
    } catch {
        return @{ Readable = $false; Names = @() }
    }
    $names = @()
    foreach ($sub in $subkeys) {
        try {
            $name = $sub.GetValue('DistributionName')
            if ($name) { $names += [string]$name }
        } catch { }
    }
    return @{ Readable = $true; Names = $names }
}

# Pure: the three-way decision over already-gathered facts.
function Resolve-DistroRegistration {
    param(
        [string]$DistroName,
        [bool]$ListRan,
        [int]$ListExitCode,
        [string]$ListText,
        [bool]$RegistryReadable,
        [string[]]$RegistryNames
    )
    $listed = $false
    if ($ListRan -and $ListText) {
        foreach ($line in ($ListText -split "`r?`n")) {
            if (($line -replace "`0", "").Trim() -eq $DistroName) { $listed = $true; break }
        }
    }
    if ($listed) { return "Registered" }
    if ($ListRan -and $ListExitCode -eq 0) { return "Absent" }
    # wsl did not produce a listing. It may still have said something DEFINITE:
    # the store build's "no distributions at all" carries an ASCII code token
    # (whitespace/NUL-stripped, like virtualization_ready.ps1's classifier).
    $flat = ([string]$ListText -replace '[\s\x00]', '')
    if ($flat -like '*WSL_E_DEFAULT_DISTRO_NOT_FOUND*') { return "Absent" }
    if ($RegistryReadable) {
        if (@($RegistryNames) -contains $DistroName) { return "Unresponsive" }
        return "Absent"
    }
    return "Unresponsive"
}

function Get-EduBoticsDistroRegistration {
    param(
        [string]$DistroName = "EduBotics"
    )
    $listText = ""
    $listRc = -1
    $listRan = $false
    try {
        $listText = ((wsl --list --quiet 2>&1 | Out-String -Width 4096) -replace "`0", "")
        $listRc = $LASTEXITCODE
        $listRan = $true
    } catch {
        # wsl.exe missing (WSL never installed) or not invokable.
        $listRan = $false
    }
    $registry = Get-LxssDistroNames
    return (Resolve-DistroRegistration -DistroName $DistroName -ListRan $listRan `
        -ListExitCode $listRc -ListText $listText `
        -RegistryReadable $registry.Readable -RegistryNames $registry.Names)
}
