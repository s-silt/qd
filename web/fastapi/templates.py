#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Jinja2 template rendering helpers for FastAPI, mirroring the namespace that
web/handlers/base.py:_BaseHandler.render_string() injects.

The jinja_env is obtained from app.state.jinja_env (set up in web/fastapi_app.py),
so no second environment is created.
"""

from typing import Any, Optional

import config

try:
    from fastapi import Request
    from fastapi.responses import HTMLResponse
except ImportError:  # graceful degradation when fastapi not installed
    Request = Any  # type: ignore
    HTMLResponse = Any  # type: ignore


# ---------------------------------------------------------------------------
# static_url helper
# ---------------------------------------------------------------------------

def _make_static_url(static_url_prefix: str):
    """
    Return a static_url(path) function that prepends the configured prefix.

    Tornado's StaticFileHandler appends a ?v=<hash> query parameter for cache-
    busting.  In FastAPI we serve files via StaticFiles which has no built-in
    versioning.  We return a simple prefix-prepend; this is the minimum needed
    to keep templates rendering correctly.  A hash-based approach can be wired
    in later without changing the template call-sites.
    """
    prefix = static_url_prefix.rstrip("/")

    def static_url(path: str) -> str:
        # Normalise leading slash
        if not path.startswith("/"):
            path = "/" + path
        return f"{prefix}{path}"

    return static_url


# ---------------------------------------------------------------------------
# XSRF stubs
# ---------------------------------------------------------------------------

def _xsrf_token_stub() -> str:
    """Stub: XSRF is not enabled in web/app.py (xsrf_cookies=True is absent)."""
    return ""


def _xsrf_form_html_stub() -> str:
    """Stub: returns empty string; replace when XSRF support is wired in."""
    return ""


# ---------------------------------------------------------------------------
# reverse_url helper
# ---------------------------------------------------------------------------

def _make_reverse_url(app):
    """
    FastAPI does not have a `reverse_url` equivalent built-in.
    We build a simple name→path map from the registered routes.
    """
    def reverse_url(name: str, *args) -> str:
        for route in app.routes:
            if hasattr(route, "name") and route.name == name:
                path = route.path
                # Substitute positional path parameters with args
                import re
                params = re.findall(r"\{([^}]+)\}", path)
                for i, param in enumerate(params):
                    if i < len(args):
                        path = path.replace(f"{{{param}}}", str(args[i]), 1)
                return path
        return f"/{name}"

    return reverse_url


# ---------------------------------------------------------------------------
# Main render helper
# ---------------------------------------------------------------------------

def render_template(request: Request, template_name: str, **kwargs) -> "HTMLResponse":
    """
    Render a Jinja2 template and return an HTMLResponse.

    Namespace injected (mirrors web/handlers/base.py:_BaseHandler.render_string):
      - static_url(name)      : prepend static prefix
      - xsrf_token            : stub (empty string — XSRF disabled in app.py)
      - xsrf_form_html        : stub
      - handler               : None (FastAPI has no handler object)
      - request               : the FastAPI Request
      - current_user          : resolved from secure cookie (via get_current_user)
      - locale                : None (i18n not implemented; placeholder for future)
      - reverse_url           : basic name→path lookup

    Additional globals already on jinja_env (set in fastapi_app.py):
      - config, format_date, varbinary2ip, version
    """
    app = request.app
    jinja_env = app.state.jinja_env

    # Lazy-import to avoid circular dependency
    from web.fastapi.base import get_current_user  # noqa: PLC0415

    current_user = get_current_user(request)

    namespace: dict = {
        "static_url": _make_static_url(config.static_url_prefix),
        "xsrf_token": _xsrf_token_stub(),
        "xsrf_form_html": _xsrf_form_html_stub,
        "handler": None,
        "request": request,
        "current_user": current_user,
        "locale": None,  # placeholder — add babel/i18n integration later
        "reverse_url": _make_reverse_url(app),
    }
    namespace.update(kwargs)

    template = jinja_env.get_template(template_name)
    html = template.render(namespace)
    return HTMLResponse(content=html)
