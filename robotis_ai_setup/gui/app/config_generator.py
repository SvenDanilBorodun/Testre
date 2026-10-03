"""Generate .env file from discovered hardware configuration."""

from __future__ import annotations

import hashlib
import os
import socket
import sys
import uuid

from .constants import (
    DEFAULT_ROBOT_PROFILE,
    ENV_FILE,
    IMAGE_TAG,
    REGISTRY,
    REGISTRY_FALLBACK,
    ROBOT_PROFILES,
    ROS_DOMAIN_ID,
)
from .device_manager import HardwareConfig


# Keys this generator owns. Anything in the existing .env that is NOT
# in this set gets preserved verbatim across rewrites. The set must stay
# in sync with the lines emitted below; orphaning a managed key here
# would leak stale values into newly generated files.
MANAGED_KEYS = frozenset({
    "FOLLOWER_PORT",
    "LEADER_PORT",
    "ROS_DOMAIN_ID",
    "REGISTRY",
    # REGISTRY_FALLBACK records the Docker Hub twin so the .env documents both
    # registries. Compose runs only ${REGISTRY}; the GUI's pull fallback uses
    # constants.REGISTRY_FALLBACK directly. MANAGED so it tracks constants.
    "REGISTRY_FALLBACK",
    # IMAGE_TAG pins compose to the installer's image build (constants.py
    # resolves it: EDUBOTICS_IMAGE_TAG env > docker/versions.env > latest).
    # It is MANAGED so a stale hand-pinned tag is superseded on the next
    # regenerate instead of silently redirecting compose: a leftover
    # validation-only IMAGE_TAG=collision-validate in the preserved block
    # broke "Umgebung starten" with "manifest unknown" on 2026-06-05 after
    # an installer upgrade had wiped the local image it pointed at.
    "IMAGE_TAG",
    # EDUBOTICS_CAMERA_NAMES is MANAGED so the phone-as-3rd-camera toggle is
    # the single source of truth: enabling it emits gripper,scene,phone and
    # disabling it later (line absent → compose default gripper,scene) SUPERSEDES
    # a stale 3-name value instead of leaving the /phone publisher orphaned.
    "EDUBOTICS_CAMERA_NAMES",
    # EDUBOTICS_FOLLOWER_ONLY is MANAGED so the Roboter-Studio-vs-recording
    # session mode is the single source of truth: a Roboter Studio start emits
    # =1 (no leader launched); a recording start omits the line so compose's
    # default (0) SUPERSEDES a stale =1 instead of silently keeping the leader
    # off in a session that needs it.
    "EDUBOTICS_FOLLOWER_ONLY",
    # EDUBOTICS_ROBOT_TYPE is the GUI-hardset robot profile id (omx_full |
    # omx_follower). MANAGED so the selector is the single source of truth: a
    # stale hand-pinned value is superseded on every regenerate, and the initial
    # EDUBOTICS_FOLLOWER_ONLY is derived from it. The server reads it at boot to
    # resolve its ArmProfile (capabilities + kinematics seam).
    "EDUBOTICS_ROBOT_TYPE",
    # CAMERA_DEVICE_N / CAMERA_NAME_N are handled by prefix-match below
    # because the count varies with how many cameras are connected.
})
_MANAGED_PREFIXES = ("CAMERA_DEVICE_", "CAMERA_NAME_")

# Auto-added separator before the preserved operator-override block. Defined
# once so _read_unmanaged_lines can skip it on re-read (otherwise a fresh copy
# compounds on every regenerate) and the two emitters stay in sync.
_PRESERVE_MARKER = "# Operator overrides preserved across regeneration."


def _is_managed_key(key: str) -> bool:
    return key in MANAGED_KEYS or key.startswith(_MANAGED_PREFIXES)


def _has_camera_source(preserved: list[str]) -> bool:
    """True if a preserved (operator-set) line already defines the camera source."""
    return any(p.lstrip().startswith("EDUBOTICS_CAMERA_SOURCE=") for p in preserved)


def _quote(value: str) -> str:
    """Double-quote a value so docker-compose handles spaces.

    Paths like `/mnt/c/Users/Max Muster/...` would otherwise break env parsing
    (compose stops at the space and treats the remainder as another var).
    """
    if value is None:
        return '""'
    # Escape any embedded double-quotes and backslashes.
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _unquote(value: str) -> str:
    """Inverse of _quote: strip one surrounding pair of double-quotes and
    unescape \\" / \\\\. Unquoted values pass through unchanged."""
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return v


