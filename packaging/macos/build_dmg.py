"""Package a verified macOS .app archive into a consistent compressed UDZO .dmg."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


APP_NAME = "KPW6 PDF Studio.app"


def _run_hdiutil_with_retry(cmd: list[str], cleanup_path: Path | None = None, attempts: int = 6) -> None:
    """Retry transient macOS hdiutil 'Resource busy' errors caused by Spotlight/XProtect."""
    last_err: subprocess.CalledProcessError | None = None
    for attempt in range(1, attempts + 1):
        if cleanup_path is not None and cleanup_path.exists():
            cleanup_path.unlink()
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode == 0:
            return
        last_err = subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if attempt < attempts and ("resource busy" in combined.lower() or "couldn't unmount" in combined.lower()):
            time.sleep(2.0 * attempt)
            continue
        sys.stderr.write(combined)
        raise last_err
    assert last_err is not None
    raise last_err


def build_dmg_from_app(app_path: Path, dmg_output: Path, volume_name: str = "KPW6 PDF Studio") -> Path:
    app_path, dmg_output = Path(app_path).resolve(), Path(dmg_output).resolve()
    if not app_path.is_dir() or app_path.name != APP_NAME:
        raise FileNotFoundError(f"Expected {APP_NAME} directory, got: {app_path}")
    if dmg_output.exists():
        raise FileExistsError(f"Use a new DMG output path: {dmg_output}")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app_path)], check=True)
    dmg_output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="kpw6-dmg-stage-") as stage_raw:
        stage = Path(stage_raw)
        staged_app = stage / APP_NAME
        subprocess.run(["ditto", "--norsrc", str(app_path), str(staged_app)], check=True)
        subprocess.run(["xattr", "-cr", str(staged_app)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(staged_app)], check=True)
        (stage / "Applications").symlink_to("/Applications")

        _run_hdiutil_with_retry([
            "hdiutil", "create",
            "-volname", volume_name,
            "-srcfolder", str(stage),
            "-fs", "HFS+",
            "-format", "UDZO",
            "-imagekey", "zlib-level=9",
            str(dmg_output),
        ], cleanup_path=dmg_output)

    with tempfile.TemporaryDirectory(prefix="kpw6-dmg-verify-") as mnt_raw:
        mnt = Path(mnt_raw)
        _run_hdiutil_with_retry([
            "hdiutil", "attach", str(dmg_output),
            "-readonly", "-nobrowse", "-noverify", "-noautoopen",
            "-mountpoint", str(mnt),
        ])
        try:
            mounted_app = mnt / APP_NAME
            if not mounted_app.is_dir():
                raise RuntimeError(f"DMG missing {APP_NAME}")
            if not (mnt / "Applications").is_symlink():
                raise RuntimeError("DMG missing Applications symlink")
            subprocess.run(["codesign", "--verify", "--deep", "--strict", str(mounted_app)], check=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mnt), "-force"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return dmg_output


def build_dmg_from_zip(zip_path: Path, dmg_output: Path, volume_name: str = "KPW6 PDF Studio") -> Path:
    zip_path = Path(zip_path).resolve()
    if not zip_path.is_file():
        raise FileNotFoundError(f"Missing macOS app archive: {zip_path}")
    with tempfile.TemporaryDirectory(prefix="kpw6-dmg-unpack-") as unpack_raw:
        subprocess.run(["ditto", "-x", "-k", str(zip_path), unpack_raw], check=True)
        return build_dmg_from_app(Path(unpack_raw) / APP_NAME, dmg_output, volume_name=volume_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--app-zip", type=Path, help="Verified .zip produced by build_macos.py")
    group.add_argument("--app", type=Path, help="Path to KPW6 PDF Studio.app")
    parser.add_argument("--output", type=Path, required=True, help="Destination .dmg path")
    args = parser.parse_args()
    if sys.platform != "darwin" or not shutil.which("hdiutil"):
        parser.error("Building a macOS DMG requires macOS with hdiutil")
    if args.app_zip:
        out = build_dmg_from_zip(args.app_zip, args.output)
    else:
        out = build_dmg_from_app(args.app, args.output)
    print(f"Created verified DMG: {out}")


if __name__ == "__main__":
    main()
