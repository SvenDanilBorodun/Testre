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


def check_rosbridge(host: str = "localhost", port: int = 9090) -> bool:
    """Check if rosbridge websocket port is accepting connections."""
    import socket
    try:
        sock = socket.create_connection((host, port), timeout=3)
        sock.close()
        return True
    except (OSError, TimeoutError):
        return False


def check_video_server(host: str = "localhost", port: int = 8080) -> bool:
    """Check if web_video_server is responding."""
    try:
        url = f"http://{host}:{port}/"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


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
