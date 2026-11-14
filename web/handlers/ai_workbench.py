"""QD chat and human-assisted sign-in recording HTTP handlers."""

import asyncio
import json
import re
from contextlib import suppress
from functools import wraps

import tornado.web
from tornado.iostream import StreamClosedError

from db.task import Task
from libs import ai_client
from libs.signin_authoring import author_template
from libs.signin_recording import chat_reply, record_workflow
from libs.signin_sessions import admit_session, get_owned_session, touch_session
from libs.signin_tasks import load_workbench_target, resume_verified_task, save_template_and_task
from libs.signin_workbench import (
    VerificationNeeded,
    WorkbenchError,
    WorkbenchPermissionError,
    browser_enabled,
    page_label,
    replay,
    web_origin,
)
from web.handlers.base import BaseHandler, logger_web_handler


def workbench_post(method, *, body_limit=32000):
    @wraps(method)
    @tornado.web.authenticated
    async def handler(self, *args):
        self.set_header("Cache-Control", "no-store")
        if not self.request.headers.get("Content-Type", "").startswith(
            "application/json"
        ) or not self.request.headers.get("X-QD-Workbench"):
            raise tornado.web.HTTPError(403)
        if len(self.request.body) > body_limit:
            raise tornado.web.HTTPError(413)
        try:
            body = self.request.body
            if len(body) > 32000:
                payload = await asyncio.to_thread(json.loads, body)
            else:
                payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("请求格式无效")
            await method(self, payload, *args)
        except tornado.web.HTTPError:
            raise
        except WorkbenchPermissionError as exc:
            raise tornado.web.HTTPError(exc.status_code) from None
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


AUTHORING_BODY_LIMIT = 5 * 1024 * 1024


class WorkbenchAuthor(BaseHandler):
    async def analyze(self, payload):
        draft = await author_template(payload.get("har"), payload.get("site"), ai_client.AIClient())
        await self.finish(draft)

    post = workbench_post(analyze, body_limit=AUTHORING_BODY_LIMIT)


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
        templateid = self.get_query_argument("template", "")
        task_name = ""
        script_name = ""
        if taskid and templateid:
            raise tornado.web.HTTPError(400)
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
        if templateid:
            if not re.fullmatch(r"[0-9]{1,10}", templateid):
                raise tornado.web.HTTPError(400)
            tpl = self.check_permission(
                await self.db.tpl.get(templateid, fields=("id", "userid", "browser", "sitename")), "w"
            )
            if not tpl["browser"]:
                raise tornado.web.HTTPError(404)
            script_name = tpl["sitename"] or "浏览器模板"
        await self.render(
            "ai_workbench.html" if taskid or templateid or self.get_argument("mode", "") == "browser"
            else "signin_authoring.html",
            model=client.model,
            owner=self.current_user["id"],
            max_upload=AUTHORING_BODY_LIMIT,
            model_enabled=client.enabled,
            browser_ready=browser_enabled(),
            taskid=taskid,
            task_name=task_name,
            templateid=templateid,
            script_name=script_name,
        )


class WorkbenchChat(BaseHandler):
    @workbench_post
    async def post(self, payload):
        self.evil(+1)
        reply = await chat_reply(payload.get("messages", []), ai_client.AIClient())
        await self.finish({"text": reply})


class WorkbenchStart(BaseHandler):
    @workbench_post
    async def post(self, payload):
        if not browser_enabled():
            raise WorkbenchError(
                "在线浏览器需要启用 AI_WORKBENCH_BROWSER、安装 Chromium、使用单进程和独立部署密钥"
            )
        owner = self.current_user["id"]
        taskid = payload.get("task")
        templateid = payload.get("template")
        target = await load_workbench_target(
            self.db,
            owner,
            taskid=taskid,
            templateid=templateid,
        )
        url = target["url"] if (taskid is not None or templateid is not None) else payload.get("url")
        session = await admit_session(
            self.application.workbench_sessions,
            owner,
            url=url,
            taskid=taskid,
            templateid=templateid,
            state=target["state"],
            script=target["script"],
        )
        await self.finish({"session": session.id, "site": page_label(session.page.url)})


class WorkbenchSessionHandler(BaseHandler):
    def _session(self, sessionid):
        session = get_owned_session(
            self.application.workbench_sessions, sessionid, self.current_user["id"]
        )
        if session is None:
            raise tornado.web.HTTPError(404)
        return session

    @tornado.web.authenticated
    async def get(self, sessionid, action):
        session = self._session(sessionid)
        self.set_header("Cache-Control", "no-store")
        if action == "draft":
            await self.finish(
                {"draft": session.draft, "template": session.saved, "task": session.created_task}
            )
            return
        if action == "frame":
            if session.lock.locked():
                self.set_status(204)
                await self.finish()
                return
            async with session.lock:
                try:
                    info = await session.capture_frame()
                except Exception:
                    self.set_status(204)
                    with suppress(StreamClosedError):
                        await self.finish()
                    return
                self.set_header("X-QD-Site", info["site"])
                self.set_header("X-QD-Recording", info["recording"])
                self.set_header("X-QD-Verification", info["verification"])
                self.set_header("X-QD-Tabs", info["tabs"])
                self.set_header("X-QD-Active-Tab", info["active_tab"])
                self.set_header("Content-Type", "image/jpeg")
                with suppress(StreamClosedError):
                    await self.finish(info["frame"])
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
                if session.taskid is not None or session.templateid is not None:
                    raise WorkbenchError("此会话关联了现有模板或任务")
                url = payload.get("url")
                if web_origin(url) != web_origin(session.start_url):
                    raise WorkbenchError("如需切换站点，请先关闭当前浏览器")
                await session.goto(url)
                session.start_url = url
                session.draft = session.captured = session.saved = session.created_task = None
                await self.finish({"ok": True})
            elif action == "record":
                await self.finish(await record_workflow(session, payload.get("mode"), ai_client.AIClient()))
            elif action == "save":
                await self.save(session, payload)
            elif action == "test":
                await self.test_script(session)
            elif action == "resume":
                await self.finish(await resume_verified_task(self.db, session))
            else:
                raise tornado.web.HTTPError(404)

    async def test_script(self, session):
        if session.taskid is not None or not (session.template_script or session.draft):
            raise WorkbenchError("请先生成签到脚本")
        script = session.template_script or session.draft["script"]
        state = await session.verification_state(script["origin"])
        if session.templateid is not None:
            self.check_permission(await self.db.tpl.get(session.templateid, fields=("userid",)), "w")
        message, _ = await replay(script, state)
        await self.finish({"result": message})

    async def save(self, session, payload):
        result, run_now = await save_template_and_task(self.db, session, payload)
        if run_now:
            from worker import BaseWorker

            await BaseWorker(self.db).do(
                {"id": result["task"], "browser_state": Task.BROWSER_READY}, manual=True
            )
        await self.finish(result)


handlers = [
    (r"/ai-workbench/?", WorkbenchPage),
    (r"/ai-workbench/chat", WorkbenchChat),
    (r"/ai-workbench/author", WorkbenchAuthor),
    (r"/ai-workbench/browser", WorkbenchStart),
    (
        r"/ai-workbench/browser/([A-Za-z0-9_-]{32})/(frame|draft|input|navigate|record|test|save|resume|close)",
        WorkbenchSessionHandler,
    ),
]
