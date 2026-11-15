"""Cover upload contracts; all input and app data are temporary synthetic fixtures."""
import importlib.util
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PIL import Image


def image_bytes(fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", (24, 32), (36, 82, 128)).save(buf, format=fmt)
    return buf.getvalue()


class CoverUploadRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory()
        cls.module_name = "_kpw6_cover_upload_test_app"
        spec = importlib.util.spec_from_file_location(
            cls.module_name, Path(__file__).resolve().parents[1] / "app.py")
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[cls.module_name] = cls.module
        with patch.dict(os.environ, {"KPW6_DATA_DIR": cls.data.name}):
            spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.module_name, None)
        cls.data.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.upload_dir = Path(self.temp.name) / "uploads"
        self.upload_dir.mkdir()
        for replacement in (
            patch.object(self.module, "UPLOAD_DIR", str(self.upload_dir)),
            patch.object(self.module, "CUSTOM_COVERS", {}),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = self.module.app.test_client()

    def upload(self, contents, filename):
        return self.client.post("/api/upload_cover", data={
            "file": (io.BytesIO(contents), filename),
        })

    def assert_rejected_without_publication(self, contents, filename):
        before_files = set(self.upload_dir.iterdir())
        before_covers = dict(self.module.CUSTOM_COVERS)
        result = self.upload(contents, filename)
        self.assertEqual(result.status_code, 400, result.get_json())
        self.assertIsInstance(result.get_json().get("error"), str)
        self.assertEqual(set(self.upload_dir.iterdir()), before_files)
        self.assertEqual(self.module.CUSTOM_COVERS, before_covers)

    def test_corrupt_cover_is_rejected_without_publication(self):
        self.assert_rejected_without_publication(b"not a PNG image", "corrupt.png")

    def test_truncated_image_is_rejected_without_publication(self):
        self.assert_rejected_without_publication(image_bytes()[:40], "truncated.png")

    def test_over_budget_image_header_is_rejected_without_decoding_large_pixels(self):
        contents = bytearray(image_bytes())
        # A valid IHDR checksum with 25,005,000 advertised pixels; no large image is allocated.
        contents[16:24] = struct.pack(">II", 5001, 5000)
        contents[29:33] = struct.pack(">I", zlib.crc32(contents[12:29]))
        self.assert_rejected_without_publication(bytes(contents), "oversized.png")

    def test_failed_upload_preserves_a_previously_valid_cover(self):
        previous = self.upload(image_bytes(), "previous.png")
        self.assertEqual(previous.status_code, 200, previous.get_json())
        old_id = previous.get_json()["cover_id"]
        old_path = Path(self.module.CUSTOM_COVERS[old_id])
        old_bytes = old_path.read_bytes()
        self.assert_rejected_without_publication(b"bad replacement", "replacement.jpg")
        self.assertEqual(old_path.read_bytes(), old_bytes)

    def test_valid_png_and_jpeg_are_published_as_cover_files(self):
        for fmt, filename in (("PNG", "cover.png"), ("JPEG", "cover.jpg")):
            with self.subTest(format=fmt):
                contents = image_bytes(fmt)
                result = self.upload(contents, filename)
                self.assertEqual(result.status_code, 200, result.get_json())
                data = result.get_json()
                cover_path = Path(self.module.CUSTOM_COVERS[data["cover_id"]])
                self.assertEqual(cover_path.read_bytes(), contents)
                self.assertEqual(data["filename"], filename)
                self.assertFalse(any(p.suffix == ".part" for p in self.upload_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
