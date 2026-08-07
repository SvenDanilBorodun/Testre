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
import re
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
    DOCKER_DIR_WSL,
    DOCKER_STARTUP_TIMEOUT,
    ENV_FILE,
    IMAGE_FRESHNESS_WARN_DAYS,
    IMAGE_NAMES,
    IMAGE_OPEN_MANIPULATOR,
    IMAGE_PHYSICAL_AI_MANAGER,
    IMAGE_PHYSICAL_AI_SERVER,
    IMAGE_TAG,
    LAST_PULL_FILE,
    MANIFEST_INSPECT_TIMEOUT,
    NETWORK_PROBE_TIMEOUT,
    REGISTRY,
    REGISTRY_FALLBACK,
    SKIP_AUTO_PULL,
    WSL_DISTRO_NAME,
    _to_wsl_path,
)

# docker-compose service name → the pinned image it runs. Lets the env-start
# pull-skip (and any future per-service logic) reason about exactly the
# image(s) a given `docker compose pull <service>` would touch — cloud-only
# starts ONLY physical_ai_manager, so it must not be forced to pull because an
# unrelated image (open_manipulator) looks stale.
_SERVICE_IMAGE = {
    "open_manipulator": IMAGE_OPEN_MANIPULATOR,
    "physical_ai_server": IMAGE_PHYSICAL_AI_SERVER,
    "physical_ai_manager": IMAGE_PHYSICAL_AI_MANAGER,
}


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