def _value_in_lines(lines: list[str], key: str) -> str | None:
    """The value of ``key`` among ALREADY-READ .env lines, or None if absent.

    Factored out of ``read_env_var`` so a caller that already holds the lines
    does not read the file a second time, and so there is one place that knows
    the ``KEY=VALUE`` shape. Keys match EXACTLY, so a key that is a prefix of
    another (``HF_TOKEN`` and ``HF_TOKEN_MACHINE``) never shadows it.
    """
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        existing_key, _, existing_value = stripped.partition("=")
        if existing_key.strip() == key:
            return _unquote(existing_value)
    return None


def _canonical_computer_name(raw: str) -> str:
    """Reduce a computer name to the ONE spelling every source agrees on.

    With no cache, seed stability is the only thing pinning a PC to one domain —
    and the three sources ``_read_machine_id`` falls through
    (``ActiveComputerName`` → ``%COMPUTERNAME%`` → ``socket.gethostname()``) can
    each hand back a different STRING for the same machine: a shell started
    before a rename still carries the old ``%COMPUTERNAME%``, ``gethostname()``
    can return the DNS form ``pc-a12.schule.local``, and case is not guaranteed
    to match. A different string is a different sha256 is a different domain, so
    the machine would silently move whenever the fallback order shifted.

    Short name, stripped, upper-cased: NetBIOS names are case-insensitive and
    have no dot, so this loses nothing a Windows computer name can carry.
    """
    short = (raw or "").strip().split(".")[0].strip()
    return short.upper()


def _read_machine_id() -> str | None:
    """Return a STABLE per-machine identifier, or None when none can be read.

    The Windows twin of the Pi's ``_read_machine_id`` (``pi_agent/
    config_generator.py``), which simply reads ``/etc/machine-id``. Windows has
    no such file, so the identity is COMPOSED from two independent parts joined
    by ``\\x00``:

      1. ``HKLM\\SOFTWARE\\Microsoft\\Cryptography`` value ``MachineGuid`` —
         readable by a standard user, no elevation. That key IS WOW64-redirected,
         so the open carries ``KEY_WOW64_64KEY``: without it a 32-bit interpreter
         reads the ``WOW6432Node`` view and can see a different (or no) value.
      2. The computer name, read from ``HKLM\\SYSTEM\\CurrentControlSet\\Control
         \\ComputerName\\ActiveComputerName`` value ``ComputerName``, falling
         back to ``%COMPUTERNAME%`` and then ``socket.gethostname()``. The
         registry comes FIRST because it is the OS's own record of the name the
         machine currently answers to and it survives a stripped or service
         environment that carries no ``%COMPUTERNAME%`` — and dropping silently
         to the guid alone there would move the PC onto a different domain.
         ``gethostname()`` is LAST: on Windows it can hand back a DNS- or
         NetBIOS-shaped variant of the same name, so it is the source least
         likely to agree with the other two. All three are put through
         ``_canonical_computer_name`` for exactly that reason — a source switch
         that changes the STRING would otherwise change the domain.

    THE COMPOSITE IS DELIBERATE, and the reason is the deployment this exists
    for. ``MachineGuid`` is regenerated by ``sysprep /generalize`` but NOT by a
    plain disk clone — so a school that images its PCs WITHOUT generalize hands
    every machine the same guid AND the same cloned user profile, i.e. exactly
    the collision the ROS_DOMAIN_ID derivation must prevent. The computer name
    is unique across a domain-joined fleet and diverges the moment a clone is
    renamed or joined, so the pair survives both imaging styles. Neither part
    alone does.

    ONE primitive would make the composite unnecessary and is deliberately not
    used: the SMBIOS system UUID (``Win32_ComputerSystemProduct.UUID``) is
    per-chassis and survives both imaging styles by itself. Reading it means WMI
    or a subprocess on the tkinter startup path, and OEMs are known to ship
    all-zero or batch-duplicated UUIDs — which fails in precisely the cloned-
    fleet case this function has to handle, while a wrong-but-plausible value
    would be indistinguishable from a correct one. (A third objection, that a
    hardware serial would end up in a file support collects, died with the
    persist below: nothing writes the identifier to disk any more.)

    ``winreg`` is imported LAZILY inside the win32 branch — and in its own
    ``try``/``except ImportError``, matching ``usbipd_resolver`` and
    ``win_camera`` — so this module still imports on macOS/Linux (the deps-free
    test suite must stay cross-platform). Off win32 the probe is skipped
    entirely and callers fall back to ``uuid.getnode()``.
    """
    # The override wins outright: it is the test seam (the deps-free suite must
    # resolve an identity on macOS/Linux, where the probes below return None)
    # AND the ops escape hatch for a fleet whose imaging defeats both probes.
    # Read at CALL time, not import time, so it behaves like the
    # EDUBOTICS_ROS_DOMAIN rollback a few lines below rather than freezing into
    # a module constant that only one of two bindings could patch.
    override = os.environ.get("EDUBOTICS_MACHINE_ID", "").strip()
    if override:
        return override

    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-untyped]
    except ImportError:
        return None

    parts: list[str] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        guid = str(guid).strip()
        if guid:
            parts.append(guid)
    except (OSError, ValueError):
        pass  # missing key / wrong value type — fall through to the name

    computer = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName",
        ) as key:
            name, _ = winreg.QueryValueEx(key, "ComputerName")
        computer = str(name).strip()
    except (OSError, ValueError):
        pass
    if not computer:
        computer = (os.environ.get("COMPUTERNAME") or "").strip()
    if not computer:
        try:
            computer = socket.gethostname().strip()
        except OSError:
            computer = ""
    computer = _canonical_computer_name(computer)
    if computer:
        parts.append(computer)

    if not parts:
        return None
    return "\x00".join(parts)


