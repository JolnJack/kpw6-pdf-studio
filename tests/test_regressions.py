"""Run with: python -m unittest discover -s tests -v."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pymupdf as fitz

import pdf_engine as engine


def make_pdf(path, label="BOOK", rotation=0, pages=1, diagram=False):
    with fitz.open() as doc:
        for i in range(pages):
            page = doc.new_page(width=595, height=842)
            for y, position in ((180, "TOP"), (420, "MIDDLE"), (700, "BOTTOM")):
                page.insert_text((80, y), f"{label} PAGE{i + 1} {position}", fontsize=16)
            if diagram:
                page.draw_rect(fitz.Rect(80, 220, 440, 380), width=4)
                page.draw_line((80, 220), (440, 380), width=4)
            page.set_rotation(rotation)
        doc.save(path)


class EngineRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_rotated_vector_export_preserves_all_text_and_source(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                source = self.root / f"rot{rotation}.pdf"
                make_pdf(source, rotation=rotation)
                source_hash = hashlib.sha256(source.read_bytes()).digest()
                output = self.root / f"out{rotation}.pdf"
                engine.build_kpw6_pdf(str(source), str(output), engine.ProcessOptions(
                    mode="tech_book", cover_mode="none", trim_header_footer=False,
                    clean_spine_shadow=False,
                ))
                with fitz.open(output) as doc:
                    text = " ".join(p.get_text() for p in doc)
                    self.assertTrue(all(word in text for word in ("TOP", "MIDDLE", "BOTTOM")), text)
                    for page in doc:
                        pix = page.get_pixmap(colorspace=fitz.csGRAY)
                        self.assertGreater(np.count_nonzero(np.frombuffer(pix.samples, dtype=np.uint8) < 240), 0)
                        self.assertEqual(page.get_images(), [], "Vector output must retain vector content")
                self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), source_hash)

    def test_invalid_page_ranges_are_rejected(self):
        for start, end in ((5, None), (5, 6), (3, 1)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    engine.get_target_page_list(3, engine.ProcessOptions(start_page=start, end_page=end))

    def test_rotation_normalization_preserves_crop_and_visual_appearance(self):
        for offset in (False, True):
            for rotation in (90, 180, 270):
                with self.subTest(offset=offset, rotation=rotation):
                    source = self.root / "cropped.pdf"
                    with fitz.open() as doc:
                        page = doc.new_page(width=595, height=842)
                        if offset:
                            page.set_mediabox(fitz.Rect(10, 20, 605, 862))
                        page.insert_text((80, 180), "INSIDE CONTENT", fontsize=16)
                        page.insert_text((10, 60), "OUTSIDE CROP", fontsize=10)
                        page.set_cropbox(fitz.Rect(60 if offset else 50, 100, 555 if offset else 545, 742))
                        page.set_rotation(rotation)
                        doc.save(source)
                    with fitz.open(source) as doc:
                        before = doc[0].get_pixmap(colorspace=fitz.csGRAY)
                    with engine._open_source_pdf(str(source)) as doc:
                        after = doc[0].get_pixmap(colorspace=fitz.csGRAY)
                        self.assertEqual((before.width, before.height), (after.width, after.height))
                        original = np.frombuffer(before.samples, dtype=np.uint8).astype(float)
                        normalized = np.frombuffer(after.samples, dtype=np.uint8).astype(float)
                        self.assertLess(np.abs(original - normalized).mean(), 0.2)
                        self.assertNotIn("OUTSIDE", doc[0].get_text())

    def test_valid_ranges_and_skip_rules(self):
        self.assertEqual(engine.get_target_page_list(3, engine.ProcessOptions()), [0, 1, 2])
        self.assertEqual(engine.get_target_page_list(3, engine.ProcessOptions(start_page=2, end_page=99)), [1, 2])
        self.assertEqual(engine.get_target_page_list(3, engine.ProcessOptions(
            explicit_pages=[0, 2], page_overrides={"0": "skip"})), [2])
        self.assertEqual(engine.get_target_page_list(0, engine.ProcessOptions()), [])

    def test_reflow_epub_preserves_whole_page_overrides_in_reading_order(self):
        source = self.root / "mixed.pdf"
        make_pdf(source, pages=3, diagram=True)
        for rule in ("whole_portrait", "whole_landscape"):
            with self.subTest(rule=rule):
                output = self.root / f"{rule}.epub"
                engine.build_kpw6_epub(str(source), str(output), engine.ProcessOptions(
                    mode="pure_text", cover_mode="none", trim_header_footer=False,
                    page_overrides={"1": rule}), force_reflow=True)
                with zipfile.ZipFile(output) as archive:
                    opf = ET.fromstring(archive.read("OEBPS/content.opf"))
                    ns = {"o": "http://www.idpf.org/2007/opf"}
                    manifest = {item.attrib["id"]: item.attrib["href"] for item in opf.findall("o:manifest/o:item", ns)}
                    bodies = [archive.read("OEBPS/" + manifest[ref.attrib["idref"]]).decode()
                              for ref in opf.findall("o:spine/o:itemref", ns)
                              if ref.attrib["idref"] != "cover-page"]
                    self.assertEqual(len(bodies), 3, "Expected text, protected page, then text")
                    self.assertIn("PAGE1", bodies[0])
                    self.assertIn("<img ", bodies[1], "Protected page must retain its graphics")
                    self.assertNotIn("<p>BOOK PAGE2", bodies[1])
                    self.assertIn("PAGE3", bodies[2])
                    images = [name for name in archive.namelist() if name.startswith("OEBPS/images/") and "cover" not in name]
                    self.assertEqual(len(images), 1)


class ApiRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old_data_dir = os.environ.get("KPW6_DATA_DIR")
        os.environ["KPW6_DATA_DIR"] = cls.temp.name
        import app
        cls.module = app
        cls.module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        if cls.old_data_dir is None:
            os.environ.pop("KPW6_DATA_DIR", None)
        else:
            os.environ["KPW6_DATA_DIR"] = cls.old_data_dir
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.module.app.test_client()

    def upload(self, label="BOOK", pages=1, filename="book.pdf"):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "input.pdf"
            make_pdf(path, label, pages=pages)
            response = self.client.post("/api/upload", data={"file": (io.BytesIO(path.read_bytes()), filename)})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["file_id"]

    def export(self, fid, route="/api/export_unified", **options):
        return self.client.post(route, json={"file_id": fid, "mode": "pure_text",
            "cover_mode": "none", "trim_header_footer": False, "export_format": "pdf", **options})

    def test_out_of_range_export_returns_400(self):
        fid = self.upload(pages=3)
        for route in ("/api/export_unified", "/api/export"):
            with self.subTest(route=route):
                response = self.export(fid, route, start_page=5)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())

    def test_export_formats_provide_reachable_valid_downloads(self):
        fid = self.upload()
        for fmt in ("pdf", "epub_layout", "epub_reflow", "cbz", "bundle_both"):
            with self.subTest(format=fmt):
                response = self.export(fid, export_format=fmt)
                self.assertEqual(response.status_code, 200, response.get_json())
                result = response.get_json()
                for key in ("download_url", "second_download_url", "view_url"):
                    if key not in result:
                        continue
                    with self.client.get(result[key]) as download:
                        self.assertEqual(download.status_code, 200)
                        contents = download.data
                    if contents.startswith(b"%PDF"):
                        with fitz.open(stream=contents, filetype="pdf") as doc:
                            self.assertGreater(doc.page_count, 0)
                    else:
                        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
                            self.assertIsNone(archive.testzip())
                            if "OEBPS/content.opf" in archive.namelist():
                                opf = ET.fromstring(archive.read("OEBPS/content.opf"))
                                ns = {"o": "http://www.idpf.org/2007/opf"}
                                for item in opf.findall("o:manifest/o:item", ns):
                                    self.assertIn("OEBPS/" + item.attrib["href"], archive.namelist())

    def test_demo_modes_preview_and_pdf_export(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        for mode in self.module.DEMO_PATHS:
            with self.subTest(mode=mode):
                fid = f"demo_{mode}"
                preview = self.client.post("/api/preview", json={
                    "file_id": fid, "mode": mode, "cover_mode": "none", "page_num": 0})
                self.assertEqual(preview.status_code, 200)
                self.assertGreater(preview.get_json()["kpw6_screens_count"], 0)
                result = self.export(fid, mode=mode)
                self.assertEqual(result.status_code, 200, result.get_json())
                with self.client.get(result.get_json()["download_url"]) as download:
                    with fitz.open(stream=download.data, filetype="pdf") as doc:
                        self.assertGreater(doc.page_count, 0)

    def test_split_volumes_keep_skip_rules_and_all_bundle_links(self):
        fid = self.upload(pages=3)
        response = self.export(fid, export_format="bundle_both", split_volumes_every=1,
                               page_overrides={"1": "skip"})
        self.assertEqual(response.status_code, 200, response.get_json())
        volumes = response.get_json()["volumes"]
        self.assertEqual(len(volumes), 2)
        for volume, expected_page in zip(volumes, ("PAGE1", "PAGE3")):
            for key in ("download_url", "second_download_url"):
                with self.client.get(volume[key]) as download:
                    self.assertEqual(download.status_code, 200)
                    if key == "second_download_url":
                        with fitz.open(stream=download.data, filetype="pdf") as doc:
                            text = " ".join(p.get_text() for p in doc)
                            self.assertIn(expected_page, text)
                            self.assertNotIn("PAGE2", text)

    @unittest.skipUnless(shutil.which("ebook-convert") or Path("/Applications/calibre.app/Contents/MacOS/ebook-convert").exists(),
                         "Calibre is optional")
    def test_calibre_exports_when_available(self):
        fid = self.upload()
        for fmt in ("azw3", "mobi"):
            with self.subTest(format=fmt):
                response = self.export(fid, export_format=fmt)
                self.assertEqual(response.status_code, 200, response.get_json())
                with self.client.get(response.get_json()["download_url"]) as download:
                    self.assertEqual(download.status_code, 200)
                    self.assertGreater(len(download.data), 1000)

    def test_password_protected_upload_is_rejected(self):
        with fitz.open() as doc:
            doc.new_page()
            data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                              owner_pw="test-owner", user_pw="test-reader")
        response = self.client.post("/api/upload", data={"file": (io.BytesIO(data), "locked.pdf")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_same_name_exports_preserve_earlier_downloads(self):
        for route in ("/api/export_unified", "/api/export"):
            with self.subTest(route=route):
                first = self.export(self.upload("FIRST"), route)
                self.assertEqual(first.status_code, 200)
                old_url = first.get_json()["download_url"]
                with self.client.get(old_url) as response:
                    old_bytes = response.data
                second = self.export(self.upload("SECOND"), route)
                self.assertEqual(second.status_code, 200)
                self.assertNotEqual(old_url, second.get_json()["download_url"])
                with self.client.get(old_url) as response:
                    self.assertEqual(response.data, old_bytes)

    def test_invalid_upload_is_rejected_without_registering_or_saving(self):
        before_files = set(Path(self.module.UPLOAD_DIR).iterdir())
        before_ids = set(self.module.FILES)
        self.module.app.config["TESTING"] = False
        self.module.app.logger.disabled = True
        try:
            response = self.client.post("/api/upload", data={"file": (io.BytesIO(b"not a pdf"), "broken.pdf")})
        finally:
            self.module.app.config["TESTING"] = True
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(response.get_json().get("error"))
        self.assertEqual(set(self.module.FILES), before_ids)
        self.assertEqual(set(Path(self.module.UPLOAD_DIR).iterdir()), before_files)

    def test_history_ignores_corrupt_pdfs_left_by_previous_versions(self):
        bad = Path(self.module.UPLOAD_DIR) / "bad00000_damaged.pdf"
        bad.write_bytes(b"invalid")
        self.addCleanup(bad.unlink)
        response = self.client.get("/api/files")
        self.assertFalse(any(item["file_id"] == "bad00000" for item in response.get_json()["uploaded"]))


if __name__ == "__main__":
    unittest.main()
