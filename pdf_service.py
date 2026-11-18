"""Serialize native PDF work in one owned child interpreter, not HTTP threads.

SPDX-License-Identifier: AGPL-3.0-only
"""
import atexit
from dataclasses import asdict, is_dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from conversion_runner import stop_process_tree


def _encode(value):
    if is_dataclass(value):
        return {'__process_options__': asdict(value)}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


class ExportCancelledError(RuntimeError):
    """Raised when an active worker operation is cancelled by the user."""


class PdfService:
    def __init__(self):
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._process = None
        self._closed = False
        self._cancel_requested = False
        atexit.register(self.close)

    def _start(self):
        with self._state_lock:
            if self._closed:
                raise RuntimeError('PDF服务正在退出，请重新打开应用。')
            if self._process is not None and self._process.poll() is None:
                return self._process
            stale = self._process
            self._process = None
        if stale is not None:
            self._dispose(stale)
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--pdf-worker']
        else:
            command = [sys.executable, str(Path(__file__).with_name('pdf_worker.py'))]
        options = {'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE,
                   'encoding': 'utf-8', 'errors': 'strict', 'bufsize': 1}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        else:
            options['start_new_session'] = True
        proc = subprocess.Popen(command, **options)
        with self._state_lock:
            if self._closed:
                self._dispose(proc)
                raise RuntimeError('PDF服务正在退出，请重新打开应用。')
            self._process = proc
            self._cancel_requested = False
        return proc

    @staticmethod
    def _dispose(process):
        try:
            stop_process_tree(process, timeout=3.5)
        finally:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            try:
                process.wait(timeout=0.5)
            except Exception:
                pass

    def _consume_cancel_flag(self, process) -> bool:
        with self._state_lock:
            cancelled = self._cancel_requested
            self._cancel_requested = False
            if self._process is process:
                self._process = None
            return cancelled

    def call(self, operation, *args, progress_cb=None, **kwargs):
        message = json.dumps({'operation': operation, 'args': _encode(args),
                              'kwargs': _encode(kwargs), 'progress': progress_cb is not None},
                             ensure_ascii=True, allow_nan=False)
        with self._lock:
            process = self._start()
            try:
                process.stdin.write(message + '\n')
                process.stdin.flush()
                while True:
                    line = process.stdout.readline()
                    if not line:
                        if self._consume_cancel_flag(process):
                            self._dispose(process)
                            raise ExportCancelledError('已取消当前导出任务。')
                        self._dispose(process)
                        raise RuntimeError('PDF处理进程已退出，请重试；原始文件未改写。')
                    response = json.loads(line)
                    if response['type'] == 'progress':
                        if progress_cb:
                            progress_cb(response['current'], response['total'], response['message'])
                    elif response['type'] == 'error':
                        if self._consume_cancel_flag(process):
                            self._dispose(process)
                            raise ExportCancelledError('已取消当前导出任务。')
                        error = ValueError if response['kind'] == 'ValueError' else RuntimeError
                        raise error(response['message'])
                    else:
                        return response['value']
            except ExportCancelledError:
                raise
            except (OSError, json.JSONDecodeError) as exc:
                cancelled = self._consume_cancel_flag(process)
                self._dispose(process)
                if cancelled:
                    raise ExportCancelledError('已取消当前导出任务。') from exc
                raise RuntimeError('无法与PDF处理进程通信，请重试。') from exc

    def cancel_current(self) -> bool:
        with self._state_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            self._cancel_requested = True
            self._process = None
        self._dispose(process)
        return True

    def close(self):
        with self._state_lock:
            self._closed = True
            process = self._process
            self._process = None
        if process is not None:
            self._dispose(process)


PDF_SERVICE = PdfService()
EXPORT_PDF_SERVICE = PdfService()


def worker_function(name, service=PDF_SERVICE):
    def call(*args, **kwargs):
        return service.call(name, *args, **kwargs)
    call.__name__ = name
    return call
