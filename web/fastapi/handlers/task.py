#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/task.py — CRUD endpoints only.

Original Tornado handlers ported here:
  - TaskNewHandler    GET  /task/new          — new-task form
  - TaskNewHandler    POST /task/new          — create task
  - TaskEditHandler   GET  /task/{taskid}/edit — edit-task form
  - TaskNewHandler    POST /task/{taskid}/edit — save task edits (shared POST)
  - TaskDelHandler    POST /task/{taskid}/del  — delete task
  - TaskGroupHandler  GET  /task/{taskid}/setgroup — set-group form
  - TaskGroupHandler  POST /task/{taskid}/setgroup — save group
  - (new)             GET  /task/{taskid}/var  — view task template variables

NOT ported here (handled by other agents):
  - TaskRunHandler         /task/{taskid}/run         — immediate run
  - TaskLogHandler         /task/{taskid}/log          — log view
  - TaskSetTimeHandler     /task/{taskid}/settime      — schedule config
  - TasksDelHandler        /tasks/{userid}             — multi-task ops
  - TaskDisableHandler     /task/{taskid}/disable      — disable task
  - TotalLogHandler        /task/{taskid}/log/total/*  — total log view
  - TaskLogDelHandler      /task/{taskid}/log/del      — delete logs
  - TaskLogSuccessDelHandler / TaskLogFailDelHandler    — partial log delete
  - GetGroupHandler        /getgroups/{userid}         — group list JSON
"""

import json
import time
from codecs import escape_decode
from urllib.parse import parse_qs

import config

try:
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import RedirectResponse
    from fastapi.exceptions import HTTPException
except ImportError:
    APIRouter = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    Request = object  # type: ignore
    RedirectResponse = object  # type: ignore

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            self.status_code = status_code
            self.detail = detail

    HTTPException = _HTTPException  # type: ignore

from libs.log import Log
from web.fastapi.base import (
    check_permission,
    get_current_user,
    get_db,
    require_user,
)
from web.fastapi.templates import render_template

logger = Log("QD.FastAPI.Task").getlogger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Form parsing helper
# ---------------------------------------------------------------------------

class _SimpleForm:
    """
    Minimal form-data abstraction that works without python-multipart.

    Attempts `request.form()` first; if that raises (python-multipart absent),
    falls back to manually decoding URL-encoded body bytes via urllib.parse.

    Supports .get(key, default), .getlist(key), .keys() and 'in' operator.
    """

    def __init__(self, data: dict):
        # data is {key: [value, ...], ...}
        self._data = data

    def get(self, key, default=None):
        vals = self._data.get(key)
        if vals:
            return vals[0]
        return default

    def getlist(self, key):
        return self._data.get(key, [])

    def keys(self):
        return self._data.keys()

    def __contains__(self, key):
        return key in self._data


async def _parse_form(request) -> "_SimpleForm":
    """
    Parse form data from a request, falling back to manual URL-decode.

    Prefers request.form() (requires python-multipart) but gracefully
    degrades to urllib.parse.parse_qs on the raw body bytes for
    application/x-www-form-urlencoded payloads — which covers all browser
    HTML form submissions and is the only format used by QD task endpoints.
    """
    try:
        form = await request.form()
        data: dict = {}
        for key in form.keys():
            data[key] = form.getlist(key)
        return _SimpleForm(data)
    except Exception:
        # Fallback: manual URL-encoded parse (no python-multipart needed)
        body = await request.body()
        data = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return _SimpleForm(data)


# ---------------------------------------------------------------------------
# Helper: collect groups for a user's tasks
# ---------------------------------------------------------------------------

async def _collect_task_groups(db, userid) -> list:
    """Return an ordered, deduplicated list of _groups values for a user's tasks."""
    _groups: list = []
    for task in await db.task.list(userid, fields=("_groups",), limit=None):
        if not isinstance(task["_groups"], str):
            task["_groups"] = str(task["_groups"])
        temp = task["_groups"]
        if temp not in _groups:
            _groups.append(temp)
    return _groups


# ---------------------------------------------------------------------------
# TaskNewHandler — GET /task/new
# ---------------------------------------------------------------------------

@router.get("/task/new")
async def task_new_get(
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Render the new-task form (mirrors TaskNewHandler.get)."""
    tplid_param = request.query_params.get("tplid")
    fields = ("id", "sitename", "success_count")

    tpls = []
    if user:
        tpls += sorted(
            await db.tpl.list(userid=user["id"], fields=fields, limit=None),
            key=lambda t: -t["id"],
        )
    if tpls:
        tpls.append({"id": 0, "sitename": "-----公开模板-----"})
    tpls += sorted(
        await db.tpl.list(userid=None, public=1, fields=fields, limit=None),
        key=lambda t: -t["success_count"],
    )

    tplid = tplid_param
    if not tplid:
        for tpl in tpls:
            if tpl.get("id"):
                tplid = tpl["id"]
                break
    if tplid:
        tplid = int(tplid)

        tpl = check_permission(
            await db.tpl.get(
                tplid,
                fields=(
                    "id",
                    "userid",
                    "note",
                    "sitename",
                    "siteurl",
                    "variables",
                    "init_env",
                ),
            ),
            "r",
            user,
        )
        variables = json.loads(tpl["variables"])
        if not tpl["init_env"]:
            tpl["init_env"] = "{}"
        init_env = json.loads(tpl["init_env"])

        _groups = await _collect_task_groups(db, user["id"]) if user else []

        return render_template(
            request,
            "task_new.html",
            tpls=tpls,
            tplid=tplid,
            tpl=tpl,
            variables=variables,
            task={},
            _groups=_groups,
            init_env=init_env,
            default_retry_count=config.task_max_retry_count,
        )
    else:
        return render_template(
            request,
            "utils_run_result.html",
            log="请先添加模板！",
            title="设置失败",
            flg="danger",
        )


# ---------------------------------------------------------------------------
# TaskNewHandler — POST /task/new (create task)
# ---------------------------------------------------------------------------

@router.post("/task/new")
async def task_new_post(
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Create a new task (mirrors TaskNewHandler.post without taskid)."""
    form = await _parse_form(request)

    tplid = int(form.get("_binux_tplid"))
    tested = form.get("_binux_tested", False)
    note = form.get("_binux_note", "")
    proxy = form.get("_binux_proxy", "")
    retry_count = form.get("_binux_retry_count", "")
    retry_interval = form.get("_binux_retry_interval", "")

    async with db.transaction() as sql_session:
        tpl = check_permission(
            await db.tpl.get(
                tplid, fields=("id", "userid", "interval"), sql_session=sql_session
            ),
            "r",
            user,
        )

        # Build env from form fields (excluding _binux_ prefixed ones)
        env: dict = {}
        for key in form.keys():
            if key.startswith("_binux_"):
                continue
            value = form.getlist(key)
            if not value:
                continue
            env[key] = form.get(key)
        env["_proxy"] = proxy

        retry_count_int = int(retry_count) if retry_count else retry_count
        retry_interval_int = int(retry_interval) if retry_interval else retry_interval
        env["retry_count"] = retry_count_int
        env["retry_interval"] = retry_interval_int

        # Determine group
        envs: dict = {}
        for key in form.keys():
            envs[key] = form.getlist(key)

        target_group = None
        if "New_group" in envs:
            new_group = envs["New_group"][0].strip()
            if new_group != "":
                target_group = new_group
            else:
                target_group = "None"
                for key, value in envs.items():
                    if value and value[0] == "on":
                        if key.find("group-select-") > -1:
                            target_group = escape_decode(
                                key.replace("group-select-", "").strip()[2:-1],
                                "hex-escape",
                            )[0].decode("utf-8")
                            break

        env = await db.user.encrypt(user["id"], env, sql_session=sql_session)
        taskid = await db.task.add(tplid, user["id"], env, sql_session=sql_session)

        if tested:
            await db.task.mod(
                taskid,
                note=note,
                next=time.time() + (tpl["interval"] or 24 * 60 * 60),
                sql_session=sql_session,
            )
        else:
            await db.task.mod(
                taskid,
                note=note,
                next=time.time() + config.new_task_delay,
                sql_session=sql_session,
            )

        if target_group is not None:
            await db.task.mod(taskid, _groups=target_group, sql_session=sql_session)

        if isinstance(retry_count_int, int) and -1 <= retry_count_int:
            await db.task.mod(
                taskid, retry_count=retry_count_int, sql_session=sql_session
            )

        if retry_interval_int:
            await db.task.mod(
                taskid, retry_interval=retry_interval_int, sql_session=sql_session
            )
        else:
            await db.task.mod(taskid, retry_interval=None, sql_session=sql_session)

    return RedirectResponse(url="/my/", status_code=303)


# ---------------------------------------------------------------------------
# TaskEditHandler — GET /task/{taskid}/edit
# ---------------------------------------------------------------------------

@router.get("/task/{taskid}/edit")
async def task_edit_get(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Render the edit-task form (mirrors TaskEditHandler.get)."""
    task = check_permission(
        await db.task.get(
            taskid,
            fields=(
                "id",
                "userid",
                "tplid",
                "disabled",
                "note",
                "retry_count",
                "retry_interval",
            ),
        ),
        "w",
        user,
    )
    task["init_env"] = await db.user.decrypt(
        user["id"],
        (await db.task.get(taskid, fields=("init_env",)))["init_env"],
    )

    tpl = check_permission(
        await db.tpl.get(
            task["tplid"],
            fields=("id", "userid", "note", "sitename", "siteurl", "variables"),
        ),
        "r",
        user,
    )
    variables = json.loads(tpl["variables"])

    init_env = []
    for var in variables:
        value = task["init_env"][var] if var in task["init_env"] else ""
        init_env.append({"name": var, "value": value})

    proxy = task["init_env"].get("_proxy", "")
    if task["retry_interval"] is None:
        task["retry_interval"] = ""

    return render_template(
        request,
        "task_new.html",
        tpls=[tpl],
        tplid=tpl["id"],
        tpl=tpl,
        variables=variables,
        task=task,
        init_env=init_env,
        proxy=proxy,
        retry_count=task["retry_count"],
        retry_interval=task["retry_interval"],
        default_retry_count=config.task_max_retry_count,
        task_title="修改任务",
    )


# ---------------------------------------------------------------------------
# TaskNewHandler — POST /task/{taskid}/edit (save task edits)
# ---------------------------------------------------------------------------

@router.post("/task/{taskid}/edit")
async def task_edit_post(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Save task edits (mirrors TaskNewHandler.post with taskid)."""
    form = await _parse_form(request)

    note = form.get("_binux_note", "")
    proxy = form.get("_binux_proxy", "")
    retry_count = form.get("_binux_retry_count", "")
    retry_interval = form.get("_binux_retry_interval", "")

    # Build env update from form fields
    env: dict = {}
    for key in form.keys():
        if key.startswith("_binux_"):
            continue
        value = form.getlist(key)
        if not value:
            continue
        env[key] = form.get(key)
    env["_proxy"] = proxy

    retry_count_int = int(retry_count) if retry_count else retry_count
    retry_interval_int = int(retry_interval) if retry_interval else retry_interval
    env["retry_count"] = retry_count_int
    env["retry_interval"] = retry_interval_int

    envs: dict = {}
    for key in form.keys():
        envs[key] = form.getlist(key)

    target_group = None
    if "New_group" in envs:
        new_group = envs["New_group"][0].strip()
        if new_group != "":
            target_group = new_group
        else:
            target_group = "None"
            for key, value in envs.items():
                if value and value[0] == "on":
                    if key.find("group-select-") > -1:
                        target_group = escape_decode(
                            key.replace("group-select-", "").strip()[2:-1],
                            "hex-escape",
                        )[0].decode("utf-8")
                        break

    async with db.transaction() as sql_session:
        task = check_permission(
            await db.task.get(
                taskid,
                fields=("id", "userid", "init_env", "retry_interval"),
                sql_session=sql_session,
            ),
            "w",
            user,
        )

        retry_interval_modified = True
        if task["retry_interval"] == retry_interval_int or (
            retry_interval_int == "" and task["retry_interval"] is None
        ):
            retry_interval_modified = False

        init_env = await db.user.decrypt(
            user["id"], task["init_env"], sql_session=sql_session
        )
        init_env.update(env)
        init_env = await db.user.encrypt(
            user["id"], init_env, sql_session=sql_session
        )
        await db.task.mod(
            taskid,
            init_env=init_env,
            env=None,
            session=None,
            note=note,
            sql_session=sql_session,
        )

        if target_group is not None:
            await db.task.mod(taskid, _groups=target_group, sql_session=sql_session)

        if isinstance(retry_count_int, int) and -1 <= retry_count_int:
            await db.task.mod(
                taskid, retry_count=retry_count_int, sql_session=sql_session
            )

        if retry_interval_modified:
            if retry_interval_int:
                await db.task.mod(
                    taskid,
                    retry_interval=retry_interval_int,
                    sql_session=sql_session,
                )
            else:
                await db.task.mod(
                    taskid, retry_interval=None, sql_session=sql_session
                )

    return RedirectResponse(url="/my/", status_code=303)


# ---------------------------------------------------------------------------
# TaskDelHandler — POST /task/{taskid}/del
# ---------------------------------------------------------------------------

@router.post("/task/{taskid}/del")
async def task_del_post(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Delete a task and all its logs (mirrors TaskDelHandler.post)."""
    async with db.transaction() as sql_session:
        check_permission(
            await db.task.get(
                taskid, fields=("id", "userid"), sql_session=sql_session
            ),
            "w",
            user,
        )
        logs = await db.tasklog.list(
            taskid=taskid, fields=("id",), sql_session=sql_session
        )
        for log in logs:
            await db.tasklog.delete(log["id"], sql_session=sql_session)
        await db.task.delete(taskid, sql_session=sql_session)

    return RedirectResponse(url="/my/", status_code=303)


# ---------------------------------------------------------------------------
# Task var endpoint — GET /task/{taskid}/var
# Mirrors TPLVarHandler but for a specific task (shows task template variables)
# ---------------------------------------------------------------------------

@router.get("/task/{taskid}/var")
async def task_var_get(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Show the template variable view for a task (new endpoint, no Tornado equivalent)."""
    task = check_permission(
        await db.task.get(
            taskid,
            fields=("id", "userid", "tplid"),
        ),
        "r",
        user,
    )
    tpl = await db.tpl.get(
        task["tplid"],
        fields=("id", "userid", "note", "sitename", "siteurl", "variables", "init_env"),
    )
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if not tpl["init_env"]:
        tpl["init_env"] = "{}"

    return render_template(
        request,
        "task_new_var.html",
        tpl=tpl,
        variables=json.loads(tpl["variables"]),
        init_env=json.loads(tpl["init_env"]),
    )


# ---------------------------------------------------------------------------
# TaskGroupHandler — GET/POST /task/{taskid}/setgroup
# ---------------------------------------------------------------------------

@router.get("/task/{taskid}/setgroup")
async def task_setgroup_get(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Render set-group form for a task (mirrors TaskGroupHandler.get)."""
    group_now = (await db.task.get(taskid, fields=("_groups",)))["_groups"]
    _groups = await _collect_task_groups(db, user["id"])

    return render_template(
        request,
        "task_setgroup.html",
        taskid=taskid,
        _groups=_groups,
        groupNow=group_now,
    )


@router.post("/task/{taskid}/setgroup")
async def task_setgroup_post(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Save task group assignment (mirrors TaskGroupHandler.post)."""
    form = await _parse_form(request)
    envs: dict = {}
    for key in form.keys():
        envs[key] = form.getlist(key)

    new_group = envs.get("New_group", [""])[0].strip()

    if new_group != "":
        target_group = new_group
    else:
        target_group = "None"
        for key, value in envs.items():
            if value and value[0] == "on":
                target_group = escape_decode(key.strip()[2:-1], "hex-escape")[
                    0
                ].decode("utf-8")
                break

    await db.task.mod(taskid, _groups=target_group)

    return RedirectResponse(url="/my/", status_code=303)
