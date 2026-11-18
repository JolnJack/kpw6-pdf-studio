"""Interactive side-by-side preview comparison and 600-PPI lightbox zoom rendering.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import base64
import io
from typing import Any, Dict, List

from PIL import Image, ImageDraw
import pymupdf as fitz

from kpw6_studio.engine.cover import _insert_cover_page_to_doc
from kpw6_studio.engine.document import _open_source_pdf, extract_book_meta
from kpw6_studio.engine.exporters import _render_slices_to_doc
from kpw6_studio.engine.layout import analyze_page_slices
from kpw6_studio.engine.models import (
    KPW6_ASPECT,
    KPW6_PT_W,
    ProcessOptions,
    get_target_page_list,
)

def _has_preview_cover(total_pages: int, pno: int, options: ProcessOptions) -> bool:
    """Show the generated cover on the first non-skipped page of the active export range."""
    if options.cover_mode == "none" or total_pages <= 0:
        return False
    page_override = (options.page_overrides or {}).get(str(pno), "auto")
    if page_override == "skip":
        return False
    try:
        target_pages = get_target_page_list(total_pages, options)
    except ValueError:
        target_pages = []
    anchor_page = target_pages[0] if target_pages else max(0, min(total_pages - 1, (options.start_page or 1) - 1))
    return pno == anchor_page


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
    has_cover_on_this_page = _has_preview_cover(total_pages, pno, options)
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
    if _has_preview_cover(total_pages, pno, options):
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
