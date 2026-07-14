"""Generate the managed ``.env`` from discovered hardware (Orange Pi 5 Pro).

Port of ``robotis_ai_setup/gui/app/config_generator.py`` — the same
managed-key model (atomic temp-file + ``os.replace`` + ``fsync`` writes,
operator overrides preserved verbatim across regenerates, ``HF_TOKEN``
deliberately UNMANAGED so it survives regenerate/factory-reset with
``upsert_env_var`` as its sole writer). Differences from the GUI:

  - The ``.env`` lives at ``/etc/edubotics/.env`` (root systemd agent), not
    ``%LOCALAPPDATA%\\EduBotics\\.env``.
  - The Pi is ALWAYS the ``usb_cam`` path — ``EDUBOTICS_CAMERA_SOURCE=usb_cam``
    is emitted as a managed key (there is no native capture bridge, so the
    Windows ``native_bridge`` branch and the phone-as-cam_id-2 line are gone).
  - ``ROS_DOMAIN_ID`` is derived from ``/etc/machine-id`` (hash mod 233,
    matching the Jetson setup.sh convention), not ``uuid.getnode()``.
  - New Orange-Pi managed keys: ``EDUBOTICS_LAN_OPEN`` (LAN exposure switch)
    and ``EDUBOTICS_ROS_NET_SUBNET`` (docker bridge subnet), plus the DERIVED
    ``EDUBOTICS_BIND_HOST`` (0.0.0.0 / 127.0.0.1) and
    ``EDUBOTICS_ROS_NET_GATEWAY`` (first host of the subnet — the
    ``/api/system`` proxy target). LAN_OPEN + SUBNET are "sticky": a caller
    that passes ``None`` carries the current ``.env`` value forward (so a
    per-rig subnet chosen at provisioning and a student's kiosk toggle both
    survive a hardware re-scan) instead of snapping back to the default.

The device dataclasses (``ArmDevice`` / ``CameraDevice`` / ``HardwareConfig``)
are defined HERE rather than imported from a ``device_manager`` module (the
Pi has none) — the scanning modules (``identify_arm``, ``camera_enum``) build
these and hand them to ``generate_env_file``. Only ``serial_path`` (arms) and
``path`` + ``role`` (cameras) are load-bearing; the other fields exist for
GUI-port familiarity.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .constants import (
    DEFAULT_LAN_OPEN,
    DEFAULT_ROS_NET_GATEWAY,
    DEFAULT_ROS_NET_SUBNET,
    ENV_FILE,
    IMAGE_TAG,
    MACHINE_ID_FILE,
    REGISTRY,
    REGISTRY_FALLBACK,
    ROS_DOMAIN_FILE,
    ROS_DOMAIN_ID,
)


# ── Hardware description (constructed by identify_arm / camera_enum) ──────────


@dataclass
class ArmDevice:
    """An identified robot arm (leader or follower)."""

    serial_path: str          # /dev/serial/by-id/... (stable, survives replug)
    role: str = ""            # "leader" or "follower"
    description: str = ""      # human-readable label for the wizard UI


@dataclass
class CameraDevice:
    """A discovered UVC camera on the ``usb_cam`` (v4l2) path."""

    path: str                 # /dev/video0 or /dev/v4l/by-id/... (usb_cam)
    role: str = ""            # "gripper" or "scene" — assigned in the wizard
    name: str = ""            # human-readable label for the wizard UI


@dataclass
class HardwareConfig:
    """The scanned hardware the ``.env`` is generated from."""

    leader: Optional[ArmDevice] = None
    follower: Optional[ArmDevice] = None
    cameras: list = field(default_factory=list)  # list[CameraDevice], 0-N cameras


# ── Managed-key model ────────────────────────────────────────────────────────

# Keys this generator owns. Anything in the existing .env that is NOT in this
# set is preserved verbatim across rewrites. The set must stay in sync with the
# lines emitted below; orphaning a managed key here would leak stale values.
MANAGED_KEYS = frozenset({
    "FOLLOWER_PORT",
    "LEADER_PORT",
    "ROS_DOMAIN_ID",
    "REGISTRY",
    # REGISTRY_FALLBACK records the Docker Hub twin so the .env documents both
    # registries. Compose runs only ${REGISTRY}; the agent's pull fallback uses
    # constants.REGISTRY_FALLBACK directly. MANAGED so it tracks constants.
    "REGISTRY_FALLBACK",
    # IMAGE_TAG pins compose to the agent's image build (constants.py resolves
    # it: EDUBOTICS_IMAGE_TAG env > docker/versions.env > latest). MANAGED so a
    # stale hand-pinned tag is superseded on the next regenerate.
    "IMAGE_TAG",
    # EDUBOTICS_FOLLOWER_ONLY is MANAGED so the Roboter-Studio-vs-recording
    # session mode is the single source of truth: a Roboter Studio start emits
    # =1 (no leader launched); a both-arms start emits =0, superseding a stale
    # value. The compose reads ${EDUBOTICS_FOLLOWER_ONLY:-0}.
    "EDUBOTICS_FOLLOWER_ONLY",
    # The Pi is ALWAYS usb_cam (no capture bridge). MANAGED + always emitted so
    # a stale native_bridge value from a copied .env can never linger.
    "EDUBOTICS_CAMERA_SOURCE",
    # Orange-Pi LAN exposure + docker-network keys. LAN_OPEN + ROS_NET_SUBNET
    # are the sticky inputs (carried forward when the caller passes None);
    # BIND_HOST + ROS_NET_GATEWAY are DERIVED from them and re-emitted so the
    # compose ports and the /api/system proxy target always agree.
    "EDUBOTICS_LAN_OPEN",
    "EDUBOTICS_BIND_HOST",
    "EDUBOTICS_ROS_NET_SUBNET",
    "EDUBOTICS_ROS_NET_GATEWAY",
    # CAMERA_DEVICE_N / CAMERA_NAME_N are handled by prefix-match below because
    # the count varies with how many cameras are connected.
})
_MANAGED_PREFIXES = ("CAMERA_DEVICE_", "CAMERA_NAME_")

# Auto-added separator before the preserved operator-override block. Defined
# once so _read_unmanaged_lines can skip it on re-read (otherwise a fresh copy
# compounds on every regenerate) and the two emitters stay in sync.
_PRESERVE_MARKER = "# Operator overrides preserved across regeneration."


def _is_managed_key(key: str) -> bool:
    return key in MANAGED_KEYS or key.startswith(_MANAGED_PREFIXES)


def _quote(value: str) -> str:
    """Double-quote a value so docker-compose handles spaces (e.g. a by-id
    path with an embedded space). Escapes embedded quotes/backslashes."""
    if value is None:
        return '""'
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _unquote(value: str) -> str:
    """Inverse of _quote: strip one surrounding pair of double-quotes and
    unescape ``\\"`` / ``\\\\``. Unquoted values pass through unchanged."""
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return v


