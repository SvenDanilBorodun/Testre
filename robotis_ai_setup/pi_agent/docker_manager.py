"""Docker Compose lifecycle management for the EduBotics Pi-Agent (arm64).

Port of ``robotis_ai_setup/gui/app/docker_manager.py`` shed of every WSL2
bit: every ``docker`` call is NATIVE (no ``wsl -d EduBotics -- docker …``
wrapper), because the Pi runs Docker Engine directly. The image-digest
pre-check is flipped from ``linux/amd64`` to ``linux/arm64`` in BOTH the
legacy single-digest probe (``_get_remote_manifest_digest``) and the
set-membership machinery (``_parse_digest_candidates`` /
``_get_remote_digest_candidates``), reusing the arm64 twins the Jetson
agent already carries.

Two lifecycle laws distinguish this from the GUI:

  1. **Two tiers.** The ``physical_ai_manager`` is ALWAYS on (it serves the
     wizard + the ``/api/system`` proxy — it IS the GUI on the Pi). Only the
     robot tier (``open_manipulator`` + ``physical_ai_server``) is
     student-owned and comes up on „Umgebung starten".
  2. **NEVER ``compose down``.** ``down`` deletes the ``ros_net`` network,
     which severs the agent's gateway HTTP listener (the proxy target) and
     drops the always-on manager. Every teardown — stop, pre-scan cleanup,
     even factory reset — uses ``stop`` + ``rm -f`` on named services only.
     The graceful ``stop`` (SIGTERM) still lets ``open_manipulator``'s
     entrypoint run its torque-disable trap before exit (Rule §2 preserved).

The compose driver runs with a SCRUBBED environment (like the Jetson
agent) so the systemd unit's ``EDUBOTICS_AGENT_TOKEN`` / secrets never leak
into container processes; compose reads every ``${VAR}`` from ``--env-file``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from typing import Optional

from . import config_generator
from .constants import (
    ALL_IMAGES,
    COMPOSE_FILE,
    DOCKER_STARTUP_TIMEOUT,
    ENV_FILE,
    IMAGE_FRESHNESS_WARN_DAYS,
    IMAGE_OPEN_MANIPULATOR,
    IMAGE_PHYSICAL_AI_MANAGER,
    IMAGE_PHYSICAL_AI_SERVER,
    LAST_PULL_FILE,
    MANIFEST_INSPECT_TIMEOUT,
    NETWORK_PROBE_TIMEOUT,
    REGISTRY,
    REGISTRY_FALLBACK,
    SKIP_AUTO_PULL,
)

# Compose service / container names (the opi compose sets container_name to
# match, exactly like the student file). The manager is the always-on tier; the
# other two are the student-owned robot tier.
MANAGER_SERVICE = "physical_ai_manager"
_ROBOT_TIER = ("open_manipulator", "physical_ai_server")
_ALL_SERVICES = ("open_manipulator", "physical_ai_server", "physical_ai_manager")

# docker-compose service name → the pinned image it runs. Lets update logic
# reason about exactly which image(s) a service touches.
_SERVICE_IMAGE = {
    "open_manipulator": IMAGE_OPEN_MANIPULATOR,
    "physical_ai_server": IMAGE_PHYSICAL_AI_SERVER,
    "physical_ai_manager": IMAGE_PHYSICAL_AI_MANAGER,
}

# The three persistent data volumes the opi compose declares. Compose prefixes
# them with the project name at create time (e.g. edubotics_huggingface_cache),
# so factory_reset matches by suffix against `docker volume ls`.
EDUBOTICS_DATA_VOLUME_SUFFIXES = (
    "ai_workspace",
    "huggingface_cache",
    "edubotics_calib",
)

# Per-attempt pull timeout. Larger than the Jetson's 180 s default because the
# arm64 opi server image is one ~5-6 GB layer that a slow classroom link needs
# minutes to fetch; `docker pull` resumes from cached layers on retry so a
# timeout-then-retry is cheap. Override with EDUBOTICS_PULL_ATTEMPT_TIMEOUT_S.
_PULL_ATTEMPT_TIMEOUT_S = 600


class DockerError(Exception):
    """Raised when a Docker operation fails."""


# ── subprocess plumbing (native docker, scrubbed env) ────────────────────────


def _scrubbed_env() -> dict:
    """Process env for docker/compose subprocesses with secrets removed.

    SECURITY: the systemd unit's EnvironmentFile injects EDUBOTICS_AGENT_TOKEN
    (and possibly a Supabase JWT secret) into the agent's env. Without
    scrubbing, `docker compose up` would inherit them and a container could
    exfiltrate them. Compose reads every ${VAR} it needs from `--env-file`, so
    keep only the shell essentials + any DOCKER_* (non-default socket/context/
    auth config) passthrough.
    """
    keep = ("PATH", "HOME", "LANG", "LC_ALL")
    return {
        k: v for k, v in os.environ.items()
        if k in keep or k.startswith("DOCKER_")
    }


def _docker(*args: str) -> list[str]:
    """Build a native ``docker <args…>`` command (no WSL wrapper)."""
    return ["docker", *args]


def _compose(*args: str) -> list[str]:
    """Build ``docker compose -f <opi compose> [--env-file <.env>] <args…>``.

    The ``--env-file`` is added only when the file exists, mirroring the GUI —
    otherwise compose errors before we've had a chance to create it. Relative
    bind-mounts in the compose file resolve against the compose file's
    directory (the project dir), which setup.sh lays out under /opt/edubotics.
    """
    cmd = ["docker", "compose", "-f", COMPOSE_FILE]
    if os.path.isfile(ENV_FILE):
        cmd.extend(["--env-file", ENV_FILE])
    cmd.extend(args)
    return cmd


# ── Registry reachability + digest helpers (arm64) ───────────────────────────


def _registry_host(registry: str) -> str:
    """The DNS host to TCP-probe for a registry value. A host-bearing value
    (``ghcr.io/<owner>``) probes that host; a bare owner (``nettername``) means
    Docker Hub (``registry-1.docker.io``)."""
    first = registry.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first.split(":", 1)[0]
    return "registry-1.docker.io"


def _host_reachable(host: str, timeout: int = NETWORK_PROBE_TIMEOUT) -> bool:
    """Plain TCP probe to ``host:443``. False on any DNS/connection/timeout."""
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def is_registry_reachable(timeout: int = NETWORK_PROBE_TIMEOUT) -> bool:
    """True if EITHER the primary (GHCR) or fallback (Docker Hub) host answers.
    Used to short-circuit the pull loop when the classroom is offline."""
    if _host_reachable(_registry_host(REGISTRY), timeout):
        return True
    if REGISTRY_FALLBACK and _host_reachable(_registry_host(REGISTRY_FALLBACK), timeout):
        return True
    return False


def _fallback_ref(image: str) -> Optional[str]:
    """Map a PRIMARY (GHCR) image ref to its Docker Hub twin, or None if it
    isn't a primary-registry ref. Exact prefix swap (not split('/'), which
    mis-parses a two-segment ghcr.io/<owner> registry)."""
    if not REGISTRY_FALLBACK or REGISTRY_FALLBACK == REGISTRY:
        return None
    prefix = REGISTRY + "/"
    if not image.startswith(prefix):
        return None
    return REGISTRY_FALLBACK + "/" + image[len(prefix):]


def _get_local_repo_digest(image: str) -> Optional[str]:
    """Return the locally-cached image's RepoDigest (bare ``sha256:…``), or
    None if the image isn't present locally / has no digest attached."""
    try:
        result = subprocess.run(
            _docker("image", "inspect", image,
                    "--format", "{{range .RepoDigests}}{{.}}|{{end}}"),
            capture_output=True, text=True, timeout=10, env=_scrubbed_env(),
        )
        if result.returncode != 0:
            return None
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
    """Return the registry-side platform manifest digest for the **linux/arm64**
    variant of ``image``, or None on any error.

    Flipped from the GUI's linux/amd64: the Pi pulls the arm64 child manifest.
    This is the LEGACY single-digest probe, kept only as the fallback for
    ``_remote_digest_candidates_for_ref``.
    """
    try:
        result = subprocess.run(
            _docker("manifest", "inspect", image),
            capture_output=True, text=True, timeout=timeout, env=_scrubbed_env(),
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None

    manifests = data.get("manifests")
    if isinstance(manifests, list):
        for entry in manifests:
            platform = entry.get("platform", {})
            if (
                platform.get("architecture") == "arm64"
                and platform.get("os") == "linux"
            ):
                digest = entry.get("digest")
                if isinstance(digest, str) and digest.startswith("sha256:"):
                    return digest
        return None
    digest = data.get("digest")
    if isinstance(digest, str) and digest.startswith("sha256:"):
        return digest
    return None


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _parse_digest_candidates(text: str) -> set:
    """Extract every sha256 digest from ``docker buildx imagetools inspect``
    output. The set contains the manifest-LIST digest (what RepoDigests records
    for an image pulled by tag from a buildx push) AND every per-platform child
    digest, so a local RepoDigest of either shape can match. Arch-neutral (all
    digests are collected regardless of platform)."""
    return set(_DIGEST_RE.findall(text or ""))


def _remote_digest_candidates_for_ref(
    image: str,
    timeout: int = MANIFEST_INSPECT_TIMEOUT,
) -> set:
    """Probe ONE registry ref for its digest candidate set — empty on any error.

    Primary probe: ``docker buildx imagetools inspect`` (one registry
    round-trip, no layer downloads), which names BOTH the manifest-list digest
    and the per-platform child digests. This matters: a buildx-pushed image's
    local RepoDigest is the LIST digest, while the legacy probe returns the
    arm64 CHILD digest — a fixed pair never matched, so set membership is the
    only correct comparison. Falls back to the legacy single-digest probe.
    """
    try:
        result = subprocess.run(
            _docker("buildx", "imagetools", "inspect", image),
            capture_output=True, text=True, timeout=timeout, env=_scrubbed_env(),
        )
        if result.returncode == 0:
            candidates = _parse_digest_candidates(result.stdout)
            if candidates:
                return candidates
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
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
    registries — so a local RepoDigest from EITHER registry matches this set.
    """
    candidates = _remote_digest_candidates_for_ref(image, timeout)
    if candidates:
        return candidates
    fb = _fallback_ref(image)
    if fb is not None:
        return _remote_digest_candidates_for_ref(fb, timeout)
    return set()


def _image_is_current(image: str, remote_candidates: Optional[set] = None) -> bool:
    """Whether the locally-present ``image`` already matches the registry, so a
    pull would be a no-op. A missing local digest or a non-matching one → pull."""
    digest = _get_local_repo_digest(image)
    if digest is None:
        return False
    if remote_candidates is None:
        remote_candidates = _get_remote_digest_candidates(image)
    return digest in remote_candidates


def _image_present_locally(image: str) -> bool:
    """True if ``image`` exists in the local Docker store."""
    try:
        result = subprocess.run(
            _docker("image", "inspect", image, "--format", "{{.Id}}"),
            capture_output=True, text=True, timeout=10, env=_scrubbed_env(),
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ── Pull with GHCR→Docker Hub fallback ───────────────────────────────────────


def _pull_one_image(
    image: str,
    idx: int,
    total: int,
    log=None,
    max_attempts: int = 3,
) -> bool:
    """Pull a single image with per-attempt timeout + exponential backoff.

    Deviation from the GUI's disk-growth stall watchdog (which called
    ``_reset_dockerd`` on stall): on the Pi dockerd is a systemd service SHARED
    with the always-on manager, so restarting it would drop the wizard the
    student is looking at. Instead we rely on ``docker pull`` resuming from
    cached layers on retry (cheap) — the Jetson agent's proven native approach.
    """
    log = log or (lambda _m: None)
    short = image.split("/")[-1]
    timeout_s = int(os.environ.get("EDUBOTICS_PULL_ATTEMPT_TIMEOUT_S", _PULL_ATTEMPT_TIMEOUT_S))
    backoff = (0, 5, 15)
    for attempt in range(max_attempts):
        delay = backoff[attempt] if attempt < len(backoff) else backoff[-1]
        if delay > 0:
            time.sleep(delay)
        suffix = f" (Versuch {attempt + 1}/{max_attempts})" if attempt > 0 else ""
        log(f"  [{idx + 1}/{total}] Lade {short}{suffix} …")
        try:
            result = subprocess.run(
                _docker("pull", image),
                capture_output=True, text=True, timeout=timeout_s, env=_scrubbed_env(),
            )
            if result.returncode == 0:
                return True
            tail = (result.stderr or "").strip().splitlines()
            log(f"    Pull fehlgeschlagen: {tail[-1][:140] if tail else 'unbekannt'}")
        except subprocess.TimeoutExpired:
            log(f"    Zeitüberschreitung nach {timeout_s}s.")
        except (FileNotFoundError, OSError) as e:
            log(f"    Fehler: {e}")
            return False
    log(f"    FEHLER: {short} konnte nach {max_attempts} Versuchen nicht geladen werden.")
    return False


def _pull_fallback_and_retag(image: str, idx: int, total: int, log=None) -> bool:
    """Pull the Docker Hub twin of ``image`` and re-tag it to the primary name.

    The twin is dual-pushed (digest-identical), so re-tagging it to the primary
    ``${REGISTRY}`` ref lets compose find it with no ``manifest unknown``.
    Returns True iff the primary-named image is present afterwards."""
    fb = _fallback_ref(image)
    if fb is None:
        return False
    if not _pull_one_image(fb, idx, total, log=log, max_attempts=2):
        return False
    try:
        result = subprocess.run(
            _docker("tag", fb, image),
            capture_output=True, text=True, timeout=15, env=_scrubbed_env(),
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _pull_image_with_fallback(image: str, idx: int, total: int, log=None) -> bool:
    """Pull ``image`` from GHCR; on an unreachable host or a failed pull, fall
    back to the digest-identical Docker Hub twin and re-tag it to the primary
    name. Returns True iff the primary-named image is present afterwards."""
    log = log or (lambda _m: None)
    short = image.split("/")[-1]
    if _host_reachable(_registry_host(REGISTRY)):
        if _pull_one_image(image, idx, total, log=log):
            return True
    if _fallback_ref(image) is None:
        return False
    log(
        f"  [{idx + 1}/{total}] {short}: Primär-Registry (GHCR) nicht verfügbar "
        "— wechsle zu Docker Hub …"
    )
    return _pull_fallback_and_retag(image, idx, total, log=log)


# ── Last-pull persistence (freshness banner) ─────────────────────────────────


def _load_last_pull_info() -> Optional[dict]:
    """Read the persisted last-pull state, or None if absent/unreadable."""
    try:
        with open(LAST_PULL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "timestamp" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def _save_last_pull_info(per_image_digests: dict) -> None:
    """Persist the current pull's per-image digests + timestamp. Best-effort."""
    payload = {
        "timestamp": int(time.time()),
        "digests": {
            img: digest for img, digest in per_image_digests.items()
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
    """Return a summary the System window can show: age + per-image digests.

    Shape: {"age_days": float|None, "is_stale": bool, "digests": {...},
    "timestamp": unix|None}. age_days None == never pulled.
    """
    info = _load_last_pull_info()
    if not info:
        return {"age_days": None, "is_stale": True, "digests": {}, "timestamp": None}
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
    """Refresh the opi images to the pinned tag on GHCR (Docker Hub fallback).

    Same three-layer defence as the GUI/Jetson: offline short-circuit, arm64
    manifest-digest pre-check (set membership — skip a pull when the local
    RepoDigest is already in the registry's candidate set), and last-pull
    persistence for the freshness banner. Per-image failures are non-fatal.
    Returns True iff at least one image's local bytes changed.

    Set ``EDUBOTICS_SKIP_AUTO_PULL=1`` to disable entirely.
    """
    log = log or (lambda _m: None)
    if SKIP_AUTO_PULL:
        log("  Auto-Pull deaktiviert (EDUBOTICS_SKIP_AUTO_PULL=1).")
        return False

    if not is_registry_reachable():
        log(
            "  Registry (GHCR/Docker Hub) nicht erreichbar — vorhandene Images "
            "werden verwendet. Bitte Internetverbindung prüfen."
        )
        return False

    any_updated = False
    pulled_digests: dict = {}
    total = len(ALL_IMAGES)

    for i, image in enumerate(ALL_IMAGES):
        short = image.split("/")[-1]
        local_digest = _get_local_repo_digest(image)
        remote_candidates = _get_remote_digest_candidates(image)

        # Layer 2: digest pre-check (set membership, arm64 candidates).
        if local_digest is not None and local_digest in remote_candidates:
            log(f"  [{i + 1}/{total}] {short}: bereits aktuell ({local_digest[7:19]}).")
            pulled_digests[image] = local_digest
            continue

        reason = (
            "lokal nicht vorhanden" if local_digest is None
            else "Update verfügbar" if remote_candidates
            else "Manifest-Probe fehlgeschlagen"
        )
        log(f"  [{i + 1}/{total}] {short}: {reason}, ziehe …")

        try:
            before = subprocess.run(
                _docker("images", "-q", image),
                capture_output=True, text=True, timeout=10, env=_scrubbed_env(),
            )
            old_id = before.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            old_id = ""

        if not _pull_image_with_fallback(image, i, total, log=log):
            log(f"  Übersprungen: {short} (aktuelle Version wird weiter verwendet).")
            pulled_digests[image] = local_digest
            continue

        new_digest = _get_local_repo_digest(image) or (
            sorted(remote_candidates)[0] if remote_candidates else None
        )
        pulled_digests[image] = new_digest

        try:
            after = subprocess.run(
                _docker("images", "-q", image),
                capture_output=True, text=True, timeout=10, env=_scrubbed_env(),
            )
            new_id = after.stdout.strip()
            if old_id and new_id and old_id != new_id:
                log(f"  Aktualisiert: {short} → {(new_digest or '')[7:19]}")
                any_updated = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if any_updated:
        try:
            subprocess.run(
                _docker("image", "prune", "-f"),
                capture_output=True, text=True, timeout=30, env=_scrubbed_env(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    _save_last_pull_info(pulled_digests)
    return any_updated


def pull_images(callback=None, log=None) -> bool:
    """Pull all required opi images (GHCR→Hub fallback). Used at agent boot for
    a fresh Pi that has no images yet. Skips images already present. Returns
    True iff ALL images are present afterwards."""
    log = log or (lambda _m: None)
    total = len(ALL_IMAGES)
    for i, image in enumerate(ALL_IMAGES):
        if callback:
            callback(image, i, total)
        if _image_present_locally(image):
            log(f"  [{i + 1}/{total}] {image.split('/')[-1]}: bereits vorhanden, überspringen.")
            continue
        if not _pull_image_with_fallback(image, i, total, log=log):
            return False
    return True


# ── Daemon / container status ────────────────────────────────────────────────


def is_docker_running() -> bool:
    """Check whether the Docker engine is reachable."""
    try:
        result = subprocess.run(
            _docker("info"),
            capture_output=True, text=True, timeout=10, env=_scrubbed_env(),
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def images_exist() -> dict:
    """Check which opi images are already pulled locally."""
    status = {}
    for image in ALL_IMAGES:
        status[image] = _image_present_locally(image)
    return status


def get_container_status() -> dict:
    """Status of all project containers → {name: "running"|"exited"|"not found"}."""
    status = {}
    for name in _ALL_SERVICES:
        try:
            result = subprocess.run(
                _docker("inspect", "-f", "{{.State.Status}}", name),
                capture_output=True, text=True, timeout=10, env=_scrubbed_env(),
            )
            status[name] = result.stdout.strip() if result.returncode == 0 else "not found"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            status[name] = "error"
    return status


def manager_running() -> bool:
    """True iff the always-on physical_ai_manager is running."""
    return get_container_status().get(MANAGER_SERVICE) == "running"


def robot_tier_running() -> bool:
    """True iff BOTH robot-tier containers are running."""
    st = get_container_status()
    return all(st.get(n) == "running" for n in _ROBOT_TIER)


def get_container_logs(container_name: str, lines: int = 50) -> str:
    """Recent logs from a container (for the Protokoll panel)."""
    try:
        result = subprocess.run(
            _docker("logs", "--tail", str(lines), container_name),
            capture_output=True, text=True, timeout=10, env=_scrubbed_env(),
        )
        return result.stdout + result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


# ── Lifecycle: two tiers, NEVER `compose down` ───────────────────────────────


def _compose_up(*services: str, log=None, timeout: int = 180) -> bool:
    """``docker compose up -d --force-recreate --no-deps <services…>``.

    ``--no-deps`` keeps compose's dependency resolution away from services we
    aren't naming (so recreating the robot tier never touches the running
    manager, and vice-versa). The health-gated ``depends_on`` BETWEEN the two
    robot-tier services still applies when both are named."""
    log = log or (lambda _m: None)
    cmd = _compose("up", "-d", "--force-recreate", "--no-deps", *services)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=_scrubbed_env(),
        )
        if result.returncode != 0:
            log(f"Docker Compose Fehler: {result.stderr.strip()}")
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Docker Compose Fehler: {e}")
        return False


def start_manager(log=None) -> bool:
    """Bring up the ALWAYS-ON physical_ai_manager (the wizard/SPA + the
    ``/api/system`` proxy). Brought up by the agent at boot so a freshly booted
    Pi serves the wizard with the robot tier intentionally down (the two-tier
    carve-out). The opi compose additionally marks it ``restart: unless-stopped``
    — a sanctioned exception to the ``restart: "no"`` invariant, because the
    manager IS the GUI on the Pi. No image pull here (boot stays fast on the
    provisioned image; refresh is the /update path)."""
    return start_cloud_only(log=log)


def start_cloud_only(log=None) -> bool:
    """Alias of ``start_manager`` — the System window's cloud-only mode reduces
    to "manager up, skip the robot tier". Kept under the GUI's name so the
    agent's cloud-only path reads the same as the Windows GUI's."""
    return _compose_up(MANAGER_SERVICE, log=log, timeout=120)


def start_robot_tier(log=None) -> bool:
    """„Umgebung starten": bring up open_manipulator + physical_ai_server. Both
    named explicitly so the health-gated ``depends_on`` between THEM applies,
    while ``--no-deps`` leaves the running manager untouched. ``up -d`` honours
    the ``service_healthy`` gate, so this can block up to open_manipulator's
    120 s start_period — the generous timeout covers a slow cold boot."""
    return _compose_up(*_ROBOT_TIER, log=log, timeout=DOCKER_STARTUP_TIMEOUT + 180)


def stop_robot_tier(log=None) -> bool:
    """Stop + remove ONLY the robot-tier containers. NEVER ``compose down`` —
    the ros_net network + the always-on manager must survive (``down`` deletes
    ros_net and severs the agent's gateway listener). The graceful ``stop``
    (SIGTERM) lets open_manipulator's entrypoint run its torque-disable trap
    before exit (Rule §2). Idempotent — a no-op when nothing is present."""
    log = log or (lambda _m: None)
    ok = True
    for action in (["stop", *_ROBOT_TIER], ["rm", "-f", *_ROBOT_TIER]):
        try:
            subprocess.run(
                _compose(*action),
                capture_output=True, text=True, timeout=60, env=_scrubbed_env(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log(f"Docker Compose Fehler: {e}")
            ok = False
    return ok


def ensure_environment_stopped(log=None) -> bool:
    """Tear down any robot-tier containers left over from a previous session
    BEFORE a hardware scan or env start.

    The Dynamixel serial bus must be free before every arm scan: identify_arm.py
    opens the same /dev/serial ports a live 100 Hz controller holds, so a
    running robot tier makes BOTH arms fail to identify. TARGETED (robot tier
    only) — the always-on manager keeps serving the wizard the student is in.
    Returns True iff at least one robot-tier container was present."""
    status = get_container_status()
    present = [
        name for name in _ROBOT_TIER
        if status.get(name, "not found") not in ("not found", "error")
    ]
    if not present:
        return False
    log = log or (lambda _m: None)
    log(f"Vorherige Roboter-Container gefunden ({', '.join(present)}) — werden gestoppt …")
    stop_robot_tier(log=log)
    return True


def restart_open_manipulator(log=None) -> bool:
    """Recreate ONLY open_manipulator in place (the Roboter-Studio leader
    toggle). ``--no-deps`` leaves physical_ai_server + the manager running, so
    the student's session stays connected — only the arm topics blip for ~15-20 s
    while the arm re-homes. Compose reads the freshly-written .env via
    ``--env-file``, so a new FOLLOWER_ONLY value takes effect. No image pull."""
    return _compose_up("open_manipulator", log=log, timeout=DOCKER_STARTUP_TIMEOUT + 60)


def set_leader_mode(config, follower_only: bool, log=None) -> tuple:
    """Switch the arm to follower-only (leader off) or both-arms (leader on) and
    recreate ONLY open_manipulator. Returns (ok, german_message).

    Port of ``gui_app.py::_rs_set_leader_mode``: regenerate the .env in the
    target mode, recreate the arm container, and on a restart FAILURE ROLL THE
    .env BACK to the previous mode so the badge + a later „Umgebung starten"
    don't lie about a dead arm. LAN/subnet keys are carried forward untouched.

    The busy-lock, the readiness gate and the Host/Origin check live in the
    agent's HTTP layer (roboter_studio_control) — this is the docker+.env core.
    """
    log = log or (lambda _m: None)
    if config is None or config.follower is None:
        return False, "Kein Follower-Arm konfiguriert."
    if not follower_only and config.leader is None:
        return False, "Kein Leader-Arm konfiguriert — bitte beide Arme scannen."

    prev_val = config_generator.read_env_var("EDUBOTICS_FOLLOWER_ONLY", ENV_FILE)
    prev_follower_only = str(prev_val).strip() == "1"
    try:
        config_generator.generate_env_file(config, ENV_FILE, follower_only=follower_only)
    except Exception as e:  # noqa: BLE001 — surfaced to the student in German
        return False, f"Konfiguration konnte nicht erstellt werden: {e}"

    if follower_only:
        log("Leader-Arm wird abgeschaltet — Roboter Studio wird vorbereitet …")
    else:
        log("Leader-Arm wird wieder verbunden — Teleoperation wird vorbereitet …")

    ok = restart_open_manipulator(log=log)
    if not ok:
        # Roll the .env back to the mode that was actually running before, so
        # the badge + the next env-start reflect reality.
        try:
            config_generator.generate_env_file(config, ENV_FILE, follower_only=prev_follower_only)
            log("Moduswechsel fehlgeschlagen — Konfiguration zurückgesetzt.")
        except Exception as e:  # noqa: BLE001
            log(f"Rücksetzen der Konfiguration fehlgeschlagen: {e}")
        return False, ("Der Arm-Container konnte nicht neu gestartet werden — "
                       "der vorherige Modus bleibt aktiv.")

    msg = ("Roboter Studio bereit — der Leader-Arm ist abgeschaltet."
           if follower_only else
           "Leader-Arm verbunden — Teleoperation ist wieder verfügbar.")
    return True, msg


def factory_reset(log=None) -> tuple:
    """Delete the persistent EduBotics data volumes (Factory Reset). Returns
    (ok, german_message).

    Wipes datasets, the HF cache and the Roboter-Studio calibration. NEVER
    ``compose down`` (that would delete ros_net + drop the manager). Only
    ``physical_ai_server`` references the data volumes, so stopping the robot
    tier releases them; ``docker volume rm`` then succeeds.

    DEVIATION FROM THE PLAN'S LITERAL SEQUENCE (documented): the plan lists
    "stop robot tier → stop+rm manager → volume rm → recreate manager". The
    always-on manager declares NO data volumes (nginx serves baked files), so
    stopping it is unnecessary for the rm — and it would drop the very
    wizard/Protokoll the student is clicking in, mid-reset. We therefore leave
    the manager UP: the end state (manager serving, volumes wiped) is identical
    with less disruption. The critical invariant (never ``down``) is preserved.
    """
    log = log or (lambda _m: None)
    log("Daten zurücksetzen: Roboter-Container werden gestoppt …")
    stop_robot_tier(log=log)

    try:
        result = subprocess.run(
            _docker("volume", "ls", "--format", "{{.Name}}"),
            capture_output=True, text=True, timeout=30, env=_scrubbed_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"Docker ist nicht erreichbar: {e}"
    if result.returncode != 0:
        return False, (
            "Die Volume-Liste konnte nicht gelesen werden: " + result.stderr.strip()
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

    log(f"Daten zurücksetzen: lösche {', '.join(sorted(targets))} …")
    rm = subprocess.run(
        _docker("volume", "rm", *targets),
        capture_output=True, text=True, timeout=60, env=_scrubbed_env(),
    )
    if rm.returncode != 0:
        return False, (
            "Die Daten-Volumes konnten nicht gelöscht werden: " + rm.stderr.strip()
        )
    return True, (
        f"{len(targets)} Daten-Volume(s) gelöscht: {', '.join(sorted(targets))}"
    )
