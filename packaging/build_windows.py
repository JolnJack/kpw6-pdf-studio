"""Build and assemble the Windows onedir distribution from a clean checkout."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from release_support import FROZEN_EXCLUDES, prune_frozen_backend, write_release_metadata


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "KindleKPW6Studio"
BATCH_NAME = "双击启动_Kindle精排大师.bat"


def assemble_distribution(backend: Path, destination: Path, source_root: Path = ROOT):
    backend, destination = Path(backend), Path(destination)
    if not (backend / f"{APP_NAME}.exe").is_file():
        raise FileNotFoundError(f"Missing Windows executable: {backend / (APP_NAME + '.exe')}")
    if destination.exists():
        raise FileExistsError(f"Use an empty output directory: {destination}")
    destination.mkdir(parents=True)
    shutil.copytree(backend, destination / APP_NAME, symlinks=True)
    prune_frozen_backend(destination / APP_NAME)
    batch = f'@echo off\r\nchcp 65001 >nul\r\nstart "" "%~dp0{APP_NAME}\\{APP_NAME}.exe"\r\n'
    (destination / BATCH_NAME).write_bytes(batch.encode("ascii"))
    write_release_metadata(source_root, destination, backend_dir=destination / APP_NAME)
    return destination


def archive_distribution(release_dir: Path, archive_path: Path) -> Path:
    release_dir, archive_path = Path(release_dir), Path(archive_path)
    if archive_path.exists():
        raise FileExistsError(f"Use a new output archive: {archive_path}")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(release_dir.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(release_dir).as_posix())
    return archive_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "Release")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build" / "windows")
    parser.add_argument("--zip-output", type=Path, default=None)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("A Windows executable must be built on Windows")
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
               "--name", APP_NAME, "--icon", str(ROOT / "packaging" / "app_icon.ico"),
               "--distpath", str(work / "dist"), "--workpath", str(work / "build"), "--specpath", str(work)]
    for module in FROZEN_EXCLUDES:
        command += ["--exclude-module", module]
    for module in ("pdf_service", "pdf_worker", "conversion_runner"):
        command += ["--hidden-import", module]
    subprocess.run(command + [str(ROOT / "app.py")], cwd=ROOT, check=True)
    release = assemble_distribution(work / "dist" / APP_NAME, args.output_dir.resolve())
    if args.zip_output:
        zip_out = archive_distribution(release, args.zip_output.resolve())
        print(f"Created Windows ZIP archive: {zip_out}")
    print(f"Created Windows distribution: {release}")


if __name__ == "__main__":
    main()
