import argparse
import atexit
from collections import OrderedDict
import math
import os
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
from dataclasses import replace

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")

if __name__ == "__main__" and "--pdf-worker" in sys.argv:
    from pdf_worker import run_worker
    run_worker()
    raise SystemExit(0)

from flask import Flask, Response, request, jsonify, send_file
from werkzeug.exceptions import HTTPException

from pdf_engine import (
    ProcessOptions,
    get_target_page_list,
)
from pdf_service import EXPORT_PDF_SERVICE, ExportCancelledError, PDF_SERVICE, worker_function
from conversion_runner import cancel_all_conversions
from local_server import LoopbackWSGIServer

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
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _resolve_data_root()
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
SAMPLE_DIR = os.path.join(BASE_DIR, "samples")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(SAMPLE_DIR, exist_ok=True)

DEMO_PATHS = generate_demo_pdfs(SAMPLE_DIR)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300 MB max PDF

FILES = {}
HIGHLIGHTS_CACHE = {}
CUSTOM_COVERS = {}
EXPORT_JOBS = OrderedDict()
MAX_EXPORT_JOBS = 64
APP_CSRF_TOKEN = secrets.token_urlsafe(24)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_UPLOAD_PDF_RE = re.compile(r"^([0-9a-f]{8})_(.+\.pdf)$", re.IGNORECASE)
_UPLOAD_COVER_RE = re.compile(r"^cover_([0-9a-f]{8})_(.+)$", re.IGNORECASE)

