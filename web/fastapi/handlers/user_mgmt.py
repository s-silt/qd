#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of user management (admin) endpoints from web/handlers/user.py.

Original Tornado handler ported:
    UserManagerHandler  GET  /user/{userid}/manage  -- list all users (admin view)
    UserManagerHandler  POST /user/{userid}/manage  -- ban/activate/verify/delete users

Routes (admin-only):
    GET  /user/manage          -> render user_manage.html with user list
    POST /user/manage/ban      -> disable selected users + their tasks
    POST /user/manage/activate -> enable selected users + their tasks
    POST /user/manage/verify   -> mark selected users as email-verified
    POST /user/manage/delete   -> delete selected users and all their data

All endpoints require admin authentication (Depends(require_admin)).
"""

import time
from typing import List, Optional

import config
from libs.log import Log

try:
    from fastapi import APIRouter, Depends, Form, Request
    from fastapi.exceptions import HTTPException
    from fastapi.responses import JSONResponse
except ImportError:
    APIRouter = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    Form = lambda *a, **kw: None  # type: ignore
    Request = object  # type: ignore
    HTTPException = Exception  # type: ignore
    JSONResponse = object  # type: ignore

from web.fastapi.base import get_db, require_admin
from web.fastapi.templates import render_template

logger = Log("QD.FastAPI.UserMgmt").getlogger()

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /user/manage -- admin user list
# ---------------------------------------------------------------------------


@router.get("/user/manage")
async def user_manage_get(
    request: Request,
    admin_user: dict = Depends(require_admin),
):
    """
    Render the admin user management page with a list of all users.

    Requires admin role; returns 401/403 for unauthenticated / non-admin.
    Mirrors UserManagerHandler.get() from web/handlers/user.py.
    """
    db = get_db(request)

    users = []
    for user in await db.user.list(
        fields=("id", "status", "role", "ctime", "email", "atime", "email_verified", "aip")
    ):
        if user.get("email_verified") == 0:
            user["email_verified"] = False
        else:
            user["email_verified"] = True
        users.append(user)

    return render_template(
        request,
        "user_manage.html",
        users=users,
        userid=admin_user.get("id"),
        adminflg=True,
        flg="",
        title="",
        log="",
    )


# ---------------------------------------------------------------------------
# POST /user/manage/ban -- disable selected users
# ---------------------------------------------------------------------------


@router.post("/user/manage/ban")
async def user_manage_ban(
    request: Request,
    admin_user: dict = Depends(require_admin),
):
    """
    Disable (ban) selected users and all their tasks.

    Expects JSON body: {"user_ids": [<id>, ...], "adminmail": "...", "adminpwd": "..."}
    Mirrors the 'banbtn' branch of UserManagerHandler.post().
    """
    db = get_db(request)
    body = await request.json()
    adminmail = body.get("adminmail", "")
    adminpwd = body.get("adminpwd", "")
    target_ids: List = body.get("user_ids", [])

    try:
        async with db.transaction() as sql_session:
            if not await db.user.challenge_md5(adminmail, adminpwd, sql_session=sql_session):
                raise HTTPException(status_code=401, detail="账号/密码错误")

            for sub_user in target_ids:
                sub_user = str(sub_user)
                row = await db.user.get(sub_user, fields=("role",), sql_session=sql_session)
                if row and row.get("role") != "admin":
                    await db.user.mod(sub_user, status="Disable", sql_session=sql_session)
                    for task in await db.task.list(sub_user, fields=("id",), limit=None, sql_session=sql_session):
                        await db.task.mod(task["id"], disabled=True, sql_session=sql_session)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ban users failed: %s", e, exc_info=config.traceback_print)
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"ok": True, "detail": "Users banned"})


# ---------------------------------------------------------------------------
# POST /user/manage/activate -- enable selected users
# ---------------------------------------------------------------------------


@router.post("/user/manage/activate")
async def user_manage_activate(
    request: Request,
    admin_user: dict = Depends(require_admin),
):
    """
    Enable (activate) selected users and all their tasks.

    Expects JSON body: {"user_ids": [<id>, ...], "adminmail": "...", "adminpwd": "..."}
    Mirrors the 'activatebtn' branch of UserManagerHandler.post().
    """
    db = get_db(request)
    body = await request.json()
    adminmail = body.get("adminmail", "")
    adminpwd = body.get("adminpwd", "")
    target_ids: List = body.get("user_ids", [])

    try:
        async with db.transaction() as sql_session:
            if not await db.user.challenge_md5(adminmail, adminpwd, sql_session=sql_session):
                raise HTTPException(status_code=401, detail="账号/密码错误")

            for sub_user in target_ids:
                sub_user = str(sub_user)
                row = await db.user.get(sub_user, fields=("role",), sql_session=sql_session)
                if row and row.get("role") != "admin":
                    await db.user.mod(sub_user, status="Enable", sql_session=sql_session)
                    for task in await db.task.list(sub_user, fields=("id",), limit=None, sql_session=sql_session):
                        await db.task.mod(task["id"], disabled=False, sql_session=sql_session)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("activate users failed: %s", e, exc_info=config.traceback_print)
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"ok": True, "detail": "Users activated"})


# ---------------------------------------------------------------------------
# POST /user/manage/verify -- mark users as email-verified
# ---------------------------------------------------------------------------


@router.post("/user/manage/verify")
async def user_manage_verify(
    request: Request,
    admin_user: dict = Depends(require_admin),
):
    """
    Mark selected users as email-verified.

    Expects JSON body: {"user_ids": [<id>, ...], "adminmail": "...", "adminpwd": "..."}
    Mirrors the 'verifybtn' branch of UserManagerHandler.post().
    """
    db = get_db(request)
    body = await request.json()
    adminmail = body.get("adminmail", "")
    adminpwd = body.get("adminpwd", "")
    target_ids: List = body.get("user_ids", [])

    try:
        async with db.transaction() as sql_session:
            if not await db.user.challenge_md5(adminmail, adminpwd, sql_session=sql_session):
                raise HTTPException(status_code=401, detail="账号/密码错误")

            for sub_user in target_ids:
                sub_user = str(sub_user)
                row = await db.user.get(sub_user, fields=("role",), sql_session=sql_session)
                if row and row.get("role") != "admin":
                    await db.user.mod(
                        sub_user,
                        email_verified=True,
                        mtime=time.time(),
                        sql_session=sql_session,
                    )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("verify users failed: %s", e, exc_info=config.traceback_print)
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"ok": True, "detail": "Users verified"})


# ---------------------------------------------------------------------------
# POST /user/manage/delete -- delete selected users and all their data
# ---------------------------------------------------------------------------


@router.post("/user/manage/delete")
async def user_manage_delete(
    request: Request,
    admin_user: dict = Depends(require_admin),
):
    """
    Delete selected users together with all their tasks, task logs, templates,
    and notepads.

    Expects JSON body: {"user_ids": [<id>, ...], "adminmail": "...", "adminpwd": "..."}
    Mirrors the 'delbtn' branch of UserManagerHandler.post().
    """
    db = get_db(request)
    body = await request.json()
    adminmail = body.get("adminmail", "")
    adminpwd = body.get("adminpwd", "")
    target_ids: List = body.get("user_ids", [])

    try:
        async with db.transaction() as sql_session:
            if not await db.user.challenge_md5(adminmail, adminpwd, sql_session=sql_session):
                raise HTTPException(status_code=401, detail="账号/密码错误")

            for sub_user in target_ids:
                sub_user = str(sub_user)
                row = await db.user.get(sub_user, fields=("role",), sql_session=sql_session)
                if row and row.get("role") != "admin":
                    # Delete tasks and their logs
                    for task in await db.task.list(sub_user, fields=("id",), limit=None, sql_session=sql_session):
                        await db.task.delete(task["id"], sql_session=sql_session)
                        logs = await db.tasklog.list(taskid=task["id"], fields=("id",), sql_session=sql_session)
                        for log in logs:
                            await db.tasklog.delete(log["id"], sql_session=sql_session)

                    # Delete user-owned templates
                    for tpl in await db.tpl.list(fields=("id", "userid"), limit=None, sql_session=sql_session):
                        if tpl["userid"] == int(sub_user):
                            await db.tpl.delete(tpl["id"], sql_session=sql_session)

                    # Delete notepads
                    for notepad in await db.notepad.list(
                        fields=("userid", "notepadid"),
                        limit=None,
                        userid=sub_user,
                        sql_session=sql_session,
                    ):
                        await db.notepad.delete(sub_user, notepad["notepadid"], sql_session=sql_session)

                    # Finally delete the user record
                    await db.user.delete(sub_user, sql_session=sql_session)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete users failed: %s", e, exc_info=config.traceback_print)
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"ok": True, "detail": "Users deleted"})
