"""EduBotics Pi-Agent — the native-Linux (Orange Pi 5 Pro) host brain.

This package is the Pi-side counterpart of the Windows GUI
(``robotis_ai_setup/gui/app/``): it owns the managed ``.env``, the Docker
Compose lifecycle, hardware scanning and the HTTP management API that the
browser-served React "System" window drives through the manager's
``/api/system`` reverse proxy.

It is a faithful port of the platform-neutral GUI brain merged with the
Jetson agent's systemd/scrubbed-env/arm64-digest skeleton, with every
Windows/WSL2 artifact shed (no usbipd, no ``wsl -d`` tunnelling, no
MSMF capture bridge, no WebView2, no PowerShell/UAC repair flows).

See ``docs/ORANGE_PI_DEPLOY_PLAN.md`` for the durable spec.
"""
