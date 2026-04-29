#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/task.py — run / log / scheduling endpoints only.

Routes registered here:
  GET  /task/{taskid}/log                  — view task logs
  GET  /task/{taskid}/log/total/{days}     — total logs across days
  GET  /task/{taskid}/log/del              — delete all task logs
  POST /task/{taskid}/log/del              — delete logs older than N days
  GET  /task/{taskid}/log/del/success      — delete success logs
  GET  /task/{taskid}/log/del/fail         — delete fail logs
  POST /task/{taskid}/run                  — immediately run a task
  GET  /task/{taskid}/settime              — render setTime form
  POST /task/{taskid}/settime              — update schedule
  POST /task/{taskid}/disable              — toggle task disabled state

CRUD endpoints (new/edit/del/var/setgroup) are handled by a separate agent.
"""

import asyncio
import datetime
import json
import time

try:
    from fastapi import APIRouter, BackgroundTasks, Depends, Request
    from fastapi.exceptions import HTTPException
    from fastapi.responses import HTMLResponse, RedirectResponse
except ImportError:
    APIRouter = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    Request = object  # type: ignore
    HTMLResponse = object  # type: ignore
    RedirectResponse = object  # type: ignore
    BackgroundTasks = object  # type: ignore
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
    from libs.funcs import Cal, Pusher
    from libs.parse_url import parse_url
except ImportError:
    Cal = None  # type: ignore
    Pusher = None  # type: ignore
    parse_url = None  # type: ignore

from web.fastapi.base import (
    check_permission,
    get_db,
    get_fetcher,
    get_current_user,
    require_user,
)
from web.fastapi.templates import render_template

logger = Log("QD.FastAPI.TaskRun").getlogger()

router = APIRouter()


# ---------------------------------------------------------------------------
# TaskLogHandler  GET /task/{taskid}/log
# ---------------------------------------------------------------------------

@router.get("/task/{taskid}/log")
async def task_log_get(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Render task log page (mirrors TaskLogHandler.get)."""
    task = check_permission(
        await db.task.get(taskid, fields=("id", "tplid", "userid", "disabled")),
        "r",
        user,
    )
    tasklog = await db.tasklog.list(
        taskid=taskid, fields=("success", "ctime", "msg")
    )
    return render_template(request, "tasklog.html", task=task, tasklog=tasklog)


# ---------------------------------------------------------------------------
# TotalLogHandler  GET /task/{userid}/log/total/{days}
# ---------------------------------------------------------------------------

