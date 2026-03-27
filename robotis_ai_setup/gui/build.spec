# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for ROBOTIS AI Setup GUI
#
# Build with:
#   cd robotis_ai_setup/gui
#   pip install pyinstaller
#   pyinstaller build.spec

import os

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets', 'assets'),
    ] if os.path.isdir('assets') and os.listdir('assets') != ['.gitkeep'] else [],
    hiddenimports=[
        'tkinter',
        'tkinter.ttk',
        'tkinter.scrolledtext',
        'tkinter.messagebox',
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
    name='RobotisAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window — GUI only
    disable_windowed_traceback=False,
    # Uncomment when icon is available:
    # icon='assets/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='RobotisAI',
)
