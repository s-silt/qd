import asyncio
import json
import logging
import time
from types import SimpleNamespace

import aiohttp
import pytest
import umsgpack
from aiohttp import web
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_scoped_session,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker
from tornado.httpserver import HTTPServer
from tornado.netutil import bind_sockets
from tornado.web import create_signed_value

import config
import libs.browser_egress as egress
import libs.mcrypto as crypto
import libs.signin_workbench as signin
import worker
from web.handlers import ai_workbench
from db import Task, basedb
from db.db_converter import DBconverter
from libs.ai.har_pipeline import preprocess_har
from libs.signin_sessions import admit_session
from run import _init_database
from web.app import Application


def test_model_summary_uses_fixed_fields_and_redacts_paths():
    item = signin.capture_summary(
        "https://example.com/api/checkin/opaque-key?day=secret", "POST", "fetch", 200
    )
    assert item == {
        "method": "POST",
        "path": "/api/checkin/{value}",
        "type": "fetch",
        "status": 200,
        "signin_hint": True,
    }
    assert signin.capture_summary("https://example.com/api/login", "POST", "fetch", 200) is None
    assert (
        signin.capture_summary("https://example.com/api/checkin?SESSION_ID=secret", "GET", "fetch", 200)
        is None
    )
    assert signin.credentials_in_chat("短信验证码：564872")
    assert signin.credentials_in_chat("password: my-secret")
    assert not signin.credentials_in_chat("微信扫码后怎样进入签到页？")


def test_legacy_har_analysis_redacts_captured_values():
    entry = {
        "request": {
            "method": "POST",
            "url": "https://example.com/checkin?csrf=fixture-param&day=1",
            "headers": [
                {"name": "Authorization", "value": "Bearer fixture-secret"},
                {"name": "Cookie", "value": "session=fixture-cookie"},
            ],
            "postData": {"text": '{"password":"fixture-pass","captcha":"693482","day":1}'},
        },
        "response": {
            "status": 200,
            "content": {
                "mimeType": "application/json",
                "text": '{"code":0,"msg":"签到成功 fixture-cookie","id_token":"fixture-id"}',
            },
        },
    }
    login = {"request": {"method": "POST", "url": "https://example.com/login"}}
    entries = preprocess_har({"log": {"entries": [entry, login]}}, 10)
    assert len(entries) == 1
    assert "Bearer {{token}}" == entries[0]["headers"][0]["value"]
    assert entries[0]["respSignals"]["code"] == "0"
    assert "fixture" not in json.dumps(entries) and "693482" not in json.dumps(entries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "www.example.com\nPerforming security verification\n"
            "This page is displayed while the website verifies you are not a bot.\nVerifying...",
            True,
        ),
        ("Account guide: Performing security verification on your own website.", False),
    ],
)
async def test_security_verification_page_detection(body, expected):
    class Page:
        url = "https://example.com/checkin"
        frames = []

        def locator(self, selector):
            assert selector == "body" or "input" in selector
            return self

        async def count(self):
            return 0

        async def inner_text(self, *, timeout):
            return body

    assert await signin.challenge_visible(Page()) is expected


@pytest.mark.asyncio
async def test_clip_storage_state_to_target_host():
    class Context:
        async def storage_state(self, *, indexed_db):
            assert indexed_db
            return {
                "cookies": [{"domain": ".example.com"}, {"domain": "auth.example.net"}],
                "origins": [
                    {"origin": "https://example.com", "indexedDB": []},
                    {"origin": "https://auth.example.net"},
                ],
            }

    assert await signin.storage_for(Context(), "https://example.com") == {
        "cookies": [{"domain": ".example.com"}],
        "origins": [{"origin": "https://example.com", "indexedDB": []}],
    }


@pytest.mark.asyncio
async def test_browser_egress_limits_direct_addresses():
    for host in ("127.0.0.1", "10.0.0.1", "::ffff:127.0.0.1"):
        with pytest.raises(ValueError):
            await egress.public_addresses(host, 443)
    with pytest.raises(ValueError):
        await egress.public_addresses("example.com", 8080)


@pytest.mark.asyncio
async def test_session_admission_reserves_slot_while_browser_opens(monkeypatch):
    opening = asyncio.Event()
    ready = asyncio.Event()
    sessions = {}

    async def open_fixture(session, state=None):
        opening.set()
        await ready.wait()
        session.page = SimpleNamespace(is_closed=lambda: False)

    monkeypatch.setattr(config, "ai_workbench_browser", True)
    monkeypatch.setattr(signin.playwright_capture, "is_available", lambda: True)
    monkeypatch.setattr(signin.playwright_capture, "PLAYWRIGHT_MAX_CONCURRENT", 1)
    monkeypatch.setattr(signin.BrowserSession, "open", open_fixture)

    url = "https://example.com/checkin"
    first = asyncio.create_task(admit_session(sessions, 1, url=url))
    await opening.wait()
    try:
        with pytest.raises(signin.WorkbenchError):
            await admit_session(sessions, 1, url=url)
        with pytest.raises(signin.WorkbenchError):
            await admit_session(sessions, 2, url=url)
        assert len(sessions) == 1
    finally:
        ready.set()
        session = await first
        await session.close()


