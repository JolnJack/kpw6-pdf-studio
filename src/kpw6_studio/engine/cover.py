"""Kindle e-ink cover page synthesis and custom cover fitting.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import io
import os

from PIL import Image, ImageOps
import pymupdf as fitz

from kpw6_studio.engine.document import extract_book_meta
from kpw6_studio.engine.image_ops import _insert_enhanced_raster_slice
from kpw6_studio.engine.models import (
    KPW6_PT_H,
    KPW6_PT_W,
    KPW6_PX_H,
    KPW6_PX_W,
    ProcessOptions,
)

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
        tmp_doc.close()
        raise ValueError("当前设置为不生成封面。")
    _insert_cover_page_to_doc(src_doc, input_pdf_path, tmp_doc, eff_opts)
    page = tmp_doc[0]
    scale_x = KPW6_PX_W / KPW6_PT_W
    scale_y = KPW6_PX_H / KPW6_PT_H
    pix = page.get_pixmap(matrix=fitz.Matrix(scale_x, scale_y), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    tmp_doc.close()
    return img
