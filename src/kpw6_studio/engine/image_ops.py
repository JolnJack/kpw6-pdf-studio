"""Page rasterization, ink mask detection, deskewing, and Carta 1300 e-ink enhancement.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import io
import math
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter, ImageOps
import pymupdf as fitz

from kpw6_studio.engine.models import KPW6_PT_W, KPW6_PX_W, ProcessOptions

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
        curved = np.power(norm, 1.0 / max(0.65, gamma))
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
        # norm is luminance (0 black / 1 white): an exponent >1 darkens it.
        curved = np.power(norm, 1.0 / max(0.1, gamma))

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
