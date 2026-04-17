"""Docker Compose lifecycle management (routed through the EduBotics WSL2 distro).

Every `docker` invocation is wrapped as `wsl -d EduBotics --cd <cwd> -- docker ...`
so the GUI never depends on Docker Desktop being installed on the host. The
distro ships its own headless Docker Engine (see `wsl_rootfs/`).

Handles:
  - Booting the EduBotics distro and waiting for dockerd
  - Pulling images if needed
  - Starting/stopping containers
  - GPU detection (via nvidia-smi on the Windows host — the WSL NVIDIA driver
    is shared from the host, so host visibility == distro visibility)
"""

import os
import subprocess
import sys
import time
from typing import Optional

# On Windows, hide console windows spawned by subprocess
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}

from .constants import (
    ALL_IMAGES,
    COMPOSE_FILE,
    COMPOSE_GPU_FILE,
    DOCKER_DIR,
    DOCKER_DIR_WSL,
    DOCKER_STARTUP_TIMEOUT,
    ENV_FILE,
    WSL_DISTRO_NAME,
    _to_wsl_path,
)


class DockerError(Exception):
    """Raised when a Docker operation fails."""


def _docker_cmd(*args: str, cwd_wsl: Optional[str] = None) -> list[str]:
    """Build `wsl -d <distro> [--cd <path>] -- docker <args...>`.

    cwd_wsl is a POSIX path INSIDE the WSL distro (e.g. /mnt/c/Program Files/...).
    """
    cmd = ["wsl", "-d", WSL_DISTRO_NAME]
    if cwd_wsl:
        cmd.extend(["--cd", cwd_wsl])
    cmd.append("--")
    cmd.append("docker")
    cmd.extend(args)
    return cmd


