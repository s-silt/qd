#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
Shared FastAPI dependencies for QD.

Mirrors the responsibilities of web/handlers/base.py (_BaseHandler / BaseHandler)
but expressed as FastAPI Depends() callables instead of class methods.

Public API
----------
get_db(request)             -> DB
get_fetcher(request)        -> Fetcher
get_current_user(request)   -> Optional[dict]   (None = anonymous)
require_user(user)          -> dict              (401 if not logged in)
require_admin(user)         -> dict              (403 if not admin)
evil_counter(request, user) -> None              (403 if evil limit hit)
check_permission(obj, mode, user) -> obj         (raises 404/401 on failure)
"""

from typing import Mapping, MutableMapping, Optional

import umsgpack  # type: ignore

import config
from libs.log import Log

try:
    from fastapi import Depends, HTTPException, Request
except ImportError:
    # Allow the module to be imported in environments without fastapi installed
    # (e.g. during static analysis). Runtime usage will fail.
    Request = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = ""):
            self.status_code = status_code
            self.detail = detail
    HTTPException = _HTTPException  # type: ignore

from web.fastapi.auth import get_secure_cookie

logger = Log("QD.FastAPI.Base").getlogger()


# ---------------------------------------------------------------------------
# Infrastructure dependencies
# ---------------------------------------------------------------------------


def get_db(request: Request):
    """Return the shared DB instance stored on app.state."""
    return request.app.state.db


def get_fetcher(request: Request):
    """Return the shared Fetcher instance stored on app.state."""
    return request.app.state.fetcher


# ---------------------------------------------------------------------------
# Authentication dependencies
# ---------------------------------------------------------------------------


def get_current_user(request: Request) -> Optional[dict]:
    """
    Decode the 'user' secure cookie and return the user dict (or None).

    Replicates _BaseHandler.get_current_user():
      1. Read 'user' cookie via Tornado-compatible secure cookie decode.
      2. umsgpack.unpackb the bytes payload.
      3. Annotate with 'isadmin' based on role field.
    """
    raw = get_secure_cookie(request, "user", max_age_days=config.cookie_days)
    if not raw:
        return None
    try:
        user = umsgpack.unpackb(raw)
        if isinstance(user, (Mapping, MutableMapping)):
            user["isadmin"] = "admin" in user.get("role", "") if user.get("role") else False
        else:
            return None
        return user
    except Exception as e:
        logger.debug("get_current_user decode error: %s", e, exc_info=config.traceback_print)
        return None


def require_user(user: Optional[dict] = Depends(get_current_user)) -> dict:
    """
    Dependency that requires an authenticated user.
    Raises HTTP 401 if the request is not authenticated.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    """
    Dependency that requires an admin user.
    Raises HTTP 403 if the user is not an admin.
    """
    if not user.get("isadmin"):
        raise HTTPException(status_code=403, detail="Admin permission required")
    return user


# ---------------------------------------------------------------------------
# Evil counter (rate limiting / abuse detection)
# ---------------------------------------------------------------------------


def _get_ip(request: Request) -> str:
    """Get the client IP, respecting X-Forwarded-For (same as Tornado xheaders)."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


def evil_counter(
    request: Request,
    user: Optional[dict] = Depends(get_current_user),
):
    """
    Replicates BaseHandler.prepare() evil check.

    In debug mode this is a no-op (same as Tornado).  In production it checks
    whether the IP/userid combo has exceeded the evil threshold and raises 403.
    """
    if config.debug:
        return

    db = get_db(request)
    ip = _get_ip(request)
    userid = user["id"] if user else None

    try:
        if db.redis.is_evil(ip, userid):
            raise HTTPException(status_code=403, detail="Forbidden: too many failed attempts")
    except HTTPException:
        raise
    except Exception as e:
        # Redis unavailable — fail open (same semantics as Tornado fallback)
        logger.debug("evil_counter redis error: %s", e, exc_info=config.traceback_print)


# ---------------------------------------------------------------------------
# Permission helper
# ---------------------------------------------------------------------------


def permission(obj: Optional[dict], mode: str = "r", user: Optional[dict] = None) -> bool:
    """
    Replicates _BaseHandler.permission().

    obj must have a 'userid' key.
    mode 'r' = read, 'w' = write/admin-only for public objects.
    """
    if not obj:
        return False
    if "userid" not in obj:
        return False
    if not obj["userid"]:
        # Public object
        if mode == "r":
            return True
        if user and user.get("isadmin"):
            return True
    if user and obj["userid"] == user.get("id"):
        return True
    return False


def check_permission(
    obj: Optional[dict],
    mode: str = "r",
    user: Optional[dict] = None,
    db=None,
    ip: Optional[str] = None,
) -> dict:
    """
    Replicates BaseHandler.check_permission().

    Raises 404 if obj is falsy, 401 if permission denied.
    Increments evil counter on failure when db + ip are provided.
    """
    def _evil(incr: int):
        if db and ip:
            try:
                userid = user["id"] if user else None
                db.redis.evil(ip, userid, incr)
            except Exception as e:
                logger.debug("check_permission evil incr error: %s", e, exc_info=config.traceback_print)

    if not obj:
        _evil(1)
        raise HTTPException(status_code=404, detail="Not Found")
    if not permission(obj, mode, user):
        _evil(5)
        raise HTTPException(status_code=401, detail="Unauthorized")
    return obj
