#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI launcher for QD.

Starts the FastAPI application on a separate port (default 8925) so it can
run side-by-side with the existing Tornado server (run.py, port 8923).

Usage::

    python run_fastapi.py               # uses FASTAPI_PORT env var or 8925
    FASTAPI_PORT=9000 python run_fastapi.py

The Tornado application (run.py) is not affected by this script.
"""

import json
import os
import sys

import config
from libs.log import Log

logger = Log("QD.FastAPI.Run").getlogger()


def _check_default_secrets():
    """Warn if default secrets are still in use (mirrors run.py behaviour)."""
    import hashlib
    default_secret = hashlib.sha256(b"binux").digest()
    if config.cookie_secret == default_secret:
        logger.warning(
            "[安全] COOKIE_SECRET 未设置, 当前为默认值 'binux'。"
            "强烈建议通过环境变量覆盖。"
        )
    if not config.domain:
        logger.warning(
            "[配置] DOMAIN 未设置, 邮件链接、推送链接将无法生成正确域名。"
        )


def main():
    try:
        import uvicorn
    except ImportError:
        logger.error(
            "uvicorn is not installed.  Run: pip install 'uvicorn[standard]'"
        )
        sys.exit(1)

    try:
        from db import DB
        from libs.fetcher import Fetcher
        from web.fastapi_app import create_app
    except ImportError as e:
        logger.error("Import error during startup: %s", e)
        sys.exit(1)

    _check_default_secrets()

    port = int(os.getenv("FASTAPI_PORT", "8925"))

    # Read version from version.json (same as run.py)
    version = "Debug"
    try:
        _version_path = os.path.join(os.path.dirname(__file__), "version.json")
        with open(_version_path, "r", encoding="utf-8") as f:
            version = str(json.load(f).get("version", "Debug"))
    except Exception as e:
        logger.warning("Could not read version.json: %s", e)

    logger.info("Initialising DB …")
    db = DB()

    logger.info("Initialising Fetcher …")
    fetcher = Fetcher()

    logger.info("Building FastAPI app (version=%s) …", version)
    app = create_app(db, fetcher, version=version)

    logger.info(
        "FastAPI server starting on %s:%d  (Tornado is on port %d)",
        config.bind, port, config.port,
    )

    uvicorn.run(
        app,
        host=config.bind,
        port=port,
        log_level="debug" if config.debug else "info",
        # access_log is handled by our own middleware
        access_log=False,
    )


if __name__ == "__main__":
    main()
