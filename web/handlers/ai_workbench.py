"""QD chat and human-assisted sign-in recording."""

import asyncio
import hashlib
import json
import re
from contextlib import suppress
from functools import wraps

import tornado.web
from tornado.iostream import StreamClosedError

import config
from db.task import Task
from libs import ai_client, playwright_capture
from libs.funcs import Cal
from libs.signin_workbench import (
    BrowserSession,
    VerificationNeeded,
    WorkbenchError,
    clean_chat,
    credentials_in_chat,
    page_label,
    web_origin,
)
from web.handlers.base import BaseHandler, logger_web_handler

CHAT_CONTEXT = (
    "你是 QD 签到助手。用用户的语言简洁回答，解释如何在浏览器中完成登录和验证、录制签到及定时。"
    "用户亲自完成站点要求的验证。不要询问密码、验证码、Cookie 或令牌。"
    "你无法打开网站、执行代码、操作浏览器或修改任务。"
)
EXAMPLE_KEYS = {
    hashlib.sha256(value).digest() for value in (b"binux", b"please-change-to-a-long-random-string")
}


def browser_enabled():
    return (
        config.ai_workbench_browser
        and not config.multiprocess
        and playwright_capture.is_available()
        and config.cookie_secret not in EXAMPLE_KEYS
        and config.aes_key not in EXAMPLE_KEYS
    )


def workbench_post(method):
    @wraps(method)
    @tornado.web.authenticated
    async def handler(self, *args):
        self.set_header("Cache-Control", "no-store")
        if not self.request.headers.get("Content-Type", "").startswith(
            "application/json"
        ) or not self.request.headers.get("X-QD-Workbench"):
            raise tornado.web.HTTPError(403)
        if len(self.request.body) > 32000:
            raise tornado.web.HTTPError(413)
        try:
            payload = json.loads(self.request.body)
            if not isinstance(payload, dict):
                raise ValueError("请求格式无效")
            await method(self, payload, *args)
        except tornado.web.HTTPError:
            raise
        except StreamClosedError:
            return
        except VerificationNeeded as exc:
            self.set_status(409)
            await self.finish({"error": str(exc), "verification": True})
        except (WorkbenchError, ValueError) as exc:
            self.set_status(400)
            await self.finish(
                {"error": str(exc) if isinstance(exc, WorkbenchError) else "请求参数或网址无效"}
            )
        except (ai_client.AIClientError, asyncio.TimeoutError) as exc:
            self.set_status(502)
            status = getattr(exc, "status", None)
            message = (
                f"模型服务返回 HTTP {status}。请检查连接设置后重试"
                if status
                else "模型暂未返回可用结果，请检查模型配置后重试"
            )
            await self.finish({"error": message})
        except Exception as exc:
            logger_web_handler.warning("AI 签到工作台操作失败 (%s)", type(exc).__name__)
            self.set_status(502)
            await self.finish({"error": "工作台操作失败，请检查站点连接或 Chromium 配置后重试"})

    return handler


class WorkbenchPage(BaseHandler):
    @tornado.web.authenticated
    async def get(self):
        self.set_header("Cache-Control", "no-store")
        self.set_header("Referrer-Policy", "no-referrer")
        self.set_header("X-Frame-Options", "DENY")
        self.set_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        client = ai_client.AIClient()
        taskid = self.get_query_argument("task", "")
        task_name = ""
        if taskid:
            if not re.fullmatch(r"[0-9]{1,10}", taskid):
                raise tornado.web.HTTPError(400)
            task = self.check_permission(
                await self.db.task.get(taskid, fields=("id", "userid", "tplid", "note", "browser_state")), "w"
            )
            if not task["browser_state"]:
                raise tornado.web.HTTPError(404)
            tpl = await self.db.tpl.get(task["tplid"], fields=("sitename",))
            task_name = task["note"] or (tpl or {}).get("sitename") or "签到任务"
        await self.render(
            "ai_workbench.html",
            model=client.model,
            model_enabled=client.enabled,
            browser_ready=browser_enabled(),
            taskid=taskid,
            task_name=task_name,
        )


