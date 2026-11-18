"""Output builders for KPW6 PDF, fixed-layout/reflowable EPUB 3, CBZ, and Calibre formats.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

from datetime import datetime, timezone
import io
import os
import shutil
import uuid
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image
import pymupdf as fitz

from kpw6_studio.engine.cover import _insert_cover_page_to_doc, generate_cover_pil_image
from kpw6_studio.engine.document import (
    _open_source_pdf,
    extract_book_meta,
    scan_document_highlights,
)
from kpw6_studio.engine.image_ops import _insert_enhanced_raster_slice
from kpw6_studio.engine.layout import analyze_page_slices
from kpw6_studio.engine.models import (
    ExtractedParagraph,
    KPW6_PT_H,
    KPW6_PT_W,
    KPW6_PX_H,
    KPW6_PX_W,
    ProcessOptions,
    SliceBox,
    TERMINAL_PUNCT,
    _encode_eink_png_bytes,
    _xml_clean,
    _xml_escape,
    get_target_page_list,
)
from kpw6_studio.engine.text_reflow import (
    _join_wrapped_text,
    _render_pure_text_items_to_kpw6_doc,
    extract_structured_text_from_page,
)

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
    detected_pure_text_toc: List[List[Any]] = []

    if options.mode == "pure_text":
        all_items: List[ExtractedParagraph] = []
        overrides = options.page_overrides or {}
        for step_i, pno in enumerate(target_pages, start=1):
            if overrides.get(str(pno), "auto") in ("whole_portrait", "whole_landscape"):
                if all_items:
                    detected_pure_text_toc.extend(
                        _render_pure_text_items_to_kpw6_doc(all_items, out_doc, options, page_map=page_map)
                    )
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
                    all_items[-1].text = _join_wrapped_text(prev_t, nxt_t)
                    p_items = p_items[1:]
            all_items.extend(p_items)
        if all_items:
            detected_pure_text_toc.extend(
                _render_pure_text_items_to_kpw6_doc(all_items, out_doc, options, page_map=page_map)
            )
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
        mapped_src_toc = []
        if toc and page_map:
            surviving_levels = []
            for lvl, title, src_p1 in toc:
                src_p0 = max(0, src_p1 - 1)
                clean_title = _xml_clean(title).strip()
                if src_p0 in page_map and clean_title:
                    while surviving_levels and surviving_levels[-1] >= lvl:
                        surviving_levels.pop()
                    surviving_levels.append(lvl)
                    mapped_src_toc.append([len(surviving_levels), clean_title, page_map[src_p0] + 1])
        if mapped_src_toc:
            new_toc.extend(mapped_src_toc)
        elif detected_pure_text_toc:
            new_toc.extend(detected_pure_text_toc)
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
            "producer": (f"KPW6 PDF Studio / PyMuPDF {fitz.VersionBind} / MuPDF {fitz.VersionFitz} "
                         "(AGPL-3.0; Copyright Artifex Software, Inc.)"),
        })
    except Exception:
        pass

    try:
        out_doc.subset_fonts()
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


def build_kpw6_epub(
    input_pdf_path: str,
    output_epub_path: str,
    options: ProcessOptions,
    force_reflow: Optional[bool] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Universal KPW6 EPUB 3 Builder with optional cover metadata:
    - When enabled, embeds a 1264x1680 cover image (OEBPS/images/cover.jpg) with EPUB 3
      (properties="cover-image") and EPUB 2 (<meta name="cover" content="cover-image"/>)
      declarations for compatible readers. cover_mode="none" omits the cover.
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
    has_cover = options.cover_mode != "none"
    if progress_cb and has_cover:
        progress_cb(0, max(1, limit_pages), "正在生成 1264×1680 KPW6 精装封面...")
    if has_cover:
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
    ]
    if has_cover:
        manifest_items.extend([
            '<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>',
            '<item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>',
        ])
    spine_items = ['<itemref idref="cover-page" linear="yes"/>'] if has_cover else []
    nav_lis = ['      <li><a href="cover.xhtml">📖 书籍封面</a></li>'] if has_cover else []
    ncx_navpoints = [
        """    <navPoint id="navPoint-0" playOrder="1">
      <navLabel><text>📖 书籍封面</text></navLabel>
      <content src="cover.xhtml"/>
    </navPoint>"""
    ] if has_cover else []

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
        if has_cover:
            zf.writestr("OEBPS/images/cover.jpg", cover_bytes)

        safe_book_title = _xml_escape(book_title)
        safe_book_author = _xml_escape(book_author)
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
        if has_cover:
            zf.writestr("OEBPS/cover.xhtml", cover_xhtml)

        first_bodymatter_href = ""
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
                            png_bytes = _encode_eink_png_bytes(pix.samples, pix.width, pix.height)
                            zf.writestr(f"OEBPS/{image_rel}", png_bytes, compress_type=zipfile.ZIP_STORED)
                            manifest_items.append(f'<item id="protected_{screens_count:04d}" href="{image_rel}" media-type="image/png"/>')
                            all_items.append(ExtractedParagraph(kind="page_image", text=image_rel, src_page=pno))
                    continue
                page_items = extract_structured_text_from_page(src_doc[pno], pno, trim_hf=options.trim_header_footer)
                if all_items and page_items and all_items[-1].kind == "para" and page_items[0].kind == "para":
                    prev_t = all_items[-1].text
                    if prev_t and prev_t[-1] not in TERMINAL_PUNCT:
                        nxt_t = page_items[0].text
                        all_items[-1].text = _join_wrapped_text(prev_t, nxt_t)
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
                if not first_bodymatter_href:
                    first_bodymatter_href = fname
                cid = f"chap_{idx:03d}"
                safe_title = _xml_escape(ch["title"])
                paras_html = "\n".join(f"  <p>{_xml_escape(p)}</p>" for p in ch["paras"])
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
                    f"""    <navPoint id="navPoint-{idx}" playOrder="{idx + int(has_cover)}">
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
                if len(tmp_doc) > 0:
                    page_to_first_screen[pno] = screen_idx + 1

                for sub_i, kp in enumerate(tmp_doc):
                    screen_idx += 1
                    pix = kp.get_pixmap(matrix=fitz.Matrix(scale_x, scale_y), colorspace=fitz.csGRAY, alpha=False)
                    png_bytes = _encode_eink_png_bytes(pix.samples, pix.width, pix.height)

                    img_rel = f"images/screen_{screen_idx:04d}.png"
                    xhtml_rel = f"screen_{screen_idx:04d}.xhtml"
                    if not first_bodymatter_href:
                        first_bodymatter_href = xhtml_rel
                    zf.writestr(f"OEBPS/{img_rel}", png_bytes, compress_type=zipfile.ZIP_STORED)

                    page_label = _xml_escape(f"原书第 {pno+1} 页 · 第 {sub_i+1} 屏")
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
                    cleaned_t = _xml_clean(t_title).strip()
                    if p0 in page_to_first_screen and cleaned_t:
                        toc_entries.append((cleaned_t, page_to_first_screen[p0]))
            except Exception:
                pass

            src_doc.close()

            # Also include detected Table / Figure highlights in the EPUB TOC so KPW6 user can jump to any Table/Figure!
            for hl in scan_document_highlights(input_pdf_path):
                p0 = hl.get("page_num", 0)
                if p0 in page_to_first_screen:
                    prefix = "📊 " if hl.get("kind") == "table" else "📈 "
                    toc_entries.append((prefix + _xml_clean(hl.get("label", f"P{p0+1}")), page_to_first_screen[p0]))

            if not toc_entries and page_to_first_screen:
                step = 1 if limit_pages <= 15 else (5 if limit_pages <= 80 else 10)
                for idx_p in range(0, len(target_pages), step):
                    p0 = target_pages[idx_p]
                    if p0 in page_to_first_screen:
                        toc_entries.append((f"原书第 {p0+1} 页", page_to_first_screen[p0]))

            chapters_count = len(toc_entries)
            for idx, (entry_title, target_scr) in enumerate(toc_entries, start=1):
                safe_t = _xml_escape(entry_title)
                href = f"screen_{target_scr:04d}.xhtml"
                nav_lis.append(f'      <li><a href="{href}">{safe_t}</a></li>')
                ncx_navpoints.append(
                    f"""    <navPoint id="navPoint-{idx}" playOrder="{idx + int(has_cover)}">
      <navLabel><text>{safe_t}</text></navLabel>
      <content src="{href}"/>
    </navPoint>"""
                )

        # 4. OEBPS/nav.xhtml (with EPUB 3 landmarks)
        landmark_lis = []
        if has_cover:
            landmark_lis.append('      <li><a epub:type="cover" href="cover.xhtml">书籍封面</a></li>')
        if first_bodymatter_href:
            landmark_lis.append(f'      <li><a epub:type="bodymatter" href="{first_bodymatter_href}">正文开始</a></li>')
        landmarks_block = (
            "\n  <nav epub:type=\"landmarks\" hidden=\"\">\n    <h2>导航地标</h2>\n    <ol>\n"
            + "\n".join(landmark_lis)
            + "\n    </ol>\n  </nav>"
        ) if landmark_lis else ""
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
  </nav>{landmarks_block}
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

        # 6. OEBPS/content.opf (EPUB 3 properties="cover-image" + legacy reader <meta name="cover">)
        modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cover_meta = '<meta name="cover" content="cover-image"/>' if has_cover else ''
        cover_guide = '<guide><reference type="cover" title="书籍封面" href="cover.xhtml"/></guide>' if has_cover else ''
        opf_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookId">urn:uuid:{book_uuid}</dc:identifier>
    <dc:title>{safe_book_title}</dc:title>
    <dc:language>zh-CN</dc:language>
    <dc:creator>{safe_book_author}</dc:creator>
    <meta property="dcterms:modified">{modified}</meta>
    {cover_meta}
  </metadata>
  <manifest>
{chr(10).join("    " + m for m in manifest_items)}
  </manifest>
  <spine toc="ncx">
{chr(10).join("    " + s for s in spine_items)}
  </spine>
  {cover_guide}
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

    has_cover = options.cover_mode != "none"
    if has_cover:
        cover_pil = generate_cover_pil_image(src_doc, input_pdf_path, options)
        cover_buf = io.BytesIO()
        cover_pil.save(cover_buf, format="JPEG", quality=92, optimize=True)

    scale_x = KPW6_PX_W / KPW6_PT_W
    scale_y = KPW6_PX_H / KPW6_PT_H
    screen_idx = 0

    with zipfile.ZipFile(output_cbz_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if has_cover:
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
                png_bytes = _encode_eink_png_bytes(pix.samples, pix.width, pix.height)
                zf.writestr(f"{screen_idx:04d}.png", png_bytes, compress_type=zipfile.ZIP_STORED)
            tmp_doc.close()

        comic_info = f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{_xml_escape(book_title)}</Title>
  <Writer>{_xml_escape(book_author)}</Writer>
  <PageCount>{screen_idx + int(has_cover)}</PageCount>
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

    from kpw6_studio.worker.conversion import run_conversion
    run_conversion(cmd)
    return {
        "output_path": output_target_path,
        "output_size_mb": round(os.path.getsize(output_target_path) / (1024 * 1024), 2),
    }
