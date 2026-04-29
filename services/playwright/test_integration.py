"""Playwright sidecar 集成测试。

这些测试启动真实 Chromium 浏览器并访问本地测试服务器, 因此:
- 需要安装 playwright + chromium 才能运行
- 单测 (offline) 环境会被 conftest.py 整体跳过
- 在 sidecar 镜像内运行最方便: 见 conftest.py 顶部说明

覆盖场景:
1. 启发式找到 [data-testid] 签到按钮并点击 (HAR 含 POST /api/sign)
2. hint 关键字优先级
3. 用户指定 selector
4. Cookie 传入后请求自带 Cookie 头
5. storage_state 失效时检测重定向到 /login
6. 跨域 storage_state 自动剔除
7. 找不到按钮时返回候选列表
8. 找不到 selector 时返回错误
"""

from __future__ import annotations

import json

import pytest

# 任一依赖缺失则整个模块 skip, 不影响 test_button_finder.py 的纯逻辑测试
pytest.importorskip("playwright.async_api")
pytest.importorskip("pytest_asyncio")
pytest.importorskip("aiohttp")
pytest.importorskip("fastapi")  # CaptureRequest 来自 app.py, 依赖 fastapi/pydantic

pytestmark = pytest.mark.asyncio

from app import CaptureRequest, perform_capture  # noqa: E402


def _post_signs_in_har(har: dict, base_url: str) -> int:
    """统计 HAR 里 POST {base_url}/api/sign 的次数。"""
    entries = (har.get("log", {}) or {}).get("entries", []) or []
    return sum(
        1
        for e in entries
        if e.get("request", {}).get("method", "").upper() == "POST"
        and e.get("request", {}).get("url", "").startswith(f"{base_url}/api/sign")
    )


async def test_heuristic_finds_signin_button(browser, site):
    req = CaptureRequest(url=f"{site['base']}/sign", hint="签到")
    res = await perform_capture(browser, req)
    assert res.ok, res.error
    assert res.found_button is not None
    # 应该选中 data-testid="signin-btn" 那个 button
    assert "签到" in res.found_button["text"]
    # 真的发生了点击 → 测试服务器收到 POST
    assert _post_signs_in_har(res.har, site["base"]) >= 1
    assert site["post_count"]["n"] >= 1


async def test_hint_overrides_default_priority(browser, site):
    # 提示词指向 "每日打卡" (a 标签), 不是 button
    req = CaptureRequest(url=f"{site['base']}/sign", hint="每日打卡")
    res = await perform_capture(browser, req)
    assert res.ok, res.error
    assert "每日打卡" in res.found_button["text"]


async def test_explicit_selector_wins(browser, site):
    req = CaptureRequest(
        url=f"{site['base']}/sign",
        selector='[data-testid="signin-btn"]',
    )
    res = await perform_capture(browser, req)
    assert res.ok, res.error
    assert res.found_button["text"] == "(用户指定)"
    # 用户指定 selector 时不返回候选
    assert res.candidates == []


async def test_explicit_selector_not_found_returns_error(browser, site):
    req = CaptureRequest(
        url=f"{site['base']}/sign",
        selector="#nonexistent-button",
        timeout_ms=8000,
    )
    res = await perform_capture(browser, req)
    assert not res.ok
    assert "selector 点击失败" in res.error or "click" in res.error.lower()


async def test_cookie_string_attached(browser, site):
    # 用 cookie 访问 /protected, 应不被重定向 -> 拿到签到页
    req = CaptureRequest(
        url=f"{site['base']}/protected",
        cookies="session=valid",
        hint="签到",
    )
    res = await perform_capture(browser, req)
    assert res.ok, res.error
    # HAR 里应该能看到带 cookie 的请求
    entries = (res.har.get("log", {}) or {}).get("entries", []) or []
    found_cookie = False
    for e in entries:
        if e["request"]["url"].endswith("/protected"):
            for h in e["request"].get("headers", []):
                if h.get("name", "").lower() == "cookie" and "session=valid" in h.get(
                    "value", ""
                ):
                    found_cookie = True
                    break
    assert found_cookie, "Cookie 未注入到请求中"


async def test_storage_state_failure_detected(browser, site):
    # 没有 cookie 访问 /protected 会被重定向到 /login, 应返回错误
    req = CaptureRequest(url=f"{site['base']}/protected", timeout_ms=15000)
    res = await perform_capture(browser, req)
    assert not res.ok
    assert "登录态" in res.error or "重定向" in res.error


async def test_cross_domain_storage_state_dropped(browser, site):
    """跨域 cookie 应在 sanitize 阶段被剔除, sidecar 不会带跨域凭据。"""
    bad_state = {
        "cookies": [
            # 同域: 应保留并生效
            {
                "name": "session",
                "value": "valid",
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            },
            # 跨域: 必须被剔除
            {
                "name": "leak",
                "value": "x",
                "domain": ".attacker.com",
                "path": "/",
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            },
        ],
        "origins": [],
    }
    req = CaptureRequest(
        url=f"{site['base']}/protected",
        storage_state=bad_state,
        hint="签到",
    )
    res = await perform_capture(browser, req)
    assert res.ok, res.error
    # leak cookie 不应出现在任何请求 header 中
    for e in (res.har.get("log", {}) or {}).get("entries", []) or []:
        for h in e["request"].get("headers", []):
            if h.get("name", "").lower() == "cookie":
                assert "leak=" not in h.get("value", "")


async def test_no_button_returns_candidates(browser, site, tmp_path):
    """页面没有签到按钮时, 返回候选列表供用户挑选。"""
    # 自定义一个没签到按钮的页面 (没用 site fixture 那个)
    html_path = tmp_path / "noop.html"
    html_path.write_text(
        '<html><body><button>登录</button><button>退出</button></body></html>',
        encoding="utf-8",
    )
    req = CaptureRequest(url=f"file://{html_path}", timeout_ms=10000)
    res = await perform_capture(browser, req)
    assert not res.ok
    assert "未找到匹配的签到按钮" in res.error
    # 至少应返回那两个登录/退出按钮作为候选
    assert len(res.candidates) >= 2
    texts = {c["text"] for c in res.candidates}
    assert texts & {"登录", "退出"}


async def test_har_contains_post_after_click(browser, site):
    """点击签到后, 等待 wait_after_click_ms 内的请求都被记录。"""
    req = CaptureRequest(
        url=f"{site['base']}/sign",
        hint="签到",
        wait_after_click_ms=2000,
    )
    res = await perform_capture(browser, req)
    assert res.ok
    n = _post_signs_in_har(res.har, site["base"])
    assert n >= 1, f"未在 HAR 中找到 POST /api/sign, har={json.dumps(res.har)[:500]}"
