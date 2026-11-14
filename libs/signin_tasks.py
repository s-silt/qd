"""Persistence and schedule changes for private browser templates and tasks."""

import json
import re
import time

import config
from db.task import Task
from libs.funcs import Cal
from libs.signin_workbench import WorkbenchError, WorkbenchPermissionError, clean_chat, validate_script, web_origin

CLOCK_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def _check_owned(row, owner):
    if not row:
        raise WorkbenchPermissionError(404, "对象不存在")
    if row.get("userid") != owner:
        raise WorkbenchPermissionError(403, "没有权限")
    return row


async def load_workbench_target(db, owner, *, taskid=None, templateid=None):
    """Open a private script and any encrypted state attached to the selected task."""
    if taskid is not None and templateid is not None:
        raise ValueError("不能同时指定任务和模板")
    if taskid is not None:
        if type(taskid) is not int or taskid <= 0:
            raise ValueError("任务 ID 无效")
        task = _check_owned(
            await db.task.get(
                taskid,
                fields=("id", "userid", "tplid", "note", "browser_state", "init_env", "browser_session"),
            ),
            owner,
        )
        if not task["browser_state"]:
            raise WorkbenchError("此任务未使用浏览器登录状态")
        tpl = _check_owned(
            await db.tpl.get(task["tplid"], fields=("id", "userid", "sitename", "browser", "tpl")),
            owner,
        )
        if tpl["browser"]:
            script = await db.user.decrypt(owner, tpl["tpl"])
        else:
            values = await db.user.decrypt(owner, task["init_env"])
            script = values.get("_qd_browser") if isinstance(values, dict) else None
        state = await db.user.decrypt(owner, task["browser_session"]) if task["browser_session"] else None
        validated = validate_script(script)
        return {
            "task": task,
            "tpl": tpl,
            "task_name": task["note"] or (tpl or {}).get("sitename") or "签到任务",
            "script_name": "",
            "script": validated,
            "state": state,
            "url": validated["url"],
        }
    if templateid is not None:
        if type(templateid) is not int or templateid <= 0:
            raise ValueError("模板 ID 无效")
        tpl = _check_owned(
            await db.tpl.get(templateid, fields=("id", "userid", "sitename", "browser", "tpl")),
            owner,
        )
        if not tpl["browser"]:
            raise WorkbenchError("此模板使用 HAR 请求")
        validated = validate_script(await db.user.decrypt(owner, tpl["tpl"]))
        return {
            "task": None,
            "tpl": tpl,
            "task_name": "",
            "script_name": tpl["sitename"] or "浏览器模板",
            "script": validated,
            "state": None,
            "url": validated["url"],
        }
    return {
        "task": None,
        "tpl": None,
        "task_name": "",
        "script_name": "",
        "script": None,
        "state": None,
        "url": None,
    }


def build_daily_schedule(clock="08:00"):
    if not isinstance(clock, str) or not CLOCK_RE.fullmatch(clock):
        raise WorkbenchError("请选择每日签到时间")
    hour, minute = map(int, clock.split(":"))
    schedule = {"sw": True, "mode": "cron", "cron_val": f"{minute} {hour} * * *"}
    next_at = Cal().cal_next_ts(schedule).get("ts")
    if not next_at:
        raise WorkbenchError("无法计算下次运行时间")
    return schedule, next_at


async def save_template_and_task(db, session, payload):
    if session.taskid is not None or not (session.template_script or session.draft):
        raise WorkbenchError("请先生成签到脚本")
    mode = payload.get("mode", "schedule" if "clock" in payload else "template")
    if mode not in ("template", "run", "schedule"):
        raise ValueError("保存模式无效")
    if session.created_task or (session.saved and mode == "template"):
        return session.created_task or session.saved, False
    name = payload.get("name", "")
    if not session.templateid and not session.saved and (
        not isinstance(name, str) or not name.strip() or len(name) > 100
    ):
        raise WorkbenchError("请输入 1 到 100 字符的模板名称")

    script = validate_script(session.template_script or session.draft["script"])
    task_fields = {}
    state = None
    if mode != "template":
        schedule, next_at = build_daily_schedule(payload.get("clock", "08:00"))
        state = await session.verification_state(script["origin"])
        task_fields = dict(next=next_at, newontime=json.dumps(schedule), browser_state=Task.BROWSER_READY)

    owner = session.owner
    async with db.transaction() as txn:
        tplid = session.templateid or (session.saved or {}).get("template")
        if tplid:
            tpl = _check_owned(
                await db.tpl.get(
                    tplid,
                    fields=("id", "userid", "browser", "sitename"),
                    sql_session=txn,
                    for_update=True,
                ),
                owner,
            )
            if not tpl["browser"]:
                raise WorkbenchError("浏览器脚本不存在")
            name = tpl["sitename"]
        else:
            encrypted_script = await db.user.encrypt(owner, script, sql_session=txn)
            tplid = await db.tpl.add(owner, None, encrypted_script, "[]", "{}", sql_session=txn, browser=True)
            await db.tpl.mod(
                tplid,
                sitename=name.strip(),
                siteurl=script["origin"],
                _groups=Task.BROWSER_GROUP,
                note=clean_chat(session.draft["summary"])[:1024],
                sql_session=txn,
            )
        taskid = None
        if task_fields:
            env = await db.user.encrypt(owner, {}, sql_session=txn)
            taskid = await db.task.add(tplid, owner, env, sql_session=txn)
            task_fields["browser_session"] = await db.user.encrypt(owner, state, sql_session=txn)
            task_fields["_groups"] = Task.BROWSER_GROUP
            task_fields["note"] = (name or "").strip()[:256]
            await db.task.mod(taskid, **task_fields, sql_session=txn)

    session.saved = {"template": tplid, "url": f"/tpl/{tplid}/edit", "name": name.strip() if name else ""}
    if taskid:
        session.created_task = {"template": tplid, "task": taskid, "url": f"/task/{taskid}/log"}
    return session.created_task or session.saved, bool(taskid and mode == "run")


