#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI standalone launcher for QD.

This script is kept as an independent entry point for users who prefer to
invoke the FastAPI server directly (e.g. ``python run_fastapi.py``).

Internally it delegates to ``run.start_server_fastapi`` so the two paths
share identical startup logic (worker launch, secret warnings, DB init, etc.).

Usage::

    python run_fastapi.py               # uses FASTAPI_PORT env var or config.port
    FASTAPI_PORT=9000 python run_fastapi.py
    WEB_FRAMEWORK=fastapi python run.py # equivalent via run.py dispatcher

To switch back to Tornado::

    WEB_FRAMEWORK=tornado python run.py
"""

import sys


def main():
    """Delegate to the shared FastAPI launcher in run.py."""
    try:
        from run import start_server_fastapi
    except ImportError as e:
        print(f"Could not import start_server_fastapi from run.py: {e}", file=sys.stderr)
        sys.exit(1)

    start_server_fastapi()


if __name__ == "__main__":
    main()
