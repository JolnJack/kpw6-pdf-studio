import os
import subprocess
import sys
from PIL import Image, ImageDraw, ImageFont


def _load_cjk_font(size: int):
    for p in (
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc"),
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simsun.ttc"),
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simhei.ttf"),
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size=size, index=0)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError as exc:
        raise RuntimeError("Generating icons without a system font requires Pillow >= 10.1") from exc


def icon_labels(font):
    """Use ASCII labels when the chosen system/default font lacks CJK glyphs."""
    try:
        missing = bytes(font.getmask(chr(0x10FFFF)))
        has_cjk = all(bytes(font.getmask(char)) != missing for char in "双栏扫描精排")
    except (UnicodeError, ValueError):
        has_cjk = False
    if has_cjk:
        return "KPW6 · 300 PPI", "PDF 双栏 · 扫描精排"
    return "KPW6 - 300 PPI", "PDF - CLEAN LAYOUT"


def build_icons(out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    size = 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded squircle dark slate-emerald gradient look
    pad = 44
    draw.rounded_rectangle([pad, pad, size - pad, size - pad], radius=210, fill=(15, 23, 42, 255))
    draw.rounded_rectangle(
        [pad + 14, pad + 14, size - pad - 14, size - pad - 14],
        radius=196,
        fill=(17, 29, 53, 255),
        outline=(16, 185, 129, 210),
        width=12,
    )

    # Miniature Kindle frame inside
    kx0, ky0, kx1, ky1 = 230, 148, 794, 876
    draw.rounded_rectangle([kx0, ky0, kx1, ky1], radius=46, fill=(28, 36, 52, 255), outline=(148, 163, 184, 180), width=8)
    # E-ink screen inside
    sx0, sy0, sx1, sy1 = kx0 + 38, ky0 + 48, kx1 - 38, ky1 - 108
    draw.rounded_rectangle([sx0, sy0, sx1, sy1], radius=16, fill=(248, 250, 252, 255))

    # Crisp split typography bars on e-ink screen
    f_badge = _load_cjk_font(40)
    f_title = _load_cjk_font(33)
    bx0, by0, bx1, by1 = sx0 + 34, sy0 + 36, sx1 - 34, sy0 + 114
    draw.rounded_rectangle([bx0, by0, bx1, by1], radius=14, fill=(16, 185, 129, 255))
    badge_txt = icon_labels(f_badge)[0]
    bb = draw.textbbox((0, 0), badge_txt, font=f_badge)
    draw.text(
        ((bx0 + bx1 - (bb[2] - bb[0])) // 2, (by0 + by1 - (bb[3] - bb[1])) // 2 - bb[1]),
        badge_txt,
        fill=(255, 255, 255, 255),
        font=f_badge,
    )

    y_cur = sy0 + 150
    bar_widths = [0.88, 0.94, 0.74, 1.0, 0.68]
    bw_max = sx1 - sx0 - 68
    for idx, frac in enumerate(bar_widths):
        bar_col = (30, 41, 59, 255) if idx != 3 else (37, 99, 235, 220)
        bh = 24 if idx != 3 else 112
        if idx == 3:
            cx0, cy0, cx1, cy1 = sx0 + 34, y_cur, sx1 - 34, y_cur + bh
            draw.rounded_rectangle(
                [cx0, cy0, cx1, cy1],
                radius=14,
                fill=(219, 234, 254, 255),
                outline=(37, 99, 235, 230),
                width=5,
            )
            card_txt = icon_labels(f_title)[1]
            cb = draw.textbbox((0, 0), card_txt, font=f_title)
            draw.text(
                ((cx0 + cx1 - (cb[2] - cb[0])) // 2, (cy0 + cy1 - (cb[3] - cb[1])) // 2 - cb[1]),
                card_txt,
                fill=(30, 64, 175, 255),
                font=f_title,
            )
            y_cur += bh + 28
        else:
            draw.rounded_rectangle([sx0 + 34, y_cur, sx0 + 34 + int(bw_max * frac), y_cur + bh], radius=8, fill=bar_col)
            y_cur += bh + 24

    # Kindle bottom wordmark bar
    draw.rounded_rectangle([kx0 + 190, ky1 - 74, kx1 - 190, ky1 - 44], radius=12, fill=(71, 85, 105, 200))

    icon_png = os.path.join(out_dir, "icon_1024.png")
    img.save(icon_png, format="PNG")

    # Windows .ico
    ico_path = os.path.join(out_dir, "app_icon.ico")
    img.save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    # macOS .icns via iconutil (only on macOS)
    icns_path = ""
    if sys.platform == "darwin":
        iconset_dir = os.path.join(out_dir, "AppIcon.iconset")
        os.makedirs(iconset_dir, exist_ok=True)
        for px in (16, 32, 128, 256, 512):
            img.resize((px, px), Image.Resampling.LANCZOS).save(os.path.join(iconset_dir, f"icon_{px}x{px}.png"))
            img.resize((px * 2, px * 2), Image.Resampling.LANCZOS).save(os.path.join(iconset_dir, f"icon_{px}x{px}@2x.png"))

        icns_path = os.path.join(out_dir, "AppIcon.icns")
        subprocess.run(["iconutil", "-c", "icns", iconset_dir, "-o", icns_path], check=True)
    print("Generated icons:", icon_png, ico_path, icns_path)


if __name__ == "__main__":
    build_icons(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"))
