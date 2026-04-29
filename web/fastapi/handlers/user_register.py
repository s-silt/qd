#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of the user registration flow from web/handlers/login.py.

Original Tornado handlers ported:
    RegisterHandler   GET  /register         — show registration form
    RegisterHandler   POST /register         — create new account, send verify email
    VerifyHandler     GET  /verify/{code}    — verify email address via token

Routes:
    GET  /register          -> render register.html or redirect to /my/
    POST /register          -> create account, set cookie, redirect to /my/
    GET  /verify/{code}     -> verify email address, return plain-text result
"""

import base64
import time

import umsgpack
from libs.log import Log

import config

try:
    from fastapi import APIRouter, Request, Response
    from fastapi.exceptions import HTTPException
    from fastapi.responses import PlainTextResponse, RedirectResponse
except ImportError:
    APIRouter = object  # type: ignore
    Request = object  # type: ignore
    Response = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    HTTPException = Exception  # type: ignore
    RedirectResponse = object  # type: ignore
    PlainTextResponse = object  # type: ignore

from web.fastapi.auth import set_secure_cookie
from web.fastapi.base import get_current_user, get_db
from web.fastapi.templates import render_template

logger = Log("QD.Web.Register").getlogger()

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


def _ip2varbinary(request: Request) -> bytes:
    """Convert request client IP to varbinary format (mirrors BaseHandler.ip2varbinary)."""
    from libs._utils.network import ip2varbinary, is_ip
    ip = _get_ip(request)
    return ip2varbinary(ip, is_ip(ip))


def _evil(db, request: Request, incr: int = 1) -> None:
    """Increment the evil counter for the current IP (mirrors BaseHandler.evil)."""
    try:
        ip = _get_ip(request)
        db.redis.evil(ip, None, incr)
    except Exception as e:
        logger.debug("evil counter error: %s", e, exc_info=config.traceback_print)


async def _send_register_mail(db, user: dict, sql_session=None) -> None:
    """
    Send an email-verification mail to the newly registered user.
    Mirrors RegisterHandler.send_mail().
    """
    from libs._utils.mail import send_mail

    verified_code = [user["email"], time.time()]
    verified_code = await db.user.encrypt(user["id"], verified_code, sql_session=sql_session)
    verified_code = await db.user.encrypt(0, [user["id"], verified_code], sql_session=sql_session)
    verified_code = base64.b64encode(verified_code).decode()

    http = "https" if config.mail_domain_https else "http"
    domain = config.domain
    html_body = (
        "<table style=\"width:99.8%%;height:99.8%%\"><tbody><tr>"
        "<td style=\" background:#fafafa url(#) \">"
        "<div style=\"border-radius:10px;font-size:13px;color:#555;width:666px;"
        "font-family:'Century Gothic','Trebuchet MS','Hiragino Sans GB','微软雅黑',"
        "'Microsoft Yahei',Tahoma,Helvetica,Arial,SimSun,sans-serif;"
        "margin:50px auto;border:1px solid #eee;max-width:100%%;"
        "background:#fff repeating-linear-gradient(-45deg,#fff,#fff 1.125rem,transparent 1.125rem,transparent 2.25rem);"
        "box-shadow:0 1px 5px rgba(0,0,0,.15)\">"
        "<div style=\"width:100%%;background:#49BDAD;color:#fff;border-radius:10px 10px 0 0;"
        "background-image:-moz-linear-gradient(0deg,#43c6b8,#ffd1f4);"
        "background-image:-webkit-linear-gradient(0deg,#4831ff,#0497ff);height:66px\">"
        "<p style=\"font-size:15px;word-break:break-all;padding:23px 32px;margin:0;"
        "background-color:hsla(0,0%%,100%%,.4);border-radius:10px 10px 0 0\">"
        f"&nbsp;[QD平台]&nbsp;&nbsp;{http}://{domain}</p></div>"
        "<div style=\"margin:40px auto;width:90%%\">"
        "<p>点击以下链接验证邮筱，"
        "当您的定时任务执行失败的时候，"
        "会自动给您发送通知邮件。</p>"
        "<p style=\"background:#fafafa repeating-linear-gradient(-45deg,#fff,#fff 1.125rem,"
        "transparent 1.125rem,transparent 2.25rem);"
        "box-shadow:0 2px 5px rgba(0,0,0,.15);margin:20px 0;padding:15px;"
        "border-radius:5px;font-size:14px;color:#555\">"
        f"<a href=\"{http}://{domain}/verify/{verified_code}\">"
        f"{http}://{domain}/verify/{verified_code}</a></p>"
        "<p>请注意：此邮件由 "
        f"<a href=\"{http}://{domain}/verify/{verified_code}\" "
        "style=\"color:#12addb\" target=\"_blank\">QD平台</a> "
        "自动发送，请勿直接回复。</p>"
        "<p>若此邮件不是您请求的，"
        "请忽略并删除！</p>"
        "</div></div></td></tr></tbody></table>"
    )

    await send_mail(
        to=user["email"],
        subject="欢迎注册 QD 平台",
        html=html_body,
        shark=True,
    )


# ---------------------------------------------------------------------------
# GET /register
# ---------------------------------------------------------------------------


@router.get("/register")
async def register_get(
    request: Request,
):
    """Show registration form, or redirect to /my/ if already logged in."""
    db = get_db(request)
    user = get_current_user(request)

    if user:
        return RedirectResponse(url="/my/", status_code=302)

    reg_flg = False if (await db.site.get(1, fields=("regEn",)))["regEn"] == 0 else True
    return render_template(request, "register.html", regFlg=reg_flg)


# ---------------------------------------------------------------------------
# POST /register
# ---------------------------------------------------------------------------


@router.post("/register")
async def register_post(
    request: Request,
    response: Response,
):
    """
    Create a new user account.

    Mirrors RegisterHandler.post() from web/handlers/login.py.
    On success: set secure cookie and redirect to /my/.
    On failure: re-render register.html with an error message.
    Raises HTTP 400 when the email already exists (for JSON/API callers).
    """
    db = get_db(request)

    try:
        form = await request.form()
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
    except Exception:
        email = ""
        password = ""

    async with db.transaction() as sql_session:
        siteconfig = await db.site.get(
            1,
            fields=("regEn", "MustVerifyEmailEn"),
            sql_session=sql_session,
        )
        reg_en = siteconfig["regEn"]
        reg_flg = False if reg_en == 0 else True
        must_verify_email_en = siteconfig["MustVerifyEmailEn"]

        # --- basic validation ---
        if not email:
            return render_template(
                request, "register.html",
                email_error="请输入邮筱",
                regFlg=reg_flg,
            )
        if email.count("@") != 1 or email.count(".") == 0:
            return render_template(
                request, "register.html",
                email_error="邮筱格式不正确",
                regFlg=reg_flg,
            )
        if len(password) < 6:
            return render_template(
                request, "register.html",
                password_error="密码需要大于6位",
                email=email,
                regFlg=reg_flg,
            )

        existing_user = await db.user.get(
            email=email,
            fields=("id", "email", "email_verified", "nickname", "role"),
            sql_session=sql_session,
        )

        if existing_user is None:
            # --- new user ---
            if reg_en != 1:
                return render_template(
                    request, "register.html",
                    email_error="管理员关闭注册",
                    regFlg=reg_flg,
                )

            _evil(db, request, +5)
            try:
                await db.user.add(
                    email=email,
                    password=password,
                    ip=_ip2varbinary(request),
                    sql_session=sql_session,
                )
            except db.user.DeplicateUser as exc:
                logger.error(
                    "email地址 %s 已注册, error: %s",
                    email, exc, exc_info=config.traceback_print,
                )
                _evil(db, request, +3)
                raise HTTPException(
                    status_code=400,
                    detail="邮筱地址已注册",
                ) from exc

            user = await db.user.get(
                email=email,
                fields=("id", "email", "nickname", "role"),
                sql_session=sql_session,
            )
            await db.notepad.add(
                dict(userid=user["id"], notepadid=1),
                sql_session=sql_session,
            )

            # Set the session cookie
            set_secure_cookie(
                response, "user", umsgpack.packb(user),
                expires_days=config.cookie_days,
            )

            # First registered user becomes admin (same as Tornado logic)
            usertmp = await db.user.list(
                sql_session=sql_session,
                fields=("id", "email", "nickname", "role", "email_verified"),
            )
            if len(usertmp) == 1 and config.user0isadmin:
                if usertmp[0]["email"] == email:
                    await db.user.mod(usertmp[0]["id"], role="admin", sql_session=sql_session)

            # Send verification mail (best-effort)
            if must_verify_email_en == 1:
                if not config.domain:
                    return render_template(
                        request, "register.html",
                        email_error=(
                            "请联系 QD 框架管理员配置"
                            "框架域名 domain, "
                            "以启用邮筱验证功能!"
                        ),
                        regFlg=reg_flg,
                    )
                else:
                    # mail sent below; inform user to verify
                    pass  # fall through to send mail + redirect

            if config.domain:
                try:
                    await _send_register_mail(db, user, sql_session=sql_session)
                except Exception as mail_exc:
                    logger.warning(
                        "Failed to send verification mail to %s: %s",
                        email, mail_exc, exc_info=config.traceback_print,
                    )
            else:
                logger.warning(
                    "请配置框架域名 domain, "
                    "以启用邮筱验证功能!"
                )

            if must_verify_email_en == 1:
                return render_template(
                    request, "register.html",
                    email_error="请验证邮筱后再登陆",
                    regFlg=reg_flg,
                )

        else:
            # --- email already registered ---
            if must_verify_email_en == 1 and existing_user["email_verified"] != 1:
                if not config.domain:
                    return render_template(
                        request, "register.html",
                        email_error=(
                            "请联系 QD 框架管理员配置"
                            "框架域名 domain, "
                            "以启用邮筱验证功能!"
                        ),
                        regFlg=reg_flg,
                    )
                try:
                    await _send_register_mail(db, existing_user, sql_session=sql_session)
                except Exception as mail_exc:
                    logger.warning(
                        "Failed to resend verification mail to %s: %s",
                        email, mail_exc, exc_info=config.traceback_print,
                    )
                return render_template(
                    request, "register.html",
                    email_error=(
                        "email地址未验证, "
                        "邮件已发送, 请验证邮件后登陆"
                    ),
                    regFlg=reg_flg,
                )
            # email exists and either already verified or verification not required
            raise HTTPException(
                status_code=400,
                detail="邮筱地址已注册",
            )

    # Successful registration without mandatory email verification — redirect
    redirect = RedirectResponse(url="/my/", status_code=302)
    for header_name, header_value in response.headers.items():
        redirect.headers.append(header_name, header_value)
    return redirect


# ---------------------------------------------------------------------------
# GET /verify/{code}
# ---------------------------------------------------------------------------


@router.get("/verify/{code}")
async def verify_email(
    code: str,
    request: Request,
):
    """
    Verify a user's email address via the token embedded in the link.
    Mirrors VerifyHandler.get() from web/handlers/login.py.
    """
    db = get_db(request)
    userid = None

    try:
        async with db.transaction() as sql_session:
            verified_code = base64.b64decode(code)
            userid, verified_code = await db.user.decrypt(0, verified_code, sql_session=sql_session)
            user = await db.user.get(
                userid,
                fields=("id", "email", "email_verified"),
                sql_session=sql_session,
            )
            if not user:
                raise ValueError("User not found")
            if user["email_verified"]:
                raise ValueError("Email already verified")
            email, time_time = await db.user.decrypt(userid, verified_code, sql_session=sql_session)
            if time.time() - time_time >= 30 * 24 * 60 * 60:
                raise ValueError("Verification code expired")
            if user["email"] != email:
                raise ValueError("Email mismatch")

            await db.user.mod(
                userid,
                email_verified=True,
                mtime=time.time(),
                sql_session=sql_session,
            )

        return PlainTextResponse("验证成功")

    except Exception as exc:
        _evil(db, request, +5)
        logger.error(
            "UserID: %s verify email failed! Reason: %s",
            userid or "-1", exc, exc_info=config.traceback_print,
        )
        raise HTTPException(status_code=400, detail="验证失败") from exc
