# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for ROBOTIS AI Setup GUI
#
# Build with:
#   cd robotis_ai_setup/gui
#   pip install pyinstaller
#   pyinstaller build.spec

import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files

block_cipher = None

# Collect all WebView2 / pywebview / pythonnet native DLLs that PyInstaller's
# auto-discovery might miss when the EXE is shipped to a machine without
# Python installed. pywebview ships its own WinForms interop DLLs in
# site-packages/webview/lib/ and pythonnet ships the CLR bridge.
pywebview_binaries = collect_dynamic_libs('webview')
pythonnet_binaries = collect_dynamic_libs('clr_loader') + collect_dynamic_libs('pythonnet')
pywebview_datas = collect_data_files('webview', includes=['lib/*'])

# OpenCV (cv2) + numpy power the native camera bridge (camera_bridge.py /
# win_camera.py). PyInstaller ships hooks for both, but collect their native
# DLLs explicitly so the camera capture works on a clean student PC with no
# Python install. cv2's DirectShow capture also needs the MSVC runtime, which
# is present on Win10/11 by default.
cv2_binaries = collect_dynamic_libs('cv2')

# Bundle the GUI's PowerShell helpers (e.g. new_phone_cert.ps1 — mints the
# self-signed cert for the phone-camera HTTPS receiver) into the payload under
# `scripts/` so phone_cert._find_helper_script() resolves them via sys._MEIPASS
# on a frozen build. The .ps1 files carry a UTF-8 BOM (powershell-encoding CI
# gate); PyInstaller copies them byte-for-byte.
ps_scripts = (
    [(os.path.join('app', 'scripts'), 'scripts')]
    if os.path.isdir(os.path.join('app', 'scripts')) else []
)

# Bundle the repo-root VERSION file at the dist root so
# constants._read_version_file() resolves the REAL product version in a frozen
# .exe (via sys._MEIPASS) instead of falling back to the baked-in literal. A
# stale fallback reads LOW and re-offers the same forced update forever — this
# closes that update-loop hazard at the source. This spec runs from
# robotis_ai_setup/gui/, so the repo root is two levels up.
_version_src = os.path.join('..', '..', 'VERSION')
version_datas = [(_version_src, '.')] if os.path.isfile(_version_src) else []

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=pywebview_binaries + pythonnet_binaries + cv2_binaries,
    datas=([
        ('assets', 'assets'),
    ] if os.path.isdir('assets') and os.listdir('assets') != ['.gitkeep'] else [])
        + ps_scripts + pywebview_datas + version_datas,
    hiddenimports=[
        'tkinter',
        'tkinter.ttk',
        'tkinter.scrolledtext',
        'tkinter.messagebox',
        'webview',
        'webview.platforms.edgechromium',
        'clr',
        'clr_loader',
        'clr_loader.netfx',
        'cv2',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EduBotics',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window — GUI only
    disable_windowed_traceback=False,
    icon='assets/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='EduBotics',
)