# ── Legacy HuggingFace token: removed from this PC ───────────────────────
# The token used to be typed into this GUI (Schritt D) and stored here as
# HF_TOKEN, with HF_TOKEN_MACHINE beside it as a machine fingerprint. That
# directory TRAVELS (roaming profile, FSLogix, a golden image), so the token
# could follow a copied profile onto another PC. The token now belongs to the
# student's ACCOUNT in the cloud and reaches the robot from the web interface's
# Startseite; this file never carries one again.
#
# Both keys stay UNMANAGED (MANAGED_KEYS is unchanged) but are never preserved:
# ``_read_unmanaged_lines`` drops them on every regenerate, including the
# runtime one (gui_app.py::_rs_set_leader_mode), so a legacy token can not reach
# the --env-file compose reads. ``purge_legacy_hf_token`` is the half that
# removes them from an EXISTING file, once per launch, and reports it.
_LEGACY_HF_KEYS = ("HF_TOKEN", "HF_TOKEN_MACHINE")


def _resolve_ros_domain_id() -> int:
    """Resolve a STABLE per-machine ROS_DOMAIN_ID so two student laptops on the
    same school LAN don't share ROS topics.

    Order: ``EDUBOTICS_ROS_DOMAIN`` env override, clamped to the legal DDS range
    [0, 232] → ``sha256(_read_machine_id() or str(uuid.getnode())) % 233``. The
    seed expression is spelled identically on the Pi twin
    (``test_ros_domain_twin_lockstep.py`` pins it). Hardcoded 30 on every
    install meant Student A's inference could drive Student B's arm on the same
    Wi-Fi — ``/leader/joint_trajectory`` is the arm command rail, so a domain
    collision is not a cosmetic DDS problem. The override always wins and is the
    documented one-variable rollback.

    THERE IS DELIBERATELY NO CACHE HERE, and the absence is the non-obvious
    part: the Pi twin has one and an earlier revision of this function had one
    too, so the next reader will want to re-add it. Windows offers no path a
    golden image does not copy — ``%LOCALAPPDATA%`` travels with a roaming
    profile / FSLogix container / AppData redirection, and ``%ProgramData%`` is
    captured by the image just the same. A cache on such a path must therefore
    be either COPY-PROOF (stamped with a machine fingerprint, discarded on a
    mismatch) or a stabiliser for the ``uuid.getnode()`` fallback — and it
    cannot be both, because the fingerprint IS ``_read_machine_id()``: exactly
    when no identity resolves, and getnode() is the seed that needs
    stabilising, there is nothing to stamp with and the entry can never be
    trusted on re-read. Copy-proofing is the half that matters, and once the
    cache is copy-proofed it can only ever hand back what re-deriving on the
    same machine hands back. It was deleted rather than kept as belt-and-braces
    the next reader would have to reason through again.

    THE PI TWIN KEEPS ITS CACHE AND THAT IS CORRECT — do not "harmonise" it
    away. ``/var/lib/edubotics/.ros_domain_id`` is genuinely machine-local and
    cannot travel with a user profile, so the Pi faces none of that tension and
    its un-fingerprinted cache legitimately stabilises its own getnode()
    fallback.

    ``uuid.getnode()`` survives only as the last resort because it is NOT stable
    on multi-NIC / VPN / docking-station PCs (it may pick a different interface,
    or a random fallback, between calls). That matters WITHIN one session too,
    not just across them: the Roboter-Studio leader toggle regenerates the .env
    and ``docker_manager.restart_open_manipulator`` recreates the arm container
    with ``--no-deps`` while ``physical_ai_server`` keeps running, so a seed
    that moved between two writes would strand the two containers on different
    DDS domains.
    """
    override = os.environ.get("EDUBOTICS_ROS_DOMAIN")
    if override and override.isdigit():
        return max(0, min(232, int(override)))

    try:
        seed = _read_machine_id() or str(uuid.getnode())
        digest = hashlib.sha256(seed.encode()).digest()
        return int.from_bytes(digest[:2], "big") % 233
    except Exception:
        # Fall back to the legacy default if anything above fails.
        return int(ROS_DOMAIN_ID)


