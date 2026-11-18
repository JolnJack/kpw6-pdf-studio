"""2-column academic segmentation, sideways figure uprighting, comic split, and slice cutting.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

import numpy as np
import pymupdf as fitz

from kpw6_studio.engine.image_ops import (
    compute_ink_mask,
    find_tight_bbox,
    render_page_gray,
    strip_running_headers_footers,
)
from kpw6_studio.engine.models import KPW6_PT_H, KPW6_PT_W, ProcessOptions, SliceBox
from kpw6_studio.engine.text_reflow import extract_structured_text_from_page

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
      Splits each figure and chooses the rotation direction from its native text so it
      fills the entire KPW6 portrait screen!
    - If a full-page sideways table:
      Keeps the entire table 100% intact on a single KPW6 screen without chopping rows or columns.
    """
    pw, ph = page.rect.width, page.rect.height
    d = page.get_text("dict")
    rot_chars = 0
    horiz_chars = 0
    downward_chars = 0
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
                if dy > 0:
                    downward_chars += len(txt)
            else:
                horiz_chars += len(txt)

    if rot_chars <= horiz_chars or rot_chars < 15:
        return None
    upright_rotation = 90 if downward_chars > rot_chars / 2 else -90

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
                    slices_out.append(SliceBox(page_num=page_num, rect=r, kind="figure_rot", out_page_idx=out_idx, rotate=upright_rotation))
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
    # Turn a wide sideways figure in the direction indicated by its native text.
    rot_angle = upright_rotation if (bw > bh * 1.15) else 0
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
