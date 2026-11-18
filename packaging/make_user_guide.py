"""Deterministically generate docs/使用说明.pdf using PyMuPDF (zero extra dependencies)."""
from __future__ import annotations

import argparse
from pathlib import Path
import pymupdf as fitz


ROOT = Path(__file__).resolve().parents[1]
PAGE_W, PAGE_H = 595.28, 841.89  # A4 portrait
MARGIN_X = 48.0


def _draw_chrome(page: fitz.Page, font: fitz.Font, page_idx: int, total_pages: int) -> None:
    tw = fitz.TextWriter(page.rect)
    tw.append((MARGIN_X, 34), "KPW6 PDF Studio · Kindle PDF 排版工具 / 买家使用说明", font=font, fontsize=8.5)
    tw.write_text(page, color=(0.42, 0.46, 0.52))
    page.draw_line((MARGIN_X, 42), (PAGE_W - MARGIN_X, 42), color=(0.82, 0.85, 0.89), width=0.6)

    page.draw_line((MARGIN_X, PAGE_H - 42), (PAGE_W - MARGIN_X, PAGE_H - 42), color=(0.82, 0.85, 0.89), width=0.6)
    tw_f = fitz.TextWriter(page.rect)
    tw_f.append((MARGIN_X, PAGE_H - 27), "Windows · Intel Mac · Apple M 系列 Mac", font=font, fontsize=8.5)
    tw_f.append((PAGE_W - MARGIN_X - 18, PAGE_H - 27), f"{page_idx} / {total_pages}", font=font, fontsize=8.5)
    tw_f.write_text(page, color=(0.45, 0.49, 0.55))


def _write_block(page: fitz.Page, font: fitz.Font, y: float, text: str, *,
                 size: float = 10.2, color=(0.14, 0.17, 0.22), lineheight: float = 1.48,
                 indent: float = 0.0) -> float:
    avail_w = PAGE_W - 2 * MARGIN_X - indent
    probe = fitz.TextWriter(fitz.Rect(0, 0, PAGE_W, 4000))
    probe.fill_textbox(fitz.Rect(MARGIN_X + indent, 0, PAGE_W - MARGIN_X, 3800),
                       text, font=font, fontsize=size, lineheight=lineheight)
    used_h = max(size * lineheight, probe.text_rect.height + 2.0)
    tw = fitz.TextWriter(page.rect)
    tw.fill_textbox(fitz.Rect(MARGIN_X + indent, y, PAGE_W - MARGIN_X, y + used_h + 8.0),
                    text, font=font, fontsize=size, lineheight=lineheight)
    tw.write_text(page, color=color)
    return y + used_h


