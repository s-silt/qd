#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/my.py.

Original Tornado handlers:
    class MyHandler(BaseHandler):
        @addslash
        @authenticated
        async def get(self):
            ...render my.html...

    class CheckUpdateHandler(BaseHandler):
        @addslash
        @authenticated
        async def get(self):
            ...update tpl updateable flags, redirect to /my/...

Routes ported:
    GET /my/                -> user task/template dashboard (login required)
    GET /my/checkupdate     -> mark updateable tpls, redirect to /my/ (login required)
"""

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from web.fastapi.base import get_db, require_user
from web.fastapi.templates import render_template

router = APIRouter()


def _my_status(task: dict) -> str:
    """Mirror web/handlers/my.py:my_status()."""
    if task["disabled"]:
        return "停止"
    if task["last_failed_count"]:
        return "已失败%d次，重试中..." % task["last_failed_count"]
    if (task["last_failed"] or 0) > (task["last_success"] or 0):
        return "失败"
    if (
        task["success_count"] == 0
        and task["failed_count"] == 0
        and task["next"]
        and (task["next"] - time.time() < 60)
    ):
        return "正在准备执行任务"
    return "正常"


@router.get("/my/")
async def my(
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Render the user's task / template dashboard.

    Requires authentication; raises HTTP 401 if not logged in.
    If the user record no longer exists in DB the visitor is redirected to /login.
    """
    # Verify the user still exists in the DB
    if not await db.user.get(user["id"], fields=("id",)):
        return RedirectResponse(url="/login", status_code=302)

    adminflg = False
    role_row = await db.user.get(user["id"], fields=("role",))
    if role_row and role_row.get("role") == "admin":
        adminflg = True

    tpls = await db.tpl.list(
        userid=user["id"],
        fields=(
            "id", "siteurl", "sitename", "banner", "note",
            "disabled", "lock", "last_success", "ctime", "mtime",
            "fork", "_groups", "updateable", "tplurl",
        ),
        limit=None,
    )

    tasks = await db.task.list(
        user["id"],
        fields=(
            "id", "tplid", "note", "disabled", "last_success",
            "success_count", "failed_count", "last_failed", "next",
            "last_failed_count", "ctime", "_groups",
        ),
        limit=None,
    )
    for task in tasks:
        tpl = await db.tpl.get(
            task["tplid"],
            fields=("id", "userid", "sitename", "siteurl", "banner", "note"),
        )
        task["tpl"] = tpl

    # Collect unique task groups (preserving insertion order)
    _groups: list = []
    for task in tasks:
        if not isinstance(task["_groups"], str):
            task["_groups"] = str(task["_groups"])
        temp = task["_groups"]
        if temp not in _groups:
            _groups.append(temp)

    # Collect unique tpl groups
    tplgroups: list = []
    for tpl in tpls:
        temp = tpl["_groups"]
        if temp not in tplgroups:
            tplgroups.append(temp)

    return render_template(
        request,
        "my.html",
        tpls=tpls,
        tasks=tasks,
        my_status=_my_status,
        userid=user["id"],
        taskgroups=_groups,
        tplgroups=tplgroups,
        adminflg=adminflg,
    )


@router.get("/my/checkupdate")
async def my_checkupdate(
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Check for template updates and mark updateable tpls, then redirect to /my/.

    Requires authentication; raises HTTP 401 if not logged in.
    """
    async with db.transaction() as sql_session:
        tpls = await db.tpl.list(
            userid=user["id"],
            fields=("id", "mtime", "tplurl"),
            limit=None,
            sql_session=sql_session,
        )

        hjson = {}
        for h in await db.pubtpl.list(
            fields=("id", "filename", "reponame", "date", "update"),
            sql_session=sql_session,
        ):
            hjson[f'{h["filename"]}|{h["reponame"]}'] = h

        for tpl in tpls:
            if (
                tpl["tplurl"] in hjson
                and hjson[tpl["tplurl"]]["update"]
                and tpl["mtime"] < time.mktime(
                    time.strptime(hjson[tpl["tplurl"]]["date"], "%Y-%m-%d %H:%M:%S")
                )
            ):
                await db.tpl.mod(tpl["id"], updateable=1, sql_session=sql_session)

    return RedirectResponse(url="/my/", status_code=302)
