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

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=pywebview_binaries + pythonnet_binaries,
    datas=([
        ('assets', 'assets'),
    ] if os.path.isdir('assets') and os.listdir('assets') != ['.gitkeep'] else []) + pywebview_datas,
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
