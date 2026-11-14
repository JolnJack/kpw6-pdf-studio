import io
import os
import pymupdf as fitz
import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def generate_demo_pdfs(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "academic_2col": os.path.join(out_dir, "demo_双栏学术论文.pdf"),
        "tech_book": os.path.join(out_dir, "demo_带图表专业书.pdf"),
        "scanned_book": os.path.join(out_dir, "demo_泛黄扫描版书籍.pdf"),
        "comic_manga": os.path.join(out_dir, "demo_PDF漫画双页跨页.pdf"),
        "pure_text": os.path.join(out_dir, "demo_纯文字小说书籍.pdf"),
    }

    _make_academic_2col(paths["academic_2col"])
    _make_tech_book(paths["tech_book"])
    _make_scanned_book(paths["scanned_book"])
    _make_comic_manga(paths["comic_manga"])
    _make_pure_text(paths["pure_text"])
    return paths


def _make_academic_2col(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    page.insert_text((65, 36), "IEEE TRANSACTIONS ON E-INK DISPLAY SYSTEMS, VOL. 18, NO. 4, NOVEMBER 2026", fontsize=7.5, color=(0.35, 0.35, 0.35))

    page.insert_text((88, 78), "Adaptive Waveform & XY-Cut Layout Optimization", fontsize=16, fontname="helv")
    page.insert_text((112, 98), "for 300 PPI Carta 1300 E-Reader Displays", fontsize=15, fontname="helv")
    page.insert_text((160, 118), "A. Researcher, B. Engineer, and C. Scientist — Lab126 Open Community", fontsize=9.5)

    abs_rect = fitz.Rect(75, 132, 520, 198)
    page.draw_rect(abs_rect, color=(0.75, 0.75, 0.75), fill=(0.97, 0.97, 0.97), width=0.6)
    abs_text = (
        "Abstract—Reading standard A4 two-column academic papers on a 7-inch e-reader (1264x1680 px) "
        "results in miniature 5pt glyphs and excessive margin waste. Naive EPUB conversion destroys "
        "mathematical notation and interleaves adjacent columns. We present a vector-preserving XY-Cut "
        "decomposition pipeline that separates full-width spans from dual-column streams."
    )
    page.insert_textbox(fitz.Rect(83, 138, 512, 194), abs_text, fontsize=8.8, fontname="times-roman")

    left_paragraphs = [
        ("I. INTRODUCTION", 10.0),
        (
            "Modern electronic paper displays such as the E-Ink Carta 1300 panel achieve 300 PPI spatial "
            "resolution with rapid binary A2 waveform updates. However, physical screen dimensions remain "
            "constrained to 7 inches diagonally. When an A4 manuscript is rendered full-page, over 34% "
            "of the active display area is occupied by empty margins and running headers.",
            8.8,
        ),
        ("E(x, y) = sum_{i=1}^{N} alpha_i * phi(w_i * x - b_i)   (1)", 8.8),
        (
            "As shown in Equation (1), preserving vector glyph outlines during spatial cropping guarantees "
            "zero rasterization blur while boosting effective font height by 2.5x on the target viewport.",
            8.8,
        ),
        ("II. RECURSIVE XY-CUT DECOMPOSITION", 10.0),
        (
            "By projecting horizontal and vertical ink densities onto orthogonal axes, our engine locates "
            "safe whitespace valleys between paragraphs, equations, and embedded vector figures. No line "
            "of text is ever bisected horizontally.",
            8.8,
        ),
    ]

    y_cur = 214
    for txt, fsize in left_paragraphs:
        h_box = 22 if fsize > 9.5 else 68
        if "(1)" in txt:
            h_box = 26
        r = fitz.Rect(65, y_cur, 282, y_cur + h_box)
        page.insert_textbox(r, txt, fontsize=fsize, fontname="times-roman")
        y_cur += h_box + 8

    right_paragraphs = [
        ("III. DE-DITHERING FOR CARTA 1300", 10.0),
        (
            "During fast menu transitions or when rendering off-white scanned backgrounds, the display "
            "controller substitutes 16-level grayscale waveforms with a 1-bit spatial checkerboard halftone. "
            "Bleaching background luminance above the 88th percentile to pure #FFFFFF eliminates unwanted "
            "checkerboard texture completely.",
            8.8,
        ),
        (
            "Furthermore, applying a non-linear gamma curve (gamma = 0.55) deepens faint toner strokes "
            "without thickening serifs or merging adjacent sub-pixel stems.",
            8.8,
        ),
        ("IV. EXPERIMENTAL EVALUATION", 10.0),
        (
            "Across 150 IEEE and ACM double-column manuscripts, vector slicing maintained file sizes below "
            "3 MB while doubling reading speed and eliminating manual pinch-to-zoom gestures on KPW6.",
            8.8,
        ),
    ]

    y_cur = 214
    for txt, fsize in right_paragraphs:
        h_box = 22 if fsize > 9.5 else 72
        r = fitz.Rect(312, y_cur, 530, y_cur + h_box)
        page.insert_textbox(r, txt, fontsize=fsize, fontname="times-roman")
        y_cur += h_box + 8

    fig_box = fitz.Rect(65, 595, 530, 755)
    page.draw_rect(fig_box, color=(0.2, 0.35, 0.6), width=1.2)
    bars = [("Raw A4 PDF", 0.32, (0.6, 0.6, 0.6)), ("Margin Crop", 0.58, (0.25, 0.55, 0.85)), ("KPW6 XY-Cut", 0.92, (0.1, 0.65, 0.45))]
    for idx, (label, val, col) in enumerate(bars):
        bx0 = 110 + idx * 140
        by1 = 715
        bh = val * 95
        page.draw_rect(fitz.Rect(bx0, by1 - bh, bx0 + 70, by1), color=col, fill=col)
        page.insert_text((bx0 + 6, by1 + 14), label, fontsize=8.5)
        page.insert_text((bx0 + 18, by1 - bh - 6), f"{int(val*100)}%", fontsize=8.5)
    page.insert_text((115, 745), "Fig. 1. Full-Width Spanning Chart: Relative Glyph Readability Score on 7-inch KPW6.", fontsize=8.5)

    page.insert_text((285, 812), "— 42 —", fontsize=8.5, color=(0.4, 0.4, 0.4))
    doc.save(path)
    doc.close()


def _make_tech_book(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    page.insert_text((95, 42), "Chapter 4: Distributed Storage Engine Architecture", fontsize=8, color=(0.4, 0.4, 0.4))
    page.insert_text((95, 92), "4.2 High-Throughput LSM-Tree Compaction", fontsize=15, fontname="helv")
    p1 = (
        "Technical books often feature wide margins (over 1.3 inches on each side) intended for print binding "
        "and handwritten notes. On a 7-inch Kindle screen, those wide margins shrink both body paragraphs "
        "and architecture diagrams to an uncomfortable scale."
    )
    page.insert_textbox(fitz.Rect(95, 112, 500, 175), p1, fontsize=9.8, fontname="times-roman")

    diag = fitz.Rect(95, 190, 500, 345)
    page.draw_rect(diag, color=(0.2, 0.2, 0.2), fill=(0.98, 0.98, 0.99), width=1.0)
    nodes = [("Write-Ahead Log", 115, 225), ("MemTable (RAM)", 248, 225), ("SSTable L0..Ln", 380, 225)]
    for title, nx, ny in nodes:
        r = fitz.Rect(nx, ny, nx + 102, ny + 55)
        page.draw_rect(r, color=(0.15, 0.4, 0.7), fill=(0.9, 0.95, 1.0), width=1.2)
        page.insert_text((nx + 10, ny + 30), title, fontsize=8.5)
    page.insert_text((135, 325), "Figure 4-2: Write Path and Compaction Pipeline (Kept Intact Without Slicing)", fontsize=8.5)

    p2 = (
        "When KPW6 PDF Studio processes a technical book in 'Tech Book' mode, it strips the outer margins "
        "and running headers, then locates safe horizontal whitespace between paragraphs and diagrams so "
        "tables and figures are never cut in half."
    )
    page.insert_textbox(fitz.Rect(95, 365, 500, 430), p2, fontsize=9.8, fontname="times-roman")

    tbl_top = 445
    headers = ["Level", "Max Size", "Compaction Strategy", "Read Latency"]
    rows = [
        ["MemTable", "64 MB", "In-Memory SkipList", "< 0.05 ms"],
        ["Level 0", "256 MB", "4 Overlapping Tiered", "0.40 ms"],
        ["Level 1", "1.0 GB", "10x Leveled Merge", "1.10 ms"],
        ["Level 2", "10.0 GB", "10x Leveled + Bloom", "2.35 ms"],
    ]
    col_w = 101
    for c_idx, h_text in enumerate(headers):
        cell = fitz.Rect(95 + c_idx * col_w, tbl_top, 95 + (c_idx + 1) * col_w, tbl_top + 24)
        page.draw_rect(cell, color=(0.3, 0.3, 0.3), fill=(0.88, 0.9, 0.94), width=0.7)
        page.insert_text((cell.x0 + 8, cell.y0 + 16), h_text, fontsize=8.5, fontname="helv")

    for r_idx, row in enumerate(rows):
        ry = tbl_top + 24 * (r_idx + 1)
        for c_idx, val in enumerate(row):
            cell = fitz.Rect(95 + c_idx * col_w, ry, 95 + (c_idx + 1) * col_w, ry + 24)
            page.draw_rect(cell, color=(0.5, 0.5, 0.5), width=0.5)
            page.insert_text((cell.x0 + 8, cell.y0 + 16), val, fontsize=8.5)

    p3 = (
        "Notice how the table above and the diagram remain 100% vector graphics and are packed cleanly onto "
        "consecutive KPW6 screens at nearly 2x magnification."
    )
    page.insert_textbox(fitz.Rect(95, 595, 500, 665), p3, fontsize=9.8, fontname="times-roman")

    page.insert_text((290, 805), "128", fontsize=9, color=(0.4, 0.4, 0.4))
    doc.save(path)
    doc.close()


def _make_scanned_book(path: str) -> None:
    temp = fitz.open()
    p = temp.new_page(width=595, height=842)
    p.insert_text((110, 110), "Section 3: Historical Archive Scan (Low Contrast)", fontsize=14, color=(0.32, 0.32, 0.32))
    body = (
        "Scanned books often have a light gray or yellowish paper background (luminance 205-230 instead of "
        "pure white 255), along with dark scanner edge shadows and washed-out gray ink.\n\n"
        "When displayed directly on a Kindle Paperwhite 6 (Carta 1300), that off-white background forces the "
        "E-Ink controller to render a pervasive checkerboard dithering grid pattern across the entire screen!\n\n"
        "KPW6 PDF Studio's Scanned Book Enhancer automatically:\n"
        "  1. Strips dark scanner borders and wide margins;\n"
        "  2. Bleaches the paper background to pure #FFFFFF (zero checkerboard dithering!);\n"
        "  3. Applies gamma darkening (0.55) and unsharp masking so faint text becomes crisp ink."
    )
    p.insert_textbox(fitz.Rect(110, 145, 485, 650), body, fontsize=11.5, color=(0.35, 0.35, 0.35), fontname="times-roman")

    pix = p.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), colorspace=fitz.csGRAY, alpha=False)
    temp.close()

    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.float32)
    yy, xx = np.mgrid[0:pix.height, 0:pix.width]
    vignette = 214.0 - 16.0 * ((xx / pix.width - 0.5) ** 2 + (yy / pix.height - 0.5) ** 2)
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 3.5, size=arr.shape)
    simulated = np.minimum(arr, vignette + noise)
    simulated[:8, :] = 45
    simulated[:, :8] = 45
    simulated_u8 = np.clip(simulated, 0, 255).astype(np.uint8)

    pil_scan = Image.fromarray(simulated_u8, mode="L").filter(ImageFilter.GaussianBlur(radius=0.45))
    buf = io.BytesIO()
    pil_scan.save(buf, format="PNG")

    out = fitz.open()
    op = out.new_page(width=595, height=842)
    op.insert_image(op.rect, stream=buf.getvalue())
    out.save(path)
    out.close()


