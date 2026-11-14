import asyncio
import json
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
from db import DB, Task, basedb
from db.db_converter import DBconverter
from libs.ai.har_pipeline import preprocess_har
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
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'workbench.db'}")
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
        model_calls.append(await request.json())
        return web.json_response({"choices": [{"message": {"content": "已分析脱敏记录。"}}]})

    model_app = web.Application()
    model_app.router.add_post("/v1/chat/completions", model_reply)
    model_runner = web.AppRunner(model_app)
    await model_runner.setup()
    model_site = web.TCPSite(model_runner, "127.0.0.1", 0)
    await model_site.start()
    model_port = model_runner.addresses[0][1]
    monkeypatch.setattr(config, "ai_base_url", f"http://127.0.0.1:{model_port}/v1")

    db = DB()
    converter = DBconverter()
    await converter.convert_new_type(db)
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
        session.page = SimpleNamespace(url=site_url)

    async def state_fixture(session, origin):
        assert origin == "https://example.com"
        return {"cookies": [{"name": "auth", "value": private_cookie}], "origins": []}

    monkeypatch.setattr(signin.BrowserSession, "open", open_fixture)
    monkeypatch.setattr(signin.BrowserSession, "verification_state", state_fixture)

    steps = [{"selector": "#checkin", "tag": "button", "text": "签到"}]

    async def paused_replay(script, state):
        assert script["steps"] == steps
        assert state["cookies"][0]["value"] == private_cookie
        raise signin.VerificationNeeded("站点要求重新验证")

    monkeypatch.setattr(worker, "replay", paused_replay)
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
            assert response.status == 200 and (await response.json())["draft"]["verified"]
            assert private_cookie not in json.dumps(model_calls)
            assert "example.com" not in model_calls[-1]["messages"][-1]["content"]
            for _ in range(2):
                response = await client.post(
                    f"{url}/ai-workbench/browser/{sid}/save", json={"name": "Test site", "clock": "08:00"}
                )
                assert response.status == 200 and (await response.json())["task"] == 1
            await client.post(f"{url}/ai-workbench/browser/{sid}/close", json={})

            task = await db.task.get(1)
            assert private_cookie.encode() not in task["browser_session"]
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

            async def replay_success(script, state):
                return "站点页面确认签到成功", state

            monkeypatch.setattr(worker, "replay", replay_success)
            response = await client.post(f"{url}/task/1/run", allow_redirects=False)
            assert response.status == 302 and response.headers["Location"] == "/task/1/log"
            assert (await db.task.get(1))["success_count"] == 1
    finally:
        for session in app.workbench_sessions.values():
            await session.close()
        server.stop()
        await server.close_all_connections()
        await model_runner.cleanup()
        await engine.dispose()
