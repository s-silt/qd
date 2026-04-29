#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of the HAR editor / test / save subset of web/handlers/har.py.

Routes ported:
    GET  /har/edit            -> HAREditor.get   (anonymous allowed)
    GET  /tpl/{id}/edit       -> HAREditor.get   (anonymous allowed, loads tpl)
    POST /tpl/{id}/edit       -> HAREditor.post  (login required, returns tpl JSON)
    POST /har/test            -> HARTest.post    (rate-limited, anonymous allowed)
    POST /har/save            -> HARSave.post    (login required, creates tpl)
    POST /tpl/{id}/save       -> HARSave.post    (login required, updates tpl)

NOT ported here (handled by har_ai.py or another agent):
    /har/ai_analyze, /har/ai_status, /har/auto_capture, /har/auto_capture_status

NOTE: heavy imports (Fetcher, utils, jinja2 meta) are deferred to request-time
so the module can be imported in environments that lack optional C-extension
packages (e.g. pbkdf2) -- same pattern as web/fastapi_app.py.
"""

import json
import re
import time
from io import BytesIO
from typing import Optional, Sequence

import config
from libs.log import Log
from libs.parse_url import parse_url
from libs.safe_eval import safe_eval

try:
    from fastapi import APIRouter, Depends, HTTPException, Request
    from fastapi.responses import JSONResponse
except ImportError:
    APIRouter = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    Request = object  # type: ignore
    JSONResponse = object  # type: ignore

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code: int = 500, detail: str = ""):
            self.status_code = status_code
            self.detail = detail

from web.fastapi.base import (
    check_permission,
    get_current_user,
    get_db,
    get_fetcher,
    require_user,
    _get_ip,
)
from web.fastapi.templates import render_template

logger = Log("QD.FastAPI.HAREditor").getlogger()

router = APIRouter()

_save_env = None


def _get_save_env():
    """Return the Jinja2 environment used for HAR variable extraction.

    Tries to use Fetcher().jinja_env (SandboxedEnvironment with QD globals) first.
    Falls back to a plain SandboxedEnvironment when optional packages (e.g. pbkdf2)
    are absent, so the handler can still be imported and tested without a full install.
    """
    global _save_env
    if _save_env is None:
        try:
            from libs.fetcher import Fetcher  # noqa: PLC0415
            _save_env = Fetcher().jinja_env
        except ImportError:
            from jinja2.sandbox import SandboxedEnvironment  # noqa: PLC0415
            _save_env = SandboxedEnvironment()
    return _save_env


_LOOP_EXTRACTED = frozenset((
    "loop_index0", "loop_index", "loop_first", "loop_last",
    "loop_length", "loop_revindex0", "loop_revindex", "loop_depth", "loop_depth0",
))


@router.get("/har/edit")
async def har_edit_get(request: Request, db=Depends(get_db)):
    """Render the HAR editor page (new template, no tplid)."""
    tplurl = request.query_params.get("tplurl", "|").split("|")
    harname = request.query_params.get("name", tplurl[0] if tplurl else "")
    reponame = request.query_params.get("reponame", tplurl[1] if len(tplurl) > 1 else "")

    if reponame and harname:
        tpl_list = await db.pubtpl.list(
            filename=harname, reponame=reponame,
            fields=("id", "name", "content", "comments"),
        )
        if tpl_list:
            hardata = tpl_list[0]["content"]
            harnote = tpl_list[0]["comments"]
        else:
            return render_template(request, "tpl_run_failed.html", log="此模板不存在")
    else:
        hardata = ""
        harnote = ""

    return render_template(
        request, "har/editor.html",
        tplid=None, harpath=reponame, harname=harname,
        hardata=hardata, harnote=harnote,
    )


@router.get("/tpl/{tplid}/edit")
async def tpl_edit_get(tplid: int, request: Request, db=Depends(get_db)):
    """Render the HAR editor for an existing template."""
    tplurl = request.query_params.get("tplurl", "|").split("|")
    harname = request.query_params.get("name", tplurl[0] if tplurl else "")
    reponame = request.query_params.get("reponame", tplurl[1] if len(tplurl) > 1 else "")

    if reponame and harname:
        tpl_list = await db.pubtpl.list(
            filename=harname, reponame=reponame,
            fields=("id", "name", "content", "comments"),
        )
        if tpl_list:
            hardata = tpl_list[0]["content"]
            harnote = tpl_list[0]["comments"]
        else:
            return render_template(request, "tpl_run_failed.html", log="此模板不存在")
    else:
        hardata = ""
        harnote = ""

    return render_template(
        request, "har/editor.html",
        tplid=tplid, harpath=reponame, harname=harname,
        hardata=hardata, harnote=harnote,
    )


@router.post("/tpl/{tplid}/edit")
async def tpl_edit_post(
    tplid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Return template data as JSON for the HAR editor (mirrors HAREditor.post)."""
    taskid = request.query_params.get("taskid", "")

    async with db.transaction() as sql_session:
        tpl = check_permission(
            await db.tpl.get(
                tplid,
                fields=(
                    "id", "userid", "sitename", "siteurl", "banner", "note",
                    "interval", "har", "variables", "lock", "init_env",
                ),
                sql_session=sql_session,
            ),
            "r",
            user,
        )

        tpl["har"] = await db.user.decrypt(tpl["userid"], tpl["har"], sql_session=sql_session)
        tpl["variables"] = json.loads(tpl["variables"])
        if not tpl["init_env"]:
            tpl["init_env"] = "{}"
        envs = json.loads(tpl["init_env"])
        if taskid:
            task = await db.task.get(taskid, sql_session=sql_session)
            if task and task.get("init_env"):
                task_envs = await db.user.decrypt(
                    user["id"], task["init_env"], sql_session=sql_session
                )
                envs.update(task_envs)

    readonly = not tpl["userid"] or not _can_write(tpl, user) or tpl["lock"]

    return {
        "filename": tpl["sitename"] or "未命名模板",
        "har": tpl["har"],
        "env": {x: envs[x] if x in envs else "" for x in tpl["variables"]},
        "setting": {
            "sitename": tpl["sitename"],
            "siteurl": tpl["siteurl"],
            "note": tpl["note"],
            "banner": tpl["banner"],
            "interval": tpl["interval"] or "",
        },
        "readonly": readonly,
    }


