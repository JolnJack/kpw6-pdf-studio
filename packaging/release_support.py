"""Shared release metadata, corresponding source, and installed-wheel notices."""
from __future__ import annotations

import ast
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

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None


SOURCE_URL = "https://github.com/JolnJack/kpw6-pdf-studio"
FROZEN_EXCLUDES = (
    "onnxruntime", "pymupdf4llm", "pymupdf_layout", "networkx", "yaml",
    "tkinter", "matplotlib", "scipy", "psutil", "tabulate", "setuptools",
    "pkg_resources", "charset_normalizer", "defusedxml", "tomli",
    "colorama", "flatbuffers", "protobuf",
)
EXCLUDED_DISTRIBUTIONS = {
    "onnxruntime", "pymupdf4llm", "pymupdf-layout", "networkx", "pyyaml",
    "matplotlib", "scipy", "psutil", "tabulate", "setuptools",
    "charset-normalizer", "defusedxml", "tomli", "colorama",
    "flatbuffers", "protobuf", "kpw6-pdf-studio",
}
PRUNED_INTERNAL_PREFIXES = (
    "pymupdf/layout/",
    "pymupdf/mupdf-devel/",
    "pymupdf4llm/",
    "pymupdf_layout/",
    "onnxruntime/",
)
RELEASE_METADATA_ITEMS = (
    "LICENSE", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES",
    "SOURCE.zip", "SOURCE_CODE.md", "RELEASE_MANIFEST.json",
)
EXCLUDED_PARTS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "uploads", "outputs", "samples", "work", "build", "dist",
    ".pytest_cache", ".ruff_cache", "kpw6_pdf_studio.egg-info",
}
SOURCE_SUFFIXES = {
    ".py", ".swift", ".spec", ".md", ".txt", ".toml",
    ".html", ".css", ".cjs", ".js", ".json", ".yml", ".yaml",
    ".png", ".ico", ".icns",
}


def prune_pyinstaller_toc(entries):
    """Filter unused ML models and C++ development headers out of PyInstaller TOC lists."""
    kept = []
    for item in entries:
        dest = str(item[0]).replace("\\", "/").lstrip("./")
        if any(dest == prefix.rstrip("/") or dest.startswith(prefix) for prefix in PRUNED_INTERNAL_PREFIXES):
            continue
        first_part = dest.split("/", 1)[0]
        if first_part.endswith(".dist-info"):
            dist_part = canonicalize_name(first_part[:-10].rsplit("-", 1)[0])
            if dist_part in EXCLUDED_DISTRIBUTIONS:
                continue
        kept.append(item)
    return kept


def prune_frozen_backend(backend_dir: Path) -> None:
    """Remove unused optional subtrees or excluded dist-info from an assembled PyInstaller directory."""
    internal = Path(backend_dir) / "_internal"
    if not internal.is_dir():
        return
    for prefix in PRUNED_INTERNAL_PREFIXES:
        target = internal.joinpath(*prefix.strip("/").split("/"))
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()
    for entry in list(internal.iterdir()):
        if entry.name.endswith(".dist-info"):
            pkg_raw = entry.name[:-10].rsplit("-", 1)[0]
            if canonicalize_name(pkg_raw) in EXCLUDED_DISTRIBUTIONS:
                shutil.rmtree(entry)


def _read_declared_requirements(source_root: Path) -> list[Requirement]:
    pyproject_path = source_root / "pyproject.toml"
    if pyproject_path.is_file():
        raw = pyproject_path.read_text(encoding="utf-8")
        if tomllib is not None:
            deps = tomllib.loads(raw).get("project", {}).get("dependencies", [])
        else:
            m = re.search(r"(?ms)^\[project\].*?^dependencies\s*=\s*(\[.*?\])", raw)
            deps = ast.literal_eval(m.group(1)) if m else []
        return [Requirement(item) for item in deps if str(item).strip()]
    req_txt = source_root / "requirements.txt"
    if req_txt.is_file():
        reqs = []
        for line in req_txt.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                reqs.append(Requirement(line))
        return reqs
    return []


def runtime_distributions(source_root: Path) -> list[metadata.Distribution]:
    """Resolve the installed dependency closure for the runtime requirements."""
    requirements = _read_declared_requirements(Path(source_root))
    found = {}
    while requirements:
        requirement = requirements.pop()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        if name in EXCLUDED_DISTRIBUTIONS or name in found:
            continue
        dist = metadata.distribution(requirement.name)
        found[name] = dist
        requirements.extend(Requirement(value) for value in dist.requires or [])
    return [found[name] for name in sorted(found)]


