#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/login.py (LoginHandler + LogoutHandler only).

Routes ported:
    GET  /login   — show login form; redirect to /my/ if already logged in
    POST /login   — authenticate user, set secure cookie, redirect to /my/
    GET  /logout  — clear user cookie, redirect to /
"""

import time

import umsgpack

import config
from libs.log import Log

try:
    from fastapi import APIRouter, Depends, Request, Response
    from fastapi.exceptions import HTTPException
    from fastapi.responses import RedirectResponse
except ImportError:
    APIRouter = object  # type: ignore
    Request = object  # type: ignore
    Response = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    HTTPException = Exception  # type: ignore
    RedirectResponse = object  # type: ignore

from web.fastapi.auth import set_secure_cookie
from web.fastapi.base import get_current_user, get_db
from web.fastapi.templates import render_template

logger = Log("QD.Web.Login").getlogger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_ip(request: Request) -> str:
    """Get client IP, respecting X-Forwarded-For (mirrors BaseHandler.ip)."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


def _evil(db, request: Request, user, incr: int = 1) -> None:
    """Increment the evil counter for the current IP/user (mirrors BaseHandler.evil)."""
    try:
        ip = _get_ip(request)
        userid = user["id"] if user else None
        db.redis.evil(ip, userid, incr)
    except Exception as e:
        logger.debug("evil counter error: %s", e, exc_info=config.traceback_print)


def _ip2varbinary(request: Request) -> bytes:
    """Convert request client IP to varbinary format (mirrors BaseHandler.ip2varbinary).

    Imports directly from libs._utils.network to avoid the pbkdf2 transitive
    dependency that libs.utils pulls in.
    """
    from libs._utils.network import ip2varbinary, is_ip  # noqa: PLC0415
    ip = _get_ip(request)
    return ip2varbinary(ip, is_ip(ip))


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------


@router.get("/login")
async def login_get(
    request: Request,
    user=Depends(get_current_user),
):
    """Show login form or redirect to /my/ if already logged in."""
    db = get_db(request)

    if user and (await db.user.get(user["id"], fields=("id",))):
        return RedirectResponse(url="/my/", status_code=302)

    reg_flg = False if (await db.site.get(1, fields=("regEn",)))["regEn"] == 0 else True
    return render_template(request, "login.html", regFlg=reg_flg)


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------


@router.post("/login")
async def login_post(
    request: Request,
    response: Response,
):
    """Authenticate user and set secure cookie on success."""
    db = get_db(request)

    # Parse form data (requires python-multipart at runtime; fail gracefully)
    try:
        form = await request.form()
        email = str(form.get("email", "") or "")
        password = str(form.get("password", "") or "")
    except Exception:
        email = ""
        password = ""

    async with db.transaction() as sql_session:
        siteconfig = await db.site.get(
            1, fields=("MustVerifyEmailEn",), sql_session=sql_session
        )
        reg_flg = (
            False
            if (await db.site.get(1, fields=("regEn",), sql_session=sql_session))["regEn"] == 0
            else True
        )

        if not email or not password:
            return render_template(
                request,
                "login.html",
                password_error="请输入用户名和密码",
                email=email,
                regFlg=reg_flg,
            )

        user_try = await db.user.get(
            email=email,
            fields=("id", "role", "status"),
            sql_session=sql_session,
        )
        if user_try:
            if (user_try["status"] != "Enable") and (user_try["role"] != "admin"):
                return render_template(
                    request,
                    "login.html",
                    password_error="账号已被禁用，请联系管理员",
                    email=email,
                    regFlg=reg_flg,
                )
        else:
            return render_template(
                request,
                "login.html",
                password_error="不存在此邮箱或密码错误",
                email=email,
                regFlg=reg_flg,
            )

        if await db.user.challenge(email, password, sql_session=sql_session):
            user = await db.user.get(
                email=email,
                fields=("id", "email", "nickname", "role", "email_verified"),
                sql_session=sql_session,
            )
            if not user:
                return render_template(
                    request,
                    "login.html",
                    password_error="不存在此邮箱或密码错误",
                    email=email,
                    regFlg=reg_flg,
                )

            if (siteconfig["MustVerifyEmailEn"] != 0) and (user["email_verified"] == 0):
                return render_template(
                    request,
                    "login.html",
                    password_error="未验证邮箱，请点击注册重新验证邮箱",
                    email=email,
                    regFlg=reg_flg,
                )

            set_secure_cookie(
                response, "user", umsgpack.packb(user), expires_days=config.cookie_days
            )
            await db.user.mod(
                user["id"],
                atime=time.time(),
                aip=_ip2varbinary(request),
                sql_session=sql_session,
            )

            # Update MD5 hash if it differs — lazy import avoids pbkdf2 at module load
            try:
                from Crypto.Hash import MD5  # noqa: PLC0415
                from libs import mcrypto as crypto  # noqa: PLC0415

                user_pw = await db.user.get(
                    email=email,
                    fields=("id", "password", "password_md5"),
                    sql_session=sql_session,
                )
                hash_obj = MD5.new()
                hash_obj.update(password.encode("utf-8"))
                decrypted = await db.user.decrypt(
                    user_pw["id"], user_pw["password"], sql_session=sql_session
                )
                tmp = crypto.password_hash(hash_obj.hexdigest(), decrypted)
                if user_pw["password_md5"] != tmp:
                    await db.user.mod(user_pw["id"], password_md5=tmp, sql_session=sql_session)
            except ImportError:
                logger.debug(
                    "pbkdf2/Crypto not available — skipping MD5 password hash update"
                )

        else:
            _evil(db, request, None, +5)
            return render_template(
                request,
                "login.html",
                password_error="不存在此邮箱或密码错误",
                email=email,
                regFlg=reg_flg,
            )

    # Successful authentication — build redirect and carry over Set-Cookie headers
    redirect = RedirectResponse(url="/my/", status_code=302)
    for header_name, header_value in response.headers.items():
        if header_name.lower() == "set-cookie":
            redirect.headers.append(header_name, header_value)
    return redirect


# ---------------------------------------------------------------------------
# GET /logout
# ---------------------------------------------------------------------------


@router.get("/logout")
def logout_get(request: Request):
    """Clear user cookie and redirect to home page."""
    redirect = RedirectResponse(url="/", status_code=302)
    redirect.delete_cookie("user")
    return redirect
