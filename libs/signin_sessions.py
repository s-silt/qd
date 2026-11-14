"""Admit, find and expire workbench sessions for one QD process."""

import asyncio

from libs import playwright_capture
from libs.signin_workbench import BrowserSession, WorkbenchError, browser_enabled, web_origin


def touch_session(sessions, session):
    async def expire():
        async with session.lock:
            if session.expires is deadline and sessions.pop(session.id, None) is session:
                await session.close()

    session.touch(lambda: asyncio.create_task(expire()))
    deadline = session.expires


def get_owned_session(sessions, sessionid, owner):
    session = sessions.get(sessionid)
    if session is None or session.owner != owner:
        return None
    return session


async def prune_stale_sessions(sessions, owner=None):
    for sid, existing in list(sessions.items()):
        if owner is not None and existing.owner != owner:
            continue
        if existing.lock.locked():
            continue
        if existing.page and (not existing.page.is_closed() or (existing.context and existing.context.pages)):
            continue
        async with existing.lock:
            if sessions.pop(sid, None) is existing:
                await existing.close()


async def admit_session(sessions, owner, *, url=None, taskid=None, templateid=None, state=None, script=None):
    if not browser_enabled():
        raise WorkbenchError(
            "在线浏览器需要启用 AI_WORKBENCH_BROWSER、安装 Chromium、使用单进程和独立部署密钥"
        )
    web_origin(url)
    await prune_stale_sessions(sessions, owner=owner)
    for existing in list(sessions.values()):
        if (
            existing.owner == owner
            and existing.taskid == taskid
            and existing.templateid == templateid
            and existing.start_url == url
            and not existing.lock.locked()
            and existing.page
            and not existing.page.is_closed()
        ):
            touch_session(sessions, existing)
            return existing
    session = BrowserSession(owner, url, taskid=taskid)
    session.templateid = templateid
    session.template_script = script
    max_slots = max(1, playwright_capture.PLAYWRIGHT_MAX_CONCURRENT)
    if len(sessions) >= max_slots or any(existing.owner == owner for existing in sessions.values()):
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
    return session