def bundled_distributions(source_root: Path, backend_dir: Path | None = None) -> list[metadata.Distribution]:
    """Combine declared runtime requirements with any distribution physically present in _internal."""
    found = {canonicalize_name(dist.metadata["Name"]): dist for dist in runtime_distributions(source_root)}
    if backend_dir is None:
        return [found[name] for name in sorted(found)]
    internal = Path(backend_dir) / "_internal"
    if not internal.is_dir():
        return [found[name] for name in sorted(found)]
    pkg_to_dists = metadata.packages_distributions()
    extra_names = set()
    for entry in internal.iterdir():
        if entry.name.endswith(".dist-info"):
            pkg_raw = entry.name[:-10].rsplit("-", 1)[0]
            extra_names.add(pkg_raw)
            continue
        mod_name = entry.name.split(".", 1)[0]
        if mod_name == "kpw6_studio":
            continue
        for dist_name in pkg_to_dists.get(mod_name, ()):
            extra_names.add(dist_name)
    for dist_name in extra_names:
        canon = canonicalize_name(dist_name)
        if canon in EXCLUDED_DISTRIBUTIONS or canon in found:
            continue
        try:
            found[canon] = metadata.distribution(dist_name)
        except metadata.PackageNotFoundError:
            continue
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
        records.append({
            "name": name,
            "version": version,
            "license": dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See upstream notices",
            "license_files": copied,
        })
    return records


def source_files(source_root: Path) -> list[Path]:
    """Use Git's inventory with an explicit source allowlist; never archive data or secrets."""
    source_root = source_root.resolve()
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=source_root,
        capture_output=True,
    )
    candidates = (
        [Path(name) for name in result.stdout.decode().split("\0") if name]
        if result.returncode == 0
        else [p.relative_to(source_root) for p in source_root.rglob("*") if p.is_file()]
    )
    files = []
    for relative in sorted(set(candidates)):
        if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if relative.name in {".env", "id_rsa", "id_ed25519"} or relative.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            continue
        allowed_root = len(relative.parts) == 1 and (
            relative.suffix in {".py", ".spec", ".toml"}
            or relative.name in {"LICENSE", "README.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", ".gitignore", ".gitattributes"}
            or re.fullmatch(r"requirements(?:-[\w-]+)?\.txt", relative.name)
        )
        allowed_directory = relative.parts[0] in {"src", "packaging", "tests", ".github", "docs"} and relative.suffix.lower() in SOURCE_SUFFIXES
        file = source_root / relative
        if (allowed_root or allowed_directory) and file.is_file() and not file.is_symlink():
            files.append(relative)
    return files


def _python_license() -> Path:
    override = os.environ.get("KPW6_PYTHON_LICENSE")
    candidates = ([Path(override)] if override else []) + [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Python LICENSE.txt was not found; set KPW6_PYTHON_LICENSE to the interpreter's license file")


def _locate_third_party_notices(source_root: Path) -> Path:
    for candidate in (source_root / "docs" / "THIRD_PARTY_NOTICES.md", source_root / "THIRD_PARTY_NOTICES.md"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Release requires THIRD_PARTY_NOTICES.md in {source_root / 'docs'}")


def write_release_metadata(source_root: Path, destination: Path, distributions=None, backend_dir: Path | None = None) -> dict:
    """Attach AGPL terms, exact source snapshot, provenance, and wheel notices."""
    source_root, destination = Path(source_root).resolve(), Path(destination).resolve()
    license_file = source_root / "LICENSE"
    if not license_file.is_file():
        raise FileNotFoundError(f"Release requires project LICENSE: {license_file}")
    notices_file = _locate_third_party_notices(source_root)
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(license_file, destination / "LICENSE")
    shutil.copyfile(notices_file, destination / "THIRD_PARTY_NOTICES.md")
    distributions = bundled_distributions(source_root, backend_dir) if distributions is None else list(distributions)
    legal_root = destination / "THIRD_PARTY_LICENSES"
    if legal_root.exists():
        shutil.rmtree(legal_root)
    records = collect_third_party_notices(legal_root, distributions)
    missing = [record["name"] for record in records if not record["license_files"]]
    if missing:
        raise ValueError("Bundled dependencies require reviewed license/notice files: " + ", ".join(missing))
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
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_url": SOURCE_URL,
        "base_commit": commit,
        "working_tree_modified": dirty,
        "project_license": "AGPL-3.0-only",
        "python_version": sys.version.split()[0],
        "source_sha256": hashes,
        "distributions": records,
        "distributions_without_license_files": [item["name"] for item in records if not item["license_files"]],
        "excluded_modules": list(FROZEN_EXCLUDES),
    }
    (destination / "RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
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
