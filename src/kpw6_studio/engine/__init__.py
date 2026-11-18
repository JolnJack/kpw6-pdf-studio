"""Modular PDF analysis, e-ink image enhancement, reflow, and export engine.

SPDX-License-Identifier: AGPL-3.0-only
"""
from kpw6_studio.engine.cover import _insert_cover_page_to_doc, generate_cover_pil_image
from kpw6_studio.engine.document import (
    _HIGHLIGHTS_DOC_CACHE,
    _SOURCE_PDF_CACHE,
    _bake_and_normalize_doc,
    _caption_sort_key,
    _open_source_pdf,
    detect_pdf_category,
    extract_book_meta,
    scan_document_highlights,
)
from kpw6_studio.engine.exporters import (
    _render_slices_to_doc,
    build_kpw6_cbz,
    build_kpw6_epub,
    build_kpw6_pdf,
    convert_epub_via_calibre,
)
from kpw6_studio.engine.image_ops import (
    _deskew_pil_image,
    _insert_enhanced_raster_slice,
    compute_ink_mask,
    enhance_eink_image,
    find_tight_bbox,
    render_page_gray,
    strip_running_headers_footers,
)
from kpw6_studio.engine.layout import (
    _analyze_rotated_sideways_page,
    _append_slice_box,
    _build_table_header_keep_together_mask,
    _compute_protected_full_width_rows,
    _longest_zero_run,
    analyze_page_slices,
    find_safe_cut_in_range,
    segment_comic_spread,
    segment_two_column_zones,
)
from kpw6_studio.engine.models import (
    CHAPTER_REGEX,
    ExtractedParagraph,
    KPW6_ASPECT,
    KPW6_PT_H,
    KPW6_PT_W,
    KPW6_PX_H,
    KPW6_PX_W,
    PAGE_NUM_REGEX,
    ProcessOptions,
    SliceBox,
    TERMINAL_PUNCT,
    _encode_eink_png_bytes,
    _xml_clean,
    _xml_escape,
    get_target_page_list,
)
from kpw6_studio.engine.preview import (
    _has_preview_cover,
    render_hd_zoom_image,
    render_preview_comparison,
)
from kpw6_studio.engine.text_reflow import (
    _MARGIN_EXCLUSIONS_CACHE,
    _is_latin_word_char,
    _join_wrapped_text,
    _render_pure_text_items_to_kpw6_doc,
    _running_margin_exclusions,
    _wrap_cjk_line,
    extract_structured_text_from_page,
)
