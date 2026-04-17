# EduBotics WSL2 Rootfs — headless Docker Engine inside WSL2
#
# This image is NOT run as a container. It is built, then exported as a tar
# with `docker export`, which becomes the rootfs that `wsl --import` loads.
# Students never see "Docker" — the distro appears as "EduBotics".
#
# Layers kept minimal on purpose; apt cache and docs are stripped at the end.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Base packages + Docker prerequisites + NVIDIA toolkit repo.
# tzdata is critical: docker-compose.yml mounts /etc/timezone and /etc/localtime
# into every container as read-only files. Without tzdata, these don't exist
# as real files and Docker auto-creates them as empty dirs → containers fail
# to start with "Are you trying to mount a directory onto a file?"
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        iproute2 \
        iputils-ping \
        jq \
        kmod \
        lsb-release \
        systemd \
        tzdata \
        udev \
        usbutils \
        v4l-utils \
    && ln -sf /usr/share/zoneinfo/Europe/Berlin /etc/localtime \
    && echo "Europe/Berlin" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Docker CE apt repository
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
         | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu jammy stable" \
         > /etc/apt/sources.list.d/docker.list

# NVIDIA container toolkit apt repository (works on WSL2 when Win host has NVIDIA driver)
RUN curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
         | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
    && curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
         | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
         > /etc/apt/sources.list.d/nvidia-container-toolkit.list

# Docker Engine pinned to 27.5.1 — critical for shipping stability.
# Docker 29.x (Ubuntu's current default from the docker-ce repo) has a
# containerd-snapshotter regression that corrupts pulls of large multi-layer
# images on custom-imported WSL2 rootfs: the extract phase fails with
# "snapshot ... does not exist" and the pull never completes. Docker 29
# removed the ability to disable containerd-snapshotter via config, so
# downgrading is the only reliable fix.
#
# Docker 27.5.1 keeps overlay2 as the default storage driver (containerd-
# snapshotter is opt-in), which is battle-tested on WSL2 for years.
ARG DOCKER_VERSION=5:27.5.1-1~ubuntu.22.04~jammy
ARG CONTAINERD_VERSION=1.7.27-1

RUN apt-get update && apt-get install -y --no-install-recommends \
        docker-ce=${DOCKER_VERSION} \
        docker-ce-cli=${DOCKER_VERSION} \
        containerd.io=${CONTAINERD_VERSION} \
        docker-buildx-plugin \
        docker-compose-plugin \
        nvidia-container-toolkit \
    && apt-mark hold docker-ce docker-ce-cli containerd.io \
    && rm -rf /var/lib/apt/lists/*

# WSL-specific init config — runs start-dockerd.sh on every boot
COPY wsl.conf /etc/wsl.conf

# Docker daemon config — register NVIDIA runtime so docker-compose.gpu.yml works.
# userland-proxy is disabled so dockerd doesn't need the absolute proxy path
# injected by Docker Desktop (WSL2 uses iptables port forwarding anyway).
COPY daemon.json /etc/docker/daemon.json

# dockerd launcher invoked from /etc/wsl.conf [boot] command. Idempotent.
COPY start-dockerd.sh /usr/local/bin/start-dockerd.sh
RUN chmod +x /usr/local/bin/start-dockerd.sh

# Friendly hostname — shows up in wsl --list and inside the shell
RUN echo "edubotics" > /etc/hostname

# Prune unneeded files so the exported tar stays ~350-450MB compressed
RUN apt-get clean \
    && rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/* \
              /usr/share/doc /usr/share/man /usr/share/locale \
              /tmp/* /var/tmp/*
