# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir / 无控制台）。

产物结构：
    黑盒语音群聊机器人/
        黑盒语音群聊机器人.exe      ← 双击这个
        data/                       ← 第一次运行后出现，人设和记忆都在这
        _internal/                  ← 运行时文件，不要动

用 onedir 而不是单文件：单文件每次启动都要解压到临时目录，
慢、杀软误报也更严重，而绿色版本来就是整个文件夹搬走。
"""

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['bot', 'monitor', 'websocket'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest', 'pdb'],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='黑盒语音群聊机器人',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # 不弹黑框，日志写 data/log/
    disable_windowed_traceback=False,
    icon='app.ico',
    version='version_info.txt',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='黑盒语音群聊机器人',
)