for key, path in DEMO_PATHS.items():
    FILES[f"demo_{key}"] = {
        "path": path,
        "name": os.path.basename(path),
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
                    # Ignore damaged files left by older versions without deleting them.
                    continue
            FILES[fid] = {
                "path": full_p,
                "name": orig_name,
                "is_demo": False,
                "mtime": os.path.getmtime(full_p),
            }


_scan_uploads_dir()

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KPW6 签名版 全能 PDF / 漫画 / 纯文字 EPUB 工作台 (免越狱五合一)</title>
<style>
  :root {
    --bg: #0f172a;
    --panel: #1e293b;
    --panel-alt: #334155;
    --text: #f8fafc;
    --muted: #94a3b8;
    --primary: #3b82f6;
    --primary-hover: #2563eb;
    --emerald: #10b981;
    --purple: #8b5cf6;
    --amber: #f59e0b;
    --border: #334155;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    background: rgba(30, 41, 59, 0.94);
    border-bottom: 1px solid var(--border);
    padding: 11px 22px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    backdrop-filter: blur(8px);
    position: sticky;
    top: 0;
    z-index: 30;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand-badge {
    background: linear-gradient(135deg, #3b82f6, #10b981);
    color: #fff;
    font-weight: 700;
    font-size: 12px;
    padding: 4px 10px;
    border-radius: 999px;
  }
  .brand h1 { font-size: 16.5px; font-weight: 600; }
  .brand p { font-size: 11.5px; color: var(--muted); }
  .header-actions { display: flex; gap: 9px; flex-wrap: wrap; }
  .btn {
    border: 1px solid var(--border);
    background: var(--panel-alt);
    color: var(--text);
    padding: 7px 13px;
    border-radius: 8px;
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    text-decoration: none;
    user-select: none;
  }
  .btn:hover { background: #475569; }
  .btn-sm { padding: 5px 10px; font-size: 12px; border-radius: 6px; }
  .btn-primary { background: var(--primary); border-color: var(--primary); color: #fff; font-weight: 600; }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-emerald { background: var(--emerald); border-color: var(--emerald); color: #fff; font-weight: 600; }
  .btn-emerald:hover { background: #059669; }
  .btn-purple { background: var(--purple); border-color: var(--purple); color: #fff; font-weight: 600; }
  .btn-purple:hover { background: #7c3aed; }

  .workspace {
    display: grid;
    grid-template-columns: 360px 1fr;
    flex: 1;
    min-height: calc(100vh - 58px);
  }
  .sidebar {
    background: var(--panel);
    border-right: 1px solid var(--border);
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    overflow-y: auto;
    max-height: calc(100vh - 58px);
    position: sticky;
    top: 58px;
  }
  .section-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    margin-bottom: 7px;
    font-weight: 600;
  }
  .dropzone {
    border: 2px dashed #475569;
    border-radius: 11px;
    padding: 13px 10px;
    text-align: center;
    cursor: pointer;
    transition: all 0.2s;
    background: rgba(15, 23, 42, 0.5);
  }
  .dropzone:hover, .dropzone.dragover {
    border-color: var(--primary);
    background: rgba(59, 130, 246, 0.1);
  }
  .dropzone h3 { font-size: 13px; margin-bottom: 3px; word-break: break-all; }
  .dropzone p { font-size: 11px; color: var(--muted); }
  .detect-banner {
    margin-top: 7px;
    padding: 6px 9px;
    border-radius: 7px;
    background: rgba(59, 130, 246, 0.15);
    border: 1px solid rgba(59, 130, 246, 0.4);
    color: #93c5fd;
    font-size: 11.5px;
    display: none;
  }
  .demo-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 7px;
  }
  .pill {
    font-size: 11px;
    padding: 4px 8px;
    border-radius: 6px;
    background: #0f172a;
    border: 1px solid var(--border);
    color: #cbd5e1;
    cursor: pointer;
    transition: all 0.15s;
  }
  .pill:hover { border-color: var(--primary); color: #fff; }
  .pill.active {
    background: rgba(59, 130, 246, 0.22);
    border-color: var(--primary);
    color: #fff;
    font-weight: 600;
  }
  .pill-table { border-color: rgba(59, 130, 246, 0.45); color: #93c5fd; }
  .pill-figure { border-color: rgba(16, 185, 129, 0.45); color: #6ee7b7; }

  .mode-cards { display: flex; flex-direction: column; gap: 6px; }
  .mode-card {
    border: 1px solid var(--border);
    background: rgba(15, 23, 42, 0.55);
    border-radius: 9px;
    padding: 8px 11px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .mode-card.active {
    border-color: var(--primary);
    background: rgba(59, 130, 246, 0.14);
    box-shadow: 0 0 0 1px var(--primary);
  }
  .mode-card h4 { font-size: 13px; margin-bottom: 2px; display: flex; justify-content: space-between; }
  .mode-card p { font-size: 11px; color: var(--muted); line-height: 1.35; }

  .control-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
    background: rgba(15, 23, 42, 0.45);
    padding: 11px;
    border-radius: 10px;
    border: 1px solid var(--border);
  }
  .checkbox-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
    gap: 8px;
  }
  .checkbox-row input[type="checkbox"] {
    width: 15px;
    height: 15px;
    accent-color: var(--primary);
    cursor: pointer;
  }
  .slider-row {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 11.5px;
  }
  .slider-row label { display: flex; justify-content: space-between; color: #cbd5e1; }
  .slider-row input[type="range"] { width: 100%; accent-color: var(--primary); }

  .seg-group {
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 11.5px;
  }
  .seg-group-label {
    display: flex;
    justify-content: space-between;
    color: #cbd5e1;
    font-weight: 500;
  }
  .seg-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(60px, 1fr));
    gap: 4px;
    background: #0f172a;
    padding: 3px;
    border-radius: 8px;
    border: 1px solid var(--border);
  }
  .seg-btn {
    border: none;
    background: transparent;
    color: var(--muted);
    font-size: 11px;
    padding: 5px 4px;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 500;
    text-align: center;
    transition: all 0.15s;
    line-height: 1.25;
  }
  .seg-btn:hover { color: #f8fafc; background: rgba(255,255,255,0.05); }
  .seg-btn.active {
    background: var(--primary);
    color: #fff;
    font-weight: 600;
    box-shadow: 0 2px 8px rgba(59,130,246,0.35);
  }
  .seg-btn.active-emerald {
    background: var(--emerald);
    color: #fff;
    font-weight: 600;
    box-shadow: 0 2px 8px rgba(16,185,129,0.35);
  }

  .main-stage {
    padding: 14px 20px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    overflow-y: auto;
  }

  /* Upgraded Navigation & Quick-Jump Toolbar */
  .nav-panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 11px 15px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .nav-top-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
  }
  .page-controls {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .page-input {
    width: 58px;
    background: #0f172a;
    border: 1px solid #475569;
    color: #fff;
    border-radius: 6px;
    padding: 5px 7px;
    font-size: 13px;
    font-weight: 600;
    text-align: center;
  }
  .page-input:focus { outline: none; border-color: var(--primary); }
  .page-slider-wrap {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
    min-width: 180px;
    max-width: 360px;
    background: #0f172a;
    padding: 5px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
  }
  .page-slider-wrap input[type="range"] {
    flex: 1;
    accent-color: var(--primary);
    cursor: pointer;
  }
  .stats-badges { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .stat-badge {
    background: #0f172a;
    border: 1px solid var(--border);
    padding: 4px 10px;
    border-radius: 7px;
    font-size: 11.5px;
  }
  .stat-badge strong { color: #38bdf8; font-size: 13px; }

  .highlights-bar {
    display: flex;
    align-items: center;
    gap: 6px;
    overflow-x: auto;
    padding-bottom: 3px;
    border-top: 1px solid rgba(255,255,255,0.06);
    padding-top: 8px;
  }
  .highlights-bar::-webkit-scrollbar { height: 5px; }
  .highlights-bar::-webkit-scrollbar-thumb { background: #475569; border-radius: 4px; }
  .hl-label {
    font-size: 11px;
    color: var(--muted);
    white-space: nowrap;
    font-weight: 600;
  }

  /* Preview Grid & View Switcher */
  .preview-grid {
    display: grid;
    grid-template-columns: 1fr 1.15fr;
    gap: 16px;
    align-items: start;
  }
  @media (max-width: 1200px) {
    .preview-grid { grid-template-columns: 1fr; }
  }
  .preview-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 11px;
    position: relative;
  }
  .preview-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 9px;
    flex-wrap: wrap;
    gap: 8px;
  }
  .preview-card-header h3 { font-size: 13.5px; font-weight: 600; }
  .legend { display: flex; gap: 7px; font-size: 11px; color: var(--muted); flex-wrap: wrap; }
  .legend span { display: inline-flex; align-items: center; gap: 4px; }
  .dot { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }

  /* Interactive Source Image Overlay */
  .source-stage {
    display: flex;
    justify-content: center;
    align-items: center;
    background: #0f172a;
    padding: 12px;
    border-radius: 8px;
    min-height: 420px;
  }
  .source-img-container {
    position: relative;
    display: inline-block;
    max-width: 100%;
  }
  .source-img-container img {
    display: block;
    max-width: 100%;
    max-height: 740px;
    width: auto;
    height: auto;
    border-radius: 4px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.45);
    cursor: zoom-in;
  }
  .slice-hotspot {
    position: absolute;
    border: 2px solid transparent;
    border-radius: 3px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .slice-hotspot:hover, .slice-hotspot.active-slice {
    border-color: #facc15;
    background: rgba(250, 204, 21, 0.18);
    box-shadow: 0 0 12px rgba(250, 204, 21, 0.55);
  }

  /* Right KPW6 Simulator & Grid Modes */
  .view-tabs {
    display: inline-flex;
    background: #0f172a;
    padding: 3px;
    border-radius: 8px;
    border: 1px solid var(--border);
    gap: 3px;
  }
  .view-tab {
    border: none;
    background: transparent;
    color: var(--muted);
    font-size: 11.5px;
    padding: 4px 10px;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 500;
  }
  .view-tab.active {
    background: var(--primary);
    color: #fff;
    font-weight: 600;
  }

  /* Single Screen 1:1 Simulator */
  .single-sim-stage {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    background: #0f172a;
    padding: 14px;
    border-radius: 10px;
  }
  .screen-Subtabs {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    flex-wrap: wrap;
    gap: 8px;
  }
  .screen-pills { display: flex; gap: 5px; flex-wrap: wrap; }
  .screen-pill {
    padding: 4px 10px;
    font-size: 12px;
    border-radius: 6px;
    background: #1e293b;
    border: 1px solid var(--border);
    color: #cbd5e1;
    cursor: pointer;
  }
  .screen-pill.active {
    background: var(--emerald);
    border-color: var(--emerald);
    color: #fff;
    font-weight: 600;
  }

  .kpw6-large-bezel {
    background: #18181b;
    border: 4px solid #3f3f46;
    border-radius: 22px;
    padding: 16px 16px 32px 16px;
    width: 100%;
    max-width: 520px;
    box-shadow: 0 16px 36px rgba(0,0,0,0.6);
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    transition: box-shadow 0.2s;
  }
  .kpw6-large-bezel.flash-highlight {
    box-shadow: 0 0 0 3px #facc15, 0 16px 36px rgba(0,0,0,0.6);
  }
  .kpw6-large-bezel::after {
    content: "KINDLE PAPERWHITE SIGNATURE EDITION · 7\\" CARTA 1300 (1264×1680)";
    position: absolute;
    bottom: 9px;
    font-size: 9.5px;
    letter-spacing: 0.08em;
    color: #71717a;
    font-weight: 600;
  }
  .kpw6-large-bezel img {
    width: 100%;
    height: auto;
    border-radius: 3px;
    background: #fff;
    cursor: zoom-in;
    display: block;
  }

  /* Grid Mode (Larger 340px+ cards) */
  .kpw6-screens-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
    gap: 14px;
  }
  .kpw6-frame {
    background: #18181b;
    border: 3px solid #3f3f46;
    border-radius: 18px;
    padding: 12px 12px 26px 12px;
    display: flex;
    flex-direction: column;
    align-items: center;
    box-shadow: 0 12px 28px rgba(0,0,0,0.5);
    position: relative;
    transition: all 0.2s;
  }
  .kpw6-frame.active-frame {
    border-color: var(--emerald);
    box-shadow: 0 0 0 2px var(--emerald), 0 12px 28px rgba(0,0,0,0.5);
  }
  .kpw6-frame::after {
    content: "KINDLE PAPERWHITE SIGNATURE (1264×1680)";
    position: absolute;
    bottom: 6px;
    font-size: 8.5px;
    letter-spacing: 0.08em;
    color: #71717a;
  }
  .kpw6-frame img {
    width: 100%;
    border-radius: 3px;
    background: #fff;
    cursor: zoom-in;
  }
  .screen-label {
    font-size: 11.5px;
    color: #e2e8f0;
    margin-bottom: 7px;
    width: 100%;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  /* Loading Overlay */
  .loading-badge {
    position: fixed;
    bottom: 20px;
    right: 24px;
    background: rgba(15, 23, 42, 0.92);
    border: 1px solid var(--primary);
    color: #93c5fd;
    padding: 8px 14px;
    border-radius: 999px;
    font-size: 12.5px;
    font-weight: 600;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5);
    display: none;
    z-index: 60;
  }

  /* Fullscreen HD Zoom Lightbox Modal */
  .lightbox-overlay {
    position: fixed;
    inset: 0;
    background: rgba(5, 10, 20, 0.92);
    backdrop-filter: blur(6px);
    z-index: 100;
    display: none;
    flex-direction: column;
  }
  .lightbox-header {
    padding: 12px 24px;
    background: rgba(30, 41, 59, 0.9);
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .lightbox-body {
    flex: 1;
    overflow: auto;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding: 24px;
  }
  .lightbox-body img {
    background: #fff;
    border-radius: 6px;
    box-shadow: 0 20px 50px rgba(0,0,0,0.75);
    transition: width 0.15s;
  }

  .toast {
    background: rgba(16, 185, 129, 0.15);
    border: 1px solid var(--emerald);
    color: #d1fae5;
    padding: 11px 16px;
    border-radius: 10px;
    font-size: 13px;
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="brand-badge">KPW6 签名版 · 五合一全能版</span>
    <div>
      <h1>Kindle PDF / 漫画 / 纯文字重排 & 去网格工作台</h1>
      <p>免越狱 · 页码跳转 / 图表索引 / KPW6 版式预览 / 高清放大镜 · 支持导出 EPUB 供传书使用</p>
    </div>
  </div>
  <div class="header-actions">
    <a class="btn" href="https://github.com/JolnJack/kpw6-pdf-studio" target="_blank" rel="noopener noreferrer">源码与许可 · AGPLv3</a>
    <button class="btn" onclick="openOutputFolder()">📂 打开导出目录</button>
    <a class="btn btn-primary" href="https://www.amazon.com/sendtokindle" target="_blank">☁️ 打开官方 Send to Kindle 网页</a>
  </div>
</header>

<div class="workspace">
  <aside class="sidebar">
    <div>
      <div class="section-title">1. 上传或切换 PDF 文档</div>
      <div class="dropzone" id="dropzone" onclick="document.getElementById('fileInput').click()">
        <input type="file" id="fileInput" accept=".pdf" style="display:none" onchange="handleFileUpload(this.files[0])">
        <h3 id="currentFileName">📥 点击上传或将 PDF 拖到这里</h3>
        <p>上传后自动检测类型并建立全书图表索引</p>
      </div>
      <div class="detect-banner" id="detectBanner"></div>

      <div class="demo-pills" id="uploadedFilesWrap" style="display:none;">
        <span style="font-size:11px;color:#38bdf8;align-self:center;width:100%;font-weight:600;">📂 你上传的 PDF 文档（点击即切换）：</span>
        <div id="uploadedFilesPills" style="display:flex;flex-wrap:wrap;gap:5px;width:100%;"></div>
      </div>

      <div class="demo-pills">
        <span style="font-size:11px;color:var(--muted);align-self:center;width:100%;">内置 5 大类型测试样张：</span>
        <button class="pill" id="pill-demo_academic_2col" onclick="loadDemo('demo_academic_2col', 'academic_2col')">📄 1.双栏论文</button>
        <button class="pill" id="pill-demo_tech_book" onclick="loadDemo('demo_tech_book', 'tech_book')">📊 2.图表专业书</button>
        <button class="pill" id="pill-demo_scanned_book" onclick="loadDemo('demo_scanned_book', 'scanned_book')">🔍 3.泛黄扫描书</button>
        <button class="pill" id="pill-demo_comic_manga" onclick="loadDemo('demo_comic_manga', 'comic_manga')">🎨 4.PDF漫画跨页</button>
        <button class="pill" id="pill-demo_pure_text" onclick="loadDemo('demo_pure_text', 'pure_text')">📖 5.纯文字小说</button>
      </div>
    </div>

    <div>
      <div class="section-title">2. 五大专属文档重排引擎</div>
      <div class="mode-cards">
        <div class="mode-card active" id="card-academic_2col" onclick="selectMode('academic_2col')">
          <h4><span>📄 双栏学术论文</span><span style="color:#38bdf8;font-size:11.5px;">跨栏图表保护 + 旋转扶正</span></h4>
          <p>识别跨栏表格与附图，支持旋转图表与双栏分屏；导出前可逐页预览并调整。</p>
        </div>
        <div class="mode-card" id="card-tech_book" onclick="selectMode('tech_book')">
          <h4><span>📊 带图表的专业书</span><span style="color:#10b981;font-size:11.5px;">图表版面保护</span></h4>
          <p>裁切白边与页眉页脚，支持竖屏或横屏大字模式；复杂图表可手动设为整页保留。</p>
        </div>
        <div class="mode-card" id="card-scanned_book" onclick="selectMode('scanned_book')">
          <h4><span>🔍 扫描版书籍</span><span style="color:#f59e0b;font-size:11.5px;">灰底与墨迹增强</span></h4>
          <p>提亮泛黄或浅灰背景，并增强文字对比度；实际效果请结合原图与预览确认。</p>
        </div>
        <div class="mode-card" id="card-comic_manga" onclick="selectMode('comic_manga')">
          <h4><span>🎨 PDF 漫画 / 纯图画册</span><span style="color:#f43f5e;font-size:11.5px;">双页拆切+满屏</span></h4>
          <p>自动识别横版双页跨页并从中缝切为两屏，支持日漫右➔左顺序与无白边满屏。</p>
        </div>
        <div class="mode-card" id="card-pure_text" onclick="selectMode('pure_text')">
          <h4><span>📖 纯文字 PDF 书籍</span><span style="color:#a78bfa;font-size:11.5px;">转原生 EPUB + 重排</span></h4>
          <p>剔除页眉页码、智能合并断句、识别章节目录，支持导出原生可调字号 EPUB。</p>
        </div>
      </div>
    </div>

    <div>
      <div class="section-title">3. 模式专属参数微调 & 扫描去污纠偏</div>
      <div class="control-group">
        <div id="ctrlCommon">
          <label class="checkbox-row" style="margin-bottom:6px;">
            <span>智能行间切分与屏幕重排</span>
            <input type="checkbox" id="optSplitPages" checked onchange="refreshPreview()">
          </label>
          <label class="checkbox-row" style="margin-bottom:6px;">
            <span>自动剔除页眉与页码页脚</span>
            <input type="checkbox" id="optTrimHeader" checked onchange="refreshPreview()">
          </label>
          <label class="checkbox-row" style="margin-bottom:6px;">
            <span>横屏大字模式 (适合单栏/宽表)</span>
            <input type="checkbox" id="optLandscape" onchange="refreshPreview()">
          </label>
          <label class="checkbox-row" style="margin-bottom:6px;">
            <span>Carta 1300 去网格漂白与黑度增强</span>
            <input type="checkbox" id="optEnhance" onchange="refreshPreview()">
          </label>
          <div style="border-top:1px solid rgba(255,255,255,0.08);padding-top:7px;margin-top:4px;display:flex;flex-direction:column;gap:6px;">
            <div style="font-size:10.5px;color:#fbbf24;font-weight:600;">🧹 扫描件/旧书深度净化与纠偏：</div>
            <label class="checkbox-row" title="自动识别并切除左右两侧孤立的复印机装订黑边与装订孔阴影，释放宝贵屏宽">
              <span>✂️ 切除书脊装订黑边与打孔阴影</span>
              <input type="checkbox" id="optCleanSpine" checked onchange="refreshPreview()">
            </label>
            <label class="checkbox-row" title="剥离页面背景上的浅灰色斜体水印、印章残影与扫描底噪，避免遮挡正文或撑大白边">
              <span>🧼 剥离浅灰水印与底纹噪点</span>
              <input type="checkbox" id="optRemoveWatermark" onchange="refreshPreview()">
            </label>
            <label class="checkbox-row" title="通过行投影方差检测扫描歪斜角度（±3°以内）并自动旋转扶正">
              <span>📐 扫描页面歪斜自动扶正 (Deskew)</span>
              <input type="checkbox" id="optAutoDeskew" onchange="refreshPreview()">
            </label>
          </div>
        </div>

        <div id="ctrlComic" style="display:none;border-top:1px solid var(--border);padding-top:8px;">
          <label class="checkbox-row" style="margin-bottom:6px;">
            <span>横版双页跨页自动居中切开</span>
            <input type="checkbox" id="optComicSplit" checked onchange="refreshPreview()">
          </label>
          <label class="checkbox-row" style="margin-bottom:6px;">
            <span>日漫翻页顺序 (先看右半页 ➔ 再看左半页)</span>
            <input type="checkbox" id="optMangaRtl" checked onchange="refreshPreview()">
          </label>
          <label class="checkbox-row">
            <span>满屏无白边模式 (Full-Bleed 铺满屏幕)</span>
            <input type="checkbox" id="optComicBleed" checked onchange="refreshPreview()">
          </label>
        </div>

        <div id="ctrlPureText" style="display:none;border-top:1px solid var(--border);padding-top:8px;">
          <div class="slider-row" style="margin-bottom:7px;">
            <label><span>重排 PDF 字号大小 (pt)</span><span id="valFontSize">13.5 pt</span></label>
            <input type="range" id="optFontSize" min="10.0" max="20.0" step="0.5" value="13.5" onchange="document.getElementById('valFontSize').innerText=this.value+' pt'; refreshPreview()">
          </div>
          <div class="slider-row">
            <label><span>重排行距倍数</span><span id="valLineSpacing">1.65x</span></label>
            <input type="range" id="optLineSpacing" min="1.30" max="2.10" step="0.05" value="1.65" onchange="document.getElementById('valLineSpacing').innerText=this.value+'x'; refreshPreview()">
          </div>
        </div>

        <div class="slider-row" id="sliderGammaWrap">
          <label><span>墨迹/线条加深强度 (Gamma)</span><span id="valGamma">0.55</span></label>
          <input type="range" id="optGamma" min="0.30" max="0.90" step="0.05" value="0.55" onchange="document.getElementById('valGamma').innerText=this.value; refreshPreview()">
        </div>
        <div class="slider-row" id="sliderMarginWrap">
          <label><span>KPW6 屏幕留白边距 (%)</span><span id="valMargin">2.5%</span></label>
          <input type="range" id="optMargin" min="0.5" max="6.0" step="0.5" value="2.5" onchange="document.getElementById('valMargin').innerText=this.value+'%'; refreshPreview()">
        </div>
      </div>
    </div>

    <div>
      <div class="section-title" style="color:#38bdf8;">4. ✨ 超清画质增强档位 & 笔画加粗控制</div>
      <div class="control-group" style="border-color:rgba(56,189,248,0.35);background:rgba(15,23,42,0.65);">
        <div class="seg-group">
          <div class="seg-group-label">
            <span>输出超采样清晰度档位</span>
            <span id="valDpiPreset" style="color:#38bdf8;font-weight:600;">450 PPI (1.5x 超清)</span>
          </div>
          <div class="seg-row">
            <button type="button" class="seg-btn" id="dpi-300" onclick="setDpiPreset(300)">300 PPI<br><span style="font-size:9.5px;opacity:0.8;">标准轻巧</span></button>
            <button type="button" class="seg-btn active" id="dpi-450" onclick="setDpiPreset(450)">450 PPI<br><span style="font-size:9.5px;opacity:0.9;">1.5x超清推荐</span></button>
            <button type="button" class="seg-btn" id="dpi-600" onclick="setDpiPreset(600)">600 PPI<br><span style="font-size:9.5px;opacity:0.85;">2.0x超采样</span></button>
          </div>
        </div>

        <div class="seg-group" style="margin-top:3px;">
          <div class="seg-group-label">
            <span>字体/图纸线条加粗档位</span>
            <span id="valStrokeBold" style="color:#6ee7b7;font-weight:600;">0: 原细度</span>
          </div>
          <div class="seg-row" style="grid-template-columns: repeat(4, 1fr);">
            <button type="button" class="seg-btn active-emerald" id="bold-0" onclick="setStrokeBoldness(0)">0 级<br><span style="font-size:9.5px;opacity:0.85;">原细度</span></button>
            <button type="button" class="seg-btn" id="bold-1" onclick="setStrokeBoldness(1)">+1 微粗<br><span style="font-size:9.5px;opacity:0.85;">修断笔</span></button>
            <button type="button" class="seg-btn" id="bold-2" onclick="setStrokeBoldness(2)">+2 中粗<br><span style="font-size:9.5px;opacity:0.85;">黑体护眼</span></button>
            <button type="button" class="seg-btn" id="bold-3" onclick="setStrokeBoldness(3)">+3 强粗<br><span style="font-size:9.5px;opacity:0.85;">极细图纸</span></button>
          </div>
        </div>

        <div class="slider-row" style="margin-top:3px;">
          <label><span>边缘锐化强度 (Unsharp Mask)</span><span id="valSharpen" style="color:#38bdf8;">1.6x</span></label>
          <input type="range" id="optSharpen" min="0.0" max="2.5" step="0.1" value="1.6" onchange="document.getElementById('valSharpen').innerText=parseFloat(this.value).toFixed(1)+'x'; refreshPreview()">
        </div>

        <label class="checkbox-row" style="margin-top:2px;border-top:1px solid rgba(255,255,255,0.07);padding-top:6px;" title="在适用的 PDF 页面上保留矢量文字与公式路径；图像增强、加粗等选项可能需要栅格化">
          <span>保留原生矢量内容 (适用时)</span>
          <input type="checkbox" id="optVectorLossless" checked onchange="refreshPreview()">
        </label>
      </div>
    </div>

    <div>
      <div class="section-title" style="color:#c084fc;">5. 🖼️ Kindle 书架/锁屏封面生成设置</div>
      <div class="control-group" style="border-color:rgba(192,132,252,0.35);background:rgba(15,23,42,0.65);">
        <div class="seg-group">
          <div class="seg-group-label">
            <span>封面生成样式 (第 1 页实时预览)</span>
            <span id="valCoverMode" style="color:#c084fc;font-weight:600;">精装书封卡</span>
          </div>
          <div class="seg-row" style="grid-template-columns: repeat(4, 1fr);">
            <button type="button" class="seg-btn active" id="cov-smart_card" onclick="setCoverMode('smart_card')">精装卡面<br><span style="font-size:9px;opacity:0.85;">标题+首页</span></button>
            <button type="button" class="seg-btn" id="cov-first_page" onclick="setCoverMode('first_page')">原书首页<br><span style="font-size:9px;opacity:0.85;">完整不切</span></button>
            <button type="button" class="seg-btn" id="cov-custom_image" onclick="setCoverMode('custom_image')">自定义图<br><span style="font-size:9px;opacity:0.85;">上传海报</span></button>
            <button type="button" class="seg-btn" id="cov-none" onclick="setCoverMode('none')">无封面<br><span style="font-size:9px;opacity:0.85;">直接切分</span></button>
          </div>
        </div>

        <div id="coverTextInputs" style="display:flex;flex-direction:column;gap:5px;margin-top:3px;">
          <input type="text" id="optCoverTitle" placeholder="自定义封面书名（留空则自动提取论文/书籍标题）"
                 style="background:#0f172a;border:1px solid var(--border);color:#f8fafc;border-radius:6px;padding:5px 8px;font-size:11.5px;width:100%;"
                 onchange="if(currentPage!==0){jumpToPage(0);}else{refreshPreview(0);}">
          <input type="text" id="optCoverAuthor" placeholder="自定义作者/副标题（留空则自动标注）"
                 style="background:#0f172a;border:1px solid var(--border);color:#cbd5e1;border-radius:6px;padding:5px 8px;font-size:11.5px;width:100%;"
                 onchange="if(currentPage!==0){jumpToPage(0);}else{refreshPreview(0);}">
        </div>

        <div id="coverUploadWrap" style="display:none;margin-top:4px;">
          <input type="file" id="coverFileInput" accept="image/*" style="display:none" onchange="handleCoverUpload(this.files[0])">
          <button type="button" class="btn btn-sm" style="width:100%;justify-content:center;" onclick="document.getElementById('coverFileInput').click()">
            🖼️ 点击上传自定义封面图片 (.jpg / .png)
          </button>
          <div id="coverUploadStatus" style="font-size:10.5px;color:#c084fc;margin-top:3px;text-align:center;"></div>
        </div>
      </div>
    </div>

    <div>
      <div class="section-title" style="color:#10b981;">6. 📦 导出格式、页码范围 & 大书自动分卷</div>
      <div class="control-group" style="border-color:rgba(16,185,129,0.35);background:rgba(15,23,42,0.65);">
        <div class="seg-group">
          <div class="seg-group-label">
            <span>目标电子书格式</span>
            <span id="valExportFormat" style="color:#6ee7b7;font-weight:600;">.EPUB (版面精排+封面)</span>
          </div>
          <div class="seg-row" style="grid-template-columns: repeat(3, 1fr);">
            <button type="button" class="seg-btn active-emerald" id="fmt-epub_layout" onclick="setExportFormat('epub_layout')">.EPUB 精排<br><span style="font-size:9px;opacity:0.88;">云传有封面★</span></button>
            <button type="button" class="seg-btn" id="fmt-pdf" onclick="setExportFormat('pdf')">.PDF 专享<br><span style="font-size:9px;opacity:0.85;">矢量优先</span></button>
            <button type="button" class="seg-btn" id="fmt-azw3" onclick="setExportFormat('azw3')">.AZW3 原生<br><span style="font-size:9px;opacity:0.85;">USB直传★</span></button>
            <button type="button" class="seg-btn" id="fmt-epub_reflow" onclick="setExportFormat('epub_reflow')">.EPUB 流式<br><span style="font-size:9px;opacity:0.85;">可调字号</span></button>
            <button type="button" class="seg-btn" id="fmt-mobi" onclick="setExportFormat('mobi')">.MOBI 经典<br><span style="font-size:9px;opacity:0.85;">旧版兼容</span></button>
            <button type="button" class="seg-btn" id="fmt-cbz" onclick="setExportFormat('cbz')">.CBZ 图集<br><span style="font-size:9px;opacity:0.85;">无损漫画包</span></button>
          </div>
        </div>
        <div id="formatHintText" style="font-size:10.5px;color:#cbd5e1;line-height:1.4;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.28);padding:6px 8px;border-radius:6px;margin-top:2px;">
          ✨ <strong>图像版 EPUB：</strong>保留生成的分屏图像，内置封面与图表目录。可尝试通过 Send to Kindle 传书；设备上的版式与封面显示请实际确认。
        </div>

        <div style="border-top:1px solid rgba(255,255,255,0.08);padding-top:7px;margin-top:3px;display:flex;flex-direction:column;gap:6px;">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:11.5px;">
            <span style="color:#cbd5e1;font-weight:500;">📑 导出页码范围截取</span>
            <div style="display:flex;gap:4px;">
              <button type="button" class="pill" style="padding:2px 6px;font-size:10px;" onclick="setPageRangeAll()">全书范围</button>
              <button type="button" class="pill" style="padding:2px 6px;font-size:10px;border-color:#38bdf8;color:#93c5fd;" onclick="setPageRangeCurrent()">仅导当前页</button>
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:6px;font-size:11.5px;">
            <span style="color:var(--muted);">从第</span>
            <input type="number" id="optStartPage" min="1" value="1"
                   style="width:64px;background:#0f172a;border:1px solid var(--border);color:#fff;border-radius:6px;padding:4px 6px;text-align:center;font-weight:600;">
            <span style="color:var(--muted);">页 到第</span>
            <input type="number" id="optEndPage" min="1" placeholder="末页"
                   style="width:64px;background:#0f172a;border:1px solid var(--border);color:#fff;border-radius:6px;padding:4px 6px;text-align:center;font-weight:600;">
            <span style="color:var(--muted);">页</span>
          </div>
        </div>

        <div class="seg-group" style="border-top:1px solid rgba(255,255,255,0.08);padding-top:7px;margin-top:2px;">
          <div class="seg-group-label">
            <span>📚 大书自动分卷 (防超200M)</span>
            <span id="valSplitVol" style="color:#fbbf24;font-weight:600;">整本不拆卷</span>
          </div>
          <div class="seg-row" style="grid-template-columns: repeat(4, 1fr);">
            <button type="button" class="seg-btn active" id="vol-0" onclick="setSplitVolumes(0)">整本导出<br><span style="font-size:9px;opacity:0.85;">单文件</span></button>
            <button type="button" class="seg-btn" id="vol-50" onclick="setSplitVolumes(50)">50页/卷<br><span style="font-size:9px;opacity:0.85;">轻量分册</span></button>
            <button type="button" class="seg-btn" id="vol-100" onclick="setSplitVolumes(100)">100页/卷<br><span style="font-size:9px;opacity:0.88;">大书推荐★</span></button>
            <button type="button" class="seg-btn" id="vol-150" onclick="setSplitVolumes(150)">150页/卷<br><span style="font-size:9px;opacity:0.85;">长卷分册</span></button>
          </div>
        </div>
      </div>
    </div>

    <div style="display:flex;flex-direction:column;gap:7px;">
      <button class="btn btn-emerald" style="padding:11px;font-size:13px;justify-content:center;" id="mainExportBtn" onclick="exportSelectedFormat()">
        🚀 一键导出【.EPUB 满屏精排版】
      </button>
      <button class="btn btn-purple" style="padding:8px;font-size:11.5px;justify-content:center;" id="bundleExportBtn" onclick="exportSelectedFormat('bundle_both')">
        ⚡ 同时生成【.PDF 矢量版 + .EPUB 版】双文件
      </button>
    </div>
  </aside>

  <main class="main-stage">
    <!-- Real-Time Export Progress Panel -->
    <div id="exportProgressPanel" style="display:none;background:rgba(30,41,59,0.96);border:1px solid #3b82f6;border-radius:11px;padding:12px 16px;flex-direction:column;gap:8px;box-shadow:0 8px 24px rgba(0,0,0,0.35);">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
        <div style="display:flex;align-items:center;gap:8px;">
          <span id="exportProgressSpinner" style="font-size:15px;">⚙️</span>
          <strong id="exportProgressTitle" style="font-size:13px;color:#f8fafc;">正在为 KPW6 精排生成电子书...</strong>
        </div>
        <div style="display:flex;align-items:center;gap:10px;font-size:12px;">
          <span id="exportProgressEta" style="color:#93c5fd;">已用时 0.0s · 正在估算剩余时间...</span>
          <span id="exportProgressPct" style="background:#3b82f6;color:#fff;padding:2px 8px;border-radius:999px;font-weight:700;font-size:11.5px;">0%</span>
          <button type="button" class="btn btn-sm" id="cancelExportBtn" style="border-color:#ef4444;color:#fca5a5;padding:3px 9px;" onclick="cancelActiveExport()">⏹ 取消</button>
        </div>
      </div>
      <div style="width:100%;height:9px;background:#0f172a;border-radius:999px;overflow:hidden;border:1px solid #334155;">
        <div id="exportProgressBarFill" style="width:2%;height:100%;background:linear-gradient(90deg, #3b82f6, #10b981);transition:width 0.25s ease;"></div>
      </div>
    </div>

    <div class="toast" id="exportToast">
      <span id="toastText">✅ 处理完成！</span>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
        <a class="btn btn-primary" id="viewPdfOnlineLink" href="#" target="_blank" style="display:none;">👁️ 在浏览器新标签页通览整本成品 PDF</a>
        <a class="btn btn-emerald" id="downloadLink" href="#" download>💾 下载主文件到本地</a>
        <a class="btn btn-purple" id="downloadSecondLink" href="#" download style="display:none;">💾 下载配套 EPUB</a>
        <div id="volumeDownloadsContainer" style="display:none;gap:6px;flex-wrap:wrap;"></div>
        <button class="btn" onclick="openOutputFolder()">📂 打开输出文件夹</button>
      </div>
    </div>

    <!-- Upgraded Navigation & Quick-Jump Bar -->
    <div class="nav-panel">
      <div class="nav-top-row">
        <div class="page-controls">
          <button class="btn btn-sm" onclick="jumpToPage(0)" title="跳到第一页">⏮ 首页</button>
          <button class="btn btn-sm" onclick="changePage(-5)" title="快退 5 页">-5页</button>
          <button class="btn btn-sm btn-primary" onclick="changePage(-1)" title="快捷键：↑ 或 PageUp">⬅️ 上一页</button>
          <span style="font-size:12.5px;display:inline-flex;align-items:center;gap:4px;">
            第
            <input type="number" id="pageInputBox" class="page-input" min="1" value="1"
                   onkeydown="if(event.key==='Enter') jumpToPage(parseInt(this.value||1)-1)"
                   onchange="jumpToPage(parseInt(this.value||1)-1)">
            / <strong id="totalPagesNum">1</strong> 页
          </span>
          <button class="btn btn-sm btn-primary" onclick="changePage(1)" title="快捷键：↓ 或 PageDown">下一页 ➡️</button>
          <button class="btn btn-sm" onclick="changePage(5)" title="快进 5 页">+5页</button>
          <button class="btn btn-sm" onclick="jumpToPage(totalPages - 1)" title="跳到最后一页">末页 ⏭</button>
        </div>

        <div class="page-slider-wrap" title="拖动滑块快速跳到任意页">
          <span style="font-size:11px;color:var(--muted);">滑块跳页</span>
          <input type="range" id="pageSlider" min="1" max="1" value="1"
                 oninput="document.getElementById('pageInputBox').value=this.value"
                 onchange="jumpToPage(parseInt(this.value)-1)">
        </div>

        <div class="stats-badges">
          <div class="stat-badge">识别区块：<strong id="statSlices">-</strong> 个</div>
          <div class="stat-badge">生成屏幕：<strong id="statScreens">-</strong> 屏</div>
          <div class="stat-badge">字号放大：<strong id="statZoom">-</strong> 倍</div>
          <div class="stat-badge" title="当前画质档位与笔画加粗状态">画质引擎：<strong id="statQualityMode" style="color:#6ee7b7;">450 PPI · 矢量优先</strong></div>
        </div>
      </div>

      <!-- Per-Page Special Rule Override Bar -->
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;border-top:1px solid rgba(255,255,255,0.06);padding-top:8px;">
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
          <span style="font-size:11.5px;color:#cbd5e1;font-weight:600;">🎯 本页单独微调（第 <span id="overridePageNumLabel">1</span> 页）：</span>
          <button type="button" class="pill active" id="ovr-auto" onclick="setPageOverride('auto')">✨ 跟随全局自动</button>
          <button type="button" class="pill" id="ovr-whole_portrait" onclick="setPageOverride('whole_portrait')" title="强制将本页作为 1 张完整竖屏输出，不拆栏也不切分（适合复杂整版架构图/公式推导）">🔒 本页强制整页不切分</button>
          <button type="button" class="pill" id="ovr-whole_landscape" onclick="setPageOverride('whole_landscape')" title="强制将本页顺时针旋转 90° 横置满屏输出（适合超宽大表或横向流程图）">🔄 本页旋转90°横置满屏</button>
          <button type="button" class="pill" id="ovr-skip" onclick="setPageOverride('skip')" title="导出整本书时直接剔除跳过本页（适合空白页、广告页或无需阅读的附录页）">🚫 导出时剔除跳过本页</button>
        </div>
        <div id="activeOverridesWrap" style="display:none;align-items:center;gap:5px;flex-wrap:wrap;font-size:11px;">
          <span style="color:#fbbf24;font-weight:600;">📌 已设单页例外：</span>
          <div id="activeOverridesPills" style="display:flex;gap:4px;flex-wrap:wrap;"></div>
          <button type="button" class="pill" style="padding:2px 6px;font-size:10px;border-color:#ef4444;color:#fca5a5;" onclick="clearAllPageOverrides()">清空全部</button>
        </div>
      </div>

      <!-- Smart Document Table / Figure Quick-Jump Bar -->
      <div class="highlights-bar" id="highlightsBar" style="display:none;">
        <span class="hl-label">📑 图表/关键页一键直达：</span>
        <div id="highlightsList" style="display:flex;gap:5px;align-items:center;"></div>
      </div>
    </div>

    <div class="preview-grid">
      <div id="previewErrorNotice" role="alert" style="display:none;grid-column:1/-1;padding:12px 16px;border:1px solid #ef4444;border-radius:10px;background:rgba(239,68,68,0.12);color:#fecaca;"></div>
      <!-- Left: Source PDF with Interactive Slice Hotspots -->
      <div class="preview-card">
        <div class="preview-card-header">
          <div>
            <h3>原版 PDF 版面解构 <span style="font-size:11.5px;color:#38bdf8;font-weight:normal;">(点击图中任意 #编号 即可跳到右侧对应屏幕)</span></h3>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <div class="legend">
              <span><i class="dot" style="background:#2563eb"></i>通栏/大表</span>
              <span><i class="dot" style="background:#059669"></i>左栏</span>
              <span><i class="dot" style="background:#d97706"></i>右栏</span>
              <span><i class="dot" style="background:#0ea5e9"></i>旋转扶正图</span>
            </div>
            <button class="btn btn-sm" onclick="openLightbox(document.getElementById('sourcePreviewImg').src, '原版 PDF 第 ' + (currentPage+1) + ' 页版面拆解大图', 'source', 0)">🔍 放大原图</button>
          </div>
        </div>
        <div class="source-stage">
          <div class="source-img-container" id="sourceContainer">
            <img id="sourcePreviewImg" src="" alt="Source Annotated Preview" onclick="openLightbox(this.src, '原版 PDF 第 ' + (currentPage+1) + ' 页版面拆解大图', 'source', 0)">
            <div id="sliceHotspotsLayer"></div>
          </div>
        </div>
      </div>

      <!-- Right: KPW6 Simulator (Single 1:1 Large Mode + Multi-Screen Grid Mode) -->
      <div class="preview-card">
        <div class="preview-card-header">
          <div>
            <h3>KPW6 签名版 (7 英寸 1264×1680) 目标版式预览</h3>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <div class="view-tabs">
              <button class="view-tab active" id="tabSingleView" onclick="setRightViewMode('single')">📱 单屏大图精看</button>
              <button class="view-tab" id="tabGridView" onclick="setRightViewMode('grid')">🪟 本页多屏平铺</button>
            </div>
            <button class="btn btn-sm" onclick="zoomCurrentKpw6Screen()">🔍 高清放大镜</button>
          </div>
        </div>

        <!-- Mode A: Single 1:1 Large Simulator -->
        <div class="single-sim-stage" id="singleSimStage">
          <div class="screen-Subtabs">
            <div class="screen-pills" id="screenSelectorPills"></div>
            <div style="display:flex;gap:6px;">
              <button class="btn btn-sm" onclick="stepKpw6Screen(-1)" title="快捷键：← 左方向键">◀ 上一屏</button>
              <button class="btn btn-sm btn-primary" onclick="stepKpw6Screen(1)" title="快捷键：→ 右方向键">下一屏 ▶</button>
            </div>
          </div>

          <div id="skipPageNotice" style="display:none;width:100%;max-width:520px;background:rgba(239,68,68,0.14);border:2px dashed #ef4444;border-radius:14px;padding:36px 20px;text-align:center;color:#fca5a5;">
            <div style="font-size:28px;margin-bottom:8px;">🚫</div>
            <h4 style="font-size:15px;color:#fecaca;margin-bottom:6px;">本页已设为【导出时剔除跳过】</h4>
            <p style="font-size:12px;color:#cbd5e1;margin-bottom:14px;">生成最终电子书时将自动略过第 <span id="skipPageNumText">1</span> 页，不占用 KPW6 屏幕与文件体积。</p>
            <button type="button" class="btn btn-sm btn-primary" onclick="setPageOverride('auto')">↩️ 恢复本页正常排版</button>
          </div>

          <div class="kpw6-large-bezel" id="singleKpw6Bezel">
            <img id="singleKpw6Img" src="" alt="KPW6 1:1 Screen"
                 title="点击加载更高分辨率的预览图层"
                 onclick="zoomCurrentKpw6Screen()">
          </div>
          <p style="font-size:11.5px;color:var(--muted);text-align:center;">
            💡 提示：支持键盘 <strong>← / →</strong> 逐屏翻阅（翻完本页最后一屏自动进入下一页）。点击屏幕可加载更高分辨率图层；扫描源的细节仍受原始图像清晰度限制。
          </p>
        </div>

        <!-- Mode B: Multi-Screen Large Grid -->
        <div class="kpw6-screens-grid" id="kpw6ScreensContainer" style="display:none;">
        </div>
      </div>
    </div>
  </main>
</div>

<div class="loading-badge" id="loadingBadge">⏳ 正在渲染预览画面...</div>

<!-- Fullscreen HD Lightbox Modal -->
<div class="lightbox-overlay" id="lightboxModal" onclick="if(event.target===this) closeLightbox()">
  <div class="lightbox-header">
    <div>
      <strong id="lightboxTitle" style="font-size:14px;">🔍 高清细节放大镜</strong>
      <span id="lightboxHdBadge" style="margin-left:10px;font-size:11.5px;padding:3px 8px;border-radius:999px;background:rgba(16,185,129,0.2);color:#6ee7b7;border:1px solid #10b981;">✨ 正在加载高清图层...</span>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <button class="btn btn-sm" onclick="setLightboxZoom(920)">1.0x 适合窗口</button>
      <button class="btn btn-sm" onclick="setLightboxZoom(1380)">1.5x 放大</button>
      <button class="btn btn-sm" onclick="setLightboxZoom(1920)">2.2x 超清细节</button>
      <button class="btn btn-sm" onclick="setLightboxZoom(2500)">3.0x 显微镜级</button>
      <button class="btn btn-sm btn-primary" onclick="closeLightbox()">✖ 关闭 (Esc)</button>
    </div>
  </div>
  <div class="lightbox-body" onclick="if(event.target===this) closeLightbox()">
    <img id="lightboxImg" src="" style="width:1100px;max-width:none;" alt="Zoomed Preview">
  </div>
</div>

<script>
const KPW6_CSRF_TOKEN = "__KPW6_CSRF_TOKEN__";
let currentFileId = "demo_academic_2col";
let currentMode = "academic_2col";
let currentPage = 0;
let totalPages = 1;
let currentScreens = [];
let currentScreenIdx = 0;
let currentSlicesMeta = [];
let currentHighlights = [];
let rightViewMode = "single"; // "single" or "grid"
let currentDpiPreset = 450;
let currentStrokeBoldness = 0;
let currentCoverMode = "smart_card";
let currentCustomCoverId = "";
let currentHasCoverScreen = false;
let currentExportFormat = "epub_layout";
let userManuallyChoseFormat = false;
let currentSplitVolumesEvery = 0;
let activeExportJobId = "";
let pageOverridesByFile = {};
let previewRequestSerial = 0;
let fileSelectionSerial = 0;
let coverUploadSerial = 0;
let lightboxRequestSerial = 0;

const ALL_MODES = ["academic_2col", "tech_book", "scanned_book", "comic_manga", "pure_text"];

function getCurrentFileOverrides() {
  if (!pageOverridesByFile[currentFileId]) {
    pageOverridesByFile[currentFileId] = {};
  }
  return pageOverridesByFile[currentFileId];
}

function setPageOverride(rule) {
  const map = getCurrentFileOverrides();
  if (!rule || rule === "auto") {
    delete map[String(currentPage)];
  } else {
    map[String(currentPage)] = rule;
  }
  renderPageOverrideToolbar();
  refreshPreview(0);
}

function clearAllPageOverrides() {
  pageOverridesByFile[currentFileId] = {};
  renderPageOverrideToolbar();
  refreshPreview(0);
}

function renderPageOverrideToolbar() {
  const lbl = document.getElementById("overridePageNumLabel");
  if (lbl) lbl.innerText = currentPage + 1;
  const map = getCurrentFileOverrides();
  const activeRule = map[String(currentPage)] || "auto";
  ["auto", "whole_portrait", "whole_landscape", "skip"].forEach(r => {
    const el = document.getElementById("ovr-" + r);
    if (el) el.classList.toggle("active", r === activeRule);
  });

  const wrap = document.getElementById("activeOverridesWrap");
  const list = document.getElementById("activeOverridesPills");
  if (!wrap || !list) return;
  list.innerHTML = "";
  const entries = Object.entries(map).filter(([_, v]) => v && v !== "auto").sort((a, b) => parseInt(a[0]) - parseInt(b[0]));
  if (!entries.length) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "flex";
  const tagMap = {
    "whole_portrait": "🔒整页",
    "whole_landscape": "🔄横置90°",
    "skip": "🚫跳过"
  };
  entries.forEach(([pStr, rule]) => {
    const p0 = parseInt(pStr);
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pill" + (p0 === currentPage ? " active" : "");
    b.style.padding = "2px 7px";
    b.style.fontSize = "10.5px";
    b.innerText = `P${p0 + 1}:${tagMap[rule] || rule}`;
    b.title = `点击跳转到第 ${p0 + 1} 页`;
    b.onclick = () => jumpToPage(p0);
    list.appendChild(b);
  });
}

function setPageRangeAll() {
  document.getElementById("optStartPage").value = 1;
  document.getElementById("optEndPage").value = totalPages || "";
}

function setPageRangeCurrent() {
  document.getElementById("optStartPage").value = currentPage + 1;
  document.getElementById("optEndPage").value = currentPage + 1;
}

function setSplitVolumes(everyPages) {
  currentSplitVolumesEvery = everyPages;
  [0, 50, 100, 150].forEach(v => {
    const el = document.getElementById("vol-" + v);
    if (el) el.classList.toggle("active", v === everyPages);
  });
  const labels = {
    0: "整本不拆卷",
    50: "每 50 页拆一卷",
    100: "每 100 页拆一卷 (推荐)",
    150: "每 150 页拆一卷"
  };
  document.getElementById("valSplitVol").innerText = labels[everyPages] || `每 ${everyPages} 页一卷`;
}

const dropzone = document.getElementById("dropzone");
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files && e.dataTransfer.files[0]) {
    handleFileUpload(e.dataTransfer.files[0]);
  }
});

// Keyboard shortcuts: Left/Right flips KPW6 screens (or pages in grid mode), Up/Down flips PDF pages, Esc closes lightbox
window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  const lb = document.getElementById("lightboxModal");
  if (lb.style.display === "flex") {
    if (e.key === "Escape") closeLightbox();
    return;
  }
  if (e.key === "ArrowRight") {
    e.preventDefault();
    if (rightViewMode === "single") stepKpw6Screen(1);
    else changePage(1);
  } else if (e.key === "ArrowLeft") {
    e.preventDefault();
    if (rightViewMode === "single") stepKpw6Screen(-1);
    else changePage(-1);
  } else if (e.key === "ArrowDown" || e.key === "PageDown") {
    e.preventDefault();
    changePage(1);
  } else if (e.key === "ArrowUp" || e.key === "PageUp") {
    e.preventDefault();
    changePage(-1);
  }
});

function setRightViewMode(mode) {
  rightViewMode = mode;
  document.getElementById("tabSingleView").classList.toggle("active", mode === "single");
  document.getElementById("tabGridView").classList.toggle("active", mode === "grid");
  document.getElementById("singleSimStage").style.display = (mode === "single") ? "flex" : "none";
  document.getElementById("kpw6ScreensContainer").style.display = (mode === "grid") ? "grid" : "none";
}

function selectMode(mode, skipRefresh = false) {
  currentMode = mode;
  ALL_MODES.forEach(m => {
    document.getElementById("card-" + m).classList.toggle("active", m === mode);
  });

  document.getElementById("optEnhance").checked = (mode === "scanned_book" || mode === "comic_manga");
  document.getElementById("ctrlComic").style.display = (mode === "comic_manga") ? "block" : "none";
  document.getElementById("ctrlPureText").style.display = (mode === "pure_text") ? "block" : "none";
  if (!userManuallyChoseFormat) {
    setExportFormat(mode === "pure_text" ? "epub_reflow" : "epub_layout", true);
  }

  if (!skipRefresh) {
    refreshPreview();
  }
}

async function fetchUploadedFilesAndInit() {
  const selectionId = fileSelectionSerial;
  const res = await fetch("/api/files");
  const data = await res.json();
  if (selectionId !== fileSelectionSerial) return;
  renderUploadedPills(data.uploaded || []);

  // Auto-open history only while no newer user selection has taken place.
  if (data.uploaded && data.uploaded.length > 0) {
    const latest = data.uploaded[0];
    await switchToUploadedFile(latest.file_id, latest.name);
  } else {
    loadDemo("demo_academic_2col", "academic_2col");
  }
}

function renderUploadedPills(uploadedList) {
  const wrap = document.getElementById("uploadedFilesWrap");
  const container = document.getElementById("uploadedFilesPills");
  container.innerHTML = "";
  if (!uploadedList || uploadedList.length === 0) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "flex";
  uploadedList.forEach(item => {
    const group = document.createElement("span");
    group.style.display = "inline-flex";
    group.style.alignItems = "center";
    group.style.gap = "2px";
    const btn = document.createElement("button");
    btn.className = "pill" + (item.file_id === currentFileId ? " active" : "");
    btn.id = "pill-" + item.file_id;
    btn.innerText = "📘 " + item.name;
    btn.onclick = () => switchToUploadedFile(item.file_id, item.name);
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "pill";
    delBtn.style.padding = "4px 6px";
    delBtn.style.color = "#fca5a5";
    delBtn.innerText = "✕";
    delBtn.title = "从上传记录中移除此 PDF";
    delBtn.onclick = (e) => {
      e.stopPropagation();
      deleteUploadedFile(item.file_id);
    };
    group.appendChild(btn);
    group.appendChild(delBtn);
    container.appendChild(group);
  });
}

async function deleteUploadedFile(fileId) {
  try {
    const res = await fetch("/api/delete_file", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify({ file_id: fileId })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "无法移除文件");
    delete pageOverridesByFile[fileId];
    renderUploadedPills(data.uploaded || []);
    if (currentFileId === fileId) {
      if (data.uploaded && data.uploaded.length > 0) {
        await switchToUploadedFile(data.uploaded[0].file_id, data.uploaded[0].name);
      } else {
        await loadDemo("demo_academic_2col", "academic_2col");
      }
    }
  } catch (err) {
    alert("移除文件失败：" + (err.message || err));
  }
}

function highlightActiveFilePill() {
  document.querySelectorAll(".demo-pills .pill").forEach(el => {
    el.classList.toggle("active", el.id === "pill-" + currentFileId);
  });
}

async function switchToUploadedFile(fileId, fileName) {
  const selectionId = ++fileSelectionSerial;
  ++previewRequestSerial;
  currentFileId = fileId;
  currentPage = 0;
  currentScreenIdx = 0;
  document.getElementById("optStartPage").value = 1;
  document.getElementById("optEndPage").value = "";
  document.getElementById("currentFileName").innerText = "📘 当前文档：" + fileName;
  highlightActiveFilePill();

  const res = await fetch("/api/doc_info?file_id=" + encodeURIComponent(fileId));
  const info = await res.json();
  if (selectionId !== fileSelectionSerial || fileId !== currentFileId) return;
  if (info.detected) {
    const banner = document.getElementById("detectBanner");
    banner.style.display = "block";
    banner.innerText = "🤖 智能识别：" + info.detected.reason;
    selectMode(info.detected.recommended_mode, true);
  }
  renderHighlightsBar(info.highlights || []);
  await refreshPreview();
}

async function loadDemo(fileId, mode) {
  const selectionId = ++fileSelectionSerial;
  ++previewRequestSerial;
  currentFileId = fileId;
  currentPage = 0;
  currentScreenIdx = 0;
  document.getElementById("optStartPage").value = 1;
  document.getElementById("optEndPage").value = "";
  document.getElementById("detectBanner").style.display = "none";
  const names = {
    "demo_academic_2col": "📄 内置样张：demo_双栏学术论文.pdf",
    "demo_tech_book": "📊 内置样张：demo_带图表专业书.pdf",
    "demo_scanned_book": "🔍 内置样张：demo_泛黄扫描版书籍.pdf",
    "demo_comic_manga": "🎨 内置样张：demo_PDF漫画双页跨页.pdf",
    "demo_pure_text": "📖 内置样张：demo_纯文字小说书籍.pdf"
  };
  document.getElementById("currentFileName").innerText = names[fileId] || fileId;
  highlightActiveFilePill();
  const res = await fetch("/api/doc_info?file_id=" + encodeURIComponent(fileId));
  const info = await res.json();
  if (selectionId !== fileSelectionSerial || fileId !== currentFileId) return;
  renderHighlightsBar(info.highlights || []);
  selectMode(mode);
}

async function handleFileUpload(file) {
  if (!file) return;
  const selectionId = ++fileSelectionSerial;
  // Keep the current document's pending preview valid until a new PDF is
  // accepted. Successful uploads change file_id, which rejects older images.
  const previousName = document.getElementById("currentFileName").innerText;
  document.getElementById("currentFileName").innerText = "⏳ 正在上传并建立图表索引：" + file.name + " ...";
  const form = new FormData();
  form.append("file", file);
  let data;
  try {
    const res = await fetch("/api/upload", {
      method: "POST",
      headers: { "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: form
    });
    data = await res.json();
    if (selectionId !== fileSelectionSerial) return;
    if (!res.ok || data.error) throw new Error(data.error || "无法上传 PDF");
  } catch (err) {
    if (selectionId !== fileSelectionSerial) return;
    document.getElementById("currentFileName").innerText = previousName;
    document.getElementById("loadingBadge").style.display = "none";
    alert("上传失败：" + (err.message || err));
    return;
  }
  if (data.file_id) {
    currentFileId = data.file_id;
    currentPage = 0;
    currentScreenIdx = 0;
    document.getElementById("optStartPage").value = 1;
    document.getElementById("optEndPage").value = "";
    document.getElementById("currentFileName").innerText = "📘 当前文档：" + data.filename;
    if (data.uploaded) {
      renderUploadedPills(data.uploaded);
    }
    highlightActiveFilePill();
    renderHighlightsBar(data.highlights || []);
    if (data.detected) {
      const banner = document.getElementById("detectBanner");
      banner.style.display = "block";
      banner.innerText = "🤖 智能识别：" + data.detected.reason;
      selectMode(data.detected.recommended_mode);
    } else {
      refreshPreview();
    }
  }
}

function renderHighlightsBar(highlights) {
  currentHighlights = highlights || [];
  const bar = document.getElementById("highlightsBar");
  const list = document.getElementById("highlightsList");
  list.innerHTML = "";
  if (!currentHighlights.length) {
    bar.style.display = "none";
    return;
  }
  bar.style.display = "flex";
  currentHighlights.forEach(h => {
    const btn = document.createElement("button");
    const isTbl = h.kind === "table";
    btn.className = "pill " + (isTbl ? "pill-table" : "pill-figure");
    btn.dataset.pageStart = h.page_num;
    btn.dataset.pageEnd = h.end_page;
    btn.innerText = (isTbl ? "📊 " : "📈 ") + h.label;
    btn.onclick = () => jumpToPage(h.page_num);
    list.appendChild(btn);
  });
  updateHighlightPillsActiveState();
}

function updateHighlightPillsActiveState() {
  document.querySelectorAll("#highlightsList .pill").forEach(btn => {
    const s = parseInt(btn.dataset.pageStart);
    const e = parseInt(btn.dataset.pageEnd);
    btn.classList.toggle("active", currentPage >= s && currentPage <= e);
  });
}

function setDpiPreset(dpi) {
  currentDpiPreset = dpi;
  [300, 450, 600].forEach(d => {
    document.getElementById("dpi-" + d).classList.toggle("active", d === dpi);
  });
  const labels = {
    300: "300 PPI (标准轻巧)",
    450: "450 PPI (1.5x 超清)",
    600: "600 PPI (2.0x 超采样)"
  };
  document.getElementById("valDpiPreset").innerText = labels[dpi] || (dpi + " PPI");
  updateQualityBadgeText();
  refreshPreview(currentScreenIdx);
}

function setStrokeBoldness(level) {
  currentStrokeBoldness = level;
  [0, 1, 2, 3].forEach(lv => {
    document.getElementById("bold-" + lv).classList.toggle("active-emerald", lv === level);
  });
  const labels = {
    0: "0: 原细度",
    1: "+1: 微加粗 (修断笔)",
    2: "+2: 中度黑体化",
    3: "+3: 强力加粗 (细图纸)"
  };
  document.getElementById("valStrokeBold").innerText = labels[level] || ("+" + level);
  updateQualityBadgeText();
  refreshPreview(currentScreenIdx);
}

function updateQualityBadgeText() {
  const vec = document.getElementById("optVectorLossless").checked;
  const enh = document.getElementById("optEnhance").checked;
  const rmWm = document.getElementById("optRemoveWatermark").checked;
  const deskew = document.getElementById("optAutoDeskew").checked;
  const boldText = currentStrokeBoldness > 0 ? ` · 加粗+${currentStrokeBoldness}` : "";
  const extraTags = [rmWm ? "去水印" : "", deskew ? "自动纠偏" : ""].filter(Boolean).join("·");
  let modeDesc = `${currentDpiPreset} PPI`;
  if (currentMode === "scanned_book" || currentMode === "comic_manga" || enh || currentStrokeBoldness > 0 || rmWm || deskew || !vec) {
    modeDesc += ` 超清精修${boldText}${extraTags ? "·" + extraTags : ""}`;
  } else {
    modeDesc += ` · 矢量优先`;
  }
  const el = document.getElementById("statQualityMode");
  if (el) el.innerText = modeDesc;
}

function setCoverMode(mode) {
  currentCoverMode = mode;
  ["smart_card", "first_page", "custom_image", "none"].forEach(m => {
    document.getElementById("cov-" + m).classList.toggle("active", m === mode);
  });
  const labels = {
    "smart_card": "精装书封卡",
    "first_page": "原书首页全幅",
    "custom_image": "自定义封面图",
    "none": "不加独立封面"
  };
  document.getElementById("valCoverMode").innerText = labels[mode] || mode;
  document.getElementById("coverTextInputs").style.display = (mode === "smart_card") ? "flex" : "none";
  document.getElementById("coverUploadWrap").style.display = (mode === "custom_image") ? "block" : "none";
  if (currentPage !== 0 && mode !== "none") {
    jumpToPage(0);
  } else {
    refreshPreview(0);
  }
}

async function handleCoverUpload(file) {
  if (!file) return;
  const uploadId = ++coverUploadSerial;
  const statusEl = document.getElementById("coverUploadStatus");
  statusEl.innerText = "⏳ 正在上传封面图...";
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch("/api/upload_cover", {
      method: "POST",
      headers: { "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: form
    });
    const data = await res.json();
    if (uploadId !== coverUploadSerial) return;
    if (!res.ok || data.error || !data.cover_id) {
      throw new Error(data.error || "无法读取封面图片，请选择完整的图片文件。");
    }
    currentCustomCoverId = data.cover_id;
    statusEl.innerText = "✅ 已加载自定义封面：" + data.filename;
    if (currentPage !== 0) jumpToPage(0);
    else refreshPreview(0);
  } catch (err) {
    if (uploadId !== coverUploadSerial) return;
    statusEl.innerText = "❌ 上传失败：" + (err.message || "请重试");
  } finally {
    if (uploadId === coverUploadSerial) document.getElementById("coverFileInput").value = "";
  }
}

function getOptionsPayload() {
  updateQualityBadgeText();
  const rawStart = parseInt(document.getElementById("optStartPage").value || "1");
  const rawEndStr = (document.getElementById("optEndPage").value || "").trim();
  const rawEnd = rawEndStr ? parseInt(rawEndStr) : null;
  return {
    file_id: currentFileId,
    page_num: currentPage,
    mode: currentMode,
    split_pages: document.getElementById("optSplitPages").checked,
    landscape_2x: document.getElementById("optLandscape").checked,
    trim_header_footer: document.getElementById("optTrimHeader").checked,
    enhance_contrast: document.getElementById("optEnhance").checked,
    clean_spine_shadow: document.getElementById("optCleanSpine").checked,
    remove_watermark: document.getElementById("optRemoveWatermark").checked,
    auto_deskew: document.getElementById("optAutoDeskew").checked,
    gamma_strength: parseFloat(document.getElementById("optGamma").value),
    margin_Percent: parseFloat(document.getElementById("optMargin").value),
    comic_split_spread: document.getElementById("optComicSplit").checked,
    manga_rtl: document.getElementById("optMangaRtl").checked,
    comic_full_bleed: document.getElementById("optComicBleed").checked,
    reflow_font_size: parseFloat(document.getElementById("optFontSize").value),
    line_spacing: parseFloat(document.getElementById("optLineSpacing").value),
    dpi_preset: currentDpiPreset,
    vector_lossless: document.getElementById("optVectorLossless").checked,
    stroke_boldness: currentStrokeBoldness,
    sharpen_strength: parseFloat(document.getElementById("optSharpen").value),
    cover_mode: currentCoverMode,
    custom_cover_title: document.getElementById("optCoverTitle").value || "",
    custom_cover_author: document.getElementById("optCoverAuthor").value || "",
    custom_cover_id: currentCustomCoverId,
    start_page: isNaN(rawStart) ? 1 : rawStart,
    end_page: (rawEnd !== null && !isNaN(rawEnd)) ? rawEnd : null,
    split_volumes_every: currentSplitVolumesEvery,
    page_overrides: getCurrentFileOverrides(),
  };
}

function clearPreviewAfterError(message) {
  currentScreens = [];
  currentSlicesMeta = [];
  currentScreenIdx = 0;
  currentHasCoverScreen = false;
  document.getElementById("sourcePreviewImg").src = "";
  document.getElementById("singleKpw6Img").src = "";
  document.getElementById("sliceHotspotsLayer").innerHTML = "";
  document.getElementById("screenSelectorPills").innerHTML = "";
  document.getElementById("kpw6ScreensContainer").innerHTML = "";
  document.getElementById("singleKpw6Bezel").style.display = "none";
  document.getElementById("skipPageNotice").style.display = "none";
  ["statSlices", "statScreens", "statZoom"].forEach(id => {
    document.getElementById(id).innerText = "-";
  });
  const notice = document.getElementById("previewErrorNotice");
  notice.innerText = "预览失败：" + message;
  notice.style.display = "block";
}

async function refreshPreview(targetScreenIdx = 0) {
  const requestId = ++previewRequestSerial;
  const requestedFileId = currentFileId;
  const badge = document.getElementById("loadingBadge");
  badge.style.display = "block";
  badge.innerText = `⏳ 正在渲染第 ${currentPage + 1} 页预览...`;
  document.getElementById("previewErrorNotice").style.display = "none";

  try {
    const payload = getOptionsPayload();
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (requestId !== previewRequestSerial || payload.file_id !== currentFileId) return;
    if (!res.ok || data.error) throw new Error(data.error || "无法生成预览，请重试。");

    currentPage = data.page_num;
    totalPages = data.total_pages;
    currentScreens = data.kpw6_pages || [];
    currentSlicesMeta = data.slices_meta || [];
    currentHasCoverScreen = !!data.has_cover_screen;

    if (data.detected_title) {
      document.getElementById("optCoverTitle").placeholder = "封面标题：" + data.detected_title.slice(0, 36);
    }
    if (data.detected_author) {
      document.getElementById("optCoverAuthor").placeholder = "副标题：" + data.detected_author.slice(0, 36);
    }

    document.getElementById("pageInputBox").value = currentPage + 1;
    document.getElementById("pageInputBox").max = totalPages;
    document.getElementById("totalPagesNum").innerText = totalPages;
    document.getElementById("optEndPage").placeholder = `末页(${totalPages})`;

    const slider = document.getElementById("pageSlider");
    slider.max = totalPages;
    slider.value = currentPage + 1;

    document.getElementById("statSlices").innerText = data.slice_count;
    document.getElementById("statScreens").innerText = data.kpw6_screens_count;
    document.getElementById("statZoom").innerText = data.magnification + "x";
    document.getElementById("sourcePreviewImg").src = data.source_annotated;

    renderPageOverrideToolbar();
    updateHighlightPillsActiveState();
    renderSliceHotspots();

    const isSkipped = (data.page_override === "skip");
    const hasRenderableScreens = currentScreens.length > 0;
    document.getElementById("skipPageNotice").style.display = isSkipped ? "block" : "none";
    document.getElementById("singleKpw6Bezel").style.display = hasRenderableScreens ? "flex" : "none";
    if (isSkipped) {
      document.getElementById("skipPageNumText").innerText = currentPage + 1;
    }
    if (!hasRenderableScreens) {
      document.getElementById("screenSelectorPills").innerHTML = "";
    }

    // Render Grid View
    const container = document.getElementById("kpw6ScreensContainer");
    container.innerHTML = "";
    if (!hasRenderableScreens) {
      container.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:#fca5a5;padding:36px 20px;background:rgba(239,68,68,0.12);border:2px dashed #ef4444;border-radius:14px;">🚫 <strong>第 ${currentPage + 1} 页已标记为【跳过此页】</strong><div style="font-size:12px;color:#94a3b8;margin-top:6px;">导出电子书或 PDF 时将自动跳过本页</div></div>`;
    }
    currentScreens.forEach((b64, i) => {
      const div = document.createElement("div");
      div.className = "kpw6-frame";
      div.id = "grid-frame-" + i;
      div.innerHTML = `
        <div class="screen-label">
          <strong>KPW6 第 ${i + 1} / ${currentScreens.length} 屏</strong>
          <span style="font-size:11px;color:#38bdf8;cursor:pointer;" onclick="openLightbox(currentScreens[${i}], '第 ${currentPage+1} 页 · KPW6 第 ${i+1} 屏', 'kpw6', ${i})">🔍 高清放大</span>
        </div>
        <img src="${b64}" alt="KPW6 Screen ${i+1}" onclick="openLightbox(currentScreens[${i}], '第 ${currentPage+1} 页 · KPW6 第 ${i+1} 屏', 'kpw6', ${i})">
      `;
      container.appendChild(div);
    });

    // Render Single 1:1 View
    if (hasRenderableScreens) {
      const safeIdx = (targetScreenIdx === -1)
        ? Math.max(0, currentScreens.length - 1)
        : Math.max(0, Math.min(currentScreens.length - 1, targetScreenIdx));
      selectKpw6Screen(safeIdx, false);
    }
  } catch (err) {
    if (requestId !== previewRequestSerial || requestedFileId !== currentFileId) return;
    clearPreviewAfterError(err.message || "无法生成预览，请重试。");
  } finally {
    if (requestId === previewRequestSerial) badge.style.display = "none";
  }
}

function renderSliceHotspots() {
  const layer = document.getElementById("sliceHotspotsLayer");
  layer.innerHTML = "";
  currentSlicesMeta.forEach(s => {
    const spot = document.createElement("div");
    spot.className = "slice-hotspot";
    spot.dataset.outPageIdx = s.out_page_idx;
    spot.style.left = s.left_pct + "%";
    spot.style.top = s.top_pct + "%";
    spot.style.width = s.width_pct + "%";
    spot.style.height = s.height_pct + "%";
    spot.title = `点击查看区块 #${s.index} 对应的【KPW6 第 ${s.out_page_idx + 1} 屏】`;
    spot.onclick = (e) => {
      e.stopPropagation();
      selectKpw6Screen(s.out_page_idx, true);
    };
    layer.appendChild(spot);
  });
}

function selectKpw6Screen(idx, flash = false) {
  if (!currentScreens.length) return;
  currentScreenIdx = Math.max(0, Math.min(currentScreens.length - 1, idx));
  document.getElementById("singleKpw6Img").src = currentScreens[currentScreenIdx];

  const pillsWrap = document.getElementById("screenSelectorPills");
  pillsWrap.innerHTML = "";
  currentScreens.forEach((_, i) => {
    const b = document.createElement("button");
    b.className = "screen-pill" + (i === currentScreenIdx ? " active" : "");
    b.innerText = (currentHasCoverScreen && i === 0) ? `🖼️ 第 1 屏 (专属封面)` : `第 ${i + 1} 屏`;
    b.onclick = () => selectKpw6Screen(i, false);
    pillsWrap.appendChild(b);
  });

  // Highlight matching hotspots on the left Source PDF
  document.querySelectorAll(".slice-hotspot").forEach(el => {
    el.classList.toggle("active-slice", parseInt(el.dataset.outPageIdx) === currentScreenIdx);
  });

  // Highlight matching card in Grid View
  document.querySelectorAll(".kpw6-frame").forEach((el, i) => {
    el.classList.toggle("active-frame", i === currentScreenIdx);
  });

  if (flash) {
    if (rightViewMode === "single") {
      const bezel = document.getElementById("singleKpw6Bezel");
      bezel.classList.add("flash-highlight");
      setTimeout(() => bezel.classList.remove("flash-highlight"), 500);
    } else {
      const card = document.getElementById("grid-frame-" + currentScreenIdx);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

function stepKpw6Screen(delta) {
  const nextScreen = currentScreenIdx + delta;
  if (nextScreen >= 0 && nextScreen < currentScreens.length) {
    selectKpw6Screen(nextScreen, false);
  } else if (nextScreen >= currentScreens.length && currentPage + 1 < totalPages) {
    // Auto-advance to next PDF page's Screen 1!
    currentPage += 1;
    refreshPreview(0);
  } else if (nextScreen < 0 && currentPage > 0) {
    // Auto-step back to previous PDF page's last screen!
    currentPage -= 1;
    refreshPreview(-1);
  }
}

function jumpToPage(targetPage0) {
  const clamped = Math.max(0, Math.min(totalPages - 1, targetPage0));
  if (isNaN(clamped)) return;
  currentPage = clamped;
  refreshPreview(0);
}

function changePage(delta) {
  const next = Math.max(0, Math.min(totalPages - 1, currentPage + delta));
  if (next !== currentPage) {
    currentPage = next;
    refreshPreview(0);
  }
}

function zoomCurrentKpw6Screen() {
  if (!currentScreens.length) return;
  openLightbox(
    currentScreens[currentScreenIdx],
    `第 ${currentPage + 1} 页 · KPW6 第 ${currentScreenIdx + 1} / ${currentScreens.length} 屏`,
    "kpw6",
    currentScreenIdx
  );
}

async function openLightbox(src, title, targetType = "kpw6", screenIdx = 0) {
  if (!src) return;
  const requestId = ++lightboxRequestSerial;
  const imgEl = document.getElementById("lightboxImg");
  const badgeEl = document.getElementById("lightboxHdBadge");
  imgEl.src = src;
  document.getElementById("lightboxTitle").innerText = "🔍 " + (title || "高清细节放大镜");
  badgeEl.innerText = "⏳ 正在加载高清图层...";
  badgeEl.style.background = "rgba(59,130,246,0.2)";
  badgeEl.style.color = "#93c5fd";
  badgeEl.style.borderColor = "#3b82f6";
  document.getElementById("lightboxModal").style.display = "flex";

  try {
    const payload = {
      ...getOptionsPayload(),
      target_type: targetType,
      screen_idx: screenIdx,
    };
    const res = await fetch("/api/preview_hd", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (requestId !== lightboxRequestSerial || document.getElementById("lightboxModal").style.display !== "flex") return;
    if (!res.ok || data.error) throw new Error(data.error || "高清图层暂不可用");
    if (data.hd_image) {
      imgEl.src = data.hd_image;
      badgeEl.innerText = `✨ 已加载高清图层 (${data.width}×${data.height} px)`;
      badgeEl.style.background = "rgba(16,185,129,0.2)";
      badgeEl.style.color = "#6ee7b7";
      badgeEl.style.borderColor = "#10b981";
    }
  } catch (err) {
    if (requestId !== lightboxRequestSerial || document.getElementById("lightboxModal").style.display !== "flex") return;
    badgeEl.innerText = "高清图层暂不可用，当前显示预览图";
  }
}

function closeLightbox() {
  ++lightboxRequestSerial;
  document.getElementById("lightboxModal").style.display = "none";
}

function setLightboxZoom(pxWidth) {
  document.getElementById("lightboxImg").style.width = pxWidth + "px";
}

const FORMAT_META = {
  "epub_layout": {
    short: ".EPUB (版面精排+封面)",
    btn: "🚀 一键导出【.EPUB 满屏精排版】(推荐传 Send to Kindle)",
    hint: "✨ <strong>图像版 EPUB：</strong>保留生成的分屏图像，内置封面与图表目录。可尝试通过 Send to Kindle 传书；设备上的版式与封面显示请实际确认。"
  },
  "pdf": {
    short: ".PDF (矢量优先版)",
    btn: "🚀 一键导出【.PDF 重排版】(封面由当前设置决定)",
    hint: "📄 <strong>PDF 格式：</strong>在适用页面上保留矢量文字与公式，启用图像增强时可能栅格化。文件大小取决于输入与处理选项，独立封面由当前设置决定。"
  },
  "azw3": {
    short: ".AZW3 (Kindle KF8 原生)",
    btn: "🚀 一键导出【.AZW3 Kindle 原生格式】(USB 数据线直传首选)",
    hint: "🔌 <strong>USB 数据线直传首选：</strong>自动调用本机 Calibre 引擎编译为 Kindle 原生 KF8 (<code>.azw3</code>) 格式，内置封面与目录，适合超大型扫描书免云端限制直拷 <code>documents/</code> 目录。"
  },
  "epub_reflow": {
    short: ".EPUB (流式可调字号)",
    btn: "🚀 一键导出【.EPUB 流式可调字号版】(纯文字小说/长文推荐)",
    hint: "📖 <strong>纯文字重排电子书：</strong>提取正文、合并断句并生成章节目录。流式文字可在支持的阅读器中调整字号，字体、词典和封面显示取决于阅读器。"
  },
  "mobi": {
    short: ".MOBI (经典 Kindle 格式)",
    btn: "🚀 一键导出【.MOBI 经典 Kindle 格式】(含封面与目录)",
    hint: "📚 <strong>经典 .MOBI 格式：</strong>调用本机 Calibre 引擎打包为经典 MOBI 电子书格式，兼容旧款 Kindle 与各类本地阅读器管理软件。"
  },
  "cbz": {
    short: ".CBZ (1264×1680 PNG 漫画包)",
    btn: "🚀 一键导出【.CBZ 1264×1680 PNG 漫画/图集包】",
    hint: "🎨 <strong>标准漫画归档包 (.cbz)：</strong>将生成的 1264×1680 封面与所有重排屏幕以无损 PNG 封装并附带 <code>ComicInfo.xml</code>，适合漫画阅读器或 KCC 二次处理。"
  }
};

function setExportFormat(fmt, isAuto = false) {
  if (!isAuto) userManuallyChoseFormat = true;
  currentExportFormat = fmt;
  ["epub_layout", "pdf", "azw3", "epub_reflow", "mobi", "cbz"].forEach(k => {
    const el = document.getElementById("fmt-" + k);
    if (el) el.classList.toggle("active-emerald", k === fmt);
  });
  const m = FORMAT_META[fmt] || FORMAT_META["epub_layout"];
  document.getElementById("valExportFormat").innerText = m.short;
  document.getElementById("mainExportBtn").innerText = m.btn;
  document.getElementById("formatHintText").innerHTML = m.hint;
}

async function exportSelectedFormat(overrideFormat = null) {
  const fmt = overrideFormat || currentExportFormat;
  const isBundle = (fmt === "bundle_both");
  const btn = document.getElementById(isBundle ? "bundleExportBtn" : "mainExportBtn");
  const oldText = btn.innerText;
  btn.innerText = `⏳ 正在为 KPW6 生成 ${fmt.toUpperCase()} 文件（请查看上方进度条）...`;
  btn.disabled = true;

  const jobId = "job_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
  activeExportJobId = jobId;
  const progPanel = document.getElementById("exportProgressPanel");
  const progTitle = document.getElementById("exportProgressTitle");
  const progEta = document.getElementById("exportProgressEta");
  const progPct = document.getElementById("exportProgressPct");
  const progFill = document.getElementById("exportProgressBarFill");
  const cancelBtn = document.getElementById("cancelExportBtn");
  if (cancelBtn) cancelBtn.disabled = false;

  progPanel.style.display = "flex";
  progTitle.innerText = "正在启动 KPW6 排版与封面引擎...";
  progEta.innerText = "已用时 0.0s · 正在计算预计耗时...";
  progPct.innerText = "1%";
  progFill.style.width = "2%";

  const pollTimer = setInterval(async () => {
    try {
      const r = await fetch("/api/export_progress?job_id=" + encodeURIComponent(jobId));
      const p = await r.json();
      if (p && p.status) {
        const pct = Math.max(1, Math.min(100, p.percent || 1));
        progTitle.innerText = p.stage_text || "正在精排页面...";
        progPct.innerText = pct + "%";
        progFill.style.width = pct + "%";
        const elapsed = (p.elapsed_sec || 0).toFixed(1);
        const etaStr = (p.eta_sec !== null && p.eta_sec !== undefined && p.eta_sec > 0.2)
          ? `预计剩余 ~${p.eta_sec.toFixed(1)}s`
          : "即将完成";
        progEta.innerText = `已用时 ${elapsed}s · ${etaStr}`;
      }
    } catch (e) {}
  }, 260);

  try {
    const payload = {
      ...getOptionsPayload(),
      export_format: fmt,
      job_id: jobId,
    };
    const res = await fetch("/api/export_unified", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    clearInterval(pollTimer);
    if (data.cancelled) {
      progPanel.style.display = "none";
      return;
    }
    if (data.error) {
      progPanel.style.display = "none";
      alert("导出失败：" + data.error);
      return;
    }

    progFill.style.width = "100%";
    progPct.innerText = "100%";
    progTitle.innerText = "✅ 导出完成！";
    setTimeout(() => { progPanel.style.display = "none"; }, 1800);

    const toast = document.getElementById("exportToast");
    toast.style.display = "flex";
    document.getElementById("toastText").innerText = data.message || `✅ 已生成：${data.output_filename}`;

    const dl1 = document.getElementById("downloadLink");
    const dl2 = document.getElementById("downloadSecondLink");
    const volWrap = document.getElementById("volumeDownloadsContainer");
    volWrap.innerHTML = "";

    if (data.volumes && data.volumes.length > 1) {
      dl1.style.display = "none";
      dl2.style.display = "none";
      volWrap.style.display = "flex";
      data.volumes.forEach((v, idx) => {
        const a = document.createElement("a");
        a.className = idx === 0 ? "btn btn-emerald" : "btn btn-primary";
        a.href = v.download_url;
        a.setAttribute("download", "");
        a.innerText = v.download_label || `💾 下载第 ${idx + 1} 卷`;
        volWrap.appendChild(a);
        if (v.second_download_url) {
          const a2 = document.createElement("a");
          a2.className = "btn btn-purple";
          a2.href = v.second_download_url;
          a2.setAttribute("download", "");
          a2.innerText = v.second_download_label || `💾 下载第 ${idx + 1} 卷配套 .PDF`;
          volWrap.appendChild(a2);
        }
      });
    } else {
      volWrap.style.display = "none";
      dl1.style.display = "inline-flex";
      dl1.href = data.download_url;
      dl1.innerText = data.download_label || "💾 下载文件到本地";

      if (data.second_download_url) {
        dl2.href = data.second_download_url;
        dl2.innerText = data.second_download_label || "💾 下载配套 EPUB";
        dl2.style.display = "inline-flex";
      } else {
        dl2.style.display = "none";
      }
    }

    const viewBtn = document.getElementById("viewPdfOnlineLink");
    if (data.view_url) {
      viewBtn.href = data.view_url;
      viewBtn.style.display = "inline-flex";
    } else {
      viewBtn.style.display = "none";
    }
  } catch (err) {
    clearInterval(pollTimer);
    progPanel.style.display = "none";
    alert("导出异常：" + (err && err.message ? err.message : err));
  } finally {
    if (activeExportJobId === jobId) activeExportJobId = "";
    clearInterval(pollTimer);
    btn.innerText = oldText;
    btn.disabled = false;
  }
}

async function cancelActiveExport() {
  if (!activeExportJobId) return;
  const cancelBtn = document.getElementById("cancelExportBtn");
  if (cancelBtn) cancelBtn.disabled = true;
  document.getElementById("exportProgressTitle").innerText = "⏹ 正在中止导出任务...";
  try {
    await fetch("/api/cancel_export", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify({ job_id: activeExportJobId })
    });
  } catch (e) {}
}

async function openOutputFolder() {
  await fetch("/api/open_folder", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
    body: "{}"
  });
}

window.addEventListener("DOMContentLoaded", () => {
  fetchUploadedFilesAndInit();
});
</script>
</body>
</html>
"""


def _set_export_job(job_id: str, info: dict) -> None:
    EXPORT_JOBS[job_id] = info
    EXPORT_JOBS.move_to_end(job_id)
    if len(EXPORT_JOBS) > MAX_EXPORT_JOBS:
        for old_id in list(EXPORT_JOBS.keys()):
            if len(EXPORT_JOBS) <= MAX_EXPORT_JOBS:
                break
            if EXPORT_JOBS[old_id].get("status") != "running":
                EXPORT_JOBS.pop(old_id, None)


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


def _parse_options(data: dict) -> ProcessOptions:
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
    cov_path = CUSTOM_COVERS.get(cov_id) if cov_id else None
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


@app.before_request
def _validate_local_and_json_request():
    req_host = urllib.parse.urlsplit("//" + (request.host or ""))
    if (req_host.hostname or "").lower() not in _LOOPBACK_HOSTS:
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
            if (orig.hostname or "").lower() not in _LOOPBACK_HOSTS or (
                req_host.port is not None and orig.port != req_host.port
            ):
                return jsonify({"error": "拒绝跨源请求访问本地服务。"}), 403
        else:
            referer = (request.headers.get("Referer") or "").strip()
            if referer:
                ref = urllib.parse.urlsplit(referer)
                if (ref.hostname or "").lower() not in _LOOPBACK_HOSTS or (
                    req_host.port is not None and ref.port != req_host.port
                ):
                    return jsonify({"error": "拒绝跨源来源访问本地服务。"}), 403

        if request.method in {"POST", "DELETE"} and (origin or sec_site):
            token = request.headers.get("X-KPW6-Token") or ""
            if not secrets.compare_digest(token, APP_CSRF_TOKEN):
                return jsonify({"error": "本地会话安全令牌无效或已过期，请刷新页面后重试。"}), 403

    if request.is_json and not isinstance(request.get_json(), dict):
        return jsonify({"error": "请求参数必须是 JSON 对象。"}), 400


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
    return Response(HTML_PAGE.replace("__KPW6_CSRF_TOKEN__", APP_CSRF_TOKEN), mimetype="text/html; charset=utf-8")


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


if __name__ == "__main__":
    def _exit_signal(signum, frame):
        _shutdown_owned_processes()
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, _exit_signal)
    signal.signal(signal.SIGINT, _exit_signal)
    parser = argparse.ArgumentParser(description="KPW6 PDF Studio")
    parser.add_argument("--port", type=int, default=int(os.environ.get("KPW6_PORT", "8765")))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port-file", default=os.environ.get("KPW6_PORT_FILE", ""))
    args, _ = parser.parse_known_args()

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

    print(f"🚀 KPW6 PDF Studio (五合一全能版) 已启动: {url} (导出目录: {OUTPUT_DIR})", flush=True)
    with server:
        server.serve_forever()