def _make_comic_manga(path: str) -> None:
    """
    Create a realistic double-page landscape manga/comic spread PDF (width 900 x height 640)
    with outer margins, right page (Page 1 in Manga RTL order) and left page (Page 2 in Manga RTL order),
    comic panels, screentone gradients, action lines, and speech bubbles.
    """
    doc = fitz.open()
    # Double-page landscape spread: 900 x 640 pt
    page = doc.new_page(width=900, height=640)

    # Outer gray/white margin around the spread
    # Right-hand page of spread (x: 465..845, y: 45..595) -> Read FIRST in Japanese Manga (RTL)!
    # Left-hand page of spread  (x: 55..435,  y: 45..595) -> Read SECOND in Japanese Manga (RTL)!

    # --- RIGHT PAGE (Manga Page 1) ---
    p_right = fitz.Rect(465, 45, 845, 595)
    page.draw_rect(p_right, color=(0, 0, 0), width=2.5)
    # Panel 1 (Top Right)
    r_p1 = fitz.Rect(480, 60, 830, 290)
    page.draw_rect(r_p1, color=(0, 0, 0), fill=(0.92, 0.92, 0.92), width=2.0)
    # Draw character silhouette / geometric manga elements
    page.draw_circle((610, 185), 55, color=(0.1, 0.1, 0.1), fill=(0.25, 0.25, 0.25), width=2.0)
    # Speech bubble
    bubble1 = fitz.Rect(685, 78, 815, 225)
    page.draw_oval(bubble1, color=(0, 0, 0), fill=(1, 1, 1), width=1.8)
    page.insert_textbox(
        fitz.Rect(705, 105, 798, 205),
        "MANGA RIGHT PAGE\n(Read 1st in RTL!)\n\nAuto-split double spread & full-bleed 1264x1680!",
        fontsize=9.5,
        fontname="helv",
    )

    # Panel 2 (Bottom Right)
    r_p2 = fitz.Rect(480, 305, 830, 580)
    page.draw_rect(r_p2, color=(0, 0, 0), fill=(0.96, 0.96, 0.96), width=2.0)
    # Action speed lines
    for i in range(12):
        page.draw_line((485, 315 + i * 22), (660, 440), color=(0.2, 0.2, 0.2), width=1.0)
    page.draw_rect(fitz.Rect(580, 360, 810, 540), color=(0, 0, 0), fill=(1, 1, 1), width=1.8)
    page.insert_textbox(
        fitz.Rect(595, 380, 795, 525),
        "PANEL 2 (RIGHT SIDE)\n\nNotice how wide outer borders are cropped and contrast is boosted for Carta 1300 E-Ink!",
        fontsize=10.5,
        fontname="helv",
    )

    # --- LEFT PAGE (Manga Page 2) ---
    p_left = fitz.Rect(55, 45, 435, 595)
    page.draw_rect(p_left, color=(0, 0, 0), width=2.5)
    # Panel 3 (Top Left)
    l_p1 = fitz.Rect(70, 60, 420, 320)
    page.draw_rect(l_p1, color=(0, 0, 0), fill=(0.88, 0.88, 0.88), width=2.0)
    bubble2 = fitz.Rect(88, 82, 395, 210)
    page.draw_oval(bubble2, color=(0, 0, 0), fill=(1, 1, 1), width=1.8)
    page.insert_textbox(
        fitz.Rect(118, 105, 370, 195),
        "MANGA LEFT PAGE\n(Read 2nd in RTL, or 1st in LTR!)\n\nNo more tiny landscape spreads on a 7-inch screen!",
        fontsize=9.5,
        fontname="helv",
    )

    # Panel 4 (Bottom Left)
    l_p2 = fitz.Rect(70, 335, 420, 580)
    page.draw_rect(l_p2, color=(0, 0, 0), fill=(0.95, 0.95, 0.95), width=2.0)
    page.draw_circle((245, 460), 75, color=(0, 0, 0), fill=(0.18, 0.18, 0.18), width=2.2)
    page.draw_rect(fitz.Rect(130, 430, 360, 495), color=(0, 0, 0), fill=(1, 1, 1), width=1.5)
    page.insert_text((148, 468), "FULL-BLEED 300 PPI CARTA 1300!", fontsize=11, fontname="helv")

    doc.save(path)
    doc.close()


