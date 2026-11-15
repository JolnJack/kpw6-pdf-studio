"""Behavioral checks for process isolation and owned-process shutdown."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import urllib.request

import psutil
import pymupdf as fitz

REPO = Path(os.environ.get('KPW6_TEST_REPO', Path(__file__).resolve().parents[1]))


class RequestIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = os.environ.get('KPW6_DATA_DIR')
        os.environ['KPW6_DATA_DIR'] = self.temp.name
        sys.path.insert(0, str(REPO))
        self.app = importlib.import_module('app')
        if old is None:
            os.environ.pop('KPW6_DATA_DIR', None)
        else:
            os.environ['KPW6_DATA_DIR'] = old
        names = ('BASE_DIR', 'UPLOAD_DIR', 'OUTPUT_DIR', 'SAMPLE_DIR', 'FILES', 'HIGHLIGHTS_CACHE', 'CUSTOM_COVERS', 'EXPORT_JOBS')
        previous = {name: getattr(self.app, name) for name in names}
        self.addCleanup(lambda: [setattr(self.app, name, value) for name, value in previous.items()])
        self.app.BASE_DIR = self.temp.name
        for name, dirname in (('UPLOAD_DIR', 'uploads'), ('OUTPUT_DIR', 'outputs'), ('SAMPLE_DIR', 'samples')):
            path = Path(self.temp.name) / dirname
            path.mkdir(exist_ok=True)
            setattr(self.app, name, str(path))
        demos = self.app.generate_demo_pdfs(self.app.SAMPLE_DIR)
        self.app.FILES = {f'demo_{mode}': {'path': path, 'name': Path(path).name, 'is_demo': True, 'mtime': 0} for mode, path in demos.items()}
        self.app.HIGHLIGHTS_CACHE = {}
        self.app.CUSTOM_COVERS = {}
        self.app.EXPORT_JOBS = {}
        self.app.app.config['TESTING'] = False
        self.app.app.logger.disabled = True

    def preview(self):
        with self.app.app.test_client() as client:
            result = client.post('/api/preview', json={'file_id': 'demo_tech_book', 'mode': 'tech_book', 'cover_mode': 'none'})
            return result.status_code

    def test_preview_does_not_call_native_renderer_in_request_process(self):
        # A child interpreter has its own native module, so this patch catches
        # calls in the web process without faking a successful render.
        with patch.object(fitz.Page, 'get_pixmap', side_effect=AssertionError('native rendering in request process')):
            self.assertEqual(self.preview(), 200)

    def test_parallel_requests_use_isolated_renderer(self):
        with patch.object(fitz.Page, 'get_pixmap', side_effect=AssertionError('native rendering in request threads')):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.preview(), range(4)))
        self.assertEqual(results, [200] * 4)

    def test_invalid_options_return_json_400_without_hanging_the_worker(self):
        for options in ({'mode': []}, {'gamma_strength': 'bad'}, {'gamma_strength': float('nan')},
                        {'page_overrides': {'0': []}}, {'margin_Percent': 50}, {'line_spacing': -1}):
            with self.subTest(options=options), self.app.app.test_client() as client:
                response = client.post('/api/preview', json={'file_id': 'demo_tech_book', **options})
                self.assertEqual(response.status_code, 400)
                self.assertIn('error', response.get_json())
        self.assertEqual(self.preview(), 200)

    def test_zero_port_selects_an_actual_listening_port(self):
        self.assertGreater(self.app._find_available_port(0), 0)

    def test_worker_restarts_after_process_failure(self):
        from pdf_service import PdfService
        service = PdfService()
        self.addCleanup(service.close)
        path = self.app.FILES['demo_tech_book']['path']
        self.assertTrue(service.call('validate_pdf', path)['valid'])
        old_process = service._process
        old_process.kill()
        old_process.wait(timeout=5)
        self.assertTrue(service.call('validate_pdf', path)['valid'])
        self.assertNotEqual(old_process.pid, service._process.pid)


@unittest.skipIf(sys.platform == 'win32', 'POSIX signal fixture; Windows frozen smoke runs in CI')
class ProcessShutdownTests(unittest.TestCase):
    def test_server_exit_stops_active_converter(self):
        self._check_server_exit(force=False)

    def test_parent_crash_also_stops_active_converter(self):
        self._check_server_exit(force=True)

    def _check_server_exit(self, force):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary_dir = root / 'bin'
            binary_dir.mkdir()
            pid_file = root / 'converter.pid'
            converter = binary_dir / 'ebook-convert'
            converter.write_text('#!/usr/bin/env python3\nimport os,time\nfrom pathlib import Path\n'
                                 f'Path({str(pid_file)!r}).write_text(str(os.getpid()))\n'
                                 'time.sleep(90)\n')
            converter.chmod(0o755)
            port_file = root / 'port.txt'
            env = {**os.environ, 'KPW6_DATA_DIR': str(root / 'data'),
                   'PATH': str(binary_dir) + os.pathsep + os.environ.get('PATH', ''),
                   'PYTHONDONTWRITEBYTECODE': '1'}
            log = (root / 'server.log').open('w')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                preferred_port = probe.getsockname()[1]
            launcher = ('import faulthandler,runpy,sys; '
                        'faulthandler.dump_traceback_later(15, repeat=True); '
                        'runpy.run_path(sys.argv.pop(1), run_name="__main__")')
            server = subprocess.Popen([sys.executable, '-u', '-c', launcher, str(REPO / 'app.py'), '--no-browser', '--port', str(preferred_port), '--port-file', str(port_file)],
                                      env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            converter_pid = None
            request_thread = None
            errors = []
            try:
                deadline = time.monotonic() + 20
                port = None
                connection_error = None
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        self.fail('server exited during startup: ' + (root / 'server.log').read_text())
                    if port_file.exists():
                        value = port_file.read_text().strip()
                        if value:
                            port = int(value)
                            try:
                                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=0.5):
                                    break
                            except OSError as exc:
                                connection_error = repr(exc)
                    time.sleep(0.05)
                else:
                    self.fail(f'server health timeout ({connection_error}): ' + (root / 'server.log').read_text())
                def export():
                    payload = json.dumps({'file_id': 'demo_tech_book', 'mode': 'tech_book', 'export_format': 'azw3', 'cover_mode': 'none'}).encode()
                    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/export_unified', data=payload, headers={'Content-Type': 'application/json'})
                    try:
                        with urllib.request.urlopen(request, timeout=25) as response:
                            response.read()
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                request_thread = threading.Thread(target=export, daemon=True)
                request_thread.start()
                deadline = time.monotonic() + 15
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(pid_file.exists(), (root / 'server.log').read_text())
                converter_pid = int(pid_file.read_text())
                if force:
                    server.kill()
                else:
                    server.terminate()
                server.wait(timeout=8)
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    try:
                        running = psutil.Process(converter_pid).status() != psutil.STATUS_ZOMBIE
                    except psutil.NoSuchProcess:
                        running = False
                    if not running:
                        break
                    time.sleep(0.05)
                else:
                    self.fail('converter is still executing after server exit')
            finally:
                if server.poll() is None:
                    server.kill()
                    server.wait(timeout=5)
                if converter_pid is not None and psutil.pid_exists(converter_pid):
                    try:
                        psutil.Process(converter_pid).kill()
                    except psutil.NoSuchProcess:
                        pass
                if request_thread is not None:
                    request_thread.join(timeout=2)
                log.close()


if __name__ == '__main__':
    unittest.main()