def is_distro_registered() -> bool:
    """Return True iff the EduBotics WSL2 distro is installed."""
    try:
        result = subprocess.run(
            ["wsl", "--list", "--quiet"],
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0:
            return False
        # wsl --list --quiet outputs UTF-16LE with embedded NULs when not captured
        # as text; python already decodes with text=True but stray NULs can appear.
        for line in result.stdout.splitlines():
            if line.replace("\x00", "").strip() == WSL_DISTRO_NAME:
                return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def is_docker_running() -> bool:
    """Check if the Docker engine is reachable inside the EduBotics distro."""
    try:
        result = subprocess.run(
            _docker_cmd("info"),
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def start_edubotics_distro() -> bool:
    """Wake the EduBotics distro so systemd starts dockerd.

    A bare `wsl -d EduBotics echo ready` triggers the WSL2 VM to boot the
    distro if it's idle; from there, systemd (enabled in wsl.conf) brings
    docker.service up on its own.
    """
    if not is_distro_registered():
        return False
    try:
        result = subprocess.run(
            ["wsl", "-d", WSL_DISTRO_NAME, "--", "echo", "ready"],
            capture_output=True, text=True, timeout=20,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def wait_for_docker(timeout: int = DOCKER_STARTUP_TIMEOUT, callback=None) -> bool:
    """Wait for Docker engine to be reachable inside the EduBotics distro.

    Args:
        timeout: Max seconds to wait.
        callback: Optional function called with (elapsed, timeout) for progress updates.

    Returns:
        True if Docker became available within timeout.
    """
    start = time.time()
    nudged_service = False
    while time.time() - start < timeout:
        if is_docker_running():
            return True
        elapsed = int(time.time() - start)
        if callback:
            callback(elapsed, timeout)
        # If we're still waiting past 15s, invoke the dockerd-wrapper directly
        # (catches the rare case where WSL's [boot] command didn't fire).
        if elapsed >= 15 and not nudged_service:
            try:
                subprocess.run(
                    ["wsl", "-d", WSL_DISTRO_NAME, "--",
                     "/usr/local/bin/start-dockerd.sh"],
                    capture_output=True, text=True, timeout=10,
                    **_SUBPROCESS_KWARGS,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            nudged_service = True
        time.sleep(2)
    return False


def has_gpu() -> bool:
    """Detect if NVIDIA GPU is available on the Windows host.

    WSL2 exposes the host NVIDIA driver to the distro automatically via
    /usr/lib/wsl/drivers, so host visibility == distro visibility.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def images_exist() -> dict[str, bool]:
    """Check which Docker images are already pulled inside the distro."""
    status = {}
    for image in ALL_IMAGES:
        try:
            result = subprocess.run(
                _docker_cmd("image", "inspect", image),
                capture_output=True, text=True, timeout=10,
                **_SUBPROCESS_KWARGS,
            )
            status[image] = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            status[image] = False
    return status


def check_for_updates(log=None) -> bool:
    """Pull all images to ensure we have the latest version.

    Uses the same stall-detection + retry machinery as pull_images(), but is
    non-fatal: a failed update for one image just means the student continues
    to use the currently-cached version. Used on GUI startup.

    Returns True if any image was updated.
    """
    any_updated = False
    total = len(ALL_IMAGES)

    for i, image in enumerate(ALL_IMAGES):
        short = image.split("/")[-1]
        # Capture image ID before so we can detect whether pull actually changed it
        try:
            local_before = subprocess.run(
                _docker_cmd("images", "-q", image),
                capture_output=True, text=True, timeout=10,
                **_SUBPROCESS_KWARGS,
            )
            old_id = local_before.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            old_id = ""

        if log:
            log(f"  Prüfe {i+1}/{total}: {short}")

        # Shorter retries — this is an update check, not a first-run pull.
        # If the network is flaky, just move on and keep using the old image.
        ok = _pull_one_image(image, i, total, log=log, stall_timeout=120, max_retries=2)
        if not ok:
            if log:
                log(f"  Übersprungen: {short} (aktuelle Version wird weiter verwendet).")
            continue

        # Did the image ID change?
        try:
            local_after = subprocess.run(
                _docker_cmd("images", "-q", image),
                capture_output=True, text=True, timeout=10,
                **_SUBPROCESS_KWARGS,
            )
            new_id = local_after.stdout.strip()
            if old_id and new_id and old_id != new_id:
                if log:
                    log(f"  Aktualisiert: {short}")
                any_updated = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if any_updated:
        # Remove dangling images to free disk space
        try:
            subprocess.run(
                _docker_cmd("image", "prune", "-f"),
                capture_output=True, text=True, timeout=30,
                **_SUBPROCESS_KWARGS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return any_updated


def _reset_dockerd(log=None) -> bool:
    """Forcefully restart dockerd inside the distro. Recovers from deadlocks
    caused by interrupted pulls or other unhealthy states.

    Returns True if dockerd is reachable after the reset.
    """
    if log:
        log("    dockerd wird neu gestartet...")
    script = (
        "pkill -KILL -f 'docker pull' 2>/dev/null; "
        "pkill -TERM dockerd 2>/dev/null; sleep 2; "
        "pkill -KILL dockerd 2>/dev/null; "
        "pkill -KILL containerd 2>/dev/null; sleep 1; "
        "rm -f /var/run/docker.sock /var/run/docker.pid 2>/dev/null; "
        "/usr/local/bin/start-dockerd.sh; sleep 4"
    )
    try:
        subprocess.run(
            ["wsl", "-d", WSL_DISTRO_NAME, "--", "bash", "-c", script],
            capture_output=True, text=True, timeout=30,
            **_SUBPROCESS_KWARGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    # Poll for readiness
    for _ in range(15):
        if is_docker_running():
            return True
        time.sleep(1)
    return False


def _get_docker_disk_usage() -> int:
    """Return bytes used by /var/lib/docker/overlay2 inside the distro.

    Used as a secondary progress signal during layer extraction — Docker's
    stdout is silent during extract (the progress bar uses \\r), but the
    overlay2 directory is growing rapidly. If disk is growing, the pull is
    alive even if no newlines are flowing.
    """
    try:
        result = subprocess.run(
            ["wsl", "-d", WSL_DISTRO_NAME, "-u", "root", "--",
             "du", "-sb", "/var/lib/docker/overlay2"],
            capture_output=True, text=True, timeout=15,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return 0


def _pull_one_image(
    image: str,
    idx: int,
    total: int,
    log=None,
    stall_timeout: int = 600,
    max_retries: int = 4,
) -> bool:
    """Pull a single image with stall detection and retry.

    Docker Hub's CDN occasionally stalls mid-blob on large layers; the HTTP
    client has no aggressive idle-read timeout, so pulls hang indefinitely.
    Additionally, `docker pull` is SILENT during the extract phase of large
    layers — the progress bar uses \\r instead of \\n, so our line-based
    reader sees nothing for minutes. A 3-GB PyTorch layer can take 5-10
    minutes to extract on average hardware.

    Watchdog strategy (either signal resets the stall timer):
      1. New stdout line (download progress or layer completion)
      2. /var/lib/docker/overlay2 grew by >= 10 MB since last check
         (covers the silent extract phase)

    Only when BOTH are silent for `stall_timeout` seconds is the pull
    considered truly stalled and killed for retry.
    """
    import queue
    import threading

    short = image.split("/")[-1]
    poll_interval = 20  # seconds — how often we check disk growth
    disk_delta_threshold = 10 * 1024 * 1024  # 10 MB

    for attempt in range(1, max_retries + 1):
        if log:
            suffix = f" (Versuch {attempt}/{max_retries})" if attempt > 1 else ""
            log(f"  [{idx+1}/{total}] Lade {short}{suffix}...")

        try:
            proc = subprocess.Popen(
                _docker_cmd("pull", image),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                **_SUBPROCESS_KWARGS,
            )
        except (FileNotFoundError, OSError) as exc:
            if log:
                log(f"    Fehler: {exc}")
            return False

        # Reader thread puts each line on a queue; main loop polls with short
        # timeout and falls back to disk-growth detection.
        line_q: "queue.Queue[str]" = queue.Queue()

        def _reader():
            try:
                for line in proc.stdout:
                    line_q.put(line)
            finally:
                line_q.put(None)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        last_line = ""
        last_progress = time.time()
        last_disk = _get_docker_disk_usage()
        stalled = False
        eof = False

        while not eof:
            # Poll the line queue with a SHORT timeout so we can intermix
            # disk-growth checks. Any kind of progress resets last_progress.
            try:
                line = line_q.get(timeout=poll_interval)
                if line is None:
                    eof = True
                    break
                stripped = line.strip()
                if stripped and stripped != last_line:
                    if log:
                        log(f"    {stripped}")
                    last_line = stripped
                last_progress = time.time()
                continue
            except queue.Empty:
                pass

            # No stdout in poll_interval — check disk growth instead.
            cur_disk = _get_docker_disk_usage()
            if cur_disk > last_disk + disk_delta_threshold:
                # Extract is writing to disk — pull is alive
                last_disk = cur_disk
                last_progress = time.time()
                continue

            # Neither stdout nor disk moved — check against full timeout
            if time.time() - last_progress >= stall_timeout:
                stalled = True
                break

        if stalled:
            if log:
                log(
                    f"    Stillstand erkannt ({stall_timeout}s ohne Fortschritt) "
                    "— Pull wird abgebrochen."
                )
            try:
                proc.kill()
                proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                pass
            if attempt >= 2:
                _reset_dockerd(log=log)
            if attempt < max_retries:
                time.sleep(min(4 * (2 ** (attempt - 1)), 30))
            continue

        # Process finished naturally
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

        if proc.returncode == 0:
            return True

        # Non-zero exit — network error, rate limit, etc. Retry.
        if log:
            log(f"    Pull exit {proc.returncode}. Wiederhole...")
        if attempt >= 2:
            _reset_dockerd(log=log)
        if attempt < max_retries:
            time.sleep(min(4 * (2 ** (attempt - 1)), 30))

    if log:
        log(f"    FEHLER: {short} konnte nach {max_retries} Versuchen nicht geladen werden.")
    return False


def pull_images(callback=None, log=None) -> bool:
    """Pull all required Docker images with stall detection + retry.

    Resilient against:
      - Docker Hub CDN mid-blob stalls (watchdog + retry)
      - dockerd deadlock after an interrupted pull (auto-reset on retry)
      - Transient network failures (exponential backoff)

    Args:
        callback: Optional function called with (image_name, index, total).
        log: Optional callable for streaming pull output lines.

    Returns:
        True if ALL images pulled successfully.
    """
    total = len(ALL_IMAGES)
    for i, image in enumerate(ALL_IMAGES):
        if callback:
            callback(image, i, total)
        # Skip if already present (covers retries after partial success)
        if images_exist().get(image):
            if log:
                log(f"  [{i+1}/{total}] {image.split('/')[-1]}: bereits vorhanden, überspringen.")
            continue
        if not _pull_one_image(image, i, total, log=log):
            return False
    return True


def _compose_args(gpu: bool = False) -> list[str]:
    """Build the `compose` portion of a docker command.

    Uses --env-file to point to the user-writable .env in %LOCALAPPDATA% so
    the GUI doesn't need admin rights to regenerate it. Only passes the flag
    when the file exists, otherwise compose would error out with "env file
    not found" before we've even had a chance to create it.

    All paths are converted to their /mnt/<drive>/... WSL forms since docker
    inside the distro won't understand Windows-style paths.
    """
    args = ["compose"]
    if os.path.isfile(ENV_FILE):
        args.extend(["--env-file", _to_wsl_path(ENV_FILE)])
    args.extend(["-f", _to_wsl_path(COMPOSE_FILE)])
    if gpu:
        args.extend(["-f", _to_wsl_path(COMPOSE_GPU_FILE)])
    return args


def start_containers(gpu: bool = False, log=None) -> bool:
    """Start all containers via docker compose up -d.

    Uses --force-recreate to handle stale containers cleanly.
    """
    cmd = _docker_cmd(
        *_compose_args(gpu), "up", "-d", "--force-recreate",
        cwd_wsl=DOCKER_DIR_WSL,
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=180,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0 and log:
            log(f"Docker Compose Fehler: {result.stderr.strip()}")
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        if log:
            log(f"Docker Compose Fehler: {e}")
        return False


def start_cloud_only(log=None) -> bool:
    """Start ONLY the physical_ai_manager container (cloud-only mode).

    Used when no robot hardware is connected — students or teachers can still
    log in to the Cloud tab to manage cloud trainings without any USB devices.

    Uses --no-deps so docker-compose doesn't transitively pull in
    physical_ai_server via the depends_on relationship.
    """
    cmd = _docker_cmd(
        *_compose_args(gpu=False),
        "up", "-d", "--force-recreate", "--no-deps", "physical_ai_manager",
        cwd_wsl=DOCKER_DIR_WSL,
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=120,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0 and log:
            log(f"Docker Compose Fehler: {result.stderr.strip()}")
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        if log:
            log(f"Docker Compose Fehler: {e}")
        return False


def stop_cloud_only(log=None) -> bool:
    """Stop the physical_ai_manager container (cloud-only mode counterpart).

    Uses 'stop' + 'rm' instead of 'down' so it doesn't tear down the network
    or volumes that the full-stack mode might rely on.
    """
    try:
        for action in (
            ["stop", "physical_ai_manager"],
            ["rm", "-f", "physical_ai_manager"],
        ):
            subprocess.run(
                _docker_cmd(*_compose_args(gpu=False), *action, cwd_wsl=DOCKER_DIR_WSL),
                capture_output=True, text=True, timeout=60,
                **_SUBPROCESS_KWARGS,
            )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        if log:
            log(f"Docker Compose Fehler: {e}")
        return False


def manager_container_running() -> bool:
    """Return True iff only the physical_ai_manager container is up.

    Useful for resuming a cloud-only session after the GUI was closed.
    """
    try:
        result = subprocess.run(
            _docker_cmd("inspect", "-f", "{{.State.Status}}", "physical_ai_manager"),
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0 and result.stdout.strip() == "running"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def stop_containers(gpu: bool = False) -> bool:
    """Stop all containers via docker compose down."""
    cmd = _docker_cmd(
        *_compose_args(gpu), "down",
        cwd_wsl=DOCKER_DIR_WSL,
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_container_status() -> dict[str, str]:
    """Get status of all project containers.

    Returns dict of container_name -> status (e.g. "running", "exited", "not found").
    """
    containers = ["open_manipulator", "physical_ai_server", "physical_ai_manager"]
    status = {}
    for name in containers:
        try:
            result = subprocess.run(
                _docker_cmd("inspect", "-f", "{{.State.Status}}", name),
                capture_output=True, text=True, timeout=10,
                **_SUBPROCESS_KWARGS,
            )
            status[name] = result.stdout.strip() if result.returncode == 0 else "not found"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            status[name] = "error"
    return status


def all_containers_running() -> bool:
    """Check if all 3 containers are in 'running' state."""
    status = get_container_status()
    return all(s == "running" for s in status.values())


def get_container_logs(container_name: str, lines: int = 50) -> str:
    """Get recent logs from a container."""
    try:
        result = subprocess.run(
            _docker_cmd("logs", "--tail", str(lines), container_name),
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        return result.stdout + result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
