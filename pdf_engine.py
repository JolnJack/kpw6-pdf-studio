import base64
import html
import io
import math
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any, Callable

import pymupdf as fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

# KPW6 Signature Edition (12th Gen, 7-inch Carta 1300)
# Physical resolution: 1264 x 1680 px (300 PPI) -> aspect ratio 1 : 1.32911
KPW6_PX_W = 1264
KPW6_PX_H = 1680
KPW6_ASPECT = KPW6_PX_H / KPW6_PX_W  # ~1.3291139

# Standard PDF canvas in points
KPW6_PT_W = 360.0
KPW6_PT_H = round(KPW6_PT_W * KPW6_ASPECT, 2)  # 478.48 pt


@dataclass
class SliceBox:
    page_num: int
    rect: fitz.Rect  # in source PDF coordinates
    kind: str = "body"  # "full", "col_left", "col_right", "body", "manga_r", "manga_l", "figure_rot"
    out_page_idx: int = 0
    rotate: int = 0  # 0 or -90 (270) for restoring sideways figures to upright portrait


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


def _open_source_pdf(pdf_path: str) -> fitz.Document:
    """Normalize rotation in memory so rendering, text and vector clips share coordinates.

    The source file is never saved or modified. Keep the visual rotation in the
    content stream instead of /Rotate, as required for clipped show_pdf_page().
    """
    doc = fitz.open(pdf_path)
    try:
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


def render_page_gray(page: fitz.Page, dpi: int = 150) -> Tuple[np.ndarray, float, float]:
    """Render a PDF page to a grayscale numpy array (H, W) and return scale factors (px per pt)."""
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    scale_x = pix.width / page.rect.width
    scale_y = pix.height / page.rect.height
    return arr, scale_x, scale_y


def compute_ink_mask(
    gray: np.ndarray,
    is_scanned: bool = False,
    clean_spine_shadow: bool = True,
    remove_watermark: bool = False,
) -> np.ndarray:
    """
    Compute binary ink mask (True = ink/content, False = paper background)
    with small-speckle morphological filtering, optional watermark thresholding,
    and automatic book-spine shadow / punch-hole margin trimming.
    """
    if remove_watermark:
        bg_val = float(np.percentile(gray, 85))
        thresh = max(125.0, min(195.0, bg_val * 0.74))
    elif is_scanned:
        bg_val = float(np.percentile(gray, 85))
        thresh = max(140.0, min(235.0, bg_val * 0.86))
    else:
        thresh = 238.0

    raw_ink = gray < thresh

    padded = np.pad(raw_ink.astype(np.uint8), 1, mode="constant", constant_values=0)
    neighborhood_sum = (
        padded[0:-2, 0:-2] + padded[0:-2, 1:-1] + padded[0:-2, 2:] +
        padded[1:-1, 0:-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:] +
        padded[2:,   0:-2] + padded[2:,   1:-1] + padded[2:,   2:]
    )
    min_neighbors = 3 if remove_watermark else 2
    clean_ink = raw_ink & (neighborhood_sum >= min_neighbors)

    h, w = clean_ink.shape
    if is_scanned or clean_spine_shadow:
        border_y = max(2, int(h * 0.016))
        border_x = max(2, int(w * 0.016))
        clean_ink[:border_y, :] = False
        clean_ink[-border_y:, :] = False
        clean_ink[:, :border_x] = False
        clean_ink[:, -border_x:] = False

    if clean_spine_shadow and w >= 120:
        # Detect outer left/right binding shadows, scanner dark bars, 3-ring punch holes, or stray edge specks.
        # Protect real content (e.g. TOC numbers "5. 6. 7." or margin labels) by verifying the isolated strip
        # actually looks like a dark border stripe/speck rather than sparse horizontal text lines.
        col_counts = clean_ink.sum(axis=0)
        empty_col = col_counts <= max(1, int(h * 0.004))
        hard_edge_x = max(border_x + 2, int(w * 0.038))
        soft_edge_x = int(w * 0.088)

        def _is_spine_or_noise_strip(strip_mask: np.ndarray, is_inner_band: bool) -> bool:
            ink_px = int(strip_mask.sum())
            if ink_px == 0:
                return False
            if not is_inner_band:
                return True
            row_has = np.any(strip_mask, axis=1)
            # Measure max contiguous vertical dark run (real spine shadows form tall vertical bands)
            max_v_run = 0
            cur_run = 0
            for v in row_has:
                if v:
                    cur_run += 1
                    if cur_run > max_v_run:
                        max_v_run = cur_run
                else:
                    cur_run = 0
            v_coverage = float(row_has.mean())
            is_tiny_speck = ink_px <= 20
            is_tall_stripe = (max_v_run >= int(h * 0.14)) or (v_coverage >= 0.30)
            return is_tiny_speck or is_tall_stripe

        # Check left margin band
        for x in range(soft_edge_x, border_x + 4, -1):
            if np.all(empty_col[max(border_x, x - 5):x + 1]):
                if _is_spine_or_noise_strip(clean_ink[:, border_x:x], is_inner_band=(x > hard_edge_x)):
                    clean_ink[:, :x] = False
                    break

        # Check right margin band
        for x in range(w - soft_edge_x, w - border_x - 4):
            if np.all(empty_col[x:min(w - border_x, x + 6)]):
                if _is_spine_or_noise_strip(clean_ink[:, x:w - border_x], is_inner_band=(x < w - hard_edge_x)):
                    clean_ink[:, x:] = False
                    break

    return clean_ink


def strip_running_headers_footers(ink: np.ndarray) -> Tuple[int, int]:
    """
    Detect and exclude isolated running headers (top 11%) and page numbers/footers (bottom 11%)
    that are separated by a clear horizontal whitespace gap from the main body.
    """
    h, w = ink.shape
    row_counts = ink.sum(axis=1)
    min_ink_pixels = max(2, int(w * 0.002))
    has_ink = row_counts >= min_ink_pixels

    runs: List[Tuple[int, int]] = []
    in_run = False
    start = 0
    for y in range(h):
        if has_ink[y] and not in_run:
            in_run = True
            start = y
        elif not has_ink[y] and in_run:
            in_run = False
            runs.append((start, y))
    if in_run:
        runs.append((start, h))

    if len(runs) <= 1:
        return 0, h

    y_top = 0
    y_bot = h

    first_s, first_e = runs[0]
    next_s, _ = runs[1]
    if first_e < int(h * 0.11) and (first_e - first_s) < int(h * 0.035) and (next_s - first_e) >= int(h * 0.012):
        y_top = next_s

    if len(runs) >= 2:
        last_s, last_e = runs[-1]
        _, prev_e = runs[-2]
        if last_s > int(h * 0.89) and (last_e - last_s) < int(h * 0.035) and (last_s - prev_e) >= int(h * 0.012):
            y_bot = prev_e

    return y_top, y_bot


def find_tight_bbox(ink: np.ndarray, y_min: int = 0, y_max: Optional[int] = None) -> Optional[Tuple[int, int, int, int]]:
    """Return (x0, y0, x1, y1) of ink within [y_min:y_max]."""
    h, w = ink.shape
    if y_max is None:
        y_max = h
    sub = ink[y_min:y_max, :]
    if not np.any(sub):
        return None

    rows = np.any(sub, axis=1)
    cols = np.any(sub, axis=0)
    y0_rel, y1_rel = np.where(rows)[0][[0, -1]]
    x0, x1 = np.where(cols)[0][[0, -1]]
    return int(x0), int(y_min + y0_rel), int(x1 + 1), int(y_min + y1_rel + 1)


def _longest_zero_run(arr_1d: np.ndarray) -> Tuple[int, int, int]:
    """Return (run_length, start_idx, mid_idx) of the longest contiguous zero run in a 1D array."""
    best_len = 0
    best_s = 0
    curr_len = 0
    curr_s = 0
    for i, val in enumerate(arr_1d):
        if val == 0:
            if curr_len == 0:
                curr_s = i
            curr_len += 1
            if curr_len > best_len:
                best_len = curr_len
                best_s = curr_s
        else:
            curr_len = 0
    return best_len, best_s, best_s + best_len // 2


def _analyze_rotated_sideways_page(
    page: fitz.Page,
    page_num: int,
    ink: np.ndarray,
    bbox: Tuple[int, int, int, int],
    sx: float,
    sy: float,
    pad_px: int,
) -> Optional[List[SliceBox]]:
    """
    Detect if a page consists predominantly of 90-degree rotated sideways figures or tables
    (e.g. full-page landscape tables like Table 9, or stacked sideways graphs like Figures 1-45).
    - If stacked sideways figures (separated by a clear horizontal blank gap near middle of page):
      Splits each figure cleanly and sets rotate=-90 so each figure stands 100% UPRIGHT and
      fills the entire KPW6 portrait screen!
    - If a full-page sideways table:
      Keeps the entire table 100% intact on a single KPW6 screen without chopping rows or columns.
    """
    pw, ph = page.rect.width, page.rect.height
    d = page.get_text("dict")
    rot_chars = 0
    horiz_chars = 0
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            lb = l.get("bbox", (0, 0, 0, 0))
            # Ignore running header (top 7.5%) and running footer (bottom 7.5%)
            if lb[3] < ph * 0.075 or lb[1] > ph * 0.925:
                continue
            txt = "".join(s.get("text", "") for s in l.get("spans", "")).strip()
            if not txt:
                continue
            dx, dy = l.get("dir", (1.0, 0.0))
            if abs(dy) > 0.5:
                rot_chars += len(txt)
            else:
                horiz_chars += len(txt)

    if rot_chars <= horiz_chars or rot_chars < 15:
        return None

    h_px, w_px = ink.shape
    bx0, by0, bx1, by1 = bbox
    bh = by1 - by0
    bw = bx1 - bx0

    # Check if there is a wide horizontal blank gap (>= 14 pt) inside [28%, 72%] of bbox height
    # separating two stacked sideways figures (and not crossed by vertical table border lines)
    sub = ink[by0:by1, bx0:bx1]
    row_ink = sub.sum(axis=1)
    mid_lo = int(bh * 0.28)
    mid_hi = int(bh * 0.72)

    min_fig_gap_px = max(20, int(14.0 * sy))
    best_gap_len = 0
    best_gap_mid = -1
    cur_len = 0
    cur_s = 0
    for y_rel in range(mid_lo, mid_hi):
        if row_ink[y_rel] == 0:
            if cur_len == 0:
                cur_s = y_rel
            cur_len += 1
            if cur_len > best_gap_len:
                best_gap_len = cur_len
                best_gap_mid = cur_s + cur_len // 2
        else:
            cur_len = 0

    if best_gap_len >= min_fig_gap_px:
        cut_y = by0 + best_gap_mid
        top_tb = find_tight_bbox(ink, by0, cut_y)
        bot_tb = find_tight_bbox(ink, cut_y, by1)
        if top_tb and bot_tb:
            # In 90-deg counter-clockwise rotated pages, check if each half is wider than tall (landscape band)
            top_w, top_h = top_tb[2] - top_tb[0], top_tb[3] - top_tb[1]
            bot_w, bot_h = bot_tb[2] - bot_tb[0], bot_tb[3] - bot_tb[1]
            if top_w > top_h * 1.10 and bot_w > bot_h * 1.10:
                # Order by Figure number if present, otherwise bottom-first (standard CCW page order) or top-first
                slices_out: List[SliceBox] = []
                for out_idx, tb in enumerate([top_tb, bot_tb]):
                    px0, py0 = max(0, tb[0] - pad_px), max(0, tb[1] - pad_px)
                    px1, py1 = min(w_px, tb[2] + pad_px), min(h_px, tb[3] + pad_px)
                    r = fitz.Rect(
                        page.rect.x0 + px0 / sx,
                        page.rect.y0 + py0 / sy,
                        page.rect.x0 + px1 / sx,
                        page.rect.y0 + py1 / sy,
                    )
                    slices_out.append(SliceBox(page_num=page_num, rect=r, kind="figure_rot", out_page_idx=out_idx, rotate=-90))
                return slices_out

    # Otherwise it is a single sideways figure or full-page sideways table (e.g. Table 9 on Pages 9-25)
    px0, py0 = max(0, bx0 - pad_px), max(0, by0 - pad_px)
    px1, py1 = min(w_px, bx1 + pad_px), min(h_px, by1 + pad_px)
    r = fitz.Rect(
        page.rect.x0 + px0 / sx,
        page.rect.y0 + py0 / sy,
        page.rect.x0 + px1 / sx,
        page.rect.y0 + py1 / sy,
    )
    # If it is a wide single sideways figure (width > height * 1.15), rotate=-90 so it fills the portrait KPW6 screen upright!
    rot_angle = -90 if (bw > bh * 1.15) else 0
    kind_tag = "figure_rot" if rot_angle != 0 else "full"
    return [SliceBox(page_num=page_num, rect=r, kind=kind_tag, out_page_idx=0, rotate=rot_angle)]


