# -*- mode: python ; coding: utf-8 -*-
# End-user window. Same engine modules as egg_hatch.spec. No console.

a = Analysis(
    ["src/game/eggs/user_main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[
        "src.game.eggs",
        "src.game.eggs.user_ui",
        "src.game.eggs.user_text",
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
    runtime_hooks=["src/game/eggs/win_stdio_hook.py"],
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
    name="AolaEggHatch-user",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
)
