"""Flask application, file/export state management, and local server CLI runner.

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import argparse
import atexit
from collections import OrderedDict
from dataclasses import replace
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser

from flask import Flask, Response, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from kpw6_studio.engine.models import ProcessOptions, get_target_page_list
from kpw6_studio.server.http_server import LoopbackWSGIServer
from kpw6_studio.server.security import (
    _coerce_bool,
    _coerce_int,
    enforce_local_request_security,
    parse_process_options,
)
from kpw6_studio.worker.conversion import cancel_all_conversions
from kpw6_studio.worker.service import (
    EXPORT_PDF_SERVICE,
    ExportCancelledError,
    PDF_SERVICE,
    worker_function,
)

# Interactive preview/inspection uses PDF_SERVICE; heavy exports use EXPORT_PDF_SERVICE
# so long exports or Calibre runs do not freeze page previews.
generate_demo_pdfs = worker_function("generate_demo_pdfs", PDF_SERVICE)
build_kpw6_pdf = worker_function("build_kpw6_pdf", EXPORT_PDF_SERVICE)
build_kpw6_epub = worker_function("build_kpw6_epub", EXPORT_PDF_SERVICE)
build_kpw6_cbz = worker_function("build_kpw6_cbz", EXPORT_PDF_SERVICE)
convert_epub_via_calibre = worker_function("convert_epub_via_calibre", EXPORT_PDF_SERVICE)
render_preview_comparison = worker_function("render_preview_comparison", PDF_SERVICE)
render_hd_zoom_image = worker_function("render_hd_zoom_image", PDF_SERVICE)
detect_pdf_category = worker_function("detect_pdf_category", PDF_SERVICE)
scan_document_highlights = worker_function("scan_document_highlights", PDF_SERVICE)


def _resolve_web_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "kpw6_studio" / "web"
    return Path(__file__).resolve().parents[1] / "web"


def _resolve_data_root() -> str:
    env_dir = (os.environ.get("KPW6_DATA_DIR") or "").strip()
    if env_dir:
        return os.path.abspath(env_dir)
    if getattr(sys, "frozen", False):
        docs_dir = os.path.join(os.path.expanduser("~"), "Documents", "KPW6_PDF_Studio")
        try:
            os.makedirs(docs_dir, exist_ok=True)
            return docs_dir
        except Exception:
            fallback_dir = os.path.join(os.path.expanduser("~"), ".kpw6_pdf_studio")
            os.makedirs(fallback_dir, exist_ok=True)
            return fallback_dir
    return str(Path(__file__).resolve().parents[3])


WEB_DIR = _resolve_web_dir()
BASE_DIR = _resolve_data_root()
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
SAMPLE_DIR = os.path.join(BASE_DIR, "samples")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(SAMPLE_DIR, exist_ok=True)

DEMO_PATHS = generate_demo_pdfs(SAMPLE_DIR)

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300 MB max PDF

FILES = {}
HIGHLIGHTS_CACHE = {}
CUSTOM_COVERS = {}
EXPORT_JOBS = OrderedDict()
MAX_EXPORT_JOBS = 64
APP_CSRF_TOKEN = secrets.token_urlsafe(24)
_UPLOAD_PDF_RE = re.compile(r"^([0-9a-f]{8})_(.+\.pdf)$", re.IGNORECASE)
_UPLOAD_COVER_RE = re.compile(r"^cover_([0-9a-f]{8})_(.+)$", re.IGNORECASE)

for _key, _path in DEMO_PATHS.items():
    FILES[f"demo_{_key}"] = {
        "path": _path,
        "name": os.path.basename(_path),
        "is_demo": True,
        "mtime": 0.0,
    }


def _validate_pdf(pdf_path: str):
    return PDF_SERVICE.call("validate_pdf", pdf_path)


def _scan_uploads_dir():
    """Register any PDFs and custom covers in UPLOAD_DIR so restarts keep user files accessible."""
    if not os.path.isdir(UPLOAD_DIR):
        return
    for fname in sorted(os.listdir(UPLOAD_DIR)):
        full_p = os.path.join(UPLOAD_DIR, fname)
        if not os.path.isfile(full_p):
            continue
        m_cover = _UPLOAD_COVER_RE.match(fname)
        if m_cover:
            cid = m_cover.group(1).lower()
            if cid not in CUSTOM_COVERS:
                CUSTOM_COVERS[cid] = full_p
            continue
        m_pdf = _UPLOAD_PDF_RE.match(fname)
        if m_pdf:
            fid, orig_name = m_pdf.group(1).lower(), m_pdf.group(2)
            if fid not in FILES:
                try:
                    _validate_pdf(full_p)
                except Exception:
                    continue
            FILES[fid] = {
                "path": full_p,
                "name": orig_name,
                "is_demo": False,
                "mtime": os.path.getmtime(full_p),
            }


_scan_uploads_dir()


def _set_export_job(job_id: str, info: dict) -> None:
    EXPORT_JOBS[job_id] = info
    EXPORT_JOBS.move_to_end(job_id)
    if len(EXPORT_JOBS) > MAX_EXPORT_JOBS:
        for old_id in list(EXPORT_JOBS.keys()):
            if len(EXPORT_JOBS) <= MAX_EXPORT_JOBS:
                break
            if EXPORT_JOBS[old_id].get("status") != "running":
                EXPORT_JOBS.pop(old_id, None)


def _parse_options(data: dict) -> ProcessOptions:
    return parse_process_options(data, CUSTOM_COVERS)


@app.before_request
def _validate_local_and_json_request():
    return enforce_local_request_security(APP_CSRF_TOKEN)

@app.errorhandler(ValueError)
def _option_error(error):
    return jsonify({"error": str(error)}), 400


@app.errorhandler(RuntimeError)
def _processing_error(error):
    return jsonify({"error": str(error)}), 500


@app.errorhandler(HTTPException)
def _http_error(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": error.description}), error.code
    return error


def _get_uploaded_list():
    _scan_uploads_dir()
    uploaded = [
        {"file_id": fid, "name": info["name"], "mtime": info.get("mtime", 0.0)}
        for fid, info in FILES.items()
        if not info.get("is_demo", False)
    ]
    uploaded.sort(key=lambda x: x["mtime"], reverse=True)
    return uploaded


def _get_highlights(fid: str, pdf_path: str):
    if fid not in HIGHLIGHTS_CACHE:
        HIGHLIGHTS_CACHE[fid] = scan_document_highlights(pdf_path)
    return HIGHLIGHTS_CACHE[fid]


@app.route("/")
def index():
    html_doc = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return Response(
        html_doc.replace("__KPW6_CSRF_TOKEN_VALUE__", APP_CSRF_TOKEN),
        mimetype="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/api/files", methods=["GET"])
def api_files():
    return jsonify({"uploaded": _get_uploaded_list()})


@app.route("/api/delete_file", methods=["POST"])
def api_delete_file():
    _scan_uploads_dir()
    data = request.json or {}
    fid = str(data.get("file_id", "") or "").strip()
    if fid not in FILES:
        return jsonify({"error": "File not found"}), 404
    info = FILES[fid]
    if info.get("is_demo", False):
        return jsonify({"error": "内置演示样张不可删除。"}), 400
    up_real = os.path.realpath(UPLOAD_DIR)
    target = os.path.realpath(info["path"])
    try:
        inside = os.path.commonpath([up_real, target]) == up_real
    except ValueError:
        inside = False
    if inside and os.path.isfile(target):
        try:
            os.remove(target)
        except OSError:
            pass
    FILES.pop(fid, None)
    HIGHLIGHTS_CACHE.pop(fid, None)
    return jsonify({"ok": True, "uploaded": _get_uploaded_list()})


@app.route("/api/doc_info", methods=["GET"])
def api_doc_info():
    _scan_uploads_dir()
    fid = request.args.get("file_id", "demo_academic_2col")
    if fid not in FILES:
        return jsonify({"error": "File not found"}), 404
    pdf_path = FILES[fid]["path"]
    detected = detect_pdf_category(pdf_path)
    highlights = _get_highlights(fid, pdf_path)
    return jsonify({
        "file_id": fid,
        "filename": FILES[fid]["name"],
        "detected": detected,
        "highlights": highlights,
    })


def _clean_upload_filename(raw_name: str, fallback: str) -> str:
    base = os.path.basename((raw_name or "").strip()) or fallback
    cleaned = re.sub(r'[\\/:*?"<>|%#&\x00]+', "_", base).strip()
    return cleaned or fallback


def _safe_filename_stem(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    cleaned = re.sub(r'[\\/:*?"<>|%#&\x00]+', "_", stem).strip()
    return cleaned or "document"


def _dl_url(filename: str) -> str:
    return "/api/download/" + urllib.parse.quote(filename, safe="")


def _view_url(filename: str) -> str:
    return "/api/view/" + urllib.parse.quote(filename, safe="")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    fid = str(uuid.uuid4())[:8]
    safe_name = _clean_upload_filename(f.filename, "document.pdf")
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"
    save_path = os.path.join(UPLOAD_DIR, f"{fid}_{safe_name}")
    temp_path = os.path.join(UPLOAD_DIR, f".upload_{uuid.uuid4().hex}.part")
    try:
        f.save(temp_path)
        _validate_pdf(temp_path)
        detected = detect_pdf_category(temp_path)
        highlights = scan_document_highlights(temp_path)
        # Publish only a successfully validated file; directory scans ignore .part files.
        os.replace(temp_path, save_path)
    except Exception:
        return jsonify({"error": "无法读取 PDF，请确认文件完整、未加密且至少包含一页。"}), 400
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    FILES[fid] = {
        "path": save_path,
        "name": safe_name,
        "is_demo": False,
        "mtime": os.path.getmtime(save_path),
    }
    HIGHLIGHTS_CACHE[fid] = highlights
    return jsonify({
        "file_id": fid,
        "filename": safe_name,
        "detected": detected,
        "highlights": highlights,
        "uploaded": _get_uploaded_list(),
    })


@app.route("/api/preview", methods=["POST"])
def api_preview():
    _scan_uploads_dir()
    data = request.json or {}
    fid = data.get("file_id", "demo_academic_2col")
    if fid not in FILES:
        return jsonify({"error": "File not found"}), 404
    opts = _parse_options(data)
    pno = _coerce_int(data["page_num"], 0, "page_num") if "page_num" in data else 0
    res = render_preview_comparison(FILES[fid]["path"], pno, opts)
    return jsonify(res)


@app.route("/api/preview_hd", methods=["POST"])
def api_preview_hd():
    _scan_uploads_dir()
    data = request.json or {}
    fid = data.get("file_id", "demo_academic_2col")
    if fid not in FILES:
        return jsonify({"error": "File not found"}), 404
    opts = _parse_options(data)
    pno = _coerce_int(data["page_num"], 0, "page_num") if "page_num" in data else 0
    target_type = str(data.get("target_type", "kpw6"))
    if target_type not in ("kpw6", "source"):
        raise ValueError("不支持的预览目标类型。")
    screen_idx = _coerce_int(data["screen_idx"], 0, "screen_idx") if "screen_idx" in data else 0
    res = render_hd_zoom_image(FILES[fid]["path"], pno, opts, target_type=target_type, screen_idx=screen_idx)
    return jsonify(res)


@app.route("/api/export", methods=["POST"])
def api_export():
    _scan_uploads_dir()
    data = request.json or {}
    fid = data.get("file_id", "demo_academic_2col")
    if fid not in FILES:
        return jsonify({"error": "File not found"}), 404
    opts = _parse_options(data)
    src_info = FILES[fid]
    base_name = _safe_filename_stem(src_info["name"])
    mode_tag = {
        "academic_2col": "双栏重排",
        "tech_book": "专业书精排",
        "scanned_book": "去网格高清",
        "comic_manga": "漫画满屏",
        "pure_text": "纯文字大字重排",
    }.get(opts.mode, "KPW6")
    out_filename = f"{base_name}_KPW6_{mode_tag}.pdf"
    export_id, export_dir = _new_export_directory()
    out_path = os.path.join(export_dir, out_filename)

    try:
        stats = build_kpw6_pdf(src_info["path"], out_path, opts)
    except ExportCancelledError:
        return jsonify({"cancelled": True, "message": "已取消导出。"}), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        **stats,
        "output_filename": out_filename,
        "download_url": _dl_url(f"{export_id}/{out_filename}"),
        "view_url": _view_url(f"{export_id}/{out_filename}"),
    })


@app.route("/api/upload_cover", methods=["POST"])
def api_upload_cover():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    cid = str(uuid.uuid4())[:8]
    safe_name = _clean_upload_filename(f.filename, "cover.jpg")
    save_path = os.path.join(UPLOAD_DIR, f"cover_{cid}_{safe_name}")
    temp_path = os.path.join(UPLOAD_DIR, f".cover_{uuid.uuid4().hex}.part")
    try:
        f.save(temp_path)
        PDF_SERVICE.call("validate_cover", temp_path)
        os.replace(temp_path, save_path)
    except Exception:
        return jsonify({"error": "无法读取封面图片，请确认图片完整且尺寸未超过限制。"}), 400
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    CUSTOM_COVERS[cid] = save_path
    return jsonify({"cover_id": cid, "filename": safe_name})


@app.route("/api/export_progress", methods=["GET"])
def api_export_progress():
    job_id = request.args.get("job_id", "")
    info = EXPORT_JOBS.get(job_id)
    if not info:
        return jsonify({"status": "starting", "percent": 1, "stage_text": "正在初始化排版引擎...", "elapsed_sec": 0.0, "eta_sec": None})
    return jsonify(info)


@app.route("/api/cancel_export", methods=["POST"])
def api_cancel_export():
    data = request.json or {}
    job_id = str(data.get("job_id", "") or "").strip()
    cancelled = EXPORT_PDF_SERVICE.cancel_current()
    if job_id and job_id in EXPORT_JOBS:
        prev = dict(EXPORT_JOBS[job_id])
        prev.update({"status": "cancelled", "stage_text": "⏹ 导出已取消", "eta_sec": 0.0})
        _set_export_job(job_id, prev)
    return jsonify({"ok": True, "cancelled": cancelled})


def _scaled_progress_cb(progress_cb, lo: int, hi: int):
    """Map an inner (cur, tot, msg) progress callback into [lo, hi] out of 100."""
    if not progress_cb:
        return None
    span = max(1, hi - lo)

    def _sub(cur: int, tot: int, msg: str):
        frac = min(1.0, max(0.0, cur / max(1, tot)))
        progress_cb(lo + int(round(frac * span)), 100, msg)

    return _sub


def _new_export_directory() -> tuple[str, str]:
    export_id = uuid.uuid4().hex
    export_dir = os.path.join(OUTPUT_DIR, export_id)
    os.makedirs(export_dir, exist_ok=False)
    return export_id, export_dir


def _export_single_volume(
    pdf_path: str,
    base_name: str,
    mode_tag: str,
    export_fmt: str,
    vol_opts: ProcessOptions,
    vol_suffix: str,
    progress_cb,
) -> dict:
    """Build one volume (or the whole book) in the requested format and return download metadata."""
    export_id, export_dir = _new_export_directory()
    cover_tag = "带封面" if vol_opts.cover_mode != "none" else ""
    cover_detail = "含封面与目录" if cover_tag else "含目录，不添加独立封面"
    if export_fmt == "pdf":
        out_filename = f"{base_name}_KPW6_{mode_tag}{vol_suffix}.pdf"
        out_path = os.path.join(export_dir, out_filename)
        stats = build_kpw6_pdf(pdf_path, out_path, vol_opts, progress_cb=progress_cb)
        return {
            **stats,
            "output_filename": out_filename,
            "download_url": _dl_url(f"{export_id}/{out_filename}"),
            "download_label": f"💾 下载{vol_suffix or ''} .PDF ({stats['output_size_mb']} MB)",
            "view_url": _view_url(f"{export_id}/{out_filename}"),
            "message": f"✅ 已生成 PDF：{out_filename} （选中 {stats['input_pages']} 页 ➔ KPW6 精排 {stats['output_pages']} 屏，体积 {stats['output_size_mb']} MB）",
        }

    if export_fmt in ("epub_layout", "epub_reflow"):
        is_reflow = (export_fmt == "epub_reflow")
        sub_tag = f"流式可调字号{cover_tag}" if is_reflow else f"{mode_tag}{cover_tag}"
        out_filename = f"{base_name}_KPW6_{sub_tag}{vol_suffix}.epub"
        out_path = os.path.join(export_dir, out_filename)
        stats = build_kpw6_epub(pdf_path, out_path, vol_opts, force_reflow=is_reflow, progress_cb=progress_cb)
        detail = (
            f"识别 {stats['chapters_count']} 章 / {stats['paragraphs_count']} 段"
            if is_reflow
            else f"{stats['screens_count']} 屏内页 + {stats['chapters_count']} 条目录" + ("，含独立封面" if cover_tag else "，不添加独立封面")
        )
        return {
            **stats,
            "output_filename": out_filename,
            "download_url": _dl_url(f"{export_id}/{out_filename}"),
            "download_label": f"💾 下载{vol_suffix or cover_tag} .EPUB ({stats['output_size_mb']} MB)",
            "message": f"✅ 已生成 EPUB：{out_filename} （{detail}，体积 {stats['output_size_mb']} MB · 设备与云传效果请在实际 Kindle 上确认）",
        }

    if export_fmt in ("azw3", "mobi"):
        ext = export_fmt
        temp_epub = os.path.join(export_dir, f"_tmp_{base_name}{vol_suffix}_KPW6.epub")
        is_reflow = (vol_opts.mode == "pure_text")
        try:
            epub_stats = build_kpw6_epub(
                pdf_path,
                temp_epub,
                vol_opts,
                force_reflow=is_reflow,
                progress_cb=_scaled_progress_cb(progress_cb, 0, 85),
            )
            if progress_cb:
                progress_cb(86, 100, f"正在调用 Calibre 引擎编译 .{ext.upper()} ({cover_detail})...")
            out_filename = f"{base_name}_KPW6_{mode_tag}{cover_tag}{vol_suffix}.{ext}"
            out_path = os.path.join(export_dir, out_filename)
            conv_stats = convert_epub_via_calibre(
                temp_epub,
                out_path,
                book_title=epub_stats.get("book_title", base_name),
                book_author=epub_stats.get("book_author", "KPW6 PDF Studio"),
            )
        finally:
            try:
                if os.path.exists(temp_epub):
                    os.remove(temp_epub)
            except Exception:
                pass
        return {
            **epub_stats,
            **conv_stats,
            "output_filename": out_filename,
            "download_url": _dl_url(f"{export_id}/{out_filename}"),
            "download_label": f"💾 下载{vol_suffix or ''} .{ext.upper()} ({conv_stats['output_size_mb']} MB)",
            "message": f"✅ 已生成 .{ext.upper()}：{out_filename} （{cover_detail}，体积 {conv_stats['output_size_mb']} MB）",
        }

    if export_fmt == "cbz":
        out_filename = f"{base_name}_KPW6_{mode_tag}高清图集{vol_suffix}.cbz"
        out_path = os.path.join(export_dir, out_filename)
        stats = build_kpw6_cbz(pdf_path, out_path, vol_opts, progress_cb=progress_cb)
        return {
            **stats,
            "output_filename": out_filename,
            "download_url": _dl_url(f"{export_id}/{out_filename}"),
            "download_label": f"💾 下载{vol_suffix or ''} .CBZ ({stats['output_size_mb']} MB)",
            "message": f"✅ 已生成 PNG 图集包：{out_filename} （{stats['screens_count']} 屏{' + 独立封面' if cover_tag else ''}，体积 {stats['output_size_mb']} MB）",
        }

    if export_fmt == "bundle_both":
        pdf_filename = f"{base_name}_KPW6_{mode_tag}{vol_suffix}.pdf"
        pdf_path_out = os.path.join(export_dir, pdf_filename)
        pdf_stats = build_kpw6_pdf(
            pdf_path,
            pdf_path_out,
            vol_opts,
            progress_cb=_scaled_progress_cb(progress_cb, 0, 35),
        )

        epub_filename = f"{base_name}_KPW6_{mode_tag}{cover_tag}{vol_suffix}.epub"
        epub_path_out = os.path.join(export_dir, epub_filename)
        epub_stats = build_kpw6_epub(
            pdf_path,
            epub_path_out,
            vol_opts,
            force_reflow=(vol_opts.mode == "pure_text"),
            progress_cb=_scaled_progress_cb(progress_cb, 35, 99),
        )
        return {
            "output_filename": f"{pdf_filename} & {epub_filename}",
            "download_url": _dl_url(f"{export_id}/{epub_filename}"),
            "download_label": f"💾 下载{vol_suffix or cover_tag} .EPUB ({epub_stats['output_size_mb']} MB)",
            "second_download_url": _dl_url(f"{export_id}/{pdf_filename}"),
            "second_download_label": f"💾 下载{vol_suffix or '矢量'} .PDF ({pdf_stats['output_size_mb']} MB)",
            "view_url": _view_url(f"{export_id}/{pdf_filename}"),
            "output_size_mb": round(epub_stats["output_size_mb"] + pdf_stats["output_size_mb"], 2),
            "message": f"✅ 双格式生成完毕：EPUB ({epub_stats['output_size_mb']} MB) + PDF ({pdf_stats['output_size_mb']} MB，按设置优先保留矢量)",
        }

    raise ValueError(f"未知导出格式: {export_fmt}")


@app.route("/api/export_unified", methods=["POST"])
def api_export_unified():
    _scan_uploads_dir()
    data = request.json or {}
    fid = data.get("file_id", "demo_academic_2col")
    if fid not in FILES:
        return jsonify({"error": "File not found"}), 404

    opts = _parse_options(data)
    export_fmt = str(data.get("export_format", "epub_layout"))
    job_id = str(data.get("job_id", "") or f"job_{uuid.uuid4().hex[:8]}")
    src_info = FILES[fid]
    pdf_path = src_info["path"]
    base_name = _safe_filename_stem(src_info["name"])
    mode_tag = {
        "academic_2col": "双栏重排",
        "tech_book": "专业书精排",
        "scanned_book": "去网格高清",
        "comic_manga": "漫画满屏",
        "pure_text": "纯文字重排",
    }.get(opts.mode, "KPW6")

    t0 = time.time()
    _set_export_job(job_id, {
        "status": "running",
        "percent": 2,
        "current": 0,
        "total": 1,
        "stage_text": "正在扫描页码范围与单页规则...",
        "elapsed_sec": 0.0,
        "eta_sec": None,
    })

    try:
        document_info = EXPORT_PDF_SERVICE.call("inspect_pdf", pdf_path, opts)
        total_doc_pages = document_info["total_pages"]
        detected_title = document_info["title"]

        target_pages = get_target_page_list(total_doc_pages, opts)
        if not target_pages:
            raise ValueError("当前所选页码范围内的所有页面均已标记为【跳过此页】，没有可导出的内容。")
        total_selected = len(target_pages)

        # Check if we need to split into multiple volumes
        chunk_size = opts.split_volumes_every if (opts.split_volumes_every and opts.split_volumes_every > 0) else 0
        if chunk_size > 0 and len(target_pages) > chunk_size:
            chunks = [target_pages[i : i + chunk_size] for i in range(0, len(target_pages), chunk_size)]
        else:
            chunks = [target_pages]

        num_vols = len(chunks)
        volumes_results = []
        pages_completed_prior = 0

        for vol_idx, page_chunk in enumerate(chunks, start=1):
            p_first = page_chunk[0] + 1
            p_last = page_chunk[-1] + 1
            if num_vols > 1:
                vol_suffix = f"_第{vol_idx}卷_P{p_first}-{p_last}"
                base_title_prefix = opts.custom_cover_title.strip() or detected_title or base_name
                vol_title = f"{base_title_prefix} (第{vol_idx}/{num_vols}卷 · P{p_first}-{p_last})"
                vol_opts = replace(opts, explicit_pages=page_chunk, custom_cover_title=vol_title)
            else:
                range_suffix = ""
                if opts.start_page > 1 or (opts.end_page is not None and opts.end_page < total_doc_pages):
                    range_suffix = f"_P{p_first}-{p_last}"
                vol_suffix = range_suffix
                vol_opts = replace(opts, explicit_pages=page_chunk)

            def _make_progress_cb(v_idx: int, prior_done: int, chunk_len: int):
                def _cb(cur_step: int, tot_step: int, msg: str):
                    elapsed = time.time() - t0
                    frac_in_chunk = min(1.0, max(0.0, cur_step / max(1, tot_step)))
                    overall_done = prior_done + frac_in_chunk * chunk_len
                    overall_frac = min(0.99, max(0.02, overall_done / max(1, total_selected)))
                    eta = (elapsed / overall_frac) * (1.0 - overall_frac) if overall_frac > 0.04 else None
                    prefix = f"【第 {v_idx}/{num_vols} 卷】" if num_vols > 1 else ""
                    _set_export_job(job_id, {
                        "status": "running",
                        "percent": int(round(overall_frac * 100)),
                        "current": int(round(overall_done)),
                        "total": total_selected,
                        "stage_text": f"{prefix}{msg}",
                        "elapsed_sec": round(elapsed, 1),
                        "eta_sec": round(eta, 1) if eta is not None else None,
                    })
                return _cb

            res_vol = _export_single_volume(
                pdf_path=pdf_path,
                base_name=base_name,
                mode_tag=mode_tag,
                export_fmt=export_fmt,
                vol_opts=vol_opts,
                vol_suffix=vol_suffix,
                progress_cb=_make_progress_cb(vol_idx, pages_completed_prior, len(page_chunk)),
            )
            volumes_results.append(res_vol)
            pages_completed_prior += len(page_chunk)

        elapsed_total = round(time.time() - t0, 1)
        _set_export_job(job_id, {
            "status": "done",
            "percent": 100,
            "current": total_selected,
            "total": total_selected,
            "stage_text": f"✅ 导出完成（共耗时 {elapsed_total}s）",
            "elapsed_sec": elapsed_total,
            "eta_sec": 0.0,
        })

        if num_vols == 1:
            single_vol = dict(volumes_results[0])
            return jsonify({
                **single_vol,
                "volumes": [single_vol],
                "elapsed_sec": elapsed_total,
            })

        total_mb = round(sum(v.get("output_size_mb", 0.0) for v in volumes_results), 2)
        first_vol = volumes_results[0]
        return jsonify({
            **first_vol,
            "volumes": volumes_results,
            "elapsed_sec": elapsed_total,
            "message": (
                f"✅ 大书自动分卷完成！共导出 {num_vols} 卷（每卷 {chunk_size} 页，合计 {total_selected} 页，"
                f"总计 {total_mb} MB，耗时 {elapsed_total}s）"
            ),
        })
    except ExportCancelledError:
        _set_export_job(job_id, {
            "status": "cancelled",
            "percent": 100,
            "stage_text": "⏹ 导出已取消",
            "elapsed_sec": round(time.time() - t0, 1),
            "eta_sec": 0.0,
        })
        return jsonify({"cancelled": True, "message": "已取消导出。"}), 200
    except ValueError as ve:
        _set_export_job(job_id, {
            "status": "error",
            "percent": 100,
            "stage_text": f"❌ 无法导出: {ve}",
            "elapsed_sec": round(time.time() - t0, 1),
            "eta_sec": 0.0,
        })
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        _set_export_job(job_id, {
            "status": "error",
            "percent": 100,
            "stage_text": f"❌ 导出失败: {e}",
            "elapsed_sec": round(time.time() - t0, 1),
            "eta_sec": 0.0,
        })
        return jsonify({"error": str(e)}), 500


def _resolve_safe_output_file(filename: str) -> str | None:
    out_real = os.path.realpath(OUTPUT_DIR)
    target = os.path.realpath(os.path.join(OUTPUT_DIR, filename))
    try:
        inside = os.path.commonpath([out_real, target]) == out_real
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(target):
        return None
    return target


@app.route("/api/download/<path:filename>")
def api_download(filename: str):
    target = _resolve_safe_output_file(filename)
    if not target:
        return jsonify({"error": "File not found"}), 404
    return send_file(target, as_attachment=True, download_name=os.path.basename(target))


@app.route("/api/view/<path:filename>")
def api_view_inline(filename: str):
    """Serve exported PDF inline so the browser can open it in its native full-screen PDF reader."""
    target = _resolve_safe_output_file(filename)
    if not target:
        return jsonify({"error": "File not found"}), 404
    return send_file(target, as_attachment=False, mimetype="application/pdf")


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "data_dir": BASE_DIR, "output_dir": OUTPUT_DIR})


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", OUTPUT_DIR])
        elif sys.platform.startswith("win"):
            os.startfile(OUTPUT_DIR)
        else:
            subprocess.Popen(["xdg-open", OUTPUT_DIR])
    except Exception:
        pass
    return jsonify({"ok": True})


def _find_available_port(preferred: int = 8765) -> int:
    if not 0 <= preferred <= 65535:
        raise ValueError("端口必须在0–65535之间。")
    if preferred == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    for candidate in [preferred, *range(8766, 8820)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as chk:
            if chk.connect_ex(("127.0.0.1", candidate)) == 0:
                continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _open_browser_when_ready(url: str, port: int):
    deadline = time.time() + 12.0
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
                return
        time.sleep(0.2)


def _shutdown_owned_processes(*_):
    PDF_SERVICE.close()
    EXPORT_PDF_SERVICE.close()
    cancel_all_conversions()


atexit.register(_shutdown_owned_processes)



def run_cli(argv: list[str] | None = None) -> None:
    def _exit_signal(signum, frame):
        _shutdown_owned_processes()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _exit_signal)
    signal.signal(signal.SIGINT, _exit_signal)
    parser = argparse.ArgumentParser(description="KPW6 PDF Studio")
    parser.add_argument("--port", type=int, default=int(os.environ.get("KPW6_PORT", "8765")))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port-file", default=os.environ.get("KPW6_PORT_FILE", ""))
    args, _ = parser.parse_known_args(argv)

    bound_port = 0 if args.port == 0 else _find_available_port(args.port)
    server = LoopbackWSGIServer("127.0.0.1", bound_port, app)
    bound_port = server.server_port
    url = f"http://127.0.0.1:{bound_port}"
    if args.port_file:
        try:
            with open(args.port_file, "w", encoding="utf-8") as pf:
                pf.write(str(bound_port))
        except Exception:
            pass

    no_browser = args.no_browser or (os.environ.get("KPW6_NO_BROWSER") == "1")
    if not no_browser:
        threading.Thread(target=_open_browser_when_ready, args=(url, bound_port), daemon=True).start()

    print(f"🚀 KPW6 PDF Studio 已启动: {url} (导出目录: {OUTPUT_DIR})", flush=True)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    run_cli()
