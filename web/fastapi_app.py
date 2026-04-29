#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI application factory for QD.

This module is intentionally side-by-side with web/app.py (Tornado).
It does NOT touch or import web/app.py; both applications can run
concurrently on different ports.

Usage::

    from db import DB
    from libs.fetcher import Fetcher
    from web.fastapi_app import create_app

    db = DB()
    fetcher = Fetcher()
    app = create_app(db, fetcher)

Then serve with uvicorn (see run_fastapi.py).
"""

import json
import os
from typing import Optional

import jinja2

import config
from libs.log import Log

# utils.format_date and utils.varbinary2ip are imported directly from the
# sub-modules that don't carry heavy crypto dependencies, so the FastAPI
# application can start even when optional packages (e.g. pbkdf2) are absent.
try:
    from libs._utils.datetime_fmt import format_date as _format_date
    from libs._utils.network import varbinary2ip as _varbinary2ip
except ImportError:
    _format_date = str  # type: ignore
    _varbinary2ip = str  # type: ignore

# jinja_globals pulls in the full crypto stack; gracefully degrade when
# optional packages are missing (tests / minimal deployments).
try:
    from libs._utils.jinja_filters import jinja_globals as _jinja_globals
except ImportError:
    _jinja_globals = {}  # type: ignore

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.gzip import GZipMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as _e:
    raise ImportError(
        "fastapi is required for web/fastapi_app.py. "
        "Install it with: pip install fastapi 'uvicorn[standard]'"
    ) from _e

logger = Log("QD.FastAPI").getlogger()


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

def _load_version() -> str:
    try:
        _version_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "version.json")
        with open(_version_path, "r", encoding="utf-8") as f:
            return str(json.load(f).get("version", "Debug"))
    except Exception:
        return "Debug"


# ---------------------------------------------------------------------------
# Jinja2 environment
# ---------------------------------------------------------------------------

def _build_jinja_env(template_path: str, version: str) -> jinja2.Environment:
    """Build a Jinja2 Environment equivalent to the one in web/app.py."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_path),
        extensions=["jinja2.ext.loopcontrols"],
        autoescape=True,
        auto_reload=config.autoreload,
    )

    # Globals that mirror web/app.py Application.__init__
    env.globals.update({
        "config": config,
        "format_date": _format_date,
        "varbinary2ip": _varbinary2ip,
        "version": version,
    })

    # Additional globals from jinja_filters (functions exposed in templates)
    env.globals.update(_jinja_globals)

    return env


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(db=None, fetcher=None, version: Optional[str] = None) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Parameters
    ----------
    db:
        A ``DB`` instance (from db/__init__.py).  If None the app will start
        but database-dependent routes will fail at request time.
    fetcher:
        A ``Fetcher`` instance (from libs/fetcher.py).  Same caveat.
    version:
        Version string shown in templates and the OpenAPI docs.  Defaults to
        the value read from version.json.
    """
    version = version or _load_version()

    app = FastAPI(
        title="QD",
        description="QD HTTP Task Scheduler — FastAPI interface",
        version=str(version),
        # Redirect /about/ → /about (mirrors Tornado @addslash)
        redirect_slashes=True,
    )

    # ----------------------------------------------------------------
    # State: shared singletons accessible via request.app.state
    # ----------------------------------------------------------------
    app.state.db = db
    app.state.fetcher = fetcher

    # Jinja2 environment
    _template_path = os.path.join(os.path.dirname(__file__), "tpl")
    app.state.jinja_env = _build_jinja_env(_template_path, version)

    # ----------------------------------------------------------------
    # Static files (mirrors config.static_url_prefix)
    # ----------------------------------------------------------------
    _static_path = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(_static_path):
        # Strip trailing slash from prefix for mount path
        _mount_path = config.static_url_prefix.rstrip("/") or "/static"
        app.mount(
            _mount_path,
            StaticFiles(directory=_static_path),
            name="static",
        )

    # ----------------------------------------------------------------
    # Middleware
    # ----------------------------------------------------------------

    # GZip (mirrors config.gzip in Tornado settings)
    if config.gzip:
        app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Access logging middleware
    if config.accesslog:
        @app.middleware("http")
        async def _access_log(request: Request, call_next):
            import time as _time
            start = _time.time()
            response = await call_next(request)
            duration_ms = (_time.time() - start) * 1000
            logger.info(
                "%s %s %s %.0fms",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
            )
            return response

    # ----------------------------------------------------------------
    # Exception handlers
    # ----------------------------------------------------------------

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        # Try to render an HTML error page; fall back to JSON.
        try:
            jinja_env = request.app.state.jinja_env
            # Use base template if available; otherwise plain HTML.
            try:
                tpl = jinja_env.get_template("error.html")
                html = tpl.render({"status_code": exc.status_code, "detail": exc.detail,
                                   "request": request, "current_user": None,
                                   "static_url": lambda p: config.static_url_prefix.rstrip("/") + "/" + p.lstrip("/"),
                                   "xsrf_token": "", "xsrf_form_html": lambda: "",
                                   "handler": None, "locale": None,
                                   "reverse_url": lambda n, *a: f"/{n}"})
                return HTMLResponse(content=html, status_code=exc.status_code)
            except jinja2.TemplateNotFound:
                pass
        except Exception:
            pass
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    # ----------------------------------------------------------------
    # Routers (auto-discovered from web/fastapi/handlers/)
    # ----------------------------------------------------------------
    try:
        from web.fastapi.handlers import routers  # noqa: PLC0415
        for _router in routers:
            app.include_router(_router)
        logger.info("FastAPI: registered %d router(s)", len(routers))
    except Exception as _e:
        logger.warning("FastAPI: router discovery failed: %s", _e, exc_info=config.traceback_print)

    return app
