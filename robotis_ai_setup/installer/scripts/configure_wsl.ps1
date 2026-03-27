# configure_wsl.ps1 — Merge recommended settings into .wslconfig
# Does NOT overwrite existing settings — only adds missing ones.

$ErrorActionPreference = "Stop"

$wslConfigPath = "$env:USERPROFILE\.wslconfig"

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

# IMPORTANT: Do NOT set networkingMode=mirrored
# Docker Desktop manages its own port forwarding from WSL2 to Windows
Write-Host "   Note: networkingMode left at default (Docker Desktop manages port forwarding)" -ForegroundColor Gray