# ── ROS_DOMAIN_ID (per-machine, from /etc/machine-id) ────────────────────────


def _read_persisted_ros_domain_id() -> Optional[int]:
    """Return the persisted per-machine ROS_DOMAIN_ID, or None when absent /
    unreadable / out of the legal DDS range [0, 232]."""
    try:
        with open(ROS_DOMAIN_FILE, encoding="utf-8") as f:
            raw = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw.isdigit():
        return None
    value = int(raw)
    if 0 <= value <= 232:
        return value
    return None


def _persist_ros_domain_id(value: int) -> None:
    """Best-effort write of the resolved domain id. Never raises — a failed
    persist just means we re-derive next time (same value from machine-id)."""
    try:
        os.makedirs(os.path.dirname(ROS_DOMAIN_FILE), exist_ok=True)
        tmp = ROS_DOMAIN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"{value}\n")
        os.replace(tmp, ROS_DOMAIN_FILE)
    except OSError:
        pass


def _read_machine_id() -> Optional[str]:
    """Return the contents of /etc/machine-id (stripped), or None if
    unreadable/empty. This is the stable per-Pi identifier."""
    try:
        with open(MACHINE_ID_FILE, encoding="utf-8") as f:
            mid = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return mid or None


def _resolve_ros_domain_id() -> int:
    """Resolve a STABLE per-machine ROS_DOMAIN_ID.

    Order: EDUBOTICS_ROS_DOMAIN env override → persisted file → derive from
    /etc/machine-id (hash mod 233; matches the Jetson setup.sh convention) →
    persist. Honest note: on the Pi ``ros_net`` is a docker BRIDGE network, so
    DDS never leaves the host and a domain collision between two Pis is
    harmless — the derivation is kept for fleet-convention consistency and
    host-side ROS tooling, not as the isolation mechanism.

    ``/etc/machine-id`` is rock-stable (unlike the GUI's ``uuid.getnode()`` on
    multi-NIC/VPN laptops), so persistence is belt-and-suspenders here; it only
    matters if machine-id is unreadable and we fall back to ``getnode()``.
    """
    override = os.environ.get("EDUBOTICS_ROS_DOMAIN")
    if override and override.isdigit():
        return max(0, min(232, int(override)))

    persisted = _read_persisted_ros_domain_id()
    if persisted is not None:
        return persisted

    try:
        seed = _read_machine_id() or str(uuid.getnode())
        digest = hashlib.sha256(seed.encode()).digest()
        resolved = int.from_bytes(digest[:2], "big") % 233
    except Exception:
        # Fall back to the legacy default if anything above fails.
        resolved = int(ROS_DOMAIN_ID)

    _persist_ros_domain_id(resolved)
    return resolved


