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

# 2026-05-18 — USB-Serial bring-up (the "Arme nicht erkannt" fix):
#
# Stock Microsoft WSL2 kernels ship CDC-ACM and the common USB-to-Serial
# bridges (FTDI, CP210x, CH341, PL2303) as MODULES, not built-in. After a
# distro boot the modules sit in /lib/modules/$(uname -r)/ but are NOT
# loaded — so when usbipd attaches a ROBOTIS OpenRB-150 (or any USB-serial
# device) the kernel sees the USB device but never creates /dev/ttyACM*,
# and identify_arm.py / ros2_control find nothing. Loading the modules
# here, on every distro boot, makes USB-Serial available before any
# usbipd attach can race the driver lookup.
#
# udev: the `udev` apt package ships /lib/udev/rules.d/60-serial.rules
# (which is what builds /dev/serial/by-id/*) but the daemon itself,
# `systemd-udevd`, only auto-starts under systemd. WSL doesn't run
# systemd by default. Starting it standalone here is the documented
# workaround. `udevadm trigger --action=add` then replays the events for
# any already-attached devices so the by-id symlinks appear immediately
# instead of waiting for the next plug-event.
#
# All steps are best-effort: a missing module, a missing udev binary,
# or a non-zero udevd start must not block dockerd from coming up.

# Audit-2 (next maintainer): if you add another USB-Serial bridge here,
# also add its FW/driver name to `device_manager.diagnose_usb_environment`
# so the GUI's "Arme scannen" rescue path knows to load it too.
modprobe cdc_acm 2>/dev/null || true       # OpenRB-150, Arduino, most modern dev boards
modprobe ftdi_sio 2>/dev/null || true      # FTDI FT232, FT2232
modprobe cp210x 2>/dev/null || true        # Silicon Labs CP210x (very common ESP32 / Sparkfun)
modprobe ch341 2>/dev/null || true         # WCH CH340/CH341 (Aliexpress-Arduino-Clones)
modprobe pl2303 2>/dev/null || true        # Prolific PL2303 (old USB-Serial cables)

if [ -x /lib/systemd/systemd-udevd ]; then
    if ! pgrep -x systemd-udevd >/dev/null 2>&1; then
        /lib/systemd/systemd-udevd --daemon 2>/dev/null || true
        # Tiny window for the daemon to enter its poll loop before we
        # ask it to replay events; without this, `udevadm trigger`
        # arrives at a not-yet-listening socket.
        sleep 1
    fi
    udevadm trigger --action=add --subsystem-match=tty 2>/dev/null || true
    udevadm trigger --action=add --subsystem-match=usb 2>/dev/null || true
    udevadm settle --timeout=3 2>/dev/null || true
fi

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
