"""Health checking for containers and web UI."""

import time
import urllib.request
import urllib.error

from .constants import PORT_WEB_UI, WEB_UI_POLL_INTERVAL, WEB_UI_POLL_TIMEOUT


def check_web_ui(host: str = "localhost", port: int = PORT_WEB_UI) -> bool:
    """Check if the web UI is responding."""
    try:
        url = f"http://{host}:{port}/"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


# Both robot transports are probed THROUGH the physical_ai_manager nginx
# same-origin proxy, because as of 2026-08-06 they have no host port publish
# at all (see docker-compose.yml): rosbridge is unauthenticated, and a
# cross-origin WebSocket handshake gets no CORS preflight, so any page open in
# the student's browser could have driven the arm. The proxy paths below are
# the ONLY route in, and nginx gates /rosbridge on an Origin allowlist that
# deliberately admits an ABSENT Origin (a browser cannot omit it on a WS
# handshake) — which is exactly why these two non-browser probes still work.
_PROXY_ROSBRIDGE_PATH = "/rosbridge"
_PROXY_VIDEO_PATH = "/video/"

# An nginx 502/503/504 means "the upstream did not answer" — the robot
# container is down. ANY other HTTP status is proof the upstream replied and
# is therefore alive, including 4xx: a plain GET to rosbridge's WebSocket
# endpoint answers 400 ('Can "Upgrade" only to "WebSocket".'), which is a
# LIVENESS signal, not a failure.
_UPSTREAM_DOWN_STATUSES = frozenset({502, 503, 504})


def _upstream_alive(path: str, host: str, port: int, timeout: int = 3) -> bool:
    """True when the proxied upstream itself answered.

    Deliberately NOT `status == 200`: this must never become a stub that
    always returns True, and it must not report a live robot as dead just
    because the endpoint answers 4xx to a non-WebSocket GET.
    """
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status not in _UPSTREAM_DOWN_STATUSES
    except urllib.error.HTTPError as exc:
        # urllib raises on >=400, so the rosbridge 400 lands here — that is
        # the alive case. Only nginx's own gateway errors mean down.
        # HTTPError IS a response object and holds an open socket; these
        # probes run in a poll loop, so close it explicitly rather than
        # leaving it to the GC.
        try:
            exc.close()
        except Exception:  # noqa: BLE001
            pass
        return exc.code not in _UPSTREAM_DOWN_STATUSES
    except (urllib.error.URLError, OSError, TimeoutError):
        # nginx itself unreachable (manager container down / port closed).
        return False


def check_rosbridge(host: str = "localhost", port: int = PORT_WEB_UI) -> bool:
    """Check if rosbridge is alive behind the manager's same-origin proxy.

    Stronger than the old raw TCP connect to the published :9090: a docker
    port-publish accepts a TCP connection as soon as docker-proxy binds, even
    when the process inside the container is dead. Going through the proxy
    means only an answer FROM ROSBRIDGE counts.
    """
    return _upstream_alive(_PROXY_ROSBRIDGE_PATH, host, port)


def check_video_server(host: str = "localhost", port: int = PORT_WEB_UI) -> bool:
    """Check if web_video_server is alive behind the manager's proxy."""
    return _upstream_alive(_PROXY_VIDEO_PATH, host, port)


def wait_for_web_ui(
    timeout: int = WEB_UI_POLL_TIMEOUT,
    interval: int = WEB_UI_POLL_INTERVAL,
    callback=None,
) -> bool:
    """Poll until web UI is ready or timeout.

    Args:
        timeout: Max seconds to wait.
        interval: Seconds between polls.
        callback: Optional function called with (elapsed, timeout) for progress.

    Returns:
        True if web UI became available.
    """
    start = time.time()
    while time.time() - start < timeout:
        if check_web_ui():
            return True
        elapsed = int(time.time() - start)
        if callback:
            callback(elapsed, timeout)
        time.sleep(interval)
    return False


def full_health_check() -> dict[str, bool]:
    """Run all health checks and return results."""
    return {
        "web_ui": check_web_ui(),
        "rosbridge": check_rosbridge(),
        "video_server": check_video_server(),
    }
