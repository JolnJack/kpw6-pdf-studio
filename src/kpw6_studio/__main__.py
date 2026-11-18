"""Unified CLI and frozen-binary entrypoint (`python -m kpw6_studio` / `kpw6-studio`).

SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import sys


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    if "--pdf-worker" in sys.argv[1:]:
        from kpw6_studio.worker.protocol import run_worker
        run_worker()
        return
    from kpw6_studio.server.web_app import run_cli
    run_cli()


if __name__ == "__main__":
    main()