def _atomic_write(path: str, content: str) -> None:
    """Write via temp file + rename so a power loss mid-write can't leave
    a truncated .env that compose would fail to parse."""
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
    """Return non-managed lines (comments, blanks, unknown KEY=VALUE)
    from an existing .env so a regenerate doesn't wipe operator-added
    overrides like EDUBOTICS_CAMERA_PIXEL_FORMAT, EDUBOTICS_ROS_DOMAIN or
    EDUBOTICS_REGISTRY.

    ONE exception to "everything unmanaged is preserved verbatim": the legacy
    ``HF_TOKEN`` / ``HF_TOKEN_MACHINE`` pair is ALWAYS dropped (see
    ``_LEGACY_HF_KEYS``). The student's token lives with the cloud account now,
    and a stale copy left in this file could be picked up by the robot instead
    of the account's token.

    Returns an empty list when the file doesn't exist yet.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError):
        return []

    # Dropping here, and not only in the launch-time purge, is what makes "no
    # legacy token reaches the --env-file" true by construction: every .env
    # compose reads is one a generator just wrote through this helper, and
    # generate_env_file has callers that never pass through startup
    # (gui_app.py::_rs_set_leader_mode regenerates at RUNTIME, on the :8769
    # control-server thread, for the Roboter-Studio leader toggle and again on
    # its rollback).
    preserved: list[str] = []
    for line in raw:
        stripped = line.strip()
        # Drop trailing newline; we re-add when emitting.
        text = line.rstrip("\r\n")
        if not stripped or stripped.startswith("#"):
            # Keep comments + blank lines so the file stays human-readable
            # if anyone hand-edited it. EXCEPT the auto-added section marker:
            # re-preserving it would compound a fresh copy on every regenerate
            # (one extra marker per hardware re-scan).
            if stripped == _PRESERVE_MARKER:
                continue
            preserved.append(text)
            continue
        if "=" not in stripped:
            # Malformed line — keep it. Compose will reject it, the
            # student will see the error, and they can fix it; better
            # than silently dropping their manual edit.
            preserved.append(text)
            continue
        key = stripped.split("=", 1)[0].strip()
        if _is_managed_key(key):
            continue
        if key in _LEGACY_HF_KEYS:
            continue
        preserved.append(text)
    # Strip leading blank lines: generate_env_file always re-adds a single
    # separating blank before the marker, so carrying leading blanks here
    # would compound them across regenerates.
    while preserved and not preserved[0].strip():
        preserved.pop(0)
    return preserved


def read_env_var(key: str, path: str = ENV_FILE) -> str | None:
    """Return the value of ``key`` from the .env at ``path``, or None if absent.

    Tolerates the quoting written by _quote() and surrounding whitespace.
    Returns None when the file is missing/unreadable.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    return _value_in_lines(raw, key)


