# stage_bundle.ps1 — Relocate the OFFLINE image bundle to a persistent dir
#
# The big "EduBotics_Setup_Full.exe" is a 7-Zip SFX that extracts the inner
# installer + images/<repo>.tar.gz + bundled_digests.json to a %TEMP% dir and
# auto-runs the inner installer from there. That %TEMP% dir is DESTROYED when
# the inner installer exits — and on a fresh Windows PC `wsl --install` forces
# a reboot, so the image load is deferred to finalize_install.ps1 which runs
# AFTER %TEMP% is gone. It also detaches from the SFX's %TEMP% cleanup on UAC
# re-elevation. So we MOVE the bundle out of {src} (%TEMP%) into
# %ProgramData%\EduBotics\bundle (persistent, survives reboot, machine-wide)
# BEFORE Steps 4/5 and before any reboot. load_images.ps1 (Step 5 and the
# post-reboot finalize) then load from there.
#
# No-op (exit 0) when this .exe carries NO bundle — i.e. the small online /
# self-update EduBotics_Setup.exe, whose {src} has no bundled_digests.json.

param(
    [Parameter(Mandatory = $true)][string]$SrcDir,
    [string]$DestDir = (Join-Path $env:ProgramData "EduBotics\bundle")
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$m) Write-Host "`n>> $m" -ForegroundColor Cyan }
function Write-OK   { param([string]$m) Write-Host "   OK: $m" -ForegroundColor Green }

$marker = Join-Path $SrcDir "bundled_digests.json"
if (-not (Test-Path -LiteralPath $marker)) {
    # Online / self-update installer — nothing to stage. load_images.ps1 will
    # find no bundle dir and fall back to a normal Docker Hub pull.
    Write-Host "Kein Offline-Paket in dieser Installationsdatei — wird übersprungen."
    exit 0
}

$srcImages = Join-Path $SrcDir "images"
if (-not (Test-Path -LiteralPath $srcImages)) {
    Write-Host "[FEHLER] Offline-Paket unvollständig: 'images' fehlt neben der Installationsdatei." -ForegroundColor Red
    exit 1
}

Write-Step "Installationsdaten werden vorbereitet..."

# Cross-volume guard: a same-drive move is an instant rename; a cross-drive
# move is a full copy+delete needing the bundle's size free on the DESTINATION
# volume. %TEMP% is sometimes redirected to a second drive on managed PCs, so
# check before committing — fail LOUD rather than let a later step silently
# fall back to a multi-GB Docker Hub download.
try {
    $srcRoot  = [System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $SrcDir).Path)
    $destRoot = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($DestDir))
    if ($srcRoot -and $destRoot -and ($srcRoot.TrimEnd('\') -ne $destRoot.TrimEnd('\'))) {
        $bundleBytes = (Get-ChildItem -LiteralPath $SrcDir -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
        $destDrive = $destRoot.TrimEnd('\').TrimEnd(':')
        $freeBytes = (Get-PSDrive -Name $destDrive -ErrorAction Stop).Free
        # +2 GB headroom for filesystem overhead / concurrent writes.
        if ($freeBytes -lt ($bundleBytes + 2GB)) {
            $needGb = [math]::Round(($bundleBytes + 2GB) / 1GB, 1)
            $freeGb = [math]::Round($freeBytes / 1GB, 1)
            Write-Host "[STOPP] Zu wenig Speicherplatz auf Laufwerk ${destDrive}: ($freeGb GB frei, ca. $needGb GB benötigt)." -ForegroundColor Red
            Write-Host "        Bitte Speicherplatz freigeben und die Installation erneut ausführen." -ForegroundColor Red
            exit 1
        }
    }
} catch {
    # A failed free-space probe must not block a same-volume rename; only the
    # genuine cross-volume shortfall above is fatal.
    Write-Host "   Hinweis: Speicherplatzprüfung übersprungen ($($_.Exception.Message))."
}

try {
    if (Test-Path -LiteralPath $DestDir) {
        # A prior aborted install may have left a partial bundle — start clean.
        Remove-Item -LiteralPath $DestDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Path $DestDir -Force | Out-Null

    # The per-image <repo>.tar.gz.sha256 checksums live INSIDE images/ (the CI
    # writes them there) and are carried by this whole-directory move — so
    # load_images.ps1 finds them at $DestDir\images\<repo>.tar.gz.sha256. Do NOT
    # add a separate SrcDir-root *.sha256 sweep: it would move nothing today and
    # would silently relocate the checksums away from where load_images reads
    # them if the CI layout ever changed.
    Move-Item -LiteralPath $srcImages -Destination (Join-Path $DestDir "images") -Force
    Move-Item -LiteralPath $marker -Destination (Join-Path $DestDir "bundled_digests.json") -Force
} catch {
    Write-Host "[FEHLER] Installationsdaten konnten nicht vorbereitet werden: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-OK "Installationsdaten vorbereitet ($DestDir)"
exit 0
