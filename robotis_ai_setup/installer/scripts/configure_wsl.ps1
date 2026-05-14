# configure_wsl.ps1 — Merge recommended settings into .wslconfig
# Does NOT overwrite existing settings — only adds missing ones.

$ErrorActionPreference = "Stop"

# When running elevated (as admin), $env:USERPROFILE points to the admin's
# home, not the logged-in student's. Find the actual user via explorer.exe.
#
# Audit H21: explorer.exe is the primary signal but it isn't always
# running (services-only installs, OOBE-finished-but-not-yet-shell,
# SCCM-deployed kiosks). Fall back to enumerating interactive logon
# sessions (LogonType=2) before falling back to USERPROFILE — that
# fallback can be the admin home and clobber the wrong .wslconfig.
$realProfile = $null
try {
    $explorerProc = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction Stop | Select-Object -First 1
    if ($explorerProc) {
        $ownerInfo = Invoke-CimMethod -InputObject $explorerProc -MethodName GetOwner -ErrorAction Stop
        $loggedInUser = $ownerInfo.User
        $loggedInDomain = $ownerInfo.Domain
        $sid = (New-Object System.Security.Principal.NTAccount("$loggedInDomain\$loggedInUser")).Translate([System.Security.Principal.SecurityIdentifier]).Value
        $realProfile = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid").ProfileImagePath
    }
} catch {
    $realProfile = $null
}
if (-not $realProfile) {
    # Audit H21 fallback: interactive logon session lookup. LogonType=2
    # is "Interactive" (the actual user sitting at the keyboard).
    try {
        $session = Get-CimInstance Win32_LogonSession -Filter "LogonType=2" -ErrorAction Stop |
            Sort-Object StartTime -Descending |
            Select-Object -First 1
        if ($session) {
            $logon = Get-CimAssociatedInstance -InputObject $session `
                -Association Win32_LoggedOnUser -ErrorAction Stop |
                Select-Object -First 1
            if ($logon) {
                $sid = (New-Object System.Security.Principal.NTAccount("$($logon.Domain)\$($logon.Name)")).Translate([System.Security.Principal.SecurityIdentifier]).Value
                $realProfile = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid").ProfileImagePath
            }
        }
    } catch {
        $realProfile = $null
    }
}
if (-not $realProfile) {
    # Last-resort fallback. Likely wrong on elevated installer runs,
    # but better than crashing — log loudly so the operator can spot
    # a misplaced .wslconfig.
    Write-Host "   (Warnung: weder explorer.exe noch interaktive Logon-Sitzung erkannt; nutze USERPROFILE-Fallback)" -ForegroundColor Yellow
    $realProfile = $env:USERPROFILE
}
$wslConfigPath = "$realProfile\.wslconfig"

$recommendedSettings = @{
    "memory" = "8GB"
    "swap"   = "4GB"
}

function Write-Step { param([string]$msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "   OK: $msg" -ForegroundColor Green }

Write-Step "Configuring .wslconfig..."

# Read existing config if it exists
$existingContent = ""
if (Test-Path $wslConfigPath) {
    $existingContent = Get-Content $wslConfigPath -Raw
    Write-Host "   Existing .wslconfig found, merging settings..." -ForegroundColor White
} else {
    Write-Host "   Creating new .wslconfig..." -ForegroundColor White
}

# Parse existing settings
$existingSettings = @{}
foreach ($line in ($existingContent -split "`n")) {
    $trimmed = $line.Trim()
    if ($trimmed -match "^(\w+)\s*=\s*(.+)$") {
        $existingSettings[$Matches[1].ToLower()] = $Matches[2].Trim()
    }
}

# Merge: only add settings not already present
$newLines = @()
$hasWsl2Section = $existingContent -match "\[wsl2\]"

if (-not $hasWsl2Section) {
    $newLines += "[wsl2]"
}

$addedCount = 0
foreach ($key in $recommendedSettings.Keys) {
    if (-not $existingSettings.ContainsKey($key.ToLower())) {
        $newLines += "$key=$($recommendedSettings[$key])"
        $addedCount++
        Write-Host "   Adding: $key=$($recommendedSettings[$key])" -ForegroundColor White
    } else {
        Write-Host "   Keeping existing: $key=$($existingSettings[$key.ToLower()])" -ForegroundColor Yellow
    }
}

if ($addedCount -gt 0) {
    if ($existingContent -and -not $existingContent.EndsWith("`n")) {
        $existingContent += "`n"
    }
    $finalContent = $existingContent + ($newLines -join "`n") + "`n"
    Set-Content -Path $wslConfigPath -Value $finalContent -NoNewline
    Write-OK "Added $addedCount setting(s) to .wslconfig"
} else {
    Write-OK "All recommended settings already present"
}

# networkingMode left at default (NAT). WSL2's built-in localhost
# forwarder proxies Windows localhost:PORT to the distro's loopback,
# which is what the GUI's "Browser oeffnen" relies on. Mirrored mode
# would also work but has known incompatibilities with some VPNs and
# AV products in school IT environments.
#
# (Historical note: an older revision of this comment said
# "Docker Desktop manages its own port forwarding from WSL2 to Windows".
# That was true when EduBotics shipped with Docker Desktop. We removed
# Docker Desktop in favor of a bundled headless dockerd inside the
# EduBotics WSL distro, so DD is no longer in the picture.)
Write-Host "   Note: networkingMode left at default (WSL2 localhost forwarder)" -ForegroundColor Gray
