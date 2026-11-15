"""Release audit regressions. All fixtures are generated locally."""
import hashlib
import io
from pathlib import Path
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pymupdf as fitz
from PIL import Image

import pdf_engine as engine
import sample_generator


class ReleaseEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def options(self, **kw):
        return engine.ProcessOptions(**dict(dict(mode="tech_book", cover_mode="none",
            trim_header_footer=False, clean_spine_shadow=False), **kw))

    def make_text(self, pages=1):
        path = self.root / "input.pdf"
        with fitz.open() as doc:
            for i in range(pages):
                p = doc.new_page(width=595, height=842)
                p.insert_text((70, 150), f"SOURCE PAGE {i + 1} body text", fontsize=12)
            doc.save(path)
        return path

    def epub_text(self, path):
        with zipfile.ZipFile(path) as z:
            return "\n".join(z.read(n).decode() for n in z.namelist() if "chapter_" in n)

    def test_f01_visible_annotations_and_fields_preserve_vectors_and_source(self):
        src = self.root / "annotated.pdf"
        with fitz.open() as doc:
            p = doc.new_page(width=595, height=842)
            p.insert_text((80, 160), "BODY_TOKEN", fontsize=20)
            p.add_freetext_annot(fitz.Rect(80, 220, 450, 260), "ANNOT_TOKEN", fontsize=20).update()
            w = fitz.Widget()
            w.field_name, w.field_type, w.field_value = "value", fitz.PDF_WIDGET_TYPE_TEXT, "FIELD_TOKEN"
            w.rect, w.text_font, w.text_fontsize = fitz.Rect(80, 300, 450, 345), "Helv", 20
            p.add_widget(w)
            p.add_ink_annot([[(80, 400), (250, 430), (400, 400)]]).update()
            doc.save(src)
        digest = hashlib.sha256(src.read_bytes()).digest()
        out = self.root / "annotated-out.pdf"
        engine.build_kpw6_pdf(str(src), str(out), self.options(page_overrides={"0": "whole_portrait"}))
        with fitz.open(out) as doc:
            text = doc[0].get_text()
            self.assertIn("ANNOT_TOKEN", text)
            self.assertIn("FIELD_TOKEN", text)
            self.assertEqual(doc[0].get_images(), [], "Bake appearances without rasterizing native text")
            self.assertGreater(len(doc[0].get_drawings()), 0, "Ink appearance must survive")
        self.assertEqual(digest, hashlib.sha256(src.read_bytes()).digest())

    def test_f01_annotation_only_body_remains_visible_in_all_formats(self):
        src = self.root / "only-annotation.pdf"
        with fitz.open() as doc:
            p = doc.new_page()
            p.add_freetext_annot(fitz.Rect(80, 220, 450, 260), "VISIBLE_TOKEN", fontsize=20).update()
            doc.save(src)
        for fmt, build in (("pdf", engine.build_kpw6_pdf), ("epub", engine.build_kpw6_epub), ("cbz", engine.build_kpw6_cbz)):
            with self.subTest(format=fmt):
                out = self.root / f"body.{fmt}"
                build(str(src), str(out), self.options())
                if fmt == "pdf":
                    with fitz.open(out) as doc:
                        arr = np.frombuffer(doc[0].get_pixmap(colorspace=fitz.csGRAY).samples, dtype=np.uint8)
                else:
                    with zipfile.ZipFile(out) as z:
                        name = "OEBPS/images/screen_0001.png" if fmt == "epub" else "0001.png"
                        arr = np.asarray(Image.open(io.BytesIO(z.read(name))).convert("L"))
                self.assertGreater(int((arr < 240).sum()), 100)

    def test_f01_baked_appearance_matches_source_for_rotated_cropped_pages(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                src = self.root / f"bake-{rotation}.pdf"
                with fitz.open() as doc:
                    p = doc.new_page(width=595, height=842)
                    p.insert_text((80, 180), "NATIVE VECTOR TEXT", fontsize=16)
                    p.add_freetext_annot(fitz.Rect(90, 250, 480, 290), "ANNOT APPEARANCE", fontsize=20).update()
                    p.add_ink_annot([[(100, 400), (250, 450), (450, 400)]]).update()
                    p.set_cropbox(fitz.Rect(50, 100, 545, 742))
                    p.set_rotation(rotation)
                    doc.save(src)
                digest = hashlib.sha256(src.read_bytes()).digest()
                with fitz.open(src) as doc:
                    before = doc[0].get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
                with engine._open_source_pdf(str(src)) as doc:
                    after = doc[0].get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
                    self.assertIsNone(doc[0].first_annot)
                    self.assertEqual(doc[0].get_images(), [])
                self.assertEqual((before.width, before.height), (after.width, after.height))
                a = np.frombuffer(before.samples, dtype=np.uint8).astype(float)
                b = np.frombuffer(after.samples, dtype=np.uint8).astype(float)
                self.assertLess(np.abs(a-b).mean(), .2)
                self.assertEqual(digest, hashlib.sha256(src.read_bytes()).digest())

    def test_f02_continuous_body_inside_margin_bands_is_not_deleted(self):
        src = self.root / "body.pdf"
        with fitz.open() as doc:
            for n in range(2):
                p = doc.new_page(width=595, height=842)
                for i in range(48):
                    p.insert_text((50, 50 + i*16), f"PAGE{n+1} BODY LINE {i+1:02d}: Unique continuous prose.", fontsize=10)
            doc.save(src)
        out = self.root / "body.epub"
        engine.build_kpw6_epub(str(src), str(out), self.options(mode="pure_text", trim_header_footer=True))
        tokens = re.findall(r"PAGE[12] BODY LINE \d{2}", self.epub_text(out))
        self.assertEqual(len(set(tokens)), 96)

    def test_f02_repeated_isolated_headers_and_page_numbers_are_removed(self):
        src = self.root / "headers.pdf"
        with fitz.open() as doc:
            for n in range(3):
                p = doc.new_page(width=595, height=842)
                p.insert_text((50, 45), "REPEATED BOOK HEADER", fontsize=9)
                p.insert_text((50, 150), f"UNIQUE BODY {n}", fontsize=11)
                p.insert_text((280, 805), str(n+1), fontsize=9)
            doc.save(src)
        with fitz.open(src) as doc:
            text = " ".join(x.text for n,p in enumerate(doc) for x in engine.extract_structured_text_from_page(p,n,True))
        self.assertNotIn("REPEATED BOOK HEADER", text)
        self.assertIn("UNIQUE BODY 2", text)
        self.assertNotIn("HEADER", text)

    def test_f04_epub_has_utc_modification_metadata(self):
        src = self.make_text()
        for reflow in (False, True):
            out = self.root / f"modified-{reflow}.epub"
            engine.build_kpw6_epub(str(src), str(out), self.options(), force_reflow=reflow)
            with zipfile.ZipFile(out) as z:
                opf = ET.fromstring(z.read("OEBPS/content.opf"))
            ns = {"o": "http://www.idpf.org/2007/opf"}
            modified = opf.findall("o:metadata/o:meta[@property='dcterms:modified']", ns)
            self.assertEqual(len(modified), 1)
            self.assertRegex(modified[0].text, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_f05_both_sideways_text_directions_become_upright(self):
        for angle in (90, 270):
            with self.subTest(angle=angle):
                src = self.root / f"side-{angle}.pdf"
                with fitz.open() as doc:
                    p = doc.new_page(width=595, height=842)
                    for i in range(13):
                        p.insert_text((80+34*i, 330 if angle == 90 else 160), f"COLUMN{i:02d}: Sideways content", fontsize=10, rotate=angle)
                    doc.save(src)
                out = self.root / f"upright-{angle}.pdf"
                engine.build_kpw6_pdf(str(src), str(out), self.options())
                with fitz.open(out) as doc:
                    directions = [tuple(l["dir"]) for p in doc for b in p.get_text("dict")["blocks"] if b["type"] == 0 for l in b["lines"]]
                self.assertTrue(directions)
                self.assertEqual(set(directions), {(1.0, 0.0)})

    def test_f06_english_punctuation_spacing_and_real_hyphens_survive(self):
        src = self.root / "english.pdf"
        with fitz.open() as doc:
            p = doc.new_page()
            for text,y in (("We tried one method,",160),("then confirmed the result.",175),
                           ("This is cost-",240),("effective for everyone.",255)):
                p.insert_text((70,y),text,fontsize=11)
            doc.save(src)
        out = self.root / "english.epub"
        engine.build_kpw6_epub(str(src),str(out),self.options(mode="pure_text"))
        text = self.epub_text(out)
        self.assertIn("method, then", text)
        self.assertIn("cost-effective", text)

    def test_f06_closing_quote_spacing_and_explicit_soft_hyphen(self):
        src = self.root / "quoted.pdf"
        with fitz.open() as doc:
            p = doc.new_page()
            p.insert_text((70, 160), 'The response was "ready"', fontsize=11)
            p.insert_text((70, 175), 'and work continued.', fontsize=11)
            tw = fitz.TextWriter(p.rect)
            tw.append((70, 240), 'co\u00ad', font=fitz.Font('cjk'), fontsize=11)
            tw.append((70, 255), 'operate safely.', font=fitz.Font('cjk'), fontsize=11)
            tw.write_text(p)
            doc.save(src)
        out = self.root / "quoted.epub"
        engine.build_kpw6_epub(str(src), str(out), self.options(mode="pure_text"))
        text = self.epub_text(out)
        self.assertIn('ready&quot; and', text)
        self.assertIn('cooperate safely.', text)

    def test_f07_partial_pdf_toc_promotes_surviving_nested_entries(self):
        src = self.make_text(4)
        with fitz.open(src) as doc:
            doc.set_toc([[1,"Chapter",1],[2,"Section",2],[3,"Subsection",3],[4,"Detail",4]])
            doc.save(str(self.root / "toc.pdf"))
        for cover in ("none", "smart_card"):
            out = self.root / f"toc-{cover}.pdf"
            engine.build_kpw6_pdf(str(self.root / "toc.pdf"),str(out),self.options(start_page=3,cover_mode=cover))
            with fitz.open(out) as doc: toc=doc.get_toc()
            self.assertEqual([x[1] for x in toc if x[1] != "📖 书籍封面"],["Subsection","Detail"])
            self.assertEqual([x[0] for x in toc if x[1] != "📖 书籍封面"],[1,2])

    def test_f08_none_omits_visible_cover_in_epub_and_cbz(self):
        src=self.make_text()
        for reflow in (False,True):
            out=self.root/f"none-{reflow}.epub"
            engine.build_kpw6_epub(str(src),str(out),self.options(),force_reflow=reflow)
            with zipfile.ZipFile(out) as z:
                self.assertNotIn("OEBPS/cover.xhtml",z.namelist())
                self.assertNotIn("cover-page",z.read("OEBPS/content.opf").decode())
        out=self.root/"none.cbz"
        engine.build_kpw6_cbz(str(src),str(out),self.options())
        with zipfile.ZipFile(out) as z:
            self.assertNotIn("0000_cover.jpg",z.namelist())
            info=ET.fromstring(z.read("ComicInfo.xml"))
            self.assertEqual(int(info.findtext("PageCount")),len([n for n in z.namelist() if n.endswith(".png")]))

    def test_f09_lower_gamma_darkens_ink_without_erasing_gray_strokes(self):
        a=np.full((300,600),255,dtype=np.uint8)
        for i,value in enumerate((160,180,190,200,210,220)): a[100:200,20+95*i:80+95*i]=value
        original=Image.fromarray(a)
        normal=engine.enhance_eink_image(original,gamma=.9,sharpen_strength=0)
        strong=engine.enhance_eink_image(original,gamma=.3,sharpen_strength=0)
        for i,value in enumerate((160,180,190,200,210,220)):
            point=(50+95*i,150)
            self.assertLess(strong.getpixel(point),normal.getpixel(point))
            self.assertLess(strong.getpixel(point),value)

    def test_f10_cjk_chapters_without_a_space_are_recognized(self):
        src=self.root/"cjk.pdf"
        with fitz.open() as doc:
            font=fitz.Font("cjk")
            for title in ("第一章研究方法","第二章结果讨论"):
                p=doc.new_page(); tw=fitz.TextWriter(p.rect)
                tw.append((70,150),title,font=font,fontsize=11)
                tw.append((70,180),"这是正文内容。",font=font,fontsize=11); tw.write_text(p)
            doc.save(src)
        out=self.root/"cjk.epub"
        stats=engine.build_kpw6_epub(str(src),str(out),self.options(mode="pure_text"))
        self.assertEqual(stats["chapters_count"],2)

    def test_f11_highlights_cover_the_whole_book_and_more_than_45_entries(self):
        src=self.root/"long.pdf"
        with fitz.open() as doc:
            for i in range(401):
                p=doc.new_page()
                if i < 50 or i == 400: p.insert_text((50,150),f"Table {i+1}. Measurements",fontsize=12)
            doc.save(src)
        highlights=engine.scan_document_highlights(str(src))
        self.assertEqual(len(highlights),51)
        self.assertIn(400,[x["page_num"] for x in highlights])

    def test_f12_demo_academic_paragraphs_are_actually_present(self):
        paths=sample_generator.generate_demo_pdfs(str(self.root/"demos"))
        with fitz.open(paths["academic_2col"]) as doc:
            text=" ".join(" ".join(p.get_text().split()) for p in doc)
        for prefix in ("Modern electronic paper displays","As shown in Equation","During fast menu transitions"):
            self.assertIn(prefix,text)

    def test_pdf_producer_retains_library_and_agpl_notices(self):
        src=self.make_text(); out=self.root/"notices.pdf"
        engine.build_kpw6_pdf(str(src),str(out),self.options())
        with fitz.open(out) as doc: producer=doc.metadata.get("producer","")
        self.assertIn("PyMuPDF",producer)
        self.assertIn("AGPL",producer)


if __name__ == "__main__":
    unittest.main()
