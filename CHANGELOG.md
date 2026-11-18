# Changelog

All notable changes to **KPW6 PDF Studio** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/lang/zh-CN/).

## [1.0.1] - 2026-11-18

### Fixed
- Initialized the browser session token correctly so previews, uploads, removals and exports work on a fresh page. The workbench HTML is served with `Cache-Control: no-store` to prevent reusing a token from a previous app session.
- Added a fresh browser test of preview, upload, removal, and PDF, EPUB and CBZ exports against the actual Windows ZIP and both macOS disk images before publication.

## [1.0.0] - 2026-11-18

### Architecture & Packaging
- Adopted the standard PyPA `src/` package layout (`src/kpw6_studio/`) with PEP 621 `pyproject.toml`.
- Decoupled the web studio frontend into standalone static assets (`web/index.html`, `web/styles.css`, `web/app.js`) and modularized the PDF processing engine (`engine/models.py`, `image_ops.py`, `text_reflow.py`, `layout.py`, `document.py`, `cover.py`, `exporters.py`, `preview.py`).
- Consolidated CI and automated tag-driven multi-platform release builds (`.github/workflows/ci.yml` and `.github/workflows/release.yml`).
- Pruned unused `pymupdf/layout` ONNX models and `mupdf-devel` headers from frozen bundles, cutting Windows and Apple Silicon installer sizes by ~48%–52%.
- Standardized LF line endings via `.gitattributes` and unified cross-platform `SOURCE.zip` SHA-256 provenance manifests and wheel license closures.

### Security & Reliability
- Hardened the local loopback web server with `Host` allowlisting (DNS rebinding protection), `Sec-Fetch-Site` / `Origin` / `Referer` validation, and per-session `X-KPW6-Token` CSRF tokens.
- Split interactive preview and heavy export workloads into dedicated worker subprocesses so long exports or Calibre conversions never block page previews.
- Added active export cancellation (`/api/cancel_export`) and uploaded file removal (`/api/delete_file`).
- Fixed macOS status-bar quit race during startup (`isQuitting`) and added transient `hdiutil` `Resource busy` retries during DMG creation.

### Engine & Typography
- Added in-memory `bake()` and rotation-normalization caching keyed by `(abspath, size, blake2b)` for instant multi-page previews on large PDFs.
- Stripped illegal XML 1.0 control characters across all EPUB 3 (`content.opf`, `nav.xhtml`, `toc.ncx`, `.xhtml`) and CBZ (`ComicInfo.xml`) outputs and added EPUB 3 `landmarks` navigation.
- Added chapter bookmark (`set_toc`) generation and CJK font subsetting (`subset_fonts`) for `pure_text` PDF exports, plus whitespace-aware Latin word wrapping in CJK lines.
- Switched fixed-layout EPUB and CBZ screen renders to compact 4-bit (16-level grayscale) Carta 1300 PNG encoding.
