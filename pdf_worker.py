"""Private JSONL worker: all native PDF calls run on this process's main thread.

SPDX-License-Identifier: AGPL-3.0-only
"""
import contextlib
import json
import os
import queue
import signal
import sys
import threading
import warnings

from conversion_runner import cancel_all_conversions

MAX_COVER_PIXELS = 25_000_000


def _validate_pdf(path):
    import pymupdf as fitz
    try:
        with fitz.open(path) as doc:
            if not doc.is_pdf or doc.needs_pass or doc.page_count == 0:
                raise ValueError('请选择有效、未加密且至少包含一页的 PDF。')
    except Exception as exc:
        raise ValueError('无法读取 PDF，请确认文件完整、未加密且至少包含一页。') from exc
    return {'valid': True}


def _validate_cover(path):
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_COVER_PIXELS:
                    raise ValueError('封面像素过大，请缩小至2500万像素以内。')
                image.verify()
            with Image.open(path) as image:
                image.load()
                return {'width': image.width, 'height': image.height, 'format': image.format}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('无法读取封面图片，请选择完整有效的 PNG、JPEG 等图像。') from exc


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {'__process_options__'}:
            from pdf_engine import ProcessOptions
            return ProcessOptions(**value['__process_options__'])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _operation(name, args, kwargs):
    import pdf_engine
    if name == 'validate_pdf':
        return _validate_pdf(*args, **kwargs)
    if name == 'validate_cover':
        return _validate_cover(*args, **kwargs)
    if name == 'generate_demo_pdfs':
        from sample_generator import generate_demo_pdfs
        return generate_demo_pdfs(*args, **kwargs)
    if name == 'inspect_pdf':
        path, options = args
        with pdf_engine._open_source_pdf(path) as document:
            title, author = pdf_engine.extract_book_meta(document, path, options)
            return {'total_pages': len(document), 'title': title, 'author': author}
    allowed = {'detect_pdf_category', 'scan_document_highlights', 'render_preview_comparison',
               'render_hd_zoom_image', 'build_kpw6_pdf', 'build_kpw6_epub',
               'build_kpw6_cbz', 'convert_epub_via_calibre'}
    if name not in allowed:
        raise ValueError('Unknown private PDF operation')
    return getattr(pdf_engine, name)(*args, **kwargs)


def run_worker():
    # NumPy's Windows native import can deadlock while another thread blocks
    # reading piped stdin (numpy/numpy#24290). Load our native runtime on this
    # main thread before the owner-pipe reader starts. Later RPC imports are
    # cached; diagnostics still stay outside the JSON result stream.
    with contextlib.redirect_stdout(sys.stderr):
        import pdf_engine

    requests = queue.Queue()
    stop_requested = threading.Event()
    output = sys.stdout
    output_lock = threading.Lock()

    def emit(message):
        with output_lock:
            output.write(json.dumps(message, ensure_ascii=True, allow_nan=False) + '\n')
            output.flush()

    def terminate(*_):
        stop_requested.set()

    def cleanup_on_shutdown():
        stop_requested.wait()
        cancel_all_conversions()
        # Includes parent-pipe EOF during native work. No further PDF call is
        # made from this IO/signal handler; the worker cannot outlive its owner.
        os._exit(0)

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, terminate)
    threading.Thread(target=cleanup_on_shutdown, name='owned-child-cleanup', daemon=True).start()

    def read_input():
        for line in sys.stdin:
            requests.put(line)
        terminate()

    threading.Thread(target=read_input, name='owner-pipe', daemon=True).start()
    while True:
        line = requests.get()
        try:
            request = json.loads(line)
            args = _decode(request.get('args', []))
            kwargs = _decode(request.get('kwargs', {}))
            if request.get('progress'):
                kwargs['progress_cb'] = lambda cur, total, message: emit({
                    'type': 'progress', 'current': cur, 'total': total, 'message': message})
            # Python/native library diagnostics belong on stderr, never in the
            # private result stream. No untrusted object deserialization is used.
            with contextlib.redirect_stdout(sys.stderr):
                result = _operation(request['operation'], args, kwargs)
            emit({'type': 'result', 'value': result})
        except Exception as exc:
            emit({'type': 'error', 'kind': 'ValueError' if isinstance(exc, ValueError) else 'RuntimeError',
                  'message': str(exc) or type(exc).__name__})


if __name__ == '__main__':
    run_worker()
