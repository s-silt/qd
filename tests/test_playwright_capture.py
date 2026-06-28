#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""libs.playwright_capture 的单测。

playwright 本地未安装, 全程 mock, 不真起浏览器。
覆盖:
- SSRF: 每跳/重定向白名单 + IP 段拦截 (route guard), HAR 剔除内网响应
- 无头模式无法登录时明确返回 ok=False (不再盲睡报成功)
- context / 临时 HAR 文件 try/finally 兜底, 不泄漏
- init_browser 加锁 + _semaphore 只建一次
- capture_cookies 走 _semaphore + try/finally + 事件驱动登录检测
- 登录重定向用 host 变化 + 会话 cookie + 登录表单字段判断 (不再用正则误杀)
- capture_cookies 返回前净化 storage_state
"""

import asyncio
import json
import os
import types

import pytest

from libs import playwright_capture as pc


# --------------------------------------------------------------------------
# 测试用假对象
# --------------------------------------------------------------------------
class FakeRoute:
    def __init__(self, url):
        self.request = types.SimpleNamespace(url=url)
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class FakeContext:
    def __init__(self, page, storage_state=None):
        self._page = page
        self._storage_state = storage_state or {"cookies": [], "origins": []}
        self.closed = False
        self.routes = []

    async def add_init_script(self, *_a, **_k):
        pass

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def new_page(self):
        return self._page

    async def storage_state(self, **_k):
        # page 可动态更新 storage_state
        return self._page.storage_state_value or self._storage_state

    async def close(self):
        self.closed = True


class FakePage:
    def __init__(self, url, has_pw=False, storage_state_value=None,
                 goto_raises=False):
        self.url = url
        self._has_pw = has_pw
        self.storage_state_value = storage_state_value
        self._goto_raises = goto_raises
        self.default_timeout = None

    def set_default_timeout(self, t):
        self.default_timeout = t

    async def goto(self, url, **_k):
        if self._goto_raises:
            raise RuntimeError("boom goto")
        self.url = url

    async def evaluate(self, _js):
        return self._has_pw

    async def wait_for_load_state(self, *_a, **_k):
        pass


class FakeBrowser:
    def __init__(self, context):
        self._context = context
        self.new_context_kwargs = None

    def is_connected(self):
        return True

    async def new_context(self, **kwargs):
        self.new_context_kwargs = kwargs
        return self._context


def _install_fake_browser(monkeypatch, page, storage_state=None):
    ctx = FakeContext(page, storage_state=storage_state)
    browser = FakeBrowser(ctx)

    async def _noop_init():
        return None

    monkeypatch.setattr(pc, "init_browser", _noop_init)
    monkeypatch.setattr(pc, "_browser", browser)
    monkeypatch.setattr(pc, "_semaphore", asyncio.Semaphore(2))
    monkeypatch.setattr(pc, "_initialized", True)
    # 让事件驱动登录轮询快速结束
    monkeypatch.setattr(pc, "_LOGIN_POLL_INTERVAL", 0.001, raising=False)
    return browser, ctx


# --------------------------------------------------------------------------
# 纯函数: host 检查 (白名单 + IP 段叠加)
# --------------------------------------------------------------------------
def test_check_host_allowed_blocks_metadata_even_without_whitelist(monkeypatch):
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", [])
    assert pc._check_host_allowed("169.254.169.254")  # 云元数据被拦
    assert pc._check_host_allowed("127.0.0.1")
    assert pc._check_host_allowed("example.com") is None  # 公网放行


def test_check_host_allowed_whitelist_also_blocks_internal_ip(monkeypatch):
    # 白名单非空分支也要叠加 IP 段拦截
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", ["example.com"])
    assert pc._check_host_allowed("example.com") is None
    assert pc._check_host_allowed("evil.com")  # 不在白名单
    # 即使把内网 IP 放进白名单也无意义 (这里测白名单外的环回)
    assert pc._check_host_allowed("127.0.0.1")


def test_validate_url_rejects_metadata(monkeypatch):
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", [])
    with pytest.raises(ValueError):
        pc.validate_url("http://169.254.169.254/latest/meta-data/")
    assert pc.validate_url("https://example.com/x") == "https://example.com/x"


# --------------------------------------------------------------------------
# route guard: 每跳 / 重定向 abort 内网
# --------------------------------------------------------------------------
def test_route_guard_aborts_internal(monkeypatch):
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", [])
    r = FakeRoute("http://169.254.169.254/latest/")
    asyncio.run(pc._route_guard(r))
    assert r.aborted is True
    assert r.continued is False


def test_route_guard_allows_public(monkeypatch):
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", [])
    r = FakeRoute("https://example.com/api")
    asyncio.run(pc._route_guard(r))
    assert r.continued is True
    assert r.aborted is False


def test_route_guard_whitelist_blocks_offlist(monkeypatch):
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", ["example.com"])
    r = FakeRoute("https://attacker.com/x")
    asyncio.run(pc._route_guard(r))
    assert r.aborted is True


# --------------------------------------------------------------------------
# HAR 剔除内网响应
# --------------------------------------------------------------------------
def test_strip_internal_from_har(monkeypatch):
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", [])
    har = {
        "log": {
            "entries": [
                {"request": {"url": "https://example.com/a"}},
                {"request": {"url": "http://169.254.169.254/meta"}},
                {"request": {"url": "http://127.0.0.1/secret"}},
                {"request": {"url": "https://cdn.example.com/b"}},
            ]
        }
    }
    out = pc._strip_internal_from_har(har)
    urls = [e["request"]["url"] for e in out["log"]["entries"]]
    assert "https://example.com/a" in urls
    assert "https://cdn.example.com/b" in urls
    assert all("169.254" not in u and "127.0.0.1" not in u for u in urls)


# --------------------------------------------------------------------------
# 登录重定向判定 (host 变化 + 会话 cookie + 登录表单字段)
# --------------------------------------------------------------------------
def test_is_login_redirect_cross_site_with_form():
    assert pc._is_login_redirect(
        "https://app.example.com/dash",
        "https://sso.other.com/login",
        has_password_field=True,
        has_session_cookie=False,
    )


def test_is_login_redirect_false_when_session_and_no_form():
    # 正常站点 URL 里含 'auth' 但已登录 (有会话 cookie, 无密码框) -> 不误杀
    assert not pc._is_login_redirect(
        "https://example.com/oauth/callback",
        "https://example.com/author/me",
        has_password_field=False,
        has_session_cookie=True,
    )


def test_is_login_redirect_same_host_login_form_no_session():
    assert pc._is_login_redirect(
        "https://example.com/dash",
        "https://example.com/account/login",
        has_password_field=True,
        has_session_cookie=False,
    )


def test_has_session_cookie():
    assert pc._has_session_cookie([{"name": "sessionid", "value": "abc"}])
    assert pc._has_session_cookie([{"name": "JWT", "value": "x"}])
    assert not pc._has_session_cookie([{"name": "lang", "value": "zh"}])
    assert not pc._has_session_cookie([{"name": "sessionid", "value": ""}])


# --------------------------------------------------------------------------
# capture_cookies: 无头模式无法登录 -> ok=False (不再盲睡报成功)
# --------------------------------------------------------------------------
def test_capture_cookies_headless_no_login_returns_false(monkeypatch):
    monkeypatch.setattr(pc, "PLAYWRIGHT_HEADLESS", True)
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", [])
    # 页面始终显示密码框, 只有游客 cookie -> 永远登录不上
    page = FakePage(
        "https://example.com/login",
        has_pw=True,
        storage_state_value={"cookies": [{"name": "guest", "value": "1",
                                          "domain": "example.com"}],
                             "origins": []},
    )
    _, ctx = _install_fake_browser(monkeypatch, page)

    res = asyncio.run(pc.capture_cookies("https://example.com/", timeout_ms=50))
    assert res["ok"] is False
    assert "扩展" in res["error"] or "本机" in res["error"] or "无头" in res["error"]
    assert ctx.closed is True  # context 不泄漏


def test_capture_cookies_success_sanitizes_storage_state(monkeypatch):
    monkeypatch.setattr(pc, "PLAYWRIGHT_HEADLESS", False)
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", [])
    # 登录成功: 无密码框 + 出现会话 cookie; 同时混入跨域 cookie 应被净化
    state = {
        "cookies": [
            {"name": "sessionid", "value": "secret", "domain": "example.com"},
            {"name": "sso_token", "value": "leak", "domain": "pay.other.com"},
        ],
        "origins": [],
    }
    page = FakePage(
        "https://example.com/dashboard",
        has_pw=False,
        storage_state_value=state,
    )
    _, ctx = _install_fake_browser(monkeypatch, page)

    res = asyncio.run(pc.capture_cookies("https://example.com/", timeout_ms=2000))
    assert res["ok"] is True
    names = [c["name"] for c in res["storage_state"]["cookies"]]
    assert "sessionid" in names
    assert "sso_token" not in names  # 跨域令牌被净化
    assert "sso_token" not in res["cookies"]
    assert ctx.closed is True


def test_capture_cookies_closes_context_on_exception(monkeypatch):
    monkeypatch.setattr(pc, "PLAYWRIGHT_HEADLESS", False)
    monkeypatch.setattr(pc, "PLAYWRIGHT_ALLOW_HOSTS", [])
    page = FakePage("https://example.com/", goto_raises=True)
    _, ctx = _install_fake_browser(monkeypatch, page)

    res = asyncio.run(pc.capture_cookies("https://example.com/", timeout_ms=2000))
    assert res["ok"] is False
    assert ctx.closed is True


# --------------------------------------------------------------------------
# init_browser: _semaphore 只建一次 (不覆盖既有)
# --------------------------------------------------------------------------
def test_init_browser_lock_exists():
    lock = pc._get_init_lock()
    assert isinstance(lock, asyncio.Lock)
    assert pc._get_init_lock() is lock  # 复用同一把锁


def test_capture_har_playwright_missing_returns_error(monkeypatch):
    # 模拟 playwright 未安装时 capture_har 优雅降级
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    res = asyncio.run(pc.capture_har("https://example.com/"))
    assert res["ok"] is False
    assert "playwright" in res["error"]
