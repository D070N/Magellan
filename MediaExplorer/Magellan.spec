# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('../MainIcon.ico', '.')]
datas += [(f'../Alternative Icons/{name}.png', 'Alternative Icons') for name in ('eye', 'list', 'lastmod', 'types', 'size', 'back', 'clearselect', 'createfavoritegroup', 'organizationmode', 'undo', 'image', 'sidebyside')]
binaries = []
hiddenimports = []
tmp_ret = collect_all('imageio_ffmpeg')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pillow_jxl')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# The host process PATH can expose unrelated ICU DLLs (for example, Poppler's
# ICU 78 build). Qt's Windows runtime resolves these DLLs by name, so shipping
# an arbitrary PATH copy can prevent QtCore from loading on another machine.
# Let Windows use the system ICU runtime instead of bundling those unrelated
# host dependencies.
a.binaries = [
    entry for entry in a.binaries
    if not entry[0].lower().startswith('icu')
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Magellan',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['../MainIcon.ico'],
)