def upsert_env_var(key: str, value: str, path: str = ENV_FILE) -> None:
    """Insert or replace ``key=value`` in the .env at ``path``, preserving
    every other line (managed keys, comments, operator overrides) verbatim.

    A key that is not one of the MANAGED_KEYS is carried across
    generate_env_file() rewrites by _read_unmanaged_lines(), so an operator
    override written here survives a hardware rescan. (The legacy HF_TOKEN pair
    is the one exception: it is never carried, see _LEGACY_HF_KEYS.) An empty
    ``value`` removes the key. The value is quoted via _quote(), so a value with
    shell-special characters is safe.
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
                # Replace the first occurrence; drop any duplicates. When
                # value is empty we drop the line entirely (removal).
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


def purge_legacy_hf_token(path: str = ENV_FILE) -> bool:
    """Remove the legacy ``HF_TOKEN`` and ``HF_TOKEN_MACHINE`` from the .env.

    Returns True only when a NON-EMPTY ``HF_TOKEN`` was present and removed,
    which is what the caller tells the student about. A leftover stamp, or an
    empty ``HF_TOKEN=`` line, is removed silently and returns False: there was
    no credential to announce. A missing or unreadable file returns False and
    writes nothing (an unreadable file is never rewritten, so a transient read
    error can not turn into a truncated .env). Idempotent.

    One atomic write for both keys, every other line kept verbatim (line endings
    are normalised to LF, as ``upsert_env_var`` does). The value is read only to
    decide whether it was empty; it is never returned, logged or stored.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError):
        return False

    kept: list[str] = []
    removed = False
    had_token = False
    for line in raw:
        text = line.rstrip("\r\n")
        stripped = text.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key in _LEGACY_HF_KEYS:
                removed = True
                if key == "HF_TOKEN" and _unquote(value):
                    had_token = True
                continue
        kept.append(text)
    if not removed:
        return False

    content = "\n".join(kept).rstrip("\n")
    if content:
        content += "\n"
    _atomic_write(path, content)
    return had_token


def _phone_camera_names_line() -> str:
    """The managed EDUBOTICS_CAMERA_NAMES line that adds the phone as cam_id 2.

    Built from CAMERA_BRIDGE_ROLES + the phone name so the order stays in lockstep
    with camera_bridge's cam_id mapping (gripper=0, scene=1, phone=2)."""
    from .constants import CAMERA_BRIDGE_ROLES, PHONE_CAMERA_NAME
    names = ",".join(list(CAMERA_BRIDGE_ROLES) + [PHONE_CAMERA_NAME])
    return f"EDUBOTICS_CAMERA_NAMES={names}"


