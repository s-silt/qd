#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/index.py.

Original Tornado handler:
    class IndexHandlers(BaseHandler):
        async def get(self):
            if self.current_user:
                self.redirect('/my/')
                return
            tplid = self.get_argument('tplid', None)
            ...
            return await self.render('index.html', ...)

Routes ported:
    GET /    -> index page (redirect to /my/ if logged in, else show template list)
"""

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from web.fastapi.base import check_permission, get_current_user, get_db
from web.fastapi.templates import render_template

router = APIRouter()


@router.get("/")
async def index(
    request: Request,
    tplid: str = None,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Render the QD index / landing page.

    If the user is already logged in they are redirected to /my/.
    Otherwise the public template list is shown so a visitor can pick a
    template to create a task from.
    """
    if user:
        return RedirectResponse(url="/my/", status_code=302)

    fields = ("id", "sitename", "success_count")
    tpls = sorted(
        await db.tpl.list(userid=None, fields=fields, limit=None),
        key=lambda t: -t["success_count"],
    )
    if not tpls:
        return render_template(request, "index.html", tpls=[], tplid=0, tpl=None, variables=[])

    if not tplid:
        for tpl in tpls:
            if tpl.get("id"):
                tplid = tpl["id"]
                break
    tplid = int(tplid)
    tpl = check_permission(
        await db.tpl.get(tplid, fields=("id", "userid", "sitename", "siteurl", "note", "variables", "init_env")),
        user=user,
    )
    variables = json.loads(tpl["variables"])
    if not tpl["init_env"]:
        tpl["init_env"] = "{}"

    return render_template(
        request,
        "index.html",
        tpls=tpls,
        tplid=tplid,
        tpl=tpl,
        variables=variables,
        init_env=json.loads(tpl["init_env"]),
    )
