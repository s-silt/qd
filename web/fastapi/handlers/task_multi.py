#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/task_multi.py — full migration.

Routes registered here:
  GET  /task/{userid}/multi           — render batch-operation form
  POST /task/{userid}/multi           — execute batch operation
  POST /task/{userid}/get_tasksinfo   — return info for selected tasks
"""

import json
import time

try:
    from fastapi import APIRouter, Depends, Request
    from fastapi.exceptions import HTTPException
    from fastapi.responses import HTMLResponse
except ImportError:
    APIRouter = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    Request = object  # type: ignore
    HTMLResponse = object  # type: ignore
    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            self.status_code = status_code
            self.detail = detail
    HTTPException = _HTTPException  # type: ignore

import config
from libs.log import Log

# Lazy imports to avoid pulling in aiohttp at import time
# (aiohttp may not be installed in test environments)
try:
    from libs.funcs import Cal
except ImportError:
    Cal = None  # type: ignore

from web.fastapi.base import (
    get_db,
    get_current_user,
    require_user,
)
from web.fastapi.templates import render_template

logger = Log("QD.FastAPI.TaskMulti").getlogger()

router = APIRouter()


# ---------------------------------------------------------------------------
# TaskMultiOperateHandler  GET/POST /task/{userid}/multi
# ---------------------------------------------------------------------------

@router.get("/task/{userid}/multi")
async def task_multi_get(
    userid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """
    Render batch-operation form (mirrors TaskMultiOperateHandler.get).

    The caller must pass ?op=<operation> where operation is one of:
    disable, enable, delete, setgroup, settime.
    """
    try:
        op = request.query_params.get("op", "")
        if not op:
            raise Exception("错误参数")
        tasktype = op.decode() if isinstance(op, bytes) else op

        _groups = []
        if tasktype == "setgroup":
            for task in await db.task.list(
                user["id"], fields=("_groups",), limit=None
            ):
                if not isinstance(task["_groups"], str):
                    task["_groups"] = str(task["_groups"])
                temp = task["_groups"]
                if temp not in _groups:
                    _groups.append(temp)

    except Exception as e:
        logger.error(
            "UserID: %s browse Task_Multi failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request, "utils_run_result.html", log=str(e), title="打开失败", flg="danger"
        )

    return render_template(
        request, "taskmulti.html", user=user, tasktype=tasktype, _groups=_groups
    )


@router.post("/task/{userid}/multi")
async def task_multi_post(
    userid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Execute a batch operation (mirrors TaskMultiOperateHandler.post)."""
    try:
        form = await request.form()
        op = request.query_params.get("op", "")
        if not op:
            raise Exception("错误参数")
        tasktype = op.decode() if isinstance(op, bytes) else op

        env = {}
        for k in form.keys():
            env[k] = json.loads(form[k])

        if len(env["selectedtasks"]) == 0:
            raise Exception("请选择任务")

        for taskid, selected in env["selectedtasks"].items():
            if selected:
                async with db.transaction() as sql_session:
                    task = await db.task.get(
                        taskid,
                        fields=("id", "note", "tplid", "userid"),
                        sql_session=sql_session,
                    )
                    if task:
                        if task["userid"] == int(userid):
                            if tasktype == "disable":
                                await db.task.mod(
                                    taskid, disabled=True, sql_session=sql_session
                                )
                            if tasktype == "enable":
                                await db.task.mod(
                                    taskid, disabled=False, sql_session=sql_session
                                )
                            if tasktype == "delete":
                                logs = await db.tasklog.list(
                                    taskid=taskid,
                                    fields=("id",),
                                    sql_session=sql_session,
                                )
                                for log in logs:
                                    await db.tasklog.delete(
                                        log["id"], sql_session=sql_session
                                    )
                                await db.task.delete(taskid, sql_session=sql_session)
                            if tasktype == "setgroup":
                                group_env = env["setgroup"]
                                new_group = group_env["newgroup"].strip()
                                if new_group != "":
                                    target_group = new_group
                                else:
                                    target_group = group_env["checkgroupname"] or "None"
                                await db.task.mod(
                                    taskid,
                                    _groups=target_group,
                                    sql_session=sql_session,
                                )
                            if tasktype == "settime":
                                time_env = env["settime"]
                                c = Cal()
                                settime_env = {
                                    "sw": True,
                                    "time": time_env["ontime_val"],
                                    "mode": time_env["ontime_method"],
                                    "date": time_env["ontime_run_date"],
                                    "tz1": time_env["randtimezone1"],
                                    "tz2": time_env["randtimezone2"],
                                    "cron_val": time_env["cron_val"],
                                }
                                if time_env["randtimezone1"]:
                                    settime_env["randsw"] = True
                                if time_env["ontime_method"] == "ontime":
                                    if time_env["ontime_run_date"] == "":
                                        settime_env["date"] = time.strftime(
                                            "%Y-%m-%d", time.localtime()
                                        )
                                    if time_env["ontime_val"] == "":
                                        settime_env["time"] = time.strftime(
                                            "%H:%M:%S", time.localtime()
                                        )
                                if len(settime_env["time"].split(":")) == 2:
                                    settime_env["time"] = settime_env["time"] + ":00"
                                tmp = c.cal_next_ts(settime_env)
                                if tmp["r"] == "True":
                                    await db.task.mod(
                                        taskid,
                                        disabled=False,
                                        newontime=json.dumps(settime_env),
                                        next=tmp["ts"],
                                        sql_session=sql_session,
                                    )
                                else:
                                    raise Exception("参数错误")
                        else:
                            raise Exception("用户id与任务的用户id不一致")

    except Exception as e:
        logger.error(
            "UserID: %s set Task_Multi failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request,
            "utils_run_result.html",
            log=str(e),
            title="设置失败",
            flg="danger",
        )

    return render_template(
        request,
        "utils_run_result.html",
        log="设置成功，请关闭操作对话框或刷新页面查看",
        title="设置成功",
        flg="success",
    )


# ---------------------------------------------------------------------------
# GetTasksInfoHandler  POST /task/{userid}/get_tasksinfo
# ---------------------------------------------------------------------------

@router.post("/task/{userid}/get_tasksinfo")
async def get_tasksinfo_post(
    userid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Return task info for selected tasks (mirrors GetTasksInfoHandler.post)."""
    try:
        form = await request.form()
        tasks = []
        for taskid in form.keys():
            selected = form[taskid]
            if isinstance(selected, bytes):
                selected = selected.decode()
            if selected == "true":
                task = await db.task.get(taskid, fields=("id", "note", "tplid"))
                if task:
                    sitename = (
                        await db.tpl.get(task["tplid"], fields=("sitename",))
                    )["sitename"]
                    task["sitename"] = sitename
                    tasks.append(task)
    except Exception as e:
        logger.error(
            "UserID: %s get Tasks_Info failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request,
            "utils_run_result.html",
            log=str(e),
            title="获取信息失败",
            flg="danger",
        )

    return render_template(request, "taskmulti_tasksinfo.html", tasks=tasks)