def generate_env_file(config: HardwareConfig, output_path: str = ENV_FILE,
                      phone_camera: bool = False,
                      robot_type: str = DEFAULT_ROBOT_PROFILE,
                      follower_only: bool | None = None) -> str:
    """Write .env file with hardware paths.

    Args:
        config: Discovered hardware configuration.
        output_path: Path to write the .env file.
        phone_camera: When True, emit the managed
            ``EDUBOTICS_CAMERA_NAMES=gripper,scene,phone`` line so the ingest
            node publishes /phone/image_raw/compressed (cam_id 2). When False the
            line is omitted and compose's default (gripper,scene) wins — and a
            stale 3-name value is superseded because the key is MANAGED.
        robot_type: The GUI-hardset ArmProfile id (``omx_full``|``omx_follower``)
            emitted as the managed ``EDUBOTICS_ROBOT_TYPE`` line and read by the
            server at boot. Also DERIVES the initial ``follower_only`` when that
            argument is left as None (omx_follower ⇒ True).
        follower_only: When True (Roboter Studio mode), emit the managed
            ``EDUBOTICS_FOLLOWER_ONLY=1`` line and OMIT ``LEADER_PORT`` — the
            entrypoint then never launches the leader, so the follower's
            arm_controller is driven solely by the workflow/calibration
            trajectory publisher (no leader broadcaster to clobber it). A
            leader need not be scanned/configured in this mode. When False a
            recording/teleop session is configured and both arms are required;
            the follower-only line is omitted so compose's default (0)
            supersedes any stale =1 (the key is MANAGED). When None (the
            default) the value is DERIVED from ``robot_type`` (via the
            ``constants.ROBOT_PROFILES`` registry) — this keeps the RS runtime
            leader-toggle able to OVERRIDE it while an omx_follower rig (which
            never scans a leader) still derives True instead of tripping the
            leader-required guard below. Explicitly passing False for a
            follower-only profile is CONTRADICTORY (the profile has no leader
            to re-arm) and raises a German ValueError instead of silently
            emitting a both-arms .env.

    Returns:
        The content written to the file.
    """
    # Resolve the initial follower_only from the ArmProfile registry — NOT a
    # hardcoded id literal — so a new follower-only profile is honoured without
    # editing this line (single source of truth: constants.ROBOT_PROFILES).
    # Derive FIRST (before the leader-null guard): without this an omx_follower
    # rig — which has no leader — would hit `not follower_only and leader is
    # None` and raise on every start. An explicit follower_only= (the RS toggle)
    # still wins, EXCEPT that re-arming the leader on a leader-less profile is
    # contradictory (a follower-only .env has no LEADER_PORT to emit) and is
    # refused loudly rather than silently writing a both-arms .env for a rig
    # that never scanned a leader.
    profile_follower_only = ROBOT_PROFILES.get(robot_type, {}).get(
        "follower_only", False)
    if follower_only is None:
        follower_only = profile_follower_only
    elif profile_follower_only and not follower_only:
        raise ValueError(
            f'Robotertyp „{robot_type}" erlaubt keinen Leader-Betrieb '
            f'(follower_only=False).'
        )
    if config.follower is None:
        raise ValueError("Der Follower-Arm muss konfiguriert sein, bevor die .env erzeugt wird")
    if not follower_only and config.leader is None:
        raise ValueError("Leader- und Follower-Arm müssen konfiguriert sein, bevor die .env erzeugt wird")

    from .constants import cameras_use_native_bridge
    native = cameras_use_native_bridge()

    domain_id = _resolve_ros_domain_id()
    preserved = _read_unmanaged_lines(output_path)
    lines: list[str] = []
    lines.append(f"FOLLOWER_PORT={_quote(config.follower.serial_path)}")
    if follower_only:
        # Roboter Studio: no leader launched. The workflow/calibration publisher
        # is the sole writer on /leader/joint_trajectory — there is no leader
        # broadcaster to arbitrate against. LEADER_PORT is intentionally omitted
        # (MANAGED → a stale value is dropped, not preserved).
        lines.append("EDUBOTICS_FOLLOWER_ONLY=1")
    else:
        lines.append(f"LEADER_PORT={_quote(config.leader.serial_path)}")

    if config.cameras:
        for i, cam in enumerate(config.cameras, 1):
            # Audit F8: refuse to write a camera without a valid role
            # (gripper / scene). omx_f_config.yaml hard-codes those
            # topic names, so a `camera1`/`camera2` fallback would
            # make the subscriber wait forever. The GUI wizard always
            # sets a role; this guard catches programmatic misuse.
            if cam.role not in ('gripper', 'scene'):
                raise ValueError(
                    f"Kamera ohne gültige Rolle (gripper/scene): {cam.path}"
                )
            # SECOND fence: a role this ROBOT TYPE has no topic for. The
            # generic check above only knows the two names that exist at all;
            # `edu6_studio` declares camera_roles=('scene',) and its
            # config/edu6_studio_config.yaml subscribes to exactly
            # /scene/image_raw/compressed, so CAMERA_NAME_1="gripper" there
            # publishes a topic nothing reads — and the failure is SILENT AND
            # GREEN, because the compose healthcheck greps
            # /${CAMERA_NAME_1}/image_raw/compressed, i.e. the very topic the
            # student's own role name causes the bridge to publish. „Umgebung
            # starten" reports success and Roboter Studio is empty.
            #
            # This is the server-side half of the picker filter in
            # `gui_app.py::_start_camera_previews` — a stale in-memory
            # selection or a programmatic caller bypasses the UI. It mirrors
            # the Pi's `handle_cameras_roles` German 400, closing the twin
            # divergence CLAUDE.md named as "known, accepted, unguarded".
            # AFTER the generic check on purpose: a bogus role must still get
            # the „ohne gültige Rolle" message that names what a role IS.
            allowed_roles = ROBOT_PROFILES.get(robot_type, {}).get(
                "camera_roles", ('gripper', 'scene'))
            if allowed_roles and cam.role not in allowed_roles:
                label = ROBOT_PROFILES.get(robot_type, {}).get(
                    "display_de", robot_type)
                role_de = "Szene" if cam.role == "scene" else "Greifer"
                raise ValueError(
                    f'Für den Robotertyp „{label}“ ist die Kamera-Rolle '
                    f'„{role_de}“ nicht vorgesehen.'
                )
            # In native_bridge mode the container does NOT capture from
            # /dev/video* (the Windows GUI streams JPEG frames into
            # camera_ingest_node.py). CAMERA_DEVICE stays empty so the
            # entrypoint/healthcheck never wait on a non-existent device;
            # the role still drives the published /<role>/image_raw/compressed
            # topic. The capture index lives in the GUI session, not the .env.
            device_value = "" if native else cam.path
            lines.append(f"CAMERA_DEVICE_{i}={_quote(device_value)}")
            lines.append(f"CAMERA_NAME_{i}={_quote(cam.role)}")

    lines.append(f"ROS_DOMAIN_ID={domain_id}")
    lines.append(f"REGISTRY={REGISTRY}")
    lines.append(f"REGISTRY_FALLBACK={REGISTRY_FALLBACK}")
    # Pin compose to the image build this GUI ships with. docker-compose.yml
    # resolves ${IMAGE_TAG:-latest} from this file (--env-file); without the
    # line, compose silently runs :latest — drifting past the installer's
    # pinned tag AND re-downloading ~9 GB the installer already pulled.
    lines.append(f"IMAGE_TAG={IMAGE_TAG}")
    # GUI-hardset robot type — the server resolves its ArmProfile from this at
    # boot. MANAGED so the selector is authoritative; a full restart changes it.
    lines.append(f"EDUBOTICS_ROBOT_TYPE={robot_type}")
    # Default camera source. Yields to an operator override already present in
    # the preserved (unmanaged) lines, so EDUBOTICS_CAMERA_SOURCE=usb_cam in a
    # hand-edited .env survives regeneration (one-variable rollback).
    if native and not _has_camera_source(preserved):
        lines.append("EDUBOTICS_CAMERA_SOURCE=native_bridge")
    # Phone-as-3rd-camera: add "phone" to the published camera names so
    # camera_ingest_node publishes /phone/image_raw/compressed (cam_id 2). Only
    # when the student enabled the toggle; otherwise omitted (compose default
    # gripper,scene). MANAGED key → unchecking later supersedes a stale value.
    if phone_camera:
        lines.append(_phone_camera_names_line())
    if preserved:
        lines.append("")
        lines.append(_PRESERVE_MARKER)
        lines.extend(preserved)
    lines.append("")  # trailing newline

    content = "\n".join(lines)
    _atomic_write(output_path, content)
    return content


