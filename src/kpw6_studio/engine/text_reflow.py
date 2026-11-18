"""Structured paragraph/chapter extraction, word-boundary CJK/Latin wrapping, and text reflow.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pymupdf as fitz

from kpw6_studio.engine.models import (
    CHAPTER_REGEX,
    ExtractedParagraph,
    KPW6_PT_H,
    KPW6_PT_W,
    PAGE_NUM_REGEX,
    ProcessOptions,
    TERMINAL_PUNCT,
    _xml_clean,
)

_MARGIN_EXCLUSIONS_CACHE: "OrderedDict[Tuple[str, int, bytes], Dict[int, set]]" = OrderedDict()


def _running_margin_exclusions(doc: fitz.Document) -> Dict[int, set]:
    """Conservatively identify isolated, repeated running text and page numbers.

    Margin position alone is not evidence: continuous body lines in these bands
    are retained. Non-numeric running text must repeat on at least two pages.
    Cache once per source document, not once per page extraction.
    """
    cached = getattr(doc, "_kpw6_margin_exclusions", None)
    if cached is not None:
        return cached
    cache_key = getattr(doc, "_kpw6_cache_key", None)
    if cache_key is not None and cache_key in _MARGIN_EXCLUSIONS_CACHE:
        _MARGIN_EXCLUSIONS_CACHE.move_to_end(cache_key)
        excluded = _MARGIN_EXCLUSIONS_CACHE[cache_key]
        doc._kpw6_margin_exclusions = excluded
        return excluded
    candidates = []
    occurrences: Dict[Tuple[str, str], set] = {}
    for pno, page in enumerate(doc):
        lines = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                txt = " ".join(_xml_clean("".join(s.get("text", "") for s in line.get("spans", []))).split())
                if txt:
                    size = max((s.get("size", 11) for s in line.get("spans", [])), default=11)
                    lines.append((txt, fitz.Rect(line["bbox"]), size))
        for txt, rect, size in lines:
            band = "top" if rect.y1 < page.rect.height * .12 else ("bottom" if rect.y0 > page.rect.height * .88 else "")
            if not band:
                continue
            gaps = [(other.y0 - rect.y1) if band == "top" else (rect.y0 - other.y1)
                    for _, other, _ in lines
                    if (other.y0 >= rect.y1 if band == "top" else other.y1 <= rect.y0)]
            if not gaps or min(gaps) < max(8.0, size * .9):
                continue
            key = (band, txt)
            occurrences.setdefault(key, set()).add(pno)
            candidates.append((pno, key))
    excluded: Dict[int, set] = {}
    for pno, key in candidates:
        if PAGE_NUM_REGEX.fullmatch(key[1]) or len(occurrences[key]) >= 2:
            excluded.setdefault(pno, set()).add(key)
    doc._kpw6_margin_exclusions = excluded
    if cache_key is not None:
        _MARGIN_EXCLUSIONS_CACHE[cache_key] = excluded
        while len(_MARGIN_EXCLUSIONS_CACHE) > 8:
            _MARGIN_EXCLUSIONS_CACHE.popitem(last=False)
    return excluded


def _join_wrapped_text(left: str, right: str) -> str:
    """Join line/page wraps without guessing whether a printed hyphen is soft."""
    if not left or not right:
        return left + right
    if left.endswith("\u00ad"):
        return left[:-1] + right  # Explicit discretionary hyphen only.
    if left.endswith("-"):
        return left + right  # Retain ambiguous printed/hard hyphens.
    if left[-1].isascii() and right[0].isascii():
        if left[-1] in "([{" or right[0] in ",.!?;:)]}":
            return left + right
        return left + " " + right
    return left + right


def extract_structured_text_from_page(page: fitz.Page, pno: int, trim_hf: bool = True) -> List[ExtractedParagraph]:
    """
    Extract lines from a single PDF page, filter out running headers/footers/page numbers,
    detect chapter headings, and merge hard-wrapped lines into complete natural paragraphs.
    """
    h = page.rect.height
    w = page.rect.width
    d = page.get_text("dict")
    blocks = d.get("blocks", [])

    raw_lines: List[Dict[str, Any]] = []
    font_sizes: List[float] = []
    exclusions = _running_margin_exclusions(page.parent).get(page.number, set()) if trim_hf else set()

    for b in blocks:
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            lbbox = fitz.Rect(line["bbox"])
            spans = line.get("spans", [])
            line_text = _xml_clean("".join(s.get("text", "") for s in spans)).strip()
            if not line_text:
                continue
            max_sz = max((float(s.get("size", 11.0)) for s in spans), default=11.0)

            # Remove only isolated, evidenced running text; never a fixed band.
            if trim_hf:
                band = "top" if lbbox.y1 < h * .12 else ("bottom" if lbbox.y0 > h * .88 else "")
                if (band, " ".join(line_text.split())) in exclusions:
                    continue

            font_sizes.append(max_sz)
            raw_lines.append({
                "text": line_text,
                "size": max_sz,
                "bbox": lbbox,
            })

    if not raw_lines:
        return []

    median_size = float(np.median(font_sizes)) if font_sizes else 11.0
    body_left_x = min(l["bbox"].x0 for l in raw_lines)
    body_right_x = max(l["bbox"].x1 for l in raw_lines)
    body_span_w = max(50.0, body_right_x - body_left_x)

    items: List[ExtractedParagraph] = []
    curr_para_text = ""
    curr_para_bbox: Optional[fitz.Rect] = None

    def flush_para():
        nonlocal curr_para_text, curr_para_bbox
        if curr_para_text.strip():
            items.append(ExtractedParagraph(kind="para", text=curr_para_text.strip(), src_page=pno, bbox=curr_para_bbox))
        curr_para_text = ""
        curr_para_bbox = None

    for idx, l in enumerate(raw_lines):
        txt = l["text"]
        sz = l["size"]
        lb = l["bbox"]

        is_chapter = bool(CHAPTER_REGEX.match(txt)) or (sz >= median_size * 1.25 and len(txt) < 45)
        if is_chapter:
            flush_para()
            items.append(ExtractedParagraph(kind="chapter", text=txt, src_page=pno, bbox=lb))
            continue

        # Check if this line starts a new paragraph (indented left edge or vertical gap from previous line)
        starts_new_para = False
        if idx > 0:
            prev_lb = raw_lines[idx - 1]["bbox"]
            line_h = max(8.0, lb.height)
            v_gap = lb.y0 - prev_lb.y1
            is_indented = (lb.x0 - body_left_x) >= max(12.0, sz * 1.4)
            prev_was_short = (prev_lb.x1 - body_left_x) < body_span_w * 0.78
            prev_ended_punct = bool(curr_para_text and curr_para_text[-1] in TERMINAL_PUNCT)

            if v_gap > line_h * 0.85 or is_indented or (prev_was_short and prev_ended_punct):
                starts_new_para = True

        if starts_new_para:
            flush_para()

        if not curr_para_text:
            curr_para_text = txt
            curr_para_bbox = fitz.Rect(lb)
        else:
            curr_para_text = _join_wrapped_text(curr_para_text, txt)
            if curr_para_bbox:
                curr_para_bbox |= lb

    flush_para()
    return items


def _is_latin_word_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "_'/")


def _wrap_cjk_line(
    font: fitz.Font,
    text: str,
    fontsize: float,
    max_width: float,
    indent_pt: float = 0.0,
) -> List[Tuple[str, float]]:
    """Greedy line wrapper supporting mixed CJK and word-boundary Latin text with hanging punctuation protection."""
    lines: List[Tuple[str, float]] = []
    curr_str = ""
    curr_indent = indent_pt
    curr_limit = max(40.0, max_width - curr_indent)
    no_start_punct = set("，。！？；：、）》”’.,!?;:)]}")

    for ch in text:
        candidate = curr_str + ch
        w = font.text_length(candidate, fontsize=fontsize)
        if w > curr_limit and len(curr_str) > 0:
            # Avoid starting a new line with closing punctuation
            if ch in no_start_punct and w <= curr_limit + fontsize * 0.85:
                lines.append((candidate, curr_indent))
                curr_str = ""
                curr_indent = 0.0
                curr_limit = max_width
                continue
            if ch == " ":
                lines.append((curr_str, curr_indent))
                curr_str = ""
                curr_indent = 0.0
                curr_limit = max_width
                continue
            # Prefer wrapping at whitespace/hyphen/CJK boundary instead of bisecting an English word
            if _is_latin_word_char(ch) and _is_latin_word_char(curr_str[-1]):
                split_pos = len(curr_str) - 1
                while split_pos > 0 and _is_latin_word_char(curr_str[split_pos - 1]):
                    split_pos -= 1
                prefix = curr_str[:split_pos].rstrip(" ")
                if prefix and font.text_length(prefix, fontsize=fontsize) >= curr_limit * 0.28:
                    lines.append((prefix, curr_indent))
                    curr_str = curr_str[split_pos:] + ch
                    curr_indent = 0.0
                    curr_limit = max_width
                    continue
            lines.append((curr_str, curr_indent))
            curr_str = ch.lstrip(" ")
            curr_indent = 0.0
            curr_limit = max_width
        else:
            if not curr_str and ch == " " and lines:
                continue
            curr_str = candidate

    if curr_str:
        lines.append((curr_str, curr_indent))
    return lines


def _render_pure_text_items_to_kpw6_doc(
    items: List[ExtractedParagraph],
    out_doc: fitz.Document,
    options: ProcessOptions,
    page_map: Optional[Dict[int, int]] = None,
) -> List[List[Any]]:
    """
    Typeset extracted pure-text paragraphs & chapter headings directly onto KPW6 vector pages
    with CJK indentation (2em), user-chosen font size, and clean line spacing.
    Returns detected PDF TOC entries ([level, title, 1-based_out_page]).
    """
    font = fitz.Font("cjk")
    margin_pt = max(12.0, KPW6_PT_W * (options.margin_Percent / 100.0))
    avail_w = KPW6_PT_W - 2.0 * margin_pt
    y_top = margin_pt + 6.0
    y_bot = KPW6_PT_H - margin_pt - 6.0

    fsize = max(9.0, min(24.0, options.reflow_font_size))
    line_h = fsize * options.line_spacing
    chap_size = fsize * 1.32

    curr_page: Optional[fitz.Page] = None
    curr_y = y_top
    tw: Optional[fitz.TextWriter] = None
    chapter_toc: List[List[Any]] = []

    def start_new_page():
        nonlocal curr_page, curr_y, tw
        if curr_page is not None and tw is not None:
            tw.write_text(curr_page)
        curr_page = out_doc.new_page(width=KPW6_PT_W, height=KPW6_PT_H)
        tw = fitz.TextWriter(curr_page.rect)
        curr_y = y_top

    start_new_page()

    for item in items:
        if item.kind == "chapter":
            if curr_y > y_top + 30.0 and (curr_y + chap_size * 2.5 > y_bot):
                start_new_page()
            elif curr_y > y_top + 5.0:
                curr_y += line_h * 0.6

            # Wrap chapter title lines
            ch_lines = _wrap_cjk_line(font, item.text, chap_size, avail_w, indent_pt=0.0)
            recorded_chap = False
            for cl, x_indent in ch_lines:
                if curr_y + chap_size * 1.4 > y_bot:
                    start_new_page()
                if not recorded_chap:
                    recorded_chap = True
                    out_page_1 = len(out_doc)
                    clean_ch = _xml_clean(item.text).strip()
                    if clean_ch:
                        chapter_toc.append([1, clean_ch, out_page_1])
                    if page_map is not None and item.src_page not in page_map:
                        page_map[item.src_page] = out_page_1 - 1
                tw.append((margin_pt + x_indent, curr_y + chap_size), cl, font=font, fontsize=chap_size)
                curr_y += chap_size * 1.45
            curr_y += line_h * 0.35
        else:
            # Body paragraph with 2em first-line indent
            indent_pt = fsize * 2.0
            p_lines = _wrap_cjk_line(font, item.text, fsize, avail_w, indent_pt=indent_pt)
            for pl, x_indent in p_lines:
                if curr_y + line_h > y_bot:
                    start_new_page()
                if page_map is not None and item.src_page not in page_map:
                    page_map[item.src_page] = len(out_doc) - 1
                tw.append((margin_pt + x_indent, curr_y + fsize), pl, font=font, fontsize=fsize)
                curr_y += line_h
            curr_y += fsize * 0.38  # Paragraph spacing

    if curr_page is not None and tw is not None:
        tw.write_text(curr_page)
    return chapter_toc