@pytest.mark.asyncio
async def test_workbench_save_pause_resume_and_model_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ai_workbench_browser", True)
    monkeypatch.setattr(config, "ai_api_key", "")
    monkeypatch.setattr(config, "ai_allow_no_key", True)
    monkeypatch.setattr(config, "debug", True)
    password_hash = crypto.password_hash
    monkeypatch.setattr(
        crypto, "password_hash", lambda word, salt=None: password_hash(word, salt, iterations=10)
    )
    monkeypatch.setattr(signin.playwright_capture, "is_available", lambda: True)
    db_path = tmp_path / "workbench.db"
    monkeypatch.setattr(config.sqlite3, "path", str(db_path))
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setattr(
        basedb,
        "async_session",
        async_scoped_session(
            sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
            scopefunc=asyncio.current_task,
        ),
    )

    model_calls = []

    async def model_reply(request):
        assert "Authorization" not in request.headers
        payload = await request.json()
        model_calls.append(payload)
        content = payload["messages"][-1]["content"]
        if '"actions"' in content:
            content = json.dumps({"summary": "已分析脱敏记录。", "steps": [{"id": 1, "label": "执行签到"}]})
        else:
            content = "已分析脱敏记录。"
        return web.json_response({"choices": [{"message": {"content": content}}]})

    model_app = web.Application()
    model_app.router.add_post("/v1/chat/completions", model_reply)
    model_runner = web.AppRunner(model_app)
    await model_runner.setup()
    model_site = web.TCPSite(model_runner, "127.0.0.1", 0)
    await model_site.start()
    model_port = model_runner.addresses[0][1]
    monkeypatch.setattr(config, "ai_base_url", f"http://127.0.0.1:{model_port}/v1")

    db = await _init_database(logging.getLogger(__name__))
    converter = DBconverter()
    await db.user.add("owner@example.com", "example-fixture-pass", "127.0.0.1")
    await db.user.add("guest@example.com", "example-other-pass", "127.0.0.1")
    app = Application(db)
    server = HTTPServer(app)
    sockets = bind_sockets(0, address="127.0.0.1")
    server.add_sockets(sockets)
    url = f"http://127.0.0.1:{sockets[0].getsockname()[1]}"

    def token(userid):
        return create_signed_value(config.cookie_secret, "user", umsgpack.packb({"id": userid})).decode()

    site_url = "https://example.com/checkin"
    private_cookie = "fixture-credentials-local-only"

    async def open_fixture(session, state=None):
        if session.taskid:
            assert state["cookies"][0]["value"] == private_cookie
        session.page = SimpleNamespace(url=site_url, is_closed=lambda: False)

    async def state_fixture(session, origin):
        assert origin == "https://example.com"
        return {"cookies": [{"name": "auth", "value": private_cookie}], "origins": []}

    monkeypatch.setattr(signin.BrowserSession, "open", open_fixture)
    monkeypatch.setattr(signin.BrowserSession, "verification_state", state_fixture)

    steps = [{"selector": "#checkin", "tag": "button", "text": "签到"}]

    async def replay_success(script, state):
        assert script["steps"] == steps
        assert state["cookies"][0]["value"] == private_cookie
        return "站点页面确认签到成功", state

    async def paused_replay(script, state):
        assert script["steps"] == steps
        assert state["cookies"][0]["value"] == private_cookie
        raise signin.VerificationNeeded("站点要求重新验证")

    monkeypatch.setattr(worker, "replay", paused_replay)
    monkeypatch.setattr(ai_workbench, "replay", replay_success)
    headers = {"X-QD-Workbench": "1"}
    try:
        async with aiohttp.ClientSession(cookies={"user": token(1)}, headers=headers) as client:
            response = await client.get(f"{url}/ai-workbench/")
            assert response.status == 200 and "AI 签到工作台" in await response.text()
            response = await client.post(
                f"{url}/ai-workbench/chat", json={"messages": [{"role": "user", "content": "密码：123456"}]}
            )
            assert response.status == 400 and not model_calls
            response = await client.post(
                f"{url}/ai-workbench/chat", json={"messages": [{"role": "user", "content": "怎样重新验证？"}]}
            )
            assert response.status == 200 and len(model_calls) == 1
            response = await client.post(f"{url}/ai-workbench/browser", json={"url": site_url})
            assert response.status == 200
            sid = (await response.json())["session"]
            async with aiohttp.ClientSession(cookies={"user": token(2)}, headers=headers) as guest:
                assert (await guest.get(f"{url}/ai-workbench/browser/{sid}/frame")).status == 404

            session = app.workbench_sessions[sid]
            session.captured = signin.Recording(
                "https://example.com",
                site_url,
                steps=steps,
                success=True,
                summaries=[signin.capture_summary(f"{site_url}?day={private_cookie}", "POST", "fetch", 200)],
            )
            response = await client.post(f"{url}/ai-workbench/browser/{sid}/record", json={"mode": "retry"})
            assert response.status == 200
            draft = (await response.json())["draft"]
            assert draft["verified"] and draft["script"]["steps"] == steps
            assert draft["script"]["labels"] == ["执行签到"]
            assert private_cookie not in json.dumps(model_calls)
            assert "example.com" not in model_calls[-1]["messages"][-1]["content"]
            assert "#checkin" not in model_calls[-1]["messages"][-1]["content"]
            response = await client.post(f"{url}/ai-workbench/browser/{sid}/test", json={})
            assert response.status == 200 and (await response.json())["result"] == "站点页面确认签到成功"
            response = await client.post(f"{url}/ai-workbench/browser/{sid}/save", json={"name": "Test site", "mode": "template"})
            assert response.status == 200 and (await response.json())["template"] == 1
            tpl = await db.tpl.get(1)
            assert tpl["browser"] and not tpl["lock"]
            assert (await db.user.decrypt(1, tpl["tpl"]))["steps"] == steps
            assert await db.task.list(1) == []
            response = await client.get(f"{url}/tpl/1/edit")
            detail = await response.text()
            assert response.status == 200 and "执行签到" in detail and private_cookie not in detail
            assert "登录站点并创建任务" in await (await client.get(f"{url}/task/new?tplid=1")).text()
            for _ in range(2):
                response = await client.post(
                    f"{url}/ai-workbench/browser/{sid}/save", json={"name": "Test site", "clock": "08:00"}
                )
                assert response.status == 200 and (await response.json())["task"] == 1
            await client.post(f"{url}/ai-workbench/browser/{sid}/close", json={})

            task = await db.task.get(1)
            assert private_cookie.encode() not in task["browser_session"]
            await db.task.mod(1, next=int(time.time()) - 1)
            assert await worker.BaseWorker(db).do(task) is False
            task = await db.task.get(1)
            assert task["browser_state"] == Task.BROWSER_VERIFY and task["next"] is None
            assert await db.task.scan() == []
            response = await client.get(f"{url}/my/")
            assert response.status == 200 and "待重新验证" in await response.text()
            await converter.convert_new_type(db)
            assert (await db.task.get(1))["browser_state"] == Task.BROWSER_VERIFY

            response = await client.post(f"{url}/ai-workbench/browser", json={"task": 1})
            sid = (await response.json())["session"]
            response = await client.post(f"{url}/ai-workbench/browser/{sid}/resume", json={})
            assert response.status == 200
            task = await db.task.get(1)
            assert task["browser_state"] == Task.BROWSER_READY and task["next"] and not task["disabled"]

            monkeypatch.setattr(worker, "replay", replay_success)
            response = await client.post(f"{url}/task/1/run", headers={"X-Requested-With": "XMLHttpRequest"})
            assert response.status == 200 and "签到成功" in await response.text()
            assert (await db.task.get(1))["success_count"] == 1
            await client.post(f"{url}/ai-workbench/browser/{sid}/close", json={})
            response = await client.post(f"{url}/ai-workbench/browser", json={"template": 1})
            assert response.status == 200
            sid = (await response.json())["session"]
            response = await client.post(f"{url}/ai-workbench/browser/{sid}/save", json={"mode": "run", "clock": "09:10"})
            assert response.status == 200 and (await response.json())["task"] == 2
            assert (await db.task.get(2))["success_count"] == 1
            assert (await db.task.get(2))["tplid"] == 1
            assert (await client.post(f"{url}/tpl/1/del", allow_redirects=False)).status == 409
            await client.post(f"{url}/task/1/del", allow_redirects=False)
            assert await db.task.get(1) is None and await db.tpl.get(1)
    finally:
        for session in app.workbench_sessions.values():
            await session.close()
        server.stop()
        await server.close_all_connections()
        await model_runner.cleanup()
        await engine.dispose()
