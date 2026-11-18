"""Comprehensive regression tests covering all security, performance, XML/EPUB,
typography, concurrency/cancellation, and packaging fixes from the repository audit.
"""
from collections import OrderedDict
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import pymupdf as fitz
from PIL import Image

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kpw6_studio import engine
from kpw6_studio.engine import document as engine_document


class EngineAuditFixesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_xml_illegal_control_chars_are_stripped_in_epub_and_cbz(self):
        src = self.root / "ctrl_chars.pdf"
        with fitz.open() as doc:
            p = doc.new_page(width=595, height=842)
            p.insert_text((72, 120), "Chapter 1\x0c Dirty\x07 Heading\x01", fontsize=16)
            p.insert_text((72, 170), "Body with form-feed\x0c and bell\x07 and unit-sep\x1f text.", fontsize=11)
            doc.set_metadata({"title": "Bad\x0cTitle\x08Book", "author": "Author\x0bName"})
            doc.set_toc([[1, "TOC\x0cEntry\x07One", 1]])
            doc.save(src)

        opts = engine.ProcessOptions(
            mode="pure_text",
            cover_mode="smart_card",
            custom_cover_title="Custom\x0cTitle\x07",
            custom_cover_author="Custom\x1fAuthor",
        )
        for reflow in (True, False):
            epub_out = self.root / f"clean_{reflow}.epub"
            engine.build_kpw6_epub(str(src), str(epub_out), opts, force_reflow=reflow)
            with zipfile.ZipFile(epub_out) as zf:
                for name in zf.namelist():
                    if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                        ET.fromstring(zf.read(name))

        cbz_out = self.root / "clean.cbz"
        engine.build_kpw6_cbz(str(src), str(cbz_out), opts)
        with zipfile.ZipFile(cbz_out) as zf:
            ET.fromstring(zf.read("ComicInfo.xml"))

    def test_source_pdf_cache_avoids_repeated_bake_and_invalidates_on_change(self):
        src = self.root / "cached.pdf"
        with fitz.open() as doc:
            for idx in range(5):
                p = doc.new_page()
                p.insert_text((80, 180), f"Cached page {idx + 1}", fontsize=14)
            doc.save(src)

        engine._SOURCE_PDF_CACHE.clear()
        calls = []
        orig_bake = engine._bake_and_normalize_doc

        def counting_bake(d):
            calls.append(len(d))
            return orig_bake(d)

        opts = engine.ProcessOptions(mode="tech_book", cover_mode="none")
        with patch.object(engine_document, "_bake_and_normalize_doc", side_effect=counting_bake):
            engine.render_preview_comparison(str(src), 0, opts)
            engine.render_preview_comparison(str(src), 2, opts)
            engine.render_hd_zoom_image(str(src), 1, opts, target_type="kpw6", screen_idx=0)
            self.assertEqual(len(calls), 1, "Unchanged PDF must only bake once across previews")

            with fitz.open() as doc2:
                p = doc2.new_page()
                p.insert_text((80, 180), "Modified content", fontsize=14)
                doc2.save(src)
            engine.render_preview_comparison(str(src), 0, opts)
            self.assertEqual(len(calls), 2, "Modified PDF must invalidate cache and bake again")

    def test_pure_text_pdf_export_populates_chapter_bookmarks(self):
        src = self.root / "novel.pdf"
        font = fitz.Font("cjk")
        with fitz.open() as doc:
            for idx, chap in enumerate(("第一章 启程", "第二章 归途"), start=1):
                p = doc.new_page(width=595, height=842)
                tw = fitz.TextWriter(p.rect)
                tw.append((80, 120), chap, font=font, fontsize=16)
                tw.append((80, 170), f"这是第 {idx} 章的正文段落内容。", font=font, fontsize=11)
                tw.write_text(p)
            doc.save(src)

        out = self.root / "novel_kpw6.pdf"
        engine.build_kpw6_pdf(str(src), str(out), engine.ProcessOptions(mode="pure_text", cover_mode="smart_card"))
        with fitz.open(out) as doc:
            toc = doc.get_toc()
        titles = [item[1] for item in toc]
        self.assertIn("📖 书籍封面", titles)
        self.assertIn("第一章 启程", titles)
        self.assertIn("第二章 归途", titles)

    def test_wrap_cjk_line_keeps_english_words_intact(self):
        font = fitz.Font("cjk")
        sentence = "Reading Paperwhite Signature Edition architecture documents comfortably every afternoon."
        wrapped = engine._wrap_cjk_line(font, sentence, fontsize=13.5, max_width=220.0, indent_pt=27.0)
        self.assertGreater(len(wrapped), 2)
        rejoined = " ".join(line.strip() for line, _ in wrapped)
        for word in ("Reading", "Paperwhite", "Signature", "Edition", "architecture", "documents", "comfortably"):
            self.assertTrue(
                any(word in line for line, _ in wrapped),
                f"Word {word!r} was split across lines: {wrapped} (rejoined={rejoined!r})",
            )

    def test_preview_cover_follows_start_page_and_never_conflicts_with_skip(self):
        src = self.root / "multi.pdf"
        with fitz.open() as doc:
            for i in range(4):
                p = doc.new_page()
                p.insert_text((80, 200), f"Page {i + 1} content", fontsize=14)
            doc.save(src)

        # Skipping page 0 must return 0 screens on page 0 and move preview cover to page 1
        skip_opts = engine.ProcessOptions(mode="tech_book", cover_mode="smart_card", page_overrides={"0": "skip"})
        p0_res = engine.render_preview_comparison(str(src), 0, skip_opts)
        self.assertEqual(p0_res["kpw6_screens_count"], 0)
        self.assertFalse(p0_res["has_cover_screen"])
        p1_res = engine.render_preview_comparison(str(src), 1, skip_opts)
        self.assertTrue(p1_res["has_cover_screen"])

        # Setting start_page=3 must preview the cover on page index 2, not 0
        range_opts = engine.ProcessOptions(mode="tech_book", cover_mode="smart_card", start_page=3)
        r0 = engine.render_preview_comparison(str(src), 0, range_opts)
        r2 = engine.render_preview_comparison(str(src), 2, range_opts)
        self.assertFalse(r0["has_cover_screen"])
        self.assertTrue(r2["has_cover_screen"])

    def test_epub_and_cbz_screens_use_compact_4bit_png(self):
        src = self.root / "single.pdf"
        with fitz.open() as doc:
            p = doc.new_page()
            p.insert_text((80, 200), "Carta 1300 4-bit grayscale test", fontsize=16)
            doc.save(src)

        epub_out = self.root / "out.epub"
        engine.build_kpw6_epub(str(src), str(epub_out), engine.ProcessOptions(mode="tech_book", cover_mode="none"))
        with zipfile.ZipFile(epub_out) as zf:
            png = zf.read("OEBPS/images/screen_0001.png")
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(png[24], 4, "Expected 4-bit PNG screen encoding")
        with Image.open(io.BytesIO(png)) as im:
            self.assertEqual(im.size, (engine.KPW6_PX_W, engine.KPW6_PX_H))


class WebSecurityAndServiceAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        cls.temp = tempfile.TemporaryDirectory()
        cls.module_name = "_kpw6_audit_fixes_test_app"
        spec = importlib.util.spec_from_file_location(
            cls.module_name, SRC_ROOT / "kpw6_studio" / "server" / "web_app.py"
        )
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[cls.module_name] = cls.module
        with patch.dict(os.environ, {"KPW6_DATA_DIR": cls.temp.name}):
            spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        cls.module.EXPORT_PDF_SERVICE.cancel_current()
        sys.modules.pop(cls.module_name, None)
        cls.temp.cleanup()

    def setUp(self):
        self.run_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.run_dir.cleanup)
        root = Path(self.run_dir.name)
        up = root / "uploads"
        out = root / "outputs"
        up.mkdir()
        out.mkdir()
        demos = {k: v for k, v in self.module.FILES.items() if v.get("is_demo")}
        for p in (
            patch.object(self.module, "UPLOAD_DIR", str(up)),
            patch.object(self.module, "OUTPUT_DIR", str(out)),
            patch.object(self.module, "FILES", dict(demos)),
            patch.object(self.module, "CUSTOM_COVERS", {}),
            patch.object(self.module, "EXPORT_JOBS", OrderedDict()),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.client = self.module.app.test_client()

    def test_dns_rebinding_and_csrf_origins_are_blocked(self):
        # Non-loopback Host header (DNS rebinding)
        r_host = self.client.get("/api/files", headers={"Host": "evil.attacker.example:8765"})
        self.assertEqual(r_host.status_code, 403)

        # Cross-site Sec-Fetch-Site
        r_sec = self.client.post("/api/open_folder", json={}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r_sec.status_code, 403)

        # Cross-origin Origin and null Origin and wrong-port loopback Origin
        for bad_origin in ("https://evil.example", "null", "http://127.0.0.1:3000"):
            r_orig = self.client.post(
                "/api/preview",
                json={"file_id": "demo_tech_book"},
                headers={"Host": "127.0.0.1:8765", "Origin": bad_origin},
            )
            self.assertEqual(r_orig.status_code, 403, bad_origin)

        # Same-origin browser POST without X-KPW6-Token must be rejected
        r_no_tok = self.client.post(
            "/api/preview",
            json={"file_id": "demo_tech_book", "cover_mode": "none"},
            headers={"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(r_no_tok.status_code, 403)

        # Use the token assigned to the browser's public property in the served page.
        homepage = self.client.get("/", headers={"Host": "127.0.0.1:8765"})
        token_assignment = re.search(
            r'window\.__KPW6_CSRF_TOKEN__\s*=\s*"([A-Za-z0-9_-]+)"', homepage.get_data(as_text=True)
        )
        self.assertIsNotNone(token_assignment, "The served page must initialize the browser session token")
        self.assertTrue(homepage.cache_control.no_store)
        with self.client.get("/static/styles.css", headers={"Host": "127.0.0.1:8765"}) as r_css:
            self.assertEqual(r_css.status_code, 200)
        with self.client.get("/static/app.js", headers={"Host": "127.0.0.1:8765"}) as r_js:
            self.assertEqual(r_js.status_code, 200)
        r_ok = self.client.post(
            "/api/preview",
            json={"file_id": "demo_tech_book", "cover_mode": "none"},
            headers={
                "Host": "127.0.0.1:8765",
                "Origin": "http://127.0.0.1:8765",
                "Sec-Fetch-Site": "same-origin",
                "X-KPW6-Token": token_assignment.group(1),
            },
        )
        self.assertEqual(r_ok.status_code, 200)

    def test_strict_boolean_and_integer_validation(self):
        opts = self.module._parse_options({"split_pages": "false", "landscape_2x": "true"})
        self.assertFalse(opts.split_pages)
        self.assertTrue(opts.landscape_2x)

        for bad_payload in (
            {"split_pages": "not_a_bool"},
            {"page_num": None},
            {"page_num": True},
            {"page_num": "abc"},
            {"gamma_strength": True},
        ):
            res = self.client.post("/api/preview", json={"file_id": "demo_tech_book", **bad_payload})
            self.assertEqual(res.status_code, 400, bad_payload)

    def test_upload_delete_and_cover_recovery_and_job_eviction(self):
        # Create valid PDF & valid cover
        pdf_buf = io.BytesIO()
        with fitz.open() as doc:
            p = doc.new_page()
            p.insert_text((72, 120), "Deletable upload", fontsize=12)
            doc.save(pdf_buf)
        up_res = self.client.post("/api/upload", data={"file": (io.BytesIO(pdf_buf.getvalue()), "temp_doc.pdf")})
        self.assertEqual(up_res.status_code, 200)
        fid = up_res.get_json()["file_id"]

        cov_buf = io.BytesIO()
        Image.new("RGB", (40, 60), (20, 40, 80)).save(cov_buf, format="PNG")
        cov_res = self.client.post("/api/upload_cover", data={"file": (io.BytesIO(cov_buf.getvalue()), "cov.png")})
        self.assertEqual(cov_res.status_code, 200)
        cid = cov_res.get_json()["cover_id"]

        # Simulate restart by clearing CUSTOM_COVERS and rescanning UPLOAD_DIR
        self.module.CUSTOM_COVERS.clear()
        self.module._scan_uploads_dir()
        self.assertIn(cid, self.module.CUSTOM_COVERS)
        self.assertNotIn("cover", self.module.FILES)

        # Demo cannot be deleted; uploaded PDF can be deleted
        demo_del = self.client.post("/api/delete_file", json={"file_id": "demo_tech_book"})
        self.assertEqual(demo_del.status_code, 400)

        del_res = self.client.post("/api/delete_file", json={"file_id": fid})
        self.assertEqual(del_res.status_code, 200)
        self.assertNotIn(fid, self.module.FILES)

        # Bounded EXPORT_JOBS eviction
        for idx in range(self.module.MAX_EXPORT_JOBS + 20):
            self.module._set_export_job(f"job_{idx}", {"status": "done", "percent": 100})
        self.assertLessEqual(len(self.module.EXPORT_JOBS), self.module.MAX_EXPORT_JOBS)

    @unittest.skipIf(sys.platform == "win32", "POSIX shell sleep converter fixture")
    def test_active_export_does_not_block_preview_and_can_be_cancelled(self):
        bin_dir = Path(self.run_dir.name) / "bin"
        bin_dir.mkdir()
        started_flag = Path(self.run_dir.name) / "conv_started"
        fake_calibre = bin_dir / "ebook-convert"
        fake_calibre.write_text(
            "#!/usr/bin/env python3\nimport time\nfrom pathlib import Path\n"
            f"Path({str(started_flag)!r}).write_text('1')\n"
            "time.sleep(60)\n"
        )
        fake_calibre.chmod(0o755)

        # Restart EXPORT_PDF_SERVICE with updated PATH so child sees fake ebook-convert
        self.module.EXPORT_PDF_SERVICE.cancel_current()
        old_path = os.environ.get("PATH", "")
        with patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + old_path}):
            export_result = {}

            def run_slow_export():
                with self.module.app.test_client() as c:
                    r = c.post("/api/export_unified", json={
                        "file_id": "demo_tech_book",
                        "mode": "tech_book",
                        "export_format": "azw3",
                        "cover_mode": "none",
                        "job_id": "job_cancel_test",
                    })
                    export_result["status"] = r.status_code
                    export_result["json"] = r.get_json()

            t = threading.Thread(target=run_slow_export, daemon=True)
            t.start()
            deadline = time.monotonic() + 15.0
            while not started_flag.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(started_flag.exists(), "Slow converter did not start in time")

            # While EXPORT_PDF_SERVICE is blocked in fake ebook-convert, preview must respond promptly!
            t_p0 = time.monotonic()
            prev_res = self.client.post("/api/preview", json={
                "file_id": "demo_academic_2col", "mode": "academic_2col", "cover_mode": "none"
            })
            self.assertEqual(prev_res.status_code, 200)
            self.assertLess(time.monotonic() - t_p0, 8.0)

            # Cancel the active export and ensure it finishes cleanly and subsequent exports still work
            cancel_res = self.client.post("/api/cancel_export", json={"job_id": "job_cancel_test"})
            self.assertEqual(cancel_res.status_code, 200)
            self.assertTrue(cancel_res.get_json()["cancelled"])
            t.join(timeout=8.0)
            self.assertFalse(t.is_alive())
            self.assertTrue(export_result["json"].get("cancelled"), export_result)

        # Subsequent normal PDF export recovers automatically with a fresh worker
        followup = self.client.post("/api/export_unified", json={
            "file_id": "demo_tech_book", "mode": "tech_book", "export_format": "pdf", "cover_mode": "none"
        })
        self.assertEqual(followup.status_code, 200)


class PackagingSlimAndGuideTests(unittest.TestCase):
    def test_pruning_and_guide_generation(self):
        repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo_root / "packaging"))
        import release_support
        import make_user_guide

        toc = [
            ("pymupdf/layout/resources/onnx/model.onnx", "/tmp/model.onnx", "DATA"),
            ("pymupdf/mupdf-devel/include/mupdf/fitz.h", "/tmp/fitz.h", "DATA"),
            ("psutil-7.2.2.dist-info/METADATA", "/tmp/METADATA", "DATA"),
            ("pymupdf/_mupdf.so", "/tmp/_mupdf.so", "BINARY"),
        ]
        pruned = release_support.prune_pyinstaller_toc(toc)
        self.assertEqual(pruned, [("pymupdf/_mupdf.so", "/tmp/_mupdf.so", "BINARY")])

        with tempfile.TemporaryDirectory() as td:
            backend = Path(td) / "server"
            (backend / "_internal" / "pymupdf" / "layout").mkdir(parents=True)
            (backend / "_internal" / "pymupdf" / "mupdf-devel").mkdir(parents=True)
            (backend / "_internal" / "psutil-7.2.2.dist-info").mkdir(parents=True)
            (backend / "_internal" / "pymupdf" / "keep.py").write_text("x = 1\n")
            release_support.prune_frozen_backend(backend)
            self.assertFalse((backend / "_internal" / "pymupdf" / "layout").exists())
            self.assertFalse((backend / "_internal" / "pymupdf" / "mupdf-devel").exists())
            self.assertFalse((backend / "_internal" / "psutil-7.2.2.dist-info").exists())
            self.assertTrue((backend / "_internal" / "pymupdf" / "keep.py").is_file())

            guide_pdf = Path(td) / "Guide.pdf"
            make_user_guide.generate_user_guide(guide_pdf)
            with fitz.open(guide_pdf) as doc:
                self.assertEqual(len(doc), 3)
                full_text = "\n".join(p.get_text() for p in doc)
                self.assertIn("KPW6 PDF Studio.app", full_text)
                self.assertNotIn("Kindle精排大师.app", full_text)

    def test_hdiutil_retry_handles_transient_resource_busy(self):
        repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo_root / "packaging" / "macos"))
        import build_dmg

        class _FakeProc:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        with tempfile.TemporaryDirectory() as td:
            partial_dmg = Path(td) / "partial.dmg"
            partial_dmg.write_bytes(b"stale")
            calls = []

            def fake_run(cmd, text=True, capture_output=True):
                calls.append(partial_dmg.exists())
                if len(calls) == 1:
                    partial_dmg.write_bytes(b"half")
                    return _FakeProc(1, stderr="hdiutil: create failed - Resource busy\n")
                partial_dmg.write_bytes(b"complete")
                return _FakeProc(0)

            with patch.object(build_dmg.subprocess, "run", side_effect=fake_run), patch.object(build_dmg.time, "sleep"):
                build_dmg._run_hdiutil_with_retry(["hdiutil", "create"], cleanup_path=partial_dmg, attempts=3)
            self.assertEqual(calls, [False, False])
            self.assertEqual(partial_dmg.read_bytes(), b"complete")


if __name__ == "__main__":
    unittest.main()
