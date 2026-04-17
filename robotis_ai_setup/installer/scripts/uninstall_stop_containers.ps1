# uninstall_stop_containers.ps1 — Best-effort `docker compose down` before
# the distro is unregistered. Called by the uninstaller before removing files.
#
# The install location isn't hardcoded — we derive it from the script's own
# path (this script lives at {app}\scripts\) and convert to the WSL form.

param(
    [string]$DistroName = "EduBotics"
)

$ErrorActionPreference = "Continue"

function To-WslPath {
    param([string]$WinPath)
    if (-not $WinPath) { return $WinPath }
    $p = $WinPath -replace '\\', '/'
    if ($p.Length -ge 2 -and $p[1] -eq ':') {
        $drive = $p.Substring(0, 1).ToLower()
        $rest = $p.Substring(2).TrimStart('/')
        return "/mnt/$drive/$rest"
    }
    return $p
}

# $PSScriptRoot = {app}\scripts → {app} = parent → docker dir = {app}\docker
$appRoot = Split-Path -Parent $PSScriptRoot
$dockerDir = Join-Path $appRoot "docker"
$wslDockerDir = To-WslPath $dockerDir

# Is the distro even registered?
$listed = $false
try {
    $out = wsl --list --quiet 2>&1
    foreach ($line in $out) {
        if (($line -replace "`0", "").Trim() -eq $DistroName) { $listed = $true; break }
    }
} catch { }

if (-not $listed) { exit 0 }

# Is docker running inside it?
wsl -d $DistroName -- docker info *>$null 2>&1
if ($LASTEXITCODE -ne 0) { exit 0 }

# Stop the stack
wsl -d $DistroName --cd $wslDockerDir -- docker compose down *>$null 2>&1
exit 0
