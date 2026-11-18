"""Release layout, notices, icon fallback, and native launcher regressions."""
from importlib import metadata, util
import csv
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class PackagingRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def load_script(self, name):
        path = ROOT / "packaging" / name
        self.assertTrue(path.is_file(), f"Missing release entry point: {path}")
        spec = util.spec_from_file_location("kpw6_test_" + path.stem, path)
        module = util.module_from_spec(spec)
        with patch.object(sys, "path", [str(path.parent), str(ROOT / "packaging"), *sys.path]):
            spec.loader.exec_module(module)
        return module

    def source_fixture(self):
        source = self.root / "source"
        (source / "docs").mkdir(parents=True)
        (source / "src" / "kpw6_studio").mkdir(parents=True)
        (source / "LICENSE").write_text("AGPL source fixture\n")
        (source / "docs" / "THIRD_PARTY_NOTICES.md").write_text("Third-party notices fixture\n")
        (source / "pyproject.toml").write_text('[project]\nname = "fixture"\ndependencies = []\n')
        (source / "src" / "kpw6_studio" / "__main__.py").write_text("print('source snapshot')\n")
        return source

    def test_windows_launcher_target_exists_in_prepared_distribution(self):
        builder = self.load_script("windows/build_windows.py")
        backend = self.root / "backend"
        backend.mkdir()
        (backend / "KindleKPW6Studio.exe").write_bytes(b"executable fixture")
        (backend / "_internal").mkdir()
        (backend / "_internal" / "dependency.dll").write_bytes(b"dependency fixture")
        release = self.root / "release with spaces"
        builder.assemble_distribution(backend, release, self.source_fixture())
        batch = next(release.glob("*.bat"))
        match = re.search(r'%~dp0([^"\r\n]+)', batch.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, batch.read_text())
        target = release.joinpath(*match.group(1).replace("\\", "/").split("/"))
        self.assertTrue(target.is_file(), f"BAT points to missing target: {target}")
        self.assertEqual(target.read_bytes(), b"executable fixture")
        self.assertTrue((target.parent / "_internal" / "dependency.dll").is_file())
        self.assertTrue((release / "LICENSE").is_file())
        self.assertTrue((release / "SOURCE.zip").is_file())

    def test_macos_assembly_has_launchable_bundle_layout_and_backend_dependencies(self):
        builder = self.load_script("macos/build_macos.py")
        backend = self.root / "backend"
        backend.mkdir()
        (backend / "KPW6StudioServer").write_bytes(b"backend fixture")
        (backend / "_internal").mkdir()
        (backend / "_internal" / "dependency.dylib").write_bytes(b"dependency fixture")
        launcher = self.root / "launcher"
        launcher.write_bytes(b"launcher fixture")
        icon = ROOT / "packaging" / "assets" / "AppIcon.icns"
        app = self.root / "release" / "KPW6 PDF Studio.app"
        builder.assemble_app(backend, launcher, app, self.source_fixture(), icon)
        with (app / "Contents" / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        executable = app / "Contents" / "MacOS" / info["CFBundleExecutable"]
        self.assertEqual(executable.read_bytes(), launcher.read_bytes())
        self.assertTrue(os.access(executable, os.X_OK))
        self.assertTrue((app / "Contents" / "Resources" / info["CFBundleIconFile"]).is_file())
        resources = app / "Contents" / "Resources"
        self.assertTrue((resources / "server" / "KPW6StudioServer").is_file())
        self.assertTrue((resources / "server" / "_internal" / "dependency.dylib").is_file())
        self.assertTrue((resources / "LICENSE").is_file())
        self.assertTrue((resources / "SOURCE.zip").is_file())

    @unittest.skipUnless(sys.platform == "darwin", "Finder attributes are a macOS signing constraint")
    def test_macos_signing_metadata_is_removed_from_build_only(self):
        builder = self.load_script("macos/build_macos.py")
        app = self.root / "fixture.app"
        app.mkdir()
        binary = app / "launcher"
        binary.write_bytes(b"fixture")
        subprocess.run(['xattr', '-w', 'com.apple.ResourceFork', 'audit fixture', str(binary)], check=True)
        self.assertTrue(hasattr(builder, 'prepare_for_signing'), "Build must sanitize copied Finder/resource fork attributes")
        builder.prepare_for_signing(app)
        attributes = subprocess.check_output(['xattr', str(binary)], text=True)
        self.assertNotIn('com.apple.ResourceFork', attributes)

    def test_actual_wheel_license_and_nested_vendor_notice_are_preserved(self):
        support = self.load_script("release_support.py")
        site = self.root / "site"
        dist_info = site / "fixture_lib-1.2.dist-info"
        (dist_info / "licenses").mkdir(parents=True)
        (dist_info / "METADATA").write_text("Metadata-Version: 2.4\nName: fixture-lib\nVersion: 1.2\nLicense-Expression: MIT\n")
        files = {"fixture_lib-1.2.dist-info/licenses/LICENSE": b"license exact bytes\r\n",
                 "fixture_lib/vendor/NOTICE.txt": b"vendor notice exact bytes\n"}
        for name, contents in files.items():
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        with (dist_info / "RECORD").open("w", newline="") as stream:
            csv.writer(stream).writerows((name, "", "") for name in files)
        target = self.root / "licenses"
        records = support.collect_third_party_notices(target, [metadata.Distribution.at(dist_info)])
        self.assertEqual({p.read_bytes() for p in target.rglob("*") if p.is_file()}, set(files.values()))
        self.assertEqual(records[0]["name"], "fixture-lib")
        self.assertEqual(records[0]["version"], "1.2")
        self.assertEqual(len(records[0]["license_files"]), 2)

    def test_source_snapshot_excludes_runtime_data_secrets_and_virtualenv(self):
        support = self.load_script("release_support.py")
        source = self.source_fixture()
        for name in ("uploads/private.pdf", "outputs/book.pdf", ".venv/key.pem", ".git/config", "work/evidence.json", ".env", "id_rsa"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must not distribute")
        release = self.root / "release"
        support.write_release_metadata(source, release, distributions=[])
        with zipfile.ZipFile(release / "SOURCE.zip") as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith("/__main__.py") for name in names))
            self.assertTrue(any(name.endswith("/LICENSE") for name in names))
            self.assertFalse(any("private.pdf" in name or "key.pem" in name or "/.git/" in name or "evidence.json" in name or name.endswith("/.env") or name.endswith("/id_rsa") for name in names), names)
        source_info = (release / "SOURCE_CODE.md").read_text(encoding="utf-8")
        self.assertIn("SOURCE.zip", source_info)
        self.assertIn("AGPL", source_info)
        self.assertTrue((release / "RELEASE_MANIFEST.json").is_file())

    def test_release_metadata_requires_project_license(self):
        support = self.load_script("release_support.py")
        source = self.source_fixture()
        (source / "LICENSE").unlink()
        with self.assertRaisesRegex((ValueError, FileNotFoundError), "LICENSE"):
            support.write_release_metadata(source, self.root / "release", distributions=[])

    def test_release_refuses_a_bundled_dependency_without_notices(self):
        support = self.load_script('release_support.py')
        from types import SimpleNamespace
        dependency = SimpleNamespace(metadata={'Name': 'missing-notice', 'License': 'MIT'}, version='1.0', files=[])
        with self.assertRaisesRegex(ValueError, 'missing-notice'):
            support.write_release_metadata(self.source_fixture(), self.root / 'release', distributions=[dependency])

    def test_excluded_optional_packages_are_not_claimed_as_bundled(self):
        support = self.load_script("release_support.py")
        from types import SimpleNamespace
        source = self.source_fixture()
        (source / "pyproject.toml").write_text('[project]\nname = "fixture"\ndependencies = ["pymupdf>=1.26.4"]\n')
        dists = {
            "pymupdf": SimpleNamespace(requires=["pymupdf-layout", "pillow"]),
            "pymupdf-layout": SimpleNamespace(requires=["flatbuffers"]),
            "pillow": SimpleNamespace(requires=[]),
            "flatbuffers": SimpleNamespace(requires=[]),
        }
        with patch.object(support.metadata, 'distribution', side_effect=lambda name: dists[name]):
            included = support.runtime_distributions(source)
        self.assertEqual(included, [dists['pillow'], dists['pymupdf']])

    def test_frozen_smoke_contacts_loopback_without_environment_proxy(self):
        from kpw6_studio.server.http_server import LoopbackWSGIServer
        smoke = self.load_script('smoke_frozen.py')
        def respond(environ, start_response):
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'local health response']
        server = LoopbackWSGIServer('127.0.0.1', 0, respond)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            environment = {'http_proxy': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1',
                           'no_proxy': '', 'NO_PROXY': ''}
            with patch.dict(os.environ, environment), patch.object(urllib.request, '_opener', None):
                result = smoke.request(f'http://127.0.0.1:{server.server_port}', '/api/health')
            self.assertEqual(result, b'local health response')
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def test_no_system_cjk_font_keeps_requested_size_and_safe_ascii_labels(self):
        icons = self.load_script("make_icon.py")
        with patch.object(icons.os.path, "exists", return_value=False):
            font = icons._load_cjk_font(40)
            self.assertEqual(getattr(font, "size", None), 40)
            labels = icons.icon_labels(font)
        self.assertTrue(all(label.isascii() for label in labels), labels)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "Native Swift check requires macOS")
    def test_native_health_directory_and_bounded_termination(self):
        source = (ROOT / "packaging" / "macos" / "macos_tray.swift").read_text()
        self.assertIn("func decodeServerHealth", source, "Launcher must decode the server's actual output directory")
        self.assertIn("func stopServerProcess", source, "Launcher needs a bounded synchronous stop path")
        body = source.split("let app = NSApplication.shared")[0]
        marker = self.root / "ready"
        driver = r'''
let health = decodeServerHealth(Data(#"{"ok":true,"output_dir":"/tmp/custom-kpw6/outputs"}"#.utf8))
guard health?.outputDirectoryURL.path == "/tmp/custom-kpw6/outputs" else { exit(21) }
guard decodeServerHealth(Data(#"{"ok":false,"output_dir":"/tmp/no"}"#.utf8)) == nil else { exit(22) }
let proc = Process()
proc.executableURL = URL(fileURLWithPath: "/bin/sh")
proc.arguments = ["-c", "trap '' TERM; touch \"$1\"; exec sleep 30", "fixture", CommandLine.arguments[1]]
try proc.run()
let readyDeadline = Date().addingTimeInterval(3)
while !FileManager.default.fileExists(atPath: CommandLine.arguments[1]) && Date() < readyDeadline { Thread.sleep(forTimeInterval: 0.01) }
let start = Date()
let stopped = stopServerProcess(proc, gracePeriod: 0.2, killWait: 0.5)
guard stopped && !proc.isRunning else { kill(proc.processIdentifier, SIGKILL); exit(23) }
guard Date().timeIntervalSince(start) < 2 else { exit(24) }
'''
        swift = self.root / "native-test.swift"
        swift.write_text(body + driver)
        binary = self.root / "native-test"
        compile_result = subprocess.run(["swiftc", str(swift), "-o", str(binary), "-framework", "Cocoa"], capture_output=True, text=True, timeout=90)
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
        result = subprocess.run([str(binary), str(marker)], capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
