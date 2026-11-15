"""Own external conversion processes so shutdown also stops their descendants.

SPDX-License-Identifier: AGPL-3.0-only
"""
import atexit
import os
import signal
import subprocess
import threading

_active = set()
_lock = threading.Lock()


def stop_process_tree(process: subprocess.Popen, timeout: float = 1.5) -> None:
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout + 2, check=False,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == 'nt':
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=timeout)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


def cancel_all_conversions() -> None:
    with _lock:
        processes = list(_active)
    for process in processes:
        try:
            stop_process_tree(process)
        except (OSError, subprocess.SubprocessError):
            # Continue cleaning the other owned children.
            pass


def run_conversion(command):
    kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    else:
        kwargs['start_new_session'] = True
    # Publish the child under the same lock used by shutdown, avoiding a gap
    # between starting it and making it visible to cancellation.
    with _lock:
        process = subprocess.Popen(command, **kwargs)
        _active.add(process)
    try:
        stdout, stderr = process.communicate()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        with _lock:
            _active.discard(process)


atexit.register(cancel_all_conversions)
