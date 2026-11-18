"""Core KPW6 canvas constants, options/slice dataclasses, and XML/PNG utilities.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

from dataclasses import dataclass, field
import html
import io
import re
from typing import Dict, List, Optional

import numpy as np
from PIL import Image
import pymupdf as fitz

_XML_ILLEGAL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


_EINK_16_PALETTE = [v for i in range(16) for v in (i * 17, i * 17, i * 17)]


_MAX_CACHED_PDF_BYTES = 80 * 1024 * 1024


def _xml_clean(text: str) -> str:
    if not text:
        return ""
    return _XML_ILLEGAL_CHARS_RE.sub("", str(text))


def _xml_escape(text: str) -> str:
    return html.escape(_xml_clean(text))


def _encode_eink_png_bytes(gray_samples: bytes, width: int, height: int) -> bytes:
    """Encode an 8-bit grayscale buffer as a compact 4-bit (16-level) grayscale PNG matching Carta 1300."""
    arr = np.frombuffer(gray_samples, dtype=np.uint8).reshape((height, width))
    q_idx = ((arr.astype(np.uint16) * 15 + 127) // 255).astype(np.uint8)
    im = Image.fromarray(q_idx, mode="P")
    im.putpalette(_EINK_16_PALETTE)
    buf = io.BytesIO()
    im.save(buf, format="PNG", bits=4, compress_level=6)
    return buf.getvalue()


KPW6_PX_W = 1264


KPW6_PX_H = 1680


KPW6_ASPECT = KPW6_PX_H / KPW6_PX_W  # ~1.3291139


KPW6_PT_W = 360.0


KPW6_PT_H = round(KPW6_PT_W * KPW6_ASPECT, 2)  # 478.48 pt


@dataclass
class SliceBox:
    page_num: int
    rect: fitz.Rect  # in source PDF coordinates
    kind: str = "body"  # "full", "col_left", "col_right", "body", "manga_r", "manga_l", "figure_rot"
    out_page_idx: int = 0
    rotate: int = 0  # 0 or +/-90 for restoring sideways figures to upright portrait


@dataclass
class ProcessOptions:
    # 5 Complete Modes:
    # "academic_2col" | "tech_book" | "scanned_book" | "comic_manga" | "pure_text"
    mode: str = "academic_2col"
    split_pages: bool = True          # Smart slice & reflow to KPW6 height
    landscape_2x: bool = False        # Rotate 90 deg & split for 2x large print on 1-col books
    trim_header_footer: bool = True
    enhance_contrast: bool = False    # Auto-enabled for scanned_book / comic_manga
    bleach_threshold: float = 0.88    # Paper background bleaching strength (eliminates KPW6 dithering grid)
    gamma_strength: float = 0.55      # Text darkening gamma (< 1.0 darkens ink)
    margin_Percent: float = 2.5       # Clean breathing margin on KPW6 screen (%)
    # Super-Resolution & Stroke Boldness Controls:
    dpi_preset: int = 450             # 300 (Standard), 450 (1.5x Super-Sampled Recommended), 600 (2.0x Print-Grade Ultra-HD)
    vector_lossless: bool = True      # Keep native vector fonts/curves 100% lossless vector paths when possible
    stroke_boldness: int = 0          # 0 (Original), 1 (+25% Repair Broken Strokes), 2 (+55% Medium Bold), 3 (+100% Heavy Bold)
    sharpen_strength: float = 1.60    # Edge sharpening multiplier (0.0x to 2.5x -> 0% to 250% UnsharpMask)
    # Comic / Manga specific options:
    manga_rtl: bool = True            # True = Japanese Manga Right-to-Left double-spread split order
    comic_split_spread: bool = True   # Auto-split landscape double-page spreads into 2 portrait KPW6 screens
    comic_full_bleed: bool = True     # Stretch/fill KPW6 screen with 0% margin
    # Pure Text PDF specific options:
    reflow_font_size: float = 13.5    # Font size (pt on 360pt KPW6 canvas) for Pure Text reflow preview/PDF
    line_spacing: float = 1.65        # Line spacing multiplier for Pure Text reflow
    # Kindle Cover Generation options:
    cover_mode: str = "smart_card"    # "smart_card" | "first_page" | "custom_image" | "none"
    custom_cover_title: str = ""      # Optional user-specified book title for cover card & metadata
    custom_cover_author: str = ""     # Optional user-specified author/subtitle for cover card & metadata
    custom_cover_path: Optional[str] = None
    # Scan Cleanup, Watermark & Deskew options:
    clean_spine_shadow: bool = True   # Auto-crop book spine binding shadows, scanner dark borders & punch holes
    remove_watermark: bool = False    # Bleach light/mid-gray diagonal watermarks, stamps & isolated dust speckles
    auto_deskew: bool = False         # Auto-detect & straighten tilted scanned pages (-3.5 to +3.5 deg)
    # Page Range, Volume Splitting & Per-Page Special Rules:
    start_page: int = 1               # 1-based start page (inclusive)
    end_page: Optional[int] = None    # 1-based end page (inclusive; None or 0 = last page)
    split_volumes_every: int = 0      # 0 = no volume split; >0 (e.g. 50/100/150) = auto split into multi-volumes
    page_overrides: Dict[str, str] = field(default_factory=dict)  # {"0": "whole_portrait"|"whole_landscape"|"skip"}
    explicit_pages: Optional[List[int]] = None  # Internal override used when building individual volumes
    max_pages: Optional[int] = None


def get_target_page_list(total_pages: int, options: ProcessOptions) -> List[int]:
    """
    Compute the exact ordered list of 0-based source page indices to export,
    respecting start_page, end_page, max_pages, explicit_pages, and per-page 'skip' overrides.
    """
    if total_pages <= 0:
        return []
    if options.explicit_pages is not None:
        base_pages = [p for p in options.explicit_pages if 0 <= p < total_pages]
    else:
        limit = min(total_pages, options.max_pages) if options.max_pages else total_pages
        if limit <= 0:
            return []
        s0 = max(0, (options.start_page or 1) - 1)
        e0 = min(limit - 1, (options.end_page - 1) if (options.end_page and options.end_page > 0) else (limit - 1))
        if s0 >= limit:
            raise ValueError(f"起始页 {s0 + 1} 超出文档页数（共 {limit} 页）。")
        if e0 < s0:
            raise ValueError("结束页不能早于起始页，请检查导出页码范围。")
        base_pages = list(range(s0, e0 + 1))

    overrides = options.page_overrides or {}
    return [p for p in base_pages if overrides.get(str(p)) != "skip"]


CHAPTER_REGEX = re.compile(
    r"^\s*(第[一二三四五六七八九十百千万零0-9]+[章节回篇卷部集]|Chapter\s+(?:\d+|[IVXLC]+)\b|序言|前言|引言|楔子|尾声|后记|附录)",
    re.IGNORECASE,
)


PAGE_NUM_REGEX = re.compile(r"^\s*[-—–~·•]*\s*(?:Page\s*)?\d+\s*[-—–~·•]*\s*$", re.IGNORECASE)


TERMINAL_PUNCT = set("。！？；：…”’）》.!?;:\"'”’)]}")


@dataclass
class ExtractedParagraph:
    kind: str  # "chapter" or "para"
    text: str
    src_page: int
    bbox: Optional[fitz.Rect] = None
