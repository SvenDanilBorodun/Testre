"""Phone-as-3rd-camera HTTPS receiver (Orange Pi agent).

A school phone's browser captures video via getUserMedia and POSTs JPEG frames
to this small HTTPS server. Ported from ``gui/app/phone_camera.py`` — the
receiver, the frame slot, the German capture page and the upload-validation are
byte-for-byte the GUI's — with two Pi-specific changes:

  1. **Cert minting uses ``openssl``, not PowerShell.** The GUI shells out to
     ``new_phone_cert.ps1`` (Windows New-SelfSignedCertificate). The Pi mints
     the same shape of self-signed cert (RSA 2048, CN=edubotics-phone-cam,
     825-day validity) with ``openssl req -x509`` — see :func:`ensure_cert`.
  2. **⚠ PREVIEW-ONLY BACKEND — the phone is NOT wired into ROS on the Pi
     (OPEN, owner decision pending).** On the Windows student host the GUI
     forwards the newest phone frame over the native capture bridge
     (127.0.0.1:5557) to ``camera_ingest_node.py`` as cam_id 2. On the Pi the
     camera path is in-container ``usb_cam``, which has NO ``camera_ingest_node``
     consumer (``entrypoint_omx.sh`` starts it only in the ``native_bridge``
     branch), so there is nothing to forward phone frames to. This module
     therefore ships the receiver + the :class:`LatestFrameSlot` ONLY — frames
     land in the slot (usable for a live preview ``<img>``) but are NEVER
     republished on a ROS topic. Wiring the phone into ROS on ``usb_cam`` (an
     agent-side sender + the ingest node running beside ``usb_cam``) is the OPEN
     item in the deploy plan (§6/§11); P3 will NOT surface phone UI until it is
     decided. Even on Windows the phone is preview/live-only (recording +
     React-grid listing are the parked ``camera_topic_list`` non-goal).

All student-facing strings are German (Rule §1); code/comments/log lines are
English.

Config (``PORT_PHONE_HTTPS`` 8444, ``PHONE_MAX_FRAME_BYTES`` 2 MiB,
``PHONE_CERT_DIR`` ``/etc/edubotics/phone-cert``) is sourced from
``pi_agent/constants.py`` (the single source of truth) and re-exported at module
level so callers/tests can read ``phone_camera.PORT_PHONE_HTTPS`` directly;
:class:`PhoneCameraServer` still accepts an explicit ``port`` override.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .constants import (
    PHONE_CERT_DIR,
    PHONE_MAX_FRAME_BYTES,
    PORT_PHONE_HTTPS,
)

# DNS/CN + validity of the minted self-signed cert (matches new_phone_cert.ps1's
# defaults). Cert-minting details, not shared config — kept local.
_PHONE_CERT_CN = "edubotics-phone-cam"
_PHONE_CERT_DAYS = 825  # under the common 825-day max-validity limit


# ----- pure, unit-testable frame-upload validation -----

def validate_frame_upload(content_length, max_bytes=None):
    """Validate a POST /frame request from its Content-Length alone.

    Pure function (no socket) so the accept/reject policy is unit-testable.

    Returns ``(ok, http_status, reason_de)``:
      * (True, 204, "")                — accept, read the body, store it
      * (False, 411, <de>)            — Content-Length missing/invalid (we will
                                         not read an unbounded body)
      * (False, 413, <de>)            — body too large
    """
    if max_bytes is None:
        max_bytes = PHONE_MAX_FRAME_BYTES
    if content_length is None:
        return False, 411, "Länge fehlt (Content-Length erforderlich)."
    try:
        length = int(content_length)
    except (TypeError, ValueError):
        return False, 411, "Ungültige Content-Length."
    if length <= 0:
        return False, 411, "Leerer Frame."
    if length > max_bytes:
        return False, 413, f"Frame zu groß ({length} > {max_bytes} Bytes)."
    return True, 204, ""


class LatestFrameSlot:
    """Thread-safe single-frame slot: newest JPEG wins.

    The HTTP handler calls :meth:`set`; a preview reader calls :meth:`get` /
    :meth:`fresh_within`. A monotonic sequence lets a reader detect a genuinely
    new frame (so it never re-sends the same bytes), and the capture timestamp
    (``time.time_ns()``) feeds the staleness gate.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._jpeg = None          # bytes | None
        self._capture_ns = 0       # host time.time_ns() of the most recent frame
        self._monotonic = 0.0      # time.monotonic() of the most recent frame
        self._seq = 0              # monotonic counter; bumps on every set()

    def set(self, jpeg_bytes, capture_ns=None):
        """Store the newest frame. ``capture_ns`` defaults to the host clock."""
        if capture_ns is None:
            capture_ns = time.time_ns()
        with self._lock:
            self._jpeg = jpeg_bytes
            self._capture_ns = capture_ns
            self._monotonic = time.monotonic()
            self._seq += 1

    def get(self):
        """Return ``(jpeg_bytes, capture_ns, seq)`` or ``None`` if empty."""
        with self._lock:
            if self._jpeg is None:
                return None
            return self._jpeg, self._capture_ns, self._seq

    def fresh_within(self, max_age_s):
        """True iff a frame exists AND arrived within ``max_age_s`` seconds.

        The staleness gate: a phone that stopped posting (closed tab, screen
        lock, walked away) must NOT keep the last frame alive on a preview
        forever.
        """
        with self._lock:
            if self._jpeg is None:
                return False
            return (time.monotonic() - self._monotonic) <= max_age_s

    def seq(self):
        with self._lock:
            return self._seq


