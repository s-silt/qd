"""pytest 配置 + 共用 fixture (集成测试用)。

运行需要安装 Playwright + 浏览器:
    pip install -r requirements-dev.txt
    pip install playwright==1.49.0
    playwright install chromium

或者直接在 sidecar 镜像内执行:
    docker compose -f docker-compose.local.yml exec playwright \\
        sh -c "cd /app && pip install -r requirements-dev.txt && pytest -v"

如果 playwright / pytest-asyncio / aiohttp 任一缺失, fixtures 不会注册;
test_integration.py 顶部用 importorskip 跳过, test_button_finder.py 的
纯逻辑用例不受影响。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

THIS_DIR = Path(__file__).parent
sys.path.insert(0, str(THIS_DIR))
PAGES_DIR = THIS_DIR / "test_pages"

# 缺任一可选依赖时, 跳过 fixture 注册; 集成测试模块顶部 importorskip 会跳过用例
try:
    import pytest_asyncio  # type: ignore
    from aiohttp import web  # type: ignore
    from playwright.async_api import async_playwright  # type: ignore

    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False


if _DEPS_OK:
    import socket


    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @pytest_asyncio.fixture(scope="session")
    async def browser():
        """Session 级共享浏览器, 减少每个测试的启动开销。"""
        async with async_playwright() as pw:
            b = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            try:
                yield b
            finally:
                await b.close()

    @pytest_asyncio.fixture
    async def site() -> AsyncIterator[dict]:
        """启动一个本地 aiohttp 测试服务器, 提供:
            GET  /sign       签到页 (含按钮)
            GET  /login      登录页
            GET  /protected  无 cookie 跳 /login, 有 cookie 返回签到页
            POST /api/sign   返回 {"ok": true, "message": "已签到"}
        """
        sign_html = (PAGES_DIR / "sign.html").read_text(encoding="utf-8")
        login_html = (PAGES_DIR / "login.html").read_text(encoding="utf-8")

        sign_post_count = {"n": 0}

        async def get_sign(_req):
            return web.Response(text=sign_html, content_type="text/html", charset="utf-8")

        async def get_login(_req):
            return web.Response(text=login_html, content_type="text/html", charset="utf-8")

        async def get_protected(req):
            if req.cookies.get("session") != "valid":
                return web.HTTPFound("/login")
            return web.Response(text=sign_html, content_type="text/html", charset="utf-8")

        async def post_sign(_req):
            sign_post_count["n"] += 1
            return web.json_response({"ok": True, "message": "已签到"})

        app = web.Application()
        app.router.add_get("/sign", get_sign)
        app.router.add_get("/login", get_login)
        app.router.add_get("/protected", get_protected)
        app.router.add_post("/api/sign", post_sign)

        runner = web.AppRunner(app)
        await runner.setup()
        port = _free_port()
        site_obj = web.TCPSite(runner, "127.0.0.1", port)
        await site_obj.start()
        base = f"http://127.0.0.1:{port}"
        try:
            yield {"base": base, "post_count": sign_post_count}
        finally:
            await runner.cleanup()
