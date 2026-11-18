"""Build a native macOS .app with its backend, icon, terms, and matching source."""
from __future__ import annotations

import argparse
from importlib import metadata
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile

from release_support import RELEASE_METADATA_ITEMS, prune_frozen_backend, runtime_distributions, write_release_metadata


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "KPW6 PDF Studio.app"


def prepare_for_signing(app: Path):
    """Clear copied Finder/resource-fork metadata from this newly built app.

    Apple QA1940 forbids these attributes in a signed bundle. This operates
    only on the assembled output; source files and system apps are untouched.
    """
    app = Path(app)
    if not app.is_dir() or app.is_symlink() or app.suffix != '.app':
        raise ValueError('Expected the newly assembled .app directory')
    subprocess.run(['xattr', '-cr', str(app)], check=True)


def archive_app(app: Path, archive: Path):
    """Keep permissions/symlinks, without Finder/resource-fork sidecar data."""
    archive = Path(archive)
    if archive.exists():
        raise FileExistsError(f'Use a new output archive: {archive}')
    archive.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ditto', '-c', '-k', '--keepParent', '--norsrc', str(app), str(archive)], check=True)


def minimum_macos_version(distributions) -> str:
    versions = [(11, 0)]
    python_target = sysconfig.get_config_var("MACOSX_DEPLOYMENT_TARGET") or ""
    if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", python_target):
        versions.append(tuple(int(part) for part in python_target.split(".")[:2]))
    for dist in distributions:
        wheel = dist.read_text("WHEEL") or ""
        versions.extend((int(major), int(minor)) for major, minor in re.findall(r"macosx_(\d+)_(\d+)_", wheel))
    return ".".join(map(str, max(versions)))


def assemble_app(backend: Path, launcher: Path, app: Path, source_root: Path = ROOT,
                 icon: Path = ROOT / "packaging" / "AppIcon.icns", minimum_system: str = "11.0"):
    backend, launcher, app, icon = map(Path, (backend, launcher, app, icon))
    if not (backend / "KPW6StudioServer").is_file():
        raise FileNotFoundError(f"Missing frozen backend: {backend}")
    if not launcher.is_file() or not icon.is_file():
        raise FileNotFoundError("The compiled launcher and .icns icon are required")
    if app.exists():
        raise FileExistsError(f"Use an empty output directory: {app}")
    contents = app / "Contents"
    macos, resources = contents / "MacOS", contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    executable = macos / "KPW6StudioLauncher"
    shutil.copy2(launcher, executable)
    executable.chmod(executable.stat().st_mode | 0o111)
    shutil.copytree(backend, resources / "server", symlinks=True)
    prune_frozen_backend(resources / "server")
    for dup_name in RELEASE_METADATA_ITEMS:
        dup_path = resources / "server" / dup_name
        if dup_path.is_dir() and not dup_path.is_symlink():
            shutil.rmtree(dup_path)
        elif dup_path.exists():
            dup_path.unlink()
    server = resources / "server" / "KPW6StudioServer"
    server.chmod(server.stat().st_mode | 0o111)
    shutil.copy2(icon, resources / "AppIcon.icns")
    info = {"CFBundleName": "KPW6 PDF Studio", "CFBundleDisplayName": "KPW6 PDF Studio",
            "CFBundleIdentifier": "io.github.jolnjack.kpw6-pdf-studio", "CFBundleExecutable": executable.name,
            "CFBundlePackageType": "APPL", "CFBundleIconFile": "AppIcon.icns",
            "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1", "LSMinimumSystemVersion": minimum_system,
            "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication"}
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(info, stream, sort_keys=True)
    write_release_metadata(source_root, resources, backend_dir=resources / "server")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "macos")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build" / "macos")
    parser.add_argument("--deployment-target", help="Defaults to the highest installed runtime wheel/Python minimum")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("A macOS app must be built on macOS")
    minimum = minimum_macos_version(runtime_distributions(ROOT))
    target = args.deployment_target or minimum
    if not re.fullmatch(r"\d+\.\d+", target) or tuple(map(int, target.split("."))) < tuple(map(int, minimum.split("."))):
        parser.error(f"Deployment target must be at least {minimum}, matching the installed runtime dependencies")
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(work / "dist"),
                    "--workpath", str(work / "build"), str(ROOT / "KPW6StudioServer.spec")], cwd=ROOT, check=True)
    launcher = work / "KPW6StudioLauncher"
    subprocess.run(["swiftc", "-O", "-target", f"{platform.machine()}-apple-macosx{target}",
                    str(ROOT / "packaging" / "macos_tray.swift"), "-o", str(launcher), "-framework", "Cocoa"], check=True)
    # File-provider/synced output folders can re-add FinderInfo while codesign
    # runs. Sign and verify in local staging, then transport the verified bundle
    # as a ZIP that does not carry those forbidden extended attributes.
    archive = args.output_dir.resolve() / f"KPW6-PDF-Studio-{platform.machine()}.zip"
    with tempfile.TemporaryDirectory(prefix='kpw6-app-staging-') as staging:
        app = assemble_app(work / "dist" / "KPW6StudioServer", launcher, Path(staging) / APP_NAME, minimum_system=target)
        prepare_for_signing(app)
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        archive_app(app, archive)
        with tempfile.TemporaryDirectory(prefix='kpw6-app-verify-') as verify:
            subprocess.run(['ditto', '-x', '-k', str(archive), verify], check=True)
            subprocess.run(['codesign', '--verify', '--deep', '--strict', str(Path(verify) / APP_NAME)], check=True)
    print(f"Created verified {platform.machine()} distribution: {archive} (macOS {target}+; local ad-hoc signature)")
    print("Developer ID signing and notarization are separate distribution steps.")


if __name__ == "__main__":
    main()
