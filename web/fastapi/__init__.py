#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
web/fastapi/ — FastAPI migration package for QD framework.

This package contains the FastAPI-based re-implementation of QD that runs
side-by-side with the existing Tornado application.  The existing Tornado app
in web/app.py is completely untouched.

Layout
------
web/fastapi_app.py          Application factory (create_app)
web/fastapi/__init__.py     This file — package marker
web/fastapi/auth.py         Tornado-compatible secure-cookie helpers
web/fastapi/base.py         Shared FastAPI dependencies (get_db, get_current_user, …)
web/fastapi/templates.py    Jinja2 render_template helper aligned with BaseHandler
web/fastapi/handlers/       Per-feature APIRouter modules (auto-discovered)

See web/fastapi/README.md for full documentation.
"""