# ── LAN exposure + docker-network derivation (Orange Pi specifics) ───────────


def _derive_gateway(subnet: str) -> str:
    """First host address of ``subnet`` (e.g. 172.28.0.0/24 → 172.28.0.1) — the
    ros_net gateway the browser-facing ``/api/system`` proxy targets. Falls
    back to the default gateway on any parse error."""
    try:
        net = ipaddress.ip_network(subnet, strict=False)
        return str(net.network_address + 1)
    except (ValueError, TypeError):
        return DEFAULT_ROS_NET_GATEWAY


def _valid_subnet(subnet: Optional[str]) -> Optional[str]:
    """Return ``subnet`` normalised (as a CIDR string) if it parses, else None."""
    if not subnet:
        return None
    try:
        return str(ipaddress.ip_network(subnet, strict=False))
    except (ValueError, TypeError):
        return None


def _resolve_lan_open(output_path: str, lan_open: Optional[bool]) -> str:
    """Resolve the sticky EDUBOTICS_LAN_OPEN value as a "1"/"0" string.

    Explicit caller value wins; otherwise the current ``.env`` value is carried
    forward (so a student's kiosk toggle survives a hardware re-scan); otherwise
    the default (open). The key is MANAGED, so the current value is read
    directly (it is not in the preserved/unmanaged block)."""
    if lan_open is not None:
        return "1" if lan_open else "0"
    existing = read_env_var("EDUBOTICS_LAN_OPEN", output_path)
    if existing is not None and existing.strip() in ("0", "1"):
        return existing.strip()
    return DEFAULT_LAN_OPEN


def _resolve_ros_net_subnet(output_path: str, ros_net_subnet: Optional[str]) -> str:
    """Resolve the sticky EDUBOTICS_ROS_NET_SUBNET (carried forward when the
    caller passes None, so a provisioning-time overlap-avoidance choice
    survives regenerates). Invalid values fall back to the default."""
    explicit = _valid_subnet(ros_net_subnet)
    if explicit is not None:
        return explicit
    carried = _valid_subnet(read_env_var("EDUBOTICS_ROS_NET_SUBNET", output_path))
    if carried is not None:
        return carried
    return DEFAULT_ROS_NET_SUBNET


# ── Atomic write + preserve/read helpers ─────────────────────────────────────


def _atomic_write(path: str, content: str) -> None:
    """Write via temp file + rename so a power loss mid-write can't leave a
    truncated .env that compose would fail to parse."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="\n") as f:
        f.write(content)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # fsync unsupported (e.g. some network filesystems)
    os.replace(tmp, path)


def _read_unmanaged_lines(path: str) -> list[str]:
    """Return non-managed lines (comments, blanks, unknown KEY=VALUE) from an
    existing .env so a regenerate doesn't wipe operator-added overrides like
    EDUBOTICS_CAMERA_PIXEL_FORMAT, EDUBOTICS_VCODEC, the calibration knobs, or
    the per-rig HF_TOKEN.

    Returns an empty list when the file doesn't exist yet.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError):
        return []

    preserved: list[str] = []
    for line in raw:
        stripped = line.strip()
        text = line.rstrip("\r\n")
        if not stripped or stripped.startswith("#"):
            # Keep comments + blank lines EXCEPT the auto-added section marker:
            # re-preserving it would compound a fresh copy on every regenerate.
            if stripped == _PRESERVE_MARKER:
                continue
            preserved.append(text)
            continue
        if "=" not in stripped:
            # Malformed line — keep it. Compose will reject it and the student
            # sees the error; better than silently dropping a manual edit.
            preserved.append(text)
            continue
        key = stripped.split("=", 1)[0].strip()
        if _is_managed_key(key):
            continue
        preserved.append(text)
    # Strip leading blank lines: generate_env_file always re-adds a single
    # separating blank before the marker, so carrying leading blanks here would
    # compound them across regenerates.
    while preserved and not preserved[0].strip():
        preserved.pop(0)
    return preserved