def _make_pure_text(path: str) -> None:
    """
    Create a multi-page Pure Text PDF with Chinese & English paragraphs, hard line breaks,
    running headers, page numbers, and chapter headings to test smart EPUB & reflow conversion.
    """
    doc = fitz.open()
    font = fitz.Font("cjk")

    pages_content = [
        {
            "header": "《墨水屏阅读的艺术》 · 第一卷",
            "chapter": "第一章 为什么纯文字PDF应该重排",
            "lines": [
                "很多人手头有大量的纯文字版 PDF 电子书（比如文学小说、历史社科或人物传",
                "记）。如果直接把这种按 A4 纸排版的 PDF 发到 7 英寸的 Kindle Paperwhite 6",
                "签名版上，不仅四周全是空荡荡的白边，而且字号小得让人眼睛酸痛。",
                "",
                "更糟糕的是，PDF 格式每一行末尾都有硬回车（Hard Line Break），每一页顶部",
                "和底部还夹杂着书名页眉与页码。如果用普通工具强行转换，往往会出现一句话被切",
                "成两段、或者读着读着突然蹦出页码数字的尴尬情况。",
                "",
                "通过我们的【纯文字 PDF 智能提取与重排引擎】，系统会自动完成以下四件事：",
                "第一，精准剥离每页顶部与底部的重复页眉、水印和页码数字；",
                "第二，根据标点符号与行宽特征，把跨行甚至跨页断裂的句子无缝拼接回完整的自然",
                "段落；",
                "第三，自动识别「第X章 / Chapter X」标题，生成支持 Kindle 原生跳转的章节导航",
                "目录（TOC）；",
                "第四，直接一键输出符合亚马逊 Send to Kindle 标准的精排 EPUB 电子书（以及可",
                "选的大字版重排 PDF），让你在 KPW6 上自由调节字号、行距并切换字体！",
            ],
            "footer": "— 1 —",
        },
        {
            "header": "《墨水屏阅读的艺术》 · 第一卷",
            "chapter": "第二章 告别网格残影与排版乱码",
            "lines": [
                "当你把本工具生成的精排 EPUB 通过亚马逊官方网页（Send to Kindle）推送到",
                "KPW6 签名版后，云端会自动将其转换为原生的 KFX 排版引擎格式。",
                "",
                "在原生文字排版引擎下，Kindle 能够直接调用矢量字库进行渲染，不仅彻底消除了",
                "图片缩放带来的模糊感，还能支持长按划词查词典、高亮批注以及跨设备阅读进度同",
                "步，真正发挥出 Carta 1300 墨水屏 300 PPI 的极致锐度。",
            ],
            "footer": "— 2 —",
        },
    ]

    for pdata in pages_content:
        page = doc.new_page(width=595, height=842)
        tw = fitz.TextWriter(page.rect)

        # Running header
        tw.append((90, 48), pdata["header"], font=font, fontsize=8.5)
        # Chapter heading
        tw.append((90, 110), pdata["chapter"], font=font, fontsize=16.0)

        y = 155
        for line in pdata["lines"]:
            if not line:
                y += 16
                continue
            tw.append((90, y), line, font=font, fontsize=11.0)
            y += 24

        # Running footer
        tw.append((280, 800), pdata["footer"], font=font, fontsize=9.0)
        tw.write_text(page)

    doc.save(path)
    doc.close()
