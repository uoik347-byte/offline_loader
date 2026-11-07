# -*- mode: python ; coding: utf-8 -*-
# Build on 64-bit Windows with Python 3.12. PyInstaller does not cross-compile.

a = Analysis(
    ["src/game/eggs/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[
        "src.game.eggs",
        "src.game.eggs.ui",
        "src.game.eggs.engine",
        "src.game.eggs.session",
        "src.game.eggs.transport",
        "src.accounts.login",
        "src.accounts.session",
        "src.network.socket_client",
        "src.network.context",
        "src.protocol.amf3",
        "src.protocol.message_codec",
        "src.protocol.encrypt",
        "aiohttp",
        "aiohttp_socks",
        "websockets",
        "python_socks",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "numpy", "PIL", "pygame"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AolaEggHatch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
