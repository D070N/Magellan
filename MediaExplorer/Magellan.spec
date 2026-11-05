# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import importlib.util

datas = [(f'../Alternative Icons/{name}', 'Alternative Icons') for name in ('MainIconNew.ico', 'MainIconNewRAR.ico', 'MainIconNewRead.ico')]
datas += [('../Alternative Icons/MainIconWelcome.png','Alternative Icons')]
datas += [(f'../Alternative Icons/{name}.png', 'Alternative Icons') for name in ('eye', 'list', 'lastmod', 'types', 'size', 'back', 'clearselect', 'createfavoritegroup', 'organizationmode', 'undo', 'image', 'sidebyside', 'compare', 'personalorder', 'imagemode', 'readmode', 'readmodevtc')]
pdfium_data, pdfium_binaries, pdfium_imports = collect_all('pypdfium2_raw')
datas += pdfium_data
binaries = pdfium_binaries
hiddenimports = pdfium_imports
# Keep FFmpeg compressed so Explorer launches stay lightweight. Video
# thumbnails expand the helper on first use or use FFmpeg available on PATH.
tmp_ret = collect_all('pillow_jxl')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
if importlib.util.find_spec('unrar'):
    tmp_ret = collect_all('unrar')
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
    # unrar2-cffi imports its compiled backend dynamically; PyInstaller cannot
    # discover this extension from the package's runtime import alone.
    hiddenimports += ['_cffi_backend']


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow lists NumPy as an optional array-conversion feature; Magellan does
    # not use that API. Excluding it avoids bundling NumPy/OpenBLAS at startup.
    excludes=['imageio_ffmpeg', 'numpy'],
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
    [],
    exclude_binaries=True,
    name='Magellan',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    icon=['../Alternative Icons/MainIconNew.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Magellan',
)