@router.get("/task/{userid}/log/total/{days}")
async def total_log_get(
    userid: int,
    days: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Render aggregate log view (mirrors TotalLogHandler.get)."""
    if userid != user["id"]:
        raise HTTPException(status_code=401, detail="Unauthorized")

    tasks = []
    for task in await db.task.list(userid, fields=("id", "tplid", "note"), limit=None):
        tpl = await db.tpl.get(
            task["tplid"],
            fields=("id", "userid", "sitename", "siteurl", "banner", "note"),
        )
        task["tpl"] = tpl
        for log in await db.tasklog.list(
            taskid=task["id"], fields=("id", "success", "ctime", "msg")
        ):
            if (time.time() - log["ctime"]) <= (days * 24 * 60 * 60):
                task["log"] = log
                tasks.append(task.copy())

    return render_template(
        request, "totalLog.html", userid=userid, tasklog=tasks, days=days
    )


# ---------------------------------------------------------------------------
# TaskLogDelHandler  GET/POST /task/{taskid}/log/del
# ---------------------------------------------------------------------------

@router.get("/task/{taskid}/log/del")
async def task_log_del_get(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Delete all logs for a task (mirrors TaskLogDelHandler.get)."""
    async with db.transaction() as sql_session:
        check_permission(
            await db.task.get(taskid, fields=("userid",), sql_session=sql_session),
            "r",
            user,
        )
        tasklog = await db.tasklog.list(
            taskid=taskid,
            fields=("id", "success", "ctime", "msg"),
            sql_session=sql_session,
        )
        for log in tasklog:
            await db.tasklog.delete(log["id"], sql_session=sql_session)
        await db.task.mod(
            taskid, success_count=0, failed_count=0, sql_session=sql_session
        )

    return RedirectResponse(url=f"/task/{taskid}/log", status_code=303)


@router.post("/task/{taskid}/log/del")
async def task_log_del_post(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Delete logs older than N days (mirrors TaskLogDelHandler.post)."""
    form = await request.form()
    day = 365
    if "day" in form:
        day = int(json.loads(form["day"]))

    async with db.transaction() as sql_session:
        check_permission(
            await db.task.get(taskid, fields=("userid",), sql_session=sql_session),
            "r",
            user,
        )
        tasklog = await db.tasklog.list(
            taskid=taskid,
            fields=("id", "success", "ctime", "msg"),
            sql_session=sql_session,
        )
        for log in tasklog:
            if (time.time() - log["ctime"]) > (day * 24 * 60 * 60):
                await db.tasklog.delete(log["id"], sql_session=sql_session)

    return RedirectResponse(url=f"/task/{taskid}/log", status_code=303)


# ---------------------------------------------------------------------------
# TaskLogSuccessDelHandler  GET /task/{taskid}/log/del/success
# ---------------------------------------------------------------------------

@router.get("/task/{taskid}/log/del/success")
async def task_log_del_success(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Delete successful logs (mirrors TaskLogSuccessDelHandler.get)."""
    async with db.transaction() as sql_session:
        check_permission(
            await db.task.get(taskid, fields=("userid",), sql_session=sql_session),
            "r",
            user,
        )
        tasklog = await db.tasklog.list(
            taskid=taskid,
            fields=("id", "success", "ctime", "msg"),
            sql_session=sql_session,
        )
        for log in tasklog:
            if log["success"] == 1:
                await db.tasklog.delete(log["id"], sql_session=sql_session)
        await db.task.mod(taskid, success_count=0, sql_session=sql_session)

    return RedirectResponse(url="/my/", status_code=303)


# ---------------------------------------------------------------------------
# TaskLogFailDelHandler  GET /task/{taskid}/log/del/fail
# ---------------------------------------------------------------------------

@router.get("/task/{taskid}/log/del/fail")
async def task_log_del_fail(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Delete failed logs (mirrors TaskLogFailDelHandler.get)."""
    async with db.transaction() as sql_session:
        check_permission(
            await db.task.get(taskid, fields=("userid",), sql_session=sql_session),
            "r",
            user,
        )
        tasklog = await db.tasklog.list(
            taskid=taskid,
            fields=("id", "success", "ctime", "msg"),
            sql_session=sql_session,
        )
        for log in tasklog:
            if log["success"] == 0:
                await db.tasklog.delete(log["id"], sql_session=sql_session)
        await db.task.mod(taskid, failed_count=0, sql_session=sql_session)

    return RedirectResponse(url="/my/", status_code=303)


# ---------------------------------------------------------------------------
# TaskRunHandler  POST /task/{taskid}/run
# ---------------------------------------------------------------------------

async def _do_run_task(taskid: int, user: dict, db, fetcher):
    """
    Execute a task and record the result.

    This is the async worker that mirrors TaskRunHandler.post logic.
    It is scheduled via asyncio.create_task so the HTTP response is returned
    immediately while execution proceeds in the background.
    """
    start_ts = int(time.time())
    pushsw = None
    title = f"QD 任务ID: {taskid} 完成"
    logtmp = ""

    try:
        async with db.transaction() as sql_session:
            task = check_permission(
                await db.task.get(
                    taskid,
                    fields=(
                        "id", "tplid", "userid", "init_env", "env", "session",
                        "retry_count", "retry_interval", "last_success", "last_failed",
                        "success_count", "note", "failed_count", "last_failed_count",
                        "next", "disabled", "ontime", "ontimeflg", "pushsw", "newontime",
                    ),
                    sql_session=sql_session,
                ),
                "w",
                user,
            )

            tpl = check_permission(
                await db.tpl.get(
                    task["tplid"],
                    fields=(
                        "id", "userid", "sitename", "siteurl", "tpl",
                        "interval", "last_success",
                    ),
                    sql_session=sql_session,
                )
            )

            fetch_tpl = await db.user.decrypt(
                0 if not tpl["userid"] else task["userid"],
                tpl["tpl"],
                sql_session=sql_session,
            )
            env = dict(
                variables=await db.user.decrypt(
                    task["userid"], task["init_env"], sql_session=sql_session
                ),
                session=[],
            )

            pushsw = json.loads(task["pushsw"])
            newontime = json.loads(task["newontime"])
            pushertool = Pusher(db, sql_session=sql_session)
            caltool = Cal()

            try:
                url = parse_url(env["variables"].get("_proxy", ""))
                if not url:
                    new_env, _ = await fetcher.do_fetch(fetch_tpl, env)
                else:
                    proxy = {
                        "scheme": url["scheme"],
                        "host": url["host"],
                        "port": url["port"],
                        "username": url["username"],
                        "password": url["password"],
                    }
                    new_env, _ = await fetcher.do_fetch(fetch_tpl, env, [proxy])
            except Exception as e:
                logger.error(
                    "taskid:%d tplid:%d failed! %.4fs \r\n%s",
                    task["id"],
                    task["tplid"],
                    time.time() - start_ts,
                    str(e).replace("\\r\\n", "\r\n"),
                    exc_info=config.traceback_print,
                )
                t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                title = f"QD任务 {tpl['sitename']}-{task['note']} 失败"
                logtmp = f"{t} \\r\\n日志：{e}"

                await db.tasklog.add(
                    task["id"], success=False, msg=str(e), sql_session=sql_session
                )
                await db.task.mod(
                    task["id"],
                    last_failed=time.time(),
                    failed_count=task["failed_count"] + 1,
                    last_failed_count=task["last_failed_count"] + 1,
                    sql_session=sql_session,
                )
                await pushertool.pusher(user["id"], pushsw, 0x4, title, logtmp)
                return

            await db.tasklog.add(
                task["id"],
                success=True,
                msg=new_env["variables"].get("__log__"),
                sql_session=sql_session,
            )

            if newontime["sw"]:
                if "mode" not in newontime:
                    newontime["mode"] = "ontime"
                if newontime["mode"] == "ontime":
                    newontime["date"] = (
                        datetime.datetime.now() + datetime.timedelta(days=1)
                    ).strftime("%Y-%m-%d")
                next_time = caltool.cal_next_ts(newontime)["ts"]
            else:
                next_time = time.time() + (
                    tpl["interval"] if tpl["interval"] else 24 * 60 * 60
                )

            await db.task.mod(
                task["id"],
                disabled=False,
                last_success=time.time(),
                last_failed_count=0,
                success_count=task["success_count"] + 1,
                mtime=time.time(),
                next=next_time,
                sql_session=sql_session,
            )

            t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            title = f"QD任务 {tpl['sitename']}-{task['note']} 成功"
            logtmp = new_env["variables"].get("__log__")
            logtmp = f"{t} \\r\\n日志：{logtmp}"

            await db.tpl.incr_success(tpl["id"], sql_session=sql_session)

            log_day = int(
                (
                    await db.site.get(1, fields=("logDay",), sql_session=sql_session)
                )["logDay"]
            )
            for log in await db.tasklog.list(
                taskid=taskid, fields=("id", "ctime"), sql_session=sql_session
            ):
                if (time.time() - log["ctime"]) > (log_day * 24 * 60 * 60):
                    await db.tasklog.delete(log["id"], sql_session=sql_session)

        await pushertool.pusher(user["id"], pushsw, 0x8, title, logtmp)
    except Exception as exc:
        logger.error(
            "TaskID:%s background run failed: %s",
            taskid,
            str(exc).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )


@router.post("/task/{taskid}/run")
async def task_run_post(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
    fetcher=Depends(get_fetcher),
):
    """
    Immediately trigger a task run (mirrors TaskRunHandler.post).

    The actual fetch is scheduled as a background coroutine so the HTTP
    response is returned without blocking on the fetch duration.
    Returns 202 Accepted with a simple JSON acknowledgement.
    """
    # Verify the task exists and the user has write permission before queuing
    task = check_permission(
        await db.task.get(taskid, fields=("id", "userid")),
        "w",
        user,
    )

    asyncio.create_task(_do_run_task(task["id"], user, db, fetcher))
    return {"status": "accepted", "taskid": taskid}


# ---------------------------------------------------------------------------
# TaskSetTimeHandler  GET/POST /task/{taskid}/settime
# ---------------------------------------------------------------------------

@router.get("/task/{taskid}/settime")
async def task_settime_get(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Render set-time form (mirrors TaskSetTimeHandler.get)."""
    task = check_permission(
        await db.task.get(
            taskid,
            fields=(
                "id", "userid", "tplid", "disabled", "note",
                "ontime", "ontimeflg", "newontime",
            ),
        ),
        "w",
        user,
    )
    newontime = json.loads(task["newontime"])
    ontime = newontime
    if "mode" not in newontime:
        ontime["mode"] = "ontime"
    else:
        ontime = newontime
    today_date = time.strftime("%Y-%m-%d", time.localtime())
    return render_template(
        request, "task_setTime.html", task=task, ontime=ontime, today_date=today_date
    )


@router.post("/task/{taskid}/settime")
async def task_settime_post(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Update task schedule (mirrors TaskSetTimeHandler.post)."""
    try:
        form = await request.form()
        envs = {}
        for key in form.keys():
            value = form.getlist(key)
            if value[0] == "true" or value[0] == "false":
                envs[key] = True if value[0] == "true" else False
            else:
                envs[key] = str(value[0])

        # Verify permission
        check_permission(
            await db.task.get(taskid, fields=("userid",)),
            "w",
            user,
        )

        async with db.transaction() as sql_session:
            if envs["sw"]:
                c = Cal()
                if "time" in envs:
                    if len(envs["time"].split(":")) < 3:
                        envs["time"] = envs["time"] + ":00"
                tmp = c.cal_next_ts(envs)
                if tmp["r"] == "True":
                    await db.task.mod(
                        taskid,
                        disabled=False,
                        newontime=json.dumps(envs),
                        next=tmp["ts"],
                        sql_session=sql_session,
                    )
                    log = f"设置成功，下次执行时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(tmp['ts']))}"
                else:
                    raise Exception(tmp["r"])
            else:
                tmp = json.loads(
                    (
                        await db.task.get(
                            taskid, fields=("newontime",), sql_session=sql_session
                        )
                    )["newontime"]
                )
                tmp["sw"] = False
                await db.task.mod(
                    taskid, newontime=json.dumps(tmp), sql_session=sql_session
                )
                log = "设置成功"

    except Exception as e:
        logger.error(
            "TaskID: %s set Time failed! Reason: %s",
            taskid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request, "utils_run_result.html", log=str(e), title="设置失败", flg="danger"
        )

    return render_template(
        request, "utils_run_result.html", log=log, title="设置成功", flg="success"
    )


# ---------------------------------------------------------------------------
# TaskDisableHandler  POST /task/{taskid}/disable
# ---------------------------------------------------------------------------

@router.post("/task/{taskid}/disable")
async def task_disable_post(
    taskid: int,
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Toggle task disabled state (mirrors TaskDisableHandler.post)."""
    async with db.transaction() as sql_session:
        check_permission(
            await db.task.get(taskid, fields=("userid",), sql_session=sql_session),
            "w",
            user,
        )
        await db.task.mod(taskid, disabled=1, sql_session=sql_session)

    return RedirectResponse(url="/my/", status_code=303)