async def resume_verified_task(db, session):
    if session.taskid is None:
        raise WorkbenchError("此会话没有关联待验证的任务")
    if session.saved:
        return session.saved
    origin = session.template_script["origin"] if session.template_script else web_origin(session.start_url)
    state = await session.verification_state(origin)
    async with db.transaction() as txn:
        task = _check_owned(
            await db.task.get(
                session.taskid,
                fields=(
                    "id",
                    "userid",
                    "browser_state",
                    "browser_session",
                    "newontime",
                    "disabled",
                    "next",
                    "last_failed_count",
                ),
                sql_session=txn,
            ),
            session.owner,
        )
        if not task["browser_state"]:
            raise WorkbenchError("此任务未使用浏览器登录状态")
        paused = task["browser_state"] == Task.BROWSER_VERIFY
        schedule = json.loads(task["newontime"])
        next_at = task["next"]
        if paused:
            next_at = Cal().cal_next_ts(schedule).get("ts") if schedule.get("sw") else None
            if next_at is None or next_at <= time.time():
                next_at = time.time() + config.new_task_delay
        encrypted = await db.user.encrypt(session.owner, state, sql_session=txn)
        changed = await db.task.mod(
            session.taskid,
            disabled=False if paused else task["disabled"],
            next=next_at,
            browser_state=Task.BROWSER_READY,
            browser_session=encrypted,
            expected={
                key: task[key]
                for key in ("browser_state", "browser_session", "newontime", "disabled", "next")
            },
            last_failed_count=0 if paused else task["last_failed_count"],
            sql_session=txn,
        )
        if not changed:
            raise WorkbenchError("任务已更新，请刷新后查看")
    session.saved = {"task": session.taskid, "url": f"/task/{session.taskid}/log"}
    return session.saved


async def update_browser_task_settings(db, owner, taskid, *, note, retry_count_raw, retry_interval_raw):
    try:
        retries = int(retry_count_raw)
        interval = int(retry_interval_raw or 0)
    except ValueError:
        raise WorkbenchError("重试参数无效") from None
    if (
        retries < -1
        or retries > config.task_max_retry_count
        or 0 < interval < 60
        or interval < 0
        or interval > 31536000
    ):
        raise WorkbenchError("重试参数无效")
    note = (note or "").strip()
    if len(note) > 256:
        raise WorkbenchError("任务备注过长")
    async with db.transaction() as txn:
        task = _check_owned(
            await db.task.get(taskid, fields=("id", "userid", "browser_state"), sql_session=txn),
            owner,
        )
        if not task.get("browser_state"):
            raise WorkbenchError("此任务不是浏览器签到任务")
        await db.task.mod(
            taskid,
            note=note,
            retry_count=retries,
            retry_interval=interval or None,
            sql_session=txn,
        )


async def load_browser_template_detail(db, owner, tplid):
    tpl = _check_owned(
        await db.tpl.get(tplid, fields=("id", "userid", "sitename", "browser", "tpl", "note")),
        owner,
    )
    if not tpl.get("browser"):
        return None
    script = validate_script(await db.user.decrypt(tpl["userid"], tpl["tpl"]))
    tasks = await db.task.list(
        tpl["userid"],
        fields=("id", "note", "browser_state", "disabled", "next", "last_success"),
        limit=None,
        tplid=tpl["id"],
    )
    return {"tpl": tpl, "script": script, "tasks": tasks}