def _compute_protected_full_width_rows(
    page: fitz.Page,
    by0: int,
    by1: int,
    sy: float,
) -> np.ndarray:
    """
    Use native PDF text lines & vector drawings to identify vertical intervals (in pixel coords relative to by0)
    that belong to Full-Width Tables (e.g. Tables 1-6, 10, Table I), Spanning Figures, or Full-Width Titles/Abstracts,
    so they are NEVER split vertically into left/right columns!
    """
    bh = max(1, by1 - by0)
    is_full_row = np.zeros(bh, dtype=bool)
    pw, ph = page.rect.width, page.rect.height

    # Restrict to the stripped body vertical range so running headers/footers NEVER merge across 2-column prose
    body_top_pt = max(ph * 0.065, page.rect.y0 + by0 / sy - 1.5)
    body_bot_pt = min(ph * 0.92, page.rect.y0 + by1 / sy + 1.5)

    d_dict = page.get_text("dict")
    all_body_lines: List[Tuple[float, float, float, float]] = []
    for b in d_dict.get("blocks", []):
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            lx0, ly0, lx1, ly1 = l["bbox"]
            if ly1 < body_top_pt or ly0 > body_bot_pt:
                continue
            if (lx1 - lx0) > pw * 0.12:
                all_body_lines.append((lx0, ly0, lx1, ly1))

    if all_body_lines:
        content_x0 = min(x[0] for x in all_body_lines)
        content_x1 = max(x[2] for x in all_body_lines)
    else:
        content_x0 = pw * 0.08
        content_x1 = pw * 0.92
    content_w = max(pw * 0.45, content_x1 - content_x0)
    mid_x = (content_x0 + content_x1) / 2.0

    spanning_y_intervals: List[Tuple[float, float]] = []

    # 1. Check vector drawings for full-width horizontal table/section lines (width > content_w * 0.58)
    wide_rule_ys: List[float] = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r and r.width > content_w * 0.58 and r.height < 6.0 and body_top_pt < r.y0 < body_bot_pt:
            wide_rule_ys.append(r.y0)
            spanning_y_intervals.append((r.y0 - 14.0, r.y1 + 14.0))

    # 2. Inspect text blocks and their individual lines (handles asymmetric page margins cleanly via mid_x)
    for b in d_dict.get("blocks", []):
        if b.get("type") != 0:
            continue
        bx0, by0_pt, bx1, by1_pt = b.get("bbox", (0, 0, 0, 0))
        if by1_pt < body_top_pt or by0_pt > body_bot_pt:
            continue
        lines = [
            l for l in b.get("lines", [])
            if l["bbox"][3] >= body_top_pt and l["bbox"][1] <= body_bot_pt
        ]
        if not lines:
            continue
        block_txt = "\n".join(
            "".join(s.get("text", "") for s in l.get("spans", [])) for l in lines
        ).strip()
        if not block_txt:
            continue

        # Check individual lines that truly span across the central column gutter around mid_x
        span_lines = [
            l["bbox"] for l in lines
            if l["bbox"][0] < mid_x - content_w * 0.06 and l["bbox"][2] > mid_x + content_w * 0.06
        ]
        if span_lines:
            for lb in span_lines:
                spanning_y_intervals.append((lb[1] - 4.0, lb[3] + 4.0))
            continue

        # If no single line spans the gutter, check if this block is a multi-column Full-Width Table body
        if bx0 < mid_x - content_w * 0.08 and bx1 > mid_x + content_w * 0.08:
            mid_lines = [
                l for l in lines
                if max(l["bbox"][0], mid_x - content_w * 0.018) < min(l["bbox"][2], mid_x + content_w * 0.018)
            ]
            near_wide_rule = any(abs(by0_pt - ry) < 38.0 or (by0_pt <= ry <= by1_pt) for ry in wide_rule_ys)
            if (len(mid_lines) >= 2 and len(lines) >= 6) or near_wide_rule:
                spanning_y_intervals.append((by0_pt - 4.0, by1_pt + 4.0))
        elif re.match(r"^(Table|TABLE|Figure|Fig\.)\s+(\d+|[IVXLCDM]+)\b", block_txt):
            near_wide_rule = any(0.0 <= (ry - by0_pt) <= 45.0 for ry in wide_rule_ys)
            if near_wide_rule:
                spanning_y_intervals.append((by0_pt - 4.0, by1_pt + 35.0))

    # 3. Identify two-column prose intervals dynamically relative to mid_x (handles asymmetric margins!)
    left_prose: List[Tuple[float, float]] = []
    right_prose: List[Tuple[float, float]] = []
    for lx0, ly0, lx1, ly1 in all_body_lines:
        if lx1 < mid_x + content_w * 0.015 and lx0 < mid_x - content_w * 0.15 and (lx1 - lx0) > content_w * 0.25:
            left_prose.append((ly0, ly1))
        elif lx0 > mid_x - content_w * 0.015 and lx1 > mid_x + content_w * 0.15 and (lx1 - lx0) > content_w * 0.25:
            right_prose.append((ly0, ly1))

    if not spanning_y_intervals:
        return is_full_row

    spanning_y_intervals.sort(key=lambda iv: iv[0])
    merged: List[List[float]] = []
    for s0, s1 in spanning_y_intervals:
        if not merged:
            merged.append([s0, s1])
        else:
            prev_s0, prev_s1 = merged[-1]
            # Merge if overlapping OR if there is no 2-column prose in the gap [prev_s1, s0]
            gap_has_2col = any(
                (ly1 > prev_s1 + 2 and ly0 < s0 - 2) for (ly0, ly1) in left_prose
            ) and any(
                (ry1 > prev_s1 + 2 and ry0 < s0 - 2) for (ry0, ry1) in right_prose
            )
            if s0 <= prev_s1 + 18.0 or not gap_has_2col:
                merged[-1][1] = max(prev_s1, s1)
            else:
                merged.append([s0, s1])

    for s0, s1 in merged:
        py0 = max(0, int((s0 - page.rect.y0) * sy) - by0)
        py1 = min(bh, int((s1 - page.rect.y0) * sy) - by0)
        if py1 > py0:
            is_full_row[py0:py1] = True

    return is_full_row


def segment_two_column_zones(
    ink: np.ndarray,
    bbox: Tuple[int, int, int, int],
    page: Optional[fitz.Page] = None,
    sy: float = 2.0833,
) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    """
    XY-Cut layout analyzer for 2-column academic papers:
    Separates full-width zones (Title, Abstract, Full-Width Tables, Spanning Figures)
    from two-column body zones (Left Column, Right Column).
    """
    bx0, by0, bx1, by1 = bbox
    bw = bx1 - bx0
    bh = by1 - by0
    if bw < 120 or bh < 120:
        return [("full", bbox)]

    sub = ink[by0:by1, bx0:bx1]
    row_ink = sub.sum(axis=1)
    is_text_row = row_ink > max(1, int(bw * 0.0015))

    protected_full_row = (
        _compute_protected_full_width_rows(page, by0, by1, sy)
        if page is not None
        else np.zeros(bh, dtype=bool)
    )

    bands: List[Tuple[int, int]] = []
    in_band = False
    b_start = 0
    min_gap = max(3, int(bh * 0.004))

    y = 0
    while y < bh:
        if is_text_row[y]:
            if not in_band:
                in_band = True
                b_start = y
            y += 1
        else:
            if in_band:
                gap_end = y
                while gap_end < bh and not is_text_row[gap_end]:
                    gap_end += 1
                if (gap_end - y) >= min_gap:
                    bands.append((b_start, y))
                    in_band = False
                y = gap_end
            else:
                y += 1
    if in_band:
        bands.append((b_start, bh))

    if not bands:
        return [("full", bbox)]

    center_l = int(bw * 0.40)
    center_r = int(bw * 0.60)
    min_gutter_w = max(14, int(bw * 0.022))

    band_types: List[str] = []
    for ys, ye in bands:
        # If protected by vector table / spanning block detector, force "full"!
        if np.mean(protected_full_row[ys:ye]) >= 0.35:
            band_types.append("full")
            continue

        band_slice = sub[ys:ye, :]
        gutter_profile = band_slice[:, center_l:center_r].sum(axis=0)
        z_len, z_start, z_mid = _longest_zero_run(gutter_profile)

        left_ink = band_slice[:, :max(1, center_l)].sum()
        right_ink = band_slice[:, min(bw - 1, center_r):].sum()

        cols_with_ink = np.where(band_slice.sum(axis=0) > 0)[0]
        max_x_rel = int(cols_with_ink[-1]) if len(cols_with_ink) > 0 else bw
        min_x_rel = int(cols_with_ink[0]) if len(cols_with_ink) > 0 else 0

        if z_len >= min_gutter_w and left_ink > 0 and right_ink > 0:
            band_types.append("2col")
        elif z_len >= min_gutter_w and (max_x_rel <= int(bw * 0.48) or min_x_rel >= int(bw * 0.52)):
            band_types.append("2col_tail")
        else:
            band_types.append("full")

    has_true_2col = any(bt == "2col" for bt in band_types)
    band_types = [("2col" if (bt == "2col_tail" and has_true_2col) else ("full" if bt == "2col_tail" else bt)) for bt in band_types]

    merged_zones: List[List[Any]] = []
    for (ys, ye), btype in zip(bands, band_types):
        if not merged_zones:
            merged_zones.append([btype, ys, ye])
        else:
            prev_type, prev_s, prev_e = merged_zones[-1]
            if btype == prev_type:
                merged_zones[-1][2] = ye
            else:
                merged_zones.append([btype, ys, ye])

    for mz in merged_zones:
        if mz[0] == "2col" and (mz[2] - mz[1]) < int(bh * 0.08):
            mz[0] = "full"

    final_zones: List[List[Any]] = []
    for btype, ys, ye in merged_zones:
        if not final_zones or final_zones[-1][0] != btype:
            final_zones.append([btype, ys, ye])
        else:
            final_zones[-1][2] = ye

    result: List[Tuple[str, Tuple[int, int, int, int]]] = []
    for ztype, zs, ze in final_zones:
        abs_y0 = by0 + zs
        abs_y1 = by0 + ze
        if ztype == "full":
            tb = find_tight_bbox(ink, abs_y0, abs_y1)
            if tb:
                result.append(("full", tb))
        else:
            zone_sub = sub[zs:ze, :]
            col_profile = zone_sub[:, center_l:center_r].sum(axis=0)
            z_len, _, z_mid = _longest_zero_run(col_profile)
            if z_len < min_gutter_w // 2:
                tb = find_tight_bbox(ink, abs_y0, abs_y1)
                if tb:
                    result.append(("full", tb))
                continue

            abs_gx = bx0 + center_l + z_mid

            left_ink_mask = np.zeros_like(ink)
            left_ink_mask[abs_y0:abs_y1, bx0:abs_gx] = ink[abs_y0:abs_y1, bx0:abs_gx]
            tb_left = find_tight_bbox(left_ink_mask, abs_y0, abs_y1)
            if tb_left:
                result.append(("col_left", tb_left))

            right_ink_mask = np.zeros_like(ink)
            right_ink_mask[abs_y0:abs_y1, abs_gx:bx1] = ink[abs_y0:abs_y1, abs_gx:bx1]
            tb_right = find_tight_bbox(right_ink_mask, abs_y0, abs_y1)
            if tb_right:
                result.append(("col_right", tb_right))

    return result