def read_env_var(key: str, path: str = ENV_FILE) -> Optional[str]:
    """Return the value of ``key`` from the .env at ``path``, or None if absent.

    Tolerates the quoting written by _quote() and surrounding whitespace. Used
    to show a "token already saved" state WITHOUT re-displaying the secret, and
    to carry sticky managed keys (LAN_OPEN / ROS_NET_SUBNET) forward.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in raw:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        existing_key, _, existing_value = stripped.partition("=")
        if existing_key.strip() == key:
            return _unquote(existing_value)
    return None


def upsert_env_var(key: str, value: str, path: str = ENV_FILE) -> None:
    """Insert or replace ``key=value`` in the .env at ``path``, preserving every
    other line (managed keys, comments, operator overrides) verbatim.

    This is the SOLE writer of HF_TOKEN. HF_TOKEN is deliberately NOT a
    MANAGED_KEY: generate_env_file() carries it across hardware-rescan rewrites
    via _read_unmanaged_lines(), and this helper is how the agent sets it once
    (Schritt D). An empty ``value`` removes the key (token clear). The value is
    quoted via _quote() so a token with shell-special chars is safe.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError):
        raw = []

    new_line = f"{key}={_quote(value)}"
    out: list[str] = []
    replaced = False
    for line in raw:
        text = line.rstrip("\r\n")
        stripped = text.strip()
        if "=" in stripped and not stripped.startswith("#"):
            if stripped.split("=", 1)[0].strip() == key:
                # Replace the first occurrence; drop duplicates. When value is
                # empty we drop the line entirely (removal).
                if value and not replaced:
                    out.append(new_line)
                    replaced = True
                continue
        out.append(text)

    if value and not replaced:
        out.append(new_line)

    content = "\n".join(out).rstrip("\n")
    if content:
        content += "\n"
    _atomic_write(path, content)


# ── Shared opi network/managed key emitter ───────────────────────────────────


def _opi_managed_lines(output_path: str, lan_open: Optional[bool],
                       ros_net_subnet: Optional[str]) -> list[str]:
    """The Orange-Pi managed lines shared by every .env variant: registry pins,
    the explicit usb_cam source, and the LAN/docker-network block (with the
    DERIVED bind host + gateway)."""
    lan_open_str = _resolve_lan_open(output_path, lan_open)
    bind_host = "0.0.0.0" if lan_open_str == "1" else "127.0.0.1"
    subnet = _resolve_ros_net_subnet(output_path, ros_net_subnet)
    gateway = _derive_gateway(subnet)
    return [
        f"REGISTRY={REGISTRY}",
        f"REGISTRY_FALLBACK={REGISTRY_FALLBACK}",
        # Pin compose to the image build this agent ships with. Without the
        # line compose runs :latest, drifting past the pinned tag.
        f"IMAGE_TAG={IMAGE_TAG}",
        # The Pi is always the in-container usb_cam path — set it EXPLICITLY
        # (don't lean on the entrypoint's non-WSL auto-detect alone).
        "EDUBOTICS_CAMERA_SOURCE=usb_cam",
        # LAN exposure: EDUBOTICS_LAN_OPEN=1 → published ports on 0.0.0.0
        # (school LAN, the locked default); =0 → 127.0.0.1 (kiosk). BIND_HOST
        # is DERIVED so the compose ${EDUBOTICS_BIND_HOST} ports and this key
        # can never disagree.
        f"EDUBOTICS_LAN_OPEN={lan_open_str}",
        f"EDUBOTICS_BIND_HOST={bind_host}",
        # ros_net IPAM. GATEWAY is the first host of the subnet — the target
        # of nginx's /api/system reverse proxy to the agent (:8769).
        f"EDUBOTICS_ROS_NET_SUBNET={subnet}",
        f"EDUBOTICS_ROS_NET_GATEWAY={gateway}",
    ]


# ── .env generators ──────────────────────────────────────────────────────────


