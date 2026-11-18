"""Exercise a bundled workbench and its PDF worker with a fresh local session."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request


EXPORT_FORMATS = ('pdf', 'epub_layout', 'cbz')


def request(base, path, payload=None, session_headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json', **(session_headers or {})} if data is not None else {}
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with direct.open(urllib.request.Request(base + path, data=data, headers=headers), timeout=90) as response:
        return response.read()


def export_size(fmt, payload):
    assert payload.startswith(b'%PDF-' if fmt == 'pdf' else b'PK'), f'Invalid {fmt} export'
    return len(payload)


def smoke_http(base):
    homepage = request(base, '/').decode('utf-8')
    token = re.search(r'window\.__KPW6_CSRF_TOKEN__\s*=\s*"([A-Za-z0-9_-]+)"', homepage)
    assert token, 'The bundled homepage did not initialize the browser session token'
    assert b':root' in request(base, '/static/styles.css')
    assert b'refreshPreview' in request(base, '/static/app.js')
    headers = {'Origin': base, 'Sec-Fetch-Site': 'same-origin', 'X-KPW6-Token': token.group(1)}
    options = {'file_id': 'demo_tech_book', 'mode': 'tech_book', 'cover_mode': 'none'}
    preview = json.loads(request(base, '/api/preview', options, headers))
    assert preview['kpw6_screens_count'] > 0
    result = {'preview_screens': preview['kpw6_screens_count']}
    for fmt in EXPORT_FORMATS:
        exported = json.loads(request(base, '/api/export_unified', {**options, 'export_format': fmt}, headers))
        result[fmt + '_bytes'] = export_size(fmt, request(base, exported['download_url']))
    return result


def smoke_browser(base, root, channel):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel)
        try:
            page = browser.new_page(accept_downloads=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def capture_dialog(dialog):
                errors.append(dialog.message)
                dialog.dismiss()

            page.on('dialog', capture_dialog)

            def post(path, action):
                with page.expect_response(base + path, timeout=60_000) as event:
                    action()
                response = event.value
                assert response.ok, f'{path}: HTTP {response.status} {response.text()[:300]}'
                return response.json()

            preview = post('/api/preview', lambda: page.goto(base + '/', wait_until='domcontentloaded'))
            assert preview['kpw6_screens_count'] > 0
            page.wait_for_function("document.getElementById('sourcePreviewImg').naturalWidth > 0")
            result = {'browser': browser.version, 'preview_screens': preview['kpw6_screens_count']}

            sample = root / 'data/samples/demo_带图表专业书.pdf'
            fixture = {'name': 'Session smoke test.pdf', 'mimeType': 'application/pdf', 'buffer': sample.read_bytes()}
            uploaded = post('/api/upload', lambda: page.locator('#fileInput').set_input_files(fixture))
            assert uploaded['file_id']
            expect(page.locator('#currentFileName')).to_contain_text(fixture['name'])
            result['upload_ok'] = True

            for fmt in EXPORT_FORMATS:
                page.locator('#fmt-' + fmt).click()
                exported = post('/api/export_unified', lambda: page.locator('#mainExportBtn').click())
                download_link = page.locator('#downloadLink')
                expect(download_link).to_have_attribute('href', exported['download_url'])
                with page.expect_download() as download:
                    download_link.click()
                result[fmt + '_bytes'] = export_size(fmt, Path(download.value.path()).read_bytes())

            remove = page.get_by_title('从上传记录中移除此 PDF')
            deleted = post('/api/delete_file', remove.click)
            assert deleted['ok'] and not deleted['uploaded']
            expect(remove).to_have_count(0)
            result['remove_ok'] = True
            assert not errors, errors
            return result
        finally:
            browser.close()


def smoke(binary, report=None, *, browser=False, browser_channel=None):
    with tempfile.TemporaryDirectory(prefix='kpw6-frozen-smoke-') as directory:
        root = Path(directory)
        port_file = root / 'port.txt'
        env = {**os.environ, 'KPW6_DATA_DIR': str(root / 'data'), 'KPW6_NO_BROWSER': '1'}
        result = {'binary': str(binary)}
        with (root / 'server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(binary), '--no-browser', '--port', '0', '--port-file', str(port_file)],
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('Frozen backend exited: ' + (root / 'server.log').read_text(errors='replace'))
                    if port_file.exists() and port_file.read_text().strip():
                        base = 'http://127.0.0.1:' + port_file.read_text().strip()
                        try:
                            health = json.loads(request(base, '/api/health'))
                            if health.get('ok'):
                                break
                        except OSError:
                            pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError('Frozen backend health timed out: ' + (root / 'server.log').read_text(errors='replace'))
                assert Path(health['output_dir']).resolve() == (root / 'data' / 'outputs').resolve()
                result['health'] = True
                result.update(smoke_browser(base, root, browser_channel) if browser else smoke_http(base))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        if report:
            Path(report).parent.mkdir(parents=True, exist_ok=True)
            Path(report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--browser', action='store_true', help='Drive the actual workbench with Playwright Chromium')
    parser.add_argument('--browser-channel', help='Use an installed browser such as chrome')
    args = parser.parse_args()
    smoke(args.binary.resolve(), args.report, browser=args.browser or bool(args.browser_channel),
          browser_channel=args.browser_channel)
