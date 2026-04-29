#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of the password-reset flow from web/handlers/login.py.

Original Tornado handler ported:
    PasswordResetHandler  GET  /password_reset/<code>  — show reset form (token check)
    PasswordResetHandler  GET  /password_reset/        — show email-entry form
    PasswordResetHandler  POST /password_reset/        — trigger reset email
    PasswordResetHandler  POST /password_reset/<code>  — set new password with token

Routes
------
    GET  /password/reset         -> render password_reset_email.html
    POST /password/reset         -> send password reset email
    GET  /password/setnew        -> validate token, render password_reset.html (400 on bad token)
    POST /password/setnew        -> set new password using token
"""

import base64
import time

import config
from libs.log import Log

try:
    from fastapi import APIRouter, Depends, Form, Query, Request
    from fastapi.exceptions import HTTPException
    from fastapi.responses import HTMLResponse, PlainTextResponse
except ImportError:
    APIRouter = object  # type: ignore
    Request = object  # type: ignore
    HTMLResponse = object  # type: ignore
    PlainTextResponse = object  # type: ignore
    Form = lambda **kw: None  # type: ignore
    Query = lambda **kw: None  # type: ignore
    Depends = lambda f: f  # type: ignore
    HTTPException = Exception  # type: ignore

from web.fastapi.base import get_db
from web.fastapi.templates import render_template

logger = Log("QD.Web.UserPasswd").getlogger()

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


def _evil(db, request: Request, incr: int = 1) -> None:
    """Increment the evil counter for the current IP (mirrors BaseHandler.evil)."""
    try:
        ip = _get_ip(request)
        db.redis.evil(ip, None, incr)
    except Exception as e:
        logger.debug("evil counter error: %s", e, exc_info=config.traceback_print)


async def _validate_reset_token(db, code: str):
    """
    Decode and validate a password-reset token.

    Returns (userid, user) on success.
    Raises ValueError with a descriptive message on failure.
    """
    verified_code = base64.b64decode(code)
    userid, verified_code = await db.user.decrypt(0, verified_code)
    user = await db.user.get(userid, fields=("id", "email", "mtime"))
    if not user:
        raise ValueError("User not found")
    mtime, time_time = await db.user.decrypt(userid, verified_code)
    if mtime != user["mtime"]:
        raise ValueError("Token mismatch")
    if time.time() - time_time >= 60 * 60:
        raise ValueError("Reset link expired")
    return userid, user


async def _send_reset_mail(db, user: dict) -> None:
    """
    Build a password-reset token and send the reset e-mail.

    Mirrors PasswordResetHandler.send_mail().
    """
    from libs._utils.mail import send_mail  # noqa: PLC0415

    verified_code = [user["mtime"], time.time()]
    verified_code = await db.user.encrypt(user["id"], verified_code)
    verified_code = await db.user.encrypt(0, [user["id"], verified_code])
    verified_code = base64.b64encode(verified_code).decode()

    http_scheme = "https" if config.mail_domain_https else "http"
    domain = config.domain

    html_body = f"""
    <table style="width:99.8%;height:99.8%"><tbody><tr><td style=" background:#fafafa url(#) ">
    <div style="border-radius:10px;font-size:13px;color:#555;width:666px;font-family:'Century Gothic','Trebuchet MS','Hiragino Sans GB',微软雅黑,'Microsoft Yahei',Tahoma,Helvetica,Arial,SimSun,sans-serif;margin:50px auto;border:1px solid #eee;max-width:100%;background:#fff repeating-linear-gradient(-45deg,#fff,#fff 1.125rem,transparent 1.125rem,transparent 2.25rem);box-shadow:0 1px 5px rgba(0,0,0,.15)">
    <div style="width:100%;background:#49BDAD;color:#fff;border-radius:10px 10px 0 0;background-image:-moz-linear-gradient(0deg,#43c6b8,#ffd1f4);background-image:-webkit-linear-gradient(0deg,#4831ff,#0497ff);height:66px">
    <p style="font-size:15px;word-break:break-all;padding:23px 32px;margin:0;background-color:hsla(0,0%,100%,.4);border-radius:10px 10px 0 0">&nbsp;[QD平台]&nbsp;&nbsp;{http_scheme}://{domain}</p></div>
    <div style="margin:40px auto;width:90%">
        <p>点击以下链接完成您的密码重置（一小时内有效）。</p>
        <p style="background:#fafafa repeating-linear-gradient(-45deg,#fff,#fff 1.125rem,transparent 1.125rem,transparent 2.25rem);box-shadow:0 2px 5px rgba(0,0,0,.15);margin:20px 0;padding:15px;border-radius:5px;font-size:14px;color:#555">
        <a href="{http_scheme}://{domain}/password_reset/{verified_code}">{http_scheme}://{domain}/password_reset/{verified_code}</a></p>
        <p>请注意：此邮件由 <a href="{http_scheme}://{domain}" style="color:#12addb" target="_blank">QD平台</a> 自动发送，请勿直接回复。</p>
        <p>若此邮件不是您请求的，请忽略并删除！</p>
    </div>
    </div>
    </td></tr></tbody></table>
    """

    import asyncio  # noqa: PLC0415
    asyncio.ensure_future(
        send_mail(
            to=user["email"],
            subject=f"QD平台({domain}) 密码重置",
            html=html_body,
            shark=True,
        )
    )


# ---------------------------------------------------------------------------
# GET /password/reset  — show the email-entry form
# ---------------------------------------------------------------------------


@router.get("/password/reset", response_class=HTMLResponse)
async def password_reset_get(request: Request):
    """Render the email-entry form for triggering a password reset."""
    return render_template(request, "password_reset_email.html")


# ---------------------------------------------------------------------------
# POST /password/reset  — trigger reset: validate email, fire off e-mail
# ---------------------------------------------------------------------------


@router.post("/password/reset")
async def password_reset_post(
    request: Request,
    email: str = Form(default=""),
):
    """
    Accept an email address and (if the account exists) send a reset link.

    Always returns a generic message to avoid user-enumeration.
    Mirrors PasswordResetHandler.post(code="").
    """
    db = get_db(request)

    if not config.domain:
        return PlainTextResponse(
            "请联系 QD 框架管理员配置框架域名 domain, 以启用密码重置功能!"
        )

    _evil(db, request, +5)

    if not email:
        return render_template(
            request, "password_reset_email.html", email_error="请输入邮箱"
        )
    if email.count("@") != 1 or email.count(".") == 0:
        return render_template(
            request, "password_reset_email.html", email_error="邮箱格式不正确"
        )

    user = await db.user.get(
        email=email, fields=("id", "email", "mtime", "nickname", "role")
    )

    # Always respond with the generic message (anti-enumeration)
    msg = "如果用户存在，会将发送密码重置邮件到您的邮箱，请注意查收。（如果您没有收到过激活邮件，可能无法也无法收到密码重置邮件）"

    if user:
        logger.info("password reset: userid=%s email=%s", user["id"], user["email"])
        try:
            await _send_reset_mail(db, user)
        except Exception as e:
            logger.error(
                "password reset send_mail failed for %s: %s",
                user["email"],
                e,
                exc_info=config.traceback_print,
            )

    return PlainTextResponse(msg)


# ---------------------------------------------------------------------------
# GET /password/setnew  — validate token, show the new-password form
# ---------------------------------------------------------------------------


@router.get("/password/setnew", response_class=HTMLResponse)
async def password_setnew_get(
    request: Request,
    token: str = Query(default=""),
):
    """
    Validate the reset token and render the new-password form.

    Returns HTTP 400 if the token is missing, expired, or invalid.
    Mirrors PasswordResetHandler.get(code=<token>).
    """
    db = get_db(request)

    if not token:
        _evil(db, request, +10)
        raise HTTPException(status_code=400, detail="Bad Request")

    try:
        await _validate_reset_token(db, token)
    except Exception as e:
        _evil(db, request, +10)
        logger.error("password setnew token validation failed: %r", e, exc_info=config.traceback_print)
        raise HTTPException(status_code=400, detail="Bad Request") from e

    return render_template(request, "password_reset.html")


# ---------------------------------------------------------------------------
# POST /password/setnew  — apply the new password
# ---------------------------------------------------------------------------


@router.post("/password/setnew")
async def password_setnew_post(
    request: Request,
    token: str = Form(default=""),
    password: str = Form(default=""),
):
    """
    Set a new password using the validated reset token.

    Returns HTTP 400 on invalid/expired token, or re-renders the form on
    validation errors (e.g. password too short).
    Mirrors PasswordResetHandler.post(code=<token>).
    """
    db = get_db(request)

    if not token:
        _evil(db, request, +10)
        raise HTTPException(status_code=400, detail="Bad Request")

    if len(password) < 6:
        return render_template(
            request, "password_reset.html", password_error="密码需要大于6位"
        )

    async with db.transaction() as sql_session:
        try:
            verified_code = base64.b64decode(token)
            userid, verified_code_inner = await db.user.decrypt(
                0, verified_code, sql_session=sql_session
            )
            user = await db.user.get(
                userid,
                fields=("id", "email", "mtime", "email_verified"),
                sql_session=sql_session,
            )
            if not user:
                raise ValueError("User not found")
            mtime, time_time = await db.user.decrypt(
                userid, verified_code_inner, sql_session=sql_session
            )
            if mtime != user["mtime"]:
                raise ValueError("Token mismatch")
            if time.time() - time_time >= 60 * 60:
                raise ValueError("Reset link expired")
        except Exception as e:
            _evil(db, request, +10)
            logger.error(
                "password setnew post token validation failed: %r",
                e,
                exc_info=config.traceback_print,
            )
            raise HTTPException(status_code=400, detail="Bad Request") from e

        await db.user.mod(
            userid,
            password=password,
            mtime=time.time(),
            sql_session=sql_session,
        )

    http_scheme = "https" if config.mail_domain_https else "http"
    domain = config.domain or "localhost"
    body = (
        f'密码重置成功! 请<a href="{http_scheme}://{domain}/login">点击此处</a>返回登录页面。'
    )
    return HTMLResponse(content=body)
