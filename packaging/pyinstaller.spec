# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_submodules

spec_dir = Path(SPECPATH).resolve()
project_root = spec_dir.parent
src_root = project_root / 'src'
sys.path.insert(0, str(spec_dir))
sys.path.insert(0, str(src_root))

from release_support import FROZEN_EXCLUDES, prune_frozen_backend, prune_pyinstaller_toc, write_release_metadata

a = Analysis(
    [str(src_root / 'kpw6_studio' / '__main__.py')],
    pathex=[str(src_root)],
    binaries=[],
    datas=[(str(src_root / 'kpw6_studio' / 'web'), 'kpw6_studio/web')],
    hiddenimports=collect_submodules('kpw6_studio'),
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