def generate_cloud_only_env(output_path: str = ENV_FILE,
                            phone_camera: bool = False,
                            robot_type: str = DEFAULT_ROBOT_PROFILE) -> str:
    """Write a minimal .env for cloud-only mode (no robot hardware).

    Docker Compose still reads .env when starting any service, so we provide
    empty placeholders for the variables referenced by the open_manipulator
    service (which we don't start in this mode anyway). Without this, compose
    would emit warnings about unset variables.

    ``phone_camera`` is accepted for signature symmetry with generate_env_file
    but is effectively a no-op here: cloud-only never starts open_manipulator,
    so no camera ingest node consumes EDUBOTICS_CAMERA_NAMES. We still honour it
    so a toggled-on value round-trips rather than being dropped.

    ``robot_type`` is emitted for MANAGED-key symmetry (so a stale hand-pinned
    EDUBOTICS_ROBOT_TYPE is superseded here too, R3) even though cloud-only
    never starts physical_ai_server.
    """
    domain_id = _resolve_ros_domain_id()
    preserved = _read_unmanaged_lines(output_path)
    lines = [
        "# Cloud-only mode — no robot hardware connected.",
        'FOLLOWER_PORT=""',
        'LEADER_PORT=""',
        'CAMERA_DEVICE_1=""',
        'CAMERA_NAME_1="gripper"',
        'CAMERA_DEVICE_2=""',
        'CAMERA_NAME_2="scene"',
        f"ROS_DOMAIN_ID={domain_id}",
        f"REGISTRY={REGISTRY}",
        f"REGISTRY_FALLBACK={REGISTRY_FALLBACK}",
        f"IMAGE_TAG={IMAGE_TAG}",
        f"EDUBOTICS_ROBOT_TYPE={robot_type}",
    ]
    from .constants import cameras_use_native_bridge
    if cameras_use_native_bridge() and not _has_camera_source(preserved):
        lines.append("EDUBOTICS_CAMERA_SOURCE=native_bridge")
    if phone_camera:
        lines.append(_phone_camera_names_line())
    if preserved:
        lines.append("")
        lines.append(_PRESERVE_MARKER)
        lines.extend(preserved)
    lines.append("")
    content = "\n".join(lines)
    _atomic_write(output_path, content)
    return content
