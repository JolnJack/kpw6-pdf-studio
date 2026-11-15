"""Bounded real-worker preflight, with child stacks if startup or RPC stalls."""
import json
import os
import queue
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


def main():
    worker = Path(__file__).resolve().parents[1] / 'pdf_worker.py'
    launcher = ('import faulthandler,runpy,sys; '
                'faulthandler.dump_traceback_later(10, repeat=True); '
                'runpy.run_path(sys.argv.pop(1), run_name="__main__")')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with (root / 'worker.log').open('w', encoding='utf-8') as log:
            options = {'bufsize': 1}
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            else:
                options['start_new_session'] = True
            process = subprocess.Popen([sys.executable, '-u', '-c', launcher, str(worker)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=log, encoding='utf-8', **options)
            responses = queue.Queue()
            def read_response():
                try:
                    responses.put(process.stdout.readline())
                except Exception as exc:
                    responses.put(exc)
            try:
                threading.Thread(target=read_response, daemon=True).start()
                payload = {'operation': 'generate_demo_pdfs', 'args': [str(root / 'samples')], 'kwargs': {}}
                process.stdin.write(json.dumps(payload) + '\n')
                process.stdin.flush()
                try:
                    response = responses.get(timeout=25)
                except queue.Empty:
                    raise RuntimeError('Worker did not return its first RPC in 25 seconds')
                if isinstance(response, Exception):
                    raise response
                message = json.loads(response)
                if message['type'] != 'result' or len(message['value']) != 5:
                    raise RuntimeError(f'Unexpected worker response: {message}')
                print('Worker protocol preflight: five real demo PDFs generated', flush=True)
            except Exception:
                log.flush()
                print((root / 'worker.log').read_text(encoding='utf-8', errors='replace'), file=sys.stderr)
                raise
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()


if __name__ == '__main__':
    main()
