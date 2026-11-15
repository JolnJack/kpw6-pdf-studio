"""Shared release metadata, corresponding source, and installed-wheel notices."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


SOURCE_URL = "https://github.com/JolnJack/kpw6-pdf-studio"
FROZEN_EXCLUDES = ("onnxruntime", "pymupdf4llm", "pymupdf_layout", "networkx", "yaml", "tkinter", "matplotlib", "scipy")
EXCLUDED_DISTRIBUTIONS = {"onnxruntime", "pymupdf4llm", "pymupdf-layout", "networkx", "pyyaml", "matplotlib", "scipy"}
EXCLUDED_PARTS = {".git", ".venv", "venv", "__pycache__", "node_modules", "uploads", "outputs", "samples", "work", "build", "dist", ".pytest_cache"}
SOURCE_SUFFIXES = {".py", ".swift", ".spec", ".md", ".txt", ".cjs", ".js", ".json", ".yml", ".yaml", ".png", ".ico", ".icns"}


def runtime_distributions(source_root: Path) -> list[metadata.Distribution]:
    """Resolve the installed dependency closure for the runtime requirements."""
    requirements = []
    for line in (source_root / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            requirements.append(Requirement(line))
    found = {}
    while requirements:
        requirement = requirements.pop()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        if name in EXCLUDED_DISTRIBUTIONS:
            # These optional analysis/ML packages are excluded by both frozen
            # recipes, along with their private dependency subtrees.
            continue
        if name in found:
            continue
        dist = metadata.distribution(requirement.name)
        found[name] = dist
        requirements.extend(Requirement(value) for value in dist.requires or [])
    return [found[name] for name in sorted(found)]


def collect_third_party_notices(destination: Path, distributions) -> list[dict]:
    """Copy exact installed-wheel legal files, including nested vendor notices."""
    destination = Path(destination)
    records = []
    for dist in distributions:
        name = dist.metadata["Name"]
        version = dist.version
        directory = destination / f"{canonicalize_name(name)}-{version}"
        copied = []
        for item in sorted(dist.files or [], key=str):
            relative = Path(str(item))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            legal_name = relative.name.lower().startswith(("license", "copying", "notice", "copyright", "authors"))
            legal_directory = any(part.lower() in {"license", "licenses", "licences"} for part in relative.parts[:-1])
            if not (legal_name or legal_directory):
                continue
            source = Path(dist.locate_file(item))
            if source.is_file():
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                copied.append(str(target.relative_to(destination)).replace(os.sep, "/"))
        records.append({"name": name, "version": version,
                        "license": dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See upstream notices",
                        "license_files": copied})
    return records


def source_files(source_root: Path) -> list[Path]:
    """Use Git's inventory with an explicit source allowlist; never archive data."""
    source_root = source_root.resolve()
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=source_root, capture_output=True)
    candidates = [Path(name) for name in result.stdout.decode().split("\0") if name] if result.returncode == 0 else [p.relative_to(source_root) for p in source_root.rglob("*") if p.is_file()]
    files = []
    for relative in sorted(set(candidates)):
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if relative.name in {".env", "id_rsa", "id_ed25519"} or relative.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            continue
        allowed_root = len(relative.parts) == 1 and (relative.suffix in {".py", ".spec"} or relative.name in {"LICENSE", "README.md", "THIRD_PARTY_NOTICES.md", ".gitignore", ".gitattributes"} or re.fullmatch(r"requirements(?:-[\w-]+)?\.txt", relative.name))
        allowed_directory = relative.parts[0] in {"packaging", "tests", ".github"} and relative.suffix.lower() in SOURCE_SUFFIXES
        file = source_root / relative
        if (allowed_root or allowed_directory) and file.is_file() and not file.is_symlink():
            files.append(relative)
    return files


def _python_license() -> Path:
    override = os.environ.get("KPW6_PYTHON_LICENSE")
    candidates = ([Path(override)] if override else []) + [Path(sys.base_prefix) / "LICENSE.txt", Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Python LICENSE.txt was not found; set KPW6_PYTHON_LICENSE to the interpreter's license file")


def write_release_metadata(source_root: Path, destination: Path, distributions=None) -> dict:
    """Attach AGPL terms, exact source snapshot, provenance, and wheel notices."""
    source_root, destination = Path(source_root).resolve(), Path(destination).resolve()
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        if not (source_root / name).is_file():
            raise FileNotFoundError(f"Release requires project {name}: {source_root / name}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(source_root / name, destination / name)
    distributions = runtime_distributions(source_root) if distributions is None else list(distributions)
    legal_root = destination / "THIRD_PARTY_LICENSES"
    if legal_root.exists():
        shutil.rmtree(legal_root)
    records = collect_third_party_notices(legal_root, distributions)
    missing = [record['name'] for record in records if not record['license_files']]
    if missing:
        raise ValueError('Bundled dependencies require reviewed license/notice files: ' + ', '.join(missing))
    python_license = legal_root / "python" / "LICENSE.txt"
    python_license.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_python_license(), python_license)
    files = source_files(source_root)
    hashes = {str(p).replace(os.sep, "/"): hashlib.sha256((source_root / p).read_bytes()).hexdigest() for p in files}
    with zipfile.ZipFile(destination / "SOURCE.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(source_root / file, "kpw6-pdf-studio/" + str(file).replace(os.sep, "/"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source_root, capture_output=True, text=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=source_root, capture_output=True, text=True)
    commit = head.stdout.strip() if head.returncode == 0 else None
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "source_url": SOURCE_URL,
                "base_commit": commit, "working_tree_modified": dirty, "project_license": "AGPL-3.0-only",
                "python_version": sys.version.split()[0], "source_sha256": hashes, "distributions": records,
                "distributions_without_license_files": [item["name"] for item in records if not item["license_files"]]}
    manifest["excluded_modules"] = list(FROZEN_EXCLUDES)
    (destination / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source_text = f"""# Corresponding source / 对应源码

This distribution of KPW6 PDF Studio is licensed under AGPL-3.0-only; see LICENSE.
SOURCE.zip contains the project source used for this build, including build scripts.
RELEASE_MANIFEST.json records SHA-256 hashes of the source snapshot and installed dependency versions.
The archive excludes uploads, exports, samples, virtual environments, build output, and secrets.

本包的项目源码快照位于 SOURCE.zip；许可证为 AGPL-3.0-only，全文见 LICENSE。
构建时的源码哈希和依赖版本见 RELEASE_MANIFEST.json。源码仓库：{SOURCE_URL}
Base commit: {commit or 'not a Git checkout'}
Working tree modified: {dirty}

For a clean committed build, obtain the same revision with:
    git clone {SOURCE_URL}.git
    git -C kpw6-pdf-studio checkout {commit or '<release commit>'}

For a modified working tree, SOURCE.zip is the matching source; the base commit alone is insufficient.
Third-party dependency terms and exact notices from the installed wheels are in THIRD_PARTY_LICENSES
and THIRD_PARTY_NOTICES.md. Those projects retain their respective licenses.
"""
    (destination / "SOURCE_CODE.md").write_text(source_text, encoding="utf-8")
    return manifest