def segment_comic_spread(
    ink: np.ndarray,
    bbox: Tuple[int, int, int, int],
    page_w_px: int,
    page_h_px: int,
    options: ProcessOptions,
) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    """
    Segment a Comic / Manga page:
    - If the page (or cropped artwork) is a double-page landscape spread (width > height * 1.05)
      and comic_split_spread is True:
      Locate the central binding gutter in [46%, 54%] of the spread and split into two portrait pages.
      Order them Right-then-Left (manga_rtl=True) or Left-then-Right (manga_rtl=False).
    - Otherwise return the tight-cropped single comic page.
    """
    bx0, by0, bx1, by1 = bbox
    bw = bx1 - bx0
    bh = by1 - by0

    is_spread = (page_w_px > page_h_px * 1.05) or (bw > bh * 1.08)
    if not (options.comic_split_spread and is_spread):
        return [("body", bbox)]

    # Search for cleanest vertical split line in [46%, 54%] of bw
    g_lo = int(bw * 0.46)
    g_hi = int(bw * 0.54)
    if g_hi > g_lo + 4:
        col_ink = ink[by0:by1, bx0 + g_lo:bx0 + g_hi].sum(axis=0)
        zero_cols = np.where(col_ink == 0)[0]
        if len(zero_cols) > 0:
            split_rel = int(np.median(zero_cols)) + g_lo
        else:
            split_rel = int(np.argmin(col_ink)) + g_lo
    else:
        split_rel = bw // 2

    split_x = bx0 + split_rel

    # Tighten Left Page and Right Page bounding boxes individually
    left_mask = np.zeros_like(ink)
    left_mask[by0:by1, bx0:split_x] = ink[by0:by1, bx0:split_x]
    tb_left = find_tight_bbox(left_mask, by0, by1) or (bx0, by0, split_x, by1)

    right_mask = np.zeros_like(ink)
    right_mask[by0:by1, split_x:bx1] = ink[by0:by1, split_x:bx1]
    tb_right = find_tight_bbox(right_mask, by0, by1) or (split_x, by0, bx1, by1)

    if options.manga_rtl:
        # Japanese / HK Manga: Read Right Page (#1) -> Left Page (#2)
        return [("manga_r", tb_right), ("manga_l", tb_left)]
    else:
        # Western Comic / Manhua: Read Left Page (#1) -> Right Page (#2)
        return [("manga_l", tb_left), ("manga_r", tb_right)]


