"""Exercise a built backend through real HTTP, including its private PDF worker."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request


def request(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'} if data is not None else {}
    with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers), timeout=90) as response:
        return response.read()


def smoke(binary, report=None):
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
                assert b'KPW6' in request(base, '/')
                options = {'file_id': 'demo_tech_book', 'mode': 'tech_book', 'cover_mode': 'none'}
                preview = json.loads(request(base, '/api/preview', options))
                assert preview['kpw6_screens_count'] > 0
                result['preview_screens'] = preview['kpw6_screens_count']
                for fmt in ('pdf', 'epub_layout', 'cbz'):
                    exported = json.loads(request(base, '/api/export_unified', {**options, 'export_format': fmt}))
                    payload = request(base, exported['download_url'])
                    assert payload.startswith(b'%PDF-' if fmt == 'pdf' else b'PK')
                    result[fmt + '_bytes'] = len(payload)
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
    args = parser.parse_args()
    smoke(args.binary.resolve(), args.report)