def generate_env_file(config: HardwareConfig, output_path: str = ENV_FILE,
                      follower_only: bool = False,
                      lan_open: Optional[bool] = None,
                      ros_net_subnet: Optional[str] = None) -> str:
    """Write the managed .env from the scanned hardware.

    Args:
        config: Discovered hardware (follower required; leader required unless
            ``follower_only``; 0-N usb_cam cameras).
        output_path: Path to write the .env file.
        follower_only: When True (Roboter Studio mode), emit the managed
            ``EDUBOTICS_FOLLOWER_ONLY=1`` line and OMIT ``LEADER_PORT`` — the
            entrypoint never launches the leader, so the workflow/calibration
            trajectory publisher is the sole writer on /leader/joint_trajectory.
            When False a both-arms recording/teleop session is configured:
            ``LEADER_PORT`` is emitted and ``EDUBOTICS_FOLLOWER_ONLY=0`` is
            emitted explicitly (the leader toggle regenerates exactly that key).
        lan_open: Explicit LAN-exposure override (True → 0.0.0.0, False →
            127.0.0.1). ``None`` carries the current .env value forward
            (default open on a fresh file).
        ros_net_subnet: Explicit docker ros_net subnet (CIDR). ``None`` carries
            the current .env value forward (default ``172.28.0.0/24``), so a
            provisioning-time overlap-avoidance choice survives regenerates.

    Returns:
        The content written to the file.
    """
    if config.follower is None:
        raise ValueError("Der Follower-Arm muss konfiguriert sein, bevor die .env erzeugt wird")
    if not follower_only and config.leader is None:
        raise ValueError("Der Leader-Arm muss konfiguriert sein — bitte beide Arme scannen")

    domain_id = _resolve_ros_domain_id()
    preserved = _read_unmanaged_lines(output_path)
    lines: list[str] = []
    lines.append(f"FOLLOWER_PORT={_quote(config.follower.serial_path)}")
    if follower_only:
        # Roboter Studio: no leader launched. LEADER_PORT is intentionally
        # omitted (MANAGED → a stale value is dropped, not preserved).
        lines.append("EDUBOTICS_FOLLOWER_ONLY=1")
    else:
        lines.append(f"LEADER_PORT={_quote(config.leader.serial_path)}")
        # Emit =0 EXPLICITLY (unlike the GUI, which omits it): the Pi compose
        # reads ${EDUBOTICS_FOLLOWER_ONLY:-0} and the leader toggle expects to
        # find and flip this exact key.
        lines.append("EDUBOTICS_FOLLOWER_ONLY=0")

    if config.cameras:
        for i, cam in enumerate(config.cameras, 1):
            # Refuse to write a camera without a valid role: omx_f_config.yaml
            # hard-codes the gripper/scene topic names, so a fallback role would
            # make the subscriber wait forever. The wizard always sets a role;
            # this guard catches programmatic misuse.
            if cam.role not in ("gripper", "scene"):
                raise ValueError(
                    f"Kamera ohne gültige Rolle (gripper/scene): {cam.path}"
                )
            # usb_cam path: the container captures /dev/video* directly, so the
            # device path is written (unlike the Windows native_bridge, which
            # left this empty).
            lines.append(f"CAMERA_DEVICE_{i}={_quote(cam.path)}")
            lines.append(f"CAMERA_NAME_{i}={_quote(cam.role)}")

    lines.append(f"ROS_DOMAIN_ID={domain_id}")
    lines.extend(_opi_managed_lines(output_path, lan_open, ros_net_subnet))

    if preserved:
        lines.append("")
        lines.append(_PRESERVE_MARKER)
        lines.extend(preserved)
    lines.append("")  # trailing newline

    content = "\n".join(lines)
    _atomic_write(output_path, content)
    return content


def generate_cloud_only_env(output_path: str = ENV_FILE,
                            lan_open: Optional[bool] = None,
                            ros_net_subnet: Optional[str] = None) -> str:
    """Write a minimal .env for manager-only / cloud-only mode (no robot tier).

    Used at agent boot to bring the always-on manager up BEFORE any hardware
    scan (a freshly flashed Pi has no arms configured yet), and for the System
    window's cloud-only checkbox. Compose still reads the .env for every
    ``${VAR}``, so we provide empty port placeholders plus the full opi managed
    block (the manager service reads ``EDUBOTICS_ROS_NET_GATEWAY`` /
    ``EDUBOTICS_BIND_HOST`` and compose reads ``REGISTRY`` / ``IMAGE_TAG`` /
    ``EDUBOTICS_ROS_NET_SUBNET``). Operator overrides + HF_TOKEN are preserved.
    """
    domain_id = _resolve_ros_domain_id()
    preserved = _read_unmanaged_lines(output_path)
    lines = [
        "# Cloud-only / manager-only mode — no robot hardware configured.",
        'FOLLOWER_PORT=""',
        'LEADER_PORT=""',
        "EDUBOTICS_FOLLOWER_ONLY=0",
        'CAMERA_DEVICE_1=""',
        'CAMERA_NAME_1="gripper"',
        'CAMERA_DEVICE_2=""',
        'CAMERA_NAME_2="scene"',
        f"ROS_DOMAIN_ID={domain_id}",
    ]
    lines.extend(_opi_managed_lines(output_path, lan_open, ros_net_subnet))
    if preserved:
        lines.append("")
        lines.append(_PRESERVE_MARKER)
        lines.extend(preserved)
    lines.append("")
    content = "\n".join(lines)
    _atomic_write(output_path, content)
    return content
