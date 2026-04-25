#!/bin/sh
# Idempotent dockerd launcher + watchdog for WSL2. Invoked from
# /etc/wsl.conf [boot] command.
#
# The WSL boot context has an empty PATH — set it explicitly so dockerd finds
# containerd, runc, etc.
#
# Previously this script spawned dockerd once and exited. If the daemon later
# segfaulted or OOMed, nothing restarted it; `docker info` would silently fail
# until the student manually `wsl --terminate`d and re-entered the distro.
# The watchdog loop below checks every 5s and respawns if dockerd is missing.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

spawn_dockerd() {
    nohup /usr/bin/dockerd >> /var/log/dockerd.log 2>&1 &
}

# First-time start.
if ! pgrep -x dockerd >/dev/null 2>&1; then
    spawn_dockerd
fi

# Background watchdog. The boot-command shell exits as soon as this
# script returns; without `nohup` here, a SIGHUP to the boot shell
# (e.g. on a non-default huponexit, or some shells) would also tear
# down the bare `( ... ) &` subshell — leaving a single dockerd with
# no respawn on the next crash, exactly the failure mode this watchdog
# was meant to fix. `nohup sh -c '...'` ignores SIGHUP unconditionally,
# matching how `spawn_dockerd` already protects dockerd itself.
nohup sh -c '
    while true; do
        sleep 5
        if ! pgrep -x dockerd >/dev/null 2>&1; then
            echo "[$(date -u +%FT%TZ)] dockerd not running — respawning" \
                >> /var/log/dockerd.log
            nohup /usr/bin/dockerd >> /var/log/dockerd.log 2>&1 &
        fi
    done
' </dev/null >/dev/null 2>&1 &