# ----- the capture page (single file, inline JS) -----

# German UI (Rule §1). Targets Android Chrome + iOS Safari: secure-context +
# getUserMedia(environment) + muted autoplay. The page draws each frame to a
# canvas, encodes JPEG at quality 0.7 (~10 fps), and POSTs it to /frame.
_CAPTURE_PAGE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>EduBotics — Handy-Kamera</title>
<style>
  html,body{margin:0;background:#111;color:#eee;font-family:Segoe UI,Roboto,sans-serif;}
  #wrap{display:flex;flex-direction:column;align-items:center;padding:12px;gap:10px;}
  video{width:100%;max-width:480px;border-radius:8px;background:#000;}
  #status{font-size:1.1rem;text-align:center;min-height:2.4rem;}
  .ok{color:#7CFC9B;} .err{color:#FF8080;} .warn{color:#FFD27C;}
  button{font-size:1rem;padding:10px 18px;border:0;border-radius:8px;background:#2D7;color:#012;}
  small{color:#999;text-align:center;max-width:480px;}
</style>
</head>
<body>
<div id="wrap">
  <h2>EduBotics — Handy als Kamera</h2>
  <video id="v" autoplay muted playsinline></video>
  <div id="status">Kamera wird gestartet…</div>
  <button id="start">Kamera starten</button>
  <small>
    Halte das Handy ruhig und richte es auf die Szene. Falls eine
    Sicherheitswarnung erscheint: „Erweitert" → „Trotzdem fortfahren" (das
    Zertifikat ist selbst signiert, nur für dein Klassennetz). Der Bildschirm
    darf nicht gesperrt werden, sonst stoppt die Kamera.
  </small>
</div>
<script>
const statusEl = document.getElementById('status');
const video = document.getElementById('v');
const startBtn = document.getElementById('start');
let sending = false, lastSentOk = 0, frames = 0;
function setStatus(msg, cls){ statusEl.textContent = msg; statusEl.className = cls || ''; }

const canvas = document.createElement('canvas');
const ctx = canvas.getContext('2d');

async function postFrame(){
  if (sending || video.readyState < 2 || !video.videoWidth) return;
  sending = true;
  try {
    canvas.width = video.videoWidth; canvas.height = video.videoHeight;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', 0.7));
    if (blob) {
      const res = await fetch('/frame', {method:'POST', body: blob,
        headers: {'Content-Type':'image/jpeg'}});
      if (res.status === 204) { lastSentOk = Date.now(); frames++; }
      else { setStatus('Server lehnte Frame ab (HTTP ' + res.status + ').', 'warn'); }
    }
  } catch(e) {
    setStatus('Verbindung zum PC verloren — WLAN prüfen.', 'err');
  } finally { sending = false; }
}

async function start(){
  setStatus('Kamerazugriff wird angefragt…');
  if (!window.isSecureContext) {
    setStatus('Unsicherer Kontext — diese Seite muss über HTTPS geöffnet werden.', 'err');
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setStatus('Dieser Browser unterstützt keine Kamera (getUserMedia fehlt).', 'err');
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' }, width: {ideal: 640}, height: {ideal: 480} },
      audio: false
    });
    video.srcObject = stream;
    await video.play();
    startBtn.style.display = 'none';
    setStatus('Kamera aktiv — Bilder werden an den Roboter gesendet.', 'ok');
    setInterval(postFrame, 100);  // ~10 fps
    setInterval(() => {
      if (Date.now() - lastSentOk > 3000)
        setStatus('Keine Bestätigung vom PC — WLAN/Verbindung prüfen.', 'warn');
      else
        setStatus('Kamera aktiv (' + frames + ' Bilder gesendet).', 'ok');
    }, 1500);
  } catch(err) {
    if (err && (err.name === 'NotAllowedError' || err.name === 'SecurityError'))
      setStatus('Kamerazugriff verweigert — bitte in den Browser-Einstellungen erlauben.', 'err');
    else if (err && err.name === 'NotFoundError')
      setStatus('Keine Kamera gefunden.', 'err');
    else
      setStatus('Kamera konnte nicht gestartet werden: ' + (err && err.name || err), 'err');
  }
}
startBtn.addEventListener('click', start);
// Auto-attempt; iOS Safari may require the button tap (user gesture).
start();
</script>
</body>
</html>
"""

_CAPTURE_PAGE_BYTES = _CAPTURE_PAGE.encode("utf-8")


class _PhoneRequestHandler(BaseHTTPRequestHandler):
    """One handler per request. The owning server sets ``frame_slot`` +
    ``on_log`` as class-style attributes via a small subclass factory."""

    # Set by _make_handler. Defaults keep the type checkers + any stray call
    # safe even before binding.
    frame_slot: LatestFrameSlot = None  # type: ignore[assignment]
    on_log = None
    server_version = "EduBoticsPhoneCam/1.0"

    # Silence the default stderr access log; route to on_log if provided.
    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        if callable(self.on_log):
            try:
                self.on_log("Handy-Kamera: " + (fmt % args))
            except Exception:  # noqa: BLE001
                pass

    def _send_text(self, status, body, content_type="text/plain; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # No caching of the capture page so a restart serves fresh JS.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - stdlib signature
        if self.path in ("/", "/index.html"):
            self._send_text(200, _CAPTURE_PAGE_BYTES, "text/html; charset=utf-8")
            return
        if self.path == "/health":
            self._send_text(200, "OK")
            return
        self._send_text(404, "Nicht gefunden.")

    def do_POST(self):  # noqa: N802 - stdlib signature
        if self.path != "/frame":
            self._send_text(404, "Nicht gefunden.")
            return
        ok, status, reason_de = validate_frame_upload(
            self.headers.get("Content-Length"))
        if not ok:
            self._send_text(status, reason_de)
            return
        length = int(self.headers.get("Content-Length"))
        try:
            body = self.rfile.read(length)
        except (OSError, ValueError):
            self._send_text(400, "Frame konnte nicht gelesen werden.")
            return
        if not body:
            self._send_text(411, "Leerer Frame.")
            return
        if self.frame_slot is not None:
            self.frame_slot.set(body)
        # 204 No Content — nothing to send back, keeps the POST loop tight.
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _make_handler(frame_slot, on_log):
    """Bind the slot + logger onto a fresh handler subclass (one per server)."""
    return type(
        "_BoundPhoneRequestHandler",
        (_PhoneRequestHandler,),
        {"frame_slot": frame_slot, "on_log": staticmethod(on_log) if callable(on_log) else None},
    )


# ----- self-signed cert minting (openssl, replaces new_phone_cert.ps1) -----

class PhoneCertError(RuntimeError):
    """Cert could not be minted or located (German message for the wizard)."""


def _pems_usable(cert_path: str, key_path: str) -> bool:
    """True iff both PEMs exist, are non-empty, and look like PEM (BEGIN + the
    expected body marker).

    A cheap structural check — enough to avoid re-minting on every run without
    pulling in a crypto library to fully parse them. ``ssl.load_cert_chain``
    does the real validation at server start; a malformed pair surfaces there.
    ``openssl req -x509 -nodes`` writes a PKCS#8 key ("PRIVATE KEY"); the GUI's
    PowerShell path wrote PKCS#1 ("RSA PRIVATE KEY") — both carry the substring
    "PRIVATE KEY", so this check accepts either.
    """
    for path, needle in ((cert_path, "CERTIFICATE"), (key_path, "PRIVATE KEY")):
        try:
            with open(path, encoding="ascii", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return False
        if "-----BEGIN" not in text or needle not in text:
            return False
    return True


def cert_paths(cert_dir: str | None = None) -> tuple[str, str]:
    """Return the ``(cert.pem, key.pem)`` paths under the phone-cert dir."""
    d = cert_dir or PHONE_CERT_DIR
    return os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")


def ensure_cert(cert_dir: str | None = None) -> tuple[str, str]:
    """Return ``(cert_path, key_path)``, minting the cert with ``openssl`` if
    needed.

    Reuses an existing valid PEM pair. Otherwise runs
    ``openssl req -x509 -newkey rsa:2048 -nodes`` (no passphrase so
    ``ssl.load_cert_chain`` can read the key) with the same subject / validity
    as the GUI's ``new_phone_cert.ps1``. Raises :class:`PhoneCertError` (German
    message) on any failure so the caller can surface it and skip the phone
    receiver without aborting the agent.
    """
    d = cert_dir or PHONE_CERT_DIR
    cert_path, key_path = cert_paths(d)

    if _pems_usable(cert_path, key_path):
        return cert_path, key_path

    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:
        raise PhoneCertError(
            f"Zertifikat-Ordner konnte nicht angelegt werden: {exc}"
        ) from exc

    # Self-signed, RSA 2048, CN + SAN edubotics-phone-cam, 825-day validity.
    # -addext writes a subjectAltName so browsers that require a SAN don't
    # reject the CN-only cert. -nodes → unencrypted key (load_cert_chain).
    argv = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
        "-days", str(_PHONE_CERT_DAYS), "-nodes",
        "-keyout", key_path, "-out", cert_path,
        "-subj", f"/CN={_PHONE_CERT_CN}",
        "-addext", f"subjectAltName=DNS:{_PHONE_CERT_CN}",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise PhoneCertError("openssl wurde nicht gefunden.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PhoneCertError("Zertifikat-Erstellung hat zu lange gedauert.") from exc

    if result.returncode != 0 or not _pems_usable(cert_path, key_path):
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "unbekannter Fehler"
        raise PhoneCertError(
            f"Zertifikat konnte nicht erstellt werden: {tail}"
        )

    # Pin the private key to 0600 regardless of the ambient umask — relying on
    # the systemd unit's UMask alone leaves the key world-readable when the
    # agent (or this helper) is run manually on a bench/debug shell. openssl
    # itself writes the key with the process umask.
    try:
        os.chmod(key_path, 0o600)
    except OSError as exc:
        raise PhoneCertError(
            f"Schlüssel-Berechtigungen konnten nicht gesetzt werden: {exc}"
        ) from exc

    return cert_path, key_path


class PhoneCameraServer:
    """HTTPS receiver for phone JPEG frames, run in an agent-owned daemon thread.

    Usage:
        srv = PhoneCameraServer(cert_path, key_path, slot, port=8444, log=agent._log)
        srv.start()        # binds + serves in a background thread
        ...                # slot fills as the phone posts
        srv.shutdown()     # stops + closes the socket

    On the Pi the slot is preview-only (no ROS republish — see the module
    docstring's OPEN item). Bound 0.0.0.0 by design: the phone must reach it
    over the LAN, and the body-size gate is the only admission control.
    """

    def __init__(self, cert_path, key_path, frame_slot=None, port=None,
                 host="0.0.0.0", log=None):
        self.cert_path = cert_path
        self.key_path = key_path
        self.frame_slot = frame_slot or LatestFrameSlot()
        self.port = int(port) if port else PORT_PHONE_HTTPS
        self.host = host
        self._log = log
        self._httpd = None
        self._thread = None

    def _build_ssl_context(self):
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
        return ctx

    def start(self):
        """Bind the HTTPS socket and serve in a daemon thread. Raises on a bind
        failure (e.g. port in use) so the caller can surface a German error."""
        handler = _make_handler(self.frame_slot, self._log)
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        httpd.daemon_threads = True
        try:
            ctx = self._build_ssl_context()
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        except Exception:
            # Don't leak the listening socket if TLS setup fails.
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            raise
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="phone-cam-https", daemon=True)
        self._thread.start()
        return self

    def shutdown(self, join_timeout=3.0):
        """Stop serving and close the socket. Best-effort + idempotent."""
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.ident is not None:
            thread.join(timeout=join_timeout)
