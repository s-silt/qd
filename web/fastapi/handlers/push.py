#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/push.py.

Original Tornado handlers:
  - PushListHandler    GET   /pushs/<status>?
  - PushActionHandler  POST  /push/<prid>/<action>
  - PushViewHandler    GET   /push/<prid>/view
                       POST  /push/<prid>/view

Routes registered here:
  GET  /pushs              — list push requests
  GET  /pushs/{status}     — list push requests filtered by status
  POST /push/{prid}/{action}  — accept/refuse/cancel a push request
  GET  /push/{prid}/view   — view push request (HAR editor)
  POST /push/{prid}/view   — fetch push request tpl data as JSON
"""

import json
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.exceptions import HTTPException

from web.fastapi.base import (
    get_db,
    get_fetcher,
    get_current_user,
    require_user,
)
from web.fastapi.templates import render_template

router = APIRouter()


# ---------------------------------------------------------------------------
# PushListHandler
# ---------------------------------------------------------------------------

@router.get("/pushs")
@router.get("/pushs/{status}")
async def push_list_get(
    request: Request,
    status: int = None,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """List push and pull requests for the current user (mirrors PushListHandler.get)."""
    isadmin = user['isadmin']

    async def get_user(userid):
        if not userid:
            return dict(nickname='公开', email=None, email_verified=True)
        if isadmin:
            u = await db.user.get(userid, fields=('id', 'nickname', 'email', 'email_verified'))
        else:
            u = await db.user.get(userid, fields=('id', 'nickname'))
        if not u:
            return dict(nickname='公开', email=None, email_verified=False)
        return u

    async def get_tpl(tplid):
        if not tplid:
            return {}
        tpl = await db.tpl.get(
            tplid,
            fields=('id', 'userid', 'sitename', 'siteurl', 'banner', 'note', 'ctime', 'mtime', 'last_success'),
        )
        return tpl or {}

    async def join(pr):
        pr['from_user'] = await get_user(pr['from_userid'])
        pr['to_user'] = await get_user(pr['to_userid'])
        pr['from_tpl'] = await get_tpl(pr['from_tplid'])
        pr['to_tpl'] = await get_tpl(pr['to_tplid'])
        return pr

    _f = {}
    if status is not None:
        _f['status'] = status

    pushs = []
    for each in await db.push_request.list(from_userid=user['id'], **_f):
        pushs.append(await join(each))
    if isadmin:
        for each in await db.push_request.list(from_userid=None, **_f):
            pushs.append(await join(each))

    pulls = []
    for each in await db.push_request.list(to_userid=user['id'], **_f):
        pulls.append(await join(each))
    if isadmin:
        for each in await db.push_request.list(to_userid=None, **_f):
            pulls.append(await join(each))

    return render_template(request, 'push_list.html', pushs=pushs, pulls=pulls)


# ---------------------------------------------------------------------------
# PushActionHandler
# ---------------------------------------------------------------------------

async def _push_accept(db, fetcher, pr, sql_session=None):
    """Accept a push request: re-encrypt tpl and create/update destination tpl."""
    tplobj = await db.tpl.get(
        pr['from_tplid'],
        fields=('id', 'userid', 'tpl', 'variables', 'sitename', 'siteurl', 'note', 'banner', 'interval', 'init_env'),
        sql_session=sql_session,
    )
    if not tplobj:
        await _push_cancel(db, pr, sql_session=sql_session)
        raise HTTPException(status_code=404, detail="Not Found")

    # Re-encrypt for destination user
    tpl_decrypted = await db.user.decrypt(pr['from_userid'], tplobj['tpl'], sql_session=sql_session)
    har = await db.user.encrypt(pr['to_userid'], fetcher.tpl2har(tpl_decrypted), sql_session=sql_session)
    tpl_encrypted = await db.user.encrypt(pr['to_userid'], tpl_decrypted, sql_session=sql_session)
    tplid = None

    if not pr['to_tplid']:
        tplid = await db.tpl.add(
            userid=pr['to_userid'],
            har=har,
            tpl=tpl_encrypted,
            variables=tplobj['variables'],
            init_env=tplobj['init_env'],
            interval=tplobj['interval'],
            sql_session=sql_session,
        )
        await db.tpl.mod(
            tplid,
            public=1,
            sitename=tplobj['sitename'],
            siteurl=tplobj['siteurl'],
            banner=tplobj['banner'],
            note=tplobj['note'],
            fork=pr['from_tplid'],
            sql_session=sql_session,
        )
    else:
        tplid = pr['to_tplid']
        await db.tpl.mod(
            tplid,
            har=har,
            tpl=tpl_encrypted,
            public=1,
            variables=tplobj['variables'],
            init_env=tplobj['init_env'],
            interval=tplobj['interval'],
            sitename=tplobj['sitename'],
            siteurl=tplobj['siteurl'],
            banner=tplobj['banner'],
            note=tplobj['note'],
            fork=pr['from_tplid'],
            mtime=time.time(),
            sql_session=sql_session,
        )

    if tplid:
        await db.push_request.mod(pr['id'], to_tplid=tplid, status=db.push_request.ACCEPT, sql_session=sql_session)
    else:
        await db.push_request.mod(pr['id'], status=db.push_request.ACCEPT, sql_session=sql_session)


async def _push_cancel(db, pr, sql_session=None):
    """Cancel a push request."""
    if pr['to_tplid'] and pr['status'] == db.push_request.ACCEPT:
        await db.tpl.mod(pr['to_tplid'], public=2, sql_session=sql_session)
    await db.push_request.mod(pr['id'], status=db.push_request.CANCEL, sql_session=sql_session)


async def _push_refuse(db, pr, reject_message=None, sql_session=None):
    """Refuse a push request."""
    await db.push_request.mod(pr['id'], status=db.push_request.REFUSE, sql_session=sql_session)
    if reject_message:
        await db.push_request.mod(pr['id'], msg=reject_message, sql_session=sql_session)


@router.post("/push/{prid}/{action}")
async def push_action_post(
    prid: int,
    action: str,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
    fetcher=Depends(get_fetcher),
):
    """Accept, refuse or cancel a push request (mirrors PushActionHandler.post)."""
    if action not in ('accept', 'refuse', 'cancel'):
        raise HTTPException(status_code=400, detail="Bad Request")

    async with db.transaction() as sql_session:
        pr = await db.push_request.get(prid, sql_session=sql_session)
        if not pr:
            raise HTTPException(status_code=404, detail="Not Found")

        if pr['status'] != db.push_request.PENDING:
            if action != 'cancel':
                raise HTTPException(status_code=400, detail="Bad Request")

        if action in ('accept', 'refuse'):
            if pr['to_userid'] != user['id'] and not (not pr['to_userid'] and user['isadmin']):
                raise HTTPException(status_code=401, detail="Unauthorized")
        elif action == 'cancel':
            if pr['from_userid'] != user['id'] and not (not pr['from_userid'] and user['isadmin']):
                raise HTTPException(status_code=401, detail="Unauthorized")

        if action == 'accept':
            await _push_accept(db, fetcher, pr, sql_session=sql_session)
            status = db.push_request.ACCEPT
        elif action == 'refuse':
            # Only parse form when we need the optional reject message
            try:
                form = await request.form()
                prompt = form.get('prompt', None)
            except Exception:
                prompt = None
            await _push_refuse(db, pr, reject_message=prompt, sql_session=sql_session)
            status = db.push_request.REFUSE
        else:  # cancel
            await _push_cancel(db, pr, sql_session=sql_session)
            status = db.push_request.CANCEL

        tpl_lock = len(list(await db.push_request.list(
            from_tplid=pr['from_tplid'],
            status=status,
            sql_session=sql_session,
        ))) == 0
        if not tpl_lock:
            await db.tpl.mod(pr['from_tplid'], lock=False, sql_session=sql_session)

    return RedirectResponse(url='/pushs', status_code=303)


# ---------------------------------------------------------------------------
# PushViewHandler
# ---------------------------------------------------------------------------

@router.get("/push/{prid}/view")
async def push_view_get(
    prid: int,
    request: Request,
    user: dict = Depends(require_user),
):
    """Render the HAR editor for a push request (mirrors PushViewHandler.get)."""
    return render_template(request, 'har/editor.html')


@router.post("/push/{prid}/view")
async def push_view_post(
    prid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
    fetcher=Depends(get_fetcher),
):
    """Return the tpl data for a push request as JSON (mirrors PushViewHandler.post)."""
    pr = await db.push_request.get(
        prid,
        fields=('id', 'from_tplid', 'from_userid', 'to_tplid', 'to_userid', 'status'),
    )
    if not pr:
        raise HTTPException(status_code=404, detail="Not Found")

    if pr['status'] not in (db.push_request.PENDING, db.push_request.ACCEPT):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Permission check: must be from/to user, or admin for public requests
    allowed = (
        pr['to_userid'] == user['id']
        or pr['from_userid'] == user['id']
        or (not pr['to_userid'] and user['isadmin'])
        or (not pr['from_userid'] and user['isadmin'])
    )
    if not allowed:
        raise HTTPException(status_code=401, detail="Unauthorized")

    tplid = None
    userid = None
    if pr['to_tplid'] and pr['status'] != db.push_request.PENDING:
        tplid = pr['to_tplid']
        userid = pr['to_userid']
    else:
        tplid = pr['from_tplid']
        userid = pr['from_userid']

    tpl = await db.tpl.get(
        tplid,
        fields=('id', 'userid', 'sitename', 'siteurl', 'banner', 'note', 'tpl', 'variables'),
    )
    if not tpl:
        raise HTTPException(status_code=404, detail="Not Found")

    tpl['har'] = fetcher.tpl2har(await db.user.decrypt(userid, tpl['tpl']))
    tpl['variables'] = json.loads(tpl['variables'])

    return JSONResponse(dict(
        filename=tpl['sitename'] or '未命名模板',
        har=tpl['har'],
        env=dict((x, '') for x in tpl['variables']),
        setting=dict(
            sitename=tpl['sitename'],
            siteurl=tpl['siteurl'],
            banner=tpl['banner'],
            note=tpl['note'],
        ),
        readonly=True,
    ))