def find_safe_cut_in_range(
    ink: np.ndarray,
    x0: int,
    y_start: int,
    x1: int,
    y_limit: int,
    min_cut_y: int,
    max_cut_y: int,
) -> Optional[int]:
    """
    Find the best horizontal whitespace gap inside [x0:x1] between min_cut_y and max_cut_y.
    Prefers wider paragraph/figure gaps over thin line gaps, and never cuts through text/figures
    if a blank row exists.
    """
    max_cut_y = min(y_limit, max_cut_y)
    if max_cut_y <= min_cut_y + 4:
        return None

    bw = max(1, x1 - x0)
    sub = ink[y_start:y_limit, x0:x1]
    row_ink = sub.sum(axis=1)
    is_blank = row_ink <= max(1, int(bw * 0.001))

    rel_min = max(0, min_cut_y - y_start)
    rel_max = min(len(row_ink), max_cut_y - y_start)

    gaps: List[Tuple[int, int, int, int]] = []
    in_gap = False
    g_start = 0
    for y in range(len(row_ink)):
        if is_blank[y] and not in_gap:
            in_gap = True
            g_start = y
        elif not is_blank[y] and in_gap:
            in_gap = False
            g_mid = (g_start + y) // 2
            gaps.append((g_start, y, g_mid, y - g_start))
    if in_gap:
        gaps.append((g_start, len(row_ink), (g_start + len(row_ink)) // 2, len(row_ink) - g_start))

    candidates = [g for g in gaps if rel_min <= g[2] <= rel_max and g[3] >= 2]
    if candidates:
        span = max(1.0, float(rel_max - rel_min))
        def gap_score(g: Tuple[int, int, int, int]) -> float:
            _, _, g_mid, g_h = g
            pos_ratio = (g_mid - rel_min) / span
            width_bonus = min(g_h, 24) * 2.5
            pos_bonus = pos_ratio * 22.0
            return width_bonus + pos_bonus

        best = max(candidates, key=gap_score)
        return y_start + best[2]

    return None


def _deskew_pil_image(pil_img: Image.Image) -> Image.Image:
    """
    Fast Projection-Profile Automatic Deskew (-3.5 to +3.5 degrees):
    Finds the rotation angle that maximizes horizontal row-projection variance of ink pixels,
    straightening tilted scan pages on KPW6's 300-PPI grid.
    """
    w, h = pil_img.size
    if w < 100 or h < 100:
        return pil_img

    target_w = 360
    scale = target_w / float(w)
    small = pil_img.resize((target_w, max(60, int(h * scale))), resample=Image.Resampling.BILINEAR)

    def eval_angle(ang: float) -> float:
        rot = small.rotate(ang, resample=Image.Resampling.NEAREST, fillcolor=255) if abs(ang) > 0.01 else small
        arr = np.asarray(rot, dtype=np.uint8)
        # Crop outer 6% to avoid border artifacts
        my, mx = int(arr.shape[0] * 0.06), int(arr.shape[1] * 0.06)
        core = arr[my:-my, mx:-mx] if (arr.shape[0] > 20 and arr.shape[1] > 20) else arr
        row_sums = (core < 195).sum(axis=1, dtype=np.float32)
        return float(np.var(row_sums))

    base_score = eval_angle(0.0)
    if base_score < 1.0:
        return pil_img

    coarse_angles = [-3.0, -2.2, -1.5, -0.9, -0.45, 0.0, 0.45, 0.9, 1.5, 2.2, 3.0]
    best_ang = max(coarse_angles, key=eval_angle)

    fine_angles = [best_ang - 0.3, best_ang - 0.15, best_ang, best_ang + 0.15, best_ang + 0.3]
    best_ang = max(fine_angles, key=eval_angle)
    best_score = eval_angle(best_ang)

    if abs(best_ang) >= 0.15 and best_score > base_score * 1.03:
        return pil_img.rotate(best_ang, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
    return pil_img


def enhance_eink_image(
    pil_img: Image.Image,
    bleach_percentile: float = 0.88,
    gamma: float = 0.55,
    is_comic: bool = False,
    stroke_boldness: int = 0,
    sharpen_strength: float = 1.60,
    clean_spine_shadow: bool = True,
    remove_watermark: bool = False,
    auto_deskew: bool = False,
) -> Image.Image:
    """
    Carta 1300 E-Ink De-Dithering, Stroke Emboldening, Watermark/Spine-Shadow Cleaner & Deskew Enhancer:
    - Preserves fine high-DPI typography and thin engineering diagram lines without blur.
    - Accurately estimates ink black-point from actual ink pixels.
    - Supports 4 levels of morphological stroke emboldening (0..3).
    - Supports automatic projection-profile deskew, spine-shadow bleaching, and light-gray watermark removal.
    - Bleaches off-white/gray paper backgrounds to pure #FFFFFF (255) to eliminate KPW6 dithering grids.
    """
    gray = pil_img.convert("L")
    if auto_deskew and not is_comic:
        gray = _deskew_pil_image(gray)

    arr = np.asarray(gray, dtype=np.float32)

    if is_comic:
        white_clip = float(np.percentile(arr, 92.0))
        black_clip = float(np.percentile(arr, 3.0))
        white_clip = max(210.0, white_clip)
        black_clip = min(45.0, black_clip)
        norm = np.clip((arr - black_clip) / max(10.0, white_clip - black_clip), 0.0, 1.0)
        curved = np.power(norm, max(0.65, gamma))
    else:
        bg_level = float(np.percentile(arr, bleach_percentile * 100.0))
        white_factor = 0.82 if remove_watermark else 0.94
        white_clip = max(162.0 if remove_watermark else 180.0, bg_level * white_factor)

        # Estimate true ink black-point from actual dark ink pixels (robust even on sparse diagrams with <1% ink)
        ink_pixels = arr[arr < white_clip * 0.80]
        if len(ink_pixels) >= 32:
            black_clip = float(np.percentile(ink_pixels, 22.0))
            black_clip = max(18.0, min(95.0, black_clip))
        else:
            black_clip = 35.0

        if white_clip <= black_clip + 20.0:
            white_clip = 235.0 if remove_watermark else 242.0
            black_clip = 35.0

        norm = np.clip((arr - black_clip) / (white_clip - black_clip), 0.0, 1.0)
        curved = np.power(norm, gamma)

    # Remove light-gray watermark remnants & isolated single-pixel scan dust speckles
    if remove_watermark and not is_comic:
        curved[curved > 0.64] = 1.0
        dark_mask = (curved < 0.72).astype(np.uint8)
        pad_d = np.pad(dark_mask, 1, mode="constant", constant_values=0)
        nb_sum = (
            pad_d[:-2, :-2] + pad_d[:-2, 1:-1] + pad_d[:-2, 2:] +
            pad_d[1:-1, :-2] + pad_d[1:-1, 1:-1] + pad_d[1:-1, 2:] +
            pad_d[2:, :-2] + pad_d[2:, 1:-1] + pad_d[2:, 2:]
        )
        curved[nb_sum <= 1] = 1.0

    # Apply morphological stroke emboldening in normalized [0, 1] luminance space (0 = black ink, 1 = white paper)
    if stroke_boldness > 0:
        pad = np.pad(curved, 1, mode="edge")
        min_4 = np.minimum.reduce([
            curved,
            pad[:-2, 1:-1],
            pad[2:, 1:-1],
            pad[1:-1, :-2],
            pad[1:-1, 2:],
        ])
        if stroke_boldness == 1:
            # Level 1: Micro-embolden (+25%) - repairs broken 1-bit CCITT fax scan lines & thin serifs
            curved = curved * 0.58 + min_4 * 0.42
        elif stroke_boldness == 2:
            # Level 2: Medium E-Ink Bold (+55%) - crisp bold strokes for Carta 1300 reading
            min_8 = np.minimum.reduce([
                min_4,
                pad[:-2, :-2],
                pad[:-2, 2:],
                pad[2:, :-2],
                pad[2:, 2:],
            ])
            curved = min_4 * 0.65 + min_8 * 0.35
        else:
            # Level 3: Heavy Bold (+100%) - thickens ultra-thin CAD/engineering diagrams & faint text
            min_8 = np.minimum.reduce([
                min_4,
                pad[:-2, :-2],
                pad[:-2, 2:],
                pad[2:, :-2],
                pad[2:, 2:],
            ])
            pad2 = np.pad(min_8, 1, mode="edge")
            min_8_plus = np.minimum.reduce([
                min_8,
                pad2[:-2, 1:-1],
                pad2[2:, 1:-1],
                pad2[1:-1, :-2],
                pad2[1:-1, 2:],
            ])
            curved = min_8 * 0.65 + min_8_plus * 0.35

    if is_comic:
        curved[curved > 0.94] = 1.0
        curved[curved < 0.06] = 0.0
    else:
        # Snap paper background to pure #FFFFFF (zero KPW6 checkerboard dithering) and core strokes to deep #000000
        curved[curved > 0.90] = 1.0
        curved[curved < 0.16] = 0.0

    out_arr = (curved * 255.0).astype(np.uint8)
    out_img = Image.fromarray(out_arr, mode="L")
    if sharpen_strength > 0.05:
        out_img = out_img.filter(
            ImageFilter.UnsharpMask(
                radius=0.68,
                percent=int(round(sharpen_strength * 100.0)),
                threshold=2,
            )
        )
    return out_img


# ==============================================================================
# PURE TEXT PDF -> SMART PARAGRAPH / CHAPTER EXTRACTOR & REFLOW ENGINE
# ==============================================================================

CHAPTER_REGEX = re.compile(
    r"^\s*(第[一二三四五六七八九十百千万零0-9]+[章节回篇卷部集]|Chapter\s+\d+|CHAPTER\s+[IVXLC\d]+|序言|前言|引言|楔子|尾声|后记|附录)\b",
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

    for b in blocks:
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            lbbox = fitz.Rect(line["bbox"])
            spans = line.get("spans", [])
            line_text = "".join(s.get("text", "") for s in spans).strip()
            if not line_text:
                continue
            max_sz = max((float(s.get("size", 11.0)) for s in spans), default=11.0)

            # Filter running header (top 8%) and running footer / page number (bottom 8.5%)
            if trim_hf:
                if lbbox.y1 < h * 0.085 and max_sz < 13.0:
                    continue
                if lbbox.y0 > h * 0.915:
                    continue
                if PAGE_NUM_REGEX.match(line_text) and (lbbox.y0 > h * 0.85 or lbbox.y1 < h * 0.12):
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
            # Join CJK without space, ASCII words with space
            if curr_para_text[-1].isascii() and curr_para_text[-1].isalnum() and txt[0].isascii() and txt[0].isalnum():
                if curr_para_text.endswith("-"):
                    curr_para_text = curr_para_text[:-1] + txt
                else:
                    curr_para_text += " " + txt
            else:
                curr_para_text += txt
            if curr_para_bbox:
                curr_para_bbox |= lb

    flush_para()
    return items


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

    final_title = options.custom_cover_title.strip() or detected_title or clean_fname
    mode_badge = {
        "academic_2col": "双栏学术论文 · KPW6 专享重排",
        "tech_book": "专业图表书籍 · KPW6 专享重排",
        "scanned_book": "Carta 1300 去网格高清重制版",
        "comic_manga": "KPW6 满屏高清漫画/画集",
        "pure_text": "KPW6 原生精排电子书",
    }.get(options.mode, "KPW6 Signature Edition 专享版")
    final_author = options.custom_cover_author.strip() or detected_author or mode_badge
    return final_title, final_author


def _insert_cover_page_to_doc(
    src_doc: fitz.Document,
    input_pdf_path: str,
    out_doc: fitz.Document,
    options: ProcessOptions,
) -> None:
    """
    Insert a dedicated, un-sliced KPW6 full-screen Cover Page (360 x 478.48 pt -> 1264 x 1680 px)
    as the first page of out_doc according to options.cover_mode:
    - "smart_card": Hardcover-style E-Ink cover card with bold title header + framed full Page 1 preview
    - "first_page": Full, un-sliced Page 1 enhanced for Carta 1300 E-Ink
    - "custom_image": User-uploaded custom cover image fitted to 1264 x 1680
    """
    if options.cover_mode == "none" or len(src_doc) == 0:
        return

    cover_page = out_doc.new_page(width=KPW6_PT_W, height=KPW6_PT_H)
    full_rect = fitz.Rect(0, 0, KPW6_PT_W, KPW6_PT_H)

    # 1. Custom uploaded image cover
    if options.cover_mode == "custom_image" and options.custom_cover_path and os.path.exists(options.custom_cover_path):
        try:
            with Image.open(options.custom_cover_path) as im:
                fitted = ImageOps.fit(im.convert("RGB"), (KPW6_PX_W, KPW6_PX_H), method=Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                fitted.save(buf, format="PNG")
                cover_page.insert_image(full_rect, stream=buf.getvalue())
                return
        except Exception:
            pass

    if options.explicit_pages:
        cover_src_idx = max(0, min(len(src_doc) - 1, options.explicit_pages[0]))
    elif options.start_page and options.start_page > 1:
        cover_src_idx = max(0, min(len(src_doc) - 1, options.start_page - 1))
    else:
        cover_src_idx = 0
    p0 = src_doc[cover_src_idx]

    # 2. Full un-sliced First Page cover
    if options.cover_mode == "first_page":
        pad = 6.0
        avail_w = KPW6_PT_W - 2 * pad
        avail_h = KPW6_PT_H - 2 * pad
        scale = min(avail_w / max(1.0, p0.rect.width), avail_h / max(1.0, p0.rect.height))
        dw, dh = p0.rect.width * scale, p0.rect.height * scale
        target = fitz.Rect((KPW6_PT_W - dw) / 2.0, (KPW6_PT_H - dh) / 2.0, (KPW6_PT_W + dw) / 2.0, (KPW6_PT_H + dh) / 2.0)
        _insert_enhanced_raster_slice(p0, p0.rect, cover_page, target, options, rotate=0)
        return

    # 3. Default "smart_card": Hardcover E-Ink Magazine/Book Cover Card
    title, author = extract_book_meta(src_doc, input_pdf_path, options)
    total_p = len(src_doc)
    cjk_font = fitz.Font("cjk")

    # Classic double-line book border
    cover_page.draw_rect(fitz.Rect(6, 6, KPW6_PT_W - 6, KPW6_PT_H - 6), color=(0.12, 0.12, 0.14), width=1.6)
    cover_page.draw_rect(fitz.Rect(9.5, 9.5, KPW6_PT_W - 9.5, KPW6_PT_H - 9.5), color=(0.45, 0.45, 0.50), width=0.6)

    # Top deep-charcoal title header block
    header_rect = fitz.Rect(14, 14, KPW6_PT_W - 14, 134)
    cover_page.draw_rect(header_rect, color=(0.08, 0.09, 0.11), fill=(0.08, 0.09, 0.11))

    # Top-left KPW6 edition badge
    tw_badge = fitz.TextWriter(cover_page.rect)
    tw_badge.append((22, 29), "KINDLE PAPERWHITE SIGNATURE EDITION · 300 PPI", font=cjk_font, fontsize=7.5)
    tw_badge.write_text(cover_page, color=(0.72, 0.78, 0.86))

    # Main Book / Paper Title (auto-scaled & wrapped with TextWriter)
    t_len = len(title)
    t_size = 14.5 if t_len <= 32 else (12.0 if t_len <= 68 else 10.2)
    tw_title = fitz.TextWriter(cover_page.rect)
    tw_title.fill_textbox(
        fitz.Rect(22, 36, KPW6_PT_W - 22, 108),
        title,
        font=cjk_font,
        fontsize=t_size,
        lineheight=1.28,
    )
    tw_title.write_text(cover_page, color=(1.0, 1.0, 1.0))

    # Subtitle / Author strip inside header
    tw_sub = fitz.TextWriter(cover_page.rect)
    tw_sub.fill_textbox(
        fitz.Rect(22, 111, KPW6_PT_W - 22, 130),
        author[:65],
        font=cjk_font,
        fontsize=8.5,
    )
    tw_sub.write_text(cover_page, color=(0.82, 0.86, 0.90))

    # Center framed miniature of Full Page 1
    frame_box = fitz.Rect(22, 142, KPW6_PT_W - 22, KPW6_PT_H - 34)
    cover_page.draw_rect(frame_box, color=(0.75, 0.77, 0.80), fill=(0.98, 0.98, 0.99), width=0.8)

    inner_pad = 6.0
    f_avail_w = frame_box.width - 2 * inner_pad
    f_avail_h = frame_box.height - 2 * inner_pad
    f_scale = min(f_avail_w / max(1.0, p0.rect.width), f_avail_h / max(1.0, p0.rect.height))
    fw, fh = p0.rect.width * f_scale, p0.rect.height * f_scale
    fx0 = frame_box.x0 + (frame_box.width - fw) / 2.0
    fy0 = frame_box.y0 + (frame_box.height - fh) / 2.0
    thumb_target = fitz.Rect(fx0, fy0, fx0 + fw, fy0 + fh)
    _insert_enhanced_raster_slice(p0, p0.rect, cover_page, thumb_target, options, rotate=0)
    cover_page.draw_rect(thumb_target, color=(0.25, 0.25, 0.28), width=0.6)

    # Bottom footer bar
    if options.explicit_pages and len(options.explicit_pages) < total_p:
        p_first = options.explicit_pages[0] + 1
        p_last = options.explicit_pages[-1] + 1
        footer_text = f"KPW6 专享排版 · P{p_first}-{p_last} (共 {len(options.explicit_pages)} 页 / 原书 {total_p} 页)"
    else:
        footer_text = f"KPW6 CARTA 1300 专享排版 · 原书共 {total_p} 页 · Send to Kindle 完美适配"

    tw_foot = fitz.TextWriter(cover_page.rect)
    tw_foot.fill_textbox(
        fitz.Rect(14, KPW6_PT_H - 27, KPW6_PT_W - 14, KPW6_PT_H - 12),
        footer_text,
        font=cjk_font,
        fontsize=7.8,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    tw_foot.write_text(cover_page, color=(0.30, 0.33, 0.38))


def generate_cover_pil_image(
    src_doc: fitz.Document,
    input_pdf_path: str,
    options: ProcessOptions,
) -> Image.Image:
    """
    Render the KPW6 Cover Page at exact 1264 x 1680 px as a PIL RGB Image for EPUB cover embedding.
    """
    tmp_doc = fitz.open()
    eff_opts = ProcessOptions(**{**options.__dict__})
    if eff_opts.cover_mode == "none":
        eff_opts.cover_mode = "smart_card"
    _insert_cover_page_to_doc(src_doc, input_pdf_path, tmp_doc, eff_opts)
    page = tmp_doc[0]
    scale_x = KPW6_PX_W / KPW6_PT_W
    scale_y = KPW6_PX_H / KPW6_PT_H
    pix = page.get_pixmap(matrix=fitz.Matrix(scale_x, scale_y), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    tmp_doc.close()
    return img


def build_kpw6_epub(
    input_pdf_path: str,
    output_epub_path: str,
    options: ProcessOptions,
    force_reflow: Optional[bool] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Universal KPW6 EPUB 3 Builder with Official Amazon Send-to-Kindle Cover Support:
    - Always embeds a 1264x1680 cover image (OEBPS/images/cover.jpg) with both EPUB 3
      (properties="cover-image") and EPUB 2 (<meta name="cover" content="cover-image"/>)
      declarations so KPW6 Library Bookshelf AND Lockscreen ("显示正在阅读的书籍封面") 100% display the cover!
    - For 'pure_text' mode: builds reflowable text chapters with native font scaling & dictionary lookup.
    - For 'academic_2col', 'tech_book', 'scanned_book', 'comic_manga' modes: packages our KPW6
      1264x1680 reformatted screens into a zero-margin full-screen EPUB with full TOC navigation
      for Tables, Figures, and Chapters!
    """
    src_doc = _open_source_pdf(input_pdf_path)
    total_pages = len(src_doc)
    target_pages = get_target_page_list(total_pages, options)
    if not target_pages:
        src_doc.close()
        raise ValueError("所选页码范围内的页面已被全部设为「剔除跳过」，没有可导出的页面。")
    limit_pages = len(target_pages)

    book_title, book_author = extract_book_meta(src_doc, input_pdf_path, options)
    if progress_cb:
        progress_cb(0, max(1, limit_pages), "正在生成 1264×1680 KPW6 精装封面...")
    cover_pil = generate_cover_pil_image(src_doc, input_pdf_path, options)
    cover_buf = io.BytesIO()
    cover_pil.save(cover_buf, format="JPEG", quality=90, optimize=True)
    cover_bytes = cover_buf.getvalue()

    book_uuid = str(uuid.uuid4())

    css_content = """@charset "utf-8";
body {
  font-family: "Songti SC", "Noto Serif CJK SC", "Bookerly", serif;
  line-height: 1.75;
  margin: 0;
  padding: 0 2%;
  text-align: justify;
}
h1, h2 {
  font-weight: bold;
  line-height: 1.35;
  margin: 1.2em 0 0.8em 0;
  text-indent: 0;
  page-break-after: avoid;
}
p {
  text-indent: 2em;
  margin: 0.35em 0;
  widows: 2;
  orphans: 2;
}
body.full-screen-page {
  margin: 0 !important;
  padding: 0 !important;
  text-align: center;
  background-color: #ffffff;
}
div.screen-box {
  margin: 0;
  padding: 0;
  width: 100%;
  text-align: center;
  page-break-inside: avoid;
}
div.screen-box img {
  max-width: 100%;
  max-height: 98vh;
  width: auto;
  height: auto;
  display: block;
  margin: 0 auto;
}
"""

    manifest_items = [
        '<item id="style" href="style.css" media-type="text/css"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>',
        '<item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_items = ['<itemref idref="cover-page" linear="yes"/>']
    nav_lis = ['      <li><a href="cover.xhtml">📖 书籍封面</a></li>']
    ncx_navpoints = [
        """    <navPoint id="navPoint-0" playOrder="1">
      <navLabel><text>📖 书籍封面</text></navLabel>
      <content src="cover.xhtml"/>
    </navPoint>"""
    ]

    chapters_count = 0
    paragraphs_count = 0
    screens_count = 0

    with zipfile.ZipFile(output_epub_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. mimetype must be first and uncompressed
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

        # 2. META-INF/container.xml
        zf.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""",
        )

        # 3. OEBPS/style.css + Cover image & Cover XHTML
        zf.writestr("OEBPS/style.css", css_content)
        zf.writestr("OEBPS/images/cover.jpg", cover_bytes)

        safe_book_title = html.escape(book_title)
        safe_book_author = html.escape(book_author)
        cover_xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN">
<head>
  <meta charset="UTF-8"/>
  <title>{safe_book_title} - 封面</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body class="full-screen-page" epub:type="cover">
  <div class="screen-box">
    <img src="images/cover.jpg" alt="{safe_book_title}"/>
  </div>
</body>
</html>"""
        zf.writestr("OEBPS/cover.xhtml", cover_xhtml)

        use_reflow = (options.mode == "pure_text") if force_reflow is None else force_reflow
        if use_reflow:
            all_items: List[ExtractedParagraph] = []
            for step_i, pno in enumerate(target_pages, start=1):
                if progress_cb:
                    progress_cb(step_i, limit_pages, f"正在智能提取并合并第 {pno+1} 页正文 ({step_i}/{limit_pages})...")
                rule = (options.page_overrides or {}).get(str(pno), "auto")
                if rule in ("whole_portrait", "whole_landscape"):
                    # Preserve protected pages as images between reflowable sections.
                    # A separate item also prevents merging text across this page.
                    slices, _, _, _ = analyze_page_slices(src_doc[pno], pno, options)
                    with fitz.open() as page_doc:
                        _render_slices_to_doc(src_doc, pno, slices, page_doc, options)
                        for page in page_doc:
                            screens_count += 1
                            image_rel = f"images/protected_{screens_count:04d}.png"
                            pix = page.get_pixmap(matrix=fitz.Matrix(
                                KPW6_PX_W / KPW6_PT_W, KPW6_PX_H / KPW6_PT_H),
                                colorspace=fitz.csGRAY, alpha=False)
                            zf.writestr(f"OEBPS/{image_rel}", pix.tobytes("png"), compress_type=zipfile.ZIP_STORED)
                            manifest_items.append(f'<item id="protected_{screens_count:04d}" href="{image_rel}" media-type="image/png"/>')
                            all_items.append(ExtractedParagraph(kind="page_image", text=image_rel, src_page=pno))
                    continue
                page_items = extract_structured_text_from_page(src_doc[pno], pno, trim_hf=options.trim_header_footer)
                if all_items and page_items and all_items[-1].kind == "para" and page_items[0].kind == "para":
                    prev_t = all_items[-1].text
                    if prev_t and prev_t[-1] not in TERMINAL_PUNCT:
                        nxt_t = page_items[0].text
                        if prev_t[-1].isascii() and nxt_t[0].isascii():
                            all_items[-1].text = prev_t + " " + nxt_t
                        else:
                            all_items[-1].text = prev_t + nxt_t
                        page_items = page_items[1:]
                all_items.extend(page_items)

            src_doc.close()

            chapters: List[Dict[str, Any]] = []
            curr_chap = {"title": "正文开始", "paras": []}
            for item in all_items:
                if item.kind == "page_image":
                    if curr_chap["paras"] or curr_chap["title"] != "正文开始":
                        chapters.append(curr_chap)
                    chapters.append({"title": f"原书第 {item.src_page + 1} 页 · 整页", "paras": [], "image": item.text})
                    curr_chap = {"title": "正文开始", "paras": []}
                elif item.kind == "chapter":
                    if curr_chap["paras"] or curr_chap["title"] != "正文开始":
                        chapters.append(curr_chap)
                    curr_chap = {"title": item.text, "paras": []}
                else:
                    curr_chap["paras"].append(item.text)
            if curr_chap["paras"] or curr_chap["title"] != "正文开始" or not chapters:
                chapters.append(curr_chap)

            chapters_count = len(chapters)
            paragraphs_count = sum(len(c["paras"]) for c in chapters)

            for idx, ch in enumerate(chapters, start=1):
                fname = f"chapter_{idx:03d}.xhtml"
                cid = f"chap_{idx:03d}"
                safe_title = html.escape(ch["title"])
                paras_html = "\n".join(f"  <p>{html.escape(p)}</p>" for p in ch["paras"])
                if ch.get("image"):
                    body_class = ' class="full-screen-page"'
                    body_html = f'<div class="screen-box"><img src="{ch["image"]}" alt="{safe_title}"/></div>'
                else:
                    body_class = ""
                    body_html = f"<h2>{safe_title}</h2>\n{paras_html}"
                xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">
<head>
  <meta charset="UTF-8"/>
  <title>{safe_title}</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body{body_class}>
{body_html}
</body>
</html>"""
                zf.writestr(f"OEBPS/{fname}", xhtml)
                manifest_items.append(f'<item id="{cid}" href="{fname}" media-type="application/xhtml+xml"/>')
                spine_items.append(f'<itemref idref="{cid}"/>')
                nav_lis.append(f'      <li><a href="{fname}">{safe_title}</a></li>')
                ncx_navpoints.append(
                    f"""    <navPoint id="navPoint-{idx}" playOrder="{idx + 1}">
      <navLabel><text>{safe_title}</text></navLabel>
      <content src="{fname}"/>
    </navPoint>"""
                )
        else:
            # High-Definition KPW6 Screen-Layout EPUB (preserves 2-col zoom, tables, rotated figures, bleached scans!)
            page_to_first_screen: Dict[int, int] = {}
            screen_idx = 0
            scale_x = KPW6_PX_W / KPW6_PT_W
            scale_y = KPW6_PX_H / KPW6_PT_H

            for step_i, pno in enumerate(target_pages, start=1):
                if progress_cb:
                    progress_cb(step_i, limit_pages, f"正在精排并渲染第 {pno+1} 页 ({step_i}/{limit_pages})...")
                page = src_doc[pno]
                slices, _, _, _ = analyze_page_slices(page, pno, options, dpi=150)
                tmp_doc = fitz.open()
                _render_slices_to_doc(src_doc, pno, slices, tmp_doc, options)
                page_to_first_screen[pno] = screen_idx + 1

                for sub_i, kp in enumerate(tmp_doc):
                    screen_idx += 1
                    pix = kp.get_pixmap(matrix=fitz.Matrix(scale_x, scale_y), colorspace=fitz.csGRAY, alpha=False)
                    im = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                    img_buf = io.BytesIO()
                    im.save(img_buf, format="PNG", compress_level=6)

                    img_rel = f"images/screen_{screen_idx:04d}.png"
                    xhtml_rel = f"screen_{screen_idx:04d}.xhtml"
                    zf.writestr(f"OEBPS/{img_rel}", img_buf.getvalue(), compress_type=zipfile.ZIP_STORED)

                    page_label = f"原书第 {pno+1} 页 · 第 {sub_i+1} 屏"
                    s_xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">
<head>
  <meta charset="UTF-8"/>
  <title>{page_label}</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body class="full-screen-page">
  <div class="screen-box">
    <img src="{img_rel}" alt="{page_label}"/>
  </div>
</body>
</html>"""
                    zf.writestr(f"OEBPS/{xhtml_rel}", s_xhtml)
                    manifest_items.append(f'<item id="img_{screen_idx:04d}" href="{img_rel}" media-type="image/png"/>')
                    manifest_items.append(f'<item id="scr_{screen_idx:04d}" href="{xhtml_rel}" media-type="application/xhtml+xml"/>')
                    spine_items.append(f'<itemref idref="scr_{screen_idx:04d}"/>')

                tmp_doc.close()

            screens_count = screen_idx
            toc_entries: List[Tuple[str, int]] = []
            try:
                raw_toc = src_doc.get_toc()
                for _, t_title, src_p1 in raw_toc:
                    p0 = src_p1 - 1
                    if p0 in page_to_first_screen and t_title.strip():
                        toc_entries.append((t_title.strip(), page_to_first_screen[p0]))
            except Exception:
                pass

            src_doc.close()

            # Also include detected Table / Figure highlights in the EPUB TOC so KPW6 user can jump to any Table/Figure!
            for hl in scan_document_highlights(input_pdf_path):
                p0 = hl.get("page_num", 0)
                if p0 in page_to_first_screen:
                    prefix = "📊 " if hl.get("kind") == "table" else "📈 "
                    toc_entries.append((prefix + hl.get("label", f"P{p0+1}"), page_to_first_screen[p0]))

            if not toc_entries and page_to_first_screen:
                step = 1 if limit_pages <= 15 else (5 if limit_pages <= 80 else 10)
                for idx_p in range(0, len(target_pages), step):
                    p0 = target_pages[idx_p]
                    if p0 in page_to_first_screen:
                        toc_entries.append((f"原书第 {p0+1} 页", page_to_first_screen[p0]))

            chapters_count = len(toc_entries)
            for idx, (entry_title, target_scr) in enumerate(toc_entries, start=1):
                safe_t = html.escape(entry_title)
                href = f"screen_{target_scr:04d}.xhtml"
                nav_lis.append(f'      <li><a href="{href}">{safe_t}</a></li>')
                ncx_navpoints.append(
                    f"""    <navPoint id="navPoint-{idx}" playOrder="{idx + 1}">
      <navLabel><text>{safe_t}</text></navLabel>
      <content src="{href}"/>
    </navPoint>"""
                )

        # 4. OEBPS/nav.xhtml
        nav_xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN">
<head><meta charset="UTF-8"/><title>目录</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>目录</h1>
    <ol>
{chr(10).join(nav_lis)}
    </ol>
  </nav>
</body>
</html>"""
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml)

        # 5. OEBPS/toc.ncx
        ncx_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="urn:uuid:{book_uuid}"/>
    <meta name="dtb:depth" content="1"/>
  </head>
  <docTitle><text>{safe_book_title}</text></docTitle>
  <navMap>
{chr(10).join(ncx_navpoints)}
  </navMap>
</ncx>"""
        zf.writestr("OEBPS/toc.ncx", ncx_xml)

        # 6. OEBPS/content.opf (Includes both EPUB 2 <meta name="cover"> and EPUB 3 properties="cover-image" + <guide>)
        opf_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:identifier id="BookId">urn:uuid:{book_uuid}</dc:identifier>
    <dc:title>{safe_book_title}</dc:title>
    <dc:language>zh-CN</dc:language>
    <dc:creator>{safe_book_author}</dc:creator>
    <meta name="cover" content="cover-image"/>
  </metadata>
  <manifest>
{chr(10).join("    " + m for m in manifest_items)}
  </manifest>
  <spine toc="ncx">
{chr(10).join("    " + s for s in spine_items)}
  </spine>
  <guide>
    <reference type="cover" title="书籍封面" href="cover.xhtml"/>
  </guide>
</package>"""
        zf.writestr("OEBPS/content.opf", opf_xml)

    return {
        "input_pages": limit_pages,
        "chapters_count": chapters_count,
        "paragraphs_count": paragraphs_count,
        "screens_count": screens_count,
        "book_title": book_title,
        "book_author": book_author,
        "output_path": output_epub_path,
        "output_size_mb": round(os.path.getsize(output_epub_path) / (1024 * 1024), 2),
    }


def build_kpw6_cbz(
    input_pdf_path: str,
    output_cbz_path: str,
    options: ProcessOptions,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Package KPW6 Cover Page + all 1264x1680 KPW6-reformatted screens into a standard
    lossless .cbz (Comic Book ZIP) archive with ComicInfo.xml metadata.
    """
    src_doc = _open_source_pdf(input_pdf_path)
    total_pages = len(src_doc)
    target_pages = get_target_page_list(total_pages, options)
    if not target_pages:
        src_doc.close()
        raise ValueError("所选页码范围内的页面已被全部设为「剔除跳过」，没有可导出的页面。")
    limit_pages = len(target_pages)
    book_title, book_author = extract_book_meta(src_doc, input_pdf_path, options)

    cover_pil = generate_cover_pil_image(src_doc, input_pdf_path, options)
    cover_buf = io.BytesIO()
    cover_pil.save(cover_buf, format="JPEG", quality=92, optimize=True)

    scale_x = KPW6_PX_W / KPW6_PT_W
    scale_y = KPW6_PX_H / KPW6_PT_H
    screen_idx = 0

    with zipfile.ZipFile(output_cbz_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("0000_cover.jpg", cover_buf.getvalue(), compress_type=zipfile.ZIP_STORED)

        for step_i, pno in enumerate(target_pages, start=1):
            if progress_cb:
                progress_cb(step_i, limit_pages, f"正在打包 CBZ 图集第 {pno+1} 页 ({step_i}/{limit_pages})...")
            page = src_doc[pno]
            slices, _, _, _ = analyze_page_slices(page, pno, options, dpi=150)
            tmp_doc = fitz.open()
            _render_slices_to_doc(src_doc, pno, slices, tmp_doc, options)
            for kp in tmp_doc:
                screen_idx += 1
                pix = kp.get_pixmap(matrix=fitz.Matrix(scale_x, scale_y), colorspace=fitz.csGRAY, alpha=False)
                im = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                img_buf = io.BytesIO()
                im.save(img_buf, format="PNG", compress_level=6)
                zf.writestr(f"{screen_idx:04d}.png", img_buf.getvalue(), compress_type=zipfile.ZIP_STORED)
            tmp_doc.close()

        comic_info = f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{html.escape(book_title)}</Title>
  <Writer>{html.escape(book_author)}</Writer>
  <PageCount>{screen_idx + 1}</PageCount>
  <Manga>{"YesAndRightToLeft" if (options.mode == "comic_manga" and options.manga_rtl) else "No"}</Manga>
</ComicInfo>"""
        zf.writestr("ComicInfo.xml", comic_info)

    src_doc.close()
    return {
        "input_pages": limit_pages,
        "screens_count": screen_idx,
        "book_title": book_title,
        "output_path": output_cbz_path,
        "output_size_mb": round(os.path.getsize(output_cbz_path) / (1024 * 1024), 2),
    }


def convert_epub_via_calibre(
    input_epub_path: str,
    output_target_path: str,
    book_title: str = "",
    book_author: str = "",
) -> Dict[str, Any]:
    """
    Convert a generated KPW6 .epub (with embedded cover) into Kindle native .azw3 (KF8) or .mobi
    using Calibre's ebook-convert CLI if installed on macOS.
    """
    calibre_bin = shutil.which("ebook-convert")
    if not calibre_bin:
        candidates = [
            "/Applications/calibre.app/Contents/MacOS/ebook-convert",
            r"C:\Program Files\Calibre2\ebook-convert.exe",
            r"C:\Program Files (x86)\Calibre2\ebook-convert.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Calibre2\ebook-convert.exe"),
        ]
        for cand in candidates:
            if cand and os.path.exists(cand):
                calibre_bin = cand
                break
    if not calibre_bin:
        raise RuntimeError("未检测到本机 Calibre (ebook-convert)，请先安装免费开源的 Calibre 或优先选择 .epub / .pdf 格式。")

    cmd = [calibre_bin, input_epub_path, output_target_path]
    if book_title:
        cmd.extend(["--title", book_title])
    if book_author:
        cmd.extend(["--authors", book_author])

    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return {
        "output_path": output_target_path,
        "output_size_mb": round(os.path.getsize(output_target_path) / (1024 * 1024), 2),
    }


def _render_pure_text_items_to_kpw6_doc(
    items: List[ExtractedParagraph],
    out_doc: fitz.Document,
    options: ProcessOptions,
) -> None:
    """
    Typeset extracted pure-text paragraphs & chapter headings directly onto KPW6 vector pages
    with CJK indentation (2em), user-chosen font size, and clean line spacing.
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
            for cl, x_indent in ch_lines:
                if curr_y + chap_size * 1.4 > y_bot:
                    start_new_page()
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
                tw.append((margin_pt + x_indent, curr_y + fsize), pl, font=font, fontsize=fsize)
                curr_y += line_h
            curr_y += fsize * 0.38  # Paragraph spacing

    if curr_page is not None and tw is not None:
        tw.write_text(curr_page)


def _wrap_cjk_line(
    font: fitz.Font,
    text: str,
    fontsize: float,
    max_width: float,
    indent_pt: float = 0.0,
) -> List[Tuple[str, float]]:
    """Greedy line wrapper supporting mixed CJK and Latin text with hanging punctuation protection."""
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
            lines.append((curr_str, curr_indent))
            curr_str = ch
            curr_indent = 0.0
            curr_limit = max_width
        else:
            curr_str = candidate

    if curr_str:
        lines.append((curr_str, curr_indent))
    return lines


# ==============================================================================
# UNIFIED PAGE ANALYZER & PDF BUILDER ACROSS ALL 5 MODES
# ==============================================================================

def _build_table_header_keep_together_mask(
    page: fitz.Page,
    zx0: int,
    zy0: int,
    zx1: int,
    zy1: int,
    sx: float,
    sy: float,
) -> np.ndarray:
    """
    Return a boolean mask of length (zy1 - zy0) where True means 'inside a Table caption/header
    or tight table body that should NOT be cut right after the header'.
    Only allows cutting immediately ABOVE a sub-table rule or outside the table.
    """
    bh = max(1, zy1 - zy0)
    no_cut = np.zeros(bh, dtype=bool)
    x0_pt = page.rect.x0 + zx0 / sx
    x1_pt = page.rect.x0 + zx1 / sx

    # Check horizontal rules inside this column's x-range
    col_rules_y: List[float] = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r and r.height < 16.0 and r.width > (x1_pt - x0_pt) * 0.45:
            # Overlaps horizontally with this column
            if min(r.x1, x1_pt) - max(r.x0, x0_pt) > (x1_pt - x0_pt) * 0.40:
                col_rules_y.append(r.y0)
                if r.height > 4.0:
                    col_rules_y.append(r.y1)

    for b in page.get_text("blocks"):
        if b[6] != 0:
            continue
        bx0, by0, bx1, by1, txt = b[0], b[1], b[2], b[3], str(b[4]).strip()
        if min(bx1, x1_pt) - max(bx0, x0_pt) <= 10.0:
            continue
        if re.match(r"^(?:Table|TABLE)\s+(?:\d+[A-Za-z]?|[IVXLCDM]+(?:\b|[.\-—:：]))", txt):
            # Protect from Table caption down to +65 pt below it so caption + header + first rows stay together
            p0 = max(0, int((by0 - 2.0 - page.rect.y0) * sy) - zy0)
            p1 = min(bh, int((by1 + 65.0 - page.rect.y0) * sy) - zy0)
            if p1 > p0:
                no_cut[p0:p1] = True

    for ry in col_rules_y:
        # Protect 45 pt below every horizontal table rule (so we never cut right below a table header rule,
        # while still allowing cuts immediately ABOVE a sub-table rule!)
        p0 = max(0, int((ry - 2.0 - page.rect.y0) * sy) - zy0)
        p1 = min(bh, int((ry + 48.0 - page.rect.y0) * sy) - zy0)
        if p1 > p0:
            no_cut[p0:p1] = True

    return no_cut


def analyze_page_slices(
    page: fitz.Page,
    page_num: int,
    options: ProcessOptions,
    dpi: int = 150,
) -> Tuple[List[SliceBox], np.ndarray, float, float]:
    """
    Analyze a single PDF page across all 5 modes.
    """
    page_rule = (options.page_overrides or {}).get(str(page_num), "auto")
    if page_rule == "skip":
        gray, sx, sy = render_page_gray(page, dpi=96)
        return [], gray, sx, sy

    if options.mode == "pure_text" and page_rule not in ("whole_portrait", "whole_landscape"):
        gray, sx, sy = render_page_gray(page, dpi=120)
        items = extract_structured_text_from_page(page, page_num, trim_hf=options.trim_header_footer)
        sboxes: List[SliceBox] = []
        for idx, it in enumerate(items):
            if it.bbox:
                k = "full" if it.kind == "chapter" else "body"
                sboxes.append(SliceBox(page_num=page_num, rect=it.bbox, kind=k, out_page_idx=0))
        if not sboxes:
            sboxes.append(SliceBox(page_num=page_num, rect=page.rect, kind="body", out_page_idx=0))
        return sboxes, gray, sx, sy

    is_scanned_or_comic = (options.mode in ("scanned_book", "comic_manga")) or options.enhance_contrast
    gray, sx, sy = render_page_gray(page, dpi=dpi)
    ink = compute_ink_mask(
        gray,
        is_scanned=is_scanned_or_comic,
        clean_spine_shadow=options.clean_spine_shadow,
        remove_watermark=options.remove_watermark,
    )

    h, w = ink.shape
    y_top, y_bot = (0, h)
    if options.trim_header_footer and options.mode != "comic_manga":
        y_top, y_bot = strip_running_headers_footers(ink)

    bbox = find_tight_bbox(ink, y_top, y_bot)
    if bbox is None:
        margin_pt = 12.0
        r = page.rect
        fallback_rect = fitz.Rect(r.x0 + margin_pt, r.y0 + margin_pt, r.x1 - margin_pt, r.y1 - margin_pt)
        return [SliceBox(page_num=page_num, rect=fallback_rect, kind="full", out_page_idx=0)], gray, sx, sy

    pad_px = max(2, int(dpi * 0.02))

    # Check per-page special rule override (Feature 3: whole_portrait / whole_landscape)
    page_rule = (options.page_overrides or {}).get(str(page_num), "auto")
    if page_rule in ("whole_portrait", "whole_landscape"):
        zx0, zy0, zx1, zy1 = bbox
        px0, py0 = max(0, zx0 - pad_px), max(0, zy0 - pad_px)
        px1, py1 = min(w, zx1 + pad_px), min(h, zy1 + pad_px)
        pdf_rect = fitz.Rect(
            page.rect.x0 + px0 / sx,
            page.rect.y0 + py0 / sy,
            page.rect.x0 + px1 / sx,
            page.rect.y0 + py1 / sy,
        )
        rot_angle = -90 if page_rule == "whole_landscape" else 0
        kind_tag = "figure_rot" if rot_angle != 0 else "full_override"
        return [SliceBox(page_num=page_num, rect=pdf_rect, kind=kind_tag, out_page_idx=0, rotate=rot_angle)], gray, sx, sy

    # Comic / Manga mode
    if options.mode == "comic_manga":
        comic_zones = segment_comic_spread(ink, bbox, w, h, options)
        sboxes = []
        for idx, (kind, (zx0, zy0, zx1, zy1)) in enumerate(comic_zones):
            px0, py0 = max(0, zx0 - 2), max(0, zy0 - 2)
            px1, py1 = min(w, zx1 + 2), min(h, zy1 + 2)
            pdf_rect = fitz.Rect(
                page.rect.x0 + px0 / sx,
                page.rect.y0 + py0 / sy,
                page.rect.x0 + px1 / sx,
                page.rect.y0 + py1 / sy,
            )
            sboxes.append(SliceBox(page_num=page_num, rect=pdf_rect, kind=kind, out_page_idx=idx))
        return sboxes, gray, sx, sy

    # Check if this is a 90-degree rotated sideways figure / sideways table page (e.g. Pages 9-48 in lemmon2006.pdf)
    if options.mode in ("academic_2col", "tech_book"):
        sideways_slices = _analyze_rotated_sideways_page(page, page_num, ink, bbox, sx, sy, pad_px)
        if sideways_slices is not None:
            return sideways_slices, gray, sx, sy

    if options.mode == "academic_2col":
        raw_zones = segment_two_column_zones(ink, bbox, page=page, sy=sy)
    else:
        raw_zones = [("body", bbox)]

    canvas_w = KPW6_PT_H if options.landscape_2x else KPW6_PT_W
    canvas_h = KPW6_PT_W if options.landscape_2x else KPW6_PT_H
    margin_pt = min(canvas_w, canvas_h) * (options.margin_Percent / 100.0)
    avail_w_pt = canvas_w - 2.0 * margin_pt
    avail_h_pt = canvas_h - 2.0 * margin_pt
    gap_pt = 10.0

    if not options.split_pages:
        slice_boxes: List[SliceBox] = []
        for idx, (kind, (zx0, zy0, zx1, zy1)) in enumerate(raw_zones):
            px0, py0 = max(0, zx0 - pad_px), max(0, zy0 - pad_px)
            px1, py1 = min(w, zx1 + pad_px), min(h, zy1 + pad_px)
            pdf_rect = fitz.Rect(
                page.rect.x0 + px0 / sx,
                page.rect.y0 + py0 / sy,
                page.rect.x0 + px1 / sx,
                page.rect.y0 + py1 / sy,
            )
            slice_boxes.append(SliceBox(page_num=page_num, rect=pdf_rect, kind=kind, out_page_idx=idx))
        return slice_boxes, gray, sx, sy

    slice_boxes = []
    out_page_idx = 0
    rem_h_pt = avail_h_pt

    for kind, (zx0, zy0, zx1, zy1) in raw_zones:
        zw_px = max(20, zx1 - zx0)
        zw_pt = zw_px / sx
        scale_to_kpw6 = avail_w_pt / zw_pt

        # Build a temporary ink copy where table headers are bridged so find_safe_cut_in_range never cuts right after a table header
        no_cut_mask = _build_table_header_keep_together_mask(page, zx0, zy0, zx1, zy1, sx, sy)
        zone_ink = ink.copy()
        if np.any(no_cut_mask):
            for rel_y in np.where(no_cut_mask)[0]:
                zone_ink[zy0 + rel_y, zx0:zx0 + 10] = True

        curr_y = zy0
        while curr_y < zy1:
            rem_sub = ink[curr_y:zy1, zx0:zx1]
            if not np.any(rem_sub):
                break
            first_ink_rel = int(np.where(np.any(rem_sub, axis=1))[0][0])
            curr_y += first_ink_rel

            rem_zone_h_px = zy1 - curr_y
            rem_zone_h_pt_on_kpw6 = (rem_zone_h_px / sy) * scale_to_kpw6

            # Allow 12% vertical scale tolerance on a fresh page for full-width blocks/tables
            tolerance_pt = (avail_h_pt * 0.14) if (kind == "full" and rem_h_pt >= avail_h_pt - 1.0) else 2.0

            if rem_zone_h_pt_on_kpw6 <= rem_h_pt + tolerance_pt:
                sub_slice = ink[curr_y:zy1, zx0:zx1]
                r_any = np.where(np.any(sub_slice, axis=1))[0]
                sy0 = curr_y + int(r_any[0])
                sy1 = curr_y + int(r_any[-1]) + 1
                _append_slice_box(slice_boxes, page, page_num, zx0, sy0, zx1, sy1, pad_px, w, h, sx, sy, kind, out_page_idx)
                used_pt = min(rem_h_pt, ((sy1 - sy0) / sy) * scale_to_kpw6)
                rem_h_pt -= (used_pt + gap_pt)
                break

            max_chunk_px = int((rem_h_pt / scale_to_kpw6) * sy)
            min_chunk_px = int(((avail_h_pt * 0.25) / scale_to_kpw6) * sy)

            if rem_h_pt >= avail_h_pt * 0.28 and max_chunk_px > min_chunk_px + 10:
                cut_y = find_safe_cut_in_range(
                    zone_ink, zx0, curr_y, zx1, zy1,
                    min_cut_y=curr_y + min_chunk_px,
                    max_cut_y=curr_y + max_chunk_px,
                )
                if cut_y is not None and cut_y > curr_y + 8 and (zy1 - cut_y >= 14 or cut_y >= zy1):
                    sub_slice = ink[curr_y:cut_y, zx0:zx1]
                    if np.any(sub_slice):
                        r_any = np.where(np.any(sub_slice, axis=1))[0]
                        sy0 = curr_y + int(r_any[0])
                        sy1 = curr_y + int(r_any[-1]) + 1
                        _append_slice_box(slice_boxes, page, page_num, zx0, sy0, zx1, sy1, pad_px, w, h, sx, sy, kind, out_page_idx)
                    curr_y = cut_y
                    out_page_idx += 1
                    rem_h_pt = avail_h_pt
                    continue

            if rem_h_pt < avail_h_pt - 1.0:
                out_page_idx += 1
                rem_h_pt = avail_h_pt
                continue

            full_max_px = int((avail_h_pt / scale_to_kpw6) * sy)
            full_min_px = int(full_max_px * 0.42)
            cut_y = find_safe_cut_in_range(
                zone_ink, zx0, curr_y, zx1, zy1,
                min_cut_y=curr_y + full_min_px,
                max_cut_y=curr_y + full_max_px,
            )
            if cut_y is None or cut_y <= curr_y + 8:
                s_lo = min(zy1 - 2, curr_y + int(full_max_px * 0.65))
                s_hi = min(zy1 - 1, curr_y + full_max_px)
                if s_hi > s_lo:
                    row_ink = zone_ink[s_lo:s_hi, zx0:zx1].sum(axis=1)
                    cut_y = s_lo + int(np.argmin(row_ink))
                else:
                    cut_y = min(zy1, curr_y + full_max_px)

            # Avoid stranding a tiny <14px bottom sliver onto an orphan screen
            if 0 < zy1 - cut_y < 14:
                if (zy1 - curr_y) <= int(full_max_px * 1.06):
                    cut_y = zy1
                else:
                    alt_hi = min(curr_y + full_max_px, zy1 - 16)
                    alt_lo = curr_y + full_min_px
                    alt_cut = find_safe_cut_in_range(
                        zone_ink, zx0, curr_y, zx1, zy1,
                        min_cut_y=alt_lo,
                        max_cut_y=alt_hi,
                    )
                    if alt_cut is not None and alt_cut > curr_y + 8:
                        cut_y = alt_cut
                    elif alt_hi > alt_lo:
                        row_ink = zone_ink[alt_lo:alt_hi, zx0:zx1].sum(axis=1)
                        cut_y = alt_lo + int(np.argmin(row_ink))

            sub_slice = ink[curr_y:cut_y, zx0:zx1]
            if np.any(sub_slice):
                r_any = np.where(np.any(sub_slice, axis=1))[0]
                sy0 = curr_y + int(r_any[0])
                sy1 = curr_y + int(r_any[-1]) + 1
                _append_slice_box(slice_boxes, page, page_num, zx0, sy0, zx1, sy1, pad_px, w, h, sx, sy, kind, out_page_idx)
                used_pt = ((sy1 - sy0) / sy) * scale_to_kpw6
                rem_h_pt = avail_h_pt - used_pt - gap_pt
            curr_y = cut_y

    return slice_boxes, gray, sx, sy


def _append_slice_box(
    slice_boxes: List[SliceBox],
    page: fitz.Page,
    page_num: int,
    sx0: int,
    sy0: int,
    sx1: int,
    sy1: int,
    pad_px: int,
    w: int,
    h: int,
    sx: float,
    sy: float,
    kind: str,
    out_page_idx: int,
) -> None:
    px0 = max(0, sx0 - pad_px)
    py0 = max(0, sy0 - pad_px)
    px1 = min(w, sx1 + pad_px)
    py1 = min(h, sy1 + pad_px)
    pdf_rect = fitz.Rect(
        page.rect.x0 + px0 / sx,
        page.rect.y0 + py0 / sy,
        page.rect.x0 + px1 / sx,
        page.rect.y0 + py1 / sy,
    )
    slice_boxes.append(SliceBox(page_num=page_num, rect=pdf_rect, kind=kind, out_page_idx=out_page_idx))


def _render_slices_to_doc(
    src_doc: fitz.Document,
    pno: int,
    slices: List[SliceBox],
    out_doc: fitz.Document,
    options: ProcessOptions,
) -> int:
    """Render analyzed slices of source page pno into out_doc. Returns index of first added page."""
    page = src_doc[pno]
    first_out_idx = len(out_doc)
    if not slices:
        return first_out_idx

    is_override_single = len(slices) == 1 and slices[0].kind in ("full_override", "figure_rot")
    if options.mode == "pure_text" and not is_override_single:
        items = extract_structured_text_from_page(page, pno, trim_hf=options.trim_header_footer)
        _render_pure_text_items_to_kpw6_doc(items, out_doc, options)
        return first_out_idx

    canvas_w = KPW6_PT_H if options.landscape_2x else KPW6_PT_W
    canvas_h = KPW6_PT_W if options.landscape_2x else KPW6_PT_H

    eff_margin_pct = 0.4 if (options.mode == "comic_manga" and options.comic_full_bleed) else options.margin_Percent
    margin_pt = min(canvas_w, canvas_h) * (eff_margin_pct / 100.0)
    avail_w = canvas_w - 2.0 * margin_pt
    avail_h = canvas_h - 2.0 * margin_pt
    gap_pt = 10.0

    is_raster_page = False
    try:
        page_area = max(1.0, page.rect.width * page.rect.height)
        for im in page.get_image_info(hashes=False):
            ib = im.get("bbox", (0, 0, 0, 0))
            if max(0.0, ib[2] - ib[0]) * max(0.0, ib[3] - ib[1]) >= page_area * 0.58:
                is_raster_page = True
                break
    except Exception:
        pass

    use_raster_enhance = (
        is_raster_page
        or (options.mode in ("scanned_book", "comic_manga"))
        or options.enhance_contrast
        or (options.stroke_boldness > 0)
        or options.remove_watermark
        or options.auto_deskew
        or (not options.vector_lossless)
    )

    groups: Dict[int, List[SliceBox]] = {}
    for sb in slices:
        groups.setdefault(sb.out_page_idx, []).append(sb)

    for g_idx in sorted(groups.keys()):
        g_slices = groups[g_idx]
        out_page = out_doc.new_page(width=canvas_w, height=canvas_h)

        if (
            options.mode == "comic_manga"
            or (
                len(g_slices) == 1
                and (
                    not options.split_pages
                    or g_slices[0].rotate != 0
                    or g_slices[0].kind == "full_override"
                )
            )
        ):
            sb = g_slices[0]
            r = sb.rect
            eff_w = r.height if abs(sb.rotate) == 90 else r.width
            eff_h = r.width if abs(sb.rotate) == 90 else r.height
            scale = min(avail_w / max(1.0, eff_w), avail_h / max(1.0, eff_h))
            draw_w = eff_w * scale
            draw_h = eff_h * scale
            x_off = (canvas_w - draw_w) / 2.0
            y_off = (canvas_h - draw_h) / 2.0
            target_rect = fitz.Rect(x_off, y_off, x_off + draw_w, y_off + draw_h)
            if use_raster_enhance:
                _insert_enhanced_raster_slice(page, r, out_page, target_rect, options, rotate=sb.rotate)
            else:
                out_page.show_pdf_page(target_rect, src_doc, pno, clip=r, rotate=sb.rotate)
            if options.landscape_2x:
                out_page.set_rotation(90)
            continue

        curr_y = margin_pt
        for sb in g_slices:
            r = sb.rect
            if r.width < 2 or r.height < 2:
                continue
            eff_w = r.height if abs(sb.rotate) == 90 else r.width
            eff_h = r.width if abs(sb.rotate) == 90 else r.height
            scale = avail_w / eff_w
            rem_avail = max(20.0, (canvas_h - margin_pt) - curr_y)
            if eff_h * scale > rem_avail:
                scale = rem_avail / eff_h
            draw_w = eff_w * scale
            draw_h = eff_h * scale
            x_off = (canvas_w - draw_w) / 2.0
            target_rect = fitz.Rect(x_off, curr_y, x_off + draw_w, curr_y + draw_h)
            if use_raster_enhance:
                _insert_enhanced_raster_slice(page, r, out_page, target_rect, options, rotate=sb.rotate)
            else:
                out_page.show_pdf_page(target_rect, src_doc, pno, clip=r, rotate=sb.rotate)
            curr_y += draw_h + gap_pt

        if options.landscape_2x:
            out_page.set_rotation(90)

    return first_out_idx


def build_kpw6_pdf(
    input_pdf_path: str,
    output_pdf_path: str,
    options: ProcessOptions,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Process input_pdf_path according to options and write a KPW6 Signature Edition
    optimized PDF to output_pdf_path, including a dedicated un-sliced Cover Page on Page 1
    and complete PDF /Title & /Author metadata.
    """
    src_doc = _open_source_pdf(input_pdf_path)
    out_doc = fitz.open()

    total_src_pages = len(src_doc)
    target_pages = get_target_page_list(total_src_pages, options)
    limit_pages = len(target_pages)
    if limit_pages == 0:
        out_doc.close()
        src_doc.close()
        raise ValueError("当前所选页码范围内的所有页面均已标记为【跳过此页】，没有可导出的内容。")

    book_title, book_author = extract_book_meta(src_doc, input_pdf_path, options)
    has_cover = options.cover_mode != "none"
    if has_cover:
        if progress_cb:
            progress_cb(0, max(1, limit_pages), "正在生成 1264×1680 KPW6 专属封面...")
        _insert_cover_page_to_doc(src_doc, input_pdf_path, out_doc, options)

    page_map: Dict[int, int] = {}

    if options.mode == "pure_text":
        all_items: List[ExtractedParagraph] = []
        overrides = options.page_overrides or {}
        for step_i, pno in enumerate(target_pages, start=1):
            if overrides.get(str(pno), "auto") in ("whole_portrait", "whole_landscape"):
                if all_items:
                    _render_pure_text_items_to_kpw6_doc(all_items, out_doc, options)
                    all_items = []
                if progress_cb:
                    progress_cb(step_i, limit_pages, f"正在按原版整页模式精排第 {pno+1} 页 ({step_i}/{limit_pages})...")
                slices, _, _, _ = analyze_page_slices(src_doc[pno], pno, options, dpi=150)
                first_idx = _render_slices_to_doc(src_doc, pno, slices, out_doc, options)
                page_map[pno] = first_idx
                continue
            if progress_cb:
                progress_cb(step_i, limit_pages, f"正在智能提取并合并第 {pno+1} 页正文 ({step_i}/{limit_pages})...")
            p_items = extract_structured_text_from_page(src_doc[pno], pno, trim_hf=options.trim_header_footer)
            if all_items and p_items and all_items[-1].kind == "para" and p_items[0].kind == "para":
                prev_t = all_items[-1].text
                if prev_t and prev_t[-1] not in TERMINAL_PUNCT:
                    nxt_t = p_items[0].text
                    all_items[-1].text = (prev_t + " " + nxt_t) if (prev_t[-1].isascii() and nxt_t[0].isascii()) else (prev_t + nxt_t)
                    p_items = p_items[1:]
            all_items.extend(p_items)
        if all_items:
            _render_pure_text_items_to_kpw6_doc(all_items, out_doc, options)
    else:
        for step_i, pno in enumerate(target_pages, start=1):
            if progress_cb:
                progress_cb(step_i, limit_pages, f"正在精排第 {pno+1} 页 ({step_i}/{limit_pages})...")
            page = src_doc[pno]
            slices, _, _, _ = analyze_page_slices(page, pno, options, dpi=150)
            first_idx = _render_slices_to_doc(src_doc, pno, slices, out_doc, options)
            page_map[pno] = first_idx

        try:
            new_toc = [[1, "📖 书籍封面", 1]] if has_cover else []
            toc = src_doc.get_toc()
            if toc and page_map:
                for lvl, title, src_p1 in toc:
                    src_p0 = max(0, src_p1 - 1)
                    if src_p0 in page_map:
                        new_toc.append([lvl, title, page_map[src_p0] + 1])
            if new_toc:
                out_doc.set_toc(new_toc)
        except Exception:
            pass

    try:
        out_doc.set_metadata({
            "title": book_title,
            "author": book_author,
            "subject": "Kindle Paperwhite Signature Edition (KPW6) 300 PPI Optimized Document",
            "creator": "KPW6 PDF Studio",
            "producer": "KPW6 PDF Studio",
        })
    except Exception:
        pass

    out_doc.save(output_pdf_path, garbage=3, deflate=True)
    out_pages_count = len(out_doc)
    out_doc.close()
    src_doc.close()

    return {
        "input_pages": limit_pages,
        "output_pages": out_pages_count,
        "book_title": book_title,
        "output_path": output_pdf_path,
        "output_size_mb": round(os.path.getsize(output_pdf_path) / (1024 * 1024), 2),
    }


def _insert_enhanced_raster_slice(
    src_page: fitz.Page,
    clip_rect: fitz.Rect,
    out_page: fitz.Page,
    target_rect: fitz.Rect,
    options: ProcessOptions,
    rotate: int = 0,
) -> None:
    """
    Render a slice at user-selected super-sampling PPI (300 / 450 / 600 PPI, never downsampling high-res scans!),
    apply E-Ink de-dithering, morphological stroke emboldening & high-definition edge sharpening,
    and insert into out_page.
    """
    eff_clip_w = clip_rect.height if abs(rotate) == 90 else clip_rect.width

    # Detect native raster image density on src_page so we NEVER downsample a 300+ DPI scan
    native_scale = 3.8
    try:
        for im in src_page.get_image_info(hashes=False):
            ib = im.get("bbox", (0, 0, 0, 0))
            bw = max(1.0, ib[2] - ib[0])
            if bw > src_page.rect.width * 0.35:
                native_scale = max(native_scale, float(im.get("width", 0)) / bw)
    except Exception:
        pass

    if options.dpi_preset <= 300:
        dpi_mult, min_scale, max_scale = 1.05, 3.4, 4.7
    elif options.dpi_preset >= 600:
        dpi_mult, min_scale, max_scale = 2.05, 5.4, 7.6
    else:
        # Default 450 PPI (1.5x super-sampling for KPW6 300 PPI Carta 1300)
        dpi_mult, min_scale, max_scale = 1.55, 4.3, 6.2

    kpw6_hd_scale = (KPW6_PX_W * dpi_mult * (target_rect.width / KPW6_PT_W)) / max(1.0, eff_clip_w)
    scale = max(min_scale, min(native_scale, max_scale), kpw6_hd_scale)
    scale = min(max_scale, scale)

    mat = fitz.Matrix(scale, scale)
    pix = src_page.get_pixmap(matrix=mat, clip=clip_rect, colorspace=fitz.csGRAY, alpha=False)
    pil_img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    if rotate == -90 or rotate == 270:
        pil_img = pil_img.transpose(Image.Transpose.ROTATE_270)
    elif rotate == 90:
        pil_img = pil_img.transpose(Image.Transpose.ROTATE_90)

    enhanced = enhance_eink_image(
        pil_img,
        bleach_percentile=options.bleach_threshold,
        gamma=options.gamma_strength,
        is_comic=(options.mode == "comic_manga"),
        stroke_boldness=options.stroke_boldness,
        sharpen_strength=options.sharpen_strength,
        clean_spine_shadow=options.clean_spine_shadow,
        remove_watermark=options.remove_watermark,
        auto_deskew=options.auto_deskew,
    )

    buf = io.BytesIO()
    enhanced.save(buf, format="PNG", optimize=True)
    out_page.insert_image(target_rect, stream=buf.getvalue())


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
        total = len(doc)
        scan_limit = min(total, 400)
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
        doc.close()
    except Exception:
        pass
    return highlights[:45]


def render_preview_comparison(
    input_pdf_path: str,
    page_num: int,
    options: ProcessOptions,
) -> Dict[str, Any]:
    """
    Generate high-definition base64 PNG previews and interactive slice metadata for the Web Studio.
    """
    src_doc = _open_source_pdf(input_pdf_path)
    total_pages = len(src_doc)
    pno = max(0, min(total_pages - 1, page_num))
    page = src_doc[pno]
    page_rect_width = page.rect.width
    page_rect_height = page.rect.height

    slices, _, _, _ = analyze_page_slices(page, pno, options, dpi=150)

    zoom = 2.2 if page.rect.width <= 650 else 1.65
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    src_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    draw = ImageDraw.Draw(src_img, "RGBA")

    color_map = {
        "full": ((37, 99, 235, 225), (37, 99, 235, 36)),
        "full_override": ((168, 85, 247, 235), (168, 85, 247, 42)),
        "col_left": ((5, 150, 105, 225), (5, 150, 105, 36)),
        "col_right": ((217, 119, 6, 225), (217, 119, 6, 36)),
        "figure_rot": ((14, 165, 233, 225), (14, 165, 233, 36)),
        "manga_r": ((225, 29, 72, 225), (225, 29, 72, 35)),
        "manga_l": ((79, 70, 229, 225), (79, 70, 229, 35)),
        "body": ((124, 58, 237, 225), (124, 58, 237, 36)),
    }

    book_title, book_author = extract_book_meta(src_doc, input_pdf_path, options)
    page_override = (options.page_overrides or {}).get(str(pno), "auto")
    has_cover_on_this_page = (pno == 0 and options.cover_mode != "none")
    cover_offset = 1 if has_cover_on_this_page else 0

    if page_override == "skip":
        draw.rectangle([0, 0, pix.width, pix.height], fill=(239, 68, 68, 48), outline=(239, 68, 68, 230), width=5)
        draw.rectangle([16, 16, min(pix.width - 16, 300), 46], fill=(220, 38, 38, 240))
        draw.text((26, 24), "SKIP THIS PAGE ON EXPORT", fill=(255, 255, 255, 255))

    slices_meta: List[Dict[str, Any]] = []
    for idx, sb in enumerate(slices, start=1):
        r = sb.rect
        x0 = (r.x0 - page.rect.x0) * zoom
        y0 = (r.y0 - page.rect.y0) * zoom
        x1 = (r.x1 - page.rect.x0) * zoom
        y1 = (r.y1 - page.rect.y0) * zoom
        border_col, fill_col = color_map.get(sb.kind, color_map["body"])
        draw.rectangle([x0, y0, x1, y1], fill=fill_col, outline=border_col, width=3)
        rot_tag = " [ROT90]" if sb.rotate != 0 else ""
        disp_screen = sb.out_page_idx + 1 + cover_offset
        badge_txt = f"#{idx}" if options.mode == "pure_text" else f"#{idx} -> P{disp_screen}{rot_tag}"
        badge_w = max(58, len(badge_txt) * 7 + 12)
        badge_h = 22
        draw.rectangle([x0, y0, min(pix.width, x0 + badge_w), y0 + badge_h], fill=border_col)
        draw.text((x0 + 6, y0 + 4), badge_txt, fill=(255, 255, 255, 255))

        slices_meta.append({
            "index": idx,
            "kind": sb.kind,
            "out_page_idx": sb.out_page_idx + cover_offset,
            "rotate": sb.rotate,
            "left_pct": round((r.x0 - page.rect.x0) / max(1.0, page_rect_width) * 100.0, 2),
            "top_pct": round((r.y0 - page.rect.y0) / max(1.0, page_rect_height) * 100.0, 2),
            "width_pct": round(r.width / max(1.0, page_rect_width) * 100.0, 2),
            "height_pct": round(r.height / max(1.0, page_rect_height) * 100.0, 2),
        })

    buf_src = io.BytesIO()
    src_img.save(buf_src, format="PNG")
    src_b64 = base64.b64encode(buf_src.getvalue()).decode("ascii")

    temp_out = fitz.open()
    if has_cover_on_this_page:
        _insert_cover_page_to_doc(src_doc, input_pdf_path, temp_out, options)
    _render_slices_to_doc(src_doc, pno, slices, temp_out, options)

    kpw6_b64_list: List[str] = []
    # Render at 3.52x (1267 x 1684 px = true KPW6 1264x1680 300 PPI physical resolution!)
    for kp in temp_out:
        kp_pix = kp.get_pixmap(matrix=fitz.Matrix(3.52, 3.52), alpha=False)
        kp_img = Image.frombytes("RGB", (kp_pix.width, kp_pix.height), kp_pix.samples)
        b_kp = io.BytesIO()
        kp_img.save(b_kp, format="PNG")
        kpw6_b64_list.append(base64.b64encode(b_kp.getvalue()).decode("ascii"))

    temp_out.close()
    src_doc.close()

    avg_zoom = 1.0
    if options.mode == "pure_text" and page_override not in ("whole_portrait", "whole_landscape"):
        avg_zoom = round((options.reflow_font_size / 10.5) * (595.0 / KPW6_PT_W), 2)
    elif slices:
        col_slices = [s for s in slices if s.kind in ("col_left", "col_right", "manga_r", "manga_l")]
        ref_slices = col_slices if col_slices else slices
        avg_slice_w = sum((s.rect.height if abs(s.rotate) == 90 else s.rect.width) for s in ref_slices) / len(ref_slices)
        orient_boost = KPW6_ASPECT if options.landscape_2x else 1.0
        eff_m = 0.4 if (options.mode == "comic_manga" and options.comic_full_bleed) else options.margin_Percent
        avg_zoom = round((page_rect_width / max(1.0, avg_slice_w)) * orient_boost * (1.0 - 2 * eff_m / 100.0), 2)

    return {
        "page_num": pno,
        "total_pages": total_pages,
        "slice_count": len(slices),
        "kpw6_screens_count": len(kpw6_b64_list),
        "magnification": avg_zoom,
        "page_override": page_override,
        "has_cover_screen": has_cover_on_this_page,
        "detected_title": book_title,
        "detected_author": book_author,
        "slices_meta": slices_meta,
        "source_annotated": f"data:image/png;base64,{src_b64}",
        "kpw6_pages": [f"data:image/png;base64,{b}" for b in kpw6_b64_list],
    }


def render_hd_zoom_image(
    input_pdf_path: str,
    page_num: int,
    options: ProcessOptions,
    target_type: str = "kpw6",
    screen_idx: int = 0,
) -> Dict[str, Any]:
    """
    On-Demand 600-PPI Ultra-HD Renderer (2160 x 2871 px for KPW6 screen, or 4.0x for Source PDF)
    so when the user opens the Lightbox Magnifier and zooms to 1.5x / 2.2x / 3.0x, every single
    formula, table border, and scanned engineering curve is 100% razor-sharp with zero upscaling blur.
    """
    src_doc = _open_source_pdf(input_pdf_path)
    total_pages = len(src_doc)
    pno = max(0, min(total_pages - 1, page_num))
    page = src_doc[pno]

    if target_type == "source":
        hd_zoom = 3.6 if page.rect.width <= 650 else 2.8
        pix = page.get_pixmap(matrix=fitz.Matrix(hd_zoom, hd_zoom), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        src_doc.close()
        return {"hd_image": f"data:image/png;base64,{b64}", "width": pix.width, "height": pix.height}

    slices, _, _, _ = analyze_page_slices(page, pno, options, dpi=150)
    temp_out = fitz.open()
    if pno == 0 and options.cover_mode != "none":
        _insert_cover_page_to_doc(src_doc, input_pdf_path, temp_out, options)
    _render_slices_to_doc(src_doc, pno, slices, temp_out, options)
    if len(temp_out) == 0:
        temp_out.close()
        src_doc.close()
        return {"error": "该页已标记为【跳过】，未生成 KPW6 预览屏", "skipped": True}
    s_idx = max(0, min(len(temp_out) - 1, screen_idx))
    kp = temp_out[s_idx]
    # 6.0x matrix -> 2160 x 2871 px (600 PPI Ultra-HD!)
    kp_pix = kp.get_pixmap(matrix=fitz.Matrix(6.0, 6.0), alpha=False)
    kp_img = Image.frombytes("RGB", (kp_pix.width, kp_pix.height), kp_pix.samples)
    buf = io.BytesIO()
    kp_img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    w_px, h_px = kp_pix.width, kp_pix.height
    temp_out.close()
    src_doc.close()
    return {"hd_image": f"data:image/png;base64,{b64}", "width": w_px, "height": h_px}
