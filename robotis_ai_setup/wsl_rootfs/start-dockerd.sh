#!/bin/sh
# Idempotent dockerd launcher for WSL2. Invoked from /etc/wsl.conf [boot] command.
# The WSL boot context has an empty PATH — set it explicitly so dockerd finds
# containerd, runc, etc.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

if ! pgrep -x dockerd >/dev/null 2>&1; then
    nohup /usr/bin/dockerd > /var/log/dockerd.log 2>&1 &
fi