class WorkbenchChat(BaseHandler):
    @workbench_post
    async def post(self, payload):
        history = payload.get("messages", [])
        if not isinstance(history, list) or not 1 <= len(history) <= 12:
            raise ValueError()
        messages = [{"role": "system", "content": CHAT_CONTEXT}]
        for item in history:
            if not isinstance(item, dict) or item.get("role") not in ("assistant", "user"):
                raise ValueError()
            text = item.get("content")
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise ValueError()
            if item["role"] == "user" and credentials_in_chat(text):
                raise WorkbenchError("检测到凭据或验证码，消息已拦截。请在浏览器页面中输入验证信息")
            messages.append({"role": item["role"], "content": clean_chat(text)})
        if messages[-1]["role"] != "user":
            raise ValueError()
        self.evil(+1)
        reply = await ai_client.AIClient().chat(messages)
        await self.finish({"text": clean_chat(reply)[:1800]})


class WorkbenchStart(BaseHandler):
    @workbench_post
    async def post(self, payload):
        if not browser_enabled():
            raise WorkbenchError(
                "在线浏览器需要启用 AI_WORKBENCH_BROWSER、安装 Chromium、使用单进程和独立部署密钥"
            )
        sessions = self.application.workbench_sessions
        owner = self.current_user["id"]
        taskid = payload.get("task")
        state = None
        if taskid is not None:
            if type(taskid) is not int or taskid <= 0:
                raise ValueError()
            task = self.check_permission(
                await self.db.task.get(
                    taskid,
                    fields=("id", "userid", "browser_state", "init_env", "browser_session"),
                ),
                "w",
            )
            if not task["browser_state"]:
                raise WorkbenchError("此任务未使用浏览器登录状态")
            values = await self.db.user.decrypt(owner, task["init_env"])
            url = values["_qd_browser"]["url"]
            if task["browser_session"]:
                state = await self.db.user.decrypt(owner, task["browser_session"])
        else:
            url = payload.get("url")
        web_origin(url)
        for existing in sessions.values():
            if (
                existing.owner == owner
                and existing.taskid == taskid
                and existing.start_url == url
                and existing.page
            ):
                touch_session(sessions, existing)
                await self.finish({"session": existing.id, "site": page_label(existing.page.url)})
                return
        session = BrowserSession(owner, url, taskid=taskid)
        # Recheck after the DB awaits before reserving a browser slot.
        if len(sessions) >= max(1, playwright_capture.PLAYWRIGHT_MAX_CONCURRENT) or any(
            existing.owner == owner for existing in sessions.values()
        ):
            raise WorkbenchError("当前没有可用的浏览器会话，请稍后重试")
        sessions[session.id] = session
        async with session.lock:
            try:
                await session.open(state)
            except BaseException:
                sessions.pop(session.id, None)
                await session.close()
                raise
        touch_session(sessions, session)
        await self.finish({"session": session.id, "site": page_label(session.page.url)})


def touch_session(sessions, session):
    async def expire():
        async with session.lock:
            if sessions.pop(session.id, None) is not None:
                await session.close()

    session.touch(lambda: asyncio.create_task(expire()))