def read_rootfs_version() -> Optional[str]:
    """Return the rootfs stamp baked into the EduBotics distro, or None.

    Reads ``/etc/edubotics-rootfs-version`` (a single UTF-8 line written by
    ``wsl_rootfs/Dockerfile``) from inside the distro. Returns the trimmed
    string, or None when the distro can't be queried or predates the marker
    (installers <= 2.6.0 shipped no stamp). None means "can't determine" — the
    caller must fail OPEN on it (a read hiccup must never block startup). Mirrors
    the .iss GetDistroRootfsVersion() so the GUI's runtime rootfs handshake
    matches the installer's import gate."""
    try:
        result = subprocess.run(
            ["wsl", "-d", WSL_DISTRO_NAME, "--",
             "cat", "/etc/edubotics-rootfs-version"],
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # wsl.exe can emit UTF-16 with stray NULs; strip them before comparing.
    version = (result.stdout or "").replace("\x00", "").strip()
    return version or None


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


def _registry_host(registry: str) -> str:
    """The DNS host to TCP-probe for a registry value.

    A value carrying a host segment (``ghcr.io/svendanilborodun``) probes that
    host; a bare owner (``nettername``, no dot/colon in the first segment) means
    Docker Hub, whose registry endpoint is ``registry-1.docker.io``.
    """
    first = registry.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first.split(":", 1)[0]
    return "registry-1.docker.io"


def _host_reachable(host: str, timeout: int = NETWORK_PROBE_TIMEOUT) -> bool:
    """Plain TCP probe to ``host:443`` — no auth, no pull, just confirms the
    network can route to the registry. Returns False on any DNS / connection /
    timeout error."""
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def is_registry_reachable(timeout: int = NETWORK_PROBE_TIMEOUT) -> bool:
    """Fast pre-check whether ANY image registry is reachable BEFORE we burn
    ~12 min on retry storms when offline. Probes the PRIMARY registry host
    (GHCR), then the FALLBACK host (Docker Hub) — only when NEITHER answers do
    we treat the classroom as offline.

    Used by check_for_updates() / _compose_pull() to short-circuit the per-image
    loop on a disconnected network.
    """
    if _host_reachable(_registry_host(REGISTRY), timeout):
        return True
    if REGISTRY_FALLBACK and _host_reachable(_registry_host(REGISTRY_FALLBACK), timeout):
        return True
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


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _parse_digest_candidates(text: str) -> set:
    """Extract every sha256 digest mentioned in ``docker buildx imagetools
    inspect`` output. The set contains the manifest-LIST digest (the
    "Digest:" header — what RepoDigests records for an image pulled by tag
    from a buildx push) AND every per-platform child manifest digest, so a
    local RepoDigest of either shape can match.
    """
    return set(_DIGEST_RE.findall(text or ""))


def _remote_digest_candidates_for_ref(
    image: str,
    timeout: int = MANIFEST_INSPECT_TIMEOUT,
) -> set:
    """Probe ONE registry ref for its digest candidate set — empty on any error.

    Primary probe: ``docker buildx imagetools inspect`` (one registry
    round-trip, no layer downloads; buildx ships in the EduBotics rootfs).
    Its output names BOTH the manifest-list digest and the per-platform
    child digests. This matters: the local RepoDigest of a buildx-pushed
    image is the LIST digest, while the legacy probe
    (`_get_remote_manifest_digest`, kept as fallback) returns the amd64
    CHILD digest — the old equality comparison therefore never matched,
    layer 2 of the 2.2.4 hardening silently never fired, and every GUI
    launch logged "Update verfügbar" + did a no-op pull (fixed 2026-06-05).

    ``buildx imagetools inspect`` handles anonymous reads of PUBLIC GHCR
    packages; we keep it (not plain ``docker manifest inspect``, which is
    flaky against some GHCR images).
    """
    try:
        result = subprocess.run(
            _docker_cmd("buildx", "imagetools", "inspect", image),
            capture_output=True, text=True, timeout=timeout,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode == 0:
            candidates = _parse_digest_candidates(result.stdout)
            if candidates:
                return candidates
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Fallback: the legacy single-digest probe (amd64 child manifest digest).
    digest = _get_remote_manifest_digest(image, timeout=timeout)
    return {digest} if digest else set()


def _get_remote_digest_candidates(
    image: str,
    timeout: int = MANIFEST_INSPECT_TIMEOUT,
) -> set:
    """Registry-side digest candidate set for ``image``, trying the PRIMARY ref
    (GHCR) first and the digest-identical Docker Hub twin second.

    Because the images are dual-pushed via ``docker buildx imagetools create``
    (a content-addressed copy), the manifest digest is byte-identical on both
    registries — so a local RepoDigest from EITHER registry matches this set,
    and the digest pre-check stays correct whichever registry served the pull.
    """
    candidates = _remote_digest_candidates_for_ref(image, timeout)
    if candidates:
        return candidates
    fb = _fallback_ref(image)
    if fb is not None:
        return _remote_digest_candidates_for_ref(fb, timeout)
    return set()


# ── Registry fallback: pull the Docker Hub twin when the primary (GHCR) fails ──


def _fallback_ref(image: str) -> Optional[str]:
    """Map a PRIMARY (GHCR) image ref to its Docker Hub fallback twin, or None
    if ``image`` isn't a primary-registry ref / no distinct fallback is set.

    Both refs share the bare ``<name>:<tag>``; we swap the ``REGISTRY`` prefix
    for ``REGISTRY_FALLBACK`` via an exact prefix match — NOT ``split('/')``,
    which mis-parses a two-segment registry like ``ghcr.io/<owner>``.
    """
    if not REGISTRY_FALLBACK or REGISTRY_FALLBACK == REGISTRY:
        return None
    prefix = REGISTRY + "/"
    if not image.startswith(prefix):
        return None
    return REGISTRY_FALLBACK + "/" + image[len(prefix):]


def _image_present_locally(image: str) -> bool:
    """True if ``image`` exists in the local Docker store.

    Used to detect whether a pull (primary or fallback-retag) actually landed
    the image before ``compose up`` reuses it — and to avoid re-pulling a
    present image.
    """
    try:
        result = subprocess.run(
            _docker_cmd("image", "inspect", image, "--format", "{{.Id}}"),
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _pull_fallback_and_retag(image: str, idx: int, total: int, log=None) -> bool:
    """Pull the Docker Hub twin of ``image`` and re-tag it to the primary name.

    The twin is dual-pushed (digest-identical), so re-tagging it to the primary
    ``${REGISTRY}`` ref lets compose find it with no `manifest unknown`, and the
    local RepoDigest still matches the remote candidate set. Returns True iff the
    primary-named image is present locally afterwards. Best-effort — never raises.
    """
    fb = _fallback_ref(image)
    if fb is None:
        return False
    if not _pull_one_image(fb, idx, total, log=log, stall_timeout=120, max_retries=2):
        return False
    try:
        result = subprocess.run(
            _docker_cmd("tag", fb, image),
            capture_output=True, text=True, timeout=15,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0:
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    # Drop the redundant fallback (nettername/*) tag — the layers stay, kept by
    # the primary tag, but leaving the extra tag clutters `docker image ls` (and
    # is the tell-tale fingerprint that a Hub fallback fired). Mirrors
    # pull_images.ps1's cleanup. Best-effort; a failure here doesn't undo the
    # successful retag above.
    try:
        subprocess.run(
            _docker_cmd("image", "rm", fb),
            capture_output=True, text=True, timeout=15,
            **_SUBPROCESS_KWARGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return True


def _pull_image_with_fallback(
    image: str,
    idx: int,
    total: int,
    log=None,
    stall_timeout: int = 120,
    max_retries: int = 3,
) -> bool:
    """Pull ``image`` from the PRIMARY registry (GHCR); only on a FAILED pull
    fall back to the digest-identical Docker Hub twin and re-tag it to the
    primary name. Returns True iff the primary-named image is present locally
    afterwards.

    The PRIMARY pull is attempted UNCONDITIONALLY — no host-reachability
    pre-gate. The old ``_host_reachable(ghcr.io)`` gate probed the WINDOWS host,
    but the pull runs inside WSL2 dockerd: a single host-side DNS / IPv6 / proxy
    blip would divert the WHOLE classroom onto Docker Hub's rate-limited
    endpoint even when WSL could reach GHCR fine. ``_pull_one_image`` already
    fails fast on a dead host (DNS) and retries transient GHCR/Fastly 5xx, so an
    unconditional attempt is both faster (no probe) and keeps students on the
    primary registry. Docker Hub is the fallback only when GHCR genuinely fails.
    """
    short = image.split("/")[-1]
    if _pull_one_image(
        image, idx, total, log=log,
        stall_timeout=stall_timeout, max_retries=max_retries,
    ):
        return True
    if _fallback_ref(image) is None:
        return False
    if log:
        log(
            f"  [{idx+1}/{total}] {short}: GHCR-Pull fehlgeschlagen "
            "— wechsle zu Docker Hub..."
        )
    return _pull_fallback_and_retag(image, idx, total, log=log)


def _image_is_current(image: str, remote_candidates: Optional[set] = None) -> bool:
    """Whether the locally-present ``image`` already matches the registry, so a
    pull would be a no-op. A normally-pulled image always carries a RepoDigest;
    a missing digest or a non-matching one means a pull is needed.
    """
    digest = _get_local_repo_digest(image)
    if digest is None:
        return False
    if remote_candidates is None:
        remote_candidates = _get_remote_digest_candidates(image)
    return digest in remote_candidates


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
    """Auto-pull on GUI start: keep student images in lockstep with the pinned
    tag on the PRIMARY registry (GHCR), with a Docker Hub fallback.

    Each image is pulled from GHCR; if GHCR is unreachable or the pull fails,
    the digest-identical Docker Hub twin is pulled and re-tagged to the primary
    name (``_pull_image_with_fallback``), so compose's single ``${REGISTRY}``
    reference always resolves.

    Hardened in 2.2.4 with three layers of defence so the existing-installs
    case (.exe rolled out months ago, F62-F66 just pushed) actually picks up
    the new bits next launch instead of silently keeping the cached version
    forever:

      1. **Offline short-circuit** (``is_registry_reachable``): a 5 s TCP
         probe to the primary (GHCR) host, then the fallback (Docker Hub) host.
         If NEITHER answers, we don't even try to pull — the existing per-image
         2-retry × 120 s stall storm would otherwise burn ~12 min before giving
         up on a classroom without internet. Logged in German so the operator
         sees we intentionally skipped, not silently failed.
      2. **Manifest-digest pre-check** (``_get_remote_digest_candidates`` vs
         ``_get_local_repo_digest``): for each image, fetch the registry's
         digest candidate set (manifest-list digest + per-platform child
         digests; one round-trip, no layers) and check the locally-cached
         RepoDigest for membership. Match → skip the pull entirely (logs
         "bereits aktuell"). No match / unknown → fall through to a real
         pull. Cuts the "all-images-up-to-date" path from ~30 s of
         docker-pull round-trips to ~3 s of HEAD requests.
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

    if not is_registry_reachable():
        if log:
            log(
                "  Registry (GHCR/Docker Hub) nicht erreichbar — vorhandene Images werden verwendet. "
                "Bitte Internetverbindung prüfen, falls aktuelle Versionen benötigt werden."
            )
        return False

    any_updated = False
    any_failed = False
    pulled_digests: dict[str, Optional[str]] = {}
    total = len(ALL_IMAGES)

    for i, image in enumerate(ALL_IMAGES):
        short = image.split("/")[-1]

        local_digest = _get_local_repo_digest(image)
        remote_candidates = _get_remote_digest_candidates(image)

        # Layer 2: digest pre-check. Saves the per-image docker-pull
        # round-trip when local already matches remote. Membership (not
        # equality against one digest): the local RepoDigest is the
        # manifest-LIST digest for buildx-pushed images, the old probe
        # returned the amd64 CHILD digest — a fixed pair never matched, so
        # this layer was silently dead (every launch: "Update verfügbar" +
        # no-op pull). The candidate set contains both shapes, from EITHER
        # registry (dual-push → identical digests), so the check is correct
        # whichever registry served the cached pull.
        if local_digest is not None and local_digest in remote_candidates:
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
                else "Update verfügbar" if remote_candidates
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

        ok = _pull_image_with_fallback(image, i, total, log=log, stall_timeout=120, max_retries=3)
        if not ok:
            if log:
                log(f"  Übersprungen: {short} (aktuelle Version wird weiter verwendet).")
            # Persist whatever we know (the local digest, if any) so the
            # next-run age check still works.
            pulled_digests[image] = local_digest
            any_failed = True
            continue

        # Pull succeeded — record the new digest for last-pull state.
        new_digest = _get_local_repo_digest(image) or (
            sorted(remote_candidates)[0] if remote_candidates else None
        )
        pulled_digests[image] = new_digest

        try:
            local_after = subprocess.run(
                _docker_cmd("images", "-q", image),
                capture_output=True, text=True, timeout=10,
                **_SUBPROCESS_KWARGS,
            )
            new_id = local_after.stdout.strip()
            # old_id == "" means the image wasn't present before this run (a
            # first-time pull) — that IS an update; the old `old_id and ...`
            # guard dropped it, so any_updated stayed False, the disk prune was
            # skipped, and the log said "aktuell" after a real pull.
            if new_id and old_id != new_id:
                if log:
                    log(f"  Aktualisiert: {short} → {(new_digest or '')[7:19]}")
                any_updated = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if any_updated and not any_failed:
        # Reclaim disk: first drop superseded TAGGED versions (prune -f is
        # dangling-only, so it can't remove e.g. a leftover :2.11.0 after
        # :2.13.0 lands), then dangling layers.
        #
        # ONLY when every image is current. Pruning after a PARTIAL upgrade
        # (some images pulled, one failed on both registries) would untag the
        # failed image's still-working previous version and then prune its
        # now-dangling layers — the student ends with NEITHER the new NOR the
        # old image, and the EDUBOTICS_IMAGE_TAG rollback is destroyed.
        # (`compose down` ran earlier in startup, so no container holds a
        # reference and docker's in-use guard does not protect here.) The
        # superseded tags are reclaimed on the next fully-successful run.
        prune_superseded_tags(log=log)
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


def prune_superseded_tags(log=None) -> int:
    """Remove local image tags that aren't the current ``IMAGE_TAG``.

    ``docker image prune -f`` is dangling-ONLY, so after an upgrade pulls
    ``:X.Y.Z`` the previously-installed ``:X.Y-1`` TAGGED images survive and the
    WSL VHDX grows a full image set per release (the v2.13.0 incident bloated it
    to ~53 GiB). ``pull_images.ps1`` prunes these at install time, but the GUI's
    own pull paths — the ones that run on a reboot-pending upgrade, when the
    installer's pull was skipped — never did. Replicate it across BOTH the
    primary and fallback registries × all short names. Untag-by-tag is
    layer-safe: only layers NOT shared with the kept ``:IMAGE_TAG`` are dropped.
    Returns the number of tags removed. Best-effort; never raises.

    SKIPPED ENTIRELY when ``EDUBOTICS_IMAGE_TAG`` is set. That variable is the
    documented per-rig rollback: a teacher pins a broken rig back to e.g.
    ``2.12.1``, launches the GUI, the auto-pull fetches 2.12.1 and reports a
    successful update — and the prune would then delete every ``:2.13.0`` tag on
    BOTH registries, so removing the override later forces a fresh ~6 GB pull
    (15-30 min on a school link). An explicit pin means the operator is managing
    tags deliberately; reclaiming disk is not worth destroying their escape
    hatch. (The caller ALSO gates on ``any_updated and not any_failed`` — that
    guards the partial-upgrade case, which is a different failure.)
    """
    # .strip(): a whitespace-only value is NOT a pin — `_resolve_setting` would
    # fall through to versions.env/default, so treating it as one here would
    # silently disable the prune forever on a rig with a stray-space env var.
    # Kept byte-for-byte in step with the pi_agent twin (pi_agent/docker_manager
    # .py::prune_superseded_tags), which is the same guard on the same variable.
    if os.environ.get("EDUBOTICS_IMAGE_TAG", "").strip():
        if log:
            log(
                "  Aufräumen übersprungen: EDUBOTICS_IMAGE_TAG ist gesetzt "
                "(andere Image-Versionen bleiben für den Rückweg erhalten)."
            )
        return 0
    repos = []
    for name in IMAGE_NAMES:
        repos.append(f"{REGISTRY}/{name}")
        if REGISTRY_FALLBACK and REGISTRY_FALLBACK != REGISTRY:
            repos.append(f"{REGISTRY_FALLBACK}/{name}")
    removed = 0
    for repo in repos:
        try:
            listed = subprocess.run(
                _docker_cmd("images", repo, "--format", "{{.Tag}}"),
                capture_output=True, text=True, timeout=15,
                **_SUBPROCESS_KWARGS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if listed.returncode != 0:
            continue
        for tag in listed.stdout.splitlines():
            tag = tag.strip()
            if not tag or tag == "<none>" or tag == IMAGE_TAG:
                continue
            try:
                rm = subprocess.run(
                    _docker_cmd("image", "rm", f"{repo}:{tag}"),
                    capture_output=True, text=True, timeout=20,
                    **_SUBPROCESS_KWARGS,
                )
                if rm.returncode == 0:
                    removed += 1
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    if removed and log:
        log(f"  {removed} veraltete Image-Version(en) entfernt (Speicher freigegeben).")
    return removed


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
    # Snapshot which images are already present ONCE (was re-queried per
    # iteration → 3 `docker image inspect` for 3 images). Newly-pulled images in
    # THIS run aren't in the snapshot, but they aren't skipped either — correct.
    present = images_exist()

    # Offline short-circuit, mirroring check_for_updates() / _compose_pull().
    # Without it, a first install with no internet burns the FULL retry ladder
    # per missing image: 3 primary attempts + 3 Hub attempts, with _pull_one_image
    # calling _reset_dockerd on every attempt >= 2 — 4 dockerd restarts per image,
    # x3 images, before reporting the failure that two 5 s TCP probes settle here.
    # Only short-circuits when something is actually MISSING: an all-present call
    # must still return True (it has nothing to pull), and the offline case must
    # still return False — the caller's German "Internetverbindung prüfen" is the
    # honest outcome, not a silent success over absent images.
    if any(not present.get(image) for image in ALL_IMAGES) and not is_registry_reachable():
        if log:
            log(
                "  Registry (GHCR/Docker Hub) nicht erreichbar — fehlende Images "
                "können nicht geladen werden. Bitte Internetverbindung prüfen."
            )
        return False

    for i, image in enumerate(ALL_IMAGES):
        if callback:
            callback(image, i, total)
        # Skip if already present (covers retries after partial success)
        if present.get(image):
            if log:
                log(f"  [{i+1}/{total}] {image.split('/')[-1]}: bereits vorhanden, überspringen.")
            continue
        # GHCR→Docker Hub fallback (same as check_for_updates / _compose_pull):
        # if the GHCR pull fails, pull the digest-identical Hub twin and re-tag it
        # to the primary name. Without this, a missing-image recovery while GHCR
        # is degraded would hard-fail even though the Hub fallback is available.
        if not _pull_image_with_fallback(image, i, total, log=log):
            return False
    # Reclaim disk from superseded tags left by a prior version. The installer's
    # pull_images.ps1 does this too, but on a reboot-pending upgrade the GUI is
    # the one that pulled (Steps 4+5 were skipped) — so prune here as well.
    prune_superseded_tags(log=log)
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


def _compose_pull(gpu: bool = False, service: Optional[str] = None, log=None) -> bool:
    """Refresh images for the pinned tag, BEFORE recreating.

    The tag reaches compose via the GUI-managed .env: constants.IMAGE_TAG
    (EDUBOTICS_IMAGE_TAG env > docker/versions.env > latest) is emitted as a
    MANAGED IMAGE_TAG line by config_generator, and _compose_args() hands
    that .env to compose via --env-file.

    Why this exists: `start_containers()` recreates with --force-recreate, but
    compose's default pull_policy is `missing` — it reuses whatever local image
    the `…:${IMAGE_TAG}` reference already resolves to. So a --force-recreate
    against a stale local `:latest` recreates onto stale bytes. The GUI's only
    other refresh is check_for_updates() at *launch*, which is skipped when the
    PC was offline at launch (and only reconnected before the student clicked
    "Umgebung starten"), or when a freshly-installed .exe bumped versions.env
    after launch. Pulling here closes that launch→start gap: the recreate one
    line below always lands on the image the pinned tag currently points at.

    Best-effort by design — `--ignore-pull-failures` lets an offline classroom
    still start on cached images instead of refusing to boot. A pinned
    immutable tag already present locally makes this a fast manifest no-op.

    Pre-check: if the relevant pinned image(s) are already current (RepoDigest
    matches the registry's manifest digest), skip the compose pull and let
    `up --force-recreate` reuse the present images (compose's default pull_policy
    `missing` never re-pulls a present image). Service-scoped so cloud-only
    (physical_ai_manager only) isn't forced to pull because an unrelated image
    looks stale. EDUBOTICS_SKIP_AUTO_PULL additionally forces the skip; `up`
    still self-heals a genuinely-missing image.

    REGISTRY FALLBACK: `docker compose pull` references the PRIMARY ${REGISTRY}
    (GHCR). If GHCR is degraded and a relevant image is still missing afterwards,
    we pull the digest-identical Docker Hub twin and re-tag it to the primary
    name so `up --force-recreate` reuses it instead of failing on
    `manifest unknown`.
    """
    relevant = (
        [_SERVICE_IMAGE[service]]
        if service and service in _SERVICE_IMAGE
        else list(ALL_IMAGES)
    )
    if relevant:
        if SKIP_AUTO_PULL:
            if log:
                log("  Auto-Pull deaktiviert — vorhandene Images werden verwendet.")
            return True
        # Offline short-circuit (mirrors check_for_updates): can't pull anyway,
        # and probing each image's remote digest would burn ~10 s/image of
        # manifest timeouts. Reuse the present images; `up` self-heals a
        # genuinely missing one (and fails clearly if offline + missing — same
        # as today's --ignore-pull-failures path).
        if not is_registry_reachable():
            if log:
                log("  Registry (GHCR/Docker Hub) nicht erreichbar — vorhandene Images werden verwendet.")
            return True
        if all(_image_is_current(img) for img in relevant):
            if log:
                log("  Images bereits aktuell — Aktualisierung übersprungen.")
            return True

    args = list(_compose_args(gpu))
    args.append("pull")
    args.append("--ignore-pull-failures")
    if service:
        args.append(service)
    cmd = _docker_cmd(*args, cwd_wsl=DOCKER_DIR_WSL)
    ok = False
    try:
        result = subprocess.run(
            cmd,
            # 1800 s (not 600): the CUDA-flattened amd64 physical-ai-server is one
            # ~5-6 GB layer, so on a slow classroom link a shorter wall-clock can
            # kill the pull mid-layer with no resumable progress. This only runs
            # when an image is genuinely stale AND online (the short-circuits above
            # cover the common cases); a timeout stays best-effort — `up` self-heals.
            capture_output=True, text=True, timeout=1800,
            **_SUBPROCESS_KWARGS,
        )
        ok = result.returncode == 0
        if not ok and log:
            detail = (result.stderr or "").strip().splitlines()
            tail = detail[-1][:140] if detail else ""
            log(
                "Hinweis: Image-Aktualisierung übersprungen — vorhandene Images "
                f"werden verwendet.{(' (' + tail + ')') if tail else ''}"
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        if log:
            log(
                "Hinweis: Image-Aktualisierung übersprungen — vorhandene Images "
                f"werden verwendet. ({e})"
            )

    # GHCR fallback: a relevant image still missing after the compose pull means
    # GHCR is degraded/unauthorized. If Docker Hub is reachable, pull the
    # digest-identical twin and re-tag it to the primary name so the recreate
    # below reuses it. Best-effort — never blocks the boot.
    missing = [img for img in relevant if not _image_present_locally(img)]
    if missing and REGISTRY_FALLBACK and _host_reachable(_registry_host(REGISTRY_FALLBACK)):
        if log:
            log("  GHCR unvollständig — fehlende Images werden von Docker Hub geladen...")
        landed = 0
        for i, img in enumerate(missing):
            if _pull_fallback_and_retag(img, i, len(missing), log=log):
                landed += 1
        if landed == len(missing):
            ok = True

    return ok


def start_containers(gpu: bool = False, log=None) -> bool:
    """Start all containers via docker compose up -d.

    Pulls the pinned image tag first (best-effort) so --force-recreate never
    lands on a stale local image, then uses --force-recreate to handle stale
    containers cleanly.
    """
    _compose_pull(gpu=gpu, log=log)
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
    _compose_pull(gpu=False, service="physical_ai_manager", log=log)
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


def restart_open_manipulator(gpu: bool = False, log=None) -> bool:
    """Recreate ONLY the open_manipulator (arm) container, in place.

    The Roboter-Studio leader toggle regenerates the .env (with or without
    EDUBOTICS_FOLLOWER_ONLY=1) and then calls this to relaunch the arm stack in
    the new mode. ``--no-deps`` leaves physical_ai_server (rosbridge + the React
    app's connection + the camera ingest) running, so the student's session
    stays connected — only the arm topics blip for ~15-20 s while the arm
    re-homes. Compose reads the freshly-written .env via --env-file, so the new
    FOLLOWER_ONLY value takes effect on this recreate. No image pull (the image
    is already local; we are only recreating with a new env)."""
    cmd = _docker_cmd(
        *_compose_args(gpu),
        "up", "-d", "--force-recreate", "--no-deps", "open_manipulator",
        cwd_wsl=DOCKER_DIR_WSL,
    )
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
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


def ensure_environment_stopped(log=None) -> bool:
    """Tear down any EduBotics containers left over from a previous session.

    The GUI is the SOLE owner of the container lifecycle: the robot stack
    must run only after the student explicitly clicks "Umgebung starten".
    Two paths can leave it running prematurely:
      1. dockerd resurrects containers on distro boot when they were created
         with a `restart` policy other than "no" (pre-2.5.3 images shipped
         `unless-stopped`), and the distro boots the moment the GUI first
         touches WSL;
      2. a previous GUI session simply left the stack up.
    Either case means the arm could be driven — and the Dynamixel serial bus
    held — before the student has scanned hardware, which ALSO makes the arm
    scan fail (identify_arm.py opens the same /dev/serial port the live
    controller is hammering at 100 Hz, so its pings collide and neither arm
    is identified).

    A graceful `compose down` (SIGTERM) lets open_manipulator's entrypoint
    disable servo torque before exit, and removing the containers means the
    next `up --force-recreate` recreates them with the current `restart:
    "no"` policy (self-healing for stacks created by an older image). The
    gpu override file is not needed for `down`. Idempotent: a fast no-op
    (exit 0) when nothing is present.

    Returns True iff at least one project container was present and a
    teardown was issued.
    """
    status = get_container_status()
    present = [name for name, s in status.items() if s not in ("not found", "error")]
    if not present:
        return False
    if log:
        log(f"Vorherige Container gefunden ({', '.join(present)}) — werden gestoppt...")
    stop_containers(gpu=False)
    return True


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


# The three persistent data volumes compose declares (docker-compose.yml).
# Compose prefixes them with the project name at create time (e.g.
# robotis_ai_setup_huggingface_cache), so factory_reset matches by suffix
# against `docker volume ls` instead of hardcoding the prefix.
EDUBOTICS_DATA_VOLUME_SUFFIXES = (
    "ai_workspace",
    "huggingface_cache",
    "edubotics_calib",
)


def factory_reset(log=None) -> tuple[bool, str]:
    """Delete the persistent EduBotics data volumes (Factory Reset).

    Wipes datasets, downloaded models/HF cache and the Roboter-Studio
    calibration — the exact set the v2.5.0 upgrade notes tell students to
    clear. The DISTRO survives (``wsl --unregister`` is the uninstaller's
    job, gated by its own consent dialog); the next "Umgebung starten"
    recreates the volumes empty.

    Order matters: ``compose down`` first (idempotent fast no-op when
    nothing runs) so the entrypoint torque-disables on SIGTERM and the
    containers release their volume references — ``docker volume rm``
    refuses volumes that are still referenced.

    Returns (ok, german_message). CLAUDE.md has referenced this button
    since v2.5.0; it ships since 2026-06-07 (leLab-comparison PR-1).
    """
    if log:
        log("Daten zurücksetzen: stoppe laufende Container ...")
    stop_containers(gpu=False)
    try:
        result = subprocess.run(
            _docker_cmd("volume", "ls", "--format", "{{.Name}}"),
            capture_output=True, text=True, timeout=30,
            **_SUBPROCESS_KWARGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"Docker ist nicht erreichbar: {e}"
    if result.returncode != 0:
        return False, (
            "Die Volume-Liste konnte nicht gelesen werden: "
            + result.stderr.strip()
        )
    targets = [
        name for name in result.stdout.split()
        if any(
            name == suffix or name.endswith(f"_{suffix}")
            for suffix in EDUBOTICS_DATA_VOLUME_SUFFIXES
        )
    ]
    if not targets:
        return True, "Keine EduBotics-Daten-Volumes vorhanden — nichts zu löschen."
    if log:
        log(f"Daten zurücksetzen: lösche {', '.join(sorted(targets))} ...")
    rm = subprocess.run(
        _docker_cmd("volume", "rm", *targets),
        capture_output=True, text=True, timeout=60,
        **_SUBPROCESS_KWARGS,
    )
    if rm.returncode != 0:
        return False, (
            "Die Daten-Volumes konnten nicht gelöscht werden: "
            + rm.stderr.strip()
        )
    return True, (
        f"{len(targets)} Daten-Volume(s) gelöscht: {', '.join(sorted(targets))}"
    )


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


# The three project containers, in one place so the probe below and
# get_container_status cannot disagree about what "the stack" is.
PROJECT_CONTAINERS = ("open_manipulator", "physical_ai_server", "physical_ai_manager")


def any_container_running() -> bool:
    """True if ANY project container is up — the „is a stack live?" question.

    `all_containers_running()` answers a different one and is the wrong tool
    for a shutdown decision: a PARTIAL stack is exactly the state that leaks.
    `gui_app.self.running` is not an answer either — `_do_start`'s `finally`
    clears it on any failure occurring AFTER `start_containers` succeeded, so
    the flag reads False over a live stack.

    ONE `docker ps` rather than get_container_status's three `docker inspect`
    calls, deliberately: this runs on the Tk main thread inside `_on_close`,
    and three 10 s timeouts against a wedged dockerd would freeze the close for
    half a minute. Names are re-checked against PROJECT_CONTAINERS because
    docker's `--filter name=` is a SUBSTRING match.

    Fails CLOSED for the caller's purposes — anything unexpected returns False,
    so a broken probe can never turn closing the GUI into a modal the student
    cannot get past. A leaked container is recoverable (the next launch's
    `ensure_environment_stopped` tears it down); an unclosable window is not.
    """
    try:
        result = subprocess.run(
            _docker_cmd(
                "ps", "--filter", "status=running", "--format", "{{.Names}}"),
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0:
            return False
        names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        return bool(names & set(PROJECT_CONTAINERS))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


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
