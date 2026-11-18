# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

project_root = Path(SPECPATH)
sys.path.insert(0, str(project_root / 'packaging'))
from release_support import FROZEN_EXCLUDES, prune_frozen_backend, prune_pyinstaller_toc, write_release_metadata

a = Analysis(
    [str(project_root / 'app.py')],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=['pdf_service', 'pdf_worker', 'conversion_runner'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(FROZEN_EXCLUDES),
    noarchive=False,
    optimize=0,
)
a.binaries = prune_pyinstaller_toc(a.binaries)
a.datas = prune_pyinstaller_toc(a.datas)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KPW6StudioServer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='KPW6StudioServer',
)
out_server_dir = Path(DISTPATH) / 'KPW6StudioServer'
prune_frozen_backend(out_server_dir)
write_release_metadata(project_root, out_server_dir, backend_dir=out_server_dir)
