"""QD Playwright sidecar - URL → HAR 自动抓包服务。

由 QD 主端 /har/auto_capture 通过 HTTP 调用。
工作流程:
  1. 接受 URL + storage_state (或 cookies) + 可选 hint
  2. 启动 Chromium, 注入登录态
  3. 加载页面, 启发式找签到按钮
  4. 点击, 录制 HAR
  5. 返回 HAR JSON

环境变量:
  HEADLESS               默认 true; debug 时设 false
  MAX_CONCURRENT         同时跑的浏览器会话数, 默认 2
  DEFAULT_TIMEOUT_MS     单次抓包超时, 默认 60000
  ALLOW_HOSTS            逗号分隔的 host 白名单, 不设则允许任何 host
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import random
import re
import socket
import struct
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)
from pydantic import BaseModel, Field, field_validator

from button_finder import JS_FIND_CANDIDATES, pick_button
from security import (
    parse_cookie_str_to_storage_state as _parse_cookie_str_to_storage_state,
    sanitize_storage_state as _sanitize_storage_state,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("qd.playwright")

HEADLESS = os.getenv("HEADLESS", "true").lower() not in ("0", "false", "no")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
DEFAULT_TIMEOUT_MS = int(os.getenv("DEFAULT_TIMEOUT_MS", "60000"))
ALLOW_HOSTS = [h.strip() for h in os.getenv("ALLOW_HOSTS", "").split(",") if h.strip()]
# 默认拒绝私有/环回/链路本地地址（SSRF 防护）。设 BLOCK_PRIVATE_IPS=false 可关闭。
BLOCK_PRIVATE_IPS = os.getenv("BLOCK_PRIVATE_IPS", "true").lower() not in ("0", "false", "no")

# 内部超时常量（毫秒）：不暴露为环境变量，但集中定义便于调整
CLICK_TIMEOUT_MS: int = 10_000          # 单次 click() / get_by_text().click() 超时
POST_CLICK_IDLE_TIMEOUT_MS: int = 5_000  # 点击后等待 networkidle 超时
NETWORK_IDLE_TIMEOUT_MS: int = 10_000   # 页面加载后等待 networkidle 超时


def _normalize_ipv4(hostname: str) -> "ipaddress.IPv4Address | None":
    """尝试将 decimal/hex/octal 形式的 IPv4 字符串规范化为 IPv4Address。

    例如：
      "2130706433"  -> IPv4Address('127.0.0.1')   (十进制整数)
      "0x7f000001"  -> IPv4Address('127.0.0.1')   (十六进制)
      "0177.0.0.1"  -> IPv4Address('127.0.0.1')   (八进制分段)

    浏览器（包括 Chromium）对这些格式的处理与 socket.inet_aton 一致；
    ipaddress.ip_address() 不接受这些格式，因此需要额外一步归一化。
    """
    try:
        # inet_aton 接受 1–4 段 IPv4，包括十进制整数、0x 十六进制、0 八进制
        packed = socket.inet_aton(hostname)
        return ipaddress.IPv4Address(struct.unpack("!I", packed)[0])
    except (OSError, struct.error):
        return None


def _is_blocked_host(hostname: str) -> bool:
    """返回 True 表示该 hostname 命中默认拒绝名单（私有/环回/链路本地/元数据地址）。

    覆盖场景：
    - "localhost" / "0.0.0.0" 字面量
    - 标准点分 IPv4（127.0.0.1, 10.x.x.x, 169.254.x.x 等）
    - 十进制整数 IPv4（2130706433 == 127.0.0.1）
    - 十六进制 IPv4（0x7f000001）/ 八进制分段（0177.0.0.1）
    - IPv6（::1, fe80::, fc00:: 等）

    注意：此函数仅检查字面 IP/localhost。DNS 重绑定攻击（将公共域名解析至内网
    IP）无法在此层防御，需在 chromedp 请求层面做二次 DNS 解析校验（Phase 2 TODO）。
    """
    if not hostname:
        return True
    h = hostname.lower()
    if h in ("localhost", "0.0.0.0"):
        return True

    # 先尝试标准 IPv4/IPv6 解析
    try:
        ip = ipaddress.ip_address(h)
        return (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        pass

    # 尝试非标准 IPv4 格式（十进制整数 / 十六进制 / 八进制）
    # 浏览器会将这些形式解析为 IPv4，Python 的 ipaddress 不接受它们
    norm = _normalize_ipv4(h)
    if norm is not None:
        return (
            norm.is_loopback
            or norm.is_link_local
            or norm.is_private
            or norm.is_multicast
            or norm.is_reserved
            or norm.is_unspecified
        )

    # 普通域名，此层不拦截
    return False

# 反检测注入: 把 navigator.webdriver 抹掉, 防止简单 bot 检测
STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class CaptureRequest(BaseModel):
    url: str = Field(..., description="目标签到页面 URL")
    storage_state: Optional[Dict[str, Any]] = Field(
        None,
        description="Playwright storage state JSON (cookies + localStorage)",
    )
    cookies: Optional[str] = Field(
        None,
        description='退化方案: "k1=v1; k2=v2" 形式的 Cookie 字符串',
    )
    hint: Optional[str] = Field(
        "",
        description="提示词, 例如 '每日签到' / '积分领取', 帮助按钮匹配",
    )
    selector: Optional[str] = Field(
        None,
        description="若用户已知签到按钮 CSS selector, 直接点这个",
    )
    user_agent: Optional[str] = Field(None, description="自定义 UA")
    viewport: Optional[Dict[str, int]] = Field(
        default_factory=lambda: {"width": 1280, "height": 800}
    )
    locale: str = Field("zh-CN")
    timezone_id: str = Field("Asia/Shanghai")
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=5000, le=300000)
    wait_after_click_ms: int = Field(default=3000, ge=0, le=60000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        u = urlparse(v)
        if u.scheme not in ("http", "https"):
            raise ValueError("URL 必须是 http(s)://")
        if not u.hostname:
            raise ValueError("URL 缺少 hostname")
        if ALLOW_HOSTS:
            host = u.hostname.lower()
            if not any(host == h or host.endswith("." + h) for h in ALLOW_HOSTS):
                raise ValueError(f"hostname 不在 ALLOW_HOSTS 白名单内: {host}")
        elif BLOCK_PRIVATE_IPS:
            if _is_blocked_host(u.hostname):
                raise ValueError(
                    f"hostname {u.hostname!r} 命中默认拒绝名单（私有/环回/链路本地地址），"
                    "如需访问内网地址请显式设置 ALLOW_HOSTS 或将 BLOCK_PRIVATE_IPS 设为 false"
                )
        return v


class CaptureResponse(BaseModel):
    ok: bool
    har: Optional[Dict[str, Any]] = None
    actions: List[Dict[str, Any]] = []
    found_button: Optional[Dict[str, Any]] = None
    candidates: List[Dict[str, Any]] = []
    error: Optional[str] = None
    elapsed_ms: int = 0


_browser: Optional[Browser] = None
_playwright_ctx = None
_semaphore: Optional[asyncio.Semaphore] = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _browser, _playwright_ctx, _semaphore
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    logger.info("Starting Playwright (headless=%s, concurrent=%d)", HEADLESS, MAX_CONCURRENT)
    if not ALLOW_HOSTS:
        logger.warning(
            "[security] ALLOW_HOSTS 未设置, sidecar 将接受任意 hostname。"
            " 部署到生产环境时请设置 ALLOW_HOSTS=example.com,foo.com 防 SSRF。"
        )
    _playwright_ctx = await async_playwright().start()
    _browser = await _playwright_ctx.chromium.launch(
        headless=HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    logger.info("Browser ready")
    yield
    logger.info("Shutting down")
    if _browser:
        await _browser.close()
    if _playwright_ctx:
        await _playwright_ctx.stop()


app = FastAPI(title="QD Playwright Sidecar", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "headless": HEADLESS,
        "max_concurrent": MAX_CONCURRENT,
        "browser_ready": _browser is not None and _browser.is_connected(),
    }


async def _find_and_click(page, hint: str, actions: List[Dict[str, Any]]):
    """启发式找签到按钮并点击。返回 (是否点中, 选中的按钮, 候选 top 10)。"""
    candidates = await page.evaluate(JS_FIND_CANDIDATES)
    chosen, top = pick_button(candidates, hint=hint)
    if not chosen:
        return False, None, top
    # 模拟人类延迟, 100-400ms
    await asyncio.sleep(0.1 + random.random() * 0.3)
    selector = chosen["selector"]
    try:
        await page.click(selector, timeout=CLICK_TIMEOUT_MS)
    except PlaywrightError:
        # selector 失效, 退化到按文本点击
        text = chosen["text"]
        try:
            await page.get_by_text(text, exact=False).first.click(timeout=CLICK_TIMEOUT_MS)
        except PlaywrightError as e:
            actions.append({"type": "click_failed", "selector": selector, "error": str(e)})
            return False, chosen, top
    actions.append({"type": "click", "selector": selector, "text": chosen["text"]})
    return True, chosen, top


async def perform_capture(
    browser: Browser,
    req: CaptureRequest,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> CaptureResponse:
    """核心抓包逻辑, 不依赖全局状态, 便于集成测试。

    Args:
        browser: Playwright 已启动的 Browser 实例
        req: CaptureRequest pydantic 模型
        semaphore: 可选并发限流; 若 None 则不限流
    """
    started = time.time()
    actions: List[Dict[str, Any]] = []

    storage_state = req.storage_state
    if not storage_state and req.cookies:
        storage_state = _parse_cookie_str_to_storage_state(req.cookies, req.url)
    if storage_state:
        storage_state = _sanitize_storage_state(storage_state, req.url)

    har_fd, har_path = tempfile.mkstemp(suffix=".har", prefix="qd_capture_")
    os.close(har_fd)

    async def _run() -> CaptureResponse:
        context = await browser.new_context(
            user_agent=req.user_agent or DEFAULT_UA,
            viewport=req.viewport or {"width": 1280, "height": 800},
            locale=req.locale,
            timezone_id=req.timezone_id,
            storage_state=storage_state,
            record_har_path=har_path,
            record_har_content="embed",
        )
        await context.add_init_script(STEALTH_INIT_JS)
        page = await context.new_page()
        page.set_default_timeout(req.timeout_ms)

        try:
            actions.append({"type": "navigate", "url": req.url})
            try:
                await page.goto(req.url, wait_until="domcontentloaded", timeout=req.timeout_ms)
            except PlaywrightTimeout:
                actions.append({"type": "navigate_timeout"})
            try:
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass

            current = page.url
            if re.search(r"login|signin|sign-in|auth", current, re.I) and not re.search(
                r"login|signin|sign-in|auth", req.url, re.I
            ):
                await context.close()
                return CaptureResponse(
                    ok=False,
                    error=f"页面被重定向到 {current}, 登录态可能已失效, 请重新提供 storage_state",
                    actions=actions,
                    elapsed_ms=int((time.time() - started) * 1000),
                )

            if req.selector:
                try:
                    await page.click(req.selector, timeout=CLICK_TIMEOUT_MS)
                    actions.append({"type": "click", "selector": req.selector, "manual": True})
                    chosen = {"selector": req.selector, "text": "(用户指定)"}
                    candidates: List[Dict[str, Any]] = []
                except PlaywrightError as e:
                    await context.close()
                    return CaptureResponse(
                        ok=False,
                        error=f"用户指定的 selector 点击失败: {e}",
                        actions=actions,
                        elapsed_ms=int((time.time() - started) * 1000),
                    )
            else:
                clicked, chosen, candidates = await _find_and_click(
                    page, req.hint or "", actions
                )
                if not clicked:
                    await context.close()
                    return CaptureResponse(
                        ok=False,
                        error="未找到匹配的签到按钮, 请检查 hint 或手动指定 selector",
                        actions=actions,
                        candidates=candidates,
                        elapsed_ms=int((time.time() - started) * 1000),
                    )

            await asyncio.sleep(req.wait_after_click_ms / 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=POST_CLICK_IDLE_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass

            await context.close()

            with open(har_path, "r", encoding="utf-8") as f:
                har_data = json.load(f)

            return CaptureResponse(
                ok=True,
                har=har_data,
                actions=actions,
                found_button=chosen,
                candidates=candidates,
                elapsed_ms=int((time.time() - started) * 1000),
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("capture failed: %s", e)
            try:
                await context.close()
            except Exception:  # pragma: no cover
                pass
            return CaptureResponse(
                ok=False,
                error=f"内部异常: {e}",
                actions=actions,
                elapsed_ms=int((time.time() - started) * 1000),
            )
        finally:
            try:
                os.unlink(har_path)
            except OSError:
                pass

    if semaphore is not None:
        async with semaphore:
            return await _run()
    return await _run()


@app.post("/capture", response_model=CaptureResponse)
async def capture(req: CaptureRequest) -> CaptureResponse:
    if not _browser or not _browser.is_connected():
        raise HTTPException(503, "Browser not ready")
    return await perform_capture(_browser, req, semaphore=_semaphore)