def _can_write(obj: Optional[dict], user: Optional[dict]) -> bool:
    """Mirror BaseHandler.permission(obj, 'w')."""
    if not obj or "userid" not in obj:
        return False
    if not obj["userid"]:
        return bool(user and user.get("isadmin"))
    return bool(user and obj["userid"] == user.get("id"))


@router.post("/har/test")
async def har_test(
    request: Request,
    user: Optional[dict] = Depends(get_current_user),
    db=Depends(get_db),
    fetcher=Depends(get_fetcher),
):
    """Execute a single HAR request and return the result (mirrors HARTest.post)."""
    from libs import json_typing  # noqa: PLC0415
    from tornado import httpclient  # noqa: PLC0415

    if not config.debug and db:
        ip = _get_ip(request)
        try:
            db.redis.evil(ip, user.get("id") if user else None, 1)
        except Exception as _e:
            logger.debug("evil counter error: %s", _e, exc_info=config.traceback_print)

    body = await request.body()
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            body = body.replace(b"\xc2\xa0", b" ")
        except Exception as _e:
            logger.debug("HARTest Replace error: %s", _e)

    try:
        data: json_typing.HARTest = json.loads(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON: {e}") from e

    url = data["request"]["url"].strip()[:1200]

    FOR_START = re.compile(
        r"^{%\s*for\s+(\w+)\s+in\s+(\w+|list\([\s\S]*\)|range\([\s\S]*\))\s*%}"
    ).match(url)
    WHILE_START = re.compile(r"^{%\s*while\s+([\s\S]*)\s*%}").match(url)
    IF_START = re.compile(r"^{%\s*if\s+(.+)\s*%}").match(url)
    ELSE_START = re.compile(r"^{%\s*else\s*%}").match(url)
    PARSE_END = re.compile(r"^{%\s*end(for|if)\s*%}").match(url)

    if FOR_START or WHILE_START or IF_START or ELSE_START or PARSE_END:
        tmp = {"env": data["env"], "rule": data["rule"]}
        tmp["request"] = {
            "method": "GET",
            "url": "api://util/unicode?content=",
            "headers": [],
            "cookies": [],
        }
        req, rule, env = fetcher.build_request(tmp)

        if FOR_START:
            _target = FOR_START.group(1)
            _from_var = FOR_START.group(2)
            _from = env["variables"].get(_from_var, [])
            try:
                if _from_var.startswith(("list(", "range(")):
                    _from = safe_eval(_from_var, env["variables"])
                if not isinstance(_from, Sequence):
                    raise Exception("for 循环只支持可迭代类型及变量")
                env["variables"]["loop_index0"] = str(env["variables"].get("loop_index0", 0))
                env["variables"]["loop_index"] = str(env["variables"].get("loop_index", 1))
                env["variables"]["loop_first"] = str(env["variables"].get("loop_first", True))
                env["variables"]["loop_last"] = str(env["variables"].get("loop_last", False))
                env["variables"]["loop_length"] = str(env["variables"].get("loop_length", len(_from)))
                env["variables"]["loop_revindex0"] = str(
                    env["variables"].get("loop_revindex0", len(_from) - 1)
                )
                env["variables"]["loop_revindex"] = str(
                    env["variables"].get("loop_revindex", len(_from))
                )
                res = (
                    f"循环内赋值变量: {_target}, 循环列表变量: {_from_var}, "
                    f"循环次数: {len(_from)}, \r\n循环列表内容: {list(_from)}."
                )
                code = 200
            except NameError as e:
                logger.debug("for 循环变量错误: %s", e, exc_info=config.traceback_print)
                res = f"循环变量错误: {e}"
                code = 500
            except ValueError as e:
                code = 500
                if str(e).startswith("<class 'NameError'>:"):
                    e_str = str(e).replace("<class 'NameError'>", "NameError")
                    logger.debug("for 循环变量错误: %s", e_str, exc_info=config.traceback_print)
                    res = f"循环变量错误: {e_str}"
                else:
                    e_str = str(e).replace("<class 'ValueError'>", "ValueError")
                    logger.debug("for 循环错误: %s", e_str, exc_info=config.traceback_print)
                    res = f"for 循环错误: {e_str}"
            except Exception as e:
                logger.debug("for 循环错误: %s", e, exc_info=config.traceback_print)
                res = f"for 循环错误: {e}"
                code = 500
            res += "\r\n此页面仅用于显示循环信息, 禁止在此页面提取变量"
            response = httpclient.HTTPResponse(
                request=req, code=code, buffer=BytesIO(str(res).encode())
            )

        elif WHILE_START:
            try:
                env["variables"]["loop_index0"] = str(env["variables"].get("loop_index0", 0))
                env["variables"]["loop_index"] = str(env["variables"].get("loop_index", 1))
                condition = safe_eval(WHILE_START.group(1), env["variables"])
                condition = (
                    "while 循环判断结果: true" if condition else "while 循环判断结果: false"
                )
                code = 200
            except NameError as e:
                logger.debug(
                    "while 循环判断结果: false, error: %s", e, exc_info=config.traceback_print
                )
                condition = "while 循环判断结果: false"
                code = 200
            except ValueError as e:
                if len(str(e)) > 20 and str(e)[:20] == "<class 'NameError'>:":
                    logger.debug(
                        "while 循环判断结果: false, error: %s", e, exc_info=config.traceback_print
                    )
                    condition = "while 循环判断结果: false"
                    code = 200
                else:
                    logger.debug(
                        "while 循环条件错误: %s, 条件: %s",
                        e, WHILE_START.group(1), exc_info=config.traceback_print,
                    )
                    e_str = str(e).replace("<class 'ValueError'>", "ValueError")
                    condition = f"while 循环条件错误: {e_str}\r\n条件表达式: {WHILE_START.group(1)}"
                    code = 500
            except Exception as e:
                logger.debug(
                    "while 循环条件错误: %s, 条件: %s",
                    e, WHILE_START.group(1), exc_info=config.traceback_print,
                )
                condition = f"while 循环条件错误: {e}\r\n条件表达式: {WHILE_START.group(1)}"
                code = 500
            condition += "\r\n此页面仅用于显示循环判断结果, 禁止在此页面提取变量"
            response = httpclient.HTTPResponse(
                request=req, code=code, buffer=BytesIO(str(condition).encode())
            )

        elif IF_START:
            try:
                condition = safe_eval(IF_START.group(1), env["variables"])
                condition = "判断结果: true" if condition else "判断结果: false"
                code = 200
            except NameError as e:
                logger.debug("判断结果: false, error: %s", e, exc_info=config.traceback_print)
                condition = "判断结果: false"
                code = 200
            except ValueError as e:
                if len(str(e)) > 20 and str(e)[:20] == "<class 'NameError'>:":
                    logger.debug("判断结果: false, error: %s", e, exc_info=config.traceback_print)
                    condition = "判断结果: false"
                    code = 200
                else:
                    logger.debug(
                        "判断条件错误: %s, 条件: %s",
                        e, IF_START.group(1), exc_info=config.traceback_print,
                    )
                    e_str = str(e).replace("<class 'ValueError'>", "ValueError")
                    condition = f"判断条件错误: {e_str}\r\n条件表达式: {IF_START.group(1)}"
                    code = 500
            except Exception as e:
                logger.debug(
                    "判断条件错误: %s, 条件: %s",
                    e, IF_START.group(1), exc_info=config.traceback_print,
                )
                condition = f"判断条件错误: {e}\r\n条件表达式: {IF_START.group(1)}"
                code = 500
            condition += "\r\n此页面仅用于显示判断结果, 禁止在此页面提取变量"
            response = httpclient.HTTPResponse(
                request=req, code=code, buffer=BytesIO(str(condition).encode())
            )

        else:
            exc = httpclient.HTTPError(405, "结束等控制语句不支持在单条请求中测试")
            response = httpclient.HTTPResponse(
                request=req, code=exc.code, reason=exc.message,
                buffer=BytesIO(str(exc).encode()),
            )

        env["session"].extract_cookies_to_jar(response.request, response)
        success, _ = fetcher.run_rule(response, rule, env)
        result = {
            "success": success,
            "har": fetcher.response2har(response),
            "env": {"variables": env["variables"], "session": env["session"].to_json()},
        }

    else:
        _proxy = parse_url(data["env"]["variables"].get("_proxy", ""))
        if _proxy:
            proxy = {
                "scheme": _proxy["scheme"],
                "host": _proxy["host"],
                "port": _proxy["port"],
                "username": _proxy["username"],
                "password": _proxy["password"],
            }
            ret = await fetcher.fetch(data, proxy=proxy)
        else:
            ret = await fetcher.fetch(data)

        result = {
            "success": ret["success"],
            "har": fetcher.response2har(ret["response"]),
            "env": {
                "variables": ret["env"]["variables"],
                "session": ret["env"]["session"].to_json(),
            },
        }

    return result


def _get_variables(env, tpl):
    """Mirror HARSave.get_variables() -- extract undeclared Jinja2 variables."""
    from jinja2 import meta  # noqa: PLC0415

    try:
        from libs import utils  # noqa: PLC0415
        extracted = set(utils.jinja_globals.keys())
    except ImportError:
        # pbkdf2 or other optional package absent (e.g. in test env)
        extracted = set()

    variables = set()

    for entry in tpl:
        req = entry["request"]
        rule = entry["rule"]
        var = set()

        def _get(obj, key):
            if not obj.get(key):
                return
            try:
                ast = env.parse(obj[key])
            except Exception as e:
                logger.debug(
                    "Parse %s from env failed: %s", obj[key], e,
                    exc_info=config.traceback_print,
                )
                return
            var.update(meta.find_undeclared_variables(ast))

        _get(req, "method")
        _get(req, "url")
        _get(req, "data")
        for header in req["headers"]:
            _get(header, "name")
            _get(header, "value")
        for cookie in req["cookies"]:
            _get(cookie, "name")
            _get(cookie, "value")

        variables.update(var - extracted - _LOOP_EXTRACTED)
        extracted.update(set(x["name"] for x in rule.get("extract_variables", [])))

    return variables


async def _har_save(tplid: Optional[int], request: Request, user: dict, db):
    """Shared save logic for both /har/save and /tpl/{id}/save."""
    from jinja2.nodes import Filter, Name  # noqa: PLC0415

    reponame = request.query_params.get("reponame", "")
    harname = request.query_params.get("name", "")
    userid = user["id"]

    body = await request.body()
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            body = body.replace(b"\xc2\xa0", b" ")
        except Exception as _e:
            logger.debug("HARSave Replace error: %s", _e, exc_info=config.traceback_print)

    try:
        data = json.loads(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON: {e}") from e

    env = _get_save_env()

    async with db.transaction() as sql_session:
        har = await db.user.encrypt(userid, data["har"], sql_session=sql_session)
        tpl_enc = await db.user.encrypt(userid, data["tpl"], sql_session=sql_session)
        variables = list(_get_variables(env, data["tpl"]))

        init_env = {}
        try:
            ast = env.parse(data["tpl"])
            for x in ast.find_all(Filter):
                if (
                    x.name == "default"
                    and isinstance(x.node, Name)
                    and len(x.args) > 0
                    and x.node.name in variables
                    and x.node.name not in init_env
                ):
                    try:
                        init_env[x.node.name] = x.args[0].as_const()
                    except Exception as _e:
                        logger.debug(
                            "HARSave init_env error: %s", _e, exc_info=config.traceback_print
                        )
        except Exception as _e:
            logger.debug("HARSave ast error: %s", _e, exc_info=config.traceback_print)

        variables_json = json.dumps(variables)
        init_env_json = json.dumps(init_env)
        group_name = "None"

        if tplid:
            _tmp = check_permission(
                await db.tpl.get(
                    tplid,
                    fields=("id", "userid", "lock"),
                    sql_session=sql_session,
                ),
                "w",
                user,
            )
            if not _tmp["userid"]:
                raise HTTPException(status_code=403, detail="公开模板不允许编辑")
            if _tmp["lock"]:
                raise HTTPException(status_code=403, detail="模板已锁定")

            await db.tpl.mod(
                tplid,
                har=har,
                tpl=tpl_enc,
                variables=variables_json,
                init_env=init_env_json,
                sql_session=sql_session,
            )
            group_name = (
                await db.tpl.get(tplid, fields=("_groups",), sql_session=sql_session)
            )["_groups"]
        else:
            try:
                tplid = await db.tpl.add(
                    userid, har, tpl_enc, variables_json,
                    init_env=init_env_json,
                    sql_session=sql_session,
                )
            except Exception as e:
                if "max_allowed_packet" in str(e):
                    raise HTTPException(
                        status_code=500,
                        detail="har大小超过MySQL max_allowed_packet 限制; \n" + str(e),
                    ) from e
                raise
            if not tplid:
                raise HTTPException(status_code=500, detail="create tpl error")

    setting = data.get("setting", {})
    await db.tpl.mod(
        tplid,
        tplurl=f"{harname}|{reponame}",
        sitename=setting.get("sitename"),
        siteurl=setting.get("siteurl"),
        note=setting.get("note"),
        interval=setting.get("interval") or None,
        mtime=time.time(),
        updateable=0,
        _groups=group_name,
        sql_session=sql_session,
    )
    return {"id": tplid}


@router.post("/har/save")
async def har_save(
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Create a new template from HAR data (mirrors HARSave.post without tplid)."""
    return await _har_save(None, request, user, db)


@router.post("/tpl/{tplid}/save")
async def tpl_save(
    tplid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Update an existing template from HAR data (mirrors HARSave.post with tplid)."""
    return await _har_save(tplid, request, user, db)
