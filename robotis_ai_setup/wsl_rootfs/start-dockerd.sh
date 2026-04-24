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

# Background watchdog. Fire-and-forget via nohup so killing the boot-command
# shell doesn't take the watchdog with it.
(
    while true; do
        sleep 5
        if ! pgrep -x dockerd >/dev/null 2>&1; then
            echo "[$(date -u +%FT%TZ)] dockerd not running — respawning" \
                >> /var/log/dockerd.log
            spawn_dockerd
        fi
    done
) </dev/null >/dev/null 2>&1 &
