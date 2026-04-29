#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/tpl.py.

Original Tornado handlers:
  - TPLPushHandler    GET/POST  /tpl/<tplid>/push
  - TPLVarHandler     GET       /tpl/<tplid>/var
  - TPLDelHandler     POST      /tpl/<tplid>/del
  - TPLRunHandler     POST      /tpl/<tplid>/run  (also /tpl//run)
  - PublicTPLHandler  GET       /tpls/public
  - TPLGroupHandler   GET/POST  /tpl/<tplid>/group

Routes registered here:
  GET  /tpl/{tplid}/push   — push request form
  POST /tpl/{tplid}/push   — submit push request
  GET  /tpl/{tplid}/var    — template variable viewer
  POST /tpl/{tplid}/del    — delete template
  POST /tpl/{tplid}/run    — run template
  POST /tpl/run            — run template without tplid
  GET  /tpls/public        — list public templates
  GET  /tpl/{tplid}/group  — group assignment form
  POST /tpl/{tplid}/group  — update group assignment
"""

import json
from codecs import escape_decode

from libs.parse_url import parse_url

try:
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from fastapi.exceptions import HTTPException
except ImportError:
    APIRouter = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    Request = object  # type: ignore
    HTMLResponse = object  # type: ignore
    RedirectResponse = object  # type: ignore
    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            self.status_code = status_code
            self.detail = detail
    HTTPException = _HTTPException  # type: ignore

import config
from libs.log import Log  # noqa: does not require pbkdf2
from web.fastapi.base import (
    get_db,
    get_fetcher,
    get_current_user,
    require_user,
    check_permission,
    permission,
)
from web.fastapi.templates import render_template

logger = Log("QD.FastAPI.Tpl").getlogger()

router = APIRouter()


# ---------------------------------------------------------------------------
# TPLPushHandler
# ---------------------------------------------------------------------------

@router.get("/tpl/{tplid}/push")
async def tpl_push_get(
    tplid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Render push request form (mirrors TPLPushHandler.get)."""
    tpl = await db.tpl.get(tplid, fields=('id', 'userid', 'sitename'))
    if not permission(tpl, 'w', user):
        raise HTTPException(status_code=403, detail='没有权限')
    tpls = await db.tpl.list(userid=None, limit=None, fields=('id', 'sitename', 'public'))
    for i, _ in enumerate(tpls):
        if tpls[i]['public'] == 2:
            tpls[i]['sitename'] += ' [已取消]'
    return render_template(request, 'tpl_push.html', tpl=tpl, tpls=tpls)


@router.post("/tpl/{tplid}/push")
async def tpl_push_post(
    tplid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Submit a push request (mirrors TPLPushHandler.post)."""
    form = await request.form()
    totpl = int(form.get('totpl', 0))
    msg = form.get('msg', '')

    async with db.transaction() as sql_session:
        tpl = await db.tpl.get(tplid, fields=('id', 'userid'), sql_session=sql_session)
        if not permission(tpl, 'w', user):
            raise HTTPException(status_code=403, detail='没有权限')

        to_tplid = totpl
        if to_tplid == 0:
            to_tplid = None
            to_userid = None
        else:
            totpl_obj = await db.tpl.get(to_tplid, fields=('id', 'userid'), sql_session=sql_session)
            if not totpl_obj:
                raise HTTPException(status_code=404, detail='模板不存在')
            to_userid = totpl_obj['userid']

        await db.push_request.add(
            from_tplid=tpl['id'],
            from_userid=user['id'],
            to_tplid=to_tplid,
            to_userid=to_userid,
            msg=msg,
            sql_session=sql_session,
        )
        await db.tpl.mod(tpl['id'], lock=True, sql_session=sql_session)

    return RedirectResponse(url='/pushs', status_code=303)


# ---------------------------------------------------------------------------
# TPLVarHandler
# ---------------------------------------------------------------------------

@router.get("/tpl/{tplid}/var")
async def tpl_var_get(
    tplid: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Render template variable viewer (mirrors TPLVarHandler.get)."""
    tpl = await db.tpl.get(
        tplid,
        fields=('id', 'note', 'userid', 'sitename', 'siteurl', 'variables', 'init_env'),
    )
    if not permission(tpl, 'r', user):
        raise HTTPException(status_code=403, detail='没有权限')
    if not tpl['init_env']:
        tpl['init_env'] = '{}'
    return render_template(
        request,
        'task_new_var.html',
        tpl=tpl,
        variables=json.loads(tpl['variables']),
        init_env=json.loads(tpl['init_env']),
    )


# ---------------------------------------------------------------------------
# TPLDelHandler
# ---------------------------------------------------------------------------

@router.post("/tpl/{tplid}/del")
async def tpl_del_post(
    tplid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Delete a template (mirrors TPLDelHandler.post)."""
    async with db.transaction() as sql_session:
        tpl = check_permission(
            await db.tpl.get(tplid, fields=('id', 'userid', 'public'), sql_session=sql_session),
            'w',
            user,
        )
        if tpl['public'] == 1:
            prs = await db.push_request.list(to_tplid=tplid, fields=('id',), sql_session=sql_session)
            for pr in prs:
                await db.push_request.mod(pr['id'], status=db.push_request.CANCEL, sql_session=sql_session)
        await db.tpl.delete(tplid, sql_session=sql_session)

    return RedirectResponse(url='/my/', status_code=303)


# ---------------------------------------------------------------------------
# TPLRunHandler
# ---------------------------------------------------------------------------

async def _tpl_run(tplid, request: Request, user, db, fetcher):
    """Shared logic for tpl run (with or without tplid)."""
    data = {}
    body = await request.body()
    content_type = request.headers.get('content-type', '')
    if 'json' in content_type:
        try:
            body_clean = body.replace(b'\xc2\xa0', b' ')
            data = json.loads(body_clean)
        except Exception as e:
            logger.debug('TPLRunHandler body parse error: %s', e, exc_info=config.traceback_print)

    # Resolve tplid from path, body or query
    effective_tplid = tplid or data.get('tplid') or request.query_params.get('_binux_tplid')
    tpl = {}
    fetch_tpl = None

    async with db.transaction() as sql_session:
        if effective_tplid:
            tpl = check_permission(
                await db.tpl.get(
                    effective_tplid,
                    fields=('id', 'userid', 'sitename', 'siteurl', 'tpl', 'interval', 'last_success'),
                    sql_session=sql_session,
                )
            )
            fetch_tpl = await db.user.decrypt(tpl['userid'], tpl['tpl'], sql_session=sql_session)

        if not fetch_tpl:
            fetch_tpl = data.get('tpl')

        if not fetch_tpl:
            tpl_param = request.query_params.get('tpl')
            if tpl_param:
                try:
                    fetch_tpl = json.loads(tpl_param)
                except Exception as e:
                    logger.debug("parse json error: %s", e, exc_info=config.traceback_print)
                    if not user:
                        return render_template(request, 'tpl_run_failed.html', log="请先登录!")
                    raise HTTPException(status_code=400, detail="Bad Request") from e

        env = data.get('env')
        if not env:
            env_param = request.query_params.get('env')
            if env_param:
                try:
                    env = dict(variables=json.loads(env_param), session=[])
                except Exception as e:
                    logger.debug("parse json error: %s", e, exc_info=config.traceback_print)
                    raise HTTPException(status_code=400, detail="Bad Request") from e

        try:
            proxy_url = env['variables'].get('_binux_proxy') if env and env.get('variables') else None
            url = parse_url(proxy_url) if proxy_url else None
            if url:
                proxy = {
                    'scheme': url['scheme'],
                    'host': url['host'],
                    'port': url['port'],
                    'username': url['username'],
                    'password': url['password'],
                }
                result, _ = await fetcher.do_fetch(fetch_tpl, env, [proxy])
            elif user:
                result, _ = await fetcher.do_fetch(fetch_tpl, env)
            else:
                result, _ = await fetcher.do_fetch(fetch_tpl, env, proxies=[])
        except Exception as e:
            uid = user.get('id', -1) if user else -1
            logger.error(
                'UserID:%d tplID:%d failed! \r\n%s',
                uid or -1,
                int(effective_tplid or -1),
                str(e).replace('\\r\\n', '\r\n'),
                exc_info=config.traceback_print,
            )
            return render_template(request, 'tpl_run_failed.html', log=str(e))

        if tpl:
            await db.tpl.incr_success(tpl['id'], sql_session=sql_session)

    return render_template(request, 'tpl_run_success.html', log=result.get('variables', {}).get('__log__'))


@router.post("/tpl/{tplid}/run")
async def tpl_run_with_id(
    tplid: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    fetcher=Depends(get_fetcher),
):
    """Run a template by ID (mirrors TPLRunHandler.post with tplid)."""
    return await _tpl_run(tplid, request, user, db, fetcher)


@router.post("/tpl/run")
async def tpl_run_no_id(
    request: Request,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    fetcher=Depends(get_fetcher),
):
    """Run a template without a stored tplid (mirrors TPLRunHandler.post without tplid)."""
    return await _tpl_run(None, request, user, db, fetcher)


# ---------------------------------------------------------------------------
# PublicTPLHandler
# ---------------------------------------------------------------------------

@router.get("/tpls/public")
async def public_tpls_get(
    request: Request,
    db=Depends(get_db),
):
    """List public templates sorted by success count (mirrors PublicTPLHandler.get)."""
    tpls = await db.tpl.list(
        userid=None,
        public=1,
        limit=None,
        fields=('id', 'siteurl', 'sitename', 'banner', 'note', 'disabled', 'lock',
                'last_success', 'ctime', 'mtime', 'fork', 'success_count'),
    )
    tpls = sorted(tpls, key=lambda t: -t['success_count'])
    return render_template(request, 'tpls_public.html', tpls=tpls)


# ---------------------------------------------------------------------------
# TPLGroupHandler
# ---------------------------------------------------------------------------

@router.get("/tpl/{tplid}/group")
async def tpl_group_get(
    tplid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Render group assignment form (mirrors TPLGroupHandler.get)."""
    group_now = (await db.tpl.get(tplid, fields=('_groups',)))['_groups']
    _groups = []
    tpls = await db.tpl.list(userid=user['id'], fields=('_groups',), limit=None)
    for tpl in tpls:
        temp = tpl['_groups']
        if temp not in _groups:
            _groups.append(temp)

    return render_template(
        request,
        'tpl_setgroup.html',
        tplid=tplid,
        _groups=_groups,
        groupNow=group_now,
    )


@router.post("/tpl/{tplid}/group")
async def tpl_group_post(
    tplid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Update template group assignment (mirrors TPLGroupHandler.post)."""
    form = await request.form()
    envs = {}
    for key in form.keys():
        envs[key] = form.getlist(key)

    new_group = envs.get('New_group', [''])[0].strip()

    if new_group != "":
        target_group = new_group
    else:
        target_group = 'None'
        for key, value in envs.items():
            if value[0] == 'on':
                target_group = escape_decode(key.strip()[2:-1], "hex-escape")[0].decode('utf-8')
                break

    await db.tpl.mod(tplid, _groups=target_group)
    return RedirectResponse(url='/my/', status_code=303)
