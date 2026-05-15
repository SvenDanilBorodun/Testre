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

import atexit
import json
import os
import socket
import subprocess
import sys
import time
from typing import Optional

# On Windows, hide console windows spawned by subprocess
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# Long-lived `wsl.exe` child that keeps the EduBotics distro from idling out.
# WSL2's default vmIdleTimeout (~60s) shuts the distro down once no wsl.exe
# client is attached, taking dockerd and the manager container with it — at
# which point http://localhost:80/ returns "connection refused" inside the
# embedded WebView. Holding one `wsl -d EduBotics -- sleep ...` open as long
# as the GUI is alive keeps the distro warm.
_keepalive_proc: Optional[subprocess.Popen] = None

from .constants import (
    ALL_IMAGES,
    COMPOSE_FILE,
    COMPOSE_GPU_FILE,
    DOCKER_DIR,
    DOCKER_DIR_WSL,
    DOCKER_STARTUP_TIMEOUT,
    ENV_FILE,
    IMAGE_FRESHNESS_WARN_DAYS,
    LAST_PULL_FILE,
    MANIFEST_INSPECT_TIMEOUT,
    NETWORK_PROBE_TIMEOUT,
    SKIP_AUTO_PULL,
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


def start_keepalive() -> bool:
    """Spawn a long-lived `wsl.exe` child to pin the EduBotics distro awake.

    Idempotent: if a live keep-alive process already exists, this is a no-op.
    Returns True if a keep-alive is running afterwards (newly spawned or
    pre-existing), False if spawning failed.
    """
    global _keepalive_proc
    if _keepalive_proc is not None and _keepalive_proc.poll() is None:
        return True
    if not is_distro_registered():
        return False
    # POSIX `sleep infinity` works on the bundled Ubuntu rootfs (GNU coreutils),
    # but a `while sleep 3600` loop is portable across BusyBox too. Either
    # holds the WSL plan9 pipe open, which is what defeats vmIdleTimeout.
    try:
        _keepalive_proc = subprocess.Popen(
            ["wsl", "-d", WSL_DISTRO_NAME, "--",
             "sh", "-c", "while sleep 3600; do :; done"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_SUBPROCESS_KWARGS,
        )
    except (FileNotFoundError, OSError):
        _keepalive_proc = None
        return False
    return True


def stop_keepalive() -> None:
    """Terminate the keep-alive child if running. Safe to call repeatedly."""
    global _keepalive_proc
    proc = _keepalive_proc
    _keepalive_proc = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


# Backstop: if the GUI process exits without explicitly calling stop_keepalive
# (crash, SIGTERM, etc.), reap the child so we don't leak `wsl.exe` workers.
atexit.register(stop_keepalive)


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


# ── Auto-pull on GUI start: helpers ───────────────────────────────────────


def is_dockerhub_reachable(timeout: int = NETWORK_PROBE_TIMEOUT) -> bool:
    """Fast pre-check whether Docker Hub is reachable BEFORE we burn ~12 min
    on retry storms when offline. Plain TCP probe to `registry-1.docker.io:443`
    — doesn't authenticate, doesn't pull, just confirms the network can route
    to the registry.

    Used by check_for_updates() to short-circuit the per-image loop on a
    disconnected classroom network. Returns False on any DNS / connection /
    timeout error.
    """
    try:
        with socket.create_connection(
            ("registry-1.docker.io", 443),
            timeout=timeout,
        ):
            return True
    except (OSError, socket.timeout):
        return False


def _get_local_repo_digest(image: str) -> Optional[str]:
    """Return the locally-cached image's RepoDigest (the registry-side
    content digest), or None if the image isn't present locally or has no
    digest attached (e.g. images built locally and never pulled).

    Format example: ``sha256:1171c7e0063a54dd7c547b8b245755d44e496234060e8056765220359cb88023``.
    We only return the bare digest, not the ``image@sha256:...`` form.
    """
    try:
        result = subprocess.run(
            _docker_cmd(
                "image", "inspect", image,
                "--format", "{{range .RepoDigests}}{{.}}|{{end}}",
            ),
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0:
            return None
        # RepoDigests format: ``nettername/foo@sha256:abc...|other/foo@sha256:def...|``
        for entry in result.stdout.strip().rstrip("|").split("|"):
            if "@sha256:" in entry:
                return entry.split("@", 1)[1]
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _get_remote_manifest_digest(
    image: str,
    timeout: int = MANIFEST_INSPECT_TIMEOUT,
) -> Optional[str]:
    """Return the registry-side platform manifest digest for the linux/amd64
    variant of ``image``, or None on any error.

    ``docker manifest inspect`` makes a single HEAD/GET to the registry's
    manifest endpoint — no layer download, fast even on slow links. The output
    is the manifest LIST (for multi-platform images), and we pick the digest
    of the linux/amd64 manifest because that's what students actually pull.

    Compared against ``_get_local_repo_digest`` to decide whether a real pull
    is necessary. Mismatch (or "no local digest") → pull. Match → skip.
    """
    try:
        # `docker manifest inspect` is an experimental-flag-free command in
        # Docker 20.10+ and works against an unauthenticated daemon for
        # public images. We still wrap it through the EduBotics distro so
        # the network path goes through the same dockerd the pull would use.
        result = subprocess.run(
            _docker_cmd("manifest", "inspect", image),
            capture_output=True, text=True, timeout=timeout,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None

    # Two output shapes are possible:
    # 1. Multi-platform manifest list (OCI index or Docker manifest list)
    #    → pick the linux/amd64 entry's digest.
    # 2. Single-platform manifest (rare for our nettername/* images, but
    #    `docker manifest inspect` returns it unwrapped for some registries)
    #    → fall back to its own config.digest if present.
    manifests = data.get("manifests")
    if isinstance(manifests, list):
        for entry in manifests:
            platform = entry.get("platform", {})
            if (
                platform.get("architecture") == "amd64"
                and platform.get("os") == "linux"
            ):
                digest = entry.get("digest")
                if isinstance(digest, str) and digest.startswith("sha256:"):
                    return digest
        return None
    # Single-platform manifest — return its top-level digest if present.
    digest = data.get("digest")
    if isinstance(digest, str) and digest.startswith("sha256:"):
        return digest
    return None


def _load_last_pull_info() -> Optional[dict]:
    """Read the persisted last-pull state. Returns ``None`` if no file exists
    or the file is unreadable (treat as 'never pulled')."""
    try:
        with open(LAST_PULL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "timestamp" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def _save_last_pull_info(per_image_digests: dict[str, Optional[str]]) -> None:
    """Persist the current pull's per-image digests + timestamp. Best-effort —
    if the directory can't be created (locked-down student machine), we just
    skip silently rather than failing the whole startup.
    """
    payload = {
        "timestamp": int(time.time()),
        "digests": {
            img: digest
            for img, digest in per_image_digests.items()
            if digest is not None
        },
    }
    try:
        os.makedirs(os.path.dirname(LAST_PULL_FILE), exist_ok=True)
        with open(LAST_PULL_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError:
        pass


def get_last_pull_status() -> dict:
    """Return a summary the GUI can show: age + per-image digests.

    Shape:
        {
          "age_days": float | None,    # None = never pulled
          "is_stale": bool,            # True if older than IMAGE_FRESHNESS_WARN_DAYS
          "digests": {<image>: <short_digest>},
          "timestamp": <unix> | None,
        }
    """
    info = _load_last_pull_info()
    if not info:
        return {
            "age_days": None,
            "is_stale": True,
            "digests": {},
            "timestamp": None,
        }
    ts = info.get("timestamp")
    age_seconds = max(0, time.time() - ts) if isinstance(ts, (int, float)) else None
    age_days = (age_seconds / 86400.0) if age_seconds is not None else None
    is_stale = age_days is None or age_days > IMAGE_FRESHNESS_WARN_DAYS
    return {
        "age_days": age_days,
        "is_stale": is_stale,
        "digests": info.get("digests", {}),
        "timestamp": ts,
    }


def check_for_updates(log=None) -> bool:
    """Auto-pull on GUI start: keep student images in lockstep with
    nettername/<image>:latest on Docker Hub.

    Hardened in 2.2.4 with three layers of defence so the existing-installs
    case (.exe rolled out months ago, F62-F66 just pushed) actually picks up
    the new bits next launch instead of silently keeping the cached version
    forever:

      1. **Offline short-circuit** (``is_dockerhub_reachable``): a 5 s TCP
         probe to registry-1.docker.io:443. If unreachable, we don't even
         try to pull — the existing per-image 2-retry × 120 s stall storm
         would otherwise burn ~12 min before giving up on a classroom
         without internet. Logged in German so the operator sees we
         intentionally skipped, not silently failed.
      2. **Manifest-digest pre-check** (``_get_remote_manifest_digest`` vs
         ``_get_local_repo_digest``): for each image, fetch the registry's
         linux/amd64 manifest digest (single HEAD request, no layers) and
         compare to the locally-cached RepoDigest. Match → skip the pull
         entirely (logs "bereits aktuell"). Mismatch / unknown → fall
         through to a real pull. Cuts the "all-images-up-to-date" path
         from ~30 s of docker-pull round-trips to ~3 s of HEAD requests.
      3. **Last-pull persistence** (``_save_last_pull_info``): on a
         successful check (any combination of pulls + skips that ended
         without a hard failure), record the per-image digests and a
         timestamp to ``LAST_PULL_FILE``. The GUI surfaces this as
         "Letzter Image-Update: vor X Tagen" so teachers can spot
         classroom PCs that have been offline too long to refresh.

    Per-image failures remain non-fatal — students keep using the cached
    image rather than blocking GUI startup. Returns ``True`` iff at least
    one image's local digest changed during this run.

    Set ``EDUBOTICS_SKIP_AUTO_PULL=1`` to disable entirely (offline
    classrooms that explicitly manage their own image cadence).
    """
    if SKIP_AUTO_PULL:
        if log:
            log("  Auto-Pull deaktiviert (EDUBOTICS_SKIP_AUTO_PULL=1).")
        return False

    if not is_dockerhub_reachable():
        if log:
            log(
                "  Docker Hub nicht erreichbar — vorhandene Images werden verwendet. "
                "Bitte Internetverbindung prüfen, falls aktuelle Versionen benötigt werden."
            )
        return False

    any_updated = False
    pulled_digests: dict[str, Optional[str]] = {}
    total = len(ALL_IMAGES)

    for i, image in enumerate(ALL_IMAGES):
        short = image.split("/")[-1]

        local_digest = _get_local_repo_digest(image)
        remote_digest = _get_remote_manifest_digest(image)

        # Layer 2: digest pre-check. Saves the per-image docker-pull
        # round-trip when local already matches remote.
        if (
            local_digest is not None
            and remote_digest is not None
            and local_digest == remote_digest
        ):
            if log:
                log(f"  [{i+1}/{total}] {short}: bereits aktuell ({local_digest[7:19]}).")
            pulled_digests[image] = local_digest
            continue

        # Need a real pull. Either remote differs, manifest probe failed
        # (treat as 'unknown — try to pull'), or the image isn't cached
        # locally with a RepoDigest yet (built locally and never pulled).
        if log:
            reason = (
                "lokal nicht vorhanden" if local_digest is None
                else "Update verfügbar" if remote_digest is not None
                else "Manifest-Probe fehlgeschlagen"
            )
            log(f"  [{i+1}/{total}] {short}: {reason}, ziehe...")

        # Capture old image ID so we can detect whether the pull actually
        # changed bytes — the manifest pre-check above means we should only
        # reach here when a real update OR a missing image is present, but
        # the bytes-changed check belt-and-suspenders against a flaky probe.
        try:
            local_before = subprocess.run(
                _docker_cmd("images", "-q", image),
                capture_output=True, text=True, timeout=10,
                **_SUBPROCESS_KWARGS,
            )
            old_id = local_before.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            old_id = ""

        ok = _pull_one_image(image, i, total, log=log, stall_timeout=120, max_retries=2)
        if not ok:
            if log:
                log(f"  Übersprungen: {short} (aktuelle Version wird weiter verwendet).")
            # Persist whatever we know (the local digest, if any) so the
            # next-run age check still works.
            pulled_digests[image] = local_digest
            continue

        # Pull succeeded — record the new digest for last-pull state.
        new_digest = _get_local_repo_digest(image) or remote_digest
        pulled_digests[image] = new_digest

        try:
            local_after = subprocess.run(
                _docker_cmd("images", "-q", image),
                capture_output=True, text=True, timeout=10,
                **_SUBPROCESS_KWARGS,
            )
            new_id = local_after.stdout.strip()
            if old_id and new_id and old_id != new_id:
                if log:
                    log(f"  Aktualisiert: {short} → {(new_digest or '')[7:19]}")
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

    # Layer 3: persist state for the freshness banner on the next GUI start.
    _save_last_pull_info(pulled_digests)

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
