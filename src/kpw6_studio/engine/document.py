"""Source PDF baking/rotation normalization cache, category classifier, metadata & highlights.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import os
import re
from typing import Any, Dict, List, Tuple

import numpy as np
import pymupdf as fitz

from kpw6_studio.engine.image_ops import (
    compute_ink_mask,
    find_tight_bbox,
    render_page_gray,
    strip_running_headers_footers,
)
from kpw6_studio.engine.layout import segment_two_column_zones
from kpw6_studio.engine.models import (
    PAGE_NUM_REGEX,
    ProcessOptions,
    _MAX_CACHED_PDF_BYTES,
    _xml_clean,
)

_SOURCE_PDF_CACHE: "OrderedDict[Tuple[str, int, bytes], bytes]" = OrderedDict()


_HIGHLIGHTS_DOC_CACHE: "OrderedDict[Tuple[str, int, bytes], List[Dict[str, Any]]]" = OrderedDict()


def _bake_and_normalize_doc(doc: fitz.Document) -> None:
    # Flatten visible appearances into this in-memory document before any
    # clipping/rotation. show_pdf_page() does not copy annotations or fields.
    # bake() retains their vector appearances; the input file is never saved.
    doc.bake(annots=True, widgets=True)
    for page in doc:
        if page.rotation:
            # remove_rotation() works on MediaBox coordinates. Temporarily
            # expose that full box, then restore the transformed visible crop.
            # Otherwise a 90/270 degree turn resets CropBox and reveals margins.
            media = page.mediabox
            crop = page.cropbox
            page.set_cropbox(fitz.Rect(media.x0, 0, media.x1, media.height))
            visible = fitz.Rect(crop.x0 - media.x0, crop.y0,
                                crop.x1 - media.x0, crop.y1) * page.rotation_matrix
            page.remove_rotation()
            origin = page.cropbox_position
            page.set_cropbox(fitz.Rect(visible.x0 + origin.x, visible.y0 + origin.y,
                                      visible.x1 + origin.x, visible.y1 + origin.y))


def _open_source_pdf(pdf_path: str) -> fitz.Document:
    """Normalize rotation in memory so rendering, text and vector clips share coordinates.

    The source file is never saved or modified. Keep the visual rotation in the
    content stream instead of /Rotate, as required for clipped show_pdf_page().
    Caches the normalized byte stream for repeated single-page previews.
    """
    abs_path = os.path.abspath(pdf_path)
    try:
        file_size = os.path.getsize(abs_path)
    except OSError:
        file_size = -1

    if 0 < file_size <= _MAX_CACHED_PDF_BYTES:
        with open(abs_path, "rb") as stream:
            raw_bytes = stream.read()
        cache_key = (abs_path, len(raw_bytes), hashlib.blake2b(raw_bytes, digest_size=16).digest())
        cached_bytes = _SOURCE_PDF_CACHE.get(cache_key)
        if cached_bytes is not None:
            _SOURCE_PDF_CACHE.move_to_end(cache_key)
            doc = fitz.open(stream=cached_bytes, filetype="pdf")
            doc._kpw6_cache_key = cache_key
            return doc

        doc = fitz.open(stream=raw_bytes, filetype="pdf")
        try:
            _bake_and_normalize_doc(doc)
            normalized = doc.tobytes(garbage=0, deflate=False)
            if len(normalized) <= _MAX_CACHED_PDF_BYTES:
                _SOURCE_PDF_CACHE[cache_key] = normalized
                while len(_SOURCE_PDF_CACHE) > 2 or sum(len(v) for v in _SOURCE_PDF_CACHE.values()) > 128 * 1024 * 1024:
                    _SOURCE_PDF_CACHE.popitem(last=False)
            doc._kpw6_cache_key = cache_key
        except Exception:
            doc.close()
            raise
        return doc

    doc = fitz.open(abs_path)
    try:
        _bake_and_normalize_doc(doc)
    except Exception:
        doc.close()
        raise
    return doc


def detect_pdf_category(pdf_path: str) -> Dict[str, Any]:
    """
    Automatically inspect a PDF file and classify it into one of the 5 modes:
    - academic_2col (双栏学术论文)
    - tech_book     (带图表的专业书)
    - scanned_book  (扫描版书籍)
    - comic_manga   (PDF漫画 / 画集)
    - pure_text     (纯文字 PDF 书籍)
    """
    doc = _open_source_pdf(pdf_path)
    total = len(doc)
    if total == 0:
        doc.close()
        return {"recommended_mode": "tech_book", "reason": "空文档"}

    sample_indices = sorted(list({0, min(1, total - 1), total // 2, min(total - 1, 3)}))
    total_chars = 0
    landscape_pages = 0
    two_col_votes = 0
    vector_drawings_count = 0
    image_coverage_sum = 0.0

    for idx in sample_indices:
        p = doc[idx]
        w, h = p.rect.width, p.rect.height
        page_area = max(1.0, w * h)
        if w > h * 1.06:
            landscape_pages += 1

        txt = p.get_text("text").strip()
        total_chars += len(txt)

        drawings = p.get_drawings()
        vector_drawings_count += len(drawings)

        # Check if page is dominated by a full-page raster scan/comic image (even if it has an invisible OCR text layer!)
        has_full_page_raster = False
        try:
            for im_info in p.get_image_info(hashes=False):
                ib = im_info.get("bbox", (0, 0, 0, 0))
                im_area = max(0.0, (ib[2] - ib[0]) * (ib[3] - ib[1]))
                if im_area >= page_area * 0.62:
                    has_full_page_raster = True
                    break
        except Exception:
            pass

        imgs = p.get_images(full=True)
        if has_full_page_raster or (imgs and len(txt) < 40):
            image_coverage_sum += 1.0

        # Check 2-column structure if there is native text (and not a single-column scan)
        if len(txt) > 120 and not has_full_page_raster:
            gray, _, sy = render_page_gray(p, dpi=100)
            ink = compute_ink_mask(gray, is_scanned=False)
            y_t, y_b = strip_running_headers_footers(ink)
            bbox = find_tight_bbox(ink, y_t, y_b)
            if bbox:
                zones = segment_two_column_zones(ink, bbox, page=p, sy=sy)
                if any(z[0] in ("col_left", "col_right") for z in zones):
                    two_col_votes += 1

    avg_chars = total_chars / len(sample_indices)
    doc.close()

    if image_coverage_sum >= len(sample_indices) / 2 or avg_chars < 35:
        if landscape_pages >= len(sample_indices) / 2:
            return {
                "recommended_mode": "comic_manga",
                "reason": "检测到整页画幅且包含横版双页跨页大图，自动匹配【PDF 漫画/画集模式】",
            }
        return {
            "recommended_mode": "scanned_book",
            "reason": "检测到页面由整页扫描图像构成（含或不含隐藏 OCR 层），自动匹配【扫描版书籍去网格高清模式】",
        }

    if landscape_pages >= len(sample_indices) / 2 and avg_chars < 450:
        return {
            "recommended_mode": "comic_manga",
            "reason": "检测到横版双页跨页画幅，自动匹配【PDF 漫画/画集模式】",
        }

    if two_col_votes >= max(1, len(sample_indices) // 2):
        return {
            "recommended_mode": "academic_2col",
            "reason": "检测到明显的左右双栏排版与中缝空白，自动匹配【双栏学术论文模式】",
        }

    if vector_drawings_count / len(sample_indices) >= 6:
        return {
            "recommended_mode": "tech_book",
            "reason": "检测到单栏正文混合大量矢量图表/表格，自动匹配【带图表的专业书模式】",
        }

    return {
        "recommended_mode": "pure_text",
        "reason": "检测到纯文字单栏排版，自动匹配【纯文字 PDF ➔ 原生 EPUB / 大字重排模式】",
    }


def extract_book_meta(
    src_doc: fitz.Document,
    input_pdf_path: str,
    options: ProcessOptions,
) -> Tuple[str, str]:
    """
    Extract or synthesize a clean (title, author/subtitle) pair for the KPW6 cover & metadata.
    """
    raw_fname = os.path.splitext(os.path.basename(input_pdf_path))[0]
    clean_fname = re.sub(r"^[0-9a-f]{8}_", "", raw_fname)

    detected_title = ""
    detected_author = ""
    junk_titles = {"no job name", "untitled", "document", "default", "unknown", "none", "null", "print"}
    try:
        meta = src_doc.metadata or {}
        m_title = (meta.get("title") or "").strip()
        m_author = (meta.get("author") or "").strip()
        m_low = m_title.lower()
        if (
            m_title
            and len(m_title) >= 5
            and m_low not in junk_titles
            and not m_low.startswith(("microsoft word", "untitled", "no job", "arxiv:", "doi:"))
            and not m_low.endswith((".pdf", ".doc", ".docx", ".dvi", ".tex", ".ps", ".indd"))
        ):
            detected_title = m_title
        if m_author and len(m_author) >= 2 and m_author.lower() not in junk_titles:
            detected_author = m_author
    except Exception:
        pass

    # Inspect largest font lines in the top 45% of Page 1 if title is still empty (skip for noisy OCR raster scans)
    if not detected_title and len(src_doc) > 0:
        try:
            p0 = src_doc[0]
            h0 = max(1.0, p0.rect.height)
            p0_area = max(1.0, p0.rect.width * h0)
            is_p0_scan = any(
                max(0.0, im.get("bbox", (0, 0, 0, 0))[2] - im.get("bbox", (0, 0, 0, 0))[0])
                * max(0.0, im.get("bbox", (0, 0, 0, 0))[3] - im.get("bbox", (0, 0, 0, 0))[1])
                >= p0_area * 0.58
                for im in p0.get_image_info(hashes=False)
            )
            if not is_p0_scan:
                td = p0.get_text("dict")
                span_candidates: List[Tuple[float, float, str]] = []
                for b in td.get("blocks", []):
                    if b.get("type") != 0:
                        continue
                    for line in b.get("lines", []):
                        lb = line.get("bbox", (0, 0, 0, 0))
                        if lb[1] > h0 * 0.45 or lb[1] < h0 * 0.04:
                            continue
                        l_txt = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in l_txt)
                        min_len = 4 if has_cjk else 15
                        if len(l_txt) < min_len or PAGE_NUM_REGEX.match(l_txt):
                            continue
                        max_sz = max((float(s.get("size", 10.0)) for s in line.get("spans", [])), default=10.0)
                        if max_sz >= 11.5:
                            span_candidates.append((round(max_sz, 1), lb[1], l_txt))
                if span_candidates:
                    top_sz = max(c[0] for c in span_candidates)
                    title_lines = [c[2] for c in sorted(span_candidates, key=lambda x: x[1]) if c[0] >= top_sz - 1.0]
                    joined = " ".join(title_lines[:3]).strip()
                    if len(joined) >= 4:
                        detected_title = f"{clean_fname} · {joined}" if clean_fname.lower() not in joined.lower() else joined
                        detected_title = detected_title[:115]
        except Exception:
            pass

    final_title = _xml_clean(options.custom_cover_title.strip() or detected_title or clean_fname).strip() or clean_fname
    mode_badge = {
        "academic_2col": "双栏学术论文 · KPW6 专享重排",
        "tech_book": "专业图表书籍 · KPW6 专享重排",
        "scanned_book": "Carta 1300 去网格高清重制版",
        "comic_manga": "KPW6 满屏高清漫画/画集",
        "pure_text": "KPW6 原生精排电子书",
    }.get(options.mode, "KPW6 Signature Edition 专享版")
    final_author = _xml_clean(options.custom_cover_author.strip() or detected_author or mode_badge).strip() or mode_badge
    return final_title, final_author


def _caption_sort_key(tok: str) -> Tuple[int, int, str]:
    m = re.match(r"(\d+)", tok)
    if m:
        return (0, int(m.group(1)), tok)
    rom = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    val = 0
    prev = 0
    for ch in reversed(tok.upper()):
        cur = rom.get(ch, 0)
        val = val - cur if cur < prev else val + cur
        prev = cur
    return (1, val, tok)


def scan_document_highlights(input_pdf_path: str) -> List[Dict[str, Any]]:
    """
    Quickly scan a PDF for pages containing actual Table captions, Figure captions, or Chapter headings
    so the Web Studio can display 1-click quick-jump pills in the preview toolbar.
    """
    highlights: List[Dict[str, Any]] = []
    try:
        doc = _open_source_pdf(input_pdf_path)
        cache_key = getattr(doc, "_kpw6_cache_key", None)
        if cache_key is not None and cache_key in _HIGHLIGHTS_DOC_CACHE:
            _HIGHLIGHTS_DOC_CACHE.move_to_end(cache_key)
            cached_hl = [dict(item) for item in _HIGHLIGHTS_DOC_CACHE[cache_key]]
            doc.close()
            return cached_hl

        total = len(doc)
        scan_limit = total
        table_cap_re = re.compile(
            r"(?:^|\n)\s*(?:Table\s+(\d+[A-Za-z]?|[IVXLCDM]+)(?:\s*[.\-—:：]|\s*\n|\s*$)|TABLE\s+(\d+[A-Za-z]?|[IVXLCDM]+)(?:\s*[.\-—:：]|\s*\n|\s*$))",
            re.MULTILINE,
        )
        fig_cap_re = re.compile(
            r"(?:^|\n)\s*(?:(?:Figure|Fig\.)\s+(\d+[A-Za-z]?|[IVXLCDM]+)(?:\s*[.\-—:：]|\s*\n|\s*$)|FIGURE\s+(\d+[A-Za-z]?|[IVXLCDM]+)(?:\s*[.\-—:：]|\s*\n|\s*$))",
            re.MULTILINE,
        )

        for pno in range(scan_limit):
            page = doc[pno]
            txt = page.get_text("text")
            t_matches = [m.group(1) or m.group(2) for m in table_cap_re.finditer(txt)]
            f_matches = [m.group(1) or m.group(2) for m in fig_cap_re.finditer(txt)]
            tables = sorted(set(x for x in t_matches if x), key=_caption_sort_key)
            figs = sorted(set(x for x in f_matches if x), key=_caption_sort_key)

            if tables:
                t_str = f"Table {','.join(tables[:3])}"
                if highlights and highlights[-1].get("raw_tag") == t_str:
                    highlights[-1]["end_page"] = pno
                    highlights[-1]["label"] = f"P{highlights[-1]['page_num']+1}-{pno+1} {t_str}"
                else:
                    highlights.append({
                        "page_num": pno,
                        "end_page": pno,
                        "kind": "table",
                        "raw_tag": t_str,
                        "label": f"P{pno+1} {t_str}",
                    })
            elif figs:
                f_str = f"Fig {figs[0]}" if len(figs) == 1 else f"Fig {figs[0]}-{figs[-1]}"
                highlights.append({
                    "page_num": pno,
                    "end_page": pno,
                    "kind": "figure",
                    "raw_tag": f_str,
                    "label": f"P{pno+1} {f_str}",
                })
        if cache_key is not None:
            _HIGHLIGHTS_DOC_CACHE[cache_key] = [dict(item) for item in highlights]
            while len(_HIGHLIGHTS_DOC_CACHE) > 8:
                _HIGHLIGHTS_DOC_CACHE.popitem(last=False)
        doc.close()
    except Exception:
        pass
    return highlights
