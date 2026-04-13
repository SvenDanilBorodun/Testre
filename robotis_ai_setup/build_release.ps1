# EduBotics Build Script — builds GUI .exe and installer on Windows
# Run from: robotis_ai_setup/
# Requires: Python 3.11+, pip, Inno Setup 6
#
# Usage:
#   cd robotis_ai_setup
#   powershell -ExecutionPolicy Bypass -File build_release.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " EduBotics Release Build" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# --- Step 1: Build GUI with PyInstaller ---
Write-Host "`n[1/3] Building EduBotics GUI..." -ForegroundColor Yellow

Push-Location "$ScriptDir\gui"
try {
    pip install pyinstaller --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }

    pyinstaller build.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

    if (!(Test-Path "dist\EduBotics\EduBotics.exe")) {
        throw "EduBotics.exe not found after build"
    }
    $size = [math]::Round((Get-Item "dist\EduBotics\EduBotics.exe").Length / 1MB, 1)
    Write-Host "  EduBotics.exe built ($size MB)" -ForegroundColor Green
} finally {
    Pop-Location
}

# --- Step 2: Build Installer with Inno Setup ---
Write-Host "`n[2/3] Building Installer..." -ForegroundColor Yellow

$isccPaths = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$iscc = $null
foreach ($p in $isccPaths) {
    if (Test-Path $p) { $iscc = $p; break }
}

if ($null -eq $iscc) {
    Write-Host "  WARNUNG: Inno Setup 6 nicht gefunden. Installer wird uebersprungen." -ForegroundColor Red
    Write-Host "  Download: https://jrsoftware.org/isdl.php" -ForegroundColor Red
} else {
    Push-Location "$ScriptDir\installer"
    try {
        & $iscc robotis_ai_setup.iss
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed" }

        if (!(Test-Path "output\EduBotics_Setup.exe")) {
            throw "EduBotics_Setup.exe not found after build"
        }
        $size = [math]::Round((Get-Item "output\EduBotics_Setup.exe").Length / 1MB, 1)
        Write-Host "  EduBotics_Setup.exe built ($size MB)" -ForegroundColor Green
    } finally {
        Pop-Location
    }
}

# --- Step 3: Summary ---
Write-Host "`n[3/3] Build complete!" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan

if (Test-Path "$ScriptDir\gui\dist\EduBotics\EduBotics.exe") {
    Write-Host "  GUI:       $ScriptDir\gui\dist\EduBotics\EduBotics.exe" -ForegroundColor Green
}
if (Test-Path "$ScriptDir\installer\output\EduBotics_Setup.exe") {
    Write-Host "  Installer: $ScriptDir\installer\output\EduBotics_Setup.exe" -ForegroundColor Green
}

Write-Host "========================================" -ForegroundColor Cyan
