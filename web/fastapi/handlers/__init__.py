#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Auto-discovery of FastAPI routers in web/fastapi/handlers/.

Mirrors the pattern used by web/handlers/__init__.py:
  - Scan every *.py file in this directory (except __init__.py).
  - Import each module.
  - If the module exposes a `router` attribute (an APIRouter), yield it.

Usage (in web/fastapi_app.py)::

    from web.fastapi.handlers import routers
    for router in routers:
        app.include_router(router)
"""

import os

routers = []

_pkg = __package__ or "web.fastapi.handlers"

for _file in os.listdir(os.path.dirname(__file__)):
    if not _file.endswith(".py"):
        continue
    if _file == "__init__.py":
        continue

    _module_name = f"{_pkg}.{_file[:-3]}"
    try:
        _mod = __import__(_module_name, fromlist=["router"])
        if hasattr(_mod, "router"):
            routers.append(_mod.router)
    except Exception as _exc:
        import warnings
        warnings.warn(
            f"web.fastapi.handlers: could not import {_module_name}: {_exc}",
            stacklevel=1,
        )
