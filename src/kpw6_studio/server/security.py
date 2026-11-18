"""Loopback Host/Origin/CSRF request validation and strict option parsing.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import math
import secrets
import urllib.parse
from typing import Mapping

from flask import jsonify, request

from kpw6_studio.engine.models import ProcessOptions

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _coerce_bool(value, default: bool, name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1"):
            return True
        if low in ("false", "0"):
            return False
    raise ValueError(f"参数 {name} 必须是布尔值。")


def _coerce_int(value, default: int, name: str) -> int:
    if value is None:
        raise ValueError(f"参数 {name} 必须是整数。")
    if isinstance(value, bool):
        raise ValueError(f"参数 {name} 必须是整数。")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"参数 {name} 必须是整数。")
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数 {name} 必须是整数。") from exc


def parse_process_options(data: dict, custom_covers: Mapping[str, str]) -> ProcessOptions:
    if not isinstance(data, dict):
        raise ValueError("请求参数必须是 JSON 对象。")
    if data.get("mode", "academic_2col") not in ("academic_2col", "tech_book", "scanned_book", "comic_manga", "pure_text"):
        raise ValueError("请选择支持的文档处理模式。")
    numeric_ranges = {
        "gamma_strength": (0.1, 3.0), "margin_Percent": (0.0, 20.0),
        "reflow_font_size": (9.0, 24.0), "line_spacing": (1.0, 3.0),
        "sharpen_strength": (0.0, 3.0), "dpi_preset": (300.0, 600.0),
        "stroke_boldness": (0.0, 3.0), "split_volumes_every": (0.0, 10000.0),
    }
    for name, (lower, upper) in numeric_ranges.items():
        if name in data:
            if isinstance(data[name], bool):
                raise ValueError(f"参数 {name} 必须是有效数字。")
            try:
                value = float(data[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"参数 {name} 必须是有效数字。") from exc
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"参数 {name} 必须在 {lower:g}–{upper:g} 之间。")
            if name in {"dpi_preset", "stroke_boldness", "split_volumes_every"} and not value.is_integer():
                raise ValueError(f"参数 {name} 必须是整数。")
    if data.get("cover_mode", "smart_card") not in ("smart_card", "first_page", "custom_image", "none"):
        raise ValueError("请选择支持的封面模式。")
    cov_id = str(data.get("custom_cover_id", "") or "").lower()
    cov_path = custom_covers.get(cov_id) if cov_id else None
    raw_start = data.get("start_page", 1)
    try:
        start_page = max(1, int(raw_start)) if raw_start is not None else 1
    except Exception:
        start_page = 1

    raw_end = data.get("end_page", None)
    end_page = None
    if raw_end is not None and str(raw_end).strip() != "":
        try:
            end_page = max(1, int(raw_end))
        except Exception:
            end_page = None

    raw_overrides = data.get("page_overrides") or {}
    if not isinstance(raw_overrides, dict):
        raise ValueError("单页规则必须是 JSON 对象。")
    if any(value not in ("auto", "whole_portrait", "whole_landscape", "skip", None, "") for value in raw_overrides.values()):
        raise ValueError("单页规则包含不支持的选项。")
    clean_overrides = {str(k): str(v) for k, v in raw_overrides.items() if v and str(v) != "auto"}

    return ProcessOptions(
        mode=data.get("mode", "academic_2col"),
        split_pages=_coerce_bool(data.get("split_pages"), True, "split_pages"),
        landscape_2x=_coerce_bool(data.get("landscape_2x"), False, "landscape_2x"),
        trim_header_footer=_coerce_bool(data.get("trim_header_footer"), True, "trim_header_footer"),
        enhance_contrast=_coerce_bool(data.get("enhance_contrast"), False, "enhance_contrast"),
        clean_spine_shadow=_coerce_bool(data.get("clean_spine_shadow"), True, "clean_spine_shadow"),
        remove_watermark=_coerce_bool(data.get("remove_watermark"), False, "remove_watermark"),
        auto_deskew=_coerce_bool(data.get("auto_deskew"), False, "auto_deskew"),
        gamma_strength=float(data.get("gamma_strength", 0.55)),
        margin_Percent=float(data.get("margin_Percent", 2.5)),
        comic_split_spread=_coerce_bool(data.get("comic_split_spread"), True, "comic_split_spread"),
        manga_rtl=_coerce_bool(data.get("manga_rtl"), True, "manga_rtl"),
        comic_full_bleed=_coerce_bool(data.get("comic_full_bleed"), True, "comic_full_bleed"),
        reflow_font_size=float(data.get("reflow_font_size", 13.5)),
        line_spacing=float(data.get("line_spacing", 1.65)),
        dpi_preset=int(data.get("dpi_preset", 450)),
        vector_lossless=_coerce_bool(data.get("vector_lossless"), True, "vector_lossless"),
        stroke_boldness=int(data.get("stroke_boldness", 0)),
        sharpen_strength=float(data.get("sharpen_strength", 1.60)),
        cover_mode=str(data.get("cover_mode", "smart_card")),
        custom_cover_title=str(data.get("custom_cover_title", "")),
        custom_cover_author=str(data.get("custom_cover_author", "")),
        custom_cover_path=cov_path,
        start_page=start_page,
        end_page=end_page,
        split_volumes_every=int(data.get("split_volumes_every", 0) or 0),
        page_overrides=clean_overrides,
    )


def enforce_local_request_security(csrf_token: str):
    req_host = urllib.parse.urlsplit("//" + (request.host or ""))
    if (req_host.hostname or "").lower() not in LOOPBACK_HOSTS:
        return jsonify({"error": "拒绝非本机回环 Host 访问。"}), 403

    if request.path.startswith("/api/"):
        sec_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sec_site and sec_site not in {"same-origin", "none"}:
            return jsonify({"error": "拒绝跨站请求访问本地服务。"}), 403

        origin = (request.headers.get("Origin") or "").strip()
        if origin:
            if origin.lower() == "null":
                return jsonify({"error": "拒绝匿名源请求访问本地服务。"}), 403
            orig = urllib.parse.urlsplit(origin)
            if (orig.hostname or "").lower() not in LOOPBACK_HOSTS or (
                req_host.port is not None and orig.port != req_host.port
            ):
                return jsonify({"error": "拒绝跨源请求访问本地服务。"}), 403
        else:
            referer = (request.headers.get("Referer") or "").strip()
            if referer:
                ref = urllib.parse.urlsplit(referer)
                if (ref.hostname or "").lower() not in LOOPBACK_HOSTS or (
                    req_host.port is not None and ref.port != req_host.port
                ):
                    return jsonify({"error": "拒绝跨源来源访问本地服务。"}), 403

        if request.method in {"POST", "DELETE"} and (origin or sec_site):
            token = request.headers.get("X-KPW6-Token") or ""
            if not secrets.compare_digest(token, csrf_token):
                return jsonify({"error": "本地会话安全令牌无效或已过期，请刷新页面后重试。"}), 403

    if request.is_json and not isinstance(request.get_json(), dict):
        return jsonify({"error": "请求参数必须是 JSON 对象。"}), 400
    return None