def generate_user_guide(output_path: Path = ROOT / "dist" / "Guide.pdf") -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = fitz.Font("cjk")
    doc = fitz.open()

    # ---------------- Page 1: 选择版本与安装启动 ----------------
    p1 = doc.new_page(width=PAGE_W, height=PAGE_H)
    _draw_chrome(p1, font, 1, 3)
    y = 72.0
    y = _write_block(p1, font, y, "买家使用说明", size=21.0, color=(0.07, 0.11, 0.18), lineheight=1.25) + 4
    y = _write_block(p1, font, y, "Windows · Intel Mac · Apple M 系列 Mac（免安装 Python / 本机离线运行）",
                     size=10.5, color=(0.35, 0.40, 0.47)) + 12
    y = _write_block(p1, font, y, "第一次使用：下载对应版本 → 安装并打开 → 添加 PDF → 预览排版 → 导出文件。\n三个版本使用方法完全一致，只需下载与你电脑匹配的一个安装包。",
                     size=10.2) + 14

    y = _write_block(p1, font, y, "一、我应该下载哪个版本？", size=13.0, color=(0.06, 0.45, 0.36)) + 6
    for item in (
        "• Windows 电脑（Intel / AMD，64 位）：下载 Windows版.zip（或 GitHub 上的 Windows.zip）",
        "• Apple M 系列芯片的 Mac（M1/M2/M3/M4）：下载 Mac版（M系列）.dmg（或 Mac-M.dmg）",
        "• Intel 芯片的 Mac：下载 Mac版（Intel）.dmg（或 Mac-Intel.dmg）",
    ):
        y = _write_block(p1, font, y, item, size=10.0, indent=6.0) + 3
    y += 4
    y = _write_block(p1, font, y, "点击屏幕左上角「苹果菜单  → 关于本机」即可查看 Mac 芯片类型：显示“芯片 Apple M…”选 M 系列版；显示“处理器 Intel…”选 Intel 版。两个 Mac 版本均需 macOS 14 或更新系统。",
                     size=9.6, color=(0.28, 0.32, 0.38)) + 14

    y = _write_block(p1, font, y, "二、Windows 如何安装和打开？", size=13.0, color=(0.06, 0.45, 0.36)) + 6
    for step in (
        "1. 右键下载好的“Windows版.zip”，选择“全部解压缩”，完整解压到电脑固定文件夹。",
        "2. 打开解压后的文件夹，双击运行“双击启动_Kindle精排大师.bat”。",
        "3. 等待系统默认浏览器自动弹出本机排版工作台页面。",
    ):
        y = _write_block(p1, font, y, step, size=10.0) + 3
    y = _write_block(p1, font, y, "注意：请勿直接在压缩包内双击，也不要把 EXE 单独拖出文件夹；运行期间请保持后台命令窗口打开。",
                     size=9.5, color=(0.28, 0.32, 0.38)) + 14

    y = _write_block(p1, font, y, "三、Mac 如何安装和首次放行？（M 系列 / Intel 通用）", size=13.0, color=(0.06, 0.45, 0.36)) + 6
    for step in (
        "1. 双击打开对应芯片的 DMG 映像，将「KPW6 PDF Studio」拖入右侧「Applications（应用程序）」文件夹。",
        "2. 打开「应用程序」，双击「KPW6 PDF Studio」，等待浏览器自动打开排版工作台。",
        "3. 由于当前预发布版采用本地签名、尚未完成 Apple 公证，首次从网盘/浏览器下载打开若提示“无法验证开发者”或“已损坏”：",
        "   • 方法 A（推荐界面放行）：点提示框关闭后，打开「系统设置 → 隐私与安全性」，滑到底部点击「KPW6 PDF Studio」旁的【仍要打开】。",
        "   • 方法 B（终端清除下载隔离标记）：打开“终端”，执行下面命令后再双击即可正常启动：",
        '     xattr -cr "/Applications/KPW6 PDF Studio.app"',
    ):
        y = _write_block(p1, font, y, step, size=9.8) + 3

    # ---------------- Page 2: 排版与导出 ----------------
    p2 = doc.new_page(width=PAGE_W, height=PAGE_H)
    _draw_chrome(p2, font, 2, 3)
    y = 72.0
    y = _write_block(p2, font, y, "排版与导出步骤", size=20.0, color=(0.07, 0.11, 0.18), lineheight=1.25) + 6
    y = _write_block(p2, font, y, "添加 PDF → 选择文档模式 → 对照预览校对 → 选择格式导出。", size=10.2, color=(0.35, 0.40, 0.47)) + 14

    sections_p2 = [
        ("第 1 步：添加 PDF", [
            "点击左侧“点击上传或将 PDF 拖到这里”，选择要处理的 PDF 文件。",
            "这里的“上传”仅将文件读入本机软件处理（保存在 ~/Documents/KPW6_PDF_Studio），绝不会上传到外部云端；不需要的历史文件可点击名称旁「✕」一键移除。",
        ]),
        ("第 2 步：选择文档类型（软件会自动推荐）", [
            "• 左右两栏排版的英文/中文论文：选「双栏学术论文」（自动保护跨栏图表并扶正侧向大图）。",
            "• 含公式、表格和架构图的单栏教材/技术书：选「带图表的专业书」。",
            "• 扫描、影印形成的灰底/泛黄旧书：选「扫描版书籍」（去网格漂白 + 墨迹加深 + 可选加粗与纠偏）。",
            "• 双页连跨漫画或画册：选「PDF 漫画 / 纯图画册」（支持中缝拆切、日漫右起顺序与满屏输出）。",
            "• 可复制文字的小说/社科长文：选「纯文字 PDF 书籍」（智能去页眉页码、合并断句并提取章节目录）。",
        ]),
        ("第 3 步：检查预览与单页微调", [
            "右侧提供 1264×1680 目标屏幕预览与 600 PPI 高清放大镜。首次建议先用默认参数查看正文、公式、图表和阅读顺序。",
            "遇到跨页复杂大图可将该页单独设为「强制整页不切分」或「旋转 90° 横置满屏」，无需阅读的空白/广告页可设为「剔除跳过」。",
        ]),
        ("第 4 步：选择导出格式", [
            "• 保留图表、公式与分屏版面：选「.PDF 专享」（矢量优先）或「.EPUB 精排」（图像版，含封面与目录）。",
            "• 纯文字书籍希望在 Kindle 上自由调节字号与查词典：选「.EPUB 流式」。",
            "• .AZW3 / .MOBI：需电脑另行安装开源软件 Calibre；仅用 Send to Kindle 或 PDF 直传可忽略。",
            "• .CBZ：无损漫画归档包（含 ComicInfo.xml），供漫画阅读器使用。",
        ]),
        ("第 5 步：一键导出（支持中途取消与自动分卷）", [
            "首次处理几百页大书前，建议先用“仅导当前页”或指定前几页试读确认效果，再导出全书；长书可开启 50/100/150 页自动分卷。导出期间进度条右侧支持随时点击「取消」，且预览翻页不会卡死。",
        ]),
    ]
    for heading, lines in sections_p2:
        y = _write_block(p2, font, y, heading, size=12.5, color=(0.06, 0.45, 0.36)) + 5
        for ln in lines:
            y = _write_block(p2, font, y, ln, size=9.9) + 3
        y += 9

    # ---------------- Page 3: 传书与常见问题 ----------------
    p3 = doc.new_page(width=PAGE_W, height=PAGE_H)
    _draw_chrome(p3, font, 3, 3)
    y = 72.0
    y = _write_block(p3, font, y, "传书到 Kindle 与常见问题", size=20.0, color=(0.07, 0.11, 0.18), lineheight=1.25) + 12

    y = _write_block(p3, font, y, "怎么把导出的书传到 Kindle？", size=12.8, color=(0.06, 0.45, 0.36)) + 6
    stk_url = "https://www.amazon.com/sendtokindle"
    help_url = "https://www.amazon.com/gp/help/customer/display.html?nodeId=G5WYD9SAF7PGXRNA"

    y_stk_top = y
    y = _write_block(p3, font, y, f"1. 网页无线传书（推荐）：打开官方 Send to Kindle 页面 {stk_url} ，登录与你 Kindle 绑定的亚马逊账号，拖入导出的 .EPUB 或 .PDF 文件，发送后让 Kindle 联网同步即可。", size=9.9) + 4
    p3.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(MARGIN_X, y_stk_top, PAGE_W - MARGIN_X, y), "uri": stk_url})

    y = _write_block(p3, font, y, "2. USB 数据线传书：请按对应 Kindle 型号支持的格式拷入 documents 目录（较新款 Kindle 2024 / KPW6 连接 Mac 时使用 MTP 协议，需使用官方 Send to Kindle 应用或 OpenMTP 等传输工具；EPUB 请勿仅靠改后缀名直拷）。", size=9.9) + 4

    y_help_top = y
    y = _write_block(p3, font, y, f"3. 亚马逊官方传书帮助文档：\n{help_url}", size=9.3, color=(0.12, 0.38, 0.78)) + 14
    p3.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(MARGIN_X, y_help_top, PAGE_W - MARGIN_X, y - 10), "uri": help_url})

    y = _write_block(p3, font, y, "常见问题解答", size=12.8, color=(0.06, 0.45, 0.36)) + 6
    faqs = [
        ("1. 为什么双击后打开的是浏览器网页？", "软件采用本地桌面引擎 + 浏览器可视化界面架构，所有排版运算 100% 在本机完成。使用期间请勿关闭后台软件。"),
        ("2. Mac 关掉浏览器标签页后怎么重新打开或彻底退出？", "点击屏幕顶部菜单栏右侧的「KPW6精排」图标，即可重新打开工作台、打开导出目录，或点击「退出 Kindle 精排大师」彻底关闭后台服务。"),
        ("3. Windows 使用完毕如何彻底退出？", "导出完成后关闭网页，并关闭启动时弹出的黑色命令窗口即可自动清理所有后台进程。"),
        ("4. 为什么扫描版 PDF 不能直接转成可调字号的纯文字书？", "纯图像扫描件不含文字编码层；本工具的扫描模式专为去除灰底抖动网格、加深字迹和切白边放大而设计，不包含内置 OCR 文字识别。"),
        ("5. 遇到异常如何联系反馈？", "请提供电脑系统版本、Mac 芯片类型（M 系列或 Intel）、提示截图以及复现步骤，切勿覆盖或删除你的原始 PDF 文件。"),
    ]
    for q, a in faqs:
        y = _write_block(p3, font, y, q, size=10.1, color=(0.08, 0.12, 0.18)) + 2
        y = _write_block(p3, font, y, a, size=9.6, color=(0.26, 0.30, 0.36), indent=8.0) + 7

    doc.set_metadata({
        "title": "Kindle PDF 排版工具 - 买家使用说明",
        "author": "KPW6 PDF Studio",
        "subject": "Windows / Intel Mac / Apple Silicon Mac 使用指南",
        "creator": "KPW6 PDF Studio",
        "producer": f"KPW6 PDF Studio / PyMuPDF {fitz.VersionBind} (AGPL-3.0)",
    })
    doc.subset_fonts()
    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()
    return output_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "Guide.pdf")
    args = parser.parse_args()
    out = generate_user_guide(args.output)
    print(f"Generated user guide PDF: {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
