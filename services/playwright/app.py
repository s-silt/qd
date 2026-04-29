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
import json
import logging
import os
import random
import re
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("qd.playwright")

HEADLESS = os.getenv("HEADLESS", "true").lower() not in ("0", "false", "no")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
DEFAULT_TIMEOUT_MS = int(os.getenv("DEFAULT_TIMEOUT_MS", "60000"))
ALLOW_HOSTS = [h.strip() for h in os.getenv("ALLOW_HOSTS", "").split(",") if h.strip()]

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


def _parse_cookie_str_to_storage_state(cookie_str: str, url: str) -> Dict[str, Any]:
    """把简单 'k1=v1; k2=v2' 转换为 Playwright storage_state cookies 列表。"""
    parsed = urlparse(url)
    domain = parsed.hostname or ""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": "." + domain if not domain.startswith(".") else domain,
                "path": "/",
                "httpOnly": False,
                "secure": parsed.scheme == "https",
                "sameSite": "Lax",
            }
        )
    return {"cookies": cookies, "origins": []}


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
        await page.click(selector, timeout=10000)
    except PlaywrightError:
        # selector 失效, 退化到按文本点击
        text = chosen["text"]
        try:
            await page.get_by_text(text, exact=False).first.click(timeout=10000)
        except PlaywrightError as e:
            actions.append({"type": "click_failed", "selector": selector, "error": str(e)})
            return False, chosen, top
    actions.append({"type": "click", "selector": selector, "text": chosen["text"]})
    return True, chosen, top


@app.post("/capture", response_model=CaptureResponse)
async def capture(req: CaptureRequest) -> CaptureResponse:
    if not _browser or not _browser.is_connected():
        raise HTTPException(503, "Browser not ready")

    started = time.time()
    actions: List[Dict[str, Any]] = []

    storage_state = req.storage_state
    if not storage_state and req.cookies:
        storage_state = _parse_cookie_str_to_storage_state(req.cookies, req.url)

    # 写到临时文件: Playwright 接受 storage_state=path 或 dict
    har_fd, har_path = tempfile.mkstemp(suffix=".har", prefix="qd_capture_")
    os.close(har_fd)

    async with _semaphore:  # type: ignore[union-attr]
        context = await _browser.new_context(
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
            # 等网络空闲一段（不强制全静止, 部分站点有长轮询）
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass

            # 检测是否被踢到登录页（storage_state 失效）
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

            # 1. 用户给了 selector 直接点
            if req.selector:
                try:
                    await page.click(req.selector, timeout=10000)
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
                # 2. 启发式
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

            # 等点击后的请求完成
            await asyncio.sleep(req.wait_after_click_ms / 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeout:
                pass

            await context.close()

            # 读出 HAR
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