async def analyze_recording(recording, client):
    if not client.enabled:
        raise WorkbenchError("请先配置自建模型服务，再分析录制结果")
    if not recording.summaries:
        raise WorkbenchError("没有录到同站点签到请求，请重新录制")
    # No URL host, headers, bodies, screenshots, selectors or verification data
    # cross the model boundary. The model describes the locally recorded script.
    summary = dict(
        requests=recording.summaries, clicks=len(recording.steps), site_reported_success=recording.success
    )
    reply = await client.chat(
        [
            {
                "role": "system",
                "content": (
                    "分析这组已脱敏的 QD 签到请求摘要。用简短中文说明观察到的步骤及能否确认结果。"
                    "只依据记录内容，不要猜测、生成可执行指令或声称未观察到的结果。"
                ),
            },
            {"role": "user", "content": json.dumps(summary, ensure_ascii=False)},
        ],
        temperature=0.1,
    )
    if not isinstance(reply, str) or not reply.strip():
        raise WorkbenchError("模型返回空结果，可重新分析")
    return {
        "summary": clean_chat(reply)[:1600],
        "steps": len(recording.steps),
        "requests": len(recording.summaries),
        "blocked": recording.blocked,
        "verified": recording.success,
        "site": page_label(recording.start_url),
    }


class WorkbenchSessionHandler(BaseHandler):
    def _session(self, sessionid):
        session = self.application.workbench_sessions.get(sessionid)
        if session is None or session.owner != self.current_user["id"]:
            raise tornado.web.HTTPError(404)
        return session

    @tornado.web.authenticated
    async def get(self, sessionid, action):
        session = self._session(sessionid)
        self.set_header("Cache-Control", "no-store")
        if action == "frame":
            if session.lock.locked():
                self.set_status(204)
                await self.finish()
                return
            async with session.lock:
                try:
                    page = session.active_page()
                    frame = await page.screenshot(type="jpeg", quality=65, timeout=1800)
                except Exception:
                    self.set_status(204)
                    with suppress(StreamClosedError):
                        await self.finish()
                    return
                site = page_label(page.url)
                self.set_header("X-QD-Site", site if "://" in site else "")
                self.set_header("X-QD-Recording", "1" if session.recording else "0")
                self.set_header(
                    "X-QD-Tabs",
                    json.dumps([page_label(tab.url) for tab in session.context.pages], ensure_ascii=True),
                )
                self.set_header("X-QD-Active-Tab", str(session.context.pages.index(page)))
                self.set_header("Content-Type", "image/jpeg")
                with suppress(StreamClosedError):
                    await self.finish(frame)
            return
        raise tornado.web.HTTPError(404)

    @workbench_post
    async def post(self, payload, sessionid, action):
        session = self._session(sessionid)
        sessions = self.application.workbench_sessions
        if action != "close" and not browser_enabled():
            raise WorkbenchError("在线浏览器当前已停用")
        async with session.lock:
            if sessions.get(sessionid) is not session:
                raise WorkbenchError("浏览器会话已关闭")
            touch_session(sessions, session)
            if action == "close":
                sessions.pop(sessionid, None)
                await session.close()
                await self.finish({"ok": True})
            elif action == "input":
                await self.finish(await session.input(payload) or {"ok": True})
            elif action == "navigate":
                await session.goto(payload.get("url"))
                await self.finish({"ok": True})
            elif action == "record":
                await self.record(session, payload)
            elif action == "save":
                await self.save(session, payload)
            elif action == "resume":
                await self.resume(session)
            else:
                raise tornado.web.HTTPError(404)

    async def record(self, session, payload):
        if session.taskid is not None:
            raise WorkbenchError("请在完成站点验证后恢复原任务")
        mode = payload.get("mode")
        if mode == "cancel":
            session.recording = None
            await self.finish({"ok": True})
            return
        if mode in ("auto", "manual"):
            if not ai_client.AIClient().enabled:
                raise WorkbenchError("请先配置自建模型服务")
            await session.begin()
            if mode == "manual" or not await session.auto_click():
                await self.finish({"manual": True})
                return
            await session.finish_recording()
        elif mode == "finish":
            if session.recording:
                await session.finish_recording()
        elif mode != "retry":
            raise ValueError()
        if not session.captured:
            raise WorkbenchError("请先录制一次签到")
        session.draft = await analyze_recording(session.captured, ai_client.AIClient())
        await self.finish({"draft": session.draft})

    async def save(self, session, payload):
        if session.taskid is not None or not session.draft or not session.captured:
            raise WorkbenchError("请先生成签到脚本草稿")
        if session.saved:
            await self.finish(session.saved)
            return
        name = payload.get("name", "")
        clock = payload.get("clock", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise WorkbenchError("请输入 1 到 100 字符的任务名称")
        if not isinstance(clock, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
            raise WorkbenchError("请选择每日签到时间")
        recording = session.captured
        if not recording.success:
            raise WorkbenchError("录制时未检测到站点确认签到的提示，请完成签到后重新录制")
        state = await session.verification_state(recording.origin)
        times = clock.split(":")
        schedule = {"sw": True, "mode": "cron", "cron_val": f"{int(times[1])} {int(times[0])} * * *"}
        next_at = Cal().cal_next_ts(schedule).get("ts")
        if not next_at:
            raise WorkbenchError("无法计算下次运行时间")
        script = dict(origin=recording.origin, url=recording.start_url, steps=recording.steps)
        owner = session.owner
        async with self.db.transaction() as txn:
            empty = await self.db.user.encrypt(owner, [], sql_session=txn)
            tplid = await self.db.tpl.add(owner, empty, empty, "[]", "{}", sql_session=txn)
            await self.db.tpl.mod(
                tplid,
                sitename=name.strip(),
                siteurl=recording.origin,
                note="AI 工作台录制的浏览器签到任务",
                lock=True,
                _groups=Task.BROWSER_GROUP,
                sql_session=txn,
            )
            env = await self.db.user.encrypt(owner, {"_qd_browser": script}, sql_session=txn)
            taskid = await self.db.task.add(tplid, owner, env, sql_session=txn)
            encrypted = await self.db.user.encrypt(owner, state, sql_session=txn)
            await self.db.task.mod(
                taskid,
                next=next_at,
                newontime=json.dumps(schedule),
                _groups=Task.BROWSER_GROUP,
                browser_state=Task.BROWSER_READY,
                browser_session=encrypted,
                sql_session=txn,
            )
        session.saved = {"task": taskid, "url": f"/task/{taskid}/log"}
        await self.finish(session.saved)

    async def resume(self, session):
        if session.taskid is None:
            raise WorkbenchError("此会话没有关联待验证的任务")
        if session.saved:
            await self.finish(session.saved)
            return
        origin = web_origin(session.start_url)
        state = await session.verification_state(origin)
        async with self.db.transaction() as txn:
            task = self.check_permission(
                await self.db.task.get(
                    session.taskid,
                    fields=("id", "userid", "browser_state", "browser_session", "newontime"),
                    sql_session=txn,
                ),
                "w",
            )
            if not task["browser_state"]:
                raise WorkbenchError("此任务未使用浏览器登录状态")
            next_at = Cal().cal_next_ts(json.loads(task["newontime"])).get("ts")
            if not next_at:
                raise WorkbenchError("无法计算下次执行时间，请检查任务定时设置")
            encrypted = await self.db.user.encrypt(session.owner, state, sql_session=txn)
            changed = await self.db.task.mod(
                session.taskid,
                disabled=False,
                next=next_at,
                browser_state=Task.BROWSER_READY,
                browser_session=encrypted,
                expected={key: task[key] for key in ("browser_state", "browser_session", "newontime")},
                last_failed_count=0,
                sql_session=txn,
            )
            if not changed:
                raise WorkbenchError("任务已更新，请刷新后查看")
        session.saved = {"task": session.taskid, "url": f"/task/{session.taskid}/log"}
        await self.finish(session.saved)


handlers = [
    (r"/ai-workbench/?", WorkbenchPage),
    (r"/ai-workbench/chat", WorkbenchChat),
    (r"/ai-workbench/browser", WorkbenchStart),
    (
        r"/ai-workbench/browser/([A-Za-z0-9_-]{32})/(frame|input|navigate|record|save|resume|close)",
        WorkbenchSessionHandler,
    ),
]
