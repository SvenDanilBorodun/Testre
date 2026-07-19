# wsl_docker_ready.ps1 — Single source of truth for dockerd-readiness in the
# EduBotics WSL2 distro.
#
# Dot-source it from a caller (do NOT run it standalone):
#     . (Join-Path $PSScriptRoot 'wsl_docker_ready.ps1')
#     if (-not (Wait-DockerReady -DistroName 'EduBotics')) { ... }
#
# The CALLER MUST keep $ErrorActionPreference = "Continue". This function makes
# native `wsl`/`docker` calls; under EAP=Stop a native command writing to stderr
# (docker info prints a swap-limit warning even on a healthy daemon) is promoted
# to a TERMINATING NativeCommandError, which would abort the readiness poll on
# the first iteration. Readiness is decided by EXIT CODE only.

function Wait-DockerReady {
    param(
        [string]$DistroName          = "EduBotics",
        [int]$MaxWaitSeconds         = 180,
        [int]$FallbackWaitSeconds    = 30,
        # Skip the start-dockerd.sh fallback entirely: for READ-ONLY callers
        # (preflight diagnostics) that must neither mutate the distro nor blow
        # past a small time budget. Repair-capable callers (finalize,
        # pull_images) keep the fallback.
        [switch]$NoFallback,
        # Optional [ref] to a string that receives the LAST `docker info` stderr.
        # import_edubotics_wsl.ps1 kept a full inline COPY of this function purely
        # to own this reporting path — two divergent implementations of
        # safety-critical readiness logic. Folding it in here is what let that
        # copy go (2026-07-19). $null when the caller does not ask for it.
        [ref]$LastError,
        # Emit the German per-poll progress line + the fallback notice. Off by
        # default so finalize/pull_images keep their terse output; import turns it
        # on because its wait is the LONGEST (180 s straight after a fresh
        # `wsl --import`) and a silent transcript there reads as a hang.
        [switch]$ShowProgress
    )

    # Step 1: trigger VM boot. First `wsl -d` invocation starts the lightweight
    # VM; `echo` is just a ping to force startup. *>$null discards all streams.
    wsl -d $DistroName -- echo ready *>$null

    # Route `docker info` stderr to a TEMP FILE (not `2>&1` into the pipeline):
    # readiness is decided by the exit code alone, and merging stderr would emit
    # a NativeCommandError record on every poll. GetTempFileName() pre-creates
    # the file (so the cleanup below can't hit a missing target) and gives one
    # consistent absolute path — but it reads the same TMP env var as $env:TEMP
    # and does NOT expand an 8.3 tilde path (dotted username, F2), so the
    # try/catch around every consumer below stays the load-bearing guard.
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        # Step 2: poll up to $MaxWaitSeconds for a healthy daemon.
        $elapsed = 0
        while ($elapsed -lt $MaxWaitSeconds) {
            & wsl -d $DistroName -- docker info 1>$null 2>$errFile
            if ($LASTEXITCODE -eq 0) { return $true }
            # Keep the daemon's own words for the caller's failure message.
            # try/catch, not -ErrorAction: binding a malformed/8.3 path to
            # -LiteralPath raises a TERMINATING PSArgumentException that
            # -ErrorAction cannot suppress (F2).
            if ($null -ne $LastError) {
                try { $LastError.Value = (Get-Content -LiteralPath $errFile -Raw -ErrorAction Stop) } catch { }
            }
            Start-Sleep -Seconds 2
            $elapsed += 2
            if ($ShowProgress) {
                Write-Host "   Warte auf Docker-Engine... ${elapsed}s/${MaxWaitSeconds}s" -ForegroundColor Gray
            }
        }

        # Fallback: boot-time autostart didn't fire — invoke the dockerd wrapper
        # directly (this distro uses /usr/local/bin/start-dockerd.sh, NOT
        # systemd), then poll again up to $FallbackWaitSeconds.
        if ($NoFallback) { return $false }
        if ($ShowProgress) {
            # Write-Host only, never a Write-Warn/Write-FAIL helper: those are
            # defined by the CALLERS and pull_images.ps1 has no Write-Warn, so
            # calling one from here would blow up in exactly the caller we are
            # least able to test from this file.
            Write-Host "   dockerd nicht automatisch gestartet - Wrapper wird manuell ausgeführt" -ForegroundColor Yellow
            if ($null -ne $LastError) {
                Write-Host "   Letzte docker-info-Meldung: $($LastError.Value)" -ForegroundColor Gray
            }
        }
        wsl -d $DistroName -- /usr/local/bin/start-dockerd.sh *>$null
        $extra = 0
        while ($extra -lt $FallbackWaitSeconds) {
            Start-Sleep -Seconds 2
            $extra += 2
            & wsl -d $DistroName -- docker info 1>$null 2>$errFile
            if ($LASTEXITCODE -eq 0) { return $true }
            if ($null -ne $LastError) {
                try { $LastError.Value = (Get-Content -LiteralPath $errFile -Raw -ErrorAction Stop) } catch { }
            }
        }
    } finally {
        try { Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue } catch { }
    }

    return $false
}
